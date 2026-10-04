# F_26 Scheduler owned exit capture

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | core TaskScheduler 私有 wrapper 归属查询 |
| 测试基线 | 定向 16 passed；受影响 165 passed、1 原有 LLM skip；strict stable 1738 passed |
| Refs | S_03_task-loop；F_24_owned-task-stop-confirmation；R2-B4 凭据退出基础 |

## 背景

原运行列表先摘除任务，再等待终态事件和 completion signal 的尾部 IO。此时业务状态已终态，
实际 wrapper Task 仍未退出。现有 owned 集合能支持整体 stop，但只保存 Task，不能按原 task_id
取得精确对象供后续 Round 退出确认使用。

## 数据结构 / 状态机

将原 `_owned_execution_tasks` 集合替换为 task_id 到 wrapper Task 的映射，在既有唯一创建点
登记，done 回调按原对象释放。继续复用原调度循环、锁、Task、业务状态和停止集合；不增加
队列、持久化记录、状态机或第二份任务状态。

## 决策

- 私有同步 `_capture_owned_execution(task_id, expected_task=...)` 只在原调度器事件循环查询。
  返回未退出的原 wrapper，包括 terminal event/completion tail；不取消、不 join。
- 无活跃 wrapper 返回 None；None 本身不能证明任务已经执行或已经退出。调用者须保存
  原 Task，以其 done 事实判定；有 expected 时，复用 ID 的新 Task 必须拒绝，不能重新绑定。
- running 与 owned 映射不一致、原 expected 尚活跃却归属消失均拒绝。空或非字符串 ID 拒绝。
- 同 ID 的旧 wrapper 未 done 时不再次调度；已退出后可沿原流程复用 ID。迟到回调只能移除
  同一 Task，不能移除新 generation 的对象。
- stop 继续等待原 owned Tasks，超时/调用者取消保留原停止对象；不改变公开 abort/cancel
  返回语义、原错误传播及整 Session 释放策略。

## 拒绝的方案

不以 TaskStatus 或空 running 列表当退出证书，不扫描所有 asyncio Tasks，不把某个 Round
映射到整个 scheduler owned 集合，不另建任务状态/索引服务，不在此基础切片增加原生 abort
接口或 Session 级取消，不将缺失归属默认为最新任务。

## 验证

原停止用例适配私有集合形状；新增 7 个确定性用例覆盖完成/取消后 wrapper tail、tail 期间
拒绝 ID 复用、退出后正常复用、旧 expected 拒绝新对象、迟到回调、映射歧义/丢失和非法 ID。
原 stop 超时重试、外层取消、并发停止、self-stop、真实调度循环、DeepAgent round stop 和
interaction 回归保留。新增用例位于 stable 已收录的原测试文件，manifest/超时不变。

验证使用固定依赖环境加本候选源码，不等同干净非 editable 发布或下游锁定配对。初次本地
strict 因 sandbox NETLINK_ROUTE 权限不足 11 blocked；同隔离配置获授权环境运行 strict 后通过，
没有降级为 audit。make check 已执行；Ruff 检查通过，原模块格式差异和既有 Pylint 告警未
全文件整改，模块新增私有接口后超过 1000 行阈值；环境缺 codespell，不能宣称完整 lint 无告警。

## 已知遗留

原 Turn 到 Round/task 的可信捕获、精确取消和有界退出仍需后续 core/host 接线。
本查询不授予权限，不使 logout/revoke/expire 自动停止活动 Provider，不证明同 Session
混合凭据的原生执行/持久 subagent 已能精确退出。真实 Provider 验证及 core/swarm 新配对仍独立。
