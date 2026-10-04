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
