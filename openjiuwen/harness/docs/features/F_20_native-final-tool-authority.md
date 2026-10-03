# F_20 Native 工具最终授权

- 日期：2026-10-04
- 关联 spec：S_05、S_08
- 状态：实现与确定性验证；Provider/UI 和下游锁定配对验收由集成批次完成

## 问题与决策

PermissionInterruptRail 的审批和最终 rail 完成之后，Tool 的输入 callback、transform
以及 TOOL_CALL_STARTED 观察者仍能改写真实参数。只授权 rail 快照会漏掉重定向后的
操作；禁用这些 callbacks 又会破坏已有插件能力。

在 Tool._ToolMeta 的生命周期包装内，全部输入/started callbacks 完成后、真实原始
bound invoke 调用前，执行本次绑定的强制授权。授权对象复用不可变 BeforeToolContext；
不增加协议字段、状态机、Session 或交互通道。原普通权限检查、确认和恢复流程保留。

## 接缝与生命周期

`bind_tool_authorizer(ctx, callback)` 只接受 AbilityManager 此次执行创建的精确
AgentCallbackContext；追加回调且同一 callable 幂等，不能覆盖既有强制检查。
AbilityManager 在本次 rails 与工具调用结束时销毁绑定；父调用的强制上下文若被子任务
继承，不能自动降级为无保护。无强制授权的旧任务复制上下文仍保持旧行为。

`current_tool_invocation()` 仅在最终授权回调期间提供 frozen 本地 ToolInvocation：
最终 operation、真实 executor、已有 agent_context 和即将执行的 original_invoke。
证明校验具体实例、构造时登记的 callable、当前注册映射、Session/agent、最终参数及
回调集合；每次 await 后复核。证明退出即失效，复制 ContextVar 不延长其生命周期。
登记表只保留弱引用，不把 Tool 生命周期扩展为进程级缓存。

严格 True 才允许执行；False/None/其它值、异常、执行器或参数变化均拒绝。
取消继续传播，不把取消转为批准。最终回调不产生第二轮 UI 确认。

PermissionInterruptRail 非空 host.authorize_tool 保留审批前后检查，并自动绑定最终
桥接。最后一次 PermissionSceneHookInput.tool_args/tool_call 使用转换后的参数；
ctx 保留真实原始执行上下文，资源映射应从最终 proof 读取实际 operation。

## 不采用的方案与限制

不依赖工具名称或 AbilityManager 的旧参数证明实际调用；不把 generic before callback
视为强制边界；不永久禁用合法输入插件。直接替换已装配 invoke 的未知包装器在保护
模式拒绝，避免在 mandatory 检查之前先执行未知代码；未保护模式保持旧功能。

当前强制范围是 AbilityManager 的已登记 Tool.invoke。保护模式的 Tool.stream 和
非 Tool ability 明确拒绝，需独立扩展后才能宣称支持。直接在上下文外调用 Tool 的
旧 SDK 行为不受此接缝管理。本方案不是恶意进程内 Python 插件沙箱，也不约束任意
插件 callback 自行直接访问 OS；宿主仍须信任装入进程的代码。

资源路径、cwd、Workspace、backend、凭据和授权世代属于宿主逐次策略；本接缝不
声明操作系统隔离或路径沙箱，也不代替下游对主体/Session 的精确所有权证明。

## 验证

新增 core/foundation/tool/test_final_authority.py 覆盖真实 Tool + AbilityManager 链：
合法 transform/started 顺序、重定向被重新授权、严格 True、授权期间参数/Session/
invoke/注册表改变、替换 invoke 未执行、证明和旧 ctx 过期、复制上下文的保护/旧行为、
并行调用隔离、原 PermissionRail 前后检查与最终检查、stream/非 Tool 拒绝。
用例加入 stable native-output 的 discovery 与 execution，以及 manifest 完整性守卫。
受影响工具暴露、渲染、中断及 Native host 回归同时执行。严格 stable 的实际 SHA、
运行归档和来源核验随候选证据记录，不将源码测试视为完整 Provider/UI 验收。

最终输入只允许一个 inputs 参数。运行期 kwargs 必须保留 AbilityManager 提供的原有
键及对象身份（session、明确支持时的 _tool_callback_context）；输入插件不能增加
BeforeToolContext 中不可见的其它执行参数。无强制授权仍保留旧 kwargs 行为。
