# F_23 Live execution origin

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | core controller、harness TaskLoop 与两个 Native child driver |
| 测试基线 | 372 passed / 1 skipped；新增来源用例纳入 stable |
| Refs | S_03_task-loop、S_18_subagents-and-lifecycle；agent_teams F_119 |

## 背景

Native 的长生命周期 supervisor、TaskScheduler 和 Team 协作队列不能借用消费者最近一次请求的身份。输入入队时必须保留原宿主准入来源，执行时由宿主重新检查该来源。

## 数据结构 / 状态机

`core.controller.schema.ExecutionOrigin` 包含 opaque `host_value`，只按对象身份比较。`execution_origin_scope` 安装单独词法句柄，退出后让继承它的子 task 读到 None，不让队列保存的来源对象失效。现有 Event/InputEvent 和 Team EventMessage 通过私有属性携带来源；JSON、恢复和 seed 不携带来源。沿原队列、ActiveRound、Task.inputs 传递，不增加调度状态机。

## 决策

- 缺省 send 捕获提交时 scope；显式 None 遮蔽来源。不同来源在 Native supervisor 合并、steer 和 follow-up 前拒绝；既有全 None 输入保持原行为。
- TaskIteration 从原 Task.inputs 读取唯一来源；TaskScheduler 完成事件沿同一 lineage 携带来源。异步 generator 每次 anext/aclose 安装 scope，yield 前退出，避免跨 task ContextVar reset。
- warm pause/resume、重试和任务计划延续保留原来源；冷恢复不补当前来源。受管 follow-up 不写旧持久字符串列表，恢复的无来源字符串不能并入有来源执行。
- 两个 Native 子 Agent 实际驱动入口显式 scope(None)，不借用 Team member 的来源。受管宿主应拒绝无独立来源的子 Agent；legacy 执行不变。
- NativeHarness/TeamHarness 的 `owns_execution(agent, session, origin=...)` 同时检查实际 Native 或 inner agent、实际 Session、active round 及来源对象身份；None 或已终止执行返回 False。

## 拒绝的方案

不把来源放入 protocol metadata、JSON、字符串 ID 注册表或第二队列；不从最新 round 恢复来源；不把 scope 退出解释为原宿主准入失效；不为子 Agent 继承 member 权限。

## 验证

新增确定性测试覆盖两个顺序请求、TaskLoop 原来源、晚到消息、混源拒绝、暂停/恢复、JSON 无来源、词法子 task 失效、双 child driver 与原 steering queue。受影响 core controller、Native/Team、子 Agent 回归：372 passed、1 既有 skipped（完整依赖环境，源码测试）。初始精简环境六项缺少 OpenTelemetry exporter；换用已存在完整依赖环境后同组通过，没有修改测试跳过规则或安装器。新增用例纳入 stable 的 discover、command 与 regression manifest；exact HEAD stable 单独留证。

## 已知遗留

来源本身不是授权，也不是可持久化的凭据。宿主仍须检查原 admission 生命周期、actor/member、Runtime generation、资源和模型逐次授权。范围为普通 inprocess Team 的 live carrier；未宣称产品 Team UI/API、远端部署、跨进程来源恢复、完整活动恢复或 Swarmflow 已验收。服务事件无来源，必须独立宿主授权，不能使用当前 member 作为回退。
