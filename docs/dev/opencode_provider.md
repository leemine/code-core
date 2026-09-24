# OpenCode Provider（OC1/OC2）

OC1/OC2 提供公共 `opencode` 注册、文本/原生工具/用量映射、审批/提问、graceful abort、
完成态会话续接和受管服务生命周期。
它复用 `SerializedTurnHarness`，可通过 `resolve_provider`、`create_harness` 或
`create_harness_engine` 构造；构造不发现 CLI、不启动服务、不安装依赖。

## 环境与配置

首批只接纳受信非 root Linux、cgroup v2 和已运行的 systemd 用户管理器。
CLI 固定为 OpenCode **1.18.18**，二进制 SHA-256：
`bb71f45b564f9234a97f54d6252a4a41d2f4388ae4b078918f691824cc3b3e54`。
其他构建/平台须重新验证后显式纳入，不能仅凭版本字符串通过。

宿主预先创建绝对路径 `runtime_root`（当前 UID 所有，权限 `0700`，无链接），并给出明确 cwd。
模型使用独立的 OpenAI-compatible endpoint 配置，不从用户 HOME、环境或模型名称推导授权。
`provider` 是模型配置中的私有别名，默认 `openjiuwen`；执行 Provider 始终是 `opencode`。

```python
import os
from openjiuwen.harness_protocol import HarnessContext, HarnessInput
from openjiuwen.harness_providers import resolve_provider

harness = resolve_provider("opencode").create({
    "cli_path": "/absolute/path/to/opencode",
    "runtime_root": "/absolute/private/opencode-runtime",
    "model": {
        "model": "your-model",
        "api_base": "https://your-compatible-endpoint/v1",
        "api_key": os.environ["MODEL_API_KEY"],
    },
})
await harness.start(HarnessContext(
    agent_name="assistant", agent_id="agent-1", host_session_id="session-1",
    system_prompt="Follow the host instructions.", cwd="/absolute/workspace",
))
try:
    receipt = await harness.send(HarnessInput("Explain the requested task."))
    async for event in harness.turn_events(receipt.turn_id):
        consume(event)  # the host's single observation consumer
finally:
    await harness.stop()
```

`AgentExecutionSpec.authorization=ExecutionAuthorization(full_access=True/False)` 由 Provider
编译为私有 `full_access`。旧工厂直接配置默认 False；同一 model 不决定权限。
默认原生权限为 ask；宿主声明 `TOOL_APPROVAL` 并提供 interaction handler 时，原生
`permission.asked` 映射为 `ToolApprovalRequest`，回答只在本地核对 session、当前根消息、call 和
request ID 后发送。未提供审批宿主时原生请求被拒绝。只有宿主明确授权 full-access 才关闭普通工具
审批。原生 `task` 保持 deny，不能代替产品子 Agent；宿主声明 `USER_INPUT` 时只将原生 `question`
工具开放为 awaited `UserInputRequest`，宿主未回答则调用原生 reject。

## 能力与失败边界

Card 声明 `GRACEFUL_ABORT`、`PERSISTENT_SESSION`、`CHECKPOINT`、`NATIVE_TOOLS`。文本、reasoning、
工具开始/更新/结果、累计 tokens 和 cost
保留原生 message/part/call ID。非文本 JSON 输入按公共 `harness_input_text` 渲染为文本；
这不表示图片/附件或结构化输出能力已实现。一个 Turn 必须有匹配当前 user messageID 的
最后 assistant completed/stop、后续 idle 及消息回读；204、step-finish、裸 idle、EOF 均不能判成功。

不支持的环境覆盖、宿主工具、MCP、Hooks、Skills、steer/pause 明确拒绝。`abort(GRACEFUL)` 调原生
session abort，并以关联 `MessageAbortedError` + idle 收口 ABORTED；回答与 abort 竞态始终优先取消
宿主 pending interaction，迟到回答不再执行工具。异常、超时、断流后服务停止并返回未知失败，
当前生命周期不重新发送输入；这并不保证已开始的原生工具没有产生副作用。

启动和每个已确认终态发布 opaque checkpoint。提交输入前先发布
`resumable=false/state=turn_active`；只有原生 stop、已确认拒绝或 abort 与 idle 关联后才发布
`resumable=true/state=idle`。`REQUIRE_RESUME` 只恢复同 scope 的原生 session ID，并核对 session、
status、pending permission/question 与最后完成消息。活动/待答/结果未知 checkpoint 明确失败，
供宿主降级为只读历史；不会用新 session 伪装恢复。

每个 agent/host-session/cwd scope 有私有目录、配置指纹、宿主锁、持久 owner 描述和随机
systemd service。服务 wrapper 持有 native 锁并检查 generation，避免宿主崩溃后的迟到启动。
恢复只回收经 unit 描述核验的自有孤儿服务；原生 data/state 在 scope 私有持久目录跨 service generation
保留，checkpoint 验证后继续原 session。HOME/config/cache/tmp 与日志仍逐 generation 隔离。
无法确认退出时保留句柄和归属，后续 stop 可重试。

HOME/config 封存；拒绝 `/etc/opencode`、native auth、链接/漂移和未接纳的有效配置。
每轮前复检。cgroup 回收覆盖脱离 Server PGID 的工具；它不提供恶意同 UID、多租户、文件或
网络隔离。服务停止保留私有数据、launch 配置和日志，由宿主按自己的保留策略清理；其中可能含敏感数据。

## 验证

确定性契约：`tests/unit_tests/harness_providers/test_opencode.py` 及原工厂/协议/engine 回归。
真实 CLI + 本机模型 fixture：

```bash
RUN_OPENCODE_OC1=1 timeout 240 python -m pytest \
  tests/system_tests/harness_providers/test_opencode_e2e.py --timeout=45 -q
```

`RUN_OPENCODE_OC1` / `OPENCODE_OC1_CLI` 名称为 OC1 建立时的兼容入口，OC2 继续复用。测试创建并回收
自己的 loopback listener、私有运行根、
服务和工具进程；不调用远端模型，不安装 CLI，不构成 Web/CLI 产品渠道或正式锁定发布验收。
