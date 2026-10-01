# scheduled 临时 reviewer 构造与退出所有权

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-01 |
| 范围 | 原 scheduler、CoordinationKernel、同 Provider factory 的可选临时执行端口 |
| 测试基线 | core 991 passed / 20 既有 skipped；下游 408 passed；普通 Team 真实 CLI 本机模型 8 passed |

## 背景

scheduled 原分支直接构造 Native TeamHarness，且丢弃后台评审 Task。仅替换模型构造
不足以支持 External：投票可以早于 Provider 结束，停止也可能先释放 Team 会话资源。

## 数据结构与状态机

TeamReviewRuntimeBuild 携带独立 invocation、原 reviewer/task/round、原工具/prompt
及已绑定 Team Session。TeamReviewRuntimeFactory 是同一成员工厂的可选结构化扩展，
autonomous 旧工厂不变。_review_runs 只保存原 scheduler 的临时执行所有者，不替代
票据 DB、Provider 队列或 Session 状态机；cleanup Task 在调用者取消后仍可被原句柄等待。

## 决策

External 缺工厂/Session 或 Provider 不一致时拒绝，不降级 Native。run_once 的失败/
未知结果不自动重放；Native 保留原 181001 重试，但先确认旧实例 dispose。kernel
pause/stop 先排空评审，再释放资源；失败保留生命周期和会话供重试。已有票据的
reviewer 不重派；未决票据仍可催办升级；PASS/FAIL 必须等本轮所有者退出后再原 CAS
settle。成功清理触发同一幂等 board scan。

## 拒绝的方案

不把临时 reviewer 伪装成普通成员/产品子 Agent，不用 Native 回退满足 External 选择；
不把投票当 Provider 退出，不在 timeout 时遗忘所有者，不取消仅有的 cleanup；不复制
调度器或事件消费者，不凭 core 端口通过解除产品 scheduled 拒绝。

## 验证

15 项新增确定性用例：Codex/OpenCode 缺端口拒绝、原 Verify 工具投票与退出顺序、
部分票据恢复仅派缺票者、失败/超时/取消等待者后重试同一清理、未知运行不重放、
kernel pause/stop 清理失败保留 Session、错 Provider/无 Session 拒绝、Native 重试与
暂停后缺票恢复。原 scheduler mock 的同步 task_verification_enabled 改用 Mock，
消除测试创建未 await coroutine 的告警。首轮 9 个新 fixture 接口错误和 1 个催办被
误挡失败留档；修正 complete 调用与只阻塞确定性结算后定向 69 passed。
扩大组 991 passed / 20 skipped（1 DSH timing opt-in、19 原自主协调 skip 标记未改）；
下游 408 passed；真实普通 Team CLI 8 passed 是相邻回归，不代表 scheduled E2E。

## 已知遗留

产品仍拒绝 scheduled，宿主临时 Provider 的绑定/持久未知状态、权限交互、单消费者
历史/UI/root Goal 用量尚未实现。真实 scheduled CLI、跨 attempt pending review、冷恢复
与远端模型/渠道/Cluster 均待验证；本地 dirty 联调非锁定安装或发布配对。

### 完整产品入口补验（2026-10-01）

真实远端模型首次调用 reviewer 的 `view_task(action=get)` 暴露错误装配：
`ViewTaskToolV2` 收到了 TeamBackend，而其公共构造要求 TeamTaskManager。
改用与 verify_task 同一个 reviewer-scoped manager；任务详情与 in_review 列表
均保持 reviewer 身份，并以真实 SQLite 看板回归。此前只调用 verify_task 的
脚本化场景无法证明 view_task 可用。完整产品验证结果仍以管理仓证据为准。
