# F_05 Provider-neutral SubAgent execution ports

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 日期 | 2026-09-22 |
| 关联 spec | `S_10_subagent-runtime.md` |

## 背景

产品子 Agent 的 registry、状态、历史与六类工具已经集中在 `subagent_runtime`，但子实例构造和
Turn 执行仍直接依赖 DeepAgent 与 Native Session：manager 调 `create_subagent()`，instance 调
`pre_run()` / `stream()` 并自行执行 Native finalize。仅把工具搬到 MCP 不能让 External 父执行
创建同 Provider 的子执行，也会诱导另建一套子 Agent 状态机。

## 决策

- 保留 `SubagentControl`、registry、worker 队列、状态、活动、转录和持久化为唯一产品运行时。
- 新增不可变 `ParentExecutionContext`、`SubagentBuildRequest`、`SubagentTurnRequest` 与
  `SubagentTurnResult`，以及 `SubagentExecutionFactory` / `SubagentExecution` 两个端口。
- `SubagentSessionManager` 只负责调用构造/恢复端口并装配既有投影回调；`SubagentInstance` 只负责
  串行、并发槽、取消、超时与状态结算。
- 默认 `NativeSubagentExecutionFactory` 封装旧 DeepAgent 构造、每 Turn Session、KV cache、
  资源 prepare/cleanup、Native stream 聚合与关闭，从而保持 Native→Native 行为。
- 父 Session 的组合根固定构造端口；spawn 参数不增加 provider/engine 字段。External 的独立
  Binding 与同父 Provider 继承在后续接线中通过新端口实现。

## 拒绝的方案

- 不在 `SubagentInstance` 中增加 `if native / if codex` 分支；这会复制 Provider 生命周期。
- 不让 Product ToolGateway 拥有第二套 registry 或状态机；传输层只转发既有工具。
- 不先建设 Codex→Native 或 Native→Codex 混合链路；产品契约是子执行直接继承父已生效 Provider。
- 不把父完整历史隐式复制到子 Session；端口只收到固定父上下文和显式任务输入。

## 验证基线

R1-03B2 以端口契约单测、`instance`/`session_manager`/`control` 定向回归及真实
Native→Native 特征链验证。后续 B3 已通过 Swarm 组合接入 Codex→Codex 与独立 Binding，B4 已复用
六工具完成产品渠道、并发、历史/UI 和父会话清理验收；这些组合证据记录在 Swarm/管理仓，不把
Provider 或产品 UI 反向纳入 core。

## 已知遗留

- core 只定义端口与 Native 默认适配；External 子构造、父 Binding 继承和产品渠道仍由宿主组合。
- 跨进程子 checkpoint 冷恢复、迟到事件和重放结果语义仍由 R1-04 补强。
