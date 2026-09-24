# F_09 OpenCode 受管产品 MCP

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-24 |
| 范围 | harness_providers/opencode、产品 ToolGateway 的协议 MCP 接缝 |
| 关联 spec | S_19 |
| Refs | R1-05 OC4 |

## 背景

OpenCode OC1 观察到原生工具，却把 card 标成宿主 `NATIVE_TOOLS`，同时明确拒绝
`HarnessContext.tools`。产品会话因此会选择无法工作的直连 ToolGateway 路径。OC0 已验证固定
1.18.18 能从配置加载带 Bearer 的远端 MCP；OC4 需要复用 Swarm 已有 Session-owned 产品 MCP，
不能复制六工具或建立 Provider 专用工具总线。

## 决策

OpenCode 对宿主声明 `MCP_TOOLS` / `MCP_SERVERS`。Provider 将协议 MCP 配置编译为原生
`type=remote` 配置，只接受显式端口的 `127.0.0.1` HTTP、唯一 Bearer Authorization 头并固定
`oauth=false`。完整配置仍进入 generation 私有启动材料和有效配置回读。

产品 MCP 的端口和 token 每次启动都会变化；稳定 scope 身份继续使用 OC1 的无临时 MCP 配置计算，
从而保留迁移前 Session 的 storage/checkpoint 冷恢复。宿主仍拥有工具定义、父主体/Session/workspace、
授权和 transport 生命周期；Provider 只负责原生装载、审批事件和调用。

## 拒绝的方案

- 不把 OpenCode 内置 bash/read 等工具当成宿主 `NATIVE_TOOL_GATEWAY` 能力。
- 不在 Provider 复制产品工具、六工具状态机或 Swarm 类型。
- 不接受任意远端/stdio/in-process MCP、匿名 header 或 OAuth 自动发现；更广的受控 MCP/Skills 属 OC5。
- 不把随机 URL/token 加进稳定 storage identity，否则既有完成态 Session 会在升级后无法恢复。

## 验证

确定性测试覆盖 capability、配置渲染、host capability、非 loopback/stdio/匿名/非法名称拒绝；真实
OpenCode 1.18.18 + loopback 模型由 Swarm 产品链完成 MCP list/call、普通审批、冷恢复和同引擎子
执行。未访问远端模型，未进行 Web/CLI 正式渠道或 full-python 验收。

## 已知遗留

Skills 与 profile 中受控 MCP 的发现/启停归 OC5；远端模型、正式 Web/CLI、锁升级和 CI 归 OC6。
