# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Actual managed JS hook + original Provider objects; no CLI or network."""

import asyncio
import copy
import json
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openjiuwen.harness_providers.opencode import OpenCodeHarnessConfig, OpenCodeModelConfig
from openjiuwen.harness_providers.opencode.errors import OpenCodeError
from openjiuwen.harness_providers.opencode.model_gateway import SOURCE_HEADERS, OpenCodeModelGateway
from openjiuwen.harness_providers.opencode.options import native_config, validate_readback
from openjiuwen.harness_providers.opencode.preflight import gate_source, preflight_fingerprint, stage_preflight
from tests.unit_tests.harness_providers.test_opencode_preflight import endpoint, prepared


def gateway(**changes):
    values = dict(
        url="http://127.0.0.1:12345/model/v1",
        token="fixture-local-" + "x" * 32,
        generation="generation-one",
        model="fixture",
        destination="https://model.invalid/v1",
    )
    values.update(changes)
    return OpenCodeModelGateway(**values)


def configured():
    harness = prepared()
    harness._config = replace(harness._config, model=OpenCodeModelConfig("fixture", "https://model.invalid/v1"))
    harness._preflight.endpoint = replace(harness._preflight.endpoint, model_gateway=gateway())

    async def request(method, path):
        assert (method, path) == ("GET", "/session/ses_one/message")
        return [
            {
                "info": {
                    "id": "msg_root",
                    "role": "user",
                    "sessionID": "ses_one",
                    "agent": "build",
                    "model": {"providerID": "openjiuwen", "modelID": "fixture"},
                },
                "parts": [],
            }
        ]

    harness._transport = SimpleNamespace(request=request)
    return harness


def headers():
    return dict(zip(SOURCE_HEADERS, ("ses_one", "msg_root", "generation-one", "build", "fixture", "openjiuwen")))


async def capture(harness, values=None, **changes):
    kwargs = dict(method="POST", path="/model/v1/chat/completions", model="fixture")
    kwargs.update(changes)
    return await harness._capture_model_source(headers() if values is None else values, **kwargs)


@pytest.mark.asyncio
async def test_retry_has_new_proof_over_original_turn_and_copy_is_not_proof():
    h = configured()
    one, two = await capture(h), await capture(h)
    assert one is not two and one.turn_id == two.turn_id == "turn-one"
    assert h._is_model_source_current(one) and h._is_model_source_current(two)
    assert not h._is_model_source_current(replace(one))
    assert not h._is_model_source_current(copy.copy(one))
    assert "fixture-local" not in repr(one)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["turn", "context", "transport", "gateway", "clear", "close", "abort", "stop", "config"]
)
async def test_capture_fixes_original_source_before_first_await(change):
    h = configured()
    old = h._transport.request
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(*args):
        result = await old(*args)
        entered.set()
        await release.wait()
        return result

    h._transport.request = pending
    task = asyncio.create_task(capture(h))
    await entered.wait()
    if change == "turn":
        h._active_turn = SimpleNamespace(turn_id="turn-two", abort_requested=False, stop_requested=False)
        h._preflight.begin(h.active_turn, "msg_root")
    elif change == "context":
        h._context = replace(h.context, agent_name="other")
    elif change == "transport":
        h._transport = SimpleNamespace(request=old)
    elif change == "gateway":
        h._preflight.endpoint = replace(h._preflight.endpoint, model_gateway=replace(gateway()))
    elif change in {"clear", "close"}:
        getattr(h._preflight, change)()
    elif change in {"abort", "stop"}:
        setattr(h.active_turn, change + "_requested", True)
    else:
        h._config = replace(h._config)
    release.set()
    assert await task is None


@pytest.mark.asyncio
async def test_old_first_delivery_never_borrows_new_turn_and_after_capture_recheck():
    h = configured()
    old = await capture(h)
    h._active_turn = SimpleNamespace(turn_id="turn-two", abort_requested=False, stop_requested=False)
    h._preflight.begin(h.active_turn, "msg_next")
    assert not h._is_model_source_current(old)
    assert await capture(h) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [(key, "wrong") for key in SOURCE_HEADERS])
async def test_wrong_original_header_denied_without_native_io(field, value):
    h = configured()

    async def must_not_read(*_):
        pytest.fail("invalid source must not access native persistence")

    h._transport.request = must_not_read
    values = headers()
    values[field] = value
    assert await capture(h, values) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"method": "GET"}, {"path": "/model/v1/responses"}, {"model": "other"}])
async def test_actual_post_path_and_body_model_are_bound(updates):
    assert await capture(configured(), **updates) is None


@pytest.mark.asyncio
async def test_hook_omission_and_extra_reserved_header_fail_closed():
    h = configured()
    assert await capture(h, {}) is None
    assert await capture(h, {**headers(), "x-openjiuwen-other": "extra"}) is None
    assert await capture(h, {**headers(), "X-OpenJiuwen-Root": "msg_root"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "assistant"),
        ("agent", "compaction"),
        ("sessionID", "other"),
        ("model", {"providerID": "other", "modelID": "fixture"}),
    ],
)
async def test_native_root_facts_must_prove_primary_request(field, value):
    h = configured()
    original = h._transport.request

    async def changed(*args):
        result = await original(*args)
        result[0]["info"][field] = value
        return result

    h._transport.request = changed
    assert await capture(h) is None


def test_gateway_config_contains_only_local_auth_and_readback_rejects_override():
    h = configured()
    gw = gateway()
    generated = native_config(h._config, governed=True, model_gateway=gw)
    options = generated["provider"]["openjiuwen"]["options"]
    assert options == {"baseURL": gw.url, "apiKey": gw.token}
    assert gw.destination not in json.dumps(generated)
    actual = copy.deepcopy(generated)
    actual["agent"]["title"].update(options={}, permission={})
    validate_readback(actual, generated)
    for mutate in (
        lambda data: data["provider"]["openjiuwen"]["options"].update(baseURL=gw.destination),
        lambda data: data["provider"]["openjiuwen"].update(headers={"x-openjiuwen-root": "forged"}),
        lambda data: data.update(plugin=["file:///unknown.js"]),
        lambda data: data.update(small_model="other/model"),
        lambda data: data.update(enabled_providers=["other"]),
    ):
        changed = copy.deepcopy(actual)
        mutate(changed)
        with pytest.raises(OpenCodeError):
            validate_readback(changed, generated)


@pytest.mark.parametrize(
    "changes", [{"api_key": "synthetic-upstream-secret"}, {"model": "other"}, {"api_base": "https://other.invalid/v1"}]
)
def test_conflicting_config_fails_before_allocation(changes):
    h = configured()
    h._config = replace(h._config, model=replace(h._config.model, **changes))
    with pytest.raises(OpenCodeError, match="model_gateway_config_conflict"):
        h._validate_context(h.context)
    assert h._server is None


def test_gateway_not_configurable_through_json_and_legacy_static_model_unchanged():
    with pytest.raises(ValueError):
        OpenCodeHarnessConfig.from_mapping({"model_gateway": {"url": "http://127.0.0.1:1"}})
    for field in ("headers", "fallback", "options"):
        with pytest.raises(ValueError):
            OpenCodeModelConfig.from_mapping({"model": "m", "api_base": "https://model.invalid", field: {}})
    conf = OpenCodeHarnessConfig(model=OpenCodeModelConfig("m", "https://model.invalid", "synthetic-legacy"))
    assert native_config(conf)["provider"]["openjiuwen"]["options"]["apiKey"] == "synthetic-legacy"
    import openjiuwen.harness_providers.opencode as package

    assert "OpenCodeModelGateway" not in package.__all__


def test_gateway_is_same_owned_endpoint_and_stable_fingerprint_excludes_local_token():
    ep = endpoint(model_gateway=gateway())
    shifted = replace(
        ep,
        token="y" * 40,
        generation="generation-two",
        model_gateway=replace(gateway(), token="z" * 40, generation="generation-two"),
    )
    assert preflight_fingerprint(ep) == preflight_fingerprint(shifted)
    for gw in (replace(gateway(), generation="foreign"), replace(gateway(), url="http://127.0.0.1:12346/v1")):
        with pytest.raises(ValueError):
            endpoint(model_gateway=gw)


def test_actual_js_hook_and_original_native_loader_registration(tmp_path):
    node = shutil.which("node")
    assert node, "stable requires Node for actual managed plugin execution"
    ep = endpoint(model_gateway=gateway())
    # Uses original package snapshot path, not an alternate loader.
    staged = stage_preflight(tmp_path, ep, 1)
    assert staged.specs
    source = tmp_path / "probe.mjs"
    source.write_text(
        gate_source(ep, 1)
        + r"""
const plugin = await Preflight()
const input = () => ({sessionID:"ses_one", agent:"build", model:{id:"fixture",providerID:"openjiuwen"},
  message:{id:"msg_root",sessionID:"ses_one",role:"user",agent:"build",
    model:{modelID:"fixture",providerID:"openjiuwen"}}})
const output = {headers:{}}
await plugin["chat.headers"](input(), output)
if (output.headers["x-openjiuwen-root"] !== "msg_root" || !Object.isFrozen(output.headers)) throw Error("missing proof")
for (const variant of ["aux", "model", "reserved", "role", "other-session"]) {
  const i=input(), o={headers:{}}
  if (variant==="aux") i.agent="compaction"
  if (variant==="model") i.model.id="other"
  if (variant==="reserved") o.headers["X-OpenJiuwen-Root"]="forged"
  if (variant==="role") i.message.role="assistant"
  if (variant==="other-session") i.message.sessionID="other"
  let denied=false
  try { await plugin["chat.headers"](i,o) } catch { denied=true }
  if (!denied) throw Error("accepted " + variant)
}
console.log("allowed-primary; rejected-five-invalid-sources")
"""
    )
    result = subprocess.run([node, str(source)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert "allowed-primary" in result.stdout


@pytest.mark.asyncio
async def test_direct_server_rejects_secret_before_allocating_resources():
    from openjiuwen.harness_providers.opencode.server import ManagedServer

    h = configured()
    server = ManagedServer(
        replace(h._config, model=replace(h._config.model, api_key="synthetic")),
        h.context,
        preflight_endpoint=h._preflight.endpoint,
    )
    with pytest.raises(OpenCodeError, match="model_gateway_config_conflict"):
        await server.start()
    assert server.scope is None and server.process is None


def test_legacy_explicit_chat_headers_uses_same_loader_and_failure_is_unadmitted(tmp_path):
    from openjiuwen.harness_providers.opencode.native_plugins import (
        OpenCodeNativePluginConfig,
        opencode_plugin_content_digest,
        stage_native_plugins,
        validate_native_plugin_packages,
    )

    node = shutil.which("node")
    assert node
    for valid in (True, False):
        base = tmp_path / str(valid)
        source = base / "source"
        source.mkdir(parents=True)
        hook = '"chat.headers"' if valid else '"permission.ask"'
        (source / "hook.js").write_text(
            "export const Hook = async () => ({"
            + hook
            + ': async (input, output) => { output.headers.fixture = "safe" }})'
        )
        plugin = OpenCodeNativePluginConfig(
            "headers",
            "local",
            str(source),
            "1",
            opencode_plugin_content_digest(source),
            "hook.js",
            "Hook",
            ("chat.headers",),
        )
        root = base / "runtime"
        (root / "tmp").mkdir(parents=True)
        stage = stage_native_plugins(root, (plugin,), fingerprint=validate_native_plugin_packages((plugin,)))
        script = (
            "const {ManagedOpenCodePlugin}=await import("
            + json.dumps(stage.specs[0])
            + ");"
            + (
                "const hooks=await ManagedOpenCodePlugin({}); const out={headers:{}}; "
                'await hooks["chat.headers"]({},out); if(out.headers.fixture!=="safe") throw Error("missing");'
            )
        )
        result = subprocess.run(
            [node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=10, check=False
        )
        if valid:
            assert result.returncode == 0, result.stderr
            stage.verify_inventory()
        else:
            assert result.returncode != 0 and not stage.inventory_ready()
            with pytest.raises(OpenCodeError, match="native_plugin_inventory_invalid"):
                stage.verify_inventory()
