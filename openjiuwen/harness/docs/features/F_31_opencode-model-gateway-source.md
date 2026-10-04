# OpenCode 模型网关的原请求来源

| 元信息 | 内容 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | OpenCode Provider 私有网关配置、managed hook、原 Turn 来源证明 |
| 测试基线 | 固定 CLI 1.18.18 的原生 hook 形状；确定性 Python + Node；pr-stable |
| Refs | S_19、既有 mandatory preflight 与产品 ticket |

## 背景

OpenCode 静态 apiKey 配置不等于逐次 HTTP 的凭据授权。宿主模型网关需要知道一次实际模型请求
属于哪个原始 Turn，不能按 HTTP 到达时间选最新权限。固定 CLI `chat.headers` 输入携带
Session、user message、agent 与 model；其输出进入实际模型请求 headers，但该 hook 本身
不承担最终 HTTP 授权，也不证明重试后的权限仍有效。

## 数据结构与生命周期

Provider 私有 `model_gateway.py` 定义 `OpenCodeModelGateway(url, token, generation, model,
destination)`。`destination` 是固定上游 API base；本片路径只支持 `POST /chat/completions`。
它仅作为 `OpenCodePreflightEndpoint(..., model_gateway=...)` 的 keyword-only 宿主装配值，
不进入 JSON provider_config 或公共 `__all__`。网关必须属于原 loopback listener 和 generation。
CLI 配置使用网关 URL/local token；配置含上游 api_key、模型名或 API base 不一致时启动前拒绝。
稳定身份包含固定模型与 destination，排除临时网关地址/token/generation。

原 `PreflightGate.begin/clear/close` 同时限定模型来源证明的有效期，不增加队列、事件消费者、
授权存储或第二状态机。只读 `OpenCodeModelSource` 捕获原 Turn/context/transport/config 与
root；同一已捕获对象可复核，复制不构成证明。其可读事实为 turn_id、session_id、root_message、
model、method 和 path；没有上游凭据。

## 决策

1. managed `chat.headers` 复用原 staging、snapshot、loader inventory、回读和失败清理。
   原 supported hooks 增加 `chat.headers`，legacy 显式插件可以声明；组织 mandatory 模式仍
   拒绝额外插件/skills/MCP 来源。保留 header 冲突、非 primary build、源消息/model 不符时抛错。
2. 网关模式提交明确 `agent=build`。hook 提供精确 session/root/generation/agent/model/provider。
   宿主先认证 local token，再用实际请求 method/path/body.model 调用
   `harness._capture_model_source(headers, *, method, path, model)`。headers 参数仅包含六个
   `SOURCE_HEADERS`，重复、别名和未知保留头由宿主解析层拒绝；本函数也拒绝多余字段。
3. capture 在第一次 await 前固定原对象，GET 原生消息要求唯一 root，role=user、原 Session、
   build agent、固定 provider/model 全部匹配；返回时复核全部原对象。缺失/查询失败/原范围变化
   返回 None，不退回当前最新 Turn。abort、stop、clear、close 或配置/transport 替换立即失效。
4. 宿主通过 `_is_model_source_current(source)` 在凭据解析后、实际发送前及输出 chunk 前复核
   同一对象。每个实际 HTTP/SDK retry 都重新捕获源、重新执行宿主凭据授权；源证明不授予权限。
   宿主仍须冻结完整 body bytes，固定上游 destination，禁重定向/env proxy/底层重试，管理响应关闭。

## 拒绝的方案

- 将静态 apiKey 写入 CLI 后称为动态撤权；凭据不可在配置阶段代替实际 HTTP sink。
- 依赖 `chat.headers` 失败会阻断所有 SDK 路径；缺头必须由宿主网关拒绝且不发送上游。
- 复用 tool context/ticket、到达时间或 latest Turn；模型请求有自己的原消息来源事实。
- 为模型增加第二 Turn 状态机，或允许 provider_config 自报任意 gateway。
- 将 compaction/title/subagent/Responses/OAuth 自动视作 build；未支持用途明确拒绝。

## 验证

新增真实 Node hook/原 loader 合成组件测试，覆盖普通 primary、保留头冲突、用途/model/session
不符、hook 遗漏、SDK 重试式重复请求、原查询 await 期间 Turn/context/transport/gateway/config
变化、stop/abort/clear/close、首次迟到原 root、复制证书、配置/readback overrides、legacy
静态模型及显式 chat.headers 插件。实际原 loader 对错误 hook 库存不发布 ready inventory。
新增测试追加到原 stable OpenCode suite，保留旧用例与 timeout。新隔离 uv.lock 环境中
OpenCode 影响回归 257 项、manifest 回归 2 项通过；strict pr-stable 1807 项通过，无失败或跳过。
Ruff lint 通过；make check 的旧文件格式/Pylint 与新私有证明复杂度提示单列，不当成授权证明。

## 已知遗留

本片尚不含宿主实际 HTTP/stream credential consumer，不宣称 B3、模型凭据、完整 OpenCode
或 Team 验收。真实固定 CLI 新 hook 的普通允许故事须在宿主接线后独立验证；本包仅确定性测试，
没有新增真实负向、迟到、取消或撤权探针。不提供 OS 或网络沙箱。已送达的响应字节不能撤回。
