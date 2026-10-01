# External 成员退出确认与严格恢复

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-30 |
| 范围 | `external/member_runtime.py`；R1-11F 的成员生命周期前置接缝 |
| 测试基线 | 成员运行时 29 passed；core 影响面 216 passed；swarm 影响面 114 passed / 2 既有 skipped |

## 背景

公共 IO adapter 已保留停止失败后的资源，但 Team wrapper 在 finally 中标记 stopped 并释放成员
Session，导致下一次 stop 不再进入 Provider，也可能用新的成员 Session 覆盖旧 checkpoint。
半启动时 adapter 的退出补偿失败同样被 Team wrapper 当作无资源处理。Team 全 Provider
接入之前需要让同一成员生命周期保留这些失败，不能把过程结束作为退出成功。

## 决策

- 复用原 IO adapter 的唯一事件消费与停止重试，不增加 Provider Turn 状态机。
- 一次周期只有 Provider、teardown hooks、成员 Session 全部收敛后才 stopped；失败保留句柄，禁止 start。
- Provider 退出之前不释放可能仍被事件消费使用的成员 Session/观测资源。之后逐项运行 hooks，
  成功项记账，失败项聚合抛出；重试不重复成功项，post_run 失败也保留原 Session。
- 半启动失败通过同一 stop 路径补偿；补偿再次失败仍可由宿主重复 stop。
- 严格恢复复用 `HarnessContext.resume_policy=REQUIRE_RESUME`，缺失 checkpoint 在启动前拒绝；
  原 CLI `resume_external_backend=True` 的缺失新建兼容保留。

## 拒绝的方案

- 不在 finally 中盲目释放资源，否则丢失重试能力和 checkpoint sink 的所有者。
- 不吞掉 teardown hook 异常，否则 stop 成功无法证明完整退出。
- 不全局删除旧 CLI 恢复兼容，也不再新增一个 strict 布尔参数；现有 ResumePolicy 已表达宿主要求。

## 验证

本地源码候选验证：成员运行时从 18 项增至 29 项，全通过；external 目录、IO adapter 和基类
合计 216 passed。Swarm Surface/External 路由/Team 配置组合 114 passed、2 既有 skipped。
覆盖退出失败/取消重试、半启动、hook 精确重试、pre_run/post_run 失败、严格恢复与旧兼容。
Ruff 稳定选择集和 diff check 通过。

沙箱内 SQLite fixture 线程等待超时在未修改正式基线同样复现；同一隔离探针在沙箱外立即通过，
上述扩大组合在沙箱外有界重跑通过。保留超时记录，不增加白名单。本地显式源码配对不是锁定 CI。
未调用真实远端模型，未验证产品 Team 六格。

## 已知遗留

这是 R1-11F 的前置修复，不代表全角色 Provider 构造、Team 工具准入、Surface 继承或真实
Native/Codex/OpenCode × Work/Code 六格完成；产品 External Team 准入仍保持关闭。
