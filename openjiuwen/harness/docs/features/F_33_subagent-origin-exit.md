# F_33 子 Agent 原操作退出来源

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | 原 UserInputOp、claimed/current、活动写入尾部、Native finalizer、无子工作静默证明 |
| 测试基线 | 隔离确定性实际组件；无真实 Provider 对抗 |
| Refs | R2-B4；未指定 issue |

## 背景

子 Agent 允许后台跨逻辑父 Turn 生存。父正常 EOF 不得等待所有后台操作，或取消共享
worker/activity drain 来伪造精确退出。另一方面，旧代码只知道当前 task_id，不能区分
同一 child 上来自两个父操作的工作；Native finalizer 的一次 shield 也会被第二次取消
打断调用者，造成实际清理尚未返回便丢失退出所有权。

## 数据结构与接口

- `UserInputOp` 保留 query/task_id。私有 InitVar origin/lifetime 不进 dataclass
  asdict、JSON 或恢复字段；显式复制保留原对象身份，显式 None 清除来源。
- `_OperationLifetime` 附着原 op，保存原 instance、op、semaphore acquisition Task、
  execution Task、取消 fence 和原 worker tail 的 done Event。没有第二队列/任务注册表。
- `SubagentControl._capture_origin_exit(origin)` 同步固定原 manager/instance/ops；
  `_finish_origin_exit(handle, cancel=...)` 只对这些原对象操作。共享 worker 不取消。
  `cancel=False` 仅供显式等待该生命周期尾部，**不接入父正常 EOF**。
- managed 活动在原 ActivityEmitter 队列中附原 operation 和 done Event，`_current_item`
  覆盖已 dequeue 但 write_stream 尚未完成的窗口。仅删除原 queued item，已进行的 write
  等真实返回；不取消公共 drain。历史 bucket 存不带 live carrier 的投影。

## 决策

1. 控制入口首 await 前固定来源。来源 scope 已失活必须拒绝，不能降为 legacy；从未
   设置或显式 None 保留旧行为。原 factory 等待后再次校验，失败回收原 instance/quota。
2. 子执行仍使用 `execution_origin_scope(None)`，不授予父工具/模型权威。nested 仅沿
   私有原 operation scope 继承生命周期根；取消祖先后不能新增 nested 工作。
   父逻辑 Turn 已终态不抹掉已准入 op 的来源。
3. claimed/semaphore/current 均从原队列/原 op 捕获。所有原 op 先 fence，再取消原
   acquisition/run Task 一次并 join；取消等待者不会二次取消正在 finally 的生产者。
   RUNNING 回调 await 后再次检查 fence，不能迟到派发。旧 callback 不得借后继 op。
4. Native 原 finalizer Task 用本地强引用持续 shield 至 actual done。真实异常传播；
   调用者取消在 finalizer 真正退出后才传播，外部期限仍可报告 unknown。
5. 无子工作的 Native 精确退出允许常驻空 worker/drain；同时检查 queued、claimed、
   current/pending 操作、pending 活动及 emitter queued/current write。queue.empty 单独
   不能证明退出。此处不启用整棵子 Agent 树的精确取消。

## 拒绝的方案

不引入新队列、注册表、后台清理调度器或持久 authority；不取消整 Session 来清理一个
父来源；不把原 op 生命周期源安装成子工具权限；不在正常 EOF join/cancel 后台 child；
不以 COMPLETED 状态、EOF、空队列或 cancel ACK 代替真实任务和写入尾部退出。

## 验证

新增实际组件用例覆盖 carrier 序列化/复制、重复取消下真实 Native finalizer、queued /
claimed / running 精确取消与 foreign successor、状态回调等待中取消、失活 scope、
原 factory 等待撤权清理、迟到 callback、后台跨父终态正常完成、活动 gate 与真实写尾部、
真实 Native 无 child 的正常完成及旧 session_spawn 未证尾部拒绝。新增用例进入严格
stable 的发现与执行入口。受影响测试还包含现有 subagent runtime、工具链、Native 与
Round 组件。执行版本/命令/数量由交付证据记录；源码测试不是真实 Provider 验收。

## 已知遗留

- 新精确端口目前覆盖一个既有 SubagentControl 的原操作/活动。尚未接入 Runtime 原
  credential owner、完整 nested control 树和 child 独立资源权威，不宣称 B4 完整出口。
- 旧 `SessionSpawnExecutor` 不走 UserInputOp；完成事件可早于 finalizer/wrapper，并能
  创建未保留精确句柄的 delayed auto-invoke。受管 Native 遇到其当前内存 toolkit 记录、
  未终态 task、存活 wrapper 或待 auto-invoke 继续 unknown，不取消它们。
  toolkit 的 completed/canceled 记录仍无真实尾部证明，因此当前也拒绝；这是明确的
  保守兼容限制，不把状态/历史 task_id 称为活资源证明，更不能据此宣称支持该组合。
  不读取持久历史增加此限制；真正完成旧路径来源/退出接线后应替换该保守检查。
- 原 Native child stream 的全 child-control release 与 sticky child 的 foreign nested
  操作仍需后续精确化。普通父 EOF 不因本片新增后台等待；F32 受管完整后台组合仍待启用。
- 未运行真实 Provider 对抗、远端 CI 或最终 core/swarm 按锁安装配对验收。
