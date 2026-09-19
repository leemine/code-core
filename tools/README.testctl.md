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

`pr-stable` 仅覆盖首批稳定套件，不代表全量回归。当前 code-core 的 logger 导入时会写入仓库 `logs/`，因此其稳定套件暂时声明 `writable_workdir`，不具有源码只读保证。GitHub Actions 已提供 PR、主干及夜间稳定回归入口，结果上传为制品；远程执行状态与发布级不可变归档仍需在平台上验证。`archivectl.py prune --execute` 会删除已到期的本地运行目录，默认仅预览。
