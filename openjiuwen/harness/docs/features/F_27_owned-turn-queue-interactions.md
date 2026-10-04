# F_27 Owned Turn queue and interaction ledger

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | SerializedTurnHarness 原队列与交互账本归属 |
| 测试基线 | 全部 Provider fake SDK 单测 564 passed、1 skip；包含新增 8 项 |
| Refs | S_19_harness-providers；R2-B4 凭据退出基础 |

## 背景

原交互账本只存 handler，不能按原 Turn 选择；取消前清空账本让尚在等待的 handler.cancel(id)
可能命中同 ID 的新交互。宿主也缺少只取消原未执行队列项的精确入口。

## 数据结构 / 状态机

复用唯一 PendingTurn、pending 队列、supervisor 和 command lock。原交互字典的值改为
私有 entry：原 request、handler、PendingTurn、handling bool、原 cancel_task。没有第二队列、
注册表、协议状态或持久化。ExecutionOrigin 和实际 Native Round 贯通仍属独立工作。

## 决策

- `_capture_owned_turn(turn_id)` 同步只读原 active 与 queue，拒绝歧义，不取 current fallback。
- `_cancel_queued_turn(expected)` 在原锁中用 is 复核；active、退休、外来或复制对象拒绝。
  保留原队列位置，由原 supervisor 发 STARTED/USER_ABORT terminal，跳过 Provider dispatch。
- 交互原 owner 在首次 await 前捕获。所有当前内置 Provider 在有 active 时提供显式 ID；
  None 只保留无 active 的 Session/startup 回调。Codex mandatory authorizer 等待后 None→active
  或旧显式 ID→successor 都拒绝，不能把旧审批绑定新 Turn。
- 活跃 ID 重复拒绝。取消 snapshot 为每个原 entry 固定取消 Task，再开始等待；并发调用
  等同 Task，shield 避免取消 waiter 导致假退出。handle 与 cancel 都结束才释放 ID。
- cancel 失败保留 entry、明确抛错，后续在同 entry 重试。公开 abort/stop 的签名和正常成功
  路径保持，但取消回调失败由旧有记录后吞掉改为向调用者传播，避免假退出；旧 finally/done callback 不能清除
  新 entry。被取消或原 Turn 已替换的响应不交付为有效审批。

## 拒绝的方案

不新增公共 abort/protocol 参数，不按 actor/最新 Turn 猜归属，不提前释放 ID，不清整 Session
取消其它凭据的 successor，不把取消排队项或交互成功当成活动 Provider 任务已退出。

## 验证

新增 8 项确定性用例：真实队列保序且不 dispatch、复制/退休/active 拒绝、歧义只读查找、
取消 callback 等待期间 ID 保留、waiter 取消与同原 Task 重试、失败重试、handle 未结束、
迟到回复与 None 规则、Codex 原 None/显式 ID 经 authorizer await 后换 Turn。
全部 Provider 单测使用 fake SDK/合成对象，不触发新真实 Provider 对抗。测试位于既有 stable
文件内，不修改 manifest 或超时。源码测试不等于干净非 editable 发布或下游锁定配对。

## 已知遗留

活动 Native abort 在取消交互的 await 后仍可能选中后继 Round；cancel_round 返回也不证明
原 wrapper/stream/subagent 已退出。本片明确不关闭此前两条活动退出红测，不更改其兼容合同。
后续必须复用 ExecutionOrigin 固定原 Turn→Round/tasks 来源，并以精确退出确认约束 terminal
与后继晋升。旧公开 abort 不能被宿主当成已经完成该强保证的接口。
