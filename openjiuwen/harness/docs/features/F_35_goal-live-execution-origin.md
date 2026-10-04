# F_35 Goal 的 live 执行来源

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | GoalManager / NativeGoalExecutionAdapter 原准入与后继 attempt |
| 测试基线 | 隔离源码、固定正式依赖；确定性实际组件，不调用 Provider 或模型 |
| Refs | 本轮 Native 能力保留；未提供 issue 编号 |

## 背景与决策

Native Goal adapter 原先直接创建无来源 work。set 的来源以及后继 attempt 的原根没有贯通，
长驻 supervisor 再 ensure 时不能借用其启动上下文。Manager 的单个私有 live 绑定记录原
Goal identity 与 source，set 首次 await 前固定，之后只在同 Goal 路径复用。实际工作和
输出仍由原 EventManager / interaction emitter 驱动，无第二队列、消费者或持久授权。

绑定不进入 GoalRecord/state；get/peek、copy 和 JSON 不携带宿主对象。Native adapter 在
实际入队前重新检查来源并附到原 RoundWorkItem；Manager 对自动 ensure 显式遮蔽 ambient。
新的 set 是新的 Goal ID，旧 work 保留自己的原 source；来源失效不能被同名或新 ambient 替代。

## 拒绝的方案

不让每次 ensure 从 supervisor/current owner 猜来源；不把 source 放到持久 GoalRecord；
不为恢复功能创建新状态机或执行者；不因 set 来源可用就放开全部宿主 Goal 控制。

## 验证

实际 Native adapter / EventManager 与 GoalManager / SessionGoalStore 覆盖来源、撤销、
锁/commit 等待、后继 attempt、输出、legacy 和序列化边界。新增 24 项确定性用例已与完整 Goal、原 Round/Native host/exact-exit 及 manifest 回归
合计通过 217 项测试和 39 项 subtests。来源测试不替代真实 Provider 验收，具体结果
与命令在本片证据中记录。

## 已知遗留

本片不打开 swarm managed Goal。idle resume 换根与持久 ACTIVE attach 需要显式新的
Runtime 准入及原 Goal ID/revision 检查；pause/clear 需要宿主精确目标选择。Provider EOF
handoff 不能借旧已结束 Turn 的权限，需要原显式 Goal 请求的 Runtime 生命周期接线。
完整恢复和后台子工作权限仍独立；拒绝未闭合能力不代表能力保留验收通过。

重新构造的 Manager 读取持久 ACTIVE 记录时没有 live slot，core 保持 legacy None；
这不是认证恢复。受管宿主必须在 attach 前拒绝或提交显式新的合法 admission，不能凭
GoalRecord 的 ACTIVE 字段直接开放执行。旧 Goal 的迟到 begin/usage/assessment 先经
现有 ID/revision 校验返回，不通过新绑定补权或改变新 Goal。

pause/clear 的原持久写入/commit、丢弃排队 work、clear 原 cancel 顺序保持；原来源若在
输出阶段失效，会拒绝 emit 并抛异常，但已经 PAUSED/clear 的状态不会回滚。此片未提供
独立 cleanup-only 权限或 control selector，调用者不能把该异常解释成“未发生写入”。

set 替换旧 Goal 后等待原取消端口返回，返回新记录前再次检查新 Goal 原来源；
已排入的新工作保留该来源，等待期间撤销不能通过成功返回或无来源 work 绕过。
