"""Host environments remain private to each actual Shell execution."""

import asyncio
import os
import shlex
from contextvars import ContextVar

import pytest

from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperation, SysOperationCard
from openjiuwen.harness.tools import BashTool


@pytest.fixture
def operation(tmp_path):
    return SysOperation(
        SysOperationCard(
            id="shell-environment-test",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(shell_allowlist=None, sandbox_root=[str(tmp_path)]),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_environment_resolved_per_execution_without_parent_mutation(operation, tmp_path, monkeypatch, stream):
    monkeypatch.delenv("AUDIT_CHILD_ENV", raising=False)
    values = iter(["first", "second"])
    tool = BashTool(operation, environment_provider=lambda: {"AUDIT_CHILD_ENV": next(values)})
    args = {"command": 'printf "%s" "$AUDIT_CHILD_ENV"', "workdir": str(tmp_path)}
    for expected in ("first", "second"):
        results = [r async for r in tool.stream(args)] if stream else [await tool.invoke(args)]
        assert all(r.success for r in results)
        assert any(expected in str(r.data) for r in results)
        assert "AUDIT_CHILD_ENV" not in os.environ
    other = await BashTool(operation).invoke(args)
    assert other.success
    assert "first" not in str(other.data) and "second" not in str(other.data)


@pytest.mark.asyncio
async def test_background_environment_is_child_only(operation, tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIT_CHILD_ENV", raising=False)
    output = tmp_path / "background.txt"
    tool = BashTool(operation, environment_provider=lambda: {"AUDIT_CHILD_ENV": "background"})
    result = await tool.invoke(
        {
            "command": 'printf "%s" "$AUDIT_CHILD_ENV" > ' + shlex.quote(str(output)),
            "workdir": str(tmp_path),
            "run_in_background": True,
        }
    )
    assert result.success
    for _ in range(100):
        if output.exists() and output.read_text() == "background":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("owned background command did not write its environment")
    assert "AUDIT_CHILD_ENV" not in os.environ


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream", "background"])
@pytest.mark.parametrize("bad", [None, {"": "x"}, {"A=B": "x"}, {"A": "x\0y"}, {"A": 1}, "exception"])
async def test_invalid_or_revoked_environment_never_executes(operation, tmp_path, mode, bad):
    marker = tmp_path / "must-not-exist"

    def provider():
        if bad == "exception":
            raise PermissionError("SYNTHETIC_SECRET_MUST_NOT_ESCAPE")
        return bad

    tool = BashTool(operation, environment_provider=provider)
    args = {"command": "touch " + shlex.quote(str(marker)), "workdir": str(tmp_path)}
    if mode == "background":
        args["run_in_background"] = True
    results = [r async for r in tool.stream(args)] if mode == "stream" else [await tool.invoke(args)]
    assert results and all(not r.success for r in results)
    assert all(r.error == "Shell environment unavailable." for r in results)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_context_bound_provider_keeps_concurrent_calls_separate(operation, tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIT_CHILD_ENV", raising=False)
    value = ContextVar("environment-test-value")
    tool = BashTool(operation, environment_provider=lambda: {"AUDIT_CHILD_ENV": value.get()})

    async def run(text):
        token = value.set(text)
        try:
            result = await tool.invoke(
                {"command": 'sleep 0.02; printf "%s" "$AUDIT_CHILD_ENV"', "workdir": str(tmp_path)}
            )
            assert result.success
            return str(result.data)
        finally:
            value.reset(token)

    first, second = await asyncio.gather(run("ALICE_ONLY"), run("BOB_ONLY"))
    assert "ALICE_ONLY" in first and "BOB_ONLY" not in first
    assert "BOB_ONLY" in second and "ALICE_ONLY" not in second
    assert "AUDIT_CHILD_ENV" not in os.environ
