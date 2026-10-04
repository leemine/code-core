# F_25 Owned interaction round stop confirmation

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | DeepAgent 原 interaction round 的退出确认与停止重试 |
| 测试基线 | 实际 DeepAgent 合成 owned round finally 阻塞反例；受影响回归见验证 |
| Refs | S_02_deep-agent-architecture、F_24_owned-task-stop-confirmation；R2-B4 cleanup |

## 背景

controller 只拥有调度执行 Tasks，不拥有 DeepAgent 的 interaction round facade。
原 `_cancel_active_round` 发送 cancel 后返回，supervisor 的 `asyncio.wait` 也不 join
其取消后的 round。stop 随后清空 facade 指针并允许宿主回收资源，round finally 却仍可能运行。

## 数据结构 / 状态机

沿用原 DeepAgent phase、start/stop 锁与原 round Task；私有
`_stopping_interaction_round_tasks` 只保留待确认的原引用，不是队列或新的状态机。
不新增公共参数、协议、持久化或宿主回收流程。

## 决策

- stop 首次 await 前固定原 round，先 fence TERMINATED，资源释放前确认该 round done。
- 复用 TaskScheduler 原五秒内部停止预算，`asyncio.wait` 不把调用者取消传播给正在清理的 round。
- timeout/调用者取消保留原引用及原 controller/session/started；仅对未取消的 owned Task 发 cancel。
- 原 start/stop 锁串行并发 stop；self-stop 在拿锁/改停止集合前拒绝，外部合法 stop 仍可完成。
- 已退出但失败的 Task 先移除原引用，再报告异常一次，避免历史错误永久锁住资源。
- supervisor 在唯一 create_task 点前复核 phase，阻止 follow-up promotion await 后迟到创建 round。
- 普通 cancel_round/goal overwrite 保留原有有界取消语义，不强制等价于完整 Session stop。

## 拒绝的方案

不把 cancel()、TERMINATED、controller.stop 或空 facade 指针当成 round 退出；不靠宿主事后
发现任务仍活跃弥补已发生的工具资源回收；不扫描全局任务、不增大停止预算、不重复 cancel
正在执行的 finally、不用新对象代替旧停止所有权。

## 验证

原实际 DeepAgent stop 反例已失败：stop 返回而 owned round finally 尚未退出。修复后
9 项新合成用例与原 interaction/controller-stop tests 共 45 passed。覆盖待退出 finally、
超时重试、无 active record 但有 round、调用者取消/旧指针丢失、首 await 捕获、并发 stop、
外部 task 隔离、自停后外部停止、退出异常结清及迟到 promotion。新文件加入原 stable
分片的 discover 与执行命令，不改变超时、strict 网络隔离或历史白名单。
受影响 Native host/output 与原 TaskScheduler.stop 合计 73 passed。runner/manifest 10 项通过。
本地 strict stable 的独立记录在 `/tmp/r2b-core-round-stop/`；源码测试环境为现有
Python 3.13 非 editable 依赖环境加本候选源码，不冒称新的干净安装配对验收。
make check 已执行；DeepAgent 既有全文件格式/import/Pylint 告警保留，未无关格式化。

## 已知遗留

本包只修 core 拥有的 round 退出确认。宿主 MCP after_stop 安全回收须实际集成此 core 后
另验；本包不代替真实 Provider 或最终 core/swarm 配对验收。其它生命周期回调的原错误
处理与停止预算保持，不宣称任意外部/未注册资源都受这个 round 集合拥有。
