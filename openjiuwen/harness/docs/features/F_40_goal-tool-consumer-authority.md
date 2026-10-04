# F_40 Goal 工具实际读取前的消费检查

| 项 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 范围 | 原 Native Tool invocation 与 GetCurrentGoalTool 的锁内读取 |
| 测试基线 | 原 AbilityManager、PermissionInterruptRail、GoalManager/SessionGoalStore；合成输入，无真实 Provider |
| Refs | leemine/codeswarm#33 |

## 背景

工具最终授权通过后，get_current_goal 仍可能等待 GoalManager 控制锁。等待期间
Goal 或资源权限改变，仅检查调用开始不能防止读取后继目标。拒绝不能被原工具的
宽异常处理翻译成“当前无 Goal”。

## 数据结构与决策

复用原 `_Invocation` 的私有 required/check/seal 字段，不增加持久记录或注册表。
原最终授权回调使用 `ToolInvocation._require_consumer_check()` 单向声明，并通过
`_bind_consumer_check(check)` 绑定同一同步闭包；同对象重复绑定允许，替换拒绝。
整个 callback 链结束必须已绑定；原 ToolExecution 固定 requirement/checker 身份。

GetCurrentGoalTool 从原方法证书捕获 consumer，等待原 GoalManager 锁前、锁内 load
前/复制后以及返回前复核。检查器负责原 Goal、Session、来源和当前资源权限；core
固定原 executor、Manager/store/锁/backing Session 引用（在首 checker 前捕获），检查前后重验原证书。失活和其它 Task 继承
的受管证书不得按 legacy 降级。checker 不得 await、审批或重入原 Goal 控制锁。

旧 Native policy callback 同样令 `_CALL.protected` 为真，因此不能据此强制新的
consumer。只有宿主显式声明的本次调用需要它；普通 policy allow 和直接工具调用
保留原行为。受管宿主必须把声明放在原最终准入链内；无法取得证书不能静默跳过。

## 拒绝的方案

- 按工具名字直接授予读取，或复制第二套 Goal 工具/记录。
- 给所有普通 Native policy callback 强加 managed requirement。
- 只在返回结果时检查，让实际读取已经发生；失活后返回 no_goal 假成功。
- 每次消费重新选择当前主体/目标或重新触发 UI 审批。

## 验证

23 项新增实际组件测试覆盖旧策略 allow、显式缺 checker 拒绝、原锁等待期间资源
撤销/Goal 更换/manager/store/锁替换及首 checker 重入替换 backing Session、callback 链和方法内改写、严格同步返回及
活跃/失活继承 Task。既有 Goal、最终授权、执行证书、MCP 证书、Native host 与
mandatory authority 回归共同验证。原 manifest 的 goal-control 目录自动收集新文件。
同一独立实际组件反例在正式基线可返回后继 Goal 文本，在本片拒绝；仅合成数据。
精确运行命令、结果和来源保存在交付证据目录，不把源码覆盖测试当锁安装验收。

## 已知遗留

本片仅提供私有消费接缝，宿主尚需绑定其真实来源和当前 ResourceGuard 检查，不能
单凭 core 测试声称组织 Goal 工具完整验收。submit_goal_report 原同步 sink 的宿主
映射为独立交付。本片没有运行真实 Provider 负向探针，不扩大完整活动恢复范围。
