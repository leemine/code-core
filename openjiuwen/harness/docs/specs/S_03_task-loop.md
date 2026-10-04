# S_03 TaskLoop 事件体系

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness/task_loop/`（8 个模块）、`openjiuwen/harness/schema/loop_event.py`、`openjiuwen/harness/schema/stop_condition.py`、`openjiuwen/harness/schema/task.py` |
| 最近一次修订日期 | 2026-10-04 |
| 关联 feature | `F_36_owned-steer-admission.md`、`F_32_native-exact-turn-exit.md`、`F_30_owned-round-admission-fence.md`、`F_24_owned-task-stop-confirmation.md`、`F_26_scheduler-owned-exit-capture.md`、`F_28_round-execution-origin.md`、`F_29_scheduler-submitted-admission.md` |

## 范围 / 边界

本规约定义 DeepAgent 的 task loop：事件模型、事件消费组件（handler / executor）、
循环协调者（controller / coordinator / queues）。它是 `S_02` 的 supervisor 的执行面。

具体覆盖：

- `schema/loop_event.py` 的事件类型（`DeepLoopEventType`）、事件结构（`DeepLoopEvent`）、
  优先级表（`_EVENT_PRIORITY_MAP`）。
- `task_loop/__init__.py` 的懒加载导出面（`DEEP_TASK_TYPE`、`TaskLoopEventExecutor`、
  `build_deep_executor`、`TaskLoopEventHandler`、`LoopCoordinator`、`LoopQueues`、
  `TaskLoopController`）。
- `TaskLoopEventExecutor`（TaskExecutor 子类，将能力绑定到 controller 任务）。
- `TaskLoopEventHandler`（EventHandler 子类，消费 task_completion / task_failed /
  task_interaction / follow_up 事件并回写 work）。
- `LoopCoordinator`（轮次计数、abort、token 记账、stop 条件求值）。
- `LoopQueues`（steering / follow_up 队列）。
- `TaskLoopController`（core Controller 子类，round 提交 / 等待 / 队列）。

不在本规约范围内：
- 事件**如何被 supervisor 消费**、`EventManager` 工作队列 —— `S_02`。
- `DeepAgentState` 字段 —— `S_02`；`TaskPlan` / `TodoItem` 字段 —— `S_05`。
- stop-condition 求值的 evaluator 定义 —— 本 spec（事件/停止条件落地）；`DeepAgentState.stop_condition_state` 承载 —— `S_02`。

## 不变量

1. `task_loop/__init__.py` 的 `__all__` 固定为 7 个符号：`DEEP_TASK_TYPE`、
   `TaskLoopEventExecutor`、`build_deep_executor`、`TaskLoopEventHandler`、`LoopCoordinator`、
   `LoopQueues`、`TaskLoopController`；模块顶层懒加载同理于 `S_01` 不变量 2。
2. `DEEP_TASK_TYPE = "deep_agent_task"` 是 DeepAgent 任务在 controller 里的类型标识；
   `build_deep_executor()` 返回绑定 DeepAgent 的 `TaskLoopEventExecutor`。
3. 事件类型全集只有三个：`DeepLoopEventType.FOLLOWUP` / `STEER` / `ABORT`；
   `_EVENT_PRIORITY_MAP` 给每类一个固定优先级。**新增事件类型 = 改 `loop_event.py` +
   handler 对应分支 + 本 spec**。
4. `TaskLoopEventHandler` 是 `EventHandler` 子类，是 task loop 的**唯一事件消费面**：
   `handle_input` / `handle_task_interaction` / `handle_task_completion` /
   `handle_task_failed` / `handle_follow_up` 五个 handler 一一对应事件环；任一事件的
   消费结果经 `_resolve_future` 写回等待方。
5. handler 必须响应 `prepare_round()`（返回 round id）与 `wait_completion()`；
   `_cancel_timed_out_round(round_id)` 在 round 超时时取消。`set_session_toolkit(toolkit)`
   供 session 工具（`SessionToolkit`）注入。
6. `LoopCoordinator` 的停止条件：`should_continue()` 汇总 iteration 上限 / token 预算 /
   abort 标志 / 完成承诺；`request_abort()` 置位；`increment_iteration()` 每次 round 递增；
   `add_token_usage(tokens)` 记账。`get_completion_promise_evaluator()` 返回
   completion-promise 求值器（`schema/stop_condition.py` 的 `CompletionPromiseEvaluator`）。
7. `LoopQueues` 只有两个队列：steering（`push_steer` / `drain_steering`）与 follow_up
   （`push_follow_up` / `drain_follow_up` / `has_follow_up`）。
8. `TaskLoopController` 是 core `Controller` 子类：`submit_round` / `wait_round_completion`
   （round 生命周期），`enqueue_steer` / `enqueue_follow_up` / `drain_follow_up` /
   `has_follow_up`。controller 的 follow_up / steer 队列与 `LoopQueues` **以 controller
   侧为准**（`_get_interaction_queues` 桥接两处）。
9. `session_spawn_executor.py` 的 `SESSION_SPAWN_TASK_TYPE = "session_spawn_task"` 是
   `S_05` session 工具的独立任务类型，**不**复用 `DEEP_TASK_TYPE`。
10. 事件对象 `DeepLoopEvent`（`@dataclass`，带 `compare=False` 字段）：`priority` + `seq`
    排序、`event_id` 唯一（uuid）、`created_at` 单调时钟；类型全集只有
    `FOLLOWUP` / `STEER` / `ABORT` 三型；`create_loop_event(...)` /
    `default_event_priority(...)` 是构造与查表的唯一入口。

11. `TaskLoopController.stop()` 复用 core `Controller.stop()`：先停止 TaskScheduler，
    确认其原始执行 Tasks 退出后再关闭 EventQueue。调度器保留从 create_task 到 done 的
    生命周期引用；运行任务列表为保持事件顺序而提前摘除时，尾部事件 IO 仍属于该引用。
    停止以内部五秒预算等待，不持调度锁跨 join；超时/调用方取消保留原引用，确认前拒绝 start。
    并发 stop 不重复取消清理中的任务；owned task 内部自停被拒，后续外部停止仍可取消它。
    已退出任务的异常须报告，但报告后不永久持有已完成引用；允许后续 stop 结清其余控制器清理。
12. Scheduler 原 owned execution 记录按 task_id 固定 wrapper Task 至 done，包含从 running
    摘除后的终态事件尾部。私有 `_capture_owned_execution` 不取消/等待任务，expected Task
    或 running 映射不一致必须拒绝；ID 复用不能替换尚未退出的 wrapper，晚到 done 回调
    只能清理同一对象。原调用者持 Task 检查退出，不以业务 terminal 状态替代实际 done。
    不全局扫描 asyncio tasks，不创建另一任务队列或调度状态机。

13. Scheduler 扫描只通过 TaskManager 私有接口捕获原 SUBMITTED Task；等待原调度锁后，
    对当前同 ID 原对象及状态、running/owned/容量和 Session 映射作无 await 复核再创建。
    被取消/删除/替换的旧扫描不调度；新扫描仍允许正常替换对象。公开任务查询保持深拷贝。
    此准入复核不等于原 wrapper 退出确认，也不扩大公开 cancel 语义。

## 接口契约

```python
# task_loop/__init__.py（懒加载）
__all__ = ["DEEP_TASK_TYPE", "TaskLoopEventExecutor", "build_deep_executor",
           "TaskLoopEventHandler", "LoopCoordinator", "LoopQueues", "TaskLoopController"]

class TaskLoopEventExecutor(TaskExecutor):
    async def execute_ability(self, ...) -> None
    async def can_pause(self) -> bool
    async def pause(self) -> None
    async def can_cancel(self) -> bool
    async def cancel(self) -> None

class TaskLoopEventHandler(EventHandler):
    def prepare_round(self) -> int
    async def wait_completion(self, ...) -> Any
    async def handle_input(self, event: EventHandlerInput) -> None
    async def handle_task_interaction(self, ...) -> None
    async def handle_task_completion(self, ...) -> None
    async def handle_task_failed(self, ...) -> None
    async def handle_follow_up(self, ...) -> None
    async def on_abort(self) -> None

class LoopCoordinator:
    def current_iteration(self) -> int
    def is_aborted(self) -> bool
    def stop_reason(self) -> Optional[str]
    def reset(self) -> None
    def increment_iteration(self) -> None
    def add_token_usage(self, tokens: int) -> None
    def set_last_result(self, result: Any) -> None
    def request_abort(self) -> None
    def should_continue(self) -> bool
    def get_completion_promise_evaluator(self) -> CompletionPromiseEvaluator
    def get_state(self) -> Dict[str, Any]
    def load_state(self, state: Dict[str, Any]) -> None

class LoopQueues:
    def push_steer(self, msg: str) -> None
    def push_follow_up(self, msg: str) -> None
    def has_follow_up(self) -> bool
    def drain_steering(self) -> List[str]
    def drain_follow_up(self) -> List[str]

class TaskLoopController(Controller):
    async def submit_round(self, ...) -> None
    async def wait_round_completion(self, ...) -> Any
    def enqueue_steer(self, msg: str) -> None
    def enqueue_follow_up(self, msg: str) -> None
    def has_follow_up(self) -> bool
    def drain_follow_up(self) -> List[str]
```

错误 / 返回语义：

- `can_pause` / `can_cancel` → 布尔；`pause` / `cancel` 幂等。
- `wait_completion` 返回 round 结果；`_cancel_timed_out_round` 取消后返回
  `_error_result(error, output)`（`{"error": ..., "output": ...}`）。
- `LoopCoordinator.get_state()` / `load_state()` 支持 checkpoint 恢复（冷恢复续跑，见 `S_02` 的 `DeepAgentState.stop_condition_state`）。

## 数据结构

### DeepLoopEvent

| 字段 | 设置时机 | 清空时机 | 备注 |
|---|---|---|---|
| `priority` | 创建时（查 `_EVENT_PRIORITY_MAP`） | 不清 | 排序键，越高越先 |
| `seq` | 创建时 | 不清 | 同优先级内递增 |
| `created_at` | 创建时（`time.monotonic`） | 不清 | 比较排除 |
| `event_id` | 创建时（uuid） | 不清 | 唯一 |
| `event_type` | 创建时 | 不清 | `FOLLOWUP` / `STEER` / `ABORT` |
| `content` | 创建时 | 不清 | 事件载荷文本 |
| `task_id` / `metadata` | 创建时（可选） | 不清 | 关联任务 / 附加元数据 |

### 事件类型 → 优先级表

| `DeepLoopEventType` | 语义 | 优先级 |
|---|---|---|
| `FOLLOWUP` | 追加对话轮次 | `_EVENT_PRIORITY_MAP` 中最低 |
| `STEER` | 中断当前轮次注入指令 | 中 |
| `ABORT` | 整体中止 | 最高 |



### StopConditionEvaluator 族（`schema/stop_condition.py`）

| 求值器 | 语义 |
|---|---|
| `MaxRoundsEvaluator` | 轮次上限 |
| `TokenBudgetEvaluator` | token 预算 |
| `TimeoutEvaluator` | 超时 |
| `CompletionPromiseEvaluator` | completion-promise（`LoopCoordinator` 经 `get_completion_promise_evaluator` 使用） |
| `CustomPredicateEvaluator` | 自定义谓词 |

`StopConditionEvaluator` 是 ABC，`StopEvaluationContext` 是求值上下文。

## 与其它 spec 的关系

- 事件如何进入 `EventManager`、supervisor 如何 `run_one_round` —— `S_02`。
- `DeepAgentState.stop_condition_state` 字段 —— `S_02`；`TaskPlan` / `TodoItem` 模型 —— `S_05`。
- `LoopCoordinator` 的 stop 条件求值调用 `schema/stop_condition.py` 的
  `StopConditionEvaluator` 族 —— 本 spec（停止条件落地）。
- `handle_task_completion` 中 `TaskCompletionRail` 的 goal 评估钩子 —— `S_04` / `S_11`。
- `SESSION_SPAWN_TASK_TYPE` 的消费方 —— `S_05`（session 工具）。

## Live execution origin

`TaskLoopController.submit_round(..., origin=...)` 将原宿主 `ExecutionOrigin` 放入 InputEvent 私有来源；省略时捕获当前词法 scope，None 明确遮蔽。原 Task.inputs 保存对象身份，TaskLoopEventExecutor 据此安装 scope，TaskScheduler 的完成/失败/交互事件沿原任务来源返回。混合来源拒绝，恢复任务没有来源，不从当前调用者补齐。原 LoopQueues 允许同来源 live envelope，drain 在返回文本前比较原来源；AgentCallbackContext 使用同一队列与相同比较，不新增队列。来源不进入 JSON，不代表权限；授权仍由宿主负责。词法 scope 不跨 async-generator yield。见 F_23_live-execution-origin。

## 已捕获 follow-up 批次来源

`LoopQueues.drain_sourced_follow_up()` 只排空原队列当前批次并返回 `(origin, messages)`；
全部来源须对象身份一致，包含 None。混合批次拒绝，不能借当前 Round/supervisor 的来源。
`TaskLoopController` 提供同名窄转发口；原 expected-origin drain 继续保留。

交互 supervisor 从该批次创建带原来源的 work，`submit_round` 显式传入来源。受管路径
不把 follow-up 转为 `state.pending_follow_ups` 字符串；正常继续时剩余消息留在原内存队列，
无新队列或调度器。原已停止/中断/Goal/aborted 分支不自动执行待续消息；该批次明确拒绝，
向原输出发送 execution.error 并结束输出，不误清其他 EventManager work。混合来源同样
可见失败，不能进入 IDLE 后让消费者挂起。

旧 `_run_task_loop` 的受管路径遵守同样的 live-only 边界，停止后仍有待续批次时明确报错并
执行原 cleanup；generator yield 不持有新的词法 scope。source-less legacy 保留原字符串
持久化语义，不能在恢复时借用新来源。

### 原 dispatch receipt

TaskManager 私有插入端口在原存储锁内返回 original stored receipt；公开查询仍为深复制。
F29 原扫描锁内捕获 receipt 后，wrapper 词法 scope 固定 manager、stored、source 和 Session。
执行器及状态/事件写入用原锁下 CAS；过期继承 scope 与被替换记录拒绝，不能按 ID 查询最新
记录重建 authority。公开 execute_task override 签名不变，合法 status 原地变化及 pause/resume
新派发保留。原 owned Task Map 仍仅在 actual done 后释放，无第二队列或任务状态。

原 Round 只等待保存的 submission/wrapper；未派发证明须 submission done、原存储对象与
非可派发状态及 owned Map 联合成立。单纯 None、业务 COMPLETED 或响应 Future 完成均不
证明退出。未知事实保留原 record，阻止终态与后继派发；确认退出的旧 wrapper 不影响新对象。

## 精确来源取消与输出尾部

EventManager 的私有 `_capture_origin_work` / `_discard_captured_work` 只按原对象身份
处理已捕获 work；LoopQueues `_discard_origin` 仅移除同原 live origin 的输入，保留
其他来源顺序及队列 unfinished 计数。它们不创建队列，也不从 latest/current 推断来源。
上层必须先关闭该来源的 admission 并 join 正在提交的原 Task，再捕获这些队列。

Round 的原 wrapper 尾部及 forwarded marker 属于完成条件。托管 Native 超时不会
清除这些对象或推动后继 Turn；普通 legacy cancel_round 的既有合同保持。


## 原 Round 的补充输入

私有 `DeepAgent._send_owned_steer(expected, request, *, check_current)` 仅将文本放入
原 Round 的原 controller steering 队列。它沿现有 send lock → control lock 顺序，
在最终入队前检查原 Round/Session/controller/facade/source、非waiting状态及宿主本次
临时输入的同步权限 checker；原 source 身份不变。输入ID/文本/STEER模式在等待期间
变化必须拒绝。没有原活动 Round、已经完成或发生替换时不退回新 Round/follow-up。
此端口不创建输出消费者、Turn或资源授权，不从上下文重建宿主身份；普通send_input兼容
行为保持。宿主仍须选择并保留原Turn、原entry与本次输入凭据，不以该端口替代宿主授权。
