"""湖存储容量治理引擎。

取数链路（只沿既有模块走，不另造数据源）：

    分区策略(partition)
        └─ 按 保留天数 / 预建天数 / 冷热分离 推导分区实例
            └─ 分区被谁引用：快照(snapshot) / 历史重跑(backfill) / 质量校验(quality)
                └─ 分区按 存储节点 汇总到湖节点(lake)
                    └─ 文件格式 / 压缩方式 决定单分区物理占用
                        └─ 结合日增容量 / 近期增长率 预测后续周期与冷热收益

设计约束：
- 计算过程是纯函数：同一份数据、同一 as_of 日期，重复计算得到完全一致的结果（幂等）。
- 被快照、历史重跑、质量校验引用的分区不可迁移或释放，原因逐条落盘返回。
- 策略侧与节点侧容量口径冲突时，取“已生效策略与节点当前引用中占用较高”的一侧。
- 待审批策略（如 PART-0002）只计入预测，不计入当前占用，并在结果里单独标注。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

# 演示环境固定基准日，保证任何时刻重算结果一致；接口也允许显式传入 as_of 覆盖。
DEFAULT_AS_OF = date(2026, 11, 13)
CYCLE_DAYS = 7
DEFAULT_CYCLES = 4
MAX_CYCLES = 8

# 分区生命周期（天）
TIER_HOT = "热"
TIER_COLD = "冷"
TIER_EXPIRED = "待释放"
TIER_PREBUILT = "预建"
TIER_RELEASED = "已释放"
TIER_MIGRATED = "已迁移"

# 文件格式 × 压缩方式 的物理占用系数（以 Parquet+ZSTD 为基准 1.0，越大越占空间）。
# 键统一大写，format_factor 查找时同样归一化为大写，避免大小写不一致导致静默落到默认值。
FORMAT_FACTORS: dict[tuple[str, str], float] = {
    ("PARQUET", "ZSTD"): 1.00,
    ("PARQUET", "SNAPPY"): 1.15,
    ("PARQUET", "GZIP"): 1.08,
    ("ORC", "ZSTD"): 1.05,
    ("ORC", "SNAPPY"): 1.12,
    ("ORC", "GZIP"): 1.10,
    ("TEXTFILE", "GZIP"): 2.40,
    ("TEXTFILE", "SNAPPY"): 2.70,
    ("TEXTFILE", "NONE"): 3.20,
}
DEFAULT_FACTOR = 1.5
# 冷分层后的占用折算系数：冷存储介质 + 再压缩后保留的物理比例。
COLD_TIER_FACTOR = 0.35

# 引用方处于这些状态时，会锁住对应分区。
ACTIVE_SNAPSHOT_STATUS = {"草稿", "运行中"}
ACTIVE_BACKFILL_STATUS = {"草稿", "运行中"}
ACTIVE_QUALITY_STATUS = {"草稿", "运行中"}
QUALITY_LOOKBACK_DAYS = 7

GB = "GB"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _round(value: float) -> float:
    return round(value + 1e-9, 2)


def format_factor(file_format: str, compression: str) -> float:
    key = (str(file_format or "").strip().upper(), str(compression or "").strip().upper())
    return FORMAT_FACTORS.get(key, DEFAULT_FACTOR)


@dataclass
class BlockRef:
    """一条对分区的占用引用（快照 / 历史重跑 / 质量校验）。"""

    kind: str
    code: str
    table: str
    start: date | None
    end: date | None

    def covers(self, partition_date: date) -> bool:
        start = self.start or partition_date
        end = self.end or self.start or partition_date
        return start <= partition_date <= end


def _active_block_refs(as_of: date) -> list[BlockRef]:
    """从既有三个模块取“正在使用中”的引用，作为不可迁移/不可释放的判定来源。"""
    refs: list[BlockRef] = []

    for row in store.rows("snapshot"):
        if row.get("快照状态") in ACTIVE_SNAPSHOT_STATUS or row.get("status") in ACTIVE_SNAPSHOT_STATUS:
            snap_day = _parse_date(row.get("快照时间"))
            refs.append(BlockRef("快照", str(row.get("快照编号")), str(row.get("表名称")), snap_day, snap_day))

    for row in store.rows("backfill"):
        if row.get("重跑状态") in ACTIVE_BACKFILL_STATUS or row.get("status") in ACTIVE_BACKFILL_STATUS:
            refs.append(BlockRef(
                "历史重跑",
                str(row.get("批次编号")),
                str(row.get("目标表名")),
                _parse_date(row.get("开始日期")),
                _parse_date(row.get("结束日期")),
            ))

    as_of = as_of
    for row in store.rows("quality"):
        if row.get("status") in ACTIVE_QUALITY_STATUS:
            last_check = _parse_date(row.get("最近校验")) or as_of
            refs.append(BlockRef(
                "质量校验",
                str(row.get("规则编号")),
                str(row.get("表名称")),
                last_check - timedelta(days=QUALITY_LOOKBACK_DAYS - 1),
                last_check,
            ))
    return refs


def _as_of_date(as_of: str | date | None) -> date:
    if isinstance(as_of, date):
        return as_of
    return _parse_date(as_of) or DEFAULT_AS_OF


def _block_reasons(table: str, partition_date: date, refs: list[BlockRef]) -> list[str]:
    reasons: list[str] = []
    for ref in refs:
        if ref.table == table and ref.covers(partition_date):
            if ref.kind == "快照":
                reasons.append(f"快照 {ref.code} 正在引用")
            elif ref.kind == "历史重跑":
                reasons.append(f"历史重跑 {ref.code} 区间占用")
            else:
                reasons.append(f"质量校验 {ref.code} 近{QUALITY_LOOKBACK_DAYS}天扫描")
    # 去重，保持稳定顺序，保证幂等输出。
    return sorted(set(reasons))


def _partition_age_tier(age_days: int, retention: int, hot_window: int) -> str:
    if age_days < 0:
        return TIER_PREBUILT
    if age_days > retention:
        return TIER_EXPIRED
    if age_days > hot_window:
        return TIER_COLD
    return TIER_HOT


def build_partitions(strategy: dict[str, Any], as_of: date, refs: list[BlockRef]) -> list[dict[str, Any]]:
    """按一条分区策略推导其全量分区实例（历史分区 + 预建分区）。"""
    retention = _to_int(strategy.get("保留天数"))
    prebuild = _to_int(strategy.get("预建天数"))
    hot_window = _to_int(strategy.get("冷热分离"))
    daily_gb = _to_float(strategy.get("日增容量GB"))
    table = str(strategy.get("表名称"))
    node_code = str(strategy.get("存储节点"))

    strategy_factor = format_factor(strategy.get("文件格式"), strategy.get("压缩方式"))
    node = _find_lake_node(node_code)
    node_factor = format_factor(node.get("文件格式"), node.get("压缩方式")) if node else DEFAULT_FACTOR
    # 口径冲突：策略侧与节点当前引用侧取占用较高的一侧，并记录来源。
    if strategy_factor >= node_factor:
        effective_factor, factor_source = strategy_factor, "策略"
    else:
        effective_factor, factor_source = node_factor, "湖节点"

    partitions: list[dict[str, Any]] = []
    # 历史分区从保留窗口最旧一天一直建到基准日，再补预建分区。
    for age in range(retention + 1, -prebuild - 1, -1):
        part_date = as_of - timedelta(days=age)
        tier = _partition_age_tier(age, retention, hot_window)
        size_gb = _round(daily_gb * effective_factor)
        blocked_reasons = _block_reasons(table, part_date, refs)
        partitions.append({
            "分区编号": f"{table}/dt={part_date.isoformat()}",
            "分区日期": part_date.isoformat(),
            "表名称": table,
            "存储节点": node_code,
            "配置编号": strategy.get("配置编号"),
            "年龄天数": age,
            "分层": tier,
            "单分区占用GB": size_gb,
            "占用系数": effective_factor,
            "系数口径": factor_source,
            "不可迁移原因": blocked_reasons,
            "可迁移": not blocked_reasons,
            "运行态": None,
            "运行态占用GB": None,
        })
    return partitions


def _find_lake_node(node_code: str) -> dict[str, Any] | None:
    for row in store.rows("lake"):
        if row.get("节点编号") == node_code:
            return row
    return None


def _apply_runtime(partitions: list[dict[str, Any]]) -> None:
    """把迁移/释放的执行记录折算回分区运行态，保证已执行结果参与后续重算。"""
    states = {item["分区编号"]: item for item in store.rows("partition_runtime")}
    for part in partitions:
        state = states.get(part["分区编号"])
        if not state:
            continue
        part["运行态"] = state.get("状态")
        if state.get("状态") == TIER_RELEASED:
            part["运行态占用GB"] = 0.0
        elif state.get("状态") == TIER_MIGRATED:
            part["运行态占用GB"] = _round(part["单分区占用GB"] * COLD_TIER_FACTOR)
        else:
            part["运行态占用GB"] = part["单分区占用GB"]


def _physical_gb(part: dict[str, Any]) -> float:
    if part.get("运行态占用GB") is not None:
        return float(part["运行态占用GB"])
    return float(part["单分区占用GB"])


def _strategy_report(strategy: dict[str, Any], as_of: date, refs: list[BlockRef],
                     cycles: int) -> dict[str, Any]:
    partitions = build_partitions(strategy, as_of, refs)
    _apply_runtime(partitions)

    effective = strategy.get("status") == "已生效"
    retention = _to_int(strategy.get("保留天数"))
    prebuild = _to_int(strategy.get("预建天数"))
    hot_window = _to_int(strategy.get("冷热分离"))
    growth = _to_float(strategy.get("近期增长率"))
    daily_gb = _to_float(strategy.get("日增容量GB"))
    factor = max(p["占用系数"] for p in partitions) if partitions else DEFAULT_FACTOR

    physical_parts = [p for p in partitions if p["年龄天数"] >= 0]
    prebuilt_parts = [p for p in partitions if p["年龄天数"] < 0]
    hot_parts = [p for p in physical_parts if p["分层"] == TIER_HOT]
    cold_parts = [p for p in physical_parts if p["分层"] == TIER_COLD]
    expired_parts = [p for p in physical_parts if p["分层"] == TIER_EXPIRED]

    current_gb = _round(sum(_physical_gb(p) for p in physical_parts))
    prebuilt_gb = _round(sum(_physical_gb(p) for p in prebuilt_parts))
    projected_gb = _round(current_gb + prebuilt_gb)

    releasable = [p for p in expired_parts if p["可迁移"] and p["运行态"] != TIER_RELEASED]
    blocked_expired = [p for p in expired_parts if not p["可迁移"]]
    release_gb = _round(sum(p["单分区占用GB"] for p in releasable))
    blocked_expired_gb = _round(sum(p["单分区占用GB"] for p in blocked_expired))

    migratable_cold = [p for p in cold_parts if p["可迁移"] and p["运行态"] not in (TIER_MIGRATED,)]
    cold_migrate_gb = _round(sum(p["单分区占用GB"] for p in migratable_cold))
    cold_benefit_gb = _round(cold_migrate_gb * (1 - COLD_TIER_FACTOR))
    blocked_cold = [p for p in cold_parts if not p["可迁移"]]
    blocked_cold_gb = _round(sum(p["单分区占用GB"] for p in blocked_cold))

    # 未来周期预测：每周期新增 CYCLE_DAYS 个分区，增长率按周期复利；
    # 同时每周期有 CYCLE_DAYS 个分区滑出保留窗口（可释放），预建分区滚动补位不重复计。
    forecast: list[dict[str, Any]] = []
    running = projected_gb
    cycle_daily = daily_gb * factor
    for idx in range(1, cycles + 1):
        grown_daily = cycle_daily * ((1 + growth) ** (idx - 1))
        incoming = _round(grown_daily * CYCLE_DAYS)
        # 到期释放量按当前“可释放 / 全部到期”比例折算，锁住的分区释放不掉。
        expired_total = len(expired_parts) + idx * CYCLE_DAYS
        releasable_ratio = (
            (len(releasable) + max(0, expired_total - len(expired_parts))) / expired_total
            if expired_total
            else 0.0
        )
        cycle_release = _round(incoming * releasable_ratio)
        running = _round(running + incoming - cycle_release)
        forecast.append({
            "周期": idx,
            "周期结束日": (as_of + timedelta(days=CYCLE_DAYS * idx)).isoformat(),
            "新增占用GB": incoming,
            "到期可释放GB": cycle_release,
            "预计占用GB": running,
        })

    # 不可迁移原因汇总（分区页与湖页读同一份口径）。
    reason_map: dict[str, int] = {}
    for part in partitions:
        for reason in part["不可迁移原因"]:
            reason_map[reason] = reason_map.get(reason, 0) + 1
    immovable_reasons = [
        {"原因": reason, "涉及分区数": count}
        for reason, count in sorted(reason_map.items())
    ]

    return {
        "配置编号": strategy.get("配置编号"),
        "表名称": strategy.get("表名称"),
        "存储节点": strategy.get("存储节点"),
        "策略状态": strategy.get("status"),
        "已生效": effective,
        "保留天数": retention,
        "预建天数": prebuild,
        "冷热分离天数": hot_window,
        "文件格式": strategy.get("文件格式"),
        "压缩方式": strategy.get("压缩方式"),
        "业务优先级": _to_int(strategy.get("业务优先级"), default=99),
        "日增容量GB": daily_gb,
        "近期增长率": growth,
        "占用系数": factor,
        "系数口径": partitions[0]["系数口径"] if partitions else None,
        "当前占用GB": current_gb,
        "预建占用GB": prebuilt_gb,
        "预计占用GB": projected_gb,
        "预计释放量GB": release_gb,
        "冷数据迁移量GB": cold_migrate_gb,
        "冷热分层收益GB": cold_benefit_gb,
        "锁住的到期分区GB": blocked_expired_gb,
        "锁住的冷分区GB": blocked_cold_gb,
        "不可迁移原因": immovable_reasons,
        "分区实例": partitions,
        "容量预测": forecast,
    }


def _migration_steps(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成可执行迁移顺序：先释放到期分区，再迁移冷分区。

    容量紧张时按业务优先级保留热数据：优先级数字小（越重要）的热分区始终不动，
    迁移序列优先处理优先级数字大（可延后）的策略，同策略内按分区年龄从旧到新。
    """
    steps: list[dict[str, Any]] = []
    seq = 0
    ordered = sorted(reports, key=lambda r: (-r["业务优先级"], r["配置编号"]))
    for report in ordered:
        parts = report["分区实例"]
        expired = sorted(
            (p for p in parts if p["分层"] == TIER_EXPIRED and p["可迁移"]
             and p["运行态"] != TIER_RELEASED),
            key=lambda p: (-p["年龄天数"], p["分区编号"]),
        )
        for part in expired:
            seq += 1
            steps.append({
                "顺序号": seq,
                "动作": "释放分区",
                "分区编号": part["分区编号"],
                "配置编号": report["配置编号"],
                "存储节点": report["存储节点"],
                "业务优先级": report["业务优先级"],
                "回收容量GB": part["单分区占用GB"],
                "不可迁移原因": part["不可迁移原因"],
            })
        cold = sorted(
            (p for p in parts if p["分层"] == TIER_COLD and p["可迁移"]
             and p["运行态"] != TIER_MIGRATED),
            key=lambda p: (-p["年龄天数"], p["分区编号"]),
        )
        for part in cold:
            seq += 1
            steps.append({
                "顺序号": seq,
                "动作": "迁移冷分区",
                "分区编号": part["分区编号"],
                "配置编号": report["配置编号"],
                "存储节点": report["存储节点"],
                "业务优先级": report["业务优先级"],
                "回收容量GB": _round(part["单分区占用GB"] * (1 - COLD_TIER_FACTOR)),
                "不可迁移原因": part["不可迁移原因"],
            })
    return steps


def capacity_overview(*, as_of: str | date | None = None, cycles: int = DEFAULT_CYCLES) -> dict[str, Any]:
    """全量容量治理口径：湖页与分区页共用这一个出口，保证两侧数字完全一致。"""
    as_of_date = _as_of_date(as_of)
    cycles = max(1, min(_to_int(cycles, DEFAULT_CYCLES) or DEFAULT_CYCLES, MAX_CYCLES))
    refs = _active_block_refs(as_of_date)

    strategies = store.rows("partition")
    reports = [_strategy_report(s, as_of_date, refs, cycles) for s in strategies]

    nodes: list[dict[str, Any]] = []
    for node in store.rows("lake"):
        node_code = node.get("节点编号")
        node_reports = [r for r in reports if r["存储节点"] == node_code]
        effective_reports = [r for r in node_reports if r["已生效"]]
        pending_reports = [r for r in node_reports if not r["已生效"]]

        current_gb = _round(sum(r["当前占用GB"] for r in effective_reports))
        prebuilt_gb = _round(sum(r["预建占用GB"] for r in effective_reports))
        projected_gb = _round(sum(r["预计占用GB"] for r in effective_reports))
        release_gb = _round(sum(r["预计释放量GB"] for r in effective_reports))
        cold_gb = _round(sum(r["冷数据迁移量GB"] for r in effective_reports))
        benefit_gb = _round(sum(r["冷热分层收益GB"] for r in effective_reports))
        blocked_gb = _round(
            sum(r["锁住的到期分区GB"] + r["锁住的冷分区GB"] for r in effective_reports)
        )
        pending_projected_gb = _round(sum(r["预计占用GB"] for r in pending_reports))

        total_capacity = _to_float(node.get("总容量"))
        used_capacity = _to_float(node.get("已用容量"))
        # 口径冲突：已生效策略推算占用 与 节点当前登记已用容量，取较高一侧。
        reference_gb = max(used_capacity, projected_gb)
        reference_source = "湖节点登记已用容量" if used_capacity >= projected_gb else "已生效分区策略推算"
        headroom_gb = _round(total_capacity - reference_gb)
        usage_ratio = _round((reference_gb / total_capacity) * 100) if total_capacity else 0.0

        reason_map: dict[str, int] = {}
        for report in effective_reports:
            for item in report["不可迁移原因"]:
                reason_map[item["原因"]] = reason_map.get(item["原因"], 0) + item["涉及分区数"]
        immovable_reasons = [
            {"原因": reason, "涉及分区数": count}
            for reason, count in sorted(reason_map.items())
        ]

        # 节点级预测：合并各策略的周期预测；待审批策略只进预测、不进当前占用。
        forecast: list[dict[str, Any]] = []
        for idx in range(cycles):
            incoming = _round(sum(r["容量预测"][idx]["新增占用GB"] for r in node_reports))
            cycle_release = _round(sum(r["容量预测"][idx]["到期可释放GB"] for r in effective_reports))
            base = reference_gb if idx == 0 else forecast[idx - 1]["预计占用GB"]
            predicted = _round(base + incoming - cycle_release)
            forecast.append({
                "周期": idx + 1,
                "周期结束日": (as_of_date + timedelta(days=CYCLE_DAYS * (idx + 1))).isoformat(),
                "新增占用GB": incoming,
                "到期可释放GB": cycle_release,
                "预计占用GB": predicted,
                "预计剩余GB": _round(total_capacity - predicted),
                "容量是否告急": predicted >= total_capacity,
            })

        steps = _migration_steps(effective_reports)
        node_steps = [s for s in steps if s["存储节点"] == node_code]
        shortage = any(item["容量是否告急"] for item in forecast) or usage_ratio >= 90

        nodes.append({
            "节点编号": node_code,
            "节点状态": node.get("节点状态"),
            "文件格式": node.get("文件格式"),
            "压缩方式": node.get("压缩方式"),
            "总容量GB": total_capacity,
            "登记已用GB": used_capacity,
            "口径来源": reference_source,
            "当前占用GB": current_gb,
            "预建占用GB": prebuilt_gb,
            "预计占用GB": reference_gb,
            "预计剩余GB": headroom_gb,
            "使用率": usage_ratio,
            "预计释放量GB": release_gb,
            "冷数据迁移量GB": cold_gb,
            "冷热分层收益GB": benefit_gb,
            "锁住不可迁移GB": blocked_gb,
            "待审批策略预计占用GB": pending_projected_gb,
            "容量是否告急": shortage,
            "不可迁移原因": immovable_reasons,
            "容量预测": forecast,
            "迁移顺序": node_steps,
            "关联策略": [r["配置编号"] for r in node_reports],
        })

    return {
        "基准日期": as_of_date.isoformat(),
        "周期天数": CYCLE_DAYS,
        "预测周期数": cycles,
        "容量单位": GB,
        "冷分层折算系数": COLD_TIER_FACTOR,
        "湖节点": nodes,
        "分区策略": reports,
    }


def node_capacity(node_code: str, *, as_of: str | date | None = None,
                  cycles: int = DEFAULT_CYCLES) -> dict[str, Any] | None:
    overview = capacity_overview(as_of=as_of, cycles=cycles)
    for node in overview["湖节点"]:
        if node["节点编号"] == node_code:
            return node
    return None


def strategy_capacity(config_code: str, *, as_of: str | date | None = None,
                      cycles: int = DEFAULT_CYCLES) -> dict[str, Any] | None:
    overview = capacity_overview(as_of=as_of, cycles=cycles)
    for report in overview["分区策略"]:
        if report["配置编号"] == config_code:
            return report
    return None
