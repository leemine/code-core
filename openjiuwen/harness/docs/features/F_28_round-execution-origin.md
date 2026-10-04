# F_28 Round 的原始执行来源贯通

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | RoundWorkItem、DeepAgent 原队列/执行、TaskLoop follow-up 批次 |
| 测试基线 | 隔离源码与固定依赖；确定性组件测试，不调用模型或真实 Provider |
| Refs | 本轮 R2-B4 原 Turn 归属基础切片；未提供 issue 编号 |

## 背景

长驻 supervisor 的 ambient 可能来自启动者。原 work 没有 live 来源时，实际 Round、rails、
控制器事件和 follow-up 不能证明属于哪个原执行。Session 持久 follow-up 字符串也不能保存
宿主的进程内权限。此变更只贯通已捕获来源，复用现有 ExecutionOrigin 和唯一队列。

## 数据结构与决策

- frozen RoundWorkItem 以私有 InitVar 存储原来源；不成为 dataclass 字段，不进入 asdict、
  JSON、inputs/context 或 Session state。copy/deepcopy/replace 保持来源 is；显式 None 清源。
- send_input 首次 await 前捕获来源，在实际入队处显式传递。Round 的词法 scope 覆盖真实
  run_one_round、rails/resume 与 terminal cleanup；None 显式遮蔽旧 ambient。
- suspended Goal resume 继承原 suspended work。steer 必须同源，不能以临时请求换根。
- 原 LoopQueues 的载体批次须全体同源，含 None；promote 不猜当前/最新来源。后继 work 和
  controller InputEvent 显式携带原来源；managed follow-up 不落持久字符串。
- 原停止/中断/Goal 不继续的受管待续批次可见报错并结束输出，只拒绝本批次，保留其他原
  EventManager 工作。旧 generator 同样不持久来源，yield 不向消费者泄漏 lexical scope。
- source-less legacy 保留原 drain→save→stop evaluator→reload 顺序；自定义 stop evaluator
  可以观察已保存状态。managed 也在 drain/save 后才评估，不为简化改变回调时序。
- 这里不增加身份供给、授权存储、调度器或公共 wire 字段。

## 拒绝的方案

不从 supervisor/current/last Work 推断来源，不在任务创建瞬间开 scope 后立即失活；不把
来源写进恢复数据；不为消除挂流自动执行原本停止的 follow-up；不从 Native 临时控制请求
猜原 Turn，不用 carrier 存在代替实际资源权限或任务退出证据。

## 验证

新增 `test_deep_agent_round_origin.py` 验证实际 send_input→queue→execute→InputEvent，
首次 await 捕获、scope 存活/遮蔽、Goal 原来源、同源/混源 promote、两条后继 work 路径、
旧 generator、停止后拒绝且保留其他队列、legacy None 和 steer。值对象测试实际覆盖
asdict/JSON 恢复、copy/deepcopy/replace/frozen；原交互断言仅显式增加 origin=None。
新增文件纳入原 stable manifest，不改变超时或历史白名单。

本地 strict 与受影响测试的准确命令、SHA、环境、结果保存在独立交付证据中；源码测试不
等于非 editable 安装、远端 CI 或下游锁定配对。未运行新的真实 Provider 对抗探针。

## 已知遗留

Native 宿主原 entry→ExecutionOrigin、same Turn plain resume/控制根、自动 Goal 再 ensure
来源及完整活动恢复尚未闭合。此切片不证明 active Turn 精确取消/退出或凭据撤销自动收尾。
受管停止后待续恢复当前明确拒绝，不能记作完整恢复通过；core/swarm 配对与真实验收独立。
