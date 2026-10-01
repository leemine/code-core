# Scheduling — 调度模式决策引擎（F_62）

leader 侧的调度分发 runtime，与 `coordination/`（唤醒层）平齐：coordination 只唤醒、不决策；本包只决策、不直接触碰其他成员的 round——所有成员交接都是 **leader 身份的邮箱消息**。仅在 `spec.dispatch_mode == "scheduled"`（静态配置）的 leader kernel 上构造，团队建成后激活。

## 文件地图

| 文件 | 职责 |
|---|---|
| `scheduler.py` | `TeamScheduler`：事件粗筛 + 双幂等扫描（开工 / 验票）+ `SchedulerHost` 窄协议 + 已挂载 `TeamSkillEvolutionRail` 的 reviewer feedback 旁路（F_73） |
| `review_feedback_evolution.py` | `ReviewFeedbackEvolutionCoordinator`：与产品无关的逐 Task Feedback 归因、团队终态汇总演进与新 Skill 候选路由；由 `TeamSkillEvolutionRail` 持有，复用其标准 host-event 队列 |
| `verdict.py` | 纯函数投票判定（`settle_review_tally`：二元票池（verifier+challenger）一票否决 + 检视者分数池平均≥0.85）——策略可整体替换，不碰票据存储与状态机 |
| `render.py` | 两类收件人两套机制：**成员**交接只组投递载荷 `meta_*`（`{template, refs, params}`，`content=""`），文案在 `prompts/<lang>/scheduler_*.md`、投递时渲染（F_63）；**leader** 摘要/升级走 `deliver_input` 直投，仍是 `i18n.py` 的 `scheduler.leader_*` 一行短串 |

## 核心不变量

1. **调度器不理解事件，只理解看板。** 任何触发（task/member transport 事件、`POLL_TASK`、`SCHEDULER_SCAN` 回声、激活）都跑同一对幂等扫描；恢复 = 激活扫描本身，无独立恢复路径。事件仅有两个例外职责：终态摘要与 `TASK_LIST_DRAINED` 收尾（事件只发一次，扫描推不出"刚刚发生"）。
2. **交接 = leader 身份邮箱消息，投递即启动。** `_send_as_leader` 先落邮箱行（持久，离线成员首次 sweep 补投）再 `host.auto_start_member`（复用 `UNSTARTED→STARTING` CAS，幂等）。成员在线与否**不是**开工前置条件，调度器不做在线过滤。
3. **发送存意图，投递才成文（F_63）。** 邮箱行的 `content` 恒空，`meta` 带模板 key + `refs`；文案由 `message_template.expand_message` 在**投递时刻**按收件人语言渲染，`{{task.*}}` 取的是**当时**的任务行——所以队列里躺了很久的交接不会投出过期的任务简报，leader 中途 `update_task` 也立刻对未投递消息生效。能表查的一律进 `refs` 现查；`params` 只放表答不出的瞬时值（某轮 fail feedback 聚合、解析后的轮数上限）。**不要**为了"省一次查询"把任务正文快照进消息。
4. **`SchedulerHost.deliver_input` 只准注入 leader 自身**（终态摘要 / 升级 / 收尾），对成员永不直投。
5. **判定在本包，票据在 DB。** `verify_task`（scheduled 团队）只追加票行；`verdict.settle_review_tally` 三值判定（二元票池一票否决 + inspector 分数平均）；settle 经 `task_manager.settle_review` 的 `IN_REVIEW` 源态 CAS——单判定者 + CAS 保证不双结算。
6. **轮数升级与停摆升级共用一条注入路径**，按 `(task_id, review_round)` 去重；升级后任务留 `IN_REVIEW`，决定性迟票仍可正常 settle（不再重复升级）。
7. **leader 自发事件的可见性靠 `SCHEDULER_SCAN` 回声**：kernel 的 `_filter_self` 丢弃 self 事件时，若调度器激活且是 `task_*` 事件，改投一个 `SCHEDULER_SCAN` inner 事件——coordination 无 handler 监听它，调度器把它当纯扫描提示。
8. **异常语义与 coordination 对齐**：`on_event` 吞普通异常（log + 下次触发重试），绝不让 bus loop 挂掉。
9. **review feedback 是可选旁路**（F_73）：失败轮次 settle 后，scheduler 从 host harness 查找已挂载且
   开启该能力的 `TeamSkillEvolutionRail`，后台调用其窄入口，不阻塞返工；全部任务终态时先等待已启动
   callback，再调用同一 Rail 的终态汇总入口。callback 按 `(task_id, review_round)` 进程内去重、
   异常只记录。scheduler 不理解 LLM、轨迹、Skill 归因、审批或持久化，也不自行构造 Rail。

## 生命周期（kernel 接线）

- `CoordinationKernel.setup` 仅在 `spec.dispatch_mode == "scheduled"` 的 leader 上构造（休眠态）；其余 kernel 不构造。
- 激活点两个：`notify_team_built()`（build_team 成功回调，先于任何 spawn / create_task）与 `kernel.start()`（team 行已存在——warm resume / 冷恢复）。
- wake 路径：`kernel._build_wake_callback()` 组合 "coordination dispatch → scheduler.on_event"。
- `pause()` / `stop()` 先 `deactivate()`，再等待 `stop_reviewers()` 确认临时 reviewer 退出，才能释放 Session/DB/transport；超时或清理失败保留原所有者供重试。恢复扫描只派发缺票 reviewer，board 真相仍在 DB。
- task review feedback callback 不进入状态机关键路径；团队终态 callback 是旁路汇总边界，不改变
  all-done 摘要与 pause/stop 决策。

## 配置消费

`default_max_review_rounds`（默认 3）、`review_stall_timeout`（默认 1800s）只在本包消费，不跨进程镜像。催办间隔 `_REVIEW_RENUDGE_SECONDS`（600s）是包内常量。

设计上下文见 `docs/features/F_62_scheduled-dispatch-runtime-and-review-voting.md`（调度器 + 投票）、
`docs/features/F_63_scheduler-message-templating-and-delivery-render.md`（交接消息的两阶段渲染）与
`docs/features/F_73_reviewer-feedback-skill-evolution-boundaries.md`（review feedback 外部接入边界）及
`docs/specs/S_22_scheduling-runtime.md`。

### 临时 reviewer 构造与所有权（F_116）

原 `_spawn_temp_reviewer` 持有一次性执行，区别于 roster 成员与产品子 Agent。Native 沿原
TeamHarness.build/run_once；显式 External 从同一个成员 factory 的 build_review_runtime
接缝构造，核对 Provider 与绑定 Team Session，缺端口拒绝，不能降级 Native。请求携带
原 verify/view 工具与原模板 prompt，票据/settle 仍归原 TaskManager。

原 scheduler 按 task/review_round/reviewer 跟踪 task/runtime/cleanup；清理任务受 shield
保护，停止等待者取消不取消实际清理。确定性 PASS/FAIL 也必须等待本轮所有临时执行退出；
未决票据仍沿原催办/升级。成功退出后使用同一幂等 scan；不新增调度器或 Provider 事件消费者。
External 不沿 Native 的 181001 字符串规则重试未知执行。宿主仍须实现临时执行的授权、
单消费者历史/交互/用量及持久未知状态保护；core 接缝通过不等于产品 scheduled 已准入。

临时审批寻址只读取 _review_runs 中 invocation 对应 runtime；停止期间拒绝查询，退出
移除后不可回答（F_117）。宿主负责持久未知状态和历史/用量确认，不把产品准入决定
搬入 scheduler。

External reviewer 的 output_sink 由原 scheduler 绑定当轮 stream_controller.stream_queue
和 Team Session，宿主写入带角色/临时身份的既有 TeamOutputSchema；换队列/Session 后
拒绝旧输出。Team Session 的持久化流不能代替 Runner 消费队列，不增加第二个消费者。
