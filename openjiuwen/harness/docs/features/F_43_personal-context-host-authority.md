# PersonalContext 宿主授权接缝

| 项目 | 值 |
|---|---|
| 日期 | 2026-10-09 |
| 范围 | PersonalContext、原模型压缩器、飞书 CLI 进程环境 |
| 测试基线 | 原 personal_context、round_level_compressor、model_request_authority；新增宿主接缝回归 |
| Refs | 用户授权省略 issue footer；仓库禁用 Issues |

## 背景

嵌入式 PersonalContext 原先直接构造 Agent、balanced 摘要和压缩模型，宿主无法沿既有 ModelRequestAuthority 校验每次模型 HTTP 请求。飞书 CLI 继承全局 HOME，两个宿主实例可能共用登录配置。配置模型并不等于获得密钥使用授权。

## 数据结构与状态机

PersonalContext 构造接受三个可选内存参数：`model_request_authority`、`fetch_environment`、`execution_check`。参数不进入 YAML、Pydantic 配置或公共导出表。原队列、调度器、消费者和停止逻辑不变。RoundLevelCompressorConfig 的运行时 `request_authority` 从序列化中排除。

## 决策

- 各模型路径透传原 Model 的回调或工厂，包含 Agent 重建和轮次压缩；不复制 HTTP 授权协议。
- 只有模型授权拒绝阻止 balanced 内层降级，其他原降级语义保留；外层授权拒绝也不重试或降级发布。
- 飞书 CLI 按 PersonalContext 实例通过 ContextVar 传递独立环境副本，不改进程 `os.environ`。私有环境缺少 CLI 时要求部署方安装，不自动修改全局 npm 环境。
- 原采集批次、游标与最终发布重验宿主提供的同步检查。最终文件提交前在线程内执行检查；后台主动停止仍由宿主调用原生命周期接口。
- 私有 CLI 取消/超时后等待被终止子进程退出；退出超时是失败，不冒充停止成功。

## 拒绝的方案

不引入账户服务、IAM、第二个采集队列或通用执行权限存储；不使用全局环境变量切换账户；不把授权失败当网络故障降级。

## 验证

测试覆盖双实例 CLI 环境、真实 Model HTTP transport 前授权、压缩器撤权、balanced 拒绝不发布、提交线程拒绝不写入。影响面与 stable 的命令、来源和最终结果由仓库外管理任务的验证记录留档，开发阶段结果不等于下游锁定来源验收。

## 已知遗留

Core 不定义用户或授权策略。宿主必须提供正确的来源范围、模型密钥解析、活动撤权停止以及每个账户的 CLI 身份。第三方 CLI 若在 HOME/XDG 之外使用系统级凭据，部署方需要独立账号或隔离执行环境；仅传入环境变量不能宣称完成所有外部系统身份验证。
