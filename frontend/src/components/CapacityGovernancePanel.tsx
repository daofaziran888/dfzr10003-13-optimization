/**
 * 湖存储容量治理共享面板。
 *
 * 湖页与分区页都渲染这一个组件、读取同一个容量引擎出口（/api/lake/capacity/overview
 * 与 /api/partition/capacity/overview 返回同一份口径），因此两页看到的预计占用、
 * 预计释放量、不可迁移原因完全一致，不再各自沿用旧容量数字。
 */
import { useCallback, useEffect, useState } from 'react'

import { request } from '@/api/client'

interface ImmovableReason {
  原因: string
  涉及分区数: number
}

interface ForecastPoint {
  周期: number
  周期结束日: string
  新增占用GB: number
  到期可释放GB: number
  预计占用GB: number
  预计剩余GB: number
  容量是否告急: boolean
}

interface MigrationStep {
  顺序号: number
  动作: string
  分区编号: string
  配置编号: string
  存储节点: string
  业务优先级: number
  回收容量GB: number
  不可迁移原因: string[]
}

export interface LakeCapacity {
  节点编号: string
  节点状态: string
  文件格式: string
  压缩方式: string
  总容量GB: number
  登记已用GB: number
  口径来源: string
  当前占用GB: number
  预建占用GB: number
  预计占用GB: number
  预计剩余GB: number
  使用率: number
  预计释放量GB: number
  冷数据迁移量GB: number
  冷热分层收益GB: number
  锁住不可迁移GB: number
  待审批策略预计占用GB: number
  容量是否告急: boolean
  不可迁移原因: ImmovableReason[]
  容量预测: ForecastPoint[]
  迁移顺序: MigrationStep[]
  关联策略: string[]
}

export interface StrategyCapacity {
  配置编号: string
  表名称: string
  存储节点: string
  策略状态: string
  已生效: boolean
  保留天数: number
  预建天数: number
  冷热分离天数: number
  文件格式: string
  压缩方式: string
  业务优先级: number
  日增容量GB: number
  近期增长率: number
  占用系数: number
  系数口径: string | null
  当前占用GB: number
  预建占用GB: number
  预计占用GB: number
  预计释放量GB: number
  冷数据迁移量GB: number
  冷热分层收益GB: number
  锁住的到期分区GB: number
  锁住的冷分区GB: number
  不可迁移原因: ImmovableReason[]
  容量预测: Array<{
    周期: number
    周期结束日: string
    新增占用GB: number
    到期可释放GB: number
    预计占用GB: number
  }>
}

interface CapacityOverview {
  基准日期: string
  周期天数: number
  预测周期数: number
  湖节点: LakeCapacity[]
  分区策略: StrategyCapacity[]
}

interface MigrationPlan {
  计划编号: string
  节点编号: string
  状态: string
  步骤总数: number
  已完成数: number
  失败数: number
  失败分区: string | null
  执行记录: Array<{ 顺序号: number; 动作: string; 分区编号: string; 状态: string; 说明: string }>
}

const gb = (value: number) => `${value.toFixed(2)} GB`

function ReasonList({ reasons }: { reasons: ImmovableReason[] }) {
  if (reasons.length === 0) {
    return <span className="ok-text">无锁定引用，分区可正常迁移/释放</span>
  }
  return (
    <ul className="reason-list">
      {reasons.map((item) => (
        <li key={item.原因}>
          {item.原因}
          <em>（{item.涉及分区数} 个分区）</em>
        </li>
      ))}
    </ul>
  )
}

function ForecastTable({ points, showRemaining }: { points: ForecastPoint[]; showRemaining?: boolean }) {
  return (
    <table className="data-table inner-table">
      <thead>
        <tr>
          <th>周期</th>
          <th>周期结束日</th>
          <th>新增占用</th>
          <th>到期可释放</th>
          <th>预计占用</th>
          {showRemaining && <th>预计剩余</th>}
          {showRemaining && <th>容量判定</th>}
        </tr>
      </thead>
      <tbody>
        {points.map((point) => (
          <tr key={point.周期} className={point.容量是否告急 ? 'row-danger' : ''}>
            <td>第 {point.周期} 周期（+{point.周期 * 7} 天）</td>
            <td>{point.周期结束日}</td>
            <td>{gb(point.新增占用GB)}</td>
            <td>{gb(point.到期可释放GB)}</td>
            <td>{gb(point.预计占用GB)}</td>
            {showRemaining && <td>{gb((point as ForecastPoint).预计剩余GB)}</td>}
            {showRemaining && (
              <td>{point.容量是否告急 ? <span className="error-text">容量告急</span> : <span className="ok-text">可承载</span>}</td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function NodeCard({ node, onPlan }: { node: LakeCapacity; onPlan: (node: LakeCapacity) => void }) {
  return (
    <article className={`capacity-card ${node.容量是否告急 ? 'capacity-card-danger' : ''}`}>
      <header className="capacity-card-head">
        <div>
          <strong>{node.节点编号}</strong>
          <span className="tag">{node.节点状态}</span>
          {node.容量是否告急 && <span className="tag tag-danger">容量紧张</span>}
        </div>
        <span className="capacity-source">口径取较高占用侧：{node.口径来源}</span>
      </header>
      <div className="capacity-metrics">
        <div><span>总容量</span><strong>{gb(node.总容量GB)}</strong></div>
        <div><span>登记已用</span><strong>{gb(node.登记已用GB)}</strong></div>
        <div><span>预计占用</span><strong className="metric-hot">{gb(node.预计占用GB)}</strong></div>
        <div><span>预计剩余</span><strong>{gb(node.预计剩余GB)}</strong></div>
        <div><span>使用率</span><strong>{node.使用率.toFixed(1)}%</strong></div>
        <div><span>预计释放量</span><strong className="metric-ok">{gb(node.预计释放量GB)}</strong></div>
        <div><span>冷数据迁移量</span><strong>{gb(node.冷数据迁移量GB)}</strong></div>
        <div><span>冷热分层收益</span><strong className="metric-ok">{gb(node.冷热分层收益GB)}</strong></div>
        <div><span>锁定不可迁移</span><strong className="metric-warn">{gb(node.锁住不可迁移GB)}</strong></div>
      </div>
      <p className="capacity-line">
        格式/压缩：{node.文件格式} + {node.压缩方式}；关联策略：{node.关联策略.join('、')}
        {node.待审批策略预计占用GB > 0 && (
          <em>；待审批策略仅进预测，预计占用 {gb(node.待审批策略预计占用GB)}，不计入当前占用</em>
        )}
      </p>
      <div className="capacity-block">
        <h4>不可迁移原因（快照 / 历史重跑 / 质量校验引用）</h4>
        <ReasonList reasons={node.不可迁移原因} />
      </div>
      <div className="capacity-block">
        <h4>未来 {node.容量预测.length} 个周期容量预测（每周期 7 天）</h4>
        <ForecastTable points={node.容量预测} showRemaining />
      </div>
      <div className="capacity-block">
        <h4>可执行迁移顺序（先释放到期分区，再按业务优先级迁移冷分区，共 {node.迁移顺序.length} 步）</h4>
        {node.迁移顺序.length === 0 ? (
          <p className="page-desc">当前没有可迁移或可释放的分区。</p>
        ) : (
          <ol className="step-list">
            {node.迁移顺序.slice(0, 8).map((step) => (
              <li key={step.顺序号}>
                <span className="step-no">{step.顺序号}</span>
                <span className={`step-action ${step.动作 === '释放分区' ? 'action-release' : 'action-migrate'}`}>{step.动作}</span>
                <span>{step.分区编号}</span>
                <em>优先级 {step.业务优先级} · 回收 {gb(step.回收容量GB)}</em>
              </li>
            ))}
            {node.迁移顺序.length > 8 && <li className="page-desc">其余 {node.迁移顺序.length - 8} 步顺序相同，提交计划后可查看完整执行记录。</li>}
          </ol>
        )}
        <button className="btn primary" type="button" onClick={() => onPlan(node)}>
          提交 {node.节点编号} 迁移计划
        </button>
      </div>
    </article>
  )
}

export default function CapacityGovernancePanel({ endpoint }: { endpoint: string }) {
  const [overview, setOverview] = useState<CapacityOverview | null>(null)
  const [error, setError] = useState('')
  const [plan, setPlan] = useState<MigrationPlan | null>(null)
  const [planMessage, setPlanMessage] = useState('')
  const [cycles, setCycles] = useState(4)

  const reload = useCallback(async () => {
    setError('')
    try {
      const response = await request(`${endpoint}?cycles=${cycles}`)
      if (!response.ok) throw new Error('容量口径读取失败')
      setOverview((await response.json()) as CapacityOverview)
    } catch (err) {
      setError(err instanceof Error ? err.message : '容量治理数据读取失败')
    }
  }, [endpoint, cycles])

  useEffect(() => {
    void reload()
  }, [reload])

  async function callPlan(path: string, options?: RequestInit) {
    setPlanMessage('')
    try {
      const response = await request(path, options)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail ?? '迁移计划操作失败')
      setPlan(payload.plan as MigrationPlan)
      setPlanMessage(payload.message as string)
      await reload()
    } catch (err) {
      setPlanMessage(err instanceof Error ? err.message : '迁移计划操作失败')
    }
  }

  function submitPlan(node: LakeCapacity) {
    void callPlan(`/api/lake/capacity/${node.节点编号}/migration-plan?cycles=${cycles}`, { method: 'POST' })
  }

  function executePlan(failPartition?: string) {
    if (!plan) return
    void callPlan(`/api/lake/migration-plans/${plan.计划编号}/execute`, {
      method: 'POST',
      body: JSON.stringify({ values: failPartition ? { fail_partition: failPartition } : {} }),
    })
  }

  function retryPlan() {
    if (!plan) return
    void callPlan(`/api/lake/migration-plans/${plan.计划编号}/retry`, { method: 'POST' })
  }

  if (error) {
    return <div className="capacity-panel"><p className="error-text">{error}</p></div>
  }
  if (!overview) {
    return <div className="capacity-panel"><p className="page-desc">容量口径计算中……</p></div>
  }

  return (
    <section className="capacity-panel">
      <header className="capacity-head">
        <div>
          <h3>湖存储容量治理（分区策略 → 湖节点统一取数口径）</h3>
          <p className="page-desc">
            基准日期 {overview.基准日期}，按保留天数、预建天数、文件格式、压缩方式与近期增量重算；
            湖页与分区页读取同一份预计占用、释放量与不可迁移原因。
          </p>
        </div>
        <label className="filter-item">
          <span>预测周期数（每周期 {overview.周期天数} 天）</span>
          <select value={cycles} onChange={(event) => setCycles(Number(event.target.value))}>
            {[2, 3, 4, 6, 8].map((value) => <option key={value} value={value}>{value} 个周期</option>)}
          </select>
        </label>
      </header>

      <div className="node-grid">
        {overview.湖节点.map((node) => (
          <NodeCard key={node.节点编号} node={node} onPlan={submitPlan} />
        ))}
      </div>

      <div className="capacity-card">
        <h4>分区策略口径（待审批策略只进预测；容量冲突时以已生效策略与节点引用中较高占用一侧为准）</h4>
        <table className="data-table inner-table">
          <thead>
            <tr>
              <th>配置编号</th>
              <th>状态</th>
              <th>湖节点</th>
              <th>保留/预建/冷热(天)</th>
              <th>格式+压缩</th>
              <th>占用系数(口径)</th>
              <th>预计占用</th>
              <th>预计释放量</th>
              <th>冷热收益</th>
              <th>不可迁移原因</th>
            </tr>
          </thead>
          <tbody>
            {overview.分区策略.map((strategy) => (
              <tr key={strategy.配置编号} className={strategy.已生效 ? '' : 'row-pending'}>
                <td>{strategy.配置编号}<div className="page-desc">{strategy.表名称}</div></td>
                <td>{strategy.策略状态}</td>
                <td>{strategy.存储节点}</td>
                <td>{strategy.保留天数} / {strategy.预建天数} / {strategy.冷热分离天数}</td>
                <td>{strategy.文件格式} + {strategy.压缩方式}</td>
                <td>{strategy.占用系数.toFixed(2)}（{strategy.系数口径 ?? '—'}）</td>
                <td>{gb(strategy.预计占用GB)}</td>
                <td>{gb(strategy.预计释放量GB)}</td>
                <td>{gb(strategy.冷热分层收益GB)}</td>
                <td className="reason-cell"><ReasonList reasons={strategy.不可迁移原因} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {plan && (
        <div className="capacity-card plan-card">
          <header className="capacity-card-head">
            <div>
              <strong>迁移执行记录 {plan.计划编号}</strong>
              <span className="tag">{plan.状态}</span>
            </div>
            <span className="capacity-source">节点 {plan.节点编号} · 同计划并发提交只落库这一条</span>
          </header>
          <p className="capacity-line">
            进度 {plan.已完成数}/{plan.步骤总数}，失败 {plan.失败数}
            {plan.失败分区 && <>，失败分区：<span className="error-text">{plan.失败分区}</span>（重试将从该分区继续）</>}
          </p>
          <div className="plan-actions">
            <button className="btn primary" type="button" onClick={() => void executePlan()}>执行 / 续跑</button>
            <button
              className="btn"
              type="button"
              onClick={() => {
                const failed = plan.执行记录.find((item) => item.状态 !== '成功' && item.状态 !== '失败')
                void executePlan(failed?.分区编号)
              }}
            >
              模拟下一分区失败
            </button>
            <button className="btn ghost" type="button" onClick={() => void retryPlan()} disabled={plan.失败数 === 0}>
              从失败分区重试
            </button>
          </div>
          <ol className="step-list plan-records">
            {plan.执行记录.map((record) => (
              <li key={record.顺序号} className={`record-${record.状态}`}>
                <span className="step-no">{record.顺序号}</span>
                <span className="step-action">{record.动作}</span>
                <span>{record.分区编号}</span>
                <em>{record.状态}{record.说明 ? ` · ${record.说明}` : ''}</em>
              </li>
            ))}
          </ol>
          {planMessage && <p className="plan-message">{planMessage}</p>}
        </div>
      )}
      {!plan && planMessage && <p className="plan-message error-text">{planMessage}</p>}
    </section>
  )
}
