# F_07 OpenCode Provider foundation

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-24 |
| 范围 | harness_providers/opencode、公共构造/manifest 工厂 |
| 关联 spec | S_19 |
| Refs | R1-05 OC1，尚未提交 |

## 背景与决策

OC0 已验证固定版本 HTTP 代际、来源封存与 cgroup 清理；原生 bash 另建进程组，
只杀 Server PGID 不满足工具后代退出。OC1 将这些约束实现为 Provider 私有服务管理，
复用 SerializedTurnHarness，不改变公共协议、Swarm 状态机或历史投影。

使用类型化配置、私有来源快照、持久资源描述、宿主/原生双启动锁与带 generation 校验的
service wrapper。文本/工具/用量映射保留原生 ID；单一 SSE 消费者及有界缓冲；完成状态须与
当前提交 messageID、最终消息读取及 idle 关联。失败不重放输入，清理不确认则保留归属。

## 拒绝的方案

- 不复用用户 OpenCode Server、共享进程池或任意 RPC 代理。
- 不自动发现模型凭据/插件，不静默降级到没有 cgroup 的平台。
- 不为提前开放产品而宣称支持审批/提问/MCP/续接；这些在 OC2–OC5 独立验收。
- 不用 EOF/idle 推断成功，也不复制 Provider 公共队列、Turn 状态或工具运行时。

## 验证基线

Python 3.13.15，本地 editable core；Provider/协议/engine 回归 360 passed、1 skipped
（既有 DSH timing opt-in），其中新增 OC1 确定性用例 34 项。真实固定 1.18.18 CLI +
本机模型 fixture 8 项：文本/追问/工具/用量、审批未实现拒绝、双服务停止隔离、scope 独占与
重启、来源漂移拒绝、宿主 SIGKILL 孤儿回收、顽固工具在 stop/原生 Server SIGKILL 后退出。
使用公共 `_contract.py` 核验单一 STARTED/terminal；未调用远端模型或产品渠道。
新增实现和测试通过 Ruff；未提交/发布，不构成正式依赖配对。

## 已知遗留

OC2 交互/abort/checkpoint、OC3 产品接线、OC4 子 factory、OC5 Skills/MCP 与正式配对发布
仍独立处理。首批来源和资源控制不承诺恶意同 UID/多租户/文件或网络隔离。
