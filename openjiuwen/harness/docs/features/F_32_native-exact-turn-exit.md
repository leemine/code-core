# F_32 Native 原 Turn 精确退出

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | Native 私有 PendingTurn/source、原交互完成、原 Round/queue/lease 退出 |
| 测试基线 | 隔离源码合成组件；不运行真实 Provider 对抗 |
| Refs | R2-B4 凭据精确退出；未指定 issue |

## 背景与冻结决策

复用 SerializedTurnHarness 原 PendingTurn、command lock、队列及交互 ledger。base 仅新增
同步私有 PendingTurn factory；Native 私有 subclass 保留原 origin/退出 handle，不进 wire。
NativeHostHooks 的 keyword-only capture_execution_origin 同步捕获原宿主来源；hook 缺失
才走 legacy，显式 hook 返回 None 拒绝，异常不降级。复合 checker 核对原 Turn、host source、
原 Session；resume/steer 复用原根，不从后继 current 借权。

外部取消预算仍有界，原 cleanup Task/handle 保留；未知退出不得由 base crash 路径转终态。
原 execute 出口等待成功确认，交互 cancel ACK 不替代原 handle finally。取消只选择相同原
来源的 queued/dequeued/active work、facade/wrapper、输出 lease，不调用 Session-wide abort。

## 拒绝的方案与当前限制

不增加队列/registry/持久权限，不以 EOF 或业务 Future 完成证明退出，不取消共享 supervisor。
原 subagent UserInputOp 尚无 root 来源；无法证明时必须 unknown，不能 whole-Session cancel_all。
子 Agent activity emitter 的原队列/正在写入项也尚无 Turn 来源；仅看到实例 terminal
或空队列不足以确认。当前托管组合存在 live activity drain 时明确 unknown。后续 F_33
需在原 UserInputOp/claimed/current 操作及原 activity 路径补来源，不新增消费者或禁用
原子 Agent 能力冒充完成。宿主 hook/Runtime 凭据 watcher 仍需下游接线与真实验收。

## 退出记录及输出确认

私有 Native PendingTurn 保留原 origin、Session、实际 admission Tasks、原 stream 和
execute-body 完成信号。一个退出 handle 保留原交互快照、Round handle、cleanup Task
和 confirmed Future。它是原记录的退出证明，不是第二个调度状态机。
Native stop 在释放 agent/session 前先确认原托管 active Turn；超时保留绑定并允许
同 cleanup Task 重试，之后才执行既有 agent.stop/session.post_run。

ActiveInteractionRound 保留原 forwarded Event/forwarder Task。托管 facade 等待原
marker 而不是依靠旧 supervisor 的 2 秒尽力等待；marker 写失败/consumer 退出明确
unknown。同步事件沿原 emit Task set 保存来源，join 同源尾部；迟到新 emit 被原
checker 拒绝。输出 lease 仍用原 token 关闭，不取当前 lease，不丢弃其他来源 work。

## 验证

新增实际 DeepAgentHarness→DeepAgent→TaskLoopController→TaskManager/Scheduler/
TaskLoopExecutor 的合成模型组件回归。覆盖正常 EOF、取消/重试、原 wrapper finally、
排队 Turn、调用者取消、late dispatch/steer、交互 ACK/真实 handle 退出和取消失败重试、
原 Session 替换、子 Agent queued/claimed/current/activity tail、实际 forwarder 延迟/
退出、fire-and-forget emit 尾部、精确多 work/外来 lease 与队列 join。

该测试加入原 native-output stable suite；受影响与 strict 结果、来源和命令留于本轮
交付证据。源码 overlay 测试不是最终按锁非 editable 安装或真实 Provider 验收。
没有新增真实 Provider 对抗、没有升级 CLI，也没有开启下游凭据退出 watcher。
