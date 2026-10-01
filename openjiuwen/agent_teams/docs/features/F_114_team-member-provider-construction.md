# Team 全角色 Provider 构造接缝

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-30 |
| 范围 | TeamAgentSpec、AgentConfigurator、宿主 MemberRuntime 构造端口 |
| 验证 | 本地 core 影响面 308 passed；不代表锁安装或真实 Provider E2E |

## 背景

现有 MemberRuntime 注入只在 external CLI spawn 调用者显式传入；leader、普通成员与冷恢复
仍直接构造 Native。产品选择同 Provider Team 时不能把旧 Native leader + external worker
当作全 External Team，也不能再创建一个 Team 调度器。

## 决策

- 在 TeamAgentSpec 保存 `execution_provider`（默认 native），沿已有 spawn payload 与 Session
  spec 序列化；不复制 AgentExecutionSpec、Binding、厂商配置或凭据。
- 宿主通过已有 BuildContext.extras 挂载具名的 TeamMemberRuntimeFactory；跨进程/冷恢复仍
  由现有 build_context_seed/factory 恢复。缺失工厂、Provider 不一致在构造基础设施之前失败。
- AgentConfigurator 是统一构造入口；工厂收到真实 member role/card/context 和已有 TeamBackend，
  返回未启动的 ExternalHarnessMemberRuntime。原 MemberRuntime、StreamController、Task/Review/
  消息与 Runner 不变；公共事件仍只由 HarnessIOAdapter 消费。
- 工厂的 validate_team_spec 是无资源分配的准入检查；build 也不得启动进程或监听服务，异步分配
  留给 runtime.start。没有工厂时显式拒绝，不能跌回 Native。
- Native 保持原工厂及模型检查；External 不以存在 Native model 配置作为构造前置。旧 CLI
  runtime 注入只在 native/default 路径保留；显式同 Provider Team 不接受绕过工厂注入。
- 未接通的 TinyAgent、Swarmflow、Native fork、HITT、bridge 与混合 CLI 声明失败关闭，不暗中启用 Native。
  这不是永久能力承诺；后续适配必须有同 Provider 与生命周期证据后再解除门禁。
- 外部成员宿主可显式关闭旧 CLI 的自动工具批准，交互仍复用 HarnessIOAdapter 请求/响应通道；
  旧调用者的默认行为保留，产品工厂必须根据已准入授权选择，不能用事件观察替代审批。
- 产品宿主可启用严格 checkpoint 校验：成员已有 external_runtime 状态但缺失有效同 backend
  checkpoint 时拒绝；旧 CLI 默认不变。TeamBackend 只读暴露原 history_restored 标记，供宿主在
  冷恢复 leader 上选择 REQUIRE_RESUME，不新增恢复状态存储。

## 拒绝的方案

- 不在 Spec 保存活 runtime 或回调；现有 excluded BuildContext 已负责宿主运行期句柄。
- 不增设另一套工厂注册表、任意 import-path 插件或序列化 Provider 私有配置；复用已注册宿主
  BuildContext 的恢复机制。
- 不只改 leader 或 external_cli_spawn：普通成员、process 与恢复必须走同一 configurator。
- 不让模型端点配置决定执行 Provider，也不在缺少 Provider 时借用 Native 模型兜底。

## 验证与遗留

本接缝的确定性验证不等于产品准入或真实 Team 六格。Swarm 的本地候选已接入冻结 Surface、
成员 Binding、工具 Gateway、授权与独立 checkpoint，并以真实 Codex/OpenCode CLI 加本地模型
验证成员工具、Turn 和恢复；产品控制/渠道及远端真实模型仍需独立验收。

定向构造 17 项覆盖 Codex/OpenCode leader 无 Native model、真实 SpawnPayloadBuilder/
from_spawn_payload、inprocess_spawn 到 Runner 成员调用、冷恢复 Provider 不变与准入负例。
扩大集合含原 configurator、Runner、恢复、External、IO adapter/base，共 308 passed。
旧基线相同受影响集合 254 passed，也有 SQLite worker 在测试 loop 关闭后的清理警告；
基线与候选日志分别保留，不忽略或增加失败白名单。审批新增用例验证拒绝响应不送进模型。

严格 checkpoint 另增 3 项损坏/错 backend/空记录负例；没有保存记录的新成员保留新建，
已有恢复状态不能静默新建。TeamBackend.history_restored 只读映射既有标记。
