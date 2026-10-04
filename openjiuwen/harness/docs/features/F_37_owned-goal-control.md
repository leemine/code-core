# F37 原 root 的精确 Goal 控制

| 元信息 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 范围 | GoalManager 私有 selector、Native 原 attempt 退出 |
| 测试基线 | 实际 Manager/SessionGoalStore/EventManager/DeepAgent，合成执行任务 |
| Refs | 未提供 issue 编号；按已授权实施安排推进 |

## 背景与决策

临时控制请求不能替换原 Goal 执行来源。私有 selector 固定 manager/store/Session、原
Goal ID/revision、原 source 和原 attempt 引用。原锁内先检查同步宿主回调，再静态核对
这些固定事实；正常 usage/assessment 更新不作为换代。set 保留原 root，pause 允许
原 attempt 收尾，同在途 resume 不换源；idle resume、冷恢复 attach 和 EOF 新 root 不开放。

clear/overwrite 只取消捕获的原 attempt，不能使用 whole-origin 取消误删同 root 新 Goal。
待答原 attempt 退出时仅在 active 仍为原对象时清原 interruption state 并唤醒原 supervisor。
原锁内提交与发出一次精确 cancel，释放锁后等待原 wrapper/facade 尾部，避免尾部 assessment
重入死锁；未知退出保留 selector 的原任务以便重试。managed Goal 出队后仍在实际消费点
复核 Goal ID/revision；clear/overwrite 的旧 local work 不能越过该检查。

## 拒绝的方案

不创建第二队列、持久 receipt/ACL 或 Goal 状态机；不按最新 Goal/active Round 补选目标；
不把 pause 当 Provider 退出；不恢复旧 terminal source；不要求 capture/apply 必须同 Task。
临时 producer 的生命周期由宿主原同步 checker 证明，core 核原 source 的词法/引用。

## 验证

新增 39 项确定性组件用例覆盖 callback 换引用、await 撤权、同 attempt resume、
初始 set、出队 stale work、实际 TaskLoop/Scheduler/executor 模型前拒绝、真实 wrapper
IO 尾部等待、外层取消与同 selector 收尾重试。相邻 Goal/Round/STEER/Native 精确退出
回归 238 passed；strict stable 在最终提交来源复验，上一候选 1985 passed。

测试使用锁定 core 804 环境依赖并从候选工作树导入源码；不冒充最终安装包或远端 CI。
最初 strict 在嵌套 sandbox 无法建立 NETLINK_ROUTE，11 blocked；宿主同一 strict 命令
保留断网规则完成。`make check` 已运行，原 DeepAgent 存量 import/长行格式问题保留，
新增独立模块与测试 Ruff 通过，没有全仓格式化。精确命令、来源与日志保存在交付证据中。

## 已知遗留

Swarm 原入口接线、实际模型/工具消费投影、idle 新准入、EOF 输出交接与普通 UI/Provider
验收仍由宿主集成；本片不能代替其完成证明。没有执行真实 Provider 负向探针。
Session commit 本身抛异常后不猜测落盘结果、不自动重放原 mutation；已写状态不回滚。
只有原 mutation 完整返回后的退出/ACK 失败，允许同 selector 仅重试原尾部确认。
