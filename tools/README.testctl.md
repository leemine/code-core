# testctl MVP

`testctl.py` 是仓库内、零第三方运行时依赖的测试编排入口。它不会替代 pytest，只负责环境预检、发现、执行、JUnit 归一化和本地归档。

```bash
# 查看依赖安装计划；增加 --execute 才会真正安装
python3 tools/testctl.py bootstrap

# 环境、工具链和 suite 入口预检
python3 tools/testctl.py doctor

# 发现测试并输出 inventory
python3 tools/testctl.py discover --profile mvp --output /tmp/code-core-inventory.json

# 查看执行决策
python3 tools/testctl.py plan --profile mvp

# 执行并归档
python3 tools/testctl.py run --profile mvp
TESTCTL_NETWORK_MODE=strict python3 tools/testctl.py run --profile pr-stable

# 重新生成人类可读摘要
python3 tools/testctl.py summarize artifacts/test-runs/<run-id>

# 全量 pytest collect 的文件级分片计划
python3 tools/shardplan.py /tmp/pytest-collect.log --target-cases 250 --output /tmp/shards.json

# 完整 Python 诊断回归：全仓收集、文件级分片、逐例超时、闭合核对和失败清单
uv sync --locked --group test --extra cli --extra pulsar --extra sandbox --extra online-rl --python 3.13
.venv/bin/python tools/full_regression.py --workers 2

# 列出归档、对比基线、预览到期清理
python3 tools/archivectl.py list
python3 tools/archivectl.py compare artifacts/test-runs/<base> artifacts/test-runs/<candidate>
python3 tools/archivectl.py prune

# 外部中断后从已有 JUnit 恢复 NOT_RUN 摘要（默认仅预览）
python3 tools/archivectl.py recover artifacts/test-runs/<interrupted-run>
```

默认归档位于 `artifacts/test-runs/<run-id>/`，包含 manifest 快照、环境指纹、plan、JUnit、日志、`summary.json` 和 `summary.md`。该目录被 Git 忽略，CI 应将其上传到持久化制品存储。

仓库要求 Python `>=3.11,<3.14`。环境不满足时，plan/run 会将必需套件标记为 `BLOCKED` 并让门禁失败。可使用 `TESTCTL_PYTHON=/absolute/path/to/python` 显式选择受控解释器；工具保留符号链接入口，避免丢失虚拟环境语义。

`TESTCTL_NETWORK_MODE=strict` 使用 Bubblewrap 网络 namespace，能力不足会将必需套件标为 `BLOCKED`。未设置时为 `audit`，不具备强隔离保证。pytest 使用 signal 单例超时；本地 HTTP 服务可由 suite 的 `services` 声明，动态端口由服务绑定端口 0 并通过 `TESTCTL_PORT=<port>` 报告。

在允许无密码 `sudo` 的专用 CI runner 上，可显式设置 `TESTCTL_BWRAP_SUDO=1`，使 strict 模式以 `sudo -n -E bwrap` 建立网络 namespace。普通本地运行保持非特权路径；若特权 probe 不可用，仍明确标为 `BLOCKED`，不会悄悄降级到 audit。

`pr-stable` 仅覆盖首批稳定套件，不代表全量回归。当前 code-core 的 logger 导入时会写入仓库 `logs/`，因此其稳定套件暂时声明 `writable_workdir`，不具有源码只读保证。GitHub Actions 已提供 PR、主干及夜间稳定回归入口，结果上传为制品；远程执行状态与发布级不可变归档仍需在平台上验证。`archivectl.py prune --execute` 会删除已到期的本地运行目录，默认仅预览。

`full_regression.py` 是独立于 PR 稳定门禁的全量 Python 诊断入口，夜间或手动触发。默认每片约 250 例、2 个 worker、单例 30 秒/分片 1,800 秒上限；归档位于 `artifacts/test-runs/full-python-<UTC>/`，保存提交和锁文件指纹、全仓及分片收集日志、JUnit、闭合摘要与逐例 `failure_inventory.csv`。夜间环境显式安装 Pulsar、sandbox、online-RL 等可选依赖。真实 AIGW 系统测试只在二进制存在时执行；否则逐例跳过并说明 `AIGW_BIN` 或默认位置。当前全量基线仍含失败，夜间任务会如实标红并上传证据。

全量入口要求 `summary.closed=true` 且 `not_run=0`；即使 pytest 本身退出 0，收集差异、缺失用例或归因清单生成失败也使任务失败。动态参数 ID 不自动按函数名合并。AIGW 跳过表示普通 runner 缺少外部能力，不等于该系统测试已通过；完整验证仍需专用 AIGW/Redis 环境。
