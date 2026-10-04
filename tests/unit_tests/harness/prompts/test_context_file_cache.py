# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for caching context files by their on-disk identity."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.core.sys_operation.base import OperationMode
from openjiuwen.harness.prompts.sections import context as context_module
from openjiuwen.harness.prompts.sections.context import _read_context_file


class _FakeFs:
    """Filesystem double counting reads and serving current file contents."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    async def read_file(self, path: str):
        self.reads.append(path)
        try:
            content = Path(path).read_text(encoding="utf-8")
        except OSError:
            return SimpleNamespace(code=1, data=None)
        return SimpleNamespace(code=0, data=SimpleNamespace(content=content))


class _FakeSysOperation:
    """SysOperation double exposing only the mode and fs() the reader uses."""

    def __init__(self, mode=OperationMode.LOCAL) -> None:
        self.mode = mode
        self._fs = _FakeFs()

    def fs(self) -> _FakeFs:
        return self._fs


class _FakeWorkspace:
    """Workspace double mapping a file key straight to a path."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get_node_path(self, file_key: str) -> Path:
        return self._path


@pytest.fixture(autouse=True)
def _clear_cache():
    context_module._CONTEXT_FILE_CACHE.clear()
    yield
    context_module._CONTEXT_FILE_CACHE.clear()


@pytest.fixture(name="agent_md")
def _agent_md(tmp_path: Path) -> Path:
    path = tmp_path / "AGENT.md"
    path.write_text("# Agent\n\nreal content that is not a template\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_unchanged_file_is_read_once(agent_md: Path) -> None:
    """The whole point: an unchanged file must not be re-read every model call."""
    sys_op = _FakeSysOperation()
    workspace = _FakeWorkspace(agent_md)

    results = [await _read_context_file(sys_op, workspace, "AGENT.md") for _ in range(5)]

    assert len(sys_op.fs().reads) == 1
    assert all(item == results[0] for item in results)
    assert "real content" in results[0]


@pytest.mark.asyncio
async def test_edited_file_is_re_read(agent_md: Path) -> None:
    """A changed file must not be served from cache."""
    sys_op = _FakeSysOperation()
    workspace = _FakeWorkspace(agent_md)
    await _read_context_file(sys_op, workspace, "AGENT.md")

    agent_md.write_text("# Agent\n\ncompletely different content here\n", encoding="utf-8")
    result = await _read_context_file(sys_op, workspace, "AGENT.md")

    assert len(sys_op.fs().reads) == 2
    assert "completely different" in result


@pytest.mark.asyncio
async def test_same_size_edit_is_re_read(agent_md: Path) -> None:
    """Equal length must not defeat invalidation; mtime carries it."""
    agent_md.write_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8")
    sys_op = _FakeSysOperation()
    workspace = _FakeWorkspace(agent_md)
    await _read_context_file(sys_op, workspace, "AGENT.md")

    stat_result = agent_md.stat()
    agent_md.write_text("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n", encoding="utf-8")
    os.utime(agent_md, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000))

    result = await _read_context_file(sys_op, workspace, "AGENT.md")
    assert len(sys_op.fs().reads) == 2
    assert result.startswith("bbbb")


@pytest.mark.asyncio
async def test_sandbox_reads_are_never_cached(agent_md: Path) -> None:
    """Under a sandbox the path names a file inside it, not this host's copy.

    Stamping against a same-named host file would serve content that was never
    read, so sandbox reads must stay on the uncached path.
    """
    sys_op = _FakeSysOperation(mode=OperationMode.SANDBOX)
    workspace = _FakeWorkspace(agent_md)

    for _ in range(3):
        await _read_context_file(sys_op, workspace, "AGENT.md")

    assert len(sys_op.fs().reads) == 3
    assert context_module._CONTEXT_FILE_CACHE == {}


@pytest.mark.asyncio
async def test_unfilled_template_result_is_cached(tmp_path: Path) -> None:
    """A rejected template resolves to None just as durably as content does."""
    path = tmp_path / "SOUL.md"
    path.write_text("<!-- TODO: fill me in -->\n", encoding="utf-8")
    sys_op = _FakeSysOperation()
    workspace = _FakeWorkspace(path)

    first = await _read_context_file(sys_op, workspace, "SOUL.md")
    second = await _read_context_file(sys_op, workspace, "SOUL.md")

    assert first is None and second is None
    # The None verdict came from a real read, so repeating it is wasted work.
    assert len(sys_op.fs().reads) == 1


@pytest.mark.asyncio
async def test_missing_file_is_not_cached(tmp_path: Path) -> None:
    """An absent file cannot be stamped, so it must stay retryable."""
    sys_op = _FakeSysOperation()
    workspace = _FakeWorkspace(tmp_path / "absent" / "AGENT.md")

    assert await _read_context_file(sys_op, workspace, "AGENT.md") is None
    assert await _read_context_file(sys_op, workspace, "AGENT.md") is None
    assert context_module._CONTEXT_FILE_CACHE == {}


@pytest.mark.asyncio
async def test_distinct_files_do_not_collide(tmp_path: Path) -> None:
    """Two context files must not serve each other's contents."""
    agent = tmp_path / "AGENT.md"
    soul = tmp_path / "SOUL.md"
    agent.write_text("agent file body, long enough to pass\n", encoding="utf-8")
    soul.write_text("soul file body, long enough to pass\n", encoding="utf-8")
    sys_op = _FakeSysOperation()

    agent_result = await _read_context_file(sys_op, _FakeWorkspace(agent), "AGENT.md")
    soul_result = await _read_context_file(sys_op, _FakeWorkspace(soul), "SOUL.md")

    assert "agent file body" in agent_result
    assert "soul file body" in soul_result


@pytest.mark.asyncio
async def test_real_local_optional_context_absence_is_quiet_and_later_creation_is_read(tmp_path, monkeypatch, caplog):
    """The real local reader must not emit errors for absent optional files."""
    from unittest.mock import AsyncMock
    from openjiuwen.core.sys_operation.config import LocalWorkConfig
    from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard

    path = tmp_path / "HEARTBEAT.md"
    operation = SysOperation(SysOperationCard(
        id="optional-context", mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)], restrict_to_sandbox=True),
    ))
    fs = operation.fs()
    read = AsyncMock(wraps=fs.read_file)
    monkeypatch.setattr(fs, "read_file", read)
    workspace = _FakeWorkspace(path)
    for _ in range(3):
        assert await _read_context_file(operation, workspace, "HEARTBEAT.md") is None
    read.assert_not_awaited()
    assert not [record for record in caplog.records if record.levelno >= 40]
    assert str(path) not in context_module._CONTEXT_FILE_CACHE
    path.write_text("Run the synthetic workspace check.\n", encoding="utf-8")
    assert await _read_context_file(operation, workspace, "HEARTBEAT.md") == path.read_text()
    read.assert_awaited_once_with(str(path))


@pytest.mark.asyncio
async def test_observed_absence_evicts_old_cache_before_same_stamp_recreation(agent_md):
    """An observed missing path cannot revive its previous same-stamp cache."""
    operation = _FakeSysOperation()
    workspace = _FakeWorkspace(agent_md)
    original = await _read_context_file(operation, workspace, "AGENT.md")
    original_stat = agent_md.stat()
    agent_md.unlink()
    assert await _read_context_file(operation, workspace, "AGENT.md") is None
    assert str(agent_md) not in context_module._CONTEXT_FILE_CACHE
    replacement = original.replace("real", "next")
    assert len(replacement) == len(original)
    agent_md.write_text(replacement, encoding="utf-8")
    os.utime(agent_md, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert await _read_context_file(operation, workspace, "AGENT.md") == replacement
    assert len(operation.fs().reads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [OperationMode.SANDBOX, None])
async def test_host_absence_does_not_skip_remote_or_unknown_operation(tmp_path, mode):
    """A missing host path says nothing about a remote filesystem."""
    from unittest.mock import AsyncMock
    operation = _FakeSysOperation(mode)
    operation.fs().read_file = AsyncMock(return_value=SimpleNamespace(
        code=0, data=SimpleNamespace(content="Remote content must still be read."),
    ))
    path = tmp_path / "absent.md"
    result = await _read_context_file(operation, _FakeWorkspace(path), "AGENT.md")
    assert result == "Remote content must still be read."
    operation.fs().read_file.assert_awaited_once_with(str(path))
    assert context_module._CONTEXT_FILE_CACHE == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("stat unavailable"), NotADirectoryError("not a directory")])
async def test_non_missing_stat_errors_keep_original_read_path(agent_md, monkeypatch, error):
    """Unknown/inaccessible is not known absent and must not suppress the read."""
    original_stat = Path.stat
    def stat(path, *args, **kwargs):
        if path == agent_md:
            raise error
        return original_stat(path, *args, **kwargs)
    operation = _FakeSysOperation()
    monkeypatch.setattr(Path, "stat", stat)
    result = await _read_context_file(operation, _FakeWorkspace(agent_md), "AGENT.md")
    assert "real content" in result
    assert operation.fs().reads == [str(agent_md)]
    assert context_module._CONTEXT_FILE_CACHE == {}


@pytest.mark.asyncio
async def test_existing_file_still_uses_real_local_sandbox_authorization(tmp_path, caplog):
    """Quiet absence handling must not become an alternate file-content reader."""
    from openjiuwen.core.sys_operation.config import LocalWorkConfig
    from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    denied = tmp_path / "outside.md"
    denied.write_text("Synthetic content outside the allowed root.\n", encoding="utf-8")
    operation = SysOperation(SysOperationCard(
        id="context-sandbox-deny", mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(sandbox_root=[str(allowed)], restrict_to_sandbox=True),
    ))
    assert await _read_context_file(operation, _FakeWorkspace(denied), "AGENT.md") is None
    assert [record for record in caplog.records if record.levelno >= 40]
