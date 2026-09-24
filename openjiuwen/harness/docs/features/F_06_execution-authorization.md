# F_06 Explicit execution authorization and legacy compatibility

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 日期 | 2026-09-24 |
| 范围 | harness_protocol、engine Binding、Provider 编译 |
| 关联 spec | S_01、S_19；harness_protocol/SPEC.md |
| Refs | R1-05 OC0/OC3；尚未提交 |

## 背景

宿主曾将 full-access 直接写成 Codex 私有字段，并从该字段决定产品工具审批。
加入新 Provider 前必须分离宿主授权决定与厂商配置编译。同时，旧 Session metadata 和冷归档
只保存旧配置指纹，不能因新增字段使未改配置的会话失去恢复能力。

## 数据结构与决策

- 冻结的 `ExecutionAuthorization(full_access: bool)` 是可信宿主输入；模型配置不授予权限。
- `AgentExecutionSpec.authorization=None` 保留原始 JSON 和四元素指纹；显式值才加入
  `authorization_v1` 指纹分量。`None` 与显式 `False` 有意不相等。
- 可选 `HarnessAuthorizationProvider` 端口负责纯配置编译与历史授权读取；不改变旧
  `HarnessProvider` 必需接口，也不启动 SDK。目前只有 Codex 实现该端口。
- Codex 编译统一覆盖 bypass 与受管 MCP 审批模式；显式授权拒绝 TOML/thread_config
  中冲突的私有权限覆盖。普通授权保留运行时审批及策略回读，不承诺 OS 沙箱。
- 旧 Web full-access 经 core `apply_legacy_full_access` 保留原 Codex 投影内容和指纹；
  非 Codex 仍不受该旧兼容路径影响。新 Provider 必须适配显式端口，不能借此默认放行。
- 已有 Binding、Session 身份、checkpoint、历史格式与严格恢复校验保持原契约。

## 拒绝的方案

- 不给每个旧 profile 自动补授权默认值：这会破坏旧归档的精确指纹。
- 不为迁移放宽 fingerprint 校验：同 revision 的授权变化仍须被发现。
- 不在宿主增加 OpenCode/Codex 私有参数分支，也不从模型或能力卡推导权限。
- 不把 full-access 与模式切换、文件隔离、多租户安全或 OpenCode 接入完成混为一谈。

## 验证基线

本地 editable 联合测试：协议/engine/Codex 单测、可选 SDK 禁导入、旧指纹黄金值、
宿主配置/Binding/加密恢复/准入测试，以及真实 Codex CLI + loopback 模型的
原生工具和产品 MCP 允许/拒绝/full-access。管理仓保存确切命令、结果与来源。
另用改动前 engine/config_source 源码在独立进程生成普通/full-access 归档，新代码恢复。

## 已知遗留

OpenCode 尚未注册。Native/Claude/DSH 显式授权尚不支持，旧路径继续保留。
修改老 profile 为显式授权是身份变更，应新建 profile/Session，不能重写旧归档。
正式 core 远端提交、Swarm 锁更新、干净锁安装与配对 CI 仍需后续完成；本地结果不替代它们。
