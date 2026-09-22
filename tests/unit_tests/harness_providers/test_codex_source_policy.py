# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in startup source admission and compatibility regressions."""

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from openjiuwen.harness_protocol import (
    HarnessProtocolError,
    HostCapability,
    McpServerConfig,
    McpTransport,
    ResumePolicy,
)
from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig
from openjiuwen.harness_providers.codex.source_policy import (
    restricted_startup_overrides,
    validate_source_config,
    validate_startup_sources,
)
from openjiuwen.harness_providers.skills import SkillSource
from tests.unit_tests.harness_providers.test_codex import _context, _install_fake_sdk


@pytest.fixture
def policy(tmp_path):
    for name in ("work/.git", "home", "codex", "allowed", "outside"):
        (tmp_path / name).mkdir(parents=True)
    config = CodexHarnessConfig(
        inherit_process_env=False, startup_source_roots=(str(tmp_path / "work"), str(tmp_path / "allowed")),
        env={"HOME": str(tmp_path / "home"), "CODEX_HOME": str(tmp_path / "codex"), "PATH": "/usr/bin:/bin"},
    )
    context = _context(cwd=str(tmp_path / "work"), interactions=object(),
                       host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}))
    return config, context


@pytest.mark.parametrize("value", [[], "a", [None], [""], 1])
def test_invalid_source_root_configuration(value):
    with pytest.raises(TypeError, match="startup_source_roots"):
        CodexHarnessConfig(startup_source_roots=value)


def test_root_array_roundtrip_and_legacy_opt_out():
    assert CodexHarnessConfig.from_mapping({"startup_source_roots": ["/a"]}).startup_source_roots == ("/a",)
    assert validate_startup_sources(CodexHarnessConfig(), _context()) is None


@pytest.mark.parametrize("change", ["inherit", "bypass", "handler", "capability", "home_override", "relative", "root"])
def test_restricted_preconditions_fail_closed(policy, change):
    config, context = policy
    if change in ("inherit", "bypass"):
        field = "inherit_process_env" if change == "inherit" else "bypass_approvals_and_sandbox"
        config = replace(config, **{field: True})
    elif change == "handler":
        context = replace(context, interactions=None)
    elif change == "capability":
        context = replace(context, host_capabilities=frozenset())
    elif change == "home_override":
        context = replace(context, env={"HOME": "/different"})
    else:
        config = replace(config, startup_source_roots=("relative" if change == "relative" else "/",))
    with pytest.raises(HarnessProtocolError):
        validate_startup_sources(config, context)


@pytest.mark.parametrize("key", [
    "mcp_servers", "plugins", "hooks", "notify", "profiles", "projects", "model_instructions_file",
])
@pytest.mark.parametrize("layer", ["file", "override", "thread"])
def test_unsupported_configuration_sources_rejected_before_start(policy, key, layer):
    config, context = policy
    if layer == "file":
        (Path(config.env["CODEX_HOME"]) / "config.toml").write_text(f'{key}="fixture"\n')
    elif layer == "override":
        config = replace(config, config_overrides=(f'{key}="fixture"',))
    else:
        config = replace(config, thread_config={key: "fixture"})
    with pytest.raises(HarnessProtocolError, match="does not admit"):
        validate_startup_sources(config, context)


@pytest.mark.parametrize("value", [1, -1, True])
def test_agents_loading_override_cannot_reenable(policy, value):
    config, context = policy
    with pytest.raises(HarnessProtocolError, match="project_doc_max_bytes"):
        validate_startup_sources(replace(config, thread_config={"project_doc_max_bytes": value}), context)


@pytest.mark.parametrize("source", ["explicit", "discovered", "config_link"])
@pytest.mark.asyncio
async def test_unadmitted_sources_fail_before_read_copy_or_client(policy, tmp_path, monkeypatch, source):
    config, context = policy
    _, state = _install_fake_sdk(monkeypatch)
    outside = tmp_path / "outside/SKILL.md"
    outside.write_text("---\nname: escape\n---\nforbidden\n")
    if source == "explicit":
        config = replace(config, skills=(SkillSource(str(outside.parent)),))
    elif source == "discovered":
        scan = tmp_path / "work/.agents/skills"
        scan.mkdir(parents=True)
        (scan / "escape").symlink_to(outside.parent)
    else:
        (tmp_path / "codex/config.toml").symlink_to(outside)
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert path.resolve() != outside, "Unadmitted content was read before rejection"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    harness = CodexHarness(config)
    with pytest.raises(HarnessProtocolError, match="outside authorized"):
        await harness.start(context)
    assert not state.clients
    assert not (tmp_path / "work/.agents/skills/installed").exists()


@pytest.mark.parametrize("kind", ["mcp", "plugins", "hooks"])
@pytest.mark.asyncio
async def test_unisolated_execution_sources_never_create_client(policy, tmp_path, monkeypatch, kind):
    config, context = policy
    _, state = _install_fake_sdk(monkeypatch)
    if kind == "mcp":
        context = replace(context, mcp_servers=(McpServerConfig(
            name="blocked", transport=McpTransport.STDIO, command=("/never/run",),
        ),))
    else:
        path = tmp_path / "codex" / ("plugins" if kind == "plugins" else "hooks.json")
        path.mkdir() if kind == "plugins" else path.write_text("{}")
    with pytest.raises(HarnessProtocolError, match="does not admit"):
        await CodexHarness(config).start(context)
    assert not state.clients


def test_authenticated_loopback_http_mcp_is_admitted_and_scope_bound(policy):
    config, context = policy
    config = replace(
        config,
        mcp_default_tools_approval_mode="prompt",
        mcp_required=True,
    )
    first = McpServerConfig(
        name="jiuwenswarm_product_tools",
        transport=McpTransport.HTTP,
        url="http://127.0.0.1:43111/mcp",
        headers={"Authorization": "Bearer " + "a" * 43},
    )
    second = McpServerConfig(
        name="jiuwenswarm_product_tools",
        transport=McpTransport.HTTP,
        url="http://127.0.0.1:43112/mcp",
        headers={"Authorization": "Bearer " + "b" * 43},
    )

    first_scope = validate_startup_sources(
        config,
        replace(
            context,
            mcp_servers=(first,),
            host_capabilities=context.host_capabilities | {HostCapability.MCP_SERVERS},
        ),
    )
    second_scope = validate_startup_sources(
        config,
        replace(
            context,
            mcp_servers=(second,),
            host_capabilities=context.host_capabilities | {HostCapability.MCP_SERVERS},
        ),
    )

    assert first_scope == second_scope


@pytest.mark.parametrize(
    ("server", "approval_mode"),
    [
        (
            McpServerConfig(
                name="remote",
                transport=McpTransport.HTTP,
                url="https://example.invalid/mcp",
                headers={"Authorization": "Bearer " + "a" * 43},
            ),
            "prompt",
        ),
        (
            McpServerConfig(
                name="weak",
                transport=McpTransport.HTTP,
                url="http://127.0.0.1:43111/mcp",
                headers={"Authorization": "Bearer short"},
            ),
            "prompt",
        ),
        (
            McpServerConfig(
                name="auto",
                transport=McpTransport.HTTP,
                url="http://127.0.0.1:43111/mcp",
                headers={"Authorization": "Bearer " + "a" * 43},
            ),
            "auto",
        ),
    ],
)
def test_restricted_managed_mcp_rejects_remote_weak_or_unreviewed(
    policy,
    server,
    approval_mode,
):
    config, context = policy
    config = replace(config, mcp_default_tools_approval_mode=approval_mode)
    with pytest.raises(HarnessProtocolError):
        validate_startup_sources(config, replace(context, mcp_servers=(server,)))


def test_effective_managed_mcp_must_remain_loopback_required_and_prompt(tmp_path):
    values = {
        "default_permissions": "a0",
        **tomllib.loads("\n".join(restricted_startup_overrides(str(tmp_path)))),
        "mcp_servers": {
            "jiuwenswarm_product_tools": {
                "url": "http://127.0.0.1:43111/mcp",
                "http_headers": {"Authorization": "Bearer " + "a" * 43},
                "required": True,
                "default_tools_approval_mode": "prompt",
                "startup_timeout_sec": 10,
                "enabled": True,
                "environment_id": "local",
                "tool_timeout_sec": None,
            }
        },
    }

    validate_source_config(
        values,
        effective=True,
        cwd=str(tmp_path),
        managed_mcp_names=("jiuwenswarm_product_tools",),
    )
    missing_auth = {
        **values,
        "mcp_servers": {
            "jiuwenswarm_product_tools": {
                key: value
                for key, value in values["mcp_servers"]["jiuwenswarm_product_tools"].items()
                if key != "http_headers"
            }
        },
    }
    with pytest.raises(HarnessProtocolError, match="lost loopback authentication"):
        validate_source_config(
            missing_auth,
            effective=True,
            cwd=str(tmp_path),
            managed_mcp_names=("jiuwenswarm_product_tools",),
        )
    ambient = {
        **values,
        "mcp_servers": {
            **values["mcp_servers"],
            "ambient": values["mcp_servers"]["jiuwenswarm_product_tools"],
        },
    }
    with pytest.raises(HarnessProtocolError, match="server set changed"):
        validate_source_config(
            ambient,
            effective=True,
            cwd=str(tmp_path),
            managed_mcp_names=("jiuwenswarm_product_tools",),
        )
    changed_defaults = {
        **values,
        "mcp_servers": {
            "jiuwenswarm_product_tools": {
                **values["mcp_servers"]["jiuwenswarm_product_tools"],
                "environment_id": "remote",
            }
        },
    }
    with pytest.raises(HarnessProtocolError, match="effective defaults"):
        validate_source_config(
            changed_defaults,
            effective=True,
            cwd=str(tmp_path),
            managed_mcp_names=("jiuwenswarm_product_tools",),
        )
    with pytest.raises(HarnessProtocolError):
        validate_source_config(values, effective=True, cwd=str(tmp_path))


def test_effective_configuration_requires_named_profile_and_disabled_agents(tmp_path):
    for values in ({}, {"default_permissions": "a0", "project_doc_max_bytes": 2},
                   {"default_permissions": "a0", "project_doc_max_bytes": 0, "mcp_servers": {"bad": {}}}):
        with pytest.raises(HarnessProtocolError):
            validate_source_config(values, effective=True, cwd=str(tmp_path))
    values = {"default_permissions": "a0", **tomllib.loads("\n".join(restricted_startup_overrides(str(tmp_path))))}
    validate_source_config(values, effective=True, cwd=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["widen", "disable"])
async def test_resume_source_change_rejected_before_copy_or_client(policy, tmp_path, monkeypatch, change):
    config, context = policy
    _, state = _install_fake_sdk(monkeypatch)
    state.security_config = {"default_permissions": "a0", "permissions": {"a0": {}},
                             **tomllib.loads("\n".join(restricted_startup_overrides(context.cwd)))}
    harness = CodexHarness(config)
    await harness.start(context)
    checkpoint = await harness.export_checkpoint()
    await harness.stop()
    assert checkpoint.data["startup_source_fingerprint"]
    new_roots = (*config.startup_source_roots, str(tmp_path / "outside")) if change == "widen" else None
    resumed = CodexHarness(replace(config, startup_source_roots=new_roots))
    with pytest.raises(HarnessProtocolError, match="source scope changed"):
        await resumed.start(replace(context, checkpoint=checkpoint, resume_policy=ResumePolicy.REQUIRE_RESUME))
    assert len(state.clients) == 1


@pytest.mark.parametrize("override", [
    'features.remote_plugin=true', 'features.plugins=true',
    'features.unknown_future_source=true', 'allow_login_shell=true',
])
def test_ambient_source_feature_overrides_rejected(policy, override):
    config, context = policy
    with pytest.raises(HarnessProtocolError):
        validate_startup_sources(replace(config, config_overrides=(override,)), context)


@pytest.mark.parametrize("change", ["missing", "trusted", "parent", "extra", "plugin", "unknown"])
def test_effective_trust_must_match_only_the_untrusted_cwd(tmp_path, change):
    values = {"default_permissions": "a0", **tomllib.loads("\n".join(restricted_startup_overrides(str(tmp_path))))}
    if change == "missing":
        del values["projects"]
    elif change == "trusted":
        values["projects"][str(tmp_path)]["trust_level"] = "trusted"
    elif change == "parent":
        values["projects"] = {str(tmp_path.parent): {"trust_level": "untrusted"}}
    elif change == "extra":
        values["projects"][str(tmp_path.parent)] = {"trust_level": "trusted"}
    elif change == "plugin":
        values["features"]["plugins"] = True
    else:
        values["projects"][str(tmp_path)]["unknown"] = True
    with pytest.raises(HarnessProtocolError):
        validate_source_config(values, effective=True, cwd=str(tmp_path))


def test_generated_trust_is_ephemeral_and_not_an_admitted_source(policy):
    config, context = policy
    values = tomllib.loads("\n".join(restricted_startup_overrides(context.cwd)))
    assert values["projects"] == {context.cwd: {"trust_level": "untrusted"}}
    assert values["features"]["plugins"] is False
    (Path(config.env["CODEX_HOME"]) / "config.toml").write_text(
        f'[projects."{context.cwd}"]\ntrust_level="untrusted"\n',
    )
    with pytest.raises(HarnessProtocolError, match="projects"):
        validate_startup_sources(config, context)
