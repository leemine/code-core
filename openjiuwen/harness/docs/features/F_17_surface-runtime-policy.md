# Work/Code Surface 冷启动运行策略

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-30 |
| 范围 | harness_protocol、CodexHarness、OpenCodeHarness |
| 关联 spec | S_19、harness_protocol/SPEC.md |
| Refs | R1-11B；用户确认本交付无关联 issue |

## 背景

产品宿主已经冻结 Session 的 Work/Code Surface、Provider Binding、workspace 与授权上限，但
Codex/OpenCode 仍只接收各自私有配置。若只增加 system prompt，Plan 可能仍可写；若直接把产品模式
写入厂商 JSON，则公共层会依赖 Provider 细节，恢复时也可能把策略更新误判成身份变化。

## 数据结构与决策

- `HarnessRuntimePolicy` 是公共、冻结、无凭据的进程周期要求：Surface、normal/plan、workspace
  access、上下文/记忆来源、能力类别、产物类别及 source-discovery。fingerprint 只用于审计。
- `HarnessContext.runtime_policy` 为可选字段。缺失时保持旧调用行为；存在时 Provider 必须在启动
  副作用前编译，不能扩大冻结的 `ExecutionAuthorization`。Plan 构造时强制 read-only。
- Codex 私有 compiler 把 read-only/workspace-write 映射到原生 sandbox 和宿主审批。所有新 Surface
  策略都要求关闭进程环境继承并提供可信 `startup_source_roots`，使原生 AGENTS/Skills/插件默认发现
  不能越过宿主来源边界。已授权 full-access 只放宽执行权限，仍生成并回读 ambient-source 禁用项；
  normal/plan 继续复用命名权限或 legacy restricted 的有效配置回读。sandbox 要求通过原生
  `thread/start` 参数和响应核验；命名权限存在时不再重复注入与其互斥的 legacy `sandbox_mode`。
- OpenCode 私有 compiler 在 generation 封存配置中生成精确 permission map：Plan 默认 deny，仅放行
  读取/搜索/LSP/显式 skill；workspace-write 默认 ask 并拒绝 external_directory/task；full-access
  仍拒绝产品未准入的 task。现有 `/config` 回读确认最终值。
- Codex checkpoint 保存 runtime-policy fingerprint。冷启动策略变化允许重新编译，但不复用旧权限
  fingerprint；来源、插件和 Provider thread 的原恢复核验仍保留。策略 revision 不进入 Binding。

## 拒绝的方案

- 不用 prompt 声明“只读”代替 sandbox/permission。
- 不把 Work/Code 或策略 revision 写入 Provider Binding，避免把冷策略升级伪装成 Session 身份变化。
- 不在公共协议生成 Codex TOML 或 OpenCode JSON，也不公开厂商原生工具名。
- 不把 full-access 理解为允许 ambient context；执行权限与来源准入分别验证。
- 不增加运行中 reconfigure；新策略只在下一 Provider 进程周期生效。

## 验证

公共单测覆盖冻结、稳定 fingerprint、非法枚举、重复来源和 Plan 非只读拒绝。Codex 覆盖授权收窄、
越权拒绝、显式来源/隔离环境前置条件、命名权限与 sandbox 映射、full-access 来源回读和旧配置不变；OpenCode 覆盖三档精确
permission map、越权拒绝及 generation 有效配置回读。下游 swarm 负责 Work/Code prompt、路径、规则、
附件、个人上下文与冷启动快照的产品级黄金/越界测试。

## 已知遗留

产品工具/子 Agent 有效目录属于 R1-11C，typed projection 属于 D，UI manifest 与真实 Single/Team
矩阵属于 E/F。本切片的 `required_capabilities` 和 `artifact_kinds` 是宿主要求，不宣称对应能力已经
安装或可用。Native 继续保留其原运行期热更新，不强制改用 External 冷启动策略。
