# S_27 External Harness Member Runtime

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/agent_teams/external/member_runtime.py`、`openjiuwen/agent_teams/external/cli_agent/spawn.py`、`openjiuwen/agent_teams/spawn/external_cli_spawn.py` |
| 最近一次修订日期 | 2026-10-01 |
| 关联 feature | F_95_dsh-external-harness-adapter.md、F_96_protocol-harness-providers-and-member-migration.md |

## 范围 / 边界

本规约定义由宿主拥有生命周期的三方 Harness（`openjiuwen.harness_protocol.HarnessProtocol` 实现）
如何成为团队成员：`ExternalHarnessMemberRuntime` 的契约、它对 `harness_providers.HarnessIOAdapter`
的依赖、`build_cli_runtime` 对 claude / codex 的分派，以及 `external_cli_spawn` 的绑定顺序。

不在范围内：协议本身（`openjiuwen/harness_protocol`）、provider 实现（`openjiuwen/harness_providers`，
见 harness `S_19`）、adapter 型子进程 CLI（`CliRuntimeBase`，见 F_22 / F_25）、
`ExternalTeamClient` 直连接入面（F_21 / F_26）。

## 不变量

1. **只消费一次 `events()`**。member runtime 通过 `HarnessIOAdapter` 持有唯一的 continuous
   consumer；宿主永远不直接读取 harness 事件流，也不并发使用 `turn_events()`。
2. **投影在 adapter，团队语义在 runtime**。`OutputSchema` chunk 的形状（`llm_output` /
   `llm_reasoning` / `tool_call` / `tool_result` / `__interaction__`）由 `HarnessIOAdapter` 决定；
   member runtime 不复制投影逻辑，只叠加 team session、可靠性、观测、fallback 持久化与 MCP 挂载。
3. **宿主服务在 start 时注入**。`_host_context` 用 `dataclasses.replace` 把成员 child AgentSession 的
   checkpoint / checkpoint sink、`bind_mcp_servers` 的 MCP、adapter 的 interaction handler 与相应
   `HostCapability` 合并进 provider `HarnessContext`；context factory 只描述 provider-neutral 字段。
4. **checkpoint 落在成员自己的 AgentSession**。sink 写 `external_runtime = {backend: <card.name>,
   checkpoint: <envelope>}`；旧 `resume_external_backend=True` 有 checkpoint 时以 `ResumePolicy.REQUIRE_RESUME` 启动，
   无 checkpoint 时保留旧 CLI 新建行为。要求严格恢复的宿主必须显式传入
   `HarnessContext.resume_policy=REQUIRE_RESUME`；无同 backend checkpoint 时在 Provider 启动前
   抛 `HarnessStateError`。有 checkpoint 但非严格模式时用 `RESUME_IF_AVAILABLE`。
   宿主也可设置 `strict_checkpoint_validation=True`：若成员已有 `external_runtime` 状态但
   不能读出有效同 backend checkpoint，拒绝启动；未有记录的新成员仍允许新建。默认 False
   保留旧 CLI 兼容；此选项不能替代冷恢复时明确的 `REQUIRE_RESUME`。
5. **可靠性只由结构化事件驱动**。`TurnLifecycleEvent(STARTED)` → `begin_attempt(phase="turn")`；
   `FAILED` → 用 `TurnResult.error`（`category` / `code` / `provider_data.sdk_error_type` /
   `http_status`）`finalize_failure` 一次；`DiagnosticEvent(data.kind == "retrying")` →
   `publish_retrying`；`start` 抛出的 `ProviderStartupError.error` → `finalize_failure` +
   `mark_member_error`。runtime 不解析 SDK 异常文本。
6. **`immediate=True` 是能力感知的**。IDLE 时为 AUTO；RUNNING 且 provider 声明 STEER 时为 STEER，
   否则 FOLLOW_UP；STEER 与 terminal 竞争失败且 provider 已 IDLE 时重试 AUTO。abort 同理：优先声明的
   模式，其次另一种，最后按 `stop_on_unsupported_force_abort` 停止整个 cycle 或抛
   `UnsupportedHarnessCapabilityError`。
7. **团队上下文搭车语义不变**。`send` 在投递前把 `TeamContextTracker.pending_text` 拼到正文最前，
   投递成功后才 `commit`；`announce_team_context` 单独投递；`InteractiveInput` 不搭车。
8. **provider-private 观测接线不进公共协议**。Codex `notification_observer` 与 Claude
   `transport_factory` 只在 `build_cli_runtime` 构造 harness 时注入；span bridge 经
   `bind_span_bridge` 绑定，按 Protocol（`MemberSpanBridge` / `ChunkRecordingSpanBridge` /
   `NativeObservationSpanBridge`）而非 `hasattr` 分派。
9. **legacy 回调名保留**。`harness.state`（`old` / `new` / `session_id`）与 `harness.round`
   （`kind` / `round_id` = 协议 `turn_id` / `result` = `TurnResult | None`）是 `StreamController`
   的兼容契约；不得反向把 `round` 写进公共协议。
10. **认证 fallback 先持久化再生效**。runtime 把自己作为 adapter 的 `provider_interaction_handler`
    注入（因此 context 总是带 `HostCapability.PROVIDER_INTERACTION`），只应答
    `request_type == "auth_fallback"`：`bind_fallback_promotion` 绑定的 `promote()` 返回 `True` →
    `COMPLETED`，返回 `False` 或抛异常 → `DECLINED`（provider 随即回退原生端点）；未绑定 promotion
    时直接 `COMPLETED`；其它 request type 一律 `DECLINED`。`ProviderEvent("auth_fallback_activated")`
    只作日志观测，不再触发持久化。

11. **退出成功以资源确认收敛为准**。Provider/IO adapter 停止失败时保留成员 Session、回调和
    teardown hooks；禁止再次 start，重复 stop 重试同一周期。Provider 确认退出后，逐项运行
    teardown hook；成功项不重复执行，失败项聚合抛出并保留以便重试。全部 hooks 与成员
    `post_run` 成功后才标记 stopped。半启动失败也沿同一 stop 路径补偿，不能丢弃未确认退出资源。

## 接口契约

```python
class ExternalHarnessMemberRuntime:
    def __init__(self, *, harness: HarnessProtocol, context: HarnessContext | ContextFactory,
                 team_context_tracker=None, stop_on_unsupported_force_abort=False,
                 resume_external_backend=False, agent_kind: str | None = None,
                 inject_mcp=False, mcp_server_name="openjiuwen-team",
                 auto_approve_tools=True, strict_checkpoint_validation=False,
                 interaction_scope=None, event_observer=None, output_projection=None) -> None
    # pre-start bindings
    def bind_team_context_tracker(tracker) / bind_mcp_servers(servers) / bind_span_bridge(bridge)
    def bind_fallback_promotion(promote)   # promote: () -> Awaitable[bool]; answers the auth_fallback interaction
    def add_teardown_hook(hook)
    def bind_reliability_context(*, session_id, team_backend, leader_name, update_status_cb, messager)
    # MemberRuntime surface
    async start(*, team_session=None) / stop() / dispose(); state; session_id; outputs()
    async send(content, *, immediate=False) -> SendReceipt | None
    async announce_team_context() / abort(*, immediate=False) / pause() / resume(*, query=None)
    async subscribe(*, on_state=None, on_round=None)
    has_pending_interrupt() / is_pending_interrupt_resume_valid(user_input)
    # read-only
    provider_name; reliability_agent_kind; span_bridge; inject_mcp; mcp_server_name
```

`build_cli_runtime(ctx, ...)` 对 `ctx.cli_agent == "claude" | "codex"` 返回
`ExternalHarnessMemberRuntime`（provider 分别为 `ClaudeCodeHarness` / `CodexHarness`），其余 backend
返回 `CliRuntimeBase` 子类；返回类型别名 `MemberRuntimeLike`。claude 分支保留
`ExternalCliAgentSpec` 的 `cli_path` / `add_dirs` / `ssh_transport` / 模型与 fallback 语义，codex
分支保留 `codex_bin` / `bypass_approvals_and_sandbox` / `turn_idle_*` / `mcp_default_tools_approval_mode`
并把团队 MCP 作为 stdio `McpServerConfig` 预绑定。

`external_cli_spawn` 的绑定顺序：`configure` → `bind_team_context_tracker` →
`_bind_protocol_member_team_tools`（仅 claude-code 且 `inject_mcp`）→ `bind_reliability_context`
→ `Runner.run_agent_team(member=True)`；`finally` 调 `runtime.stop()`。

## 数据结构

| 项 | 位置 | 说明 |
|---|---|---|
| `external_runtime` state | 成员 child AgentSession | `{backend, checkpoint}`；`checkpoint` 是 `HarnessCheckpoint` 的 JSON 信封（`checkpoint_to_dict` / `checkpoint_from_dict`） |
| `_extra_mcp_servers` | runtime 内存 | start 前绑定的 `McpServerConfig` 列表，start 时并入 context |
| `_round_seq` / `_current_round_id` | runtime 内存 | 可靠性 `round_id`（单调整数），与协议 `turn_id` 并存 |

## 与其它 spec 的关系

- `S_18_harness-interaction-contract.md`：`MemberRuntime` 表面；本 runtime 是其外部 harness 实现。
- `S_19_reliability-framework.md`：`RuntimeReliabilityContext` 的 delivery 语义；本规约只定义事件到
  该上下文的映射。
- harness `S_19_harness-providers.md`：provider 骨架、IO adapter 与 factory 的契约。

## 全角色宿主构造端口（2026-09-30，F_114）

同 Provider Team 通过 `TeamAgentSpec.execution_provider` 和 BuildContext 中的
`TeamMemberRuntimeFactory` 构造成员；默认 Native 与旧 external CLI 注入路径保留。
工厂必须无副作用验证全团队配置，构造只返回尚未启动的 ExternalHarnessMemberRuntime，
异步进程/服务/工具资源在其 context factory/start/stop 周期管理。Provider 配置与授权继续由
既有 AgentExecutionSpec/Binding/HarnessContext 承载，不在 Team Spec 复制厂商配置。
成员 identity 来自构造请求的真实 card/context，不能把所有成员复用成 root Provider Session。

`auto_approve_tools=True` 保留旧 CLI 兼容；宿主可显式设为 False，经原 IO adapter 的审批
请求/响应通道等待批准或拒绝。该参数不替代宿主的运行时权限检查，也不新增事件消费者。

## Scoped 宿主交互与输出（F_115）

`interaction_scope=(team, member, root_session)` 可选启用精确交互地址，start 每周期更换 token。
Runner 只查原 leader / inprocess spawn handle，按地址核验唯一原 IO pending；raw、多目标、
错误成员/Session/cycle、已完成 future 和未知 ID 均拒绝，不作为新 Provider 输入。默认未启用
时旧 CLI 兼容不变。地址是关联标识，不代替宿主权限与 generation 检查。

`event_observer` 在同一事件 pump 上观察；`output_projection` 在原唯一有界输出消费者上
映射。`publish_output` 将产品输出放回相同预算队列；不增加 Provider 游标。开启 scope 后
Provider 和宿主产品问题均通过 runtime 的原 `handle/cancel` 通道，pending 权威仍为
HarnessIOAdapter。`answer_pending` 先验证所有条目再解决 futures，不带普通输入 fallback。

严格恢复拒绝 `external_member_interaction_pending` / `external_member_products_active`。
标记属于原成员 Session，不是第二份交互 ledger；成功 start 后确认所有资源退出才清除，
冷启动拒绝后的补偿不得清除此类标记。


### scheduled reviewer 扩展（2026-10-01，F_116）

公开 `TeamReviewRuntimeBuild`、`TeamReviewRuntimeFactory`、`TeamReviewRuntime`；
它们扩展同一成员 factory 的可选 scheduled 能力，不改变旧 autonomous factory 契约。
缺 reviewer 端口的显式 External scheduled Spec 在基础设施前拒绝。临时执行的原工具、
投票、退出所有权与恢复扫描契约见 S_22；不使用普通成员 card/roster/checkpoint 假冒身份。


### 临时 reviewer 宿主与精确交互（2026-10-01，F_117）

原 scheduler 暴露 review_interaction_target，按 _review_runs 的 invocation owner 查询；
停止期间拒绝，退出后移除。Runner 原 InteractiveInput 在普通成员未命中时使用该查询，
仍核对 team/root Session/cycle/pending，不新增 registry。宿主复用原 External IO/投影，
在原 Team Session 保存评审 pending/closed/blocked 并拒绝未知冷重放。产品 scheduled
仍关闭；真实本机 CLI 正向已验，pending 冷恢复及渠道/Cluster 尚未验收。
详见 [F_117](../features/F_117_scheduled-review-host-interactions.md)。
