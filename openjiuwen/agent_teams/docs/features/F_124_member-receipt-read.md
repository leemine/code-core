# Original member receipt-associated fact query

| 项 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 范围 | 原 MemberDao 的纯事实读取，非授权 |
| Refs | leemine/codeswarm#33 |

## 决策

`await database.member.read_committed_member(original_receipt)` 返回原
`MemberCommittedFacts` 对象，仅在原数据库整行与其 stamp/完整不可变内容一致时成功。
原 receipt 必须来自创建/更新交易后保留的登记槽，不能在查询时由当前行补签。
方法不接受 team/member 字符串覆盖，不调用旧 source checker，也不恢复 ExecutionOrigin。
原 writer scope 已结束不阻止纯事实查询；宿主必须另外验证当前 parent/entity/credential。

复用现有 writer session 和锁；等待原锁后、SQL 前后及 session 退出后检查原 DAO、DB、
sessions、发行时记录的 engine/factory/有效配置与实际 mapper/table 目的地。发行时纯引用
快照保留原对象，不能只在读取时重新捕获被替换后的同一个可变 DB。返回内容可能含私有字段，
消费者必须按允许字段投影，禁止直接序列化返回值。无新的 store、锁、队列或索引。

## 拒绝的方案

不使用可替换 read replica：真实 A/B 两库可以拥有相同 stamp 的旧行，A 已改变而 B
仍返回旧行；普通 get_member 会读到 B，新的 receipt 查询必须核对 A 并拒绝。
不把 receipt.check_current 用作后台读授权；那会恢复或借用已经结束的 writer 来源。
不把当前 stamp 与 revision 等同 owner 权限，也不延续事务锁到网络发送。

## 验证与边界

真实临时 SQLite：同 ID 覆写、删除重建、合法更新并推进 receipt、旧 writer scope
退出、reader 替换、writer/mapper/DAO 替换、整组可变 DB 引用重定向到完整克隆 B、
伪造 receipt、等待锁期间原 DB 变化。原 record 回归 92 passed。使用已有测试文件及
stable 发现/执行，不调整预算或失败白名单。无真实 Provider、外部服务或迁移。

本接口只证明该次查询观察到的完整原行，不保证随后 SQL/历史写入/E2A queue 没有变化。
当前 parent/entity 授权、全部写回执推进、持久化/最终发送的异步一致性仍由宿主接线，
受管 Team 与复杂 cleanup 未因此开放。源码 overlay 不算 swarm 正式锁配对验收。
