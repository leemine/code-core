# F_24 Owned task stop confirmation

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | core TaskScheduler 与 DeepAgent.stop 退出和重试 |
| 测试基线 | 96 passed、33 subtests passed；含 11 个新增退出用例，源码环境验证 |
| Refs | S_02_deep-agent-architecture、S_03_task-loop；R2-B4 cleanup |

## 背景

原调度器只发送取消，不等待执行 Task 真正退出；DeepAgent 又抑制 controller.stop 错误。
任务还可能已从运行列表摘除、却仍在发送终止事件，单看运行列表或 facade 的 EXIT 标记不足以确认资源释放。

## 数据结构 / 状态机

复用原调度循环、锁、Task 和 DeepAgent 的启动/停止锁。内部 `_owned_execution_tasks`
保存实际创建到 done 的强引用，`_stopping_tasks` 保存未确认退出的原对象；均不是调度队列。
不新增公共参数、协议状态或持久化系统。DeepAgent 在 teardown 开始进入 TERMINATED，
但只有 controller.stop 成功后才清除 started；失败可用原对象重试 stop。

## 决策

- stop 仅取消原 owned Tasks，内部五秒等待实际退出，包括终止事件的尾部 IO；未退出或调用方取消时保留句柄。
- 调度循环在原锁内复核 running 再创建任务。停止尚未结清时拒绝 start。
- 并发停止不重复取消已经清理的任务；调度器等待子任务使用 shield，避免取消等待方再次打断子任务清理。
- self-stop 在写停止集合前拒绝，保证合法外部 stop 仍能发取消。所有任务已退出时先移除原引用，再报告异常，避免历史执行错误变成永久资源锁。
- DeepAgent 不抑制 controller.stop 错误，保留同 controller/session 重试。既有 unbind 错误抑制不构成退出确认，且不删除 owned Task 引用。

## 拒绝的方案

不把 task.cancel() 或空运行列表视为退出，不扫描/取消进程中的其它 Tasks，不对正在清理的任务反复 cancel，
不无限等待、不增大宿主停止超时、不用新 controller 替换失败对象，不改变运行列表先摘除再发送 UI 事件的既有次序。

## 验证

11 个新增合成用例覆盖正常退出、非 owned task 隔离、超时与重试、调用方取消、并发停止、
调度等待方取消、退出异常可重试、query await 后晚到调度、运行记录摘除后的终止事件 IO、
self-stop 后合法外部停止，以及 unbind 成功/失败两分支中的 DeepAgent controller 保留。
原 Controller 并发、DeepAgent interaction、Native host/output/rendered-result 与 manifest 合计
96 passed、33 subtests passed，11.03 秒。两新增测试文件加入原 stable 分片 discover/command，未改变门禁和超时。
验证使用源码 overlay；不等同干净非 editable 安装或 core/swarm 锁定配对验收。

## 已知遗留

真实 Provider 退出与新 core/swarm 配对必须另验。旧宿主只快照 `_running_tasks` 的通过记录不能
覆盖终止事件尾部 IO，也不能当成本修复已实际安装的证明。未扩大到完整活动恢复、远端进程隔离或 Swarmflow。
原 unbind 持久化/订阅错误抑制保留；不得借此抑制 controller.stop 失败。
make check 已执行；新 scheduler/tests Ruff 与测试 Pylint 通过，DeepAgent 既有格式/导入及 Pylint 告警未全文件整改，环境缺少 codespell。
