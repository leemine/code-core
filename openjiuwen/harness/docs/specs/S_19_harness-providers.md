# S_19 Harness Providers（协议实现、IO Adapter 与 Manifest 工厂）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness_providers/`（`base.py` / `stream.py` / `io_adapter.py` / `factory.py` / `inputs.py` / `jsonsafe.py` / `native/` / `claudecode/` / `codex/` / `dsh/` / `opencode/`） |
| 最近一次修订日期 | 2026-10-04 |
| 关联 feature | `F_32_native-exact-turn-exit.md`、F_03_harness-providers-and-manifest-factory.md、F_07_opencode-provider-foundation.md、F_08_opencode-interaction-and-resume.md、F_09_opencode-managed-product-mcp.md、F_17_surface-runtime-policy.md、F_27_owned-turn-queue-interactions.md |

## 范围 / 边界

本规约定义 `openjiuwen.harness_protocol` 的内置实现包：共享的串行 Turn 骨架、六个 provider 的能力
声明、DeepAgent 风格 IO adapter，以及从 AgentTemplate manifest 创建 harness 的工厂。协议契约本身
以 `openjiuwen/harness_protocol/SPEC.md` 为准；团队成员接线见 agent_teams `S_27`。

## 不变量

1. **骨架唯一**：provider 继承 `SerializedTurnHarness`，只实现 `_open_session` / `_close_session` /
   `_execute_turn`（+ `_steer` / `_interrupt_turn`）。每个已接受输入恰好一个 STARTED 与一个 terminal
   `TurnLifecycleEvent`；stop 时排队中的 Turn 以 `HARNESS_STOP` ABORTED 收口；terminal 后队列为空才
   进入 IDLE。私有 `_capture_owned_turn` 仅查唯一原 active/queue 对象；
   `_cancel_queued_turn` 在原 command lock 按对象身份核验，只标记尚未 dispatch 的原项。
   同一 supervisor 按原序发 STARTED/USER_ABORT，不调用 Provider；它不是活动 Turn 退出接口。
   交互入账前固定原 PendingTurn：显式 turn_id 必须匹配原 active；None 只允许当时没有
   active 的 Session 级请求，不从后来 active 补来源。账本保留 request/handler/owner、
   handling 以及原 cancel_task；ID 在 handle 和 cancel 都真实结束前不能复用，cancel
   失败保留并上抛、后续可在同 entry 重试；因此公开 abort/stop 会传播原先被吞掉的取消
   回调错误，签名及正常成功路径不变。取消只等待原 snapshot，finally/done 只按 is
   清理原 entry；取消 waiter 不取消原 callback。迟到或已取消回复不能成为有效审批。
2. **能力声明真实**：

   | provider | card | capabilities | optional host capabilities |
   |---|---|---|---|
   | `native` | `deepagent` | STEER, FORCE_ABORT | USER_INPUT |
   | `native_v2` | `native_v2` | STEER, GRACEFUL_ABORT, FORCE_ABORT, PAUSE_RESUME, CHECKPOINT, PERSISTENT_SESSION | USER_INPUT, CHECKPOINT_SINK |
   | `claudecode` | `claude-code` | STEER, GRACEFUL_ABORT, PERSISTENT_SESSION, CHECKPOINT, MCP_TOOLS | TOOL_APPROVAL, USER_INPUT, CHECKPOINT_SINK, MCP_SERVERS, PROVIDER_INTERACTION |
   | `codex` | `codex` | 同 claudecode | TOOL_APPROVAL, USER_INPUT, CHECKPOINT_SINK, MCP_SERVERS, PROVIDER_INTERACTION |
   | `dsh` | `deepseek-harness` | MCP_TOOLS | MCP_SERVERS |
   | `opencode` | `opencode` | GRACEFUL_ABORT, PERSISTENT_SESSION, CHECKPOINT, MCP_TOOLS | TOOL_APPROVAL, USER_INPUT, CHECKPOINT_SINK, MCP_SERVERS |

   未声明的命令抛 `UnsupportedHarnessCapabilityError`；`_validate_context` 在 `start` 里 fail-fast。
3. **SDK 惰性加载**：config / provider / 包 import 不导入 vendor SDK；缺 SDK 在 `start` 抛
   `HarnessError`；SDK 启动失败抛 `ProviderStartupError(error: TurnError)`。
4. **失败词汇统一**：`TurnError.category ∈ {auth_required, quota_exceeded, rate_limited,
   server_unavailable, network_timeout, process_start_failed, sdk_error, unknown}`；
   `provider_data` 可带 `sdk_error_type` / `http_status`；`retryable` 由类别推导。
5. **JSON 边界**：进入事件的 vendor 对象一律先 `to_json_safe`；原始 SDK 对象只经
   provider-private 构造参数（`CodexHarness(notification_observer)`、
   `ClaudeCodeHarness(transport_factory)`）流向宿主。
6. **用户输入是 interaction**：Claude `AskUserQuestion`、Codex `request_user_input`
   （App Server 请求 `item/tool/requestUserInput`）、OpenCode `question.asked` 与 DeepAgent
   `ask_user` 中断映射为
   `UserInputRequest`，Turn 在应答前保持 RUNNING；宿主未提供 handler 时 Claude 拒绝该工具、Codex 回
   空 `answers`、OpenCode 调原生 reject、DeepAgent 以 `stop_reason="interrupt"` 结束 Turn 并保留
   `pending_interrupt_ids`。
   Codex 的该工具是 CLI 实验特性，宿主声明 USER_INPUT 时 harness 自动追加
   `features.default_mode_request_user_input=true`；多题请求渲染为一条 prompt，首题选项作 `choices`，
   原始 `questions` 进 `provider_data`，宿主答案按题 id / 题面 / 位置归一化回
   `{"answers": {id: {"answers": [...]}}}`。
   `DeepAgentHarness._open_session` 必须在 `agent.start` 之前 `ensure_initialized()`：DeepAgent 的
   交互循环不会自行初始化 agent，pending rails（含观测 rail）与 cwd ContextVar 都在此时落定，
   随后创建的 supervisor / scheduler task 才能继承。
7. **IO adapter 是唯一 DeepAgent 投影**：`HarnessIOAdapter` 输出 `llm_output` / `llm_reasoning` /
   `tool_call` / `tool_result` / `__interaction__`（`InteractionOutput(id=request_id, value=...)`）；
   `tool_result` 的 `result` 是结构化值，provider 提供模型可见文本时另带独立字段 `rendered_result`
   （DeepAgent：`_ObservationRail` 写入流式块，`_consume_chunk` 放进 `ItemLifecycleEvent.data` 与
   `ContentBlock.data`，见 `S_05` 不变量 11）；
   DELTA 直出，FINAL/SNAPSHOT 只补前缀增量；`send(InteractiveInput)` 先应答 pending interaction，
   未匹配时以 `metadata.kind="interactive_input"` 转发；`delivery_mode(immediate)` 按状态与 STEER
   能力选 AUTO / STEER / FOLLOW_UP。
8. **provider 扩展先 ratify 再提交**：会改变 provider 持久身份的一次性切换（当前只有 Claude Code /
   Codex 的认证 fallback）在生效前经 `SerializedTurnHarness._confirm_provider_extension(request_type,
   payload)` 发 `ProviderInteractionRequest`（`request_type="auth_fallback"`，payload
   `{model, api_base[, provider]}`）。宿主未声明 `PROVIDER_INTERACTION` 或未装 interactions 时视为
   同意；声明了但应答非 `COMPLETED` 时 harness 必须断开 fallback client、用原 session / thread 重连
   原生端点并让当前 Turn 按原 `auth_required` 失败，不发布 `auth_fallback_activated`。
9. **manifest 是 DeepAgent-first**：`create_harness` 对 `native` 传整份 template
   （`NativeHarnessProvider.create({"deep_agent", "agent_template", "session_id", "language",
   "event_buffer_capacity"})`）；对 `claudecode` / `codex` / `dsh` / `opencode` 只把 `model` 端点映射进 provider
   配置（显式 `config` 优先），manifest 的 `tools` / `rails` / `subagents` 非空时
   `ValueError`。`build_harness_context` 对三方 provider 渲染 prompt sections 与 MCP，对 `native`
   只放 `extra_system_prompt`。

10. **Codex 权限不随模型隐式提升**：只有显式 `bypass_approvals_and_sandbox=True` 才设置
    `deny_all + full_access`；选择模型、外部端点或认证 fallback 本身不关闭 sandbox。
    宿主声明 TOOL_APPROVAL / USER_INPUT 时，SDK 审批钩子必须存在且成功保留；不兼容时在
    thread start/resume 之前失败，并关闭新建 client。TOOL_APPROVAL 要求交互 handler 且禁止 bypass；
    start/resume/fallback 显式协商 `untrusted + user + read-only/workspace-write`，并核对服务器回显。
    配置模型与权限开关分离，权限开关只接受 bool。命令、补丁和原生 MCP 工具审批走既有
    ToolApprovalRequest；未知 MCP elicitation 拒绝，审批超时取消宿主等待。

11. **Codex 进程隔离必须实际生效**：`inherit_process_env=False` 使用 sdk_compat 的实例级启动接缝，
    只传递选定 env（及 SDK 捆绑 PATH），不修改宿主 os.environ 或 SDK 全局。锁版本为 0.144.4；
    缺少所需私有布局即失败关闭。此适配需随 SDK 升级重新实测。

12. **Codex cwd 不构成读取隔离**：锁定 0.144.4 的 legacy readOnly/workspaceWrite 均允许
    cwd 外读取；untrusted 下受信任 cat 不触发宿主工具审批。写入限制与读取授权是不同边界。
    TOOL_APPROVAL 只处理 CLI 实际发出的审批请求，不承诺所有文件读取都会经过宿主。
    Linux helper 不能在 `/tmp` 下的 CODEX_HOME 创建；该场景的 sandbox retry 拒绝不等于正常读取隔离。

13. **Codex 宿主审批路径保留命名权限配置**：先通过 config/read 获取有效服务端配置。
    存在 default_permissions 时只发送 permissionProfile，不发送 legacy sandbox；二者配置冲突、
    未知 profile 或线程级 profile 定义均失败关闭。使用原始响应确认 activePermissionProfile.id、
    untrusted/user 和受限 sandbox；锁 SDK 生成响应类型丢失 profile 字段，不能据其证明读取范围。
    选中 profile、继承链与 cwd 的配置指纹写入 Provider 私有 checkpoint；start/resume/fallback/回退
    复用同一建连接缝，指纹变化在 thread 请求前拒绝。带指纹的 checkpoint 不允许移除 TOOL_APPROVAL；
    历史无指纹 checkpoint 按当前配置首次绑定。不承诺运行中动态配置/文件系统竞态或其他读取渠道隔离。
    无命名 profile 时保留受限 legacy 行为及其全盘只读边界；旧显式 bypass 的非宿主审批调用保留。

14. **Codex 输入加载与工具读取分界**：锁定 0.144.4 的 AGENTS 自动注入、原生 SkillInput/
    LocalImageInput 读取不继承命令可读根；project_doc_max_bytes=0 可抑制 AGENTS 自动注入。
    模型 view_image 已实测受命名可读根限制，但当前 core 的 HarnessInput 仍只序列化文本，
    不等于原生附件接线。portable SkillSource 在 CLI 启动前由宿主复制；stdio MCP 启动也不受
    命令 profile 文件 ACL 约束。MCP prompt 的工具允许/拒绝有效，显式 approve 不调用宿主审批；
    TOOL_APPROVAL 不承诺拦截配置加载、服务启动或所有 MCP 调用。来源授权与进程隔离由宿主负责。

15. **显式受限启动来源准入**：CodexHarnessConfig.startup_source_roots 为 None 时保留旧行为；
    非空绝对目录数组启用准入，授权依据由可信宿主提供，不能将用户输入的目录视作授权。
    要求 TOOL_APPROVAL、关闭 env 继承和 bypass、显式分离的 HOME/CODEX_HOME/cwd、命名权限；
    cwd 必须在来源根内。Skills 显式来源和已知自动发现目录在读取内容/复制前做真实路径与树检查。
    stdio/in-process、远端或无认证 MCP，ambient 插件、hooks、未知配置/特性在该模式拒绝；B1 只例外准入
    宿主按 Session 管理的 loopback HTTP MCP：必须显式端口、仅 Bearer Authorization、required=true 且工具审批为 prompt，
    有效配置回读必须保持精确 server 名称集合、认证头和限制，并只接受锁定 0.144.4 回报的
    enabled=true、environment_id=local、tool_timeout_sec=null 惰性默认；环境 AGENTS、login shell 和已识别自动来源特性强制关闭，
    config/read 只接受核验过的有效配置及锁版本精确空默认项。未提供受管原生插件快照时同时关闭
    features.plugins，阻止后台插件市场同步；显式 `native_plugins` 快照只准入其固定插件树，并保持
    remote_plugin=false。
16. **Codex 原生插件由 CLI 装载、宿主快照失败关闭**：`native_plugins=None` 保留旧 CLI 所有权；显式数组要求
    `inherit_process_env=false` 和隔离的绝对 `CODEX_HOME`。C1 首期只接受部署预置的本地 marketplace 来源；每个快照固定 `<name>@<marketplace>`、原生来源、版本、
    包内容 SHA-256、启用状态、必需 Skills/MCP 组件与 MCP server 名称。Provider 在 CLI 启动前核验安装缓存、
    manifest、摘要、C2 组件和名称冲突，随后通过 plugin/list、plugin/read 与 mcpServerStatus/list 回读原生装载结果；
    显式启用的缺包、版本/来源/摘要/组件/鉴权或 MCP 启动异常均使 Session 启动失败。hooks、commands、agents、apps
    不在 C1；`config_overrides` 不得改写插件控制。插件指纹进入 checkpoint，包内容变化使当前 Turn 失败并关闭
    Provider；启停变化只由新 Session 的宿主 Binding 快照生效。harness 不安装、更新或删除插件。
    启动覆盖仅将规范化 cwd 临时设为 untrusted，并回读确认唯一 projects 项，避免可写 profile 自动持久化祖先仓库信任；
    用户文件/线程/覆盖中的 projects 表仍拒绝，不新增信任授权。内置 Skill 缓存须由宿主单独登记，不自动授权整个 home。
    来源范围指纹随原 checkpoint 保存，恢复时改变范围或开关在复制前拒绝；连接/fallback/每轮前复检。
    新增不准入来源使该轮失败并关闭 client。Skill 安装仍早于 SDK 加载，旧 Team 显式 approve/bypass 不改。
    这是来源准入与受限启动，非 OS 文件 ACL；同 UID 修改、检查使用竞态、硬链接及全部进程隔离不由此保证。
17. **受管产品 MCP 不扩大协议或工具所有权**：产品宿主仍拥有原工具目录、主体/父 Session/工作空间路由和授权；
    Codex/OpenCode Provider 只消费 HarnessContext 的 MCP 配置并走原生工具审批。临时 URL/token 不进入稳定来源范围身份。
    Codex 的 server 名称进入来源指纹；CLI 有效配置丢失认证、出现额外 server、改变 required/prompt 或改成非 loopback 时启动失败。
    OpenCode 的产品保留 server 只接受显式端口的 `127.0.0.1` HTTP、唯一 Bearer Authorization
    头和禁用 OAuth 的远端 MCP；
    完整有效配置逐 generation 封存和回读，但稳定 scope 身份继续按无临时 MCP 的既有算法计算，使旧 Session 可冷恢复。
    Provider 不复制产品工具、不启动通用 MCP 注册中心，也不把 Team operator 权限用于 Single。

18. **Codex 停止等待原生退出确认**：interrupt RPC 应答不等于工具退出。每次物理 Turn 在
    turn/start 发出前保留独立的 client、启动回执及唯一 SDK reader；只有相同原生 turn id 的
    turn/completed 且该 reader 排空才允许关闭 App Server 或重试输入。idle timeout 不取消正在
    消费的 SDK anext；等待超时、错误或无匹配 terminal 的 EOF 均保留原所有者供停止重试。
    停止同时等待在途连接清理，禁止迟到连接发布新 client 或派发输入；关闭 client 后保留并
    等待 SDK 子进程句柄，确认失败不能发布 TERMINATED。Linux 启动器以独立私有 subreaper
    包装 App Server，不修改宿主进程；CLI 退出后继续回收被收养的工具后代，直到无自有子进程
    才以 0 退出。包装器被强杀或清理失败不是退出确认。其它平台不宣称该 Linux 进程树保证。
    清理锁只串行资源释放，不另建 Turn 队列。
19. **Surface runtime policy 只做冷启动收窄**：宿主提供的 `HarnessRuntimePolicy` 先于 Provider
    资源分配编译。Codex 要求关闭进程环境继承并提供可信 `startup_source_roots`；normal 的
    read-only/workspace-write 走既有宿主审批并回读 sandbox/来源；命名权限沿原生 `thread/start`
    参数生效，不同时注入互斥的 legacy `sandbox_mode`。已冻结 full-access 仍保留显式来源
    覆盖并单独回读，不把文件权限放宽等同于 ambient context 放宽。OpenCode 在 generation 私有配置中
    生成精确 permission map，并经现有 `/config` 回读。Plan 只能 read-only，策略不能扩大构造授权；
    checkpoint 只保存策略 fingerprint 供权限指纹复用判断，策略 revision 不成为 Binding 身份。
    未提供 runtime policy 的旧调用保持原行为。

20. **Codex steer 回执以原生接受为准**：turn/start 尚未返回时仅保存待确认命令与 Future，
    不提前返回 STEER 回执。原生明确以 `-32600` + `no active turn to steer` 拒绝时，
    输入尚未被接受，Provider 委托原 `SerializedTurnHarness.send(FOLLOW_UP)` 排队并返回
    新 Turn 的 FOLLOW_UP 回执。原回合继续由同一 reader 排空、发布自己的终态与用量，
    不因这次明确拒绝被中断或改判失败。其它错误与未确认结果不自动重发；启动失败、
    取消或停止释放等待者，未派发的取消命令不送入 SDK。没有第二套 Turn 队列或状态机。

## 接口契约

```python
def create_harness(manifest: AgentTemplateSpec | str | Path, *, provider: HarnessProviderName,
                   config: Mapping[str, Any] | None = None, language: str | None = None) -> HarnessProtocol
def build_harness_context(manifest, *, provider, host_session_id, agent_id=None, agent_name=None,
                          language="cn", cwd=None, env=None, extra_system_prompt=None,
                          host_capabilities=frozenset(), resume_policy=ResumePolicy.NEW,
                          checkpoint=None, checkpoint_sink=None, interactions=None, metadata=None) -> HarnessContext
def resolve_provider(provider: str) -> HarnessProvider
PROVIDER_NAMES == ("native", "native_v2", "claudecode", "codex", "dsh", "opencode")

class HarnessIOAdapter:
    def __init__(self, harness, *, event_observer=None, auto_approve_tools=True,
                 stop_on_unsupported_force_abort=False, provider_interaction_handler=None)
    # provider_interaction_handler 非空时 prepare_context 追加 HostCapability.PROVIDER_INTERACTION，
    # handle(ProviderInteractionRequest) 转交该 handler；为空时一律 DECLINED
    async start(context) / stop(); outputs() -> AsyncIterator[OutputSchema]
    async send(content, *, immediate=False) -> SendReceipt | None
    async abort(*, immediate=False) / pause() / resume(*, query=None)
    has_pending_interrupt(); is_pending_interrupt_resume_valid(user_input); pending_interrupt_ids
    async handle(request) / cancel(request_id, *, reason)   # HarnessInteractionHandler
```

provider 配置模型：`ClaudeCodeHarnessConfig`（`cwd` / `add_dirs` / `env` / `inherit_process_env` /
`cli_path` / `model` / `fallback_model` / `session_id` / `permission_mode` / `system_prompt_mode` /
`include_partial_messages` / `max_turns` / `settings` / `event_buffer_capacity`）、
`CodexHarnessConfig`（`cwd` / `env` / `inherit_process_env` / `codex_bin` / `model` / `fallback_model` / `native_plugins` /
`config_overrides` / `thread_config` / `bypass_approvals_and_sandbox` / `turn_idle_timeout_s` /
`turn_idle_retries` / `max_will_retry_count` / `mcp_*` / `client_*` / `experimental_raw_events` /
`event_buffer_capacity`）、`DshHarnessConfig`（镜像 `DeepSeekHarnessConfig` + `launch_args_override` /
`system_prompt_env_var` / `event_buffer_capacity`）。`from_mapping` 拒绝未知字段。

## 数据结构

| 项 | 说明 |
|---|---|
| `PendingTurn` | `content` / `message_id` / `turn_id` / `accepted_mode` / `abort_requested` / `abort_mode` / `stop_requested` |
| checkpoint data | claudecode `{session_id, resumed}`；codex `{thread_id, resumed[, fallback]}`；opencode `{session_id, resumable, state, resumed, turn_id}`；dsh / native 不发布 |
| `ProviderEvent` | claudecode `system/<subtype>`、`auth_fallback_activated`；codex 未识别 notification、`auth_fallback_activated`；native 未识别 chunk 类型 |
| `DiagnosticEvent` | codex `data.kind == "retrying"`（WARNING）与 pending error（ERROR）；claudecode assistant error（ERROR） |

## 与其它 spec 的关系

- `S_01` / `S_02`：`native` provider 通过 `DeepAgentSpec.build` 与 DeepAgent 交互循环
  （`start` / `attach_output` / `send_input` / `cancel_round` / `stop`）驱动。
- `S_12` / `S_13`：manifest（`AgentTemplateSpec`、`load_agent_template_package`）是工厂输入。
- agent_teams `S_27`：团队成员如何组合本包的 adapter 与 provider。

`native_v2` 实现在 `agent_teams/harness/protocol_adapter.py`，统一工厂只在显式选择时惰性加载。
它复用 NativeHarness 的 manifest snapshot 装配和边界停止，支持父上下文/任务状态 checkpoint 冷恢复。
`native` 的 DeepAgent 实现不变。详情见 team F_97/F_98。

## 系统提示词模式

Codex 和 DSH provider config 新增 `system_prompt_mode: append | replace`，默认 replace 保持兼容。
Codex append 读取 app-server config/read 的生效 developer_instructions，并优先采用显式
thread_config.developer_instructions，再追加宿主提示词；每次连接重新从原始配置构造，避免 resume
或 fallback 重复追加。读取失败则启动失败，不静默降级为替换。replace 直接设置字段；均不修改
base_instructions。空宿主提示词不覆盖现有字段。

DSH append 注册独立末尾 section，不改变 prefix/suffix，宿主文本按字面量处理；replace 通过
assembly hook 仅替换 prefix 文本，保留其它 sections（新 prefix 仍遵循原生模板语法）。
显式 system_prompt_env_var 和 append 冲突时校验失败。见 team F_100。

## Portable skills

三方 provider 接受 manifest.skills；每个 SkillSpec.dir 可指向单个含 SKILL.md 的 bundle 或包含多个
bundle 的 library。包路径按现有 manifest loader 解析为绝对路径，内存配置也建议传绝对源路径。
同名的 config.skills 显式覆盖 manifest 声明；skill_conflict 为 skip（默认）或 replace。

start 在 SDK/CLI 启动前复制完整目录到 cwd/.claude/skills（claudecode）、
cwd/.agents/skills（codex）、cwd/.dsh/skills（dsh）。OpenCode 使用项目内按配置指纹隔离的
cwd/.openjiuwen/harness-skills/opencode/<fingerprint>，仅将该精确路径编译到原生
`skills.paths`；不把副本放入 ambient 默认扫描目录。cwd 优先取 HarnessContext，再取 provider
config，再取当前进程目录。
名称取 SKILL.md front matter.name，缺省取目录名；同名按不区分大小写比较，同时识别已有目录里的
声明名。enabled_skills 非空时筛选声明名；mode 仍被解析校验，但原生 CLI 决定加载/调用方式，
不仿造 DeepAgent 的 auto_list 工具。skip 保留已有目录全部内容，replace 完整替换（清除旧文件），
多源重名按声明顺序处理。临时完整副本切换失败会恢复原目录。复制结果跨 stop 保留。

复制保留普通文件、子目录、隐藏资源和可执行位；内部链接物化成文件，越界/循环链接拒绝。
Claude 指定 portable skills 时显式启用 SDK skills=all，并保留 user/project/local settings 来源；
DSH sdk-minimal 自动挂载原生 skill/skill-filesystem/tool-skill 插件。SSH/custom Claude transport
不自动上传本地技能，显式报错而非复制到错误主机。见 team F_101。

## 公共授权构建

`AgentExecutionSpec.authorization` 可选；`None` 保留旧四项配置指纹与厂商策略。显式
`ExecutionAuthorization(full_access: bool)` 是可信宿主快照，加入 Binding 指纹。
可选 `HarnessAuthorizationProvider` 提供编译与旧授权读取；未实现者拒绝显式授权。
Codex 编译对应 bypass/MCP 参数，Swarm 不再解释这些字段；原运行时权限回读仍执行。
旧 Web full_access 的 Codex profile 由 core 兼容投影保留完全相同的旧 JSON，
不自动给旧 profile 注入新字段，也不修改已有 Binding/恢复归档校验。

## OpenCode OC1–OC5 构建、交互、Skills/MCP 与受管服务边界

OpenCode 首批固定 1.18.18 的 `/session` + `/event` HTTP/SSE 代际，复用
SerializedTurnHarness 的输入队列、事件信封与唯一终态。配置与工厂导入不启动进程；
运行要求受信非 root Linux、用户级 systemd/cgroup v2、明确授权的私有 runtime_root 和 cwd。
每个宿主/agent/workspace scope 独占锁与随机 service；资源描述先于启动落盘，重试先核验并
回收该描述所属的孤儿 unit。service 内 wrapper 持有原生启动锁，禁止旧排队启动跨 generation。
不能确认退出则保留所有权和描述，禁止新建/attach；数据不自动删除。

新 Provider 配置仅接受模型、显式 full_access、portable skills、CLI/私有运行根与有界传输参数；不接收任意
原生 JSON、环境、插件或可执行覆盖。公共授权在 Provider 编译到私有 full_access；旧工厂
未声明授权时保留默认普通策略，模型配置不能扩权。HOME/config 封存、managed/auth 来源拒绝、
固定二进制与有效配置回读在启动完成前执行，每轮前复检。cgroup 用于资源回收，非 OS 沙箱。

OpenCode 支持基础文本/原生工具观察、宿主审批/提问、graceful abort 及完成态会话 checkpoint/续接。
审批与提问经基类 awaited interaction，Provider 在回复原生 API 前核对 session、当前根消息、call 和
request ID；重复/迟到/跨 session 请求不能再次执行。宿主未声明对应能力时失败关闭。审批拒绝须有
关联 tool error、completed/tool-calls 及 idle 后才以失败终态收口；不能伪造成正常 stop。

每轮提交前发布 `resumable=false/state=turn_active`，只有原生 stop、已确认拒绝或 MessageAbortedError
与后续 idle 关联后才发布 `resumable=true/state=idle`。恢复固定同 scope 的原生 session ID，并核对
session/status、pending permission/question 及最后完成消息；活动/待答/结果未知 checkpoint 明确拒绝，
由宿主保持只读历史，不能静默创建新会话。原生 data/state 在 scope 内跨随机 service generation 保留；
HOME/config/cache/tmp、来源快照、launch 和日志仍逐 generation 隔离。

未适配的 Hook、steer/pause 命令明确拒绝，不忽略输入。ambient Skills 及项目配置扫描仍禁用；
portable skills 只从宿主配置的完整 bundle 复制到独立显式路径。未配置 Skills 的新 Session 不会发现
历史副本；不同源选择不共用扫描根。

普通宿主 MCP 接受显式 stdio、HTTPS 或 loopback HTTP，且关闭 OAuth；拒绝配置插值、
HTTP 远程明文、userinfo/fragment、in-process、非法/重名 server。该配置进入稳定 scope 身份。
产品保留 MCP 仍只开放唯一 Bearer 认证的 `127.0.0.1` HTTP，其临时 URL/token 按 OC4 兼容
要求排除于稳定身份。完整本代配置始终封存和回读。不自动批准原生交互，也不把普通策略解释成
full-access。
流与 HTTP 响应有上限；EOF、裸 idle、204 均不是成功。成功要求匹配本轮 user messageID 的
assistant 完成消息、原生 stop 原因、后续 idle 和权威消息回读一致。异常/超时/断流后停止
受管服务、禁止未知副作用重试；SSE 断开会取消宿主待答并产生未知失败，不自动重连或重放。活动 Turn/
待答的冷重建仍不支持，不复制第二套 Turn 状态机。


### OpenCode host lease 失效清理

私有 systemd launcher 观察既有 host.lock 与 generation owner 描述。宿主进程崩溃释放
租约或恢复替换 owner 时，launcher 停止本代 CLI 并退出，由原 KillMode=control-group
收敛其后代。无宿主租约时禁止启动；不等待新请求重新构造 Provider 才清理孤儿进程。
这不恢复活动 Turn，不改变已有私有 cgroup/lease 身份核验。


### OpenCode token normalization

The pinned OpenCode v1.18.18 native token record contains disjoint buckets.
Protocol input includes fresh input plus cache reads and cache writes; protocol
output includes visible output plus reasoning. Cached and reasoning counters
remain subsets, not extra charges. Native total is preserved, so an inconsistent
provider total remains detectable. Missing or invalid component counters leave
the corresponding aggregate unknown; snapshots for the same message are not
counted twice. This applies to Single, Team members and scheduled reviewers.
Source: [OpenCode v1.18.18 getUsage](https://github.com/anomalyco/opencode/blob/v1.18.18/packages/opencode/src/session/session.ts).


OpenCode model configuration accepts positive `context_window` and
`max_output_tokens` (defaults remain 32000/4096); output cannot exceed context.
These limits are emitted into the owned native configuration and verified by
normal config readback. They let deployments configure the actual selected
model budget without private patching. A native completed `finish=length`
record fails the Turn explicitly as `model_output_limit_exceeded`; it neither
waits for the overall timeout nor treats an incomplete answer as successful.

Default model budgets preserve the historical storage fingerprint byte for byte;
explicit budget changes remain part of the bound configuration identity.

### OpenCode terminal error diagnostics

Native `session.error` and assistant `message.updated` errors preserve a finite
`native_error_name` and `error_source` in `TurnError.provider_data.opencode`.
Allowed names are `UnknownError`, `MessageAbortedError`, `ProviderAuthError`, and
`APIError`; other non-null values become `unrecognized`. The safe labels also
appear in the existing terminal message so message-only host history keeps the
diagnostic. Raw native messages, response bodies, and request content are never
included. Existing error codes, HTTP status categories, retryability and actual
abort handling remain authoritative; these labels do not diagnose historical
failures or authorize replay.

### OpenCode mandatory preflight（固定 1.18.18）

声明 `HarnessContext.tool_authorizer` 的 OpenCode 必须在第一次 `start` 前调用
`bind_preflight_endpoint(OpenCodePreflightEndpoint(...))`，且只能绑定一次。该 Provider 私有值对象
只接受 authenticated `127.0.0.1` HTTP endpoint；令牌不进入 repr、JSON 配置或 checkpoint。
宿主 listener 先验证 Bearer、generation 与自身生命周期，再调用 `authorize_preflight(payload)`。
不满足接线条件在进程分配前拒绝；无 mandatory authorizer 的旧路径保持原有行为。

受管模式使用唯一内置 `tool.execute.before` gate，复用 native plugin staging、精确库存、文件摘要、
有效配置回读和每 Turn 来源核验。gate 固定原始参数对象后 awaited 宿主决策，拒绝/超时/异常必须抛错，
从而不进入原生执行体；不能用晚于 write/edit 原内容读取的 `permission.asked` 代替。
首版原生参数仅接受 read/write/edit/bash 的明确字段与规范绝对路径，拒绝额外原生插件、portable
skills、普通 MCP、其他原生工具与未证明组合；这些限制不代表相关能力已验收。

请求为 `{version:1,generation,nonce,session_id,call_id,tool,args}`，响应严格为 `{allowed,nonce}`。
每 Turn 固定实际提交的 native user message ID。每次授权捕获 Turn/context/callback/native
Session/generation/transport，先独立 GET 原生消息，不等待唯一 SSE 消费者：要求唯一 user root、
唯一 callID tool-part、assistant parent/session、part message/session、running 状态及原工具/参数
全部匹配，才允许进入宿主 callback。查询缺失、重复、异常或 await 后范围变化均拒绝。不能把首次
迟到的旧 Turn 调用按到达时间归入新 Turn；generation tombstone 仅去重，不能证明 root 归属。后续
原生 permission 必须匹配已证 assistant message 与同 call 的原工具/参数。审批及审批后复核沿用原交互通道，拒绝参数重写、
未知/重复/迟到 permission。完整记录每 Turn 退出或取消清理；generation 内仅保留有界 call/nonce
去重标记，256 calls/Turn、4096 calls/generation，满后拒绝且不驱逐复活，结束 lifecycle 后销毁。

唯一保留产品 MCP 可与 gate 共存：必须与 endpoint 同 listener、同 Bearer，server 名固定为
`jiuwenswarm_product_tools`，原工具名由宿主 bound ToolGateway 编译为 endpoint 的不可变精确名单。
固定 CLI 生成 `sanitize(server) + '_' + sanitize(tool)`，该 server 的命名空间不能覆盖原生工具。
gate 与 permission 对此类记录只复核精确来源/名单和当前范围并执行原审批；资源授权仍在产品
ToolGateway.invoke 的最终执行边界，不通过 native mapper 推断。仅匹配前缀不能获得权限。
固定 gate 源码和产品名单进入稳定配置/checkpoint 指纹；临时 URL、token、generation 不进入。

该接缝不是 OS 沙箱，也不证明符号链接竞争、所有 CLI 内部读取或完整 Provider/Team 验收。
宿主仍须实现当前文件 read/write 资源映射、可信路径政策及最终产品工具授权。


#### Product MCP 原 Turn ticket（Provider 私有传输接缝）

固定 CLI 的 MCP `callTool` 仅发送 name/arguments，不携带 native call ID。
不能按参数相等、到达时间或新建随机 call ID 归属到当前 Turn。对于 endpoint 精确登记的产品工具，
preflight 在原生 GET root/input 证明成功后返回随机一次性 ticket；内置 hook 将其原位写入
`PRODUCT_TICKET_FIELD`（`__openjiuwen_product_ticket`），模型预先提供该字段即拒绝。
该字段仅用于认证宿主的 MCP wire 参数，不能进入宿主工具输入、历史或日志。

宿主在解析认证 MCP 调用后使用
`harness.consume_product_preflight(local_tool_name, wire_arguments)`，成功时返回**原始**
`BeforeToolContext` 对象；失败返回 None。此调用先销毁 ticket，再验证完整原参数、精确产品名、
原 session/Turn/root/context/transport、原生审批已允许和当前生命周期。参数不符也不能重试 ticket。
宿主仅使用返回对象的 arguments、turn_id 和 call_id，丢弃 wire arguments，不能自行复制对象冒充证明。

该 ticket 只证明来源和生命周期，不授予最终工具或资源权限。宿主仍在现有 ToolGateway 完成当前权限、
资源和撤权检查，并在异步授权后及获得执行锁后调用
`harness.is_product_preflight_current(operation)`；仅同一已消费对象且原 scope 仍有效时为 True。
Turn 结束、abort、stop、gate clear/close、context/session/transport/root 改变均使证明失效。
不新增事件消费者、harness 协议字段或 Runtime 状态机。旧模式无 preflight 时这两个 API 均拒绝。

源码路径与合成测试确认固定 CLI before-hook 后直接将相同参数对象交给 MCP callTool；
CLI 原生持久化历史不含传输 ticket 仍需普通真实产品 MCP 验证，不以此确定性测试代替端到端验收。

## Native 原 Turn 退出端口

base 私有同步 `_make_pending_turn` 默认构造原 PendingTurn，Native 仅在显式
`NativeHostHooks.capture_execution_origin(content)` 配置时构造私有子类。该 hook
是 keyword-only、同步且 live-only：必须返回 ExecutionOrigin，None/异步值/异常
不能降级 legacy。入队前在原 command lock 中捕获；resume/steer 保留原 root。

`_abort_owned_turn(expected, mode=FORCE)` 只处理原 active/queued 对象，不改协议。
queued 原对象仅设置原 abort 标志；active 原对象在首次 await 前关闭来源并保留
实际 attach/dispatch/steer Task、原 interaction entry 和退出 handle。取消 ACK 之外，
必须等原 handler.handle 的 finally；不等待共享 Native supervisor 自己退出。

外部调用使用原 5 秒预算且 shield 原 cleanup Task。超时/调用方取消/未知资源不会
resolve 原 confirmed Future；execute 出口继续阻塞，重试仍使用原 handle。错误明确
向调用方传播，不把失败伪装成成功取消。正常 EOF 同样等待原生产者退出。Native stop 也先确认原托管 active Turn，
然后才执行原 agent.stop/session.post_run 并释放绑定，unknown 保留同对象重试。无 hook
保持原 legacy 路径；该路径并未被本切片证明支持按凭据精确退出。

子 Agent 活动来源与宿主实际绑定分别需要后续验收；本切片的未知退出限制见 S_02。
