# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Synthetic preflight authority and real JS execution; no CLI or network."""

import asyncio
import json
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openjiuwen.harness_protocol import (
    HarnessContext,
    HarnessProtocolError,
    HostCapability,
    McpServerConfig,
    McpTransport,
)
from openjiuwen.harness_providers.opencode import OpenCodeHarness, OpenCodeHarnessConfig, OpenCodeModelConfig
from openjiuwen.harness_providers.opencode.preflight import (
    PRODUCT_TICKET_FIELD,
    OpenCodePreflightEndpoint,
    PreflightGate,
    gate_source,
    stage_preflight,
)


def endpoint(**updates):
    return OpenCodePreflightEndpoint(
        **{
            "url": "http://127.0.0.1:12345/private-preflight",
            "token": "synthetic-token-" + "x" * 32,
            "generation": "generation-one",
            **updates,
        }
    )


def config(**updates):
    return OpenCodeHarnessConfig(model=OpenCodeModelConfig("fixture", "http://127.0.0.1:1"), **updates)


def context(callback):
    return HarnessContext(
        "oc",
        "agent",
        "host",
        "",
        cwd="/work",
        tool_authorizer=callback,
        host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
        interactions=SimpleNamespace(),
    )


def native_messages(body=None, root="msg_root"):
    body = body or payload()
    return [
        {"info": {"id": root, "role": "user", "sessionID": body["session_id"]}, "parts": []},
        {
            "info": {"id": "msg_assistant", "role": "assistant", "parentID": root, "sessionID": body["session_id"]},
            "parts": [
                {
                    "id": "prt_tool",
                    "type": "tool",
                    "messageID": "msg_assistant",
                    "sessionID": body["session_id"],
                    "callID": body["call_id"],
                    "tool": body["tool"],
                    "state": {"status": "running", "input": body["args"]},
                }
            ],
        },
    ]


def prepared(callback=None):
    async def allow(_):
        return True

    harness = OpenCodeHarness(config())
    harness.bind_preflight_endpoint(endpoint())
    harness._context = context(callback or allow)
    harness._session_id = "ses_one"
    harness._active_turn = SimpleNamespace(turn_id="turn-one", abort_requested=False, stop_requested=False)

    async def request(method, path):
        assert (method, path) == ("GET", "/session/ses_one/message")
        return native_messages()

    async def close():
        pass

    harness._transport = SimpleNamespace(request=request, close=close)
    harness._preflight.begin(harness._active_turn, "msg_root")
    return harness


def payload(**updates):
    return {
        "version": 1,
        "generation": "generation-one",
        "nonce": "a" * 32,
        "session_id": "ses_one",
        "call_id": "call_one",
        "tool": "write",
        "args": {"filePath": "/work/file", "content": "synthetic"},
        **updates,
    }


@pytest.mark.parametrize(
    "values",
    [
        {"url": "https://example.test/x"},
        {"url": "http://localhost:12345/x"},
        {"url": "http://127.0.0.1/x"},
        {"url": "http://u:p@127.0.0.1:12/x"},
        {"url": "http://127.0.0.1:12/x?q=1"},
        {"url": "http://127.0.0.1:12/x#f"},
        {"token": "short"},
        {"token": "a" * 32 + "\n"},
        {"generation": "bad generation"},
    ],
)
def test_endpoint_rejects_unmanaged_sources(values):
    with pytest.raises(ValueError):
        endpoint(**values)


def test_endpoint_is_private_and_binding_closes_before_start():
    value = endpoint()
    assert value.token not in repr(value)
    harness = OpenCodeHarness(config())
    harness.bind_preflight_endpoint(value)
    with pytest.raises(HarnessProtocolError):
        harness.bind_preflight_endpoint(value)
    other = OpenCodeHarness(config())
    other._validate_context(HarnessContext("oc", "agent", "host", ""))
    with pytest.raises(HarnessProtocolError):
        other.bind_preflight_endpoint(value)
    with pytest.raises(ValueError):
        OpenCodeHarnessConfig.from_mapping({"preflight_endpoint": value})


def test_governed_requires_endpoint_and_legacy_remains_available():
    async def allow(_):
        return True

    with pytest.raises(HarnessProtocolError, match="preflight endpoint"):
        OpenCodeHarness(config())._validate_context(context(allow))
    harness = OpenCodeHarness(config())
    harness.bind_preflight_endpoint(endpoint())
    harness._validate_context(context(allow))
    with pytest.raises(HarnessProtocolError, match="fresh governed"):
        harness._validate_context(context(allow))
    OpenCodeHarness(config())._validate_context(HarnessContext("oc", "agent", "host", ""))


@pytest.mark.asyncio
async def test_original_args_and_callback_are_frozen_across_permission_checks():
    seen = []

    async def allow(request):
        seen.append(request)
        return True

    harness = prepared(allow)
    body = payload()
    assert (await harness.authorize_preflight(body))["allowed"]
    body["args"]["filePath"] = "/other"
    record = harness._preflight.claim(
        harness,
        "call_one",
        {
            "sessionID": "ses_one",
            "tool": {"messageID": "msg_assistant"},
            "permission": "edit",
            "metadata": {"filepath": "/work/file"},
        },
    )
    assert record is not None and await harness._preflight.check(harness, record)
    assert seen[0] is seen[1]
    assert seen[1].tool_name == "write" and seen[1].arguments["filePath"] == "/work/file"
    assert harness._preflight.claim(harness, "call_one", {}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["turn", "context", "session", "abort", "stop", "clear", "close", "generation"])
async def test_authority_await_does_not_switch_to_new_context_or_turn(change):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def delayed(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return True

    harness = prepared(delayed)
    task = asyncio.create_task(harness.authorize_preflight(payload()))
    await entered.wait()
    if change == "turn":
        harness._active_turn = SimpleNamespace(turn_id="new", abort_requested=False, stop_requested=False)
    elif change == "context":
        harness._context = replace(harness.context, agent_name="other")
    elif change == "session":
        harness._session_id = "ses_other"
    elif change == "abort":
        harness.active_turn.abort_requested = True
    elif change == "stop":
        harness._stopping = True
    elif change == "generation":
        harness._preflight.endpoint = replace(harness._preflight.endpoint, generation="next")
    else:
        getattr(harness._preflight, change)()
    release.set()
    assert not (await task)["allowed"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_replay_is_rejected_during_await_and_in_later_turn():
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(_):
        entered.set()
        await release.wait()
        return True

    harness = prepared(delayed)
    first = asyncio.create_task(harness.authorize_preflight(payload()))
    await entered.wait()
    assert not (await harness.authorize_preflight(payload()))["allowed"]
    release.set()
    assert (await first)["allowed"]
    harness._preflight.clear()
    assert harness._preflight.records == {}
    harness._active_turn = SimpleNamespace(turn_id="later", abort_requested=False, stop_requested=False)
    harness._preflight.begin(harness.active_turn, "msg_root")
    assert not (await harness.authorize_preflight(payload()))["allowed"]
    assert not (await harness.authorize_preflight(payload(call_id="other")))["allowed"]
    assert not (await harness.authorize_preflight(payload(nonce="b" * 32)))["allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"version": True},
        {"generation": "old"},
        {"session_id": "ses_other"},
        {"actor": "forged"},
        {"tool": "apply_patch"},
        {"args": {"filePath": "relative", "content": "a"}},
        {"args": {"filePath": "/work/../private", "content": "a"}},
        {"args": {"filePath": "/work/f", "content": {}, "unknown": "a"}},
        {"nonce": "bad"},
        {"call_id": ""},
    ],
)
async def test_malformed_scope_and_unproven_tools_never_reach_authority(updates):
    calls = []

    async def deny(request):
        calls.append(request)
        return False

    harness = prepared(deny)
    assert not (await harness.authorize_preflight(payload(**updates)))["allowed"]
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"permission": "read"},
        {"sessionID": "ses_other"},
        {"metadata": {"filepath": "/other"}},
    ],
)
async def test_permission_cannot_change_executor_or_path(mutation):
    harness = prepared()
    assert (await harness.authorize_preflight(payload()))["allowed"]
    props = {"sessionID": "ses_one", "permission": "edit", "metadata": {"filepath": "/work/file"}, **mutation}
    assert harness._preflight.claim(harness, "call_one", props) is None


@pytest.mark.asyncio
async def test_tombstone_capacity_never_evicts_and_authority_failure_denies(monkeypatch):
    from openjiuwen.harness_providers.opencode import preflight

    monkeypatch.setattr(preflight, "_MAX_GENERATION_CALLS", 1)
    harness = prepared()
    assert (await harness.authorize_preflight(payload()))["allowed"]
    harness._preflight.begin(harness.active_turn, "msg_root")
    assert not (await harness.authorize_preflight(payload(call_id="next", nonce="b" * 32)))["allowed"]

    async def broken(_):
        raise RuntimeError("synthetic failure")

    assert not (await prepared(broken).authorize_preflight(payload()))["allowed"]


def test_stage_uses_existing_inventory_and_detects_gate_tampering(tmp_path):
    (tmp_path / "tmp").mkdir()
    stage = stage_preflight(tmp_path, endpoint(), 1)
    assert len(stage.specs) == 1
    assert not stage.inventory_ready()
    stage.verify_files()
    inventory, expected = stage.inventories[0]
    assert expected["hooks"] == ["chat.headers", "tool.execute.before"] and expected["tools"] == []
    inventory.write_text(json.dumps(expected))
    stage.verify_inventory()
    path = stage.packages[0][0] / "gate.js"
    assert path.stat().st_mode & 0o777 == 0o400
    path.chmod(0o600)
    path.write_text("changed")
    with pytest.raises(Exception, match="native_plugin_stage_drift"):
        stage.verify_files()


def test_actual_generated_js_fixes_original_args_and_fails_closed():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the generated plugin with synthetic fetch")
    script = """
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) Object.defineProperty(globalThis,'crypto',{value:webcrypto});
const source = SOURCE;
const {Preflight} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const hook = (await Preflight())["tool.execute.before"];
let calls = 0;
const make = () => [{tool:'write',sessionID:'ses_one',callID:'call_one'},
  {args:{filePath:'/work/file',content:'synthetic'}}];
globalThis.fetch = async (_, opts) => {
  calls++; const body = JSON.parse(opts.body);
  assert(Object.isFrozen(original));
  assert.throws(()=>{original.filePath="/during-await"});
  assert.equal(body.args.filePath, '/work/file');
  assert.equal(opts.redirect, 'error');
  return {status:200,text:async()=>JSON.stringify({allowed:true,nonce:body.nonce})};
};
const [input, output] = make(), original = output.args;
await hook(input, output);
assert.equal(original, output.args);
assert(Object.isFrozen(original)); assert(Object.isFrozen(output));
assert.throws(()=>{original.filePath='/other'});
assert.throws(()=>{output.args={filePath:'/other'}});
assert.throws(()=>{input.tool='read'});
assert.equal(calls,1);
for (const answer of [false, 'true', null]) {
  globalThis.fetch=async (_,o)=>({status:200,
    text:async()=>JSON.stringify({allowed:answer,nonce:JSON.parse(o.body).nonce})});
  await assert.rejects(hook(...make()),/mandatory native preflight denied/);
}
for (const fake of [async()=>{throw Error('network')}, async()=>({status:302}),
  async()=>({status:200,text:async()=>JSON.stringify({allowed:true,nonce:'wrong'})}),
  async()=>({status:200,text:async()=>'{bad'})]) {
  globalThis.fetch=fake; await assert.rejects(hook(...make()));
}
globalThis.fetch=async()=>{throw Error('must not fetch')};
for (const mutate of [x=>{x[0].tool='apply_patch'},x=>{x[1].args.filePath='../escape'},
  x=>{x[1].args.extra='bad'},x=>{Object.defineProperty(x[1].args,'filePath',{get:()=>{throw Error('getter')}})}]) {
  const value=make(); mutate(value); await assert.rejects(hook(...value),/mandatory native preflight denied/);
}
globalThis.fetch=async(_,o)=>new Promise((_,reject)=>o.signal.addEventListener('abort',()=>reject(Error('timeout'))));
await assert.rejects(hook(...make()),/mandatory native preflight denied/);
const productSource = PRODUCT_SOURCE;
const module = await import('data:text/javascript;base64,' + Buffer.from(productSource).toString('base64'));
const productHook = (await module.Preflight())["tool.execute.before"];
let productCalls = 0;
globalThis.fetch=async(_,o)=>{productCalls++;return {status:200,
  text:async()=>JSON.stringify({allowed:true,nonce:JSON.parse(o.body).nonce,ticket:"pt_"+"a".repeat(64)})}};
const productArgs={query:{values:[1,true,null]}};
await productHook({tool:'jiuwenswarm_product_tools_workflow',sessionID:'ses_one',callID:'call_product'},
  {args:productArgs});
assert(Object.isFrozen(productArgs.query.values));
assert(Object.isFrozen(productArgs));
assert.equal(productArgs.__openjiuwen_product_ticket, 'pt_'+'a'.repeat(64));
assert.equal(JSON.parse(JSON.stringify(productArgs)).__openjiuwen_product_ticket, 'pt_'+'a'.repeat(64));
assert.throws(()=>{productArgs.__openjiuwen_product_ticket='forged'});
assert.throws(()=>productArgs.query.values.push('late'));
await assert.rejects(productHook({tool:'jiuwenswarm_product_tools_unknown',sessionID:'ses_one',callID:'call_other'},
  {args:{}}));
assert.equal(productCalls,1);
await assert.rejects(productHook({tool:'jiuwenswarm_product_tools_workflow',sessionID:'ses_one',callID:'call_forged'},
  {args:{__openjiuwen_product_ticket:'a'.repeat(64)}}));
assert.equal(productCalls,1);
for (const ticket of [undefined, 'a'.repeat(32), 12, null]) {
  globalThis.fetch=async(_,o)=>({status:200,text:async()=>JSON.stringify({allowed:true,nonce:JSON.parse(o.body).nonce,ticket})});
  await assert.rejects(productHook(
    {tool:'jiuwenswarm_product_tools_workflow',sessionID:'ses_one',callID:'call_bad'}, {args:{}}));
}
console.log('generated JS synthetic cases passed');
""".replace("PRODUCT_SOURCE", json.dumps(gate_source(endpoint(product_tool_names=("workflow",)), 0.1)))
    script = script.replace("SOURCE", json.dumps(gate_source(endpoint(), 0.1)))
    result = subprocess.run([node, "--input-type=module"], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "synthetic cases passed" in result.stdout


@pytest.mark.asyncio
async def test_product_inventory_uses_scope_and_final_gateway_not_native_authority():
    calls = []

    async def native_only(request):
        calls.append(request)
        return False

    harness = prepared(native_only)
    harness._preflight = PreflightGate(endpoint(product_tool_names=("read", "workflow")))
    harness._context = replace(
        harness.context,
        mcp_servers=(
            McpServerConfig(
                name="jiuwenswarm_product_tools",
                transport=McpTransport.HTTP,
                url="http://127.0.0.1:12345/mcp",
                headers={"Authorization": "Bearer " + harness._preflight.endpoint.token},
            ),
        ),
    )
    harness._preflight.begin(harness.active_turn, "msg_root")
    tool = "jiuwenswarm_product_tools_read"
    body = payload(tool=tool, args={"query": {"nested": [1, True, None]}})

    async def product_request(method, path):
        return native_messages(body)

    harness._transport = SimpleNamespace(request=product_request)
    assert (await harness.authorize_preflight(body))["allowed"]
    record = harness._preflight.claim(
        harness,
        "call_one",
        {
            "sessionID": "ses_one",
            "tool": {"messageID": "msg_assistant"},
            "permission": tool,
            "metadata": {},
        },
    )
    assert record.product and await harness._preflight.check(harness, record)
    assert calls == []  # Actual execution still must reach the bound product ToolGateway.
    harness._context = replace(harness.context, agent_name="different")
    assert not await harness._preflight.check(harness, record)
    for name in ("read", "jiuwenswarm_product_tools_unknown", "other_read"):
        result = await harness.authorize_preflight(payload(tool=name, call_id="other", nonce="b" * 32, args={}))
        assert not result["allowed"]


def test_product_server_requires_same_listener_bearer_and_exact_inventory():
    from openjiuwen.harness_providers.opencode.preflight import preflight_fingerprint

    ep = endpoint(product_tool_names=("read",))
    server = McpServerConfig(
        name="jiuwenswarm_product_tools",
        transport=McpTransport.HTTP,
        url="http://127.0.0.1:12345/mcp",
        headers={"Authorization": "Bearer " + ep.token},
    )
    assert ep.admits_servers((server,))
    assert not ep.admits_servers(())
    for updated in (
        replace(server, name="other"),
        replace(server, url="http://127.0.0.1:12346/mcp"),
        replace(server, headers={"Authorization": "Bearer other"}),
    ):
        assert not ep.admits_servers((updated,))
    assert not ep.admits_servers((server, server))
    assert preflight_fingerprint(ep) == preflight_fingerprint(replace(ep, token="y" * 32, generation="new"))
    assert preflight_fingerprint(ep) != preflight_fingerprint(endpoint())
    with pytest.raises(ValueError, match="inventory"):
        endpoint(product_tool_names=("same.name",))
    with pytest.raises(ValueError, match="inventory"):
        endpoint(product_tool_names=("same", "same"))


@pytest.mark.asyncio
async def test_cancelled_request_and_close_cannot_leave_usable_records():
    entered = asyncio.Event()

    async def blocked(_):
        entered.set()
        await asyncio.Event().wait()

    harness = prepared(blocked)
    task = asyncio.create_task(harness.authorize_preflight(payload()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not harness._preflight.records["call_one"].authorized
    await harness._close_session()
    assert harness._preflight.records == {} and harness._preflight.calls == set()
    assert not (await harness.authorize_preflight(payload()))["allowed"]


@pytest.mark.asyncio
async def test_missing_preflight_record_denies_permission_without_ordinary_approval():
    harness = prepared()
    replies = []

    async def reply(*args):
        replies.append(args[-1])

    async def must_not_approve(_):
        pytest.fail("ordinary approval must not run before preflight")

    harness._reply_native = reply
    harness._await_host_interaction = must_not_approve
    denied = []
    await harness._route_permission(
        harness.active_turn,
        SimpleNamespace(mark_denied=denied.append),
        "per_one",
        "call_one",
        {
            "sessionID": "ses_one",
            "tool": {"messageID": "msg_assistant"},
            "permission": "edit",
            "metadata": {"filepath": "/work/file"},
        },
    )
    assert replies == [{"response": "reject"}] and denied == ["call_one"]


@pytest.mark.asyncio
async def test_direct_managed_server_cannot_skip_required_gate_before_allocation():
    from openjiuwen.harness_providers.opencode.server import ManagedServer

    async def allow(_):
        return True

    server = ManagedServer(config(), context(allow))
    with pytest.raises(Exception, match="mandatory_preflight_endpoint_required"):
        await server.start()
    assert server.process is None and server.scope is None


@pytest.mark.asyncio
async def test_readonly_policy_denies_preflight_before_host_callback():
    from openjiuwen.harness_protocol import (
        HarnessRuntimePolicy,
        RuntimeExecutionState,
        RuntimeSurface,
        WorkspaceAccess,
    )

    calls = []

    async def allow(request):
        calls.append(request)
        return True

    harness = prepared(allow)
    harness._context = replace(
        harness.context,
        runtime_policy=HarnessRuntimePolicy(
            "readonly",
            RuntimeSurface.CODE,
            RuntimeExecutionState.PLAN,
            WorkspaceAccess.READ_ONLY,
        ),
    )
    assert not (await harness.authorize_preflight(payload()))["allowed"]
    assert calls == []


def test_governed_skills_and_additional_plugin_fail_explicitly_before_allocation(tmp_path):
    from openjiuwen.harness_providers.skills import SkillSource
    from tests.unit_tests.harness_providers.test_opencode_native_plugins import _plugin, _source

    async def allow(_):
        return True

    for selected in (
        config(skills=(SkillSource("/not-loaded"),)),
        config(native_plugins=(_plugin(_source(tmp_path)),)),
    ):
        harness = OpenCodeHarness(selected)
        harness.bind_preflight_endpoint(endpoint())
        with pytest.raises(HarnessProtocolError, match="additional tool or plugin"):
            harness._validate_context(context(allow))
        assert harness._server is None


@pytest.mark.asyncio
async def test_native_authority_timeout_denies_and_does_not_retry():
    calls = []

    async def stalled(request):
        calls.append(request)
        await asyncio.Event().wait()

    harness = prepared(stalled)
    harness._config = config(request_timeout_s=0.01)
    assert not (await harness.authorize_preflight(payload()))["allowed"]
    assert not (await harness.authorize_preflight(payload()))["allowed"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_first_delivery_from_cancelled_turn_cannot_borrow_next_turn_callback():
    calls = []

    async def allow(request):
        calls.append(request)
        return True

    harness = prepared(allow)
    harness._preflight.clear()
    harness._active_turn = SimpleNamespace(turn_id="new-turn", abort_requested=False, stop_requested=False)
    harness._preflight.begin(harness.active_turn, "msg_new_root")
    # Previously unseen call/nonce; native persistence still attributes it to the old root.
    assert not harness._preflight.calls
    assert not (await harness.authorize_preflight(payload()))["allowed"]
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "missing_root",
        "duplicate_call",
        "duplicate_root",
        "bad_role",
        "bad_parent",
        "bad_session",
        "bad_message",
        "bad_part_session",
        "bad_tool",
        "bad_input",
        "pending",
        "completed",
        "malformed",
        "query_error",
        "wrong_root_role",
    ],
)
async def test_unproven_native_call_never_reaches_host_callback(mutation):
    calls = []

    async def allow(request):
        calls.append(request)
        return True

    harness = prepared(allow)
    messages = native_messages()
    info, part = messages[1]["info"], messages[1]["parts"][0]
    if mutation == "missing":
        messages[1]["parts"] = []
    elif mutation == "missing_root":
        messages.pop(0)
    elif mutation == "duplicate_call":
        messages[1]["parts"].append(dict(part))
    elif mutation == "duplicate_root":
        messages.insert(0, messages[0])
    elif mutation == "bad_role":
        info["role"] = "user"
    elif mutation == "bad_parent":
        info["parentID"] = "msg_old"
    elif mutation == "bad_session":
        info["sessionID"] = "ses_other"
    elif mutation == "bad_message":
        part["messageID"] = "msg_other"
    elif mutation == "bad_part_session":
        part["sessionID"] = "ses_other"
    elif mutation == "bad_tool":
        part["tool"] = "edit"
    elif mutation == "bad_input":
        part["state"]["input"] = {"filePath": "/private", "content": "synthetic"}
    elif mutation in {"pending", "completed"}:
        part["state"]["status"] = mutation
    elif mutation == "malformed":
        messages[1]["parts"] = None
    elif mutation == "wrong_root_role":
        messages[0]["info"]["role"] = "assistant"

    async def request(method, path):
        if mutation == "query_error":
            raise RuntimeError("synthetic query unavailable")
        return messages

    harness._transport = SimpleNamespace(request=request)
    assert not (await harness.authorize_preflight(payload()))["allowed"]
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["turn", "context", "session", "generation", "transport", "root", "abort"])
async def test_scope_change_while_native_query_is_pending_cannot_reach_authority(change):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def allow(request):
        calls.append(request)
        return True

    harness = prepared(allow)

    async def query(method, path):
        entered.set()
        await release.wait()
        return native_messages()

    harness._transport = SimpleNamespace(request=query)
    task = asyncio.create_task(harness.authorize_preflight(payload()))
    await entered.wait()
    if change == "turn":
        harness._active_turn = SimpleNamespace(turn_id="new", abort_requested=False, stop_requested=False)
        harness._preflight.begin(harness.active_turn, "msg_new")
    elif change == "context":
        harness._context = replace(harness.context, agent_name="new")
    elif change == "session":
        harness._session_id = "ses_other"
    elif change == "generation":
        harness._preflight.endpoint = replace(harness._preflight.endpoint, generation="new")
    elif change == "transport":
        harness._transport = SimpleNamespace(request=query)
    elif change == "root":
        harness._preflight.root_message = "msg_other"
    else:
        harness.active_turn.abort_requested = True
    release.set()
    assert not (await task)["allowed"]
    assert calls == []


@pytest.mark.asyncio
async def test_permission_cannot_claim_a_different_native_assistant_message():
    harness = prepared()
    assert (await harness.authorize_preflight(payload()))["allowed"]
    assert (
        harness._preflight.claim(
            harness,
            "call_one",
            {
                "sessionID": "ses_one",
                "permission": "edit",
                "metadata": {"filepath": "/work/file"},
                "tool": {"messageID": "msg_other"},
            },
        )
        is None
    )


async def product_ticket(*, approve=True):
    """Synthetic native persistence plus the real Harness permission route."""
    from openjiuwen.harness_protocol import ToolApprovalDecision, ToolApprovalResponse

    harness = prepared()
    harness._preflight = PreflightGate(endpoint(product_tool_names=("workflow", "read")))
    harness._context = replace(
        harness.context,
        mcp_servers=(
            McpServerConfig(
                name="jiuwenswarm_product_tools",
                transport=McpTransport.HTTP,
                url="http://127.0.0.1:12345/mcp",
                headers={"Authorization": "Bearer " + harness._preflight.endpoint.token},
            ),
        ),
    )
    harness._preflight.begin(harness.active_turn, "msg_root")
    body = payload(tool="jiuwenswarm_product_tools_workflow", args={"query": {"values": [1, True, None]}})

    async def request(method, path):
        return native_messages(body)

    async def approval(value):
        return ToolApprovalResponse(value.request_id, ToolApprovalDecision.ALLOW)

    async def reply(*args):
        assert args[-1] == {"response": "once"}

    harness._transport = SimpleNamespace(request=request)
    harness._await_host_interaction = approval
    harness._reply_native = reply
    answer = await harness.authorize_preflight(body)
    assert answer["allowed"] and len(answer["ticket"]) == 67
    if approve:
        await harness._route_permission(
            harness.active_turn,
            SimpleNamespace(mark_denied=lambda _: None),
            "permission_one",
            body["call_id"],
            {
                "sessionID": body["session_id"],
                "tool": {"messageID": "msg_assistant"},
                "permission": body["tool"],
                "metadata": {},
            },
        )
    arguments = {**body["args"], PRODUCT_TICKET_FIELD: answer["ticket"]}
    return harness, body, answer, arguments


@pytest.mark.asyncio
async def test_product_ticket_returns_original_clean_identity_and_replay_is_impossible():
    harness, body, answer, arguments = await product_ticket()
    operation = harness.consume_product_preflight("workflow", arguments)
    record = harness._preflight.records[body["call_id"]]
    assert operation is record.request and harness.is_product_preflight_current(operation)
    assert operation.call_id == body["call_id"] and operation.turn_id == "turn-one"
    assert operation.provider_session_id == "ses_one" and PRODUCT_TICKET_FIELD not in operation.arguments
    assert operation.arguments["query"]["values"] == (1, True, None)
    assert not harness.is_product_preflight_current(replace(operation))
    assert harness.consume_product_preflight("workflow", arguments) is None
    assert harness.is_product_preflight_current(operation)
    assert answer["ticket"] not in repr(operation)
    assert answer["ticket"] not in harness._preflight.tickets


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["wrong_name", "extra", "missing", "different", "bool_int", "field_copy"])
async def test_product_ticket_mismatch_burns_ticket_before_retry(mutation):
    harness, _, _, arguments = await product_ticket()
    altered = json.loads(json.dumps(arguments))
    name = "workflow"
    if mutation == "wrong_name":
        name = "read"
    elif mutation == "extra":
        altered["new_argument"] = True
    elif mutation == "missing":
        del altered["query"]
    elif mutation == "different":
        altered["query"]["values"].append("changed")
    elif mutation == "bool_int":
        altered["query"]["values"][1] = 1
    else:
        altered["query"][PRODUCT_TICKET_FIELD] = altered[PRODUCT_TICKET_FIELD]
    assert harness.consume_product_preflight(name, altered) is None
    assert harness.consume_product_preflight("workflow", arguments) is None


@pytest.mark.asyncio
async def test_nonce_is_not_ticket_and_no_approval_cannot_consume():
    harness, body, answer, arguments = await product_ticket()
    mixed = {**body, "call_id": "mixed", "nonce": answer["ticket"]}
    assert not (await harness.authorize_preflight(mixed))["allowed"]
    assert harness.consume_product_preflight("workflow", {**arguments, PRODUCT_TICKET_FIELD: body["nonce"]}) is None
    assert harness.consume_product_preflight("workflow", arguments) is not None
    harness, _, _, arguments = await product_ticket(approve=False)
    assert harness.consume_product_preflight("workflow", arguments) is None


@pytest.mark.asyncio
async def test_model_cannot_supply_reserved_ticket_or_extra_envelope_fields():
    harness, body, _, _ = await product_ticket()
    for value in (
        payload(tool=body["tool"], args={PRODUCT_TICKET_FIELD: "b" * 64}, call_id="new", nonce="b" * 32),
        payload(tool=body["tool"], args={}, ticket="b" * 64, call_id="new", nonce="b" * 32),
    ):
        assert not (await harness.authorize_preflight(value))["allowed"]
    assert set(harness._preflight.records) == {body["call_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["turn", "context", "session", "abort", "stop", "clear", "close", "generation", "root", "transport", "denied"],
)
@pytest.mark.parametrize("consume_first", [True, False])
async def test_product_ticket_and_consumed_identity_die_with_original_scope(change, consume_first):
    harness, body, _, arguments = await product_ticket()
    operation = harness.consume_product_preflight("workflow", arguments) if consume_first else None
    if change == "turn":
        harness._active_turn = SimpleNamespace(turn_id="new", abort_requested=False, stop_requested=False)
    elif change == "context":
        harness._context = replace(harness.context, agent_name="new")
    elif change == "session":
        harness._session_id = "ses_new"
    elif change == "abort":
        harness.active_turn.abort_requested = True
    elif change == "stop":
        harness._stopping = True
    elif change == "generation":
        harness._preflight.endpoint = replace(harness._preflight.endpoint, generation="next")
    elif change == "root":
        harness._preflight.root_message = "msg_new"
    elif change == "transport":
        harness._transport = SimpleNamespace()
    elif change == "denied":
        harness._preflight.records[body["call_id"]].permission_allowed = False
    else:
        getattr(harness._preflight, change)()
    assert not harness.is_product_preflight_current(operation)
    assert harness.consume_product_preflight("workflow", arguments) is None


@pytest.mark.asyncio
async def test_product_late_first_delivery_cannot_mint_ticket_for_next_turn():
    harness, body, _, _ = await product_ticket()
    harness._preflight.clear()
    harness._active_turn = SimpleNamespace(turn_id="next", abort_requested=False, stop_requested=False)
    harness._preflight.begin(harness.active_turn, "msg_next")
    late = {**body, "call_id": "unseen_old_call", "nonce": "b" * 32}

    async def request(*_):
        return native_messages(late)

    harness._transport = SimpleNamespace(request=request)
    answer = await harness.authorize_preflight(late)
    assert not answer["allowed"] and "ticket" not in answer
    assert not harness._preflight.tickets


@pytest.mark.asyncio
async def test_product_current_must_be_rechecked_after_host_await():
    harness, _, _, arguments = await product_ticket()
    operation = harness.consume_product_preflight("workflow", arguments)
    assert harness.is_product_preflight_current(operation)
    entered, release = asyncio.Event(), asyncio.Event()

    async def host_resource_check():
        entered.set()
        await release.wait()
        return harness.is_product_preflight_current(operation)

    task = asyncio.create_task(host_resource_check())
    await entered.wait()
    harness._preflight.clear()
    release.set()
    assert not await task


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_product_permission_reply_failure_invalidates_ticket(cancelled):
    harness, body, _, arguments = await product_ticket(approve=False)

    async def fail_reply(*_):
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("synthetic send failure")

    harness._reply_native = fail_reply
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await harness._route_permission(
            harness.active_turn,
            SimpleNamespace(mark_denied=lambda _: None),
            "permission_one",
            body["call_id"],
            {
                "sessionID": body["session_id"],
                "tool": {"messageID": "msg_assistant"},
                "permission": body["tool"],
                "metadata": {},
            },
        )
    assert harness.consume_product_preflight("workflow", arguments) is None
