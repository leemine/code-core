# S_20 Browser 执行身份

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness/tools/browser_move/playwright_runtime/identity.py`、`browser_capabilities.py`、`artifact_projection.py`、`browser_gateway.py`、`service.py`、`service_registry.py` |
| 最近一次修订日期 | 2026-09-29 |
| 关联 feature | `F_15_browser-execution-identity.md`、`F_16_browser-task-exit.md` |

## 范围 / 边界

本规约定义 Browser Profile、运行实例和任务的三层不可变身份。它只表达 Browser
资源授权与恢复必须核对的稳定事实，不拥有 Provider Turn、产品 Session、子 Agent 状态机、
Profile 存储、浏览器进程、MCP transport、权限交互或 Artifact 历史。

`BrowserProfile` 是既有驱动连接元数据，可能包含 CDP、端口和 user-data-dir；它不是本规约的
授权身份。三层身份不得包含 cookie、token、Authorization header、CDP URL、user-data-dir、
浏览器二进制路径或其它凭据/宿主路径配置。

## 不变量

1. **三层身份不可混同**：
   - `BrowserProfileIdentity` 绑定 profile ID、owner subject/tenant、backend、policy revision 与
     profile generation；
   - `BrowserInstanceIdentity` 绑定 profile ID/generation、instance ID、父 Session、子 Agent、
     workspace 与 instance generation；
   - `BrowserTaskIdentity` 绑定 instance ID/generation、task、child Turn、request 与 capability
     fingerprint。
2. **值对象不可变且可 JSON 序列化**：三类均为 frozen/slots dataclass，只接受 JSON 标量；
   `to_dict()` 产出新字典，`from_dict()` 拒绝缺失、未知、类型错误或非法值。
3. **标识符不是路径或凭据**：ID 去除首尾空白后必须非空、长度有界且不含控制字符；profile ID
   不接受路径分隔符。workspace 必须是规范绝对路径，但不得作为 profile ID 或 owner ID。
4. **generation 单调且为正整数**：Profile reset 后必须使用更高 profile generation；实例重建、
   target/MCP 代际变化使用更高 instance generation。布尔值不得作为整数 generation 接受。
5. **跨层一致性先于副作用**：`BrowserExecutionIdentity` 构造时核对 profile ID/generation 与
   instance 引用、instance ID/generation 与 task 引用；不一致时抛 `ValueError`，不得启动
   Chrome、连接 MCP、创建目录或恢复 Provider。
6. **作用域复核显式执行**：`validate_scope()` 精确核对 owner subject/tenant、父 Session、
   subagent 和规范 workspace；调用者不能只比较 display name、browser key 或全局 selected profile。
7. **capability fingerprint 是授权结果摘要**：`browser_tool_allowlist_fingerprint()` 对去空、
   去重并排序后的有效 Browser 工具名 JSON 计算 SHA-256，格式固定为 `sha256:<64 lowercase hex>`；
   它不携带原始权限、凭据或模型输入，其它格式非法。
8. **旧 Native 兼容单独映射**：现有 `BrowserInstanceConfig` / legacy `jiuwenclaw` profile
   在接入该契约时由可信宿主建立显式兼容身份；本值对象不读取环境变量或自行选择 Profile。
9. **managed user-data-dir 单一 owner**：不同 `BrowserServiceIdentity` 不能同时占有同一规范化
   managed user-data-dir。相同 identity 可共享；最后一个无进程 owner 释放后可重新领取；为有界
   idle 保留的 managed driver 继续持有目录，直到显式 reset/退出确认。remote/extension 不以本地
   user-data-dir 参与该冲突判定。
10. **运行时绑定不可漂移**：提供 `execution_identity` 时，`BrowserService` 在建立 Profile store
    前核对 backend/driver、profile ID/name、instance ID/key、规范 workspace/MCP cwd 及有效工具
    allowlist fingerprint；Profile/instance generation 纳入 process identity。旧调用者不提供该值
    时保留原行为，但不能因此宣称具备产品级授权身份。
11. **文件根按用途分离**：产品身份必须同时提供 `BrowserExecutionFileRoots`；workspace、uploads、
    outputs、audit 均为解析符号链接后的绝对路径，后三者必须位于 workspace 内且彼此不相等、不
    嵌套。构造不创建目录；截图与用户 Artifact 只写入 outputs 下的固定子目录。uploads 和 audit
    根只作为后续准入/证据接缝，本切片不将其自动暴露给模型。
12. **终止后的结果不可复活任务**：frontend cancel 在 worker 返回后复核 marker；task binding
    release 和显式 reset 取消全部 in-flight task，并返回不同稳定错误码。由当前 task 发起的内部
    transport restart 可保留该 task，其他共享 task 仍被取消；迟到成功不得更新进度或作为成功返回。
13. **清理失败保持封锁**：reset 从资源 detach 起设置 `reset_in_progress` barrier；MCP binding
    移除或 managed driver stop 失败时，registry 保留精确 identity、失败 driver 与非敏感错误类别，
    `BrowserLifecycleCleanupError` 向调用者报告失败。相同 identity 的 acquire/activate/register 及
    同 user-data-dir 的其它 identity 均不得继续，直到显式 reset 重试完整成功；不得在 `finally`
    中无条件 unregister 或遗忘失败进程。同一 identity 同时只允许一个 reset owner，后到调用稳定
    抛 `BrowserLifecycleResetInProgressError`；原 owner 完成后才解除 barrier。
14. **进程退出必须确认**：owned managed Chrome 的 stop 先 terminate + wait，未退出再 kill + wait；
    仅当 `poll()` 返回退出码后才能清除 Popen handle 和 owns-process。两阶段后仍存活必须保留 handle
    并抛异常，使 registry barrier 可以保留 owner；external/非 owned 进程仍不得被终止。
15. **用户 Artifact 只投影受控输出**：`project_browser_output_artifact()` 只接纳规范
    `outputs_root` 内的普通文件，拒绝目录、缺失文件、路径越界和任何符号链接；打开前后同时核对
    inode/device/size/mtime 并计算 SHA-256，变化时失败关闭。它映射到既有 `AgentResult.Artifact/Part`
    而不复制文件、不新建存储、不内嵌正文或绝对路径。公共元数据固定记录 kind、去凭据/查询/fragment
    的来源 URL、生成工具、task/child Turn/request、permission decision、workspace 相对路径、mime、size
    和 hash；audit/offload 不属于允许的用户 Artifact kind。identity-bound `run_task()` 省略 request ID
    时使用冻结值，提供不一致值必须在 Browser/MCP/Provider 启动前拒绝。
16. **Provider-neutral gateway 不能扩大身份能力**：`BrowserExecutionToolGateway` 只接受同时绑定
    `BrowserExecutionIdentity`、显式文件根与工具 allowlist 的 runtime；首次列举时确认 allowlist
    中每个 primitive 都真实存在于当前 MCP catalog，否则整体失败关闭。公开面是协议
    `ToolDefinition/ToolInvocation/ToolExecutionResult`，另加入同 runtime 的 Probe/Batch helper；调用
    递归解冻协议 JSON 后校验 schema，拒绝未知工具、过期 ref、raw primitive 的 PageState target、
    session/request 漂移及宿主 admission 拒绝。调用按 Task 串行，成功后复用 runtime 的结果归一、
    PageState/ref 记录；异常只返回类别，不泄露 transport、凭据或路径。`close()` 幂等并等待活动调用
    后释放 Task 资源，不直接销毁授权 Profile。宿主可在构造时指定 `stop_on_close=True`，
    此时先通过既有 runtime reset(graceful=True)/registry barrier 停止精确实例的 MCP 和受管 Chrome，确认
    成功后才释放 Task 并报告 closed；异常保持 gateway 未关闭以供重试。磁盘 Profile 不删除。
    调用者取消时保留内部清理任务，下次 close 加入同一任务；不能取消后遗失 reset owner。
    优雅关闭仅作用于自有 Chrome，CDP 失败后使用原进程退出确认；强制退出不承诺最新 Cookie 落盘。
    默认 False 保留既有 Native/共享实例的 release 行为，不把停止策略隐式应用于其它调用者。

## 接口契约

```python
class BrowserBackend(str, Enum):
    MANAGED = "managed"
    REMOTE = "remote"
    EXTENSION = "extension"
    ELECTRON = "electron"

@dataclass(frozen=True, slots=True)
class BrowserProfileIdentity:
    profile_id: str
    owner_subject_id: str
    backend: BrowserBackend
    policy_revision: str
    generation: int = 1
    owner_tenant_id: str = ""
    def to_dict(self) -> dict[str, JsonScalar]: ...
    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BrowserProfileIdentity: ...

@dataclass(frozen=True, slots=True)
class BrowserInstanceIdentity:
    instance_id: str
    profile_id: str
    profile_generation: int
    parent_session_id: str
    subagent_id: str
    workspace: str
    generation: int = 1

@dataclass(frozen=True, slots=True)
class BrowserTaskIdentity:
    task_id: str
    instance_id: str
    instance_generation: int
    child_turn_id: str
    request_id: str
    capability_fingerprint: str

@dataclass(frozen=True, slots=True)
class BrowserExecutionIdentity:
    profile: BrowserProfileIdentity
    instance: BrowserInstanceIdentity
    task: BrowserTaskIdentity
    def validate_scope(self, *, owner_subject_id: str,
                       parent_session_id: str, subagent_id: str,
                       workspace: str, owner_tenant_id: str = "") -> None: ...

@dataclass(frozen=True, slots=True)
class BrowserExecutionFileRoots:
    workspace: str
    uploads_root: str
    outputs_root: str
    audit_root: str

def browser_tool_allowlist_fingerprint(tool_names: Iterable[str]) -> str: ...

class BrowserService:
    def __init__(..., execution_identity: BrowserExecutionIdentity | None = None,
                 file_roots: BrowserExecutionFileRoots | None = None): ...

class BrowserLifecycleCleanupError(RuntimeError): ...
class BrowserLifecycleResetInProgressError(RuntimeError): ...

class BrowserArtifactKind(str, Enum):
    DOWNLOAD = "download"
    SCREENSHOT = "screenshot"
    PDF = "pdf"
    TRACE = "trace"
    VIDEO = "video"
    EXPORT = "export"

def project_browser_output_artifact(
    file_path: str | os.PathLike[str], *,
    execution_identity: BrowserExecutionIdentity,
    file_roots: BrowserExecutionFileRoots,
    kind: BrowserArtifactKind | str,
    source_url: str,
    tool_name: str,
    permission_decision_id: str,
) -> Artifact: ...

BrowserToolAdmission = Callable[
    [BrowserExecutionIdentity, ToolInvocation],
    bool | Awaitable[bool],
]

class BrowserExecutionToolGateway:
    def __init__(self, runtime: BrowserAgentRuntime, *,
                 admit: BrowserToolAdmission | None = None,
                 stop_on_close: bool = False): ...
    async def definitions(self) -> tuple[ToolDefinition, ...]: ...
    async def invoke(self, invocation: ToolInvocation) -> ToolExecutionResult: ...
    async def close(self) -> None: ...
```

## 与其它 spec 的关系

- `S_05`：Browser 工具 capability catalog 和工具执行契约；fingerprint 只引用其编译结果。
- `S_08`：动作审批与文件/网络 guard；身份校验不能代替权限判断。
- `S_09`：workspace 授权根；本规约只持有规范路径快照。
- `S_10`：产品子 Agent 的唯一状态机和父子执行端口；Browser 身份不复制该状态机。
- `S_18`：Native Browser preset 与旧实例配置的兼容接入。
- `S_19`：External Provider 消费宿主受管 MCP；Provider 配置不能扩大 Browser 身份范围。

### Native managed download ownership (R1-10F)

`build_browser_agent_config` / `create_browser_agent` accept optional
`downloads_root`, supplied by the host inside its authorized output workspace.
The default remains disabled for callers that do not supply this port. Native
rails and tool permissions still execute normally. The runtime uses a separate
CDP connection because MCP's `noDefaults` attachment does not enable download
events. Each invocation gets a private directory and an exclusive lease keyed
by Chrome profile path; a competing invocation fails instead of changing its
download destination. Temporary files are never completion evidence.

Completed tools expose `browser_downloads` receipts (name, absolute host path,
size, completed status) only after Chrome's terminal event and safe rename. The
host continues to authorize and publish these files through its original file
tool. On task release the runtime cancels pending GUIDs, confirms terminal events,
resets download behavior and closes its CDP connection. Unconfirmed reset falls
back to confirmed managed Chrome exit; failed exit retains ownership.

`shutdown_managed_browser_runtimes()` closes process-owned managed Chrome at
application shutdown, including idle drivers retained after their Agent was
collected. It does not reset remote/attached browser identities, erase profiles,
or silently swallow failed shutdown. Normal task release keeps the existing
warm Chrome behavior after download cleanup is confirmed.
