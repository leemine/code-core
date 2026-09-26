# F_10 Provider-neutral Goal 驱动

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-26 |
| 范围 | R1-09A；Goal 宿主端口、attempt 结算与 Native 兼容 |
| 测试基线 | Goal 与 TaskCompletion 定向确定性测试；验证结果见下 |
| Refs | R1-09A（本轮按用户授权使用任务编号） |

## 背景

GoalManager 已是唯一状态写者，但其构造、续排和通知直接依赖 Native EventManager
及输出消费者。公共宿主需要保留目标语义，又能委托自己的 Session Runtime。

## 数据结构与控制

沿用 GoalRecord、SessionGoalStore 与四种状态。新增可选持久字段
`last_assessed_attempt`（缺省 0），记录已结算 attempt；不新增执行 registry。
新驱动完成入口必须提供 `attempt_index`，以当前 goal ID、revision、attempt_count
和结算序号拒绝迟到或重复结果。旧构造和无 attempt_index 的 manager 方法继续支持。

## 决策

- GoalExecutionPort 是 harness/goal 内的宿主领域接口，不扩展 Provider SPI。
  宿主实现准入、唯一工作请求、取消、在途检查和状态通知；Manager 不持有第二队列。
- NativeGoalExecutionAdapter 将既有 EventManager、输出附着和 InteractionEvent
  包装为同一端口。旧 GoalManager 构造自动创建该适配，Native 行为保持。
- GoalAttemptDriver 复用 GoalEvaluator 与 Manager 结算。Native rail 负责提取报告、
  transcript 和模型用量，完成决策委托同一驱动；不增加后台循环或事件消费者。
- pause 停止后续 attempt，在途仍可完成或阻塞；流关闭、取消和待答不是完成证据。
  预算/次数保持 assessment 边界的现有强度，不承诺跨 Provider 的模型调用前拦截。
  非结算 outcome 显式携带 usage 时拒绝，宿主须先经去重后的原 usage 通道计量，
  不将取消时已发生的用量静默丢弃。
- 冷恢复仅加载 GoalRecord；原宿主必须确认执行所有权和可续接性，才请求下一次执行。
  core 不持有 Provider generation/checkpoint，宿主仍校验这些关联及 usage 事件去重。

## 拒绝的方案

不复制 GoalManager、状态存储、Session Runtime 或 Provider 队列；不让 Heartbeat
恢复/推进目标；不新增 Native 与公共双驱动；不以流结束或普通文本推断目标完成。

## 验证

正式 develop 基线的 Goal 与 TaskCompletion 定向集为 84 passed，0 skipped。
实现后 Goal（含新增 21 项）、TaskCompletion、DeepAgent interaction 与 rail 路由合计
142 passed，0 failed/error/skipped，3 条既有弃用 warning。新增 Native 真实 Manager/rail
接缝验证连续 attempt 单次结算，以及 interrupt 后 pause 收尾不丢原 attempt。命令：

```bash
PYTHONPATH=/tmp/codeswarm-r1-09a-core timeout -k 5 120 \
  /home/leewanlong/work_leewanlong/code-projects/codeswarm/.venv/bin/python -m pytest \
  -o addopts= -q tests/unit_tests/harness/goal \
  tests/unit_tests/harness/test_task_completion_extensions.py \
  tests/unit_tests/harness/test_deep_agent_interaction.py \
  tests/unit_tests/harness/test_deep_agent_rail_event_routing.py --timeout=30 \
  --junitxml=/tmp/r1-09a-evidence/core-goal-premerge.xml
```

显式 PYTHONPATH 指向隔离 core 工作树，Python 3.13.15；日志与来源在
`/tmp/r1-09a-evidence/`。公共新入口、旧 Native 构造、暂停收尾、重复/旧 attempt、
替换/clear、重建去重、预算停止、interrupt/cancel/流关闭均有确定性覆盖。
新增文件经 collect-only 收集 19 项；正式 full-python 使用 tests 全量发现，会收集该目录。
现有 pr-stable 是固定 suite，未覆盖此 Goal 专项；本轮不修改 CI 切片。
Ruff 定向检查与 git diff --check 均通过。合入前已执行精确暂存文件的 make check：
import-order、Ruff lint、codespell 通过；格式检查仍报告 6 个已有文件，Pylint 保留
34 条基线诊断。已对正式 develop 原文件复查，未新增失败白名单；没有按检查触发
历史文件整篇格式化。Makefile 忽略子检查退出码，因此不将 make 返回 0 写成全绿。
证据在 `make-check-final.log`、`baseline-format.log` 与 `baseline-pylint.json`。

跨仓 Native Goal adapter/history/defer 同一 55 项在原 core 和本切片均通过，
证据 `/tmp/r1-08-09-evidence/swarm-goal-new-core-host.log`。以上均为本地联调，
不是 swarm uv.lock 干净安装或正式配对验收，也没有真实 Provider 模型调用。

合入前另以 core 自身 `uv sync --locked --group test --extra cli --python 3.13`
安装的新 `.venv`，不设置 PYTHONPATH 覆盖，复跑同一组 142 passed、0 failed/error/skipped，
6 条依赖弃用/语法 warning。来源与结果分别在 `locked-source.txt` 和
`core-goal-locked.log` / `core-goal-locked.xml`；这是本地 core 锁环境验证，
仍不代替正式远端 full-python 或 swarm 新锁验收。

## 已知遗留

R1-09B 才接 External 产品控制、历史/UI、Heartbeat 联动和真实 Provider/渠道；
Team 根目标真实执行依赖后续 Team 公共执行切片。正式合入顺序为 R1-08 swarm →
R1-09A core → 基于新配对的 R1-09B swarm；锁升级和正式双仓 full-python 门禁独立记录。
