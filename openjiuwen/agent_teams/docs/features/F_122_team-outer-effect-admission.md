# Team outer effect admission

| 项 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 范围 | 原 TeamBackend.spawn_member / force_delete_team_session |
| Refs | leemine/codeswarm#33 |

## 原问题和本片出口

原创建先写 member workspace/link/refcount、card/prompt/cache，最后才到受管 DAO；原 force delete
先清 worktree，再由 DAO 拒绝。真实 SQLite / 临时目录反例证明无来源操作虽最终被拒，文件已经
写入或删除。底层 record CAS 无法代替外层副作用授权。

`MemberRecordAuthorizer` 末尾新增可选 `bind_for_effect(operation, origin)`。原 MemberEffectOperation
是 live-only 不可 pickle 的冻结对象，捕获原 database/backend/team/member/session、创建参数和
原 workspace placement/config 意图；不是客户端提供的 path 或持久 stamp 升级权限。
MemberEffectPermit 持原 operation/origin/entity/actor/source_id、同步 `check_current()` 与
`on_committed(receipt)`。原 source 先于首 await 捕获；读后、两处同步 FS 前、DAO 前，以及原
receipt 回调前后均校验原引用。回调后有纯复核，变更或非 None/awaitable 返回拒绝。

创建仍走原 `prepare_member_workspace` / `WorkspaceAssembler.write_member_identity`，原 DAO
独立授权/CAS 不能被 effect permit 代替。受管状态枚举转为原 DAO 的标量值，legacy 传参不变。
实际提交 receipt 原样交 `on_committed`，由宿主存回已有 member 登记槽，不建 map/store。
缺 callback 在 FS 前拒绝。提交后回调失败、重入或来源失效使用原
MemberWriteCommittedButUnconfirmed，保留实际 receipt，不返回普通 False 诱导再次创建。
原 MemberOpResult / 工具 wire 结构不变；已允许 FS 后再遭 DAO 拒绝不宣称原 FS 自动回滚。

## 明确未开放

本片受管 `force_delete_team_session` 一律在首次 await/FS 前拒绝。原复杂清理仍会经过
repo_lock、backend 查询 branch、git remove、branch delete，以及 `asyncio.to_thread(rmtree)`；
这些最终消费者尚无原来源检查，不能只在 await 前检查就称完整保护。此片没有改通用
harness worktree API 或线程来源语义，没有新锁/队列/状态机。

无 SPI 的旧入口对当时原 Team 已有受管/损坏 stamp 行提前拒绝；纯 legacy 实际创建与清理
保留。该读检查不是跨进程首次创建受管身份的原子 FS 屏障，也不证明其它旧进程不能碰相同路径。
真正组织 Team 的原 host/资源挂载、member receipt 登记和并发资源归属仍待完成，不能据此开放
Team、完整退出或跨 owner 工作区共用。宿主 effect checker 必须独立证明原 workspace 资源范围，
不能把相同 Team/member 名或已有符号链接自动认作拥有权。

## 测试与来源

实际临时 SQLite、原 Backend、原 binder/identity 写入与 legacy 清理组件；合成 owner/source、
messager 不联网，不启动 Provider。原两项红测已保留；首次临时红测的第二项导入错误已更正，
只以更正后的两项实际断言失败记红证据。
新增文件进入既有 stable discover/command 与 regression_manifest；原预算/白名单保持。
最终受影响定向 350 passed、14 既有移除工具 skips、41 subtests；新增文件含 15 例。不能把 skips 写成通过。
使用明确候选 core source overlay 与既有 6dce 锁环境；新正式配对/真实 UI 尚未验证。

## Legacy reader 路由修复

决定文件副作用的 legacy 检查必须走原 writer factory/session，不能从可改向或滞后的 read
factory 推断原库没有受管成员。现复用原 DbSessions.write（只在查询阶段持原锁，不跨 FS
await），首 await 前固定 DB/DAO/writer 引用，取得 session 后和查询返回后复核实际
Team/TeamMember mapper/table 路由。纯 read factory 改向不能遮蔽原 writer 的受管行；writer
或 mapper 改向也在 FS 前拒绝。查询结束后的跨进程首次受管登记与 FS 竞态仍属上述未完成范围，
此修复不把它描述为原子资源权限。

实际 A 库有受管成员、B 库空 roster，替换原 DbSessions._read_session_local 的独立反例在修复前
仍删掉 A 工作区，已留红证据。新增创建/清理 × reader/writer/mapper 六例转绿；影响面 356
passed、14 既有 skips、42 subtests。正式基线已合入本候选，先前 a464 的 stable 不代表此候选。
