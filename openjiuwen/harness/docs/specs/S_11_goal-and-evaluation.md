# S_11 Goal 与评估

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness/goal/` |
| 最近一次修订日期 | 2026-09-26 |
| 关联 feature | `F_10_provider-neutral-goal-driver.md` |

## 范围 / 边界

GoalManager 是会话持久目标唯一写者，GoalEvaluator 判定完成与停止策略，
GoalAttemptDriver 提供宿主和 Native 共用的完成入口。GoalExecutionPort 委托宿主
已有 Session Runtime；NativeGoalExecutionAdapter 保留 EventManager 和输出附着。
不新增调度器、Provider 状态机、事件消费者或持久存储；不依赖 swarm 或 Projects。
Native 的工具、报告/消息采集和 transcript 模型调用仍由 TaskCompletionRail 适配。

## 不变量

1. 状态、计量和 assessment 只由 GoalManager 写入 SessionGoalStore。所有写操作与
   宿主控制共用同一个 asyncio.Lock。端口回调不得重入该锁。
2. GoalStatus 为 ACTIVE / PAUSED / COMPLETED / BLOCKED。pause 丢弃排队续轮，允许
   已开始的 attempt 收尾。CONTINUE 保持 PAUSED；COMPLETE/BLOCKED 可覆盖 PAUSED。
3. 新目标生成新 goal_id。空闲 resume 递增 revision；在途 resume 保留 revision，
   避免重复 attempt 并允许其结算。旧 ID/revision 的写入被拒绝。
4. 公共驱动的完成入口必带 attempt_index；仅当前 attempt_count 且尚未结算的结果
   可写入。last_assessed_attempt 持久化在原记录内，重建后仍拒绝重复/旧 attempt。
   旧 manager 方法允许不传 attempt_index，以保留原调用契约。
5. begin_attempt 递增 attempt_count。max_attempts/token_budget 在 GoalEvaluator.assess
   完成后只将 CONTINUE 改为 BLOCKED；不会把 COMPLETE 改为失败。它们不是跨 Provider
   的模型调用前硬拦截。未设置预算不添加默认上限。
6. AGENT_REPORT 使用结构化报告；TRANSCRIPT 使用 transcript 评估；HYBRID 信任
   CONTINUE（可按间隔核验），COMPLETE/BLOCKED 必须通过 transcript 核验；失败保守继续。
7. 中断待答、取消、流关闭不代表 attempt 完成。宿主显式提供完成/执行失败结论，
   由公共驱动结算；流关闭不得自动构造 assessment。
   finish 的 usage 参数仅接受 completed/failed 结算；其它 outcome 携带 usage 明确
   抛 ValueError，宿主应将去重后的这部分用量先交原 accumulate_usage 通道，不静默丢弃。
8. ensure_work 仅委托原宿主准入与去重；Native 继续要求输出消费者。冷恢复构造
   Manager 不启动工作；宿主先以原 registry/checkpoint 确认关联与续接性。
9. 端口不接管审批、用户/Team/Heartbeat 优先级、Provider generation 或 usage 事件
   去重。这些由原 Session Runtime 和单事件消费者保证；Heartbeat 不写 Goal 状态。
10. get/peek 返回深拷贝，端口接收快照。GoalOperationError 保留 operation/code/goal。

## 接口契约

```python
GoalManager(store=..., control_lock=..., execution=GoalExecutionPort())
# 原 event_manager/has_output_stream/cancel_active_round/emit_event/notify_work 构造仍支持；
# 两种构造互斥，避免两套执行者。
await manager.get() -> GoalRecord | None
manager.peek() -> GoalRecord | None
await manager.set(objective, *, overwrite_confirmed=False, token_budget=None, max_attempts=None)
await manager.pause() -> GoalRecord | None
await manager.resume() -> GoalRecord | None
await manager.clear() -> GoalRecord | None
manager.ensure_active_goal_work_locked() -> bool
await manager.begin_attempt(...) -> GoalRecord | None
await manager.accumulate_usage(...) -> None
await manager.apply_assessment(..., attempt_index=None) -> GoalRecord | None

GoalAttemptDriver(manager, evaluator=None)
await driver.finish(..., attempt_index: int, ...) -> GoalRecord | None
GoalEvaluator.assess(...) -> GoalAssessment
```

无记录的 pause/resume/clear 返回 None。存在任意状态记录时 set 默认要求确认覆盖。
last_stop_reason 在有效 COMPLETE/BLOCKED 后分别为 `completed`/`blocked`；具体预算或
次数原因保留在 last_assessment.evidence。CONTINUE 不增加 attempt_count，开始才增加。

## 数据结构

GoalRecord 是 dataclass，使用 to_dict/from_dict；不是 Pydantic 模型。身份、objective、
四态、revision、attempt_count、TokenUsage、预算、assessment、stop reason、累计时间和
ISO 时间戳保持原格式。`last_assessed_attempt` 是非负整数，旧记录缺省为 0。
字段追加在 dataclass 末尾，保留既有位置参数。SessionGoalStore 仍使用
`harness.goal.record`，没有 sidecar 或历史反写状态。

## 与其它 spec 的关系

- Native EventManager/InteractionEvent 与 DeepAgent supervisor：S_02。
- TaskCompletionRail 收集报告、transcript 和 usage，并调用公共驱动：S_04。
- submit_goal_report/get_current_goal 工具：S_05。
- Provider SPI 与有序单消费者事件不改变：S_19。
