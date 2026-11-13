"""冷热迁移计划的提交与执行治理。

关键规则：
- 同一湖节点的同一份迁移计划并发提交时，只允许一个执行记录落库（线程锁 + 节点唯一活动计划）。
- 计划本身由容量引擎按当前数据现算，重复提交幂等，返回同一条执行记录。
- 冷数据迁移失败后，重试从失败分区继续，已成功/已释放的分区不会重复处理。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.services import capacity
from app.store import store

PLAN_MODULE = "migration_plan"
RUNTIME_MODULE = "partition_runtime"

PLAN_DRAFT = "待执行"
PLAN_RUNNING = "执行中"
PLAN_PARTIAL = "部分失败"
PLAN_DONE = "已完成"
PLAN_FAILED = "失败"

# 同一时刻只允许一个提交/执行临界区，保证并发提交只有一条记录落库。
_submit_lock = threading.Lock()


def _now() -> str:
    return datetime(2026, 11, 13, 10, 0, 0).isoformat()


def _runtime_rows() -> list[dict[str, Any]]:
    return store.rows(RUNTIME_MODULE)


def _plan_rows() -> list[dict[str, Any]]:
    return store.rows(PLAN_MODULE)


def _upsert_runtime(partition_no: str, status: str, detail: str) -> None:
    for row in _runtime_rows():
        if row["分区编号"] == partition_no:
            row["状态"] = status
            row["说明"] = detail
            row["更新时间"] = _now()
            return
    _runtime_rows().append({
        "分区编号": partition_no,
        "状态": status,
        "说明": detail,
        "更新时间": _now(),
    })


def _get_runtime(partition_no: str) -> dict[str, Any] | None:
    for row in _runtime_rows():
        if row["分区编号"] == partition_no:
            return row
    return None


def _snapshot_steps(node_code: str, as_of: str | None, cycles: int) -> list[dict[str, Any]]:
    """重新走一遍分区策略→湖节点取数链路，拿到当前口径下的迁移顺序。"""
    node = capacity.node_capacity(node_code, as_of=as_of, cycles=cycles)
    if node is None:
        return []
    return list(node["迁移顺序"])


def submit_plan(node_code: str, *, as_of: str | None = None,
                cycles: int = capacity.DEFAULT_CYCLES) -> tuple[dict[str, Any] | None, str, int]:
    """提交（或幂等取回）某湖节点的迁移计划。

    返回 (计划, 说明, HTTP 状态码)。并发提交同一节点时，后来的请求拿到已落库的同一条记录，
    不会产生第二条执行记录。
    """
    node = capacity.node_capacity(node_code, as_of=as_of, cycles=cycles)
    if node is None:
        return None, f"湖存储节点 {node_code} 不存在", 404

    with _submit_lock:
        active_codes = {PLAN_DRAFT, PLAN_RUNNING, PLAN_PARTIAL}
        for plan in _plan_rows():
            if plan["节点编号"] == node_code and plan["状态"] in active_codes:
                return plan, "该节点已有迁移执行记录，重复提交已幂等返回同一条记录", 200

        steps = _snapshot_steps(node_code, as_of, cycles)
        plan = {
            "计划编号": f"PLAN-{node_code}",
            "节点编号": node_code,
            "状态": PLAN_DRAFT,
            "基准日期": as_of or capacity.DEFAULT_AS_OF.isoformat(),
            "步骤总数": len(steps),
            "已完成数": 0,
            "失败数": 0,
            "失败分区": None,
            "预计释放量GB": node["预计释放量GB"],
            "冷数据迁移量GB": node["冷数据迁移量GB"],
            "冷热分层收益GB": node["冷热分层收益GB"],
            "创建时间": _now(),
            "更新时间": _now(),
            "执行记录": [
                {
                    "顺序号": step["顺序号"],
                    "动作": step["动作"],
                    "分区编号": step["分区编号"],
                    "状态": "待执行",
                    "回收容量GB": step["回收容量GB"],
                    "说明": "",
                }
                for step in steps
            ],
        }
        _plan_rows().append(plan)
        return plan, "迁移计划已生成，执行记录唯一落库", 201


def list_plans() -> list[dict[str, Any]]:
    return _plan_rows()


def get_plan(plan_no: str) -> dict[str, Any] | None:
    for plan in _plan_rows():
        if plan["计划编号"] == plan_no:
            return plan
    return None


def _mark(plan: dict[str, Any], index: int, status: str, detail: str) -> None:
    record = plan["执行记录"][index]
    record["状态"] = status
    record["说明"] = detail
    runtime_status = (
        capacity.TIER_RELEASED if record["动作"] == "释放分区" else capacity.TIER_MIGRATED
    )
    if status == "成功":
        _upsert_runtime(record["分区编号"], runtime_status, detail)
    elif status == "失败":
        _upsert_runtime(record["分区编号"], "迁移失败", detail)


def _refresh_plan_state(plan: dict[str, Any]) -> None:
    done = sum(1 for item in plan["执行记录"] if item["状态"] == "成功")
    failed = [item for item in plan["执行记录"] if item["状态"] == "失败"]
    plan["已完成数"] = done
    plan["失败数"] = len(failed)
    plan["失败分区"] = failed[0]["分区编号"] if failed else None
    if failed and done + len(failed) < plan["步骤总数"]:
        plan["状态"] = PLAN_PARTIAL
    elif failed:
        plan["状态"] = PLAN_FAILED
    elif done == plan["步骤总数"]:
        plan["状态"] = PLAN_DONE
    else:
        plan["状态"] = PLAN_RUNNING
    plan["更新时间"] = _now()


def execute_plan(plan_no: str, *, fail_partition: str | None = None) -> tuple[dict[str, Any] | None, str, int]:
    """执行迁移计划。

    - 从第一个未成功的分区开始顺序执行，失败立即停在该分区（断点），后续重试接着跑。
    - fail_partition 用于演示/演练：指定分区第一次执行时模拟迁移失败。
    """
    with _submit_lock:
        plan = get_plan(plan_no)
        if plan is None:
            return None, f"迁移计划 {plan_no} 不存在", 404
        if plan["状态"] == PLAN_RUNNING:
            return None, "计划正在执行中，并发执行已被拦截", 409
        if plan["状态"] == PLAN_DONE:
            return plan, "计划已全部完成，重复执行幂等返回", 200

        plan["状态"] = PLAN_RUNNING
        started = False
        for index, record in enumerate(plan["执行记录"]):
            if record["状态"] == "成功":
                continue
            started = True
            # 断点续跑：失败分区在没有显式 fail_partition 的重试中视为恢复，继续向下执行。
            if fail_partition and record["分区编号"] == fail_partition:
                runtime = _get_runtime(record["分区编号"])
                already_failed = bool(runtime and runtime.get("状态") == "迁移失败")
                if not already_failed:
                    _mark(plan, index, "失败", f"模拟迁移失败：{record['分区编号']} 写入冷存储超时")
                    _refresh_plan_state(plan)
                    return plan, f"分区 {record['分区编号']} 迁移失败，已停在失败分区，可重试续跑", 200

            _mark(plan, index, "成功", f"{record['动作']}完成")
            _refresh_plan_state(plan)

        if not started:
            plan["状态"] = PLAN_DONE
        _refresh_plan_state(plan)
        if plan["状态"] == PLAN_DONE:
            return plan, "迁移计划全部执行完成", 200
        return plan, "迁移计划执行结束，存在失败分区", 200


def retry_plan(plan_no: str) -> tuple[dict[str, Any] | None, str, int]:
    """从失败分区重试：清掉失败标记后继续执行剩余分区，已成功分区不会重做。"""
    plan = get_plan(plan_no)
    if plan is None:
        return None, f"迁移计划 {plan_no} 不存在", 404
    if plan["状态"] not in (PLAN_PARTIAL, PLAN_FAILED):
        return None, f"计划当前状态为「{plan['状态']}」，无需重试", 409

    failed_records = [item for item in plan["执行记录"] if item["状态"] == "失败"]
    for record in failed_records:
        # 失败分区解除失败标记，重试时重新执行；运行态同步清掉失败痕迹。
        record["状态"] = "待执行"
        record["说明"] = "失败分区已重置，等待断点重试"
        runtime = _get_runtime(record["分区编号"])
        if runtime:
            runtime["状态"] = "待重试"
            runtime["说明"] = record["说明"]
            runtime["更新时间"] = _now()
    # 注意：这里不刷新计划状态，保留「部分失败/失败」让 execute_plan 的并发守卫放行，
    # 状态由 execute_plan 进入临界区后统一接管。
    # 不带 fail_partition，失败分区本次正常执行，随后继续处理后续分区。
    return execute_plan(plan_no)
