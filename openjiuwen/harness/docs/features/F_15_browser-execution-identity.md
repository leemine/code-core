# Browser Profile、Instance 与 Task 身份冻结

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-28 |
| 范围 | Browser 三层身份、文件根、Artifact 接缝、恢复代际、副作用前运行时绑定与 managed Profile 租约 |
| 测试基线 | Artifact/身份定向 59 项；Provider-neutral gateway 14 项；相关组合与 Browser 广覆盖见管理仓验证记录；生命周期新增 6 项及相邻 3 项；managed driver 8 项；Native 构造与实例隔离 70 项 |
| Refs | S_20；管理仓 R1-10A |

## 背景

既有 `BrowserInstanceConfig.key` 同时影响 MCP server ID、profile 名、端口和 user-data-dir，
能够实现同 key 共享、异 key 隔离，但没有表达授权主体、父 Session、子 Agent、workspace、
恢复代际和单次任务。产品层若只透传 `subagent_type="browser_agent"` 或复用全局 selected
profile，无法证明登录态、工具实例和恢复记录属于当前执行。

## 决策

- Profile、Instance、Task 使用三个不可变值对象；聚合对象在任何资源副作用前验证跨层引用。
- Profile 绑定主体、backend、policy revision 与 generation；实例绑定父 Session、子 Agent、
  workspace 与 generation；任务绑定 child Turn、request 和有效 capability fingerprint。
- 身份只保留非敏感稳定字段。驱动连接信息继续留在既有 Browser 配置/Profile 实现中，不能进入
  Binding、事件、历史或 checkpoint 的公共身份。
- JSON 解码严格拒绝未知字段、缺字段、非字符串 ID、布尔 generation 和非绝对 workspace，
  防止恢复时静默接受漂移配置。
- `browser_tool_allowlist_fingerprint()` 对规范化、排序后的有效工具 allowlist 生成
  `sha256:<64 lowercase hex>`；Task 身份拒绝其它格式。
- `BrowserService` / `BrowserAgentRuntime` 提供可选 `execution_identity` 接缝。新产品路径必须让
  backend、profile、instance key、MCP cwd 和工具 allowlist 指纹全部一致；任一漂移在 Profile
  store、Chrome、MCP 或 Provider 副作用前失败。未提供身份的既有 Native 路径保持兼容。
- 产品身份必须同时提供 `BrowserExecutionFileRoots`：uploads、outputs、audit 三个绝对根均位于
  同一规范 workspace 内且彼此不重叠。构造只校验、不创建目录；截图和用户 Artifact 被定向到
  `outputs/screenshots` 与 `outputs/artifacts`，不再落入进程 cwd 的全局默认目录。
- 既有 process registry 在资源取得时拒绝不同 managed identity 复用同一 user-data-dir；普通
  task release 且无保留 driver 后释放 owner，暖 Chrome 存活时 owner 保持到 reset。租约使用与
  Chrome 启动逻辑相同的有效目录解析，不用 profile 名占位符猜测。
- 前端取消在 worker 返回后再次检查 cancel marker，拒绝取消竞态中的迟到成功；task binding
  release 与显式 lifecycle reset 会取消 in-flight task，并分别返回
  `browser_task_binding_released` / `browser_lifecycle_reset`。内部 transport restart 保留发起该
  restart 的当前 task，避免把受控重试误判成外部 reset。
- reset 开始即设置 registry barrier；MCP binding 移除或 managed driver stop 任一失败时，不再吞掉
  异常或释放 owner，而是保留失败 driver/identity、抛出 `BrowserLifecycleCleanupError`，并阻止同
  identity 与同 user-data-dir 的 acquire/activate/register。只有显式重试完整成功才解除 barrier。
- 并发 reset 只有先到调用成为 cleanup owner；后到调用稳定抛
  `BrowserLifecycleResetInProgressError`，不参与 detach/stop，也不会提前解除 owner 的 barrier。
- `ManagedBrowserDriver.stop()` 只在 `poll()` 确认子进程退出后清除 handle/owner；terminate 超时或
  失败时执行 kill 并再次 wait。kill 后仍存活则保留 `Popen` 与 owns-process 状态并抛错，让上层
  cleanup barrier 保持，而不是把“已发送信号”误记为“已退出”。
- `project_browser_output_artifact()` 是 outputs 到既有 `AgentResult.Artifact/Part` 的无存储桥接。
  它只接受当前 execution workspace 的规范 outputs 根内普通文件，拒绝符号链接/越界/目录/缺失及
  读取中变化，计算 mime/size/SHA-256，并记录去凭据和查询串的来源 URL、生成工具、task/turn/request
  与 permission decision。返回值只含 workspace 相对引用，不含文件正文或绝对路径；audit/offload
  不能伪装为用户 Artifact。产品 file service 后续消费该接缝，core 不建立第二套持久存储。
- identity-bound `run_task()` 使用冻结 task request ID；调用者提供不同 request ID 时在启动
  Browser/MCP/Provider 前拒绝，避免运行结果和 Artifact 元数据错绑到另一请求。
- `BrowserExecutionToolGateway` 把同一个身份绑定 runtime 投影成公共 `ToolGateway`，供 External
  Provider 的原生工具调用使用。它只暴露冻结 allowlist 的真实 Playwright primitive 与 core
  Probe/Batch helper，调用前做 schema、admission、Task ID 和 ref 校验，调用中串行化，调用后复用
  runtime 的 PageState/ref/result 语义；不会创建嵌套 Native worker，也不会向 Provider 交出私有
  MCP client。关闭网关等待在途调用后只释放 Task 资源，Profile 生命周期仍归既有 owner。

## 拒绝的方案

不把 `browser_key` 扩成一个承载所有字段的复合字符串；不把 user-data-dir、CDP URL、cookie
或 token 当作身份；不让模型选择 owner、workspace、backend 或 generation；不因 Profile ID
相同就跳过 subject/tenant 校验；不在 `harness_protocol` 增加 Playwright 私有字段。

## 验证

确定性测试覆盖三层构造和 JSON round-trip、空值/控制字符/路径式 profile ID、未知 backend、
非法 generation、相对 workspace、未知或缺失字段、profile/instance/task 引用漂移，以及
subject、tenant、Session、subagent、workspace 跨 scope 复用。测试不得启动 Chrome、MCP 或
Provider，失败必须发生在副作用之前。
Registry 测试另覆盖同 identity 共享、异 identity 冲突、无 driver 后释放、保留 driver 后继续
占有、remote identity 不声明本地 user-data-dir owner、三个创建入口不可绕过，以及实际派生目录
冲突。运行时绑定测试覆盖 backend/profile/instance/workspace/allowlist 漂移、文件根越界/重叠和
构造零写入；生命周期测试覆盖取消后迟到成功、release/reset 显式终止原因、内部 restart 保留，
以及 driver/MCP cleanup 失败后 owner 保留与成功重试。
Artifact 测试覆盖受控绝对/相对路径、稳定 ID/hash/mime/size、URL 凭据与查询清理、无正文/绝对路径、
越界/目录/缺失/符号链接、非法 kind/来源/权限关联、跨 workspace 及 service 绑定 request 漂移。
Gateway fake-MCP 测试覆盖 catalog 裁剪/缺项失败、未知与 admission 拒绝、协议冻结 JSON 解码、schema、
过期 ref、raw target、Task ID 注入/漂移、调用串行化、结果/PageState 绑定、异常脱敏及幂等 close。

## 已知遗留

本切片只把身份、文件根、用户 Artifact 映射、终止原因及 Provider-neutral ToolGateway 接入 Browser
service/runtime 的可选构造门禁与 process registry 租约；尚未由 Native preset 或 External
composition root 生成产品身份并装配 gateway，也未接
Profile 授权存储、swarm file service/Artifact 历史、audit writer 或 UI；这些分别归 R1-10B 至
R1-10E。旧 `jiuwenclaw`
Profile 的可信兼容身份仍待 composition root 生成，不能据本契约文件宣称登录态迁移已经实现。
