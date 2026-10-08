# F_42 Runtime authorization

## 元信息

日期：2026-10-08。范围：可选 HarnessRuntimeAuthorization、SerializedTurnHarness、OpenCode、Codex、IO Adapter。测试基线：Python 3.13，固定真实 OpenCode 1.18.18 / Codex 0.144.4，隔离用户目录与回环模型。Refs：PR #57（用户允许本次以 PR 编号替代 issue）。

## 背景

把可变化的宿主工具权限当作不可变构造身份，会使合法的权限切换变成配置漂移，旧 Session 无法继续。

## 数据结构与状态机

新增可选能力 RUNTIME_AUTHORIZATION 及 update_authorization。原 Binding、构造配置与检查点身份不变。新不可变授权快照在原 command lock、supervisor 的 Turn 链空闲边界应用；确认失败或取消时拒绝新输入。无第二条队列、状态机或事件消费者。

## 决策

OpenCode 固定版本的 PATCH 追加规则，必须核对完整前缀和回读结果。“会话内记住”的缓存优先于 ask，故改变已应用规则时确认本任务拥有的服务退出，然后恢复同一原生 Session。受管路径强制每次经过宿主授权，不允许原生 always 缓存，保持原治理门禁。

Codex 确认原进程退出，在相同 thread 上重新握手并核验 sandbox、approval、MCP。不会重放用户输入。IO 只重评估待处理工具审批，不回答普通用户问题；精确交互关闭回执沿原输出路径发送。权限生效状态由宿主回读确认后展示。

## 拒绝的方案

不删除配置指纹、不修改已有 Binding、不切换 Provider、不创建新会话掩盖失败、不把设置保存视为生效；不单独 PATCH OpenCode 后忽略 remembered grants。

## 验证

核心受影响单测及固定真实 CLI 三项测试通过；双向切换保留原生 Session/thread，收紧后再次审批，进程退出有确认。Web 与产品恢复验证由 swarm/管理仓证据承载。使用本地源码联合验证，不代表锁定依赖 CI 已通过。

## 已知遗留

本次产品接线为 Single 及其产品子 Agent；其他 Provider 和组织 Team 不声明新能力。不改变 mandatory resource authorization。发布前仍需核心合入、swarm 固定新 SHA/uv.lock 与干净 CI 来源验收。
