# scheduled 临时 reviewer 的宿主接线与精确回答

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-01 |
| 范围 | 原 scheduler 临时所有者查找、Runner interact；Swarm 候选 reviewer host |
| 前置 | F_116 构造/退出所有权；F_115 原 pending 精确回答 |

## 背景与决策

临时 reviewer 不在 roster，原 Runner interact 仅查 leader/spawn handle，无法回答其审批。
沿原 _review_runs 按独立 invocation owner 查找临时 runtime，复用原 InteractiveInput
地址与 pending/cycle 验证。停止期间、退出移除后、错误 owner 均拒绝；不新增 registry，
不广播、不降级普通输入。普通成员仍从原 spawn handle 获取。

Swarm 的同一个工厂现实现一次性 runtime，绑定原 Team Session、主体/Workspace/Provider、
独立 reviewer invocation 与原 Verify/View 工具。原 ExternalHarnessMemberRuntime/IO 承担
Provider 队列和唯一事件消费者，原产品投影承担历史、工具/产物身份和 root Goal 用量。
verify_task 调用前核对当前授权、绑定 task/review_round 与 IN_REVIEW；TaskManager 保留
最终 reviewer/投票/CAS 权威。临时 reviewer 不进入成员名册，不构造另一个 GoalManager。

原 Team Session 在派发前提交 pending，Provider/输出/历史/用量/MCP 全部确认后写 closed；
取消、失败或不明状态保留 blocked，冷恢复拒绝自动重放。Provider 清理失败仍保留原
runtime/transport，可经 scheduler 重试同一 dispose。缺终态和历史写入失败不制造成功。

## 拒绝的方案

不新增审批系统、前端控件、事件 reader、票据表或 Provider 状态机；不让临时 reviewer
借用普通成员地址；不以本机 CLI 正向解除产品 scheduled/global 准入限制。

## 验证与边界

core 扩大集 992 passed / 20 既有 skipped；新增临时 owner Runner 回答分支，并验证原
scheduler owner 在退出后不可寻址。Swarm 新增 26 项组件测试覆盖两 Provider 的单消费者、
历史/用量未知、持久状态重读、退出失败重试、拒绝重复输入、审批旧地址/重复回答、取消，
以及错误 task/round/status/授权投票拒绝。真实 Codex 0.144.4 与 OpenCode 1.18.18 使用
本机受控模型/assessor，经原调度/任务/MCP/Runner 完成评审和两次根 Goal，临时 reviewer
只执行一次、名册只有 leader/worker、用量包含评审、历史唯一、冷读 closed 状态保留。

测试专用覆盖工厂 scheduled 拒绝以运行候选；生产拒绝保持。真实在途 pending review
冷恢复/部分票据跨 attempt、失败与迟到交互的真实 CLI、远端模型/原渠道 UI/Cluster、
干净锁安装仍待验收。活动 Provider pause/resume 不支持。准确命令/源码哈希和所有初始
失败、超时、SQLite fixture 诊断记录归管理仓 R1-11F 验证第 17 节。


### 原 Team 输出接线补充（2026-10-01）

真实 Runtime/Runner 审批验收发现：临时 reviewer 仅写 Team Session 流，原 TeamAgent
实际消费 StreamController.stream_queue，因此审批已登记却无法到达回答端。现有
TeamReviewRuntimeBuild 增加可选 output_sink；原 scheduler 注入绑定当轮队列与
Team Session 的回调，队列或 Session 改变后拒绝迟到输出。宿主将投影包装为已有
TeamOutputSchema，保留 source_member 与 payload 中 scheduled_review/invocation 身份；
不把临时评审加入 roster。不新增流消费者、队列、registry 或协议事件。独立宿主
未提供 sink 时仍可使用原 Session stream；接入原 scheduler 的宿主必须提供原流所有者。

原 scheduler/Native/成员构造 52 项通过；Swarm 原 Runtime.stream→已绑定 facade→
TeamManager→Runner→scheduler owner 的 Codex/OpenCode 允许/取消 4 项真实 CLI 通过。
仅本机受控模型与候选配置，不代表 Web/TUI、远端模型、Cluster 或全局准入通过。
