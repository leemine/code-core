# Codex steer 接受回执与结束竞态

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-01 |
| 范围 | Codex Provider 投递回执；原队列与原生 reader 复用 |
| 测试基线 | Provider/External 581 passed、1 既有 opt-in skip；下游 408 passed；真实 CLI 本机模型 8 passed |
| Refs | S_19；管理仓 R1-11F 第 15 节 |

## 背景

Team 自动启动成员后立即发送消息，可能早于 SDK 的 turn/start 返回。此前先返回 STEER
成功，再补发命令；如果此时原生 Turn 已结束，拒绝被计入原 Turn 的失败，根 Goal 正确
保守阻塞。提前回执错误不能通过允许失败回合进入 Goal 评估来掩盖。

## 数据结构与状态机

原 pending steer 文本增加 asyncio Future，仅记录原生命令确认；早到的发送方等待它。
私有 `_SteerNotAccepted` 仅表示精确 SDK 拒绝。`CodexHarness.send` 在该异常时委托
基类 FOLLOW_UP，基类继续独占 Turn 队列、ID、事件及状态；其它模式原样委托。

## 决策

原生成功后才返回 STEER；精确未接受时返回新 Turn 的 FOLLOW_UP。原 Turn 的唯一
reader 继续排空，不中断、不丢用量。等待期间取消的未派发命令跳过，SDK 启动失败与
stop 释放等待者。未知错误保留既有失败/退出确认路径，不自动重发。

## 拒绝的方案

不将本地缓存等同原生接受；不因明确未接受而终止已经正常完成的原 Turn；不对任意
HarnessStateError、网络错误或模糊字符串自动重发；不更改公共协议或另建 Turn 队列；
不放宽根 Goal 的 FINISHED 和已知用量要求。

## 验证

新确定性用例覆盖早/晚明确拒绝各产生一次后续 Turn、原 Turn 保持 FINISHED、无多余
interrupt；取消未派发命令、启动失败/stop 释放等待者；不明响应与错误 code 不重放。
既有早到 steer 用例改为断言确认前发送未完成；既有失败仍排空原 reader 的用例保留。
下游真实 Codex/OpenCode CLI 经受控本机模型重新注入 worker/reviewer 消息，完成原
Task/Review、根 Goal 双 attempt、全部用量、历史唯一性和真实 checkpoint 恢复。
首轮沙箱定向测试超时，本机隔离重跑通过；扩大组保留既有 SQLite 线程清理告警。

## 已知遗留

这是本地 dirty 候选验证，非锁安装/远端模型/渠道 UI/Cluster 发布验收。scheduled
临时 reviewer 仍未接同 Provider 构造与 Goal 生命周期；活动 Provider pause/resume
仍不支持。消息与完成竞态无法由有限次 E2E 穷尽，确定性测试固定两个关键接受窗口。
