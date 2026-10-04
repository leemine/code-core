# Original member committed facts

| 项 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 范围 | 原 MemberWriteReceipt 的纯事实读取 |
| Refs | leemine/codeswarm#33 |

## 事实与授权分开

原 receipt.check_current 调用原写入来源，并要求原 execution_origin_scope。它适合成功提交后
即时保存原回执，却不能成为稍后 monitor / history worker 的读取授权。后台不能重装旧 origin，
也不能借最近 Turn 的写入权限。

新增 `receipt.check_integrity() -> None` 和 `receipt.committed_facts() -> MemberCommittedFacts`。
两者只检查原发行实例和不可变字段，不读取 ContextVar、不调用/安装原 source checker、不查询
数据库。原 check_current 仍严格调用原 source，并在前后复用完整性检查；语义没有放宽。

MemberCommittedFacts 是 frozen/live-only、copy/deepcopy 保持 identity、pickle 拒绝的事实载体。
字段是原 database/dao/sessions/transaction 引用、kind/team_name/member_name、actor/entity/source_id、
独立 stamp 副本和提交时完整 record_values tuple（删除时为原被删记录）。包含私有记录内容，
不得直接进入 RPC、JSON、审计或 UI。它不证明当前数据库目的地、当前记录仍存在、当前 owner
权限或资源范围；原对象的可变状态仍必须由当前读取授权独立复核。

完整性校验钉原 receipt 实例、原 operation 全部字段、facts 全部字段和 stamp 原值，类型敏感：
True 不能冒充 revision 1。同 stamp 换正文、operation、actor、DB、transaction 都拒绝。
单独构造一个 facts 数据对象不是 capability；消费者必须持原登记槽实际 receipt 并检查其发行
完整性，不能从数据库读回 stamp 后补签。

## 消费方要求和未实现范围

之后的 Team projection 必须先获得当前原 parent/entity/member registration 的 live 读取授权，
再用实际原 DAO 的完整 row 字段和 stamp 对照原 receipt 事实，最后只投影允许字段。事实接口
不能取代该授权、原源替换屏障或 E2A 最终发送检查。

本片不增加 SpawnManager 登记槽，不改原 shared DB cache / BuildContext 工厂，不推进所有
status 写的回执，也不改 Task/Review/message/monitor 调度。原正常 bool 更新确实可使行 revision
前进而调用者未接收新 receipt；该后续接线仍待冻结，不能读时取最新 stamp 伪装已经完成。
受管 Team、复杂 cleanup、跨进程资源退出均未因此开放。

## 确定性验证

使用真实临时 SQLite 原 DAO 和已提交回执，合成主体，无 Provider/网络。
新增 10 例：原 scope 退出且 source 已失效后 worker 可读纯事实、check_current 仍拒绝；
伪造 receipt、stamp/完整row/operation/actor/DB/transaction/类型变更拒绝；正常更新与删除后
旧事实仍保持原 revision/内容且不冒充当前存在。原 record/effect 回归共 102 passed。
新例复用已纳入 stable 的原测试文件，无预算/白名单变化。
候选源码 overlay 使用既有 6dce 环境，不视为新的 swarm 正式锁配对验收。
