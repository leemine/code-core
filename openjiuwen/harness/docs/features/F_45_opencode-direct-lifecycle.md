# F_45 OpenCode 显式直接启动与原生关闭

## 元信息

| 项 | 值 |
| --- | --- |
| 日期 | 2026-10-10 |
| 范围 | OpenCode 配置、服务所有权、正常关闭；不改 Codex 实现 |
| 测试基线 | 309 项定向单测；真实 CLI direct 宿主/沙箱各 4 项；默认 systemd 6 项 |
| Refs | #62 |

## 背景

外部 Linux 沙箱已提供用户、文件和资源隔离，却没有用户 systemd/cgroup 控制面。
原先受管服务实现将此控制面作为启动前置条件，阻止原生 OpenCode 在该环境运行。
此外，直接结束服务与请求原生取消并不等价；正常关闭应给原生执行器完成取消的机会。

## 数据与生命周期

`OpenCodeHarnessConfig.server_mode` 仅接受 `systemd` 和 `direct`，默认前者。
默认配置身份去掉这个新增默认字段，以保留旧会话的持久化身份；direct scope 单独分区。
共享 SerializedTurnHarness 仍是唯一 Turn 队列/状态机，HTTP/SSE 仍只有原事件消费者。

direct 直接启动固定版本 CLI 的 `serve`，保存确切子进程句柄。启动被取消时仍取得句柄后
进入既有回滚，避免丢失新进程。关闭先请求原生 session abort，以状态查询等待该 session
idle；随后关闭传输和自有服务。原生失败或超时仍执行服务回收，传输关闭失败也不能跳过。
关闭入口串行化，退出未确认则保留句柄供重试。

## 决策

- 默认 systemd 路径继续核验用户服务与 cgroup 退出，保留孤儿受管服务回收能力。
- direct 仅供外部已隔离的非 root Linux 环境显式选择。它确认自有服务子进程退出，
  不保证宿主或原生服务崩溃后的全部工具后代回收。
- direct 遗留 owner 返回 `direct_owner_recovery_required`，不凭旧 PID 接管或回收。
- 两种模式均保留固定二进制摘要、私有 HOME、来源、插件、普通权限和每轮准入检查。
- 原生 idle 只是正常关闭屏障，不是所有工具进程已退出的证明。

## 拒绝的方案

不隐式从 systemd 降级；不新增 subreaper、全局进程扫描或第二套 Provider 状态机；
不通过放宽运行时权限解决部署问题。单 Provider 异常后代回收按本轮范围暂缓。

## 验证

固定 OpenCode 1.18.18，真实 CLI 配合本地模型夹具：宿主 direct 4 项、一次性 2GiB
jiuwenbox direct 4 项、默认 systemd 6 项均通过。覆盖文本、真实工具、普通运行中止、
正常关闭、恢复、遗留 owner 拒绝和默认模式崩溃回收；沙箱删除返回 204，复查 404。
定向单测 309 项通过，包括关闭失败重试、启动取消竞争、默认身份兼容和 Codex
stop-confirmation 回归。新增测试加入现有 pr-stable，未增加逐提交 full-python 门禁。

测试工具 10 项通过。首次受限网络运行的服务夹具失败与一次错误测试路径的收集错误
分别留档，不计入成功。make check 的格式、拼写、Ruff 通过；Pylint 为现有模块风格与
复杂度建议（9.64/10）。实际命令、版本与日志在工作区 release-validation 交付证据中归档。

## 已知遗留

Windows/macOS 不属于本次支持范围。正常 idle 时抗 TERM 工具仍可能存活；默认模式
依靠既有 cgroup 收口，direct 不作同等承诺。新 Core/Swarm 锁定配对的 Web＋真实模型
验收在 Core 合入后执行，早期覆盖安装的 demo 结果不作为该配对验收结论。
