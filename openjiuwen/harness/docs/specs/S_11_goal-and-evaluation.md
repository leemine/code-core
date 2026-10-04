# S_11 Goal 与评估

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness/goal/` |
| 最近一次修订日期 | 2026-10-05 |
| 关联 feature | `F_10_provider-neutral-goal-driver.md`, `F_35_goal-live-execution-origin.md`, `F_37_owned-goal-control.md` |

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
   Native supervisor 在中断时保留原在途 Goal owner；ACTIVE 不得触发新的 attempt。
   显式 InteractiveInput 在原 goal/revision/attempt 下恢复，并通过原 rail 结算；
   待答期间 pause/resume 不增加 revision，clear/overwrite/stop 清理旧 owner。
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

## Native live 执行来源

GoalManager 的私有单槽保存本进程 set 准入时的原 Goal identity 与 ExecutionOrigin；
不写入 GoalRecord、Session state 或 wire，不增加持久授权。set 首次 await 前捕获，
锁内修改前及 commit 后重新检查；同一 Goal 的后继 attempt 和更新输出只复用该来源。
ensure_active 的长驻调用显式遮蔽 ambient，Native adapter 将来源附到原 RoundWorkItem，
EventManager 仍为唯一工作队列。旧无来源记录保持 legacy；重建不恢复 live 权限。

受管原来源失效时不能以新的 ambient 重新授予原 Goal；managed resume 不能自动换根，
本片不开放持久 ACTIVE 记录的受管 attach。get/peek 保持只读。pause/clear 的宿主
权限、idle resume 的新准入、EOF handoff 与完整宿主接线不由本来源载体代替。

重新构造的 Manager 读取持久 ACTIVE 记录时没有 live slot，core 保持 legacy None；
这不是认证恢复。受管宿主必须在 attach 前拒绝或提交显式新的合法 admission，不能凭
GoalRecord 的 ACTIVE 字段直接开放执行。旧 Goal 的迟到 begin/usage/assessment 先经
现有 ID/revision 校验返回，不通过新绑定补权或改变新 Goal。

pause/clear 的原持久写入/commit、丢弃排队 work、clear 原 cancel 顺序保持；原来源若在
输出阶段失效，会拒绝 emit 并抛异常，但已经 PAUSED/clear 的状态不会回滚。F35 不提供独立 cleanup-only 权限；F37 私有 selector 也不回滚已经发生的持久写入，
调用者不能把该异常解释成“未发生写入”。


## 原 root 精确控制（F37）

私有 `_capture_owned_control(expected_origin=...)` 返回不入 wire/持久化的 selector；
`_apply_owned_control(selector, action=..., check_current=..., ...)` 保留旧公开方法与返回值。
clear 成功返回已移除记录副本。peek/get 仍只读，不触发运行；宿主另验 owner 权限。

捕获先固定 manager/store/控制锁/Session/记录事实以及原 Round、work、facade，随后
执行原 source checker 并静态复核。apply 在原控制锁内核原 source 和临时同步 checker，
回调后复核固定引用与参数，每次 commit await 后重验。不要求调用 Task 相同；不得以
当前 ambient 或新控制身份替换原 source。usage、计量、assessment 不等同 Goal 换代。

active set/pause/clear 与同 live attempt resume 属于原 root。无记录 set 要求真实原
live Round；idle resume、冷 attach 和 EOF 新 root 仍需新的宿主准入，不能据此开放。
clear/overwrite 只取消原 attempt，不取消相同来源下的新 Goal 或其它排队 work。
原锁提交后释放锁，等待原 facade、submission、scheduler wrapper 和原输出尾部。
外层取消保留同一操作；明确尾部失败可用同 selector/参数/checker 仅重试退出与确认，
不得重复持久修改或二次打断原 finally。成功缓存返回前仍重验当前权限。

仅 managed Goal 在出队、原 wrapper/model 消费点复核原 Goal ID/revision/source；
clear/overwrite 后的旧工作拒绝，pause 不撤原 root、不改变原 revision，允许在途收尾。
待答原 attempt 的退出只在 active 仍是原对象时清理原 Session interruption state，
并唤醒既有 supervisor；不能清理后继 Round 状态。
legacy None 的队列顺序、公开控制签名、状态格式和行为保持。


私有 live admission 要求原 DeepAgent Session 尚未 TERMINATED。所有实际 commit
前后、ensure 与 Goal emit 前的 mutation 检查亦核原 Session phase，但不要求刚被
clear 取消的 facade 仍 live。退出完成/ACK 的静态复核不新增 active-Round 要求；
正常完成不能被当作新控制准入，shutdown 后则不能继续持久控制/排队/输出。


私有 `_apply_owned_control(..., check_ack=None)` 固定原 ACK callback 身份。只有原 clear
已完成 mutation，且原精确 drain Task 成功完成时，`selector.check_result()` 才可使用
独立同步 ACK checker，不要求已合法结束的原 PendingTurn 再提供执行来源。回调前后
核原记录/slot/execution refs、operation Task/applied、结果对象与完整返回字段；给
调用者的是新副本。宿主仍核临时凭据、owner/Binding 与原 entry 退出证明。queued clear
无 attempt、未确认退出、set/pause/resume 都不能借用此例外，执行前仍用原 live checker。
