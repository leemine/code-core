# F_30 原 Round 的 admission 与任务归属

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | ExecutionOrigin 私有 checker、原 Round 和提交任务、执行归属检查 |
| 测试基线 | 本轮隔离源码确定性组件验证；不调用真实 Provider |
| Refs | R2-B4 原 Turn 精确退出前置；未提供 issue 编号 |

## 背景与决策

F28 贯通 live 来源，但对象身份自身不能证明原 Turn 仍接受输入。原输入可在 readiness、
消息 handler、TaskManager 锁或 rail await 后迟到。使用原根同步 checker 检查原 PendingTurn
和宿主 entry/Binding/Session，不建立新的撤权状态、不从最新身份补权。

ExecutionOrigin 保留 host_value，末尾追加私有可选同步 checker，默认 None 保持旧行为；
repr 不暴露且不进入协议/Task/Session 持久化，复制仍保留原对象。原 Round、提交任务与 scheduler wrapper 的归属继续
复用原记录/队列；TaskManager 的公开查询是深复制，执行对象匹配不能比较两个公开副本。

## 拒绝的方案

不以空 task 查询/业务 terminal 当 Task 退出，不用 Session abort/cancel_all 回收单 Turn，
不因 timeout 通过 provider crash 分支提前 terminal，不引入旁路队列或第二份任务状态。

## 验证与已知遗留

本基础切片不对外启用凭据退出成功。Native 原根绑定、active-abort、原 Turn 终态门槛和
subagent 精确来源仍需配套闭合；真实 Provider 对抗不在本轮范围。未验证部分不得记为通过。

## 原对象与退出证据

- `ActiveInteractionRound` 在 facade 创建前发布，保存原 work、Session、controller、
  submission Task、scheduler wrapper 和 TaskManager receipt。首次 await 后不从 current 补权。
- TaskManager `_add_task_execution` 在原存储锁内生成 receipt，复用公开 `add_task` 的
  原 CRUD 实现；公开方法签名与返回行为不变。scheduler 在 F29 的原扫描临界区捕获
  dispatch receipt，保留在原 wrapper 上，无第二索引。
- wrapper 的词法 ContextVar scope 固定 manager/stored/source/Session。原 Task 被公开
  `update_task` 替换时，旧 receipt 的读写及 failure/completion 事件均拒绝；正常原地 status
  变化仍允许，pause/resume 的新派发取得新票据。继承 Context 的迟到任务不能把失活 scope
  当作未受管 legacy。`execute_task` 的公开 override 签名不变。
- 正常结果、后继 Round 入队、round boundary 和终态清理均等待原 submission/wrapper
  实际结束。取消仅对保存的原 wrapper 首次发 cancel，重复 caller cancel 不打断它的 finally。
  外层既有 stop 超时报 unknown，原 facade/record/producer 句柄继续保留。
- 未保存 wrapper 时，必须原 submission 已结束、原 manager 锁核对同一 stored receipt，
  并把尚未派发的 SUBMITTED 原记录改为 CANCELED、确认原 owned map 无该活动任务；
  WORKING 或归属不一致不判退出。F29 锁内复核阻止已扫描的旧 SUBMITTED 继续派发。
- 已保存 wrapper 的 actual done 是原任务退出事实，后继存储记录替换不会使原已退出任务
  永久占有，也不会取消新任务。不能证明时不发送终态、不晋升新 work，stop retry 检查同一
  原 record。没有新增任务队列、状态机或权限存储。

## 确定性验证

新增测试使用实际 DeepAgent Round、TaskLoopController、handler、TaskManager、scheduler、
executor；模型与传输 IO 使用合成替身。覆盖原 send 等待撤销、原存储插入后返回等待替换、
executor rail 等待撤销/替换、原 wrapper 输出尾部阻止 Round 完成、原 submission/任务取消
重试、同源多 follow-up、F29 已 scan 后 CAS 取消、词法 scope 失活与公开 pause/resume。

这是源码 overlay 的组件验证，不是锁安装发布配对、真实模型执行或真实 Provider 撤权验收。
Native expected PendingTurn → host entry 绑定、精确 active abort、原 subagent/control 退出和
凭据 watcher 尚须后续接线；未改变旧 `cancel_round` 为 whole-Session stop。
