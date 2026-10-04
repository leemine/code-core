# F_29 Scheduler submitted Task admission

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | TaskManager 私有原引用捕获、TaskScheduler 原创建点复核 |
| 基线 | core `33d5b922f6a09df07a856c85f47b4851fa5f61e4` |
| Refs | S_03_task-loop；F_26_scheduler-owned-exit-capture |

## 问题与源码事实

原调度器查询 SUBMITTED 任务后等待自身 `_lock`。这期间 `cancel_task` 可以把尚未运行的任务
标为 CANCELED，或 TaskManager 可移除后重新添加同 ID 任务。原创建点仅检查容量、运行标志和
已有 wrapper，仍会启动旧扫描结果，甚至把替换任务 ID 与旧 Session 组合使用。

初始方案误以为公开 `get_task` 返回原引用；实际代码返回深拷贝。真实 TaskManager 测试纠正
了这一假设，不能用两次公开查询结果的 `is` 比较，否则所有正常任务都会被拒绝。

## 最小修复与兼容边界

- TaskManager 私有 `_capture_submitted_tasks()` 在原 manager 锁内取得当前 SUBMITTED 原
  Task 引用快照，保持公开 status 查询的 candidate set 迭代次序。
- `_is_submitted_task(expected)` 只在同一事件循环同步核验当前注册对象仍是原 Task，且状态
  仍为 SUBMITTED。没有导出、持久化或新增 registry。
- Scheduler 仅自己的扫描改用私有捕获；等待原 scheduler 锁后复核原对象/状态、运行标志、
  容量、running/owned wrapper，并从当前 Session 映射取实际 Session。最后判断到原
  `asyncio.create_task` 之间没有 await。
- 旧扫描拒绝被替换对象；下一次正常扫描仍可调度新对象及其 Session。正常任务继续运行。
- 公开 `get_task`、`get_state` 和 codec 的深拷贝行为、所有公开 cancel/stop 签名与语义不变。
  不新增取消集合、队列、任务状态机或第二套生命周期。

## 验证与证据

真实 TaskManager + TaskScheduler 在原 scheduler 锁上建立确定性屏障，记录实际 wrapper
创建后的进入。修复前取消、替换、移除三例均失败（误启动）；未变化正常任务通过。
修复后新增七例覆盖以上四场景、公开查询/状态快照继续隔离、Session 映射锁后解析，以及
捕获 await 期间 stop 后不创建。替换用例还确认下一轮扫描正常执行新 Session。

旧 scheduler stop 测试的 fake Manager 仅补上对应私有引用语义，保留原断言。新增文件追加到
stable native-interaction 的发现与执行列表，无删除原测试或修改超时。
受影响 Controller、DeepAgent interaction/event executor/stop/round/stream 回归：
**178 passed，1 项原有 LLM skip**。编排器自身：**10 passed**。
新锁环境 strict stable：**1753 passed，0 failed/error/timeout/skipped/blocked**。
执行命令为 `TESTCTL_NETWORK_MODE=strict TESTCTL_BWRAP_SUDO=0
TESTCTL_PYTHON=/tmp/r2b-scheduler-submit-recheck-venv/bin/python python3 tools/testctl.py
run --profile pr-stable --archive /tmp/r2b-scheduler-submit-recheck/stable-unprivileged`。
归档 `20261004T195112-4986f750` 对应代码/测试提交 `d0e79782`（后续仅文档）。
首轮沿 CI sudo 模式因主机需要密码得到 11 blocked；同原 strict 配置使用本机受支持的
非 sudo bubblewrap 后通过，没有改为 audit 或降低超时。

证据目录：`/tmp/r2b-scheduler-submit-recheck/`。`red-corrected.log` 是纠正深拷贝误判后的
真实红测；`affected-final2.log` 是受影响成功结果；`source.json` 记录候选源码与 uv.lock。
新隔离环境由 `uv sync --locked --group test --extra cli` 安装（OpenAI 2.24.0），不把候选
editable core 测试冒充下游正式锁定配对。初次安装被沙箱 DNS 阻断，获授权环境重试成功。

make check 已执行：测试文件 Ruff/format 通过；生产原模块存在旧 import/format/Pylint
告警，新增两处刻意的私有调用触发 protected-access 告警，环境缺 codespell。没有为这些
问题全文件格式化或降低门禁，不能把 make 忽略工具退出的返回值称为完整 lint 通过。
编排器首轮本地 socket 夹具受沙箱阻断，原测试在授权环境重验通过。

## 尚未证明

此补丁只闭合扫描到创建之间的原 Task 准入；不证明已创建 wrapper 的精确取消、Round
自然排空、Provider 真实退出或共享 Session 全部任务已结束。原 F_26 owned wrapper 退出
事实、宿主原 Round 关联与后续真实验证仍各自必须完成。没有启动真实 Provider 或新增真实
撤权、延迟和取消对抗探针。
