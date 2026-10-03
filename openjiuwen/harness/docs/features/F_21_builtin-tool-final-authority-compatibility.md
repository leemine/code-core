# F_21 内置工具最终授权兼容

- 日期：2026-10-04
- 范围：AgentModeRail、EnterPlanModeTool、ExitPlanModeTool、TeamTool 日志
- 关联 spec：S_05；Team S_08
- Refs：#27

## 背景与决策

F_20 拒绝构造之后替换 invoke，但既有计划模式 rail 用这种包装追加说明，Team 工厂
也用这种包装记录日志。恒 True 授权的真实 AbilityManager 调用会在执行前被拒绝。

计划工具新增可选 keyword-only instructions / notification；rail 在构造时注入，
工具原方法追加原来的两个换行与文本。创建计划、既有计划、空计划和完成计划的原
状态变化与返回不变。TeamTool 将日志装配前移至子类定义，工厂只开实例开关。
因此构造时登记的 bound method 仍与 type(tool).invoke 一致。

## 拒绝的方案

不放宽 invoke 身份校验，不对 wrapper 建白名单，不提供重新登记任意包装器的逃逸口。
不删除 Team 的起止 debug 诊断，也不把输出说明变成第二轮用户确认。

## 验证与限制

真实内置计划工具经 AbilityManager 验证有/无强制授权时逐字等价的 suffix、拒绝零
状态访问；真实 Team 工厂生成工具验证恒 True 执行、False 拒绝、日志与结构化返回。
原未知包装器拒绝用例继续保留；新增取消期间 proof 失效、继承子任务拒绝、后续
独立旧调用兼容用例。对应 tests 加入 stable discovery/execution。
完整 UI/Provider、Single/Team 和下游锁定配对仍须由集成批次验收。
