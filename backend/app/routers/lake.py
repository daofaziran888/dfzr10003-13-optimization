"""数据湖存储接口：维护湖存储节点，覆盖启动节点、暂停节点、归档节点等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import capacity, migration
from app.services.lake import LakeService

router = APIRouter(prefix="/api/lake", tags=["数据湖存储"])

service = LakeService()

LIST_FIELDS = ["节点编号", "存储路径", "文件格式", "压缩方式", "总容量", "已用容量", "访问策略", "节点状态"]
STATUSES = ["草稿", "运行中", "已暂停", "已归档"]


@router.get("/capacity/overview")
def capacity_overview(
    as_of: str | None = Query(default=None, description="容量重算基准日，默认 2026-11-13"),
    cycles: int = Query(default=capacity.DEFAULT_CYCLES, description="预测周期数，每周期 7 天"),
) -> dict[str, Any]:
    """湖存储取数链路统一出口：分区策略→分区实例→占用引用→湖节点容量与冷热收益。

    分区页也读取同一份结果，保证两页预计占用、释放量、不可迁移原因完全一致。
    """
    return capacity.capacity_overview(as_of=as_of, cycles=cycles)


@router.get("/capacity/{node_code}")
def node_capacity(
    node_code: str,
    as_of: str | None = Query(default=None),
    cycles: int = capacity.DEFAULT_CYCLES,
) -> dict[str, Any]:
    """单个湖节点的容量治理口径（含迁移顺序、容量预测、不可迁移原因）。"""
    report = capacity.node_capacity(node_code, as_of=as_of, cycles=cycles)
    if report is None:
        raise HTTPException(status_code=404, detail=f"湖存储节点 {node_code} 不存在")
    return report


@router.post("/capacity/{node_code}/migration-plan")
def submit_migration_plan(
    node_code: str,
    as_of: str | None = Query(default=None),
    cycles: int = capacity.DEFAULT_CYCLES,
) -> dict[str, Any]:
    """提交迁移计划：同节点并发提交只允许一条执行记录落库，重复提交幂等返回同一条。"""
    plan, message, status_code = migration.submit_plan(node_code, as_of=as_of, cycles=cycles)
    if plan is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "plan": plan}


@router.get("/migration-plans")
def list_migration_plans() -> dict[str, Any]:
    """查看全部迁移执行记录。"""
    return {"items": migration.list_plans()}


@router.get("/migration-plans/{plan_no}")
def get_migration_plan(plan_no: str) -> dict[str, Any]:
    plan = migration.get_plan(plan_no)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"迁移计划 {plan_no} 不存在")
    return plan


@router.post("/migration-plans/{plan_no}/execute")
def execute_migration_plan(
    plan_no: str,
    payload: EntryPayload | None = None,
) -> dict[str, Any]:
    """执行迁移计划；可在 values.fail_partition 传入分区编号模拟该分区迁移失败。"""
    fail_partition = None
    if payload is not None:
        fail_partition = str(payload.values.get("fail_partition") or "").strip() or None
    plan, message, status_code = migration.execute_plan(plan_no, fail_partition=fail_partition)
    if plan is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "plan": plan}


@router.post("/migration-plans/{plan_no}/retry")
def retry_migration_plan(plan_no: str) -> dict[str, Any]:
    """从失败分区断点重试，接着处理后续分区，已成功分区不重做。"""
    plan, message, status_code = migration.retry_plan(plan_no)
    if plan is None:
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "plan": plan}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按节点编号与状态过滤数据湖存储列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条湖存储节点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"湖存储节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条湖存储节点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="湖存储节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条湖存储节点执行启动节点、暂停节点、归档节点；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据湖存储清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "lake", "total": total, "items": items}
