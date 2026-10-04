# F_39 空闲 Native Goal 的精确暂停与清空

| 项 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 范围 | GoalManager、Native 私有控制入口；仅已有 Goal 的 pause/clear |
| 测试基线 | 实际 Native Pending/退出图、GoalManager、SessionGoalStore、EventManager 和 Scheduler；模型 IO 为合成数据 |
| Refs | leemine/codeswarm#33 |

## 背景

F37 的控制选择器要求原 live Round，F38 的 resume/attach 则是新执行准入。
已退出或已暂停的 Goal 需要合法的停止性控制；不能为了清空目标先启动一轮执行。
持久 GoalRecord 是数据，不携带认证或执行权限。

## 数据与接口

Native 私有 `_capture_idle_goal_control(expected_record, previous_turn, check_current)`
返回不可变控制对象，提供 `apply(action='pause'|'clear')` 和 `check_result()`。
原 GoalManager、store 及其真实 backing Session、执行适配器、controller、两把交互锁、
完整 GoalRecord 和 live slot 固定在 F38 的原选择器中。Native 另固定原 command lock、
上下文、host hooks、事件缓冲区及首次 managed Pending 标记，拒绝换 Session/cycle。
控制回调同步返回 None；其检查前后再次核对结构、记录、退出证明和原库存。

hot 控制必须显式传入原 Pending，其 source 与原 slot 按对象身份一致；原 cleanup、
confirmed、admissions、interaction handles、round exit 均已成功退出且仍是原对象。
它复用 F38 的退出证明，既不读取最新 Pending，也不调用已经结束的原 source checker。
cold 控制仅允许真正首次 managed Pending 尚未产生、slot 为 None 的当前 Native Session，
同时验证全部原工作库存为空。未知 legacy 子工作仍拒绝，不以空队列代替退出证明。

## 决策

- 控制凭据仅通过 `check_current` 授予此次 pause/clear，不安装成 execution source。
- 复用原 command → send → Goal control 锁序及 `_pause_locked/_clear_locked`、原 Session commit。
  不创建 Turn、模型请求、输出 lease、工作队列或独立持久状态。
- pause 保持原 slot/revision；clear 在原同步保存点清空 slot，之后验证清空后的事实。
  已 PAUSED/BLOCKED/COMPLETED 的 pause 保持原无操作语义。
- 不取消已经退出的原任务，也不丢弃其他来源队列。出现新工作立即拒绝。
- idle 专用 `_emit_updated` 只复核；响应沿原调用 ACK 返回 GoalRecord 副本，
  不向旧 execution 的输出发送事件。legacy/live 控制继续原 goal_updated 通知。
- 原操作 Task 被调用者取消屏蔽保护；相同对象、相同行为重试等待同一次 commit。
  最终 ACK 再检查当前控制授权、原目标及返回结果，失败不能伪成功。

## 拒绝的方案

不使用公共 resume/attach 或一轮空输入获得暂停权限；不从同 sid 的最新对象补旧退出证明；
不因原 source 过期就认定退出；不使用原执行输出承载新控制授权；不复制 Goal 状态存储。

## 验证

新增实际组件用例覆盖 cold/hot pause/clear、失活旧 source、原退出证书缺失或替换、
同步控制回调重入、真实 backing Session 替换、commit 等待期间撤权/停机、
原锁等待期间撤权、调用者取消与同操作重试、最终返回结果/任务替换、
非空队列/dequeued/emit/wrapper/follow-up、原 cycle 替换、无输出/无模型/无新 Turn，
并回归 F37/F38、原 GoalManager、Native host/exact-exit。新文件加入既有 stable manifest。

## 已知遗留

宿主必须绑定新的真实控制 producer、当前认证、目标 owner 和资源授权，并在最终发送调用
`check_result()`。本片没有启用 swarm RPC，没有真实 Provider 验收；不代表完整活动恢复。
没有 Goal 的幂等读取 ACK 不属于该 mutation 入口。未确认提交异常明确失败，
不声称回滚已保存的数据，也不新增跨重启事务恢复；调用者取消后仅原操作对象可等待其结果。
共享 supervisor 可正常自行退出，它不是特定 Turn 的资源退出证据。
