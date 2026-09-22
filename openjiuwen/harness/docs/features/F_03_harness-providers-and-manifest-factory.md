# Harness Providers、DeepAgent IO Adapter 与 Manifest 工厂

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-09 |
| 范围 | `openjiuwen/harness_providers/`（`base` / `stream` / `io_adapter` / `factory` / `native` / `claudecode` / `codex` / `dsh`）、`openjiuwen/harness/resources/extension_resolver.py`（`render_agent_template_system_prompt`） |
| 测试基线 | `tests/unit_tests/harness_providers`（46 通过）；`tests/system_tests/harness_providers`：Claude Code 8/8、Codex 7/7、DSH 5/5、native 7/7（`API_BASE/API_KEY/MODEL_NAME` 指向 DeepSeek `deepseek-v4-flash`）通过 |
| Refs | #751 |

## 背景

`openjiuwen.harness_protocol` 是 provider-neutral 的三方 Harness SPI，但 harness 侧没有：
（1）把协议输出翻译回 DeepAgent 输入输出契约的适配层；（2）以 DeepAgent 自身作为协议实现；
（3）从 harness 专家定义的 AgentTemplate manifest 创建协议 harness 的入口。manifest 面向
DeepAgent 设计，`tools` / `rails` / `subagents` / `skills` 依赖框架，三方 CLI 无法承载。

## 决策

1. **`SerializedTurnHarness` 骨架**（`base.py`）：所有内置 provider 共享一套串行 Turn 状态机、
   有界 BLOCK 事件流（`stream.py`）、interaction 记账与 checkpoint 发布；provider 只译 SDK。
2. **`DeepAgentHarness`**（`native/`）：一个外部 Turn = 一次 `attach_output` + `send_input` 到输出流
   结束；`_ObservationRail` 把工具生命周期写成 `tool_call` / `tool_result` chunk 以产出
   `ItemLifecycleEvent`；ask-user 中断保持 Turn 打开，经 `UserInputRequest` 取得宿主答案后用
   `InteractiveInput` 继续；无宿主 handler 时以 `stop_reason="interrupt"` 结束并在 `provider_data`
   带 `pending_interrupt_ids`，宿主可用 `metadata.kind=interactive_input` 的输入恢复。声明
   STEER（`send_input(mode=STEER)`）与 FORCE_ABORT（`cancel_round`）。`_open_session` 在 `agent.start`
   前显式 `ensure_initialized()`：交互循环不会自行初始化 agent，否则 pending rails 不注册、cwd
   ContextVar 不按 `cwd` 初始化（native e2e 发现）。文件/shell 工具由 manifest 的 `core.sys_operation`
   rail 提供，spec build 不默认挂载。
3. **`HarnessIOAdapter`**（`io_adapter.py`）：协议 ⇄ DeepAgent I/O；自身实现
   `HarnessInteractionHandler`，`UserInputRequest` → `__interaction__` chunk → `InteractiveInput`
   应答；工具审批默认自动放行。
4. **`create_harness` / `build_harness_context`**（`factory.py`）：`provider` 参数取
   `native | claudecode | codex | dsh`；`native` 热加载整份 `AgentTemplateSpec`（card / model
   缺省从 template 补），三方 provider 只取模型端点并拒绝 DeepAgent-only 段；context 构建把
   persona sections 按 priority 渲染为 `system_prompt`（新增
   `render_agent_template_system_prompt`），manifest MCP → `McpServerConfig`。
5. **Claude Code / Codex / DSH provider** 的映射与限制见 agent_teams `F_96`；DSH 配置字段更新为当前
   安装 SDK 的 `DeepSeekHarnessConfig`（`dsh_home` / `profile` / `dsh_bin` / `patches` 等）。

## 拒绝的方案

- **让 `NativeHarness`（agent_teams）充当 native provider**：它是 team 包内的 DeepAgent 子类，harness
  侧不能依赖 agent_teams；DeepAgent 自身的 session-scoped 交互循环已足以承载协议。
- **用轮询 `agent.phase` 判定 Turn 结束**：输出流结束（`_close_idle_output_if_finished`）是 DeepAgent
  的权威边界，且 DeepAgent 内部的任务循环续轮本就属于同一外部 Turn。
- **为三方 provider 静默裁剪 manifest**：见 `F_96`。

## 验证

`tests/unit_tests/harness_providers/`（假 SDK）与 `tests/system_tests/harness_providers/`（真实
CLI，共享 `_contract.py`）。native e2e 使用环境变量配置的真实模型。

## 已知遗留

- `DeepAgentHarness` 未声明 GRACEFUL_ABORT / PAUSE_RESUME（DeepAgent 交互 API 只有 `cancel_round`）。
- provider entry point discovery 未做；`create_harness` 只认四个内置名字。

## 2026-09-21 Codex 启动权限修正

范围：`codex/options.py`、`codex/harness.py`；对应 S_19 不变量 10。
模型配置曾隐式触发 full access；宿主钩子缺失曾仅警告并保留 SDK 默认接受。
本次移除模型触发的权限提升，保留显式 bypass 兼容入口；将钩子检查纳入 client 失败清理区间，
缺失或赋值不生效时在 thread start/resume 前拒绝。复用原启动回滚与 Turn 状态机，无新增协议或状态。
拒绝继续警告后执行、静默切换 SDK、删除 Team 的显式配置等方案。

验证：Codex 定向 36 passed，共享 base/factory/IO 与 Team 接缝 61 passed；增加 14 个权限/不兼容布局负例。
本地 Python 3.13.15；开发环境 editable core，非干净锁定依赖发布验证。
锁定 SDK/CLI 0.144.4 原生插件可装载；真实模型调用与审批拒绝效果仍待验证。
SDK 默认 auto_review、子进程环境合并及插件启停的配置来源仍需宿主接入阶段处理，
本修正不声明完整 External Single 安全门禁通过。


## 2026-09-21 Codex 宿主审批与进程隔离增量

`codex/sdk_compat.py` 集中锁定 SDK 0.144.4 的实例级环境启动和低层审批协商，不新增公共 API。
TOOL_APPROVAL 的 start/resume/fallback 强制 user reviewer 与受限 sandbox，并核验回显；
无 handler、显式 bypass、策略不匹配或不兼容 SDK 均失败关闭。未声明该能力的旧 Team 显式 bypass 入口保留。
原生 MCP 工具确认使用 `mcpServer/elicitation/request` 的 form + `codex_approval_kind=mcp_tool_call`；
映射现有 ToolApprovalRequest（wire 未提供结构化 tool 名，使用 server 名，message/参数保留供审批），
ALLOW_FOR_SESSION 暂保守映射为单次接受，不向 CLI 写永久授权；其他 elicitation 拒绝。
命令/补丁审批超时取消宿主等待；隔离启动保留 SDK 的解析器、reader/router/close，避免全局 monkeypatch。

127 项 Codex/共享接缝确定性测试通过。`test_codex_security_local.py` 使用真实锁定 SDK/CLI、
本地脚本化 Responses 与原生安装插件，验证环境、启动/恢复、读/写/命令/MCP 的实际允许/拒绝副作用。
这不是远端模型 E2E：Swarm 授权模型端点的 `/responses` 返回 404，A0 仍需远端验证；未运行正式配对 CI。


## 2026-09-21 A0 Linux 沙箱边界验证

新增 `test_codex_sandbox_local.py`：锁定 SDK/CLI 的实际 command/exec 边界矩阵，以及本地 Responses 驱动的真实 Turn。readOnly 禁写；workspaceWrite 只在明确配置的 cwd/可写范围写入，目录外及符号链接越界写入被拒绝；禁网时回环 HTTP 和 IPv4/IPv6 TCP/UDP socket 对照受限。超时检查必须识别宿主 PID，不能使用沙箱 namespace PID。

两种 legacy 模式均允许 cwd 外读取，真实受信任 cat 不触发 TOOL_APPROVAL。此前 /tmp CODEX_HOME 无法创建 sandbox helper 导致的读取拒绝仅证明重试审批；隔离测试改用 Git 忽略的 .pytest_cache 独立目录。部署时需要的读取隔离应由受限可读根/OS 授权边界保证，不能从 cwd 推导。该批不修改生产实现、不宣称远端模型或产品读隔离已验收。


## 2026-09-21 A0 命名可读根能力探针

新增 test_codex_read_roots_local.py，10 项实际锁版本探针通过：命名 profile 限定可读目录，覆盖符号链接、路径穿越、deny 子目录、未知配置与冷恢复。含 1 项当前 core 越界读取行为刻画，其通过表示缺口复现。CLI 原生能力已具备；现有 connect_with_host_approvals 发送 legacy sandbox 字段使 profile 失效，SDK 生成响应丢弃 activePermissionProfile，不能靠旧 readOnly 回显确认读取边界。

本批只验证能力，不改生产实现、不升级 SDK；拒绝将配置接受或 helper 启动失败当成隔离成功。后续最小接线需保留原始有效配置字段，覆盖 start/resume/fallback，按既有宿主空间授权绑定可读根及必要运行时目录。命令沙箱不代替配置加载/MCP/附件等其他通道的授权验证。


### 2026-09-22：Codex 命名权限最小接线

背景：0.144.4 能限制可读根，但 core 显式 legacy sandbox 覆盖命名选择；生成响应还丢弃有效 profile 标识。
本次在 Provider 私有 SDK 接缝用 config/read 与原始 thread 响应协商，按 profile/legacy 二选一发送字段，
冲突、缺失、不匹配失败关闭。命名定义留在服务端配置，线程配置仅可选择已有 profile。

状态仍由 SerializedTurnHarness 管理；仅在已有 checkpoint.data 增加 permission_fingerprint。
配置指纹绑定选中 profile 的继承链和 cwd，恢复/认证 fallback/拒绝后原端点重连一致；变化拒绝连接，
新 checkpoint 不允许通过去掉 TOOL_APPROVAL 绕过绑定。旧 checkpoint 无指纹按当前配置首次绑定，保留兼容。

拒绝继续发送 legacy sandbox 并仅检查 readOnly：该回显无法证明命名可读范围；拒绝增加第二套权限协议、
状态机或为此升级 SDK。非审批路径的显式 bypass 仍是宿主原有配置，不由模型选择触发。

验证：146 项定向单测/Team 接缝回归通过。真实 0.144.4 CLI 与本地 Responses 覆盖首次/冷恢复、
同名配置变化拒绝、HTTP 401 触发 fallback，以及宿主拒绝切换后恢复原端点的实际允许/拒绝读取。
本机 sandbox/审批/MCP 回归另见管理仓本批证据；两个既有 Pydantic deprecation 未扩大忽略范围。
遗留：这是 Linux 命令沙箱读取边界，非 Provider 进程 ACL；AGENTS/Skills/MCP/附件其他读取渠道、
跨平台及远端模型端点验收仍待完成。配置指纹不等价于运行中 OS 资源/挂载或符号路径的身份冻结。


### 2026-09-22：Codex 非命令读取通道实测边界

范围：只新增真实 CLI + 回环 Responses 测试与规约事实，生产源码未改。复用命名可读根 fixture，
用固定哨兵区分自动加载、宿主显式输入、模型工具和 stdio MCP 进程读取。

实测 AGENTS 祖先/符号链接注入、Skill 元数据/显式正文、原生 LocalImageInput、portable skill 复制
均不能继承命令沙箱结论。view_image 工作目录成功，目录外/链接失败；当前 HarnessInput 仍为文本。
MCP 启动代码在审批前可读取命令不可读哨兵；prompt 工具拒绝有效，approve 显式放行无宿主回调。

决策：把源码/原生能力/产品接线分开记证，保留失败试跑归因；AGENTS 关闭配置只证实正文未注入，
不证明进程无文件访问。拒绝通过禁用整个既有 Team 能力、全局改写 approve 或靠提示词充当来源 ACL。
初次 MCP 两个断言因混淆 approve/prompt 失败，修正测试配置并增加显式 approve 对照，无生产规避。

验证基线与命令：tests/system_tests/harness_providers/test_codex_read_channels_local.py，锁定 0.144.4；
本地生成内容、隔离 HOME/CODEX_HOME、命名 profile 与进程清理；全组结果随管理仓证据归档。
遗留：宿主上下文/附件来源授权、插件/MCP 启动隔离/可信准入和远端模型仍需验收；A0 不能据本批解锁。


### 2026-09-22：显式受限 Codex 启动与来源准入

范围：新增可选 startup_source_roots 与 Provider 私有 source_policy；协议、状态机、Team 调度不变。
背景：原生 AGENTS/Skill/附件/MCP 启动不继承命令沙箱。选择由可信宿主登记来源根的 opt-in 模式，
先检查已知配置来源、Skill 显式来源与发现目录，后安装 Skill、加载 SDK，再核对有效配置并创建线程。

受限模式拒绝未受管 MCP/ambient 插件/hooks 和未知配置，强制关闭 AGENTS 自动注入、login shell 与已知自动来源特性；
分离 HOME/CODEX_HOME，保留命名 profile 和宿主审批。内置 Skill 缓存只能显式登记授权，不能扩大到整个 HOME。
已有 checkpoint 增加 startup_source_fingerprint；恢复在复制前核验，fallback 与下一 Turn 复用准入检查。
改变来源范围或关闭受限模式均不可恢复旧受限会话；旧非受限调用、显式 Team approve/bypass 保持。

拒绝将命令 sandbox 当进程 ACL、拒绝自动批准未知默认配置、拒绝全局禁用旧 Team 能力。
首轮真实配置回读发现 0.144.4 隐式空扩展映射/特性默认值和 network_proxy=null，逐项精确处理，未白名单整个未知配置。
内置 Skill 缓存先被拒绝，再由测试宿主显式登记；回归发现 SDK 加载顺序改变，已恢复既有安装先于 SDK 的契约。

验证：203 项来源/Codex/Skill/共享/Team 定向回归通过；真实本地 CLI 覆盖首次/恢复、命令读取、授权 Skill、
启动前拒绝、动态新增来源拒绝与 fallback。试跑失败归因和最终 CLI 套件见管理仓证据。
遗留：此模式不提供 OS 隔离或附件 ACL；B1 后续只为宿主按 Session 管理、带认证的 loopback HTTP 产品 MCP
增加严格例外，原生插件由 C1 的固定快照例外接入。
锁 0.144.4 以外版本、跨平台和远端模型未验证；来源指纹不是授权令牌，也不是文件内容快照。


### 2026-09-22：受限可写启动与后台插件同步修复

范围：Codex source_policy、启动 options 和有效配置回读；没有新公共字段或协议，也不改变非受限 Team 配置。

背景：真实可写 profile 验证发现锁定 CLI 在项目无信任记录时自动持久化祖先仓库 trusted，随后被来源检查拒绝；另一次失败探针留有插件克隆缓存。核对 0.144.4 上游源码并用固定 git 替身对照，确认 remote_plugin=false 仍会启动官方插件市场同步，git 子进程可存活到 app-server 退出之后。

决策：仅在 startup_source_roots 显式启用时，追加当前规范化 cwd 的临时 untrusted projects 覆盖，config/read 精确核对该唯一条目；默认强制 features.plugins=false 并核对有效值。持久文件、调用者覆盖和 thread_config 的 projects 仍不准入。现有 checkpoint/权限指纹与状态机保持；C1 后续只对显式受管插件快照开放固定插件树。

### 2026-09-22：Codex 受管原生插件 C1

范围：在 Provider config 增加 `native_plugins` 固定快照，不增加插件市场、安装器或公共插件运行时。宿主继续通过
Execution Profile/Binding 保存授权选择；Codex CLI 仍是 Skills/MCP 的唯一原生装载器。

实现：C1 首期接受部署预置的本地 marketplace；快照固定插件 ID、原生来源、版本、安装包 SHA-256、启用状态、Skills/MCP 必需性及 MCP 名称。启动前核验
隔离 CODEX_HOME、包树、manifest、C2 组件和名称冲突；启动时回读 plugin/list、plugin/read、
mcpServerStatus/list。任一显式必需项缺失即失败；hooks/commands/agents/apps、插件控制 config_overrides、未授权已启用
插件均拒绝。插件包指纹写入 checkpoint 并在每 Turn 前复核；变更关闭当前 Provider。启停只随新 Session 快照生效。

验证：新增 8 项确定性测试覆盖配置解析、来源树/逃逸、摘要篡改、C2/冲突/ambient 拒绝、原生清单与禁用省略；锁定
SDK/CLI 0.144.4 的本地脚本化 Responses 覆盖 Skills 读取、插件 MCP 实际调用、宿主允许/拒绝、自然回收以及新
Session 禁用。该闭环不代表 B1 产品 ToolGateway，也不开放 C2。

### 2026-09-22：受管产品 ToolGateway/MCP B1

范围：Codex 受限启动仅增加产品宿主受管 MCP 的窄准入，不把产品工具或权限模型搬入 Provider。
宿主为一个父 Session 生成临时 loopback HTTP endpoint 与高熵 Bearer token；Provider 配置必须 required=true、
default_tools_approval_mode=prompt。stdio/in-process、远端地址、弱/额外 header、额外 server 及有效配置回读漂移均失败关闭。

来源指纹只固定 server 名称，避免把短生命周期端口/token 当稳定来源身份；CLI 有效配置仍逐项核对 URL、认证头、
required、prompt 及锁定版本的 enabled/local environment/null tool-timeout 默认。工具执行继续使用既有 MCP 审批交互，Provider 不拥有产品目录，不复制六类工具，也不复用 Team operator。
Swarm 负责固定主体、父 Session、workspace、启动就绪、失败补偿及 Session 资源回收；B2 才负责子运行时构造和六工具组装。

验证：锁定 SDK/CLI 0.144.4 + 本地 Responses 的真实 External 产品链路完成 MCP tools/list、原生 prompt 审批、
call_tool、结果回送和 transport 关闭。初轮因 CLI 回读新增 enabled/environment_id/tool_timeout_sec 而失败关闭，
实现只接受实测的 true/local/null 精确默认后通过；没有扩大为未知字段白名单。

拒绝的方案：不将任意 projects 加入白名单，不持久化祖先仓库 trusted，不用超时重删目录掩盖后台写入，不使用只在 debug 编译可用的禁用启动任务参数，不修改全局 SDK/CLI 或旧模式进程管理。

验证：214 项定向单测/接缝回归，29 项真实 CLI + 本地 Responses 回归通过。新增可写启动/冷恢复写入、越界读取、配置不变；后台 git 的 legacy 正对照、受限正常/失败关闭负对照。已授权火山 glm-5.2 的 3 项真实可写允许/拒绝与越界读验证通过，所有配置保持不变且无插件缓存残留。Ruff、文档和 diff 检查通过；本地 dirty 开发来源，不是正式配对 CI。

已知遗留：本修复阻止受限模式的插件启动后台任务，不承诺旧模式所有后代进程均被 SDK 回收；历史探针退出 2 的精确异常仍未还原。部署 UID/挂载隔离、远端恢复/活动停止、原生插件隔离验收另续。
