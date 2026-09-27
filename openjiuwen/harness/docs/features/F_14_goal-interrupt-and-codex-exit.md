# Goal 交互等待与 Codex 退出确认

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-27 |
| 范围 | Native Goal 的交互恢复；Codex 停止、idle retry、认证 fallback |
| 测试基线 | 扩展组 666 passed + 1 既有 opt-in skip；真实 Native 四项、Codex 替换聊天一项及本机 CLI 两项 |
| Refs | S_02、S_11、S_19；管理仓 R1-09B 交付验收 |

## 背景

真实 Goal 验收暴露两个问题。Native 权限等待结束当前任务循环后，交互 supervisor 错把
未获回答的 Goal 当成空闲任务，再次增加 attempt；Codex 普通聊天替换 Goal 时，interrupt
应答后立即关闭 App Server，可能让旧工具进程与新用户输入重叠。

## 数据结构与状态机

Native 保留原 ActiveInteractionRound，以 waiting_for_input 标识暂停等待；RoundOutcome
显式携带 interrupted。等待期间 EventManager 只交付 InteractiveInput，回答恢复原 Goal
上下文与同一 attempt。普通用户任务保持原队列次序，pause/resume 不重复领取 attempt。

Codex 以私有 _NativeTurnDrain 保存一次物理 SDK Turn 的 client、启动、reader 与确认结果。
逻辑 Turn 仍由 SerializedTurnHarness 唯一管理。关闭路径等待同一 reader 消费匹配的原生
terminal、等待在途连接结束并关闭精确 client，最后等待所保留 SDK Popen 句柄退出。真实反例证明即使 terminal 排空，工具父进程退出后
仍可能留下 sleep 后代，因此 Linux 为每个 client 引入私有 subreaper 包装器。包装器继承
stdio，不读取协议；CLI 退出或收到停止信号后逐个终止自己的直接子进程，收割及收养后代，
直到 waitpid 确认无子进程才以 0 退出。

## 决策

- Native 回答恢复继续原 TaskIteration 生命周期，保留 report/transcript；取消不消费尚未结算报告。
- Codex 原有 SDK stream 是唯一消费者；idle timeout 使用 shield 保留正在进行的读取。
- RPC 应答、流 EOF、逻辑 ABORTED 都不单独充当资源已退出的证据；确认失败保留资源重试。
- fallback 每个可能挂起的阶段均重新核对取消；停止期间出现的迟到连接先关闭，不派发新请求。
- Linux 的 subreaper 仅设置于新建包装器，不污染宿主进程，不按全局进程名或进程组杀进程。
- 清理使用私有互斥锁与在途连接 future，不改变公共协议、既有交互通道或调度所有权。

## 拒绝的方案

不在宿主增加第二套 Goal attempt 调度或第二个事件消费者；不把审批等待算作新 attempt；
不把 SDK close 返回或 mock 测试通过视为真实工具退出。不把 App Server 或工具父进程退出等同于后代全部退出；不通过缩短测试工具或忽略活体
子进程规避普通 Web/Gateway 替换聊天的验收。

## 验证

Native 确定性测试覆盖 ask_user/permission、同 attempt 回答、pause/resume、clear/覆盖、
stop 和取消期间报告结算。Codex 覆盖迟到 terminal、错误 turn id、EOF、在途 start、idle
retry、pending steer 失败、fallback/stop 竞态、迟到 client 关闭失败及同句柄重试。

本地冻结候选真实 Native 四项、Codex 普通请求替换 Goal 一项以及本机 CLI 两种时序均通过。
Codex 在新请求 accepted 和实际新工具入口两处核对旧包装器、CLI、工具父子进程均不存在。
make check 已执行；存量格式/import/拼写和 pylint 诊断未整体整改，新增复杂度及私有 SDK
接缝诊断如实记录，不把 Makefile 忽略退出码当作检查全绿。

真实 Native 与 Codex 验收结果、来源版本和清理记录由管理仓交付证据维护；必须在最终
正式 core 及锁定该 core 的 swarm 配对上验证，不能沿用修复前 SHA 的成功结论。

## 已知遗留

Linux 私有 subreaper 保证不能外推到未运行该包装器的其它操作系统。SDK 子进程接缝依赖锁定的 openai-codex 0.144.4 私有布局，升级必须重新验证。普通
Web/Gateway 替换聊天仍沿用原取消旧流、等待退出、启动新流的语义。
