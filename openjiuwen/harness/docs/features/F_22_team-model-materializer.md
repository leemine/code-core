# F_22 Live Team model materializer

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | ModelSpec / BuildContext / DeepAgentSpec / SubAgentSpec |
| 测试基线 | 实际 ModelPool JSON、NativeHarness、Model、SDK；合成内存 HTTP transport |
| Refs | S_12_manifest-declarative-assembly |

## 背景

ModelPool 转换保留 typed client 的扩展元数据，但原 ModelSpec.build 直接创建 Model，
宿主无法在成员实际模型创建时安装每调用权限 SPI。成员与子 Agent 也不能共用授权身份。

## 数据结构与决策

BuildContext 新增仅 live 的 model_factory(ModelSpec, BuildContext) -> Model，及与成员
字段分离的 subagent_name。ModelSpec.build(context=None) 没有 factory 时保持原调用。
有 factory 时只调用一次，拒绝非 Model 返回且不回退。模型 spec 保持可序列化，callback
不存入 spec 或 seed，跨进程重建必须由宿主重新提供可信 live capability。

DeepAgentSpec 先派生成员 context 再构造；父原始模型 spec 仅在 live extras 中传给子
Agent。标准子 Agent 从自身或父 spec 重新调用 factory，不能借用已构造的父 Model。
factory_name provider 可以任意自行构造；本切片在 live factory 模式明确拒绝该入口，
包括未知 provider。自动 general-purpose 注入也明确拒绝；必须使用显式 child spec。
无 factory 的既有 provider、父模型继承和自动注入行为保留。

## 拒绝的方案

- 全局 monkeypatch Model、修改已有 Model 私有字段：跨实例串用且失去构造归属。
- 序列化 callback 或从成员字符串恢复权限：配置不等于 live authority。
- 将 root Model 作为子 Agent fallback：绕过子 Agent 独立授权。
- 只修改 Team ModelPool：显式模型与后续重建也必须经过相同入口。

## 验证

覆盖旧 build、成员派生身份、显式/继承 spec 的 child 重建、缺 spec、拒绝/无效返回、
provider 与自动子 Agent 拒绝。普通确定性测试使用实际 ModelPool JSON roundtrip、
NativeHarness 构造、Model.invoke、OpenAI SDK；仅 HTTP transport 为合成内存替身，
逐成员验证不同授权 header，不需要监听端口或真实凭据。

## 已知遗留

此入口只规定构造，不提供宿主资源授权或请求生命周期。宿主仍需将每成员当前请求的
权限 SPI 绑定到 Model；子 Agent 默认拒绝，独立授权由宿主显式提供。未验证 Team UI、
真实远端模型、分布式、Swarmflow 或活动恢复。factory_name 子 Agent 的受管支持需
独立扩展契约，不能把配置或构造通过作为产品验收。
