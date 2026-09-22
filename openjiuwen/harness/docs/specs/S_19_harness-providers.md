# S_19 Harness Providers（协议实现、IO Adapter 与 Manifest 工厂）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness_providers/`（`base.py` / `stream.py` / `io_adapter.py` / `factory.py` / `inputs.py` / `jsonsafe.py` / `native/` / `claudecode/` / `codex/` / `dsh/`） |
| 最近一次修订日期 | 2026-09-21 |
| 关联 feature | F_03_harness-providers-and-manifest-factory.md |

## 范围 / 边界

本规约定义 `openjiuwen.harness_protocol` 的内置实现包：共享的串行 Turn 骨架、五个 provider 的能力
声明、DeepAgent 风格 IO adapter，以及从 AgentTemplate manifest 创建 harness 的工厂。协议契约本身
以 `openjiuwen/harness_protocol/SPEC.md` 为准；团队成员接线见 agent_teams `S_27`。

## 不变量

1. **骨架唯一**：provider 继承 `SerializedTurnHarness`，只实现 `_open_session` / `_close_session` /
   `_execute_turn`（+ `_steer` / `_interrupt_turn`）。每个已接受输入恰好一个 STARTED 与一个 terminal
   `TurnLifecycleEvent`；stop 时排队中的 Turn 以 `HARNESS_STOP` ABORTED 收口；terminal 后队列为空才
   进入 IDLE。
2. **能力声明真实**：

   | provider | card | capabilities | optional host capabilities |
   |---|---|---|---|
   | `native` | `deepagent` | STEER, FORCE_ABORT | USER_INPUT |
   | `native_v2` | `native_v2` | STEER, GRACEFUL_ABORT, FORCE_ABORT, PAUSE_RESUME, CHECKPOINT, PERSISTENT_SESSION | USER_INPUT, CHECKPOINT_SINK |
   | `claudecode` | `claude-code` | STEER, GRACEFUL_ABORT, PERSISTENT_SESSION, CHECKPOINT, MCP_TOOLS | TOOL_APPROVAL, USER_INPUT, CHECKPOINT_SINK, MCP_SERVERS, PROVIDER_INTERACTION |
   | `codex` | `codex` | 同 claudecode | TOOL_APPROVAL, USER_INPUT, CHECKPOINT_SINK, MCP_SERVERS, PROVIDER_INTERACTION |
   | `dsh` | `deepseek-harness` | MCP_TOOLS | MCP_SERVERS |

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
   （App Server 请求 `item/tool/requestUserInput`）与 DeepAgent `ask_user` 中断映射为
   `UserInputRequest`，Turn 在应答前保持 RUNNING；宿主未提供 handler 时 Claude 拒绝该工具、Codex 回
   空 `answers`、DeepAgent 以 `stop_reason="interrupt"` 结束 Turn 并保留 `pending_interrupt_ids`。
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
   "event_buffer_capacity"})`）；对 `claudecode` / `codex` / `dsh` 只把 `model` 端点映射进 provider
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
    Codex Provider 只消费 HarnessContext 的 MCP 配置并走原生工具审批。临时 URL/token 不进入稳定来源范围身份，
    server 名称进入来源指纹；CLI 有效配置丢失认证、出现额外 server、改变 required/prompt 或改成非 loopback 时启动失败。
    Provider 不复制产品工具、不启动通用 MCP 注册中心，也不把 Team operator 权限用于 Single。

## 接口契约

```python
def create_harness(manifest: AgentTemplateSpec | str | Path, *, provider: HarnessProviderName,
                   config: Mapping[str, Any] | None = None, language: str | None = None) -> HarnessProtocol
def build_harness_context(manifest, *, provider, host_session_id, agent_id=None, agent_name=None,
                          language="cn", cwd=None, env=None, extra_system_prompt=None,
                          host_capabilities=frozenset(), resume_policy=ResumePolicy.NEW,
                          checkpoint=None, checkpoint_sink=None, interactions=None, metadata=None) -> HarnessContext
def resolve_provider(provider: str) -> HarnessProvider
PROVIDER_NAMES == ("native", "native_v2", "claudecode", "codex", "dsh")

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
| checkpoint data | claudecode `{session_id, resumed}`；codex `{thread_id, resumed[, fallback]}`；dsh / native 不发布 |
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

start 在 SDK 启动前复制完整目录到 cwd/.claude/skills（claudecode）、cwd/.agents/skills（codex）、
cwd/.dsh/skills（dsh）。cwd 优先取 HarnessContext，再取 provider config，再取当前进程目录。
名称取 SKILL.md front matter.name，缺省取目录名；同名按不区分大小写比较，同时识别已有目录里的
声明名。enabled_skills 非空时筛选声明名；mode 仍被解析校验，但原生 CLI 决定加载/调用方式，
不仿造 DeepAgent 的 auto_list 工具。skip 保留已有目录全部内容，replace 完整替换（清除旧文件），
多源重名按声明顺序处理。临时完整副本切换失败会恢复原目录。复制结果跨 stop 保留。

复制保留普通文件、子目录、隐藏资源和可执行位；内部链接物化成文件，越界/循环链接拒绝。
Claude 指定 portable skills 时显式启用 SDK skills=all，并保留 user/project/local settings 来源；
DSH sdk-minimal 自动挂载原生 skill/skill-filesystem/tool-skill 插件。SSH/custom Claude transport
不自动上传本地技能，显式报错而非复制到错误主机。见 team F_101。
