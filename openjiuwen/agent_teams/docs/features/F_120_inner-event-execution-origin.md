# F_120 协调内层事件保留原始执行来源

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | coordination InnerEventMessage / CoordinationKernel |
| 测试基线 | 新增 8 项；与原来源、协调 loop / lifecycle 合计 55 passed |
| Refs | S_18_harness-interaction-contract；F_119_live-execution-origin |

## 背景

原 EventMessage 已有私有来源，但用户输入入队与 self task echo 转 scheduler_scan 两个桥
重新创建普通 InnerEventMessage，导致 EventBus 将原来源遮蔽为 None。直接 NativeHarness
测试未覆盖这两个真实协调入口。

## 决策

InnerEventMessage 复用既有 OriginCarrier。user_input 提交时显式捕获 resolve_execution_origin，
self task echo 显式复制原事件 execution_origin。构造器本身不捕获环境；无来源、轮询事件及
JSON 恢复仍为 None。原 EventBus 负责安装和失效词法 scope，不增加队列、状态机或协议字段。

## 拒绝的方案

不把 origin 放入 payload / JSON，不从正在处理的最新请求补来源，不改变持久 Task、Review
或 mailbox 数据；扫描事件来源不能给扫描结果整体授权。没有新增 completion snapshot 或
排空状态机，queue.empty、EOF、idle 或 owns_execution=False 均不是 Team 完成的证明。

## 验证

真实 CoordinationKernel→原 EventBus→NativeHarness / TaskScheduler 组合，模型执行及
scheduler 的扫描决策使用确定性夹具。包含 user_input、经原 InProcessMessager 的 self task
scan、混源拒绝、无来源旧行为、JSON 冷恢复、私有字段伪造与 deepcopy，以及 callback 结束后
继承子任务的词法失效。先 red 后 green，新增文件同时纳入 stable discovery 和 command，
manifest 完整性检查固定其存在。定向结果与 strict stable 的精确候选来源单独归档。

## 已知遗留

本修复只闭合两个内部事件载体；持久扫描行归属、完整 Team 原请求生命周期、正常排空、
宿主资源和模型授权仍需分别接线。无真实 Provider / UI 验收结论；不能将保守拒绝或停机
成功记为普通 Team 全功能完成。旧可选 observability 依赖缺失会阻断较宽构造测试，验证
记录保留环境失败并与具备完整依赖的来源测试区分。
