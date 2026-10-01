# Scoped member interactions and host projection

日期：2026-09-30

## 决策（编码前）

External 成员可选启用 team/member/session/cycle 交互地址；原 HarnessIOAdapter pending future 仍是唯一回复权威。Runner 从现有 leader/spawn handle 找到成员，精确校验地址和 pending 后才返回成功；拒绝 raw、多目标、过期和未知回答，不能降级为普通输入。旧 Native/CLI 未启用此选项时保持原行为。

成员可选注入只读事件 observer 与原单消费者输出映射，宿主使用已有 typed projection；不建立第二条 Provider cursor。产品输出写入原有有界 IO 输出队列。

## 拒绝的方案

不通过观察事件审批，不建立第二份成员 pending 表，不广播回复让所有成员猜 request_id。跨进程确认暂不伪造；宿主先限定 inprocess。

## 验证

core 影响面 437 passed（0 failed/error/skipped），覆盖相同原始 request_id 的成员隔离、重复/迟到/重启回复、取消与拒绝、原 Runner 定位和 Native 兼容。宿主影响面 830 passed；真实 Codex/OpenCode CLI + 自有 loopback 模型 2 passed，含成员工具调用、同 Session 恢复及退出。新源码及详细命令/哈希由管理仓 R1_11F_TEAM_CLUSTER_E2E 第 9 节归档。core 18 warnings：2 项 Pydantic 弃用、16 项 SQLite 后台线程 closed-loop；未修改基线 395 passed 也出现同类告警，不能视为零告警或扩大白名单。

## 恢复边界

scoped 交互和活跃产品子 Agent 在原成员 Session 写未确认标记；仅在本周期成功启动且 Provider、产品、MCP 全部确认退出后清除。冷恢复遇到未确认标记明确拒绝，不重建旧 approval future、不重放产品执行；失败启动的补偿不能抹掉原标记。

## 已知遗留

宿主仅接 inprocess；跨进程成员回复确认、真实远端模型/渠道/UI 与冷恢复活跃产品均未验收。活动 Provider 暂停不支持。全局 External Team 准入仍关闭。
