# F38 显式 idle Goal 新准入

| 元信息 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 范围 | GoalManager 私有 readmission、Native pre-attach、原输出 lease |
| 测试基线 | 实际 Manager/EventManager/Native admission 与退出回执，合成 IO |
| Refs | leemine/codeswarm#33 |

## 背景与决策

F37 控制原 live root；idle resume 和持久 ACTIVE attach 是新的执行准入，不能用临时
控制身份替换旧 root。新 selector 固定原记录全字段、manager/store/Session、live slot、
execution、队列与锁；不保存认证到 GoalRecord，不增加队列或持久收据。

Native 在原 managed admission 内、普通 attach 的自动 ensure 之前消费同步 typed plan。
热路径要求显式原 Native Pending，与旧 slot 的 source 为同对象，同 harness/agent/Session，
原 execution_done 和原 exit confirmed/cleanup 成功，交互及任务尾部均已退出。冷路径只
允许原 harness 创建的首个受管 Pending，原 slot None，且执行 inventories 无任何工作；
这不支持未知旧子任务的完整活动恢复。过期 source、当前 active 为空都不构成退出证明。

DeepAgent 沿原 send→control 锁顺序核对新 source 和宿主同步 checker，固定事实在回调
之后再次核对。只有取得 sole output lease 才修改记录/slot。idle PAUSED/BLOCKED resume
沿旧合同 revision+1；ACTIVE attach 保持 id/revision，只绑定新 live source。原存储 commit
等待后、实际排队/emit 前均检查；失败只释放本次 lease 和实际本次创建的队列项。
新 plan 仅可在原 source 与原 proof 下重用；不重复 public resume。已发生的存储写入不回滚，
commit 未知/失败不能当成功或无条件重放。

## 拒绝的方案

不在旧 dispatch_input 内迟到换源，因为普通 attach 已先自动 ensure；不把 persisted ACTIVE
视为认证；不从 latest Pending/ambient observer 补 owner；不关闭整个 Session 证明退出；
不新增输出消费者；不改变旧 None public attach/resume 或 F37 原 root 控制。

## 验证与遗留

新增实际组件测试覆盖 cold→hot 新源、原 exit 缺失/未完成、第一 Pending、锁与 commit
等待、callback 替换、sole lease、状态与 revision、精确失败清理。命令和结果在交付证据
中按最终 SHA 留档。无真实 Provider 负向探针。

宿主须提供当前独立 principal、原 Binding、新 Runtime producer 与完整新资源 bundle；
临时 Python typed plan 不代表已经完成宿主授权。owner-only get、自动 EOF 的 Runtime 新
producer、UI 和普通真实验证由后续宿主集成；本片不关闭完整活动恢复或子树出口。
EOF observer 不得自行重新赋权或消费第二条流。

普通 managed attach 也在自动 ensure 前、原锁内核 ACTIVE Goal 的 source 必须是该
Pending 的同一对象；冷 None、旧源和同 host_value 的不同源必须提供 readmission plan。
无 Goal/PAUSED 的普通请求与 legacy public attach 保留。真实原 Native/kernel/TaskLoop
测试使用合成 React 输出，验证两个后继 attempt 均携带新 Pending source 至真实退出；
这不是实际模型服务或远端 Provider 验证。

本地最终受影响组 292 passed（40 manifest 子断言），42 项新增组件用例。
初次用例编写期间保留了缺 request_id、冷 Manager 夹具来源和 before_start 尚无 rail
三个夹具错误日志；修正为原构造/启动合同，没有放宽生产断言。新增独立文件 Ruff 通过；
实际 make check 保留仓库已有格式/动态私有对象 Pylint 报告，不能宣称全量 lint 清洁。
同 SHA strict stable 的精确统计另存交付证据；源码 overlay 不冒充安装包/CI。

## 同 ID Store backing 替换复核

selector 不仅固定 Store 对象和 session_id，还要求 `store._session is 原 Session`。
同 ID、同 GoalRecord 的另一 Session 不可在同步 checker 或 commit await 后取代原存储。
普通受管 attach 对无 Goal、暂停 Goal 及 active Goal 同样复核 backing 对象。
实际 Native 替换反例和 commit/普通 attach 回归补入原测试文件；旧候选结果保留为历史证据，
修复候选须重新执行受影响回归与 strict stable。
