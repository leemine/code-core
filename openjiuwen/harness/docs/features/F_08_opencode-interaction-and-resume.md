# F_08 OpenCode interaction and completed-session resume

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-24 |
| 范围 | harness_providers/opencode、真实 OpenCode 回环契约 |
| 关联 spec | S_19 |
| Refs | R1-05 OC2，尚未提交 |

## 背景

OC1 已固定 OpenCode 1.18.18 的旧代 `/session` + `/event` HTTP/SSE、受管 systemd cgroup、
来源封存和基础消息映射，但遇到审批/提问会关闭 Provider，原生数据也位于随机 generation 目录，
无法兑现已实证的完成态会话续接。原生旧审批端点还会接受错误 session URL，因此不能依赖 Server
替宿主验证请求归属。

## 数据与状态

Provider 继续复用 `SerializedTurnHarness` 的 pending interaction、取消和 checkpoint 权威。
OpenCode 私有 checkpoint 为：

```text
{session_id, resumable, state, resumed, turn_id}
```

新 session 或已验证恢复后发布 `idle/resumable=true`；每轮向原生提交前发布
`turn_active/resumable=false`；只有 stop、已确认拒绝或 abort 的原生终态与 idle 关联后重新发布可续接
checkpoint。活动、待答和结果未知状态不能冷恢复。

## 决策

- `permission.asked` 映射为 `ToolApprovalRequest`，`question.asked` 映射为 `UserInputRequest`。
  Provider 在任何原生回复前核对 session、当前 user 根消息、assistant message、call 和 request ID，
  并用本地簿记拒绝冲突重复；不开放任意原生 RPC。
- 宿主未提供对应能力、拒绝或取消时调用原生 reject。审批拒绝只在关联 tool error、
  completed/tool-calls 和 idle 齐备后以 FAILED 收口，不伪造正常 stop。
- graceful abort 调原生 session abort；待答 interaction 先由基类取消。只有关联
  `MessageAbortedError` + idle 判为 ABORTED。回答、取消和原生撤回竞态中的 404 只在已标记 abort
  的同一请求上容忍，不能重试到别的 session。
- SSE 断开会取消宿主 pending interaction，并将 Turn 判为结果未知的失败；不重连后重放输入。
- scope 下 `persistent/data` 与 `persistent/state` 跨随机 service generation 保留；HOME/config/cache/tmp、
  来源快照、launch 和日志仍逐 generation 隔离。恢复核对 session/status、pending interaction 和
  最后完成消息，失败关闭并交由宿主保持只读历史。

## 拒绝的方案

- 不用 observation event 代替 awaited interaction，也不在 mapping 层直接回答审批。
- 不相信旧原生 permission URL 的 session 参数，不允许重复/迟到答复再次执行工具。
- 不把活动 Turn 的上一个安全 checkpoint 当作当前状态继续，也不自动新建 session 或重发输入。
- 不把整个 generation 目录改成持久配置源；只持久原生会话所需 data/state。
- 不在 OC2 接产品 MCP、子 Agent、历史/UI 或完整活动交互冷恢复。

## 验证

Python 3.13.15，本地 editable core。Provider/协议/engine 定向回归 366 passed、1 个既有 DSH timing
opt-in skipped。OpenCode 确定性用例 40 passed，覆盖审批允许/拒绝、问题答复、跨 session/冲突重复、
abort/回答竞态、断流未知结果、checkpoint 状态及完成态续接。

真实固定 OpenCode 1.18.18 + loopback 模型 11 passed：保留 OC1 的文本/工具/资源清理与崩溃场景，
新增真实审批、真实提问、graceful abort 和停止受管服务后同 session 续接。首轮在受限 sandbox 因
loopback socket `PermissionError` 产生 11 个环境错误，不计通过；在获准的本机端口/systemd 环境完整
复跑通过。未调用远端模型、产品 Web/CLI 或正式配对 CI。

## 已知遗留

产品 Single/profile/历史/UI 归 OC3，子 factory 与六工具归 OC4，Skills/MCP 归 OC5，远端模型、渠道、
正式 core→swarm 锁升级与 CI 归 OC6。活动 Turn/待答对象的冷重建仍归后续恢复契约；当前明确只读降级。
