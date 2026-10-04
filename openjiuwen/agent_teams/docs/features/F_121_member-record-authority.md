# Member record authority

| 项 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 范围 | 原 member DAO、原 team 删除级联、monitor 内部 stamp |
| Refs | leemine/codeswarm#33 |

## 决策

每次受管 create 使用内部随机 nonce，原事务 revision 从 1 开始；更新同时 CAS 原 nonce/revision，
并由原 host-issued live permit 校验调用者及 exact operation。旧 nullable 行保持 legacy，
不能读时自动提升。create/update 可返回原 committed receipt，宿主须保存到已有成员登记对象。

## 拒绝的方案

仅 member_name、updated_at、读取时 hash、已知 nonce 都不能授权写入。冻结所有 status
会破坏原 Team 生命周期。新数据库、权限表和全局 map 不属于此片。

## 验证

真实 file-backed SQLite：42 新例与原 DB/concurrency/monitor 共 194 例通过；扩到原 Team tools/options 后 357 通过、14 既有跳过（旧工具已移除/并入现 Task 工具，未放宽）。覆盖六类更新与 create、未知来源、同 ID 覆盖/重建、旧 DAO 降级、错误 nonce/revision、并发 CAS、原事务 SQL 后拒绝回滚、取消/commit 异常、nullable 幂等迁移、bulk reset 在任意 DROP 前拒绝混合受管行。测试入口同时纳入 pr-stable 的 discover/command 与 regression_manifest。

这不等于原 swarm producer 红测整体转绿：原 host 未保存/比较 receipt，跨 E2A 检查尚未挂载。验证只使用合成主体和临时 DB，无真实 Provider。

## 已知遗留

这是底层原事务接缝，不自动挂 Runtime/Team factory，也不涵盖 task/message 全部写入。
宿主必须提供真实 entity/member actor 来源及服务操作权限；缺 source 拒绝，不能借最近 Turn。
跨 E2A 最终 member revision 重验和原 parent permit 关联尚未实现。

## API 和兼容

- `TeamDatabase(config, *, member_record_authorizer=None)` 是可选 live SPI，不进入 Spec/JSON。
- `MemberRecordAuthorizer.bind_for_write(operation, actual_origin)` 返回原 `MemberWritePermit`；operation 固定 actual DB、team/member、kind 和更改字段。permit 固定原 origin/entity/actor、source_id、原 receipt stamp 及同步 checker。
- 七写方法末尾 `return_receipt: bool=False` 默认仍返回旧 bool；True 在受管成功提交时返回不可序列化 `MemberWriteReceipt`。它包含原 operation、stamp、permit 和 committed transaction 引用，缺 receipt 不能通过读行补签。
- 更新必须保留原 nonce/source、CAS 原 revision/digest 并 revision+1；create 内部生成不可复用 nonce。旧全 NULL 行不自动认领；部分 stamp 损坏拒绝。
- 原 `team.delete_team` 对每个受管成员校验，再按原 stamp CAS 删除，最终 team DELETE 要求无剩余成员，避免级联吞入新增行。bulk reset 无逐成员 authority，在任何 DROP 前检测受管/部分 stamp 即拒绝。
- 无 SPI 的旧 update 在 SQL WHERE 排除任意 stamp 非空行；原 worktree/fallback/cascade 写法也拒绝受管行。旧全 NULL 行状态机/返回值保留。
- stamp 是非秘密完整性/版本对照，并非签名或授权。实际任意 Python/SQL 已能越过 DAO 的可信进程不在该 SPI 的隔离范围内。row 字段被改而原 digest 未更新可检测；恶意重写全部 provenance 不是凭 hash 就可防御。
- checker 在原 write lock 后、读后、SQL 前、flush 后 commit 前运行；callback 返回后再次纯复核原引用及事务。checker 必须同步，不得重入原 DB write 或跨 await 持有其它锁。不可确认的 commit 异常不会伪造 receipt。

## 后续外层消费边界

`TeamBackend` 创建前写 workspace/identity，`force_delete_team_session` 在 DAO 删除前清 worktrees，
这些外层副作用需要原 host admission 前置，当前不因 DAO 新 SPI 就开放受管 Team。
普通 status 更新不冻结：host 必须把每次 committed receipt 保存到已有成员登记对象；
source/projection 仅能消费该原 receipt，而不能在 monitor 读取时取最新 stamp 自封来源。

受管 fallback model 提升保留原 no-op 语义：已提升/无候选时返回 False，不写行、不增加 revision，
不伪造新的 committed receipt。该反例纳入同一稳定回归文件。

## 首回调与提交后确认

首次 origin checker/bind callback 前固定实际 DAO、DB、DbSessions、write factory/engine 与原锁引用；
不仅比较相同成员名/nonce。实际 SQL Session 的 bind 与原 DB 必须相同。两个真实不同 SQLite 文件
复制同一初始记录后，首 source callback / bind callback 的 DB、sessions、DAO 重定向均拒绝。

commit 成功返回后再次检查原来源。若真实写入已经提交但来源不再允许当前 ACK，抛
`MemberWriteCommittedButUnconfirmed` 并保留原 immutable receipt；不得声称未写、返回普通 True
或自动重放。`receipt.check_current()` 对原 issuance facts/原 bound source 复核，宿主保存或交付
前仍须调用。异常中的 receipt 只是原交易事实/补偿线索，不能借当前新 owner 授权。commit 本身
异常/取消且未知实际持久结果时不造 receipt，也不得推断一定未写。

本次新增/累计 58 个确定性反例与正常行为用例，DB/concurrency/monitor 共 210 通过；
58 例均在既有 stable discover/command 内。旧完整产品 Team 的挂载与外层副作用阻断未因此关闭。

## ORM 路由与跨实例清库屏障补充

仅固定 factory/default bind 仍不能证明实际 SQL 去向：SQLAlchemy mapper/table `binds`
可指向另一引擎。受管分支现在要求原 TeamDatabase 创建的标准单引擎
`async_sessionmaker` / `AsyncSession` 配置；拒绝额外 binds、自定义 Session class 或变更
factory 参数。原事务每次权限检查同时核对 Team / TeamMember 的实际 mapper 和 table
路由，包含 callback 返回后的检查。未受管旧 DAO 不增加此构造限制。

SQLite storage-wide cleanup 在原数据库事务内、schema/受管行查询前取得 `BEGIN IMMEDIATE`
写 reservation，防另一 TeamDatabase 在检查后创建受管行而被后续清库吞掉。并发者须等待
原数据库事务完成；若父 team 已被清除，create 保持原 False 失败，不能返回失真的 receipt。
未增加进程锁或持久化存储。PostgreSQL 使用原事务 table lock，但本次没有真实 PostgreSQL
服务验证。MySQL 的 DROP 隐式提交无法保留同一事务屏障：只要原 member 表存在，storage-wide
cleanup 明确拒绝，即使当前仅 legacy 行；单次 SELECT 无法证明稍后仍是纯 legacy。此项确实
收窄 MySQL 全量清库兼容性，但不改变逐成员/逐 team DAO 操作。库内唯一生产入口是原
`TeamDatabase.cleanup_all_runtime_state`，未发现其它自动生产调用，数据库类型仍支持 MySQL。
不得把此拒绝或未运行的服务验证记为 MySQL / PostgreSQL 清库已验收。

原两个实际 SQLite 红测在修复前均失败，现新增 13 例（路由 source/permit/事务内变化、
status/cascade、原数据库并发屏障），本文件累计 71 例；受影响 DB/concurrency/monitor
223 例通过。第一次扩大命令用了不存在的旧 monitor 路径而未运行测试，已单独保留并更正；
结果只取更正后命令。来源均为候选 core overlay 与既有 6dce 锁环境，未称新锁配对验收。
