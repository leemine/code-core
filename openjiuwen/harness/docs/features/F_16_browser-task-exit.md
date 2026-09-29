# Browser 任务退出确认策略

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-29 |
| 范围 | BrowserExecutionToolGateway、BrowserAgentRuntime、BrowserService、ManagedBrowserDriver |
| 测试基线 | Browser 单测；下游隔离 Chrome/MCP 的持久 Cookie、跨 Session/Profile 与慢下载取消 |
| Refs | R1-10D、S_20、S_05 |

## 背景

不同任务实例复用持久 Profile 时，旧 Chrome 保活会继续占有目录并可能继续下载。
只释放 Task 绑定无法交接 Profile；直接 terminate Chrome 又可能丢失刚写入的持久 Cookie。

## 状态与决策

Gateway 增加可选 `stop_on_close=False`，默认 release 行为保持兼容。宿主选择 True 时，
在原 catalog/invoke 锁内调用 `runtime.reset(graceful=True)`，经现有 service registry 的
排他 reset barrier 停止精确身份的 MCP 与受管 Chrome，随后释放 Task 并置 closed。
磁盘 Profile 不删除，instance 与 profile 身份保持分离。

优雅停止只对本 driver 拥有的活进程发送 CDP `Browser.close`，有界等待进程退出以便落盘。
CDP 错误或超时后仍通过原 terminate/kill 路径确认退出；不能证明退出时保留 driver 与
registry 失败 barrier。强制退出只能证明进程停止，不承诺最后一批登录数据完整落盘。
调用方取消/超时不能取消内部清理任务；gateway 保留该任务，下次 close 加入同一清理。
失败后可再次发起清理，不把 EXIT_UNCONFIRMED 当作 closed。

## 拒绝的方案

- 用 Profile ID 代替 instance ID：会混用任务 MCP、init 与输出配置。
- 删除 Profile 解锁：破坏持久登录语义。
- 用 init-page 的 marker 消失证明退出：它无法独立确认 Chrome 级下载已停止。
- 全局改变 Native 默认退出策略：本次只增加宿主显式选择，旧 reset/release 调用不变。

## 验证

确定性用例覆盖关闭失败不 release、幂等重试、取消后复用清理任务、优雅关闭的所有权、
进程退出等待与 CDP 失败回退。相邻 Browser 回归保留旧调用行为。
下游真实 Chrome 测试先复现 terminate 后持久 Cookie 缺失，再验证优雅停止后不同 child/
Session 复用相同 Profile、另一用户隔离。慢下载项观察真实 `.crdownload` 和 pending，
取消调用后关闭 gateway，确认进程退出、文件不再增长、部分产物不发布，再由新 child 获取 Profile。
具体命令与来源配对记录在产品管理仓；本地源码联合验证不替代正式锁依赖 CI。

## 已知遗留

暖 Chrome 移交、逐 GUID 取消与跨进程 Profile 租约未在本切片实现。
外接 Chrome 不归 managed driver 所有，不能靠本策略停止其下载。
Native 通用 cleanup 错误传播不因本可选参数改变。
