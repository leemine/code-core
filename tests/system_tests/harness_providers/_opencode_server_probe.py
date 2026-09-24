# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in OC0 probe: real pinned CLI, loopback model/MCP, no user credentials.

Run directly with --output PATH. No installation or external model is used.
All child state is private; the process group is always reaped. This probes
native Server contracts, not the not-yet-implemented Harness Provider.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web


class Probe:
    def __init__(self, cli, output):
        if sys.platform != "linux" or os.geteuid() == 0:
            raise RuntimeError("OC0 probe requires non-root Linux; other platforms are not qualified")
        if Path("/etc/opencode").exists():
            raise RuntimeError("system managed OpenCode config requires separate source-policy review")
        self.cli = Path(cli).resolve()
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="r1-05-oc0-"))
        self.process = None
        self.events = []
        self.requests = []
        self.mcp_calls = []
        self.actions = []
        self.checks = {"source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        self.password = secrets.token_hex(24)
        self.token = secrets.token_hex(24)
        self.pump = None
        self.runner = None
        self.client = None
        self.log = None
        self.slow = asyncio.Event()

    def save(self, name, data):
        (self.output / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def check(self, name, condition, detail=None):
        self.checks[name] = {"passed": bool(condition), "detail": detail}
        print(name, "PASS" if condition else "FAIL", flush=True)
        assert condition, (name, detail)

    async def model(self, request):
        body = await request.json()
        self.requests.append(body)
        action = self.actions.pop(0) if body.get("tools") and self.actions else {"text": "OC0-LOCAL-OK"}
        if action.get("slow"):
            self.slow.set()
            await asyncio.sleep(8)
        if "tool" in action:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(self.requests)}",
                        "type": "function",
                        "function": {"name": action["tool"], "arguments": json.dumps(action["args"])},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta = {"content": action.get("text", "OC0-LOCAL-OK")}
            finish = "stop"
        base = {
            "id": f"chatcmpl-{len(self.requests)}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture",
        }
        chunks = [
            dict(base, choices=[{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]),
            dict(
                base,
                choices=[{"index": 0, "delta": {}, "finish_reason": finish}],
                usage={"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22},
            ),
        ]
        return web.Response(
            text="".join("data: " + json.dumps(x) + "\n\n" for x in chunks) + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    async def mcp(self, request):
        if request.headers.get("Authorization") != "Bearer " + self.token:
            return web.Response(status=401)
        if request.method != "POST":
            return web.Response(status=405)
        body = await request.json()
        method = body.get("method")
        self.mcp_calls.append({"method": method, "authenticated": True, "params": body.get("params")})
        if "id" not in body:
            return web.Response(status=202)
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oc0-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo an OC0 probe value",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "OC0-MCP-" + body["params"]["arguments"]["value"]}]}
        else:
            result = {}
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})

    async def setup(self):
        for name in ("home", "config", "data", "cache", "state", "tmp", "work"):
            (self.root / name).mkdir()
        # A real project config/plugin canary must never be loaded.
        (self.root / "work" / "opencode.json").write_text('{"model":"forbidden/model"}')
        plugins = self.root / "work" / ".opencode" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "canary.js").write_text('throw new Error("OC0_PROJECT_PLUGIN_EXECUTED")')
        config_dir = self.root / "config" / "opencode"
        config_dir.mkdir()
        (config_dir / ".gitignore").write_text("node_modules\n")
        config_dir.chmod(0o500)  # Npm.install checks writable before any reify.
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.model)
        app.router.add_route("*", "/mcp", self.mcp)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        await web.SockSite(self.runner, sock).start()
        self.config = {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "model": "oc0/fixture",
            "small_model": "oc0/fixture",
            "enabled_providers": ["oc0"],
            "provider": {
                "oc0": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "OC0 loopback",
                    "options": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": "fixture-only"},
                    "models": {
                        "fixture": {"name": "fixture", "tool_call": True, "limit": {"context": 32000, "output": 4096}}
                    },
                }
            },
            "permission": {"*": "ask", "question": "allow"},
            "lsp": False,
            "formatter": False,
            "agent": {"title": {"disable": True}},
            "mcp": {
                "probe": {
                    "type": "remote",
                    "url": f"http://127.0.0.1:{port}/mcp",
                    "headers": {"Authorization": "Bearer " + self.token},
                    "oauth": False,
                }
            },
        }
        self.env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            **{"XDG_" + name.upper() + "_HOME": str(self.root / name) for name in ("config", "data", "cache", "state")},
            **{
                name: "true"
                for name in (
                    "OPENCODE_DISABLE_AUTOUPDATE",
                    "OPENCODE_DISABLE_MODELS_FETCH",
                    "OPENCODE_DISABLE_PROJECT_CONFIG",
                    "OPENCODE_DISABLE_DEFAULT_PLUGINS",
                    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
                    "OPENCODE_DISABLE_LSP_DOWNLOAD",
                    "OPENCODE_DISABLE_FFF",
                    "OPENCODE_PURE",
                )
            },
            "OPENCODE_SERVER_PASSWORD": self.password,
            "OPENCODE_CONFIG_CONTENT": json.dumps(self.config),
            "npm_config_registry": "http://127.0.0.1:1",
            "npm_config_offline": "true",
            "npm_config_fetch_retries": "0",
        }
        self.client = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth("opencode", self.password), timeout=aiohttp.ClientTimeout(total=25)
        )
        await self.start()

    async def start(self):
        # Do not use --port 0: this version first attempts the shared default 4096.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.log = (self.root / "server.log").open("ab")
        started = time.monotonic()
        self.process = await asyncio.create_subprocess_exec(
            str(self.cli),
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
            cwd=self.root / "work",
            env=self.env,
            stdout=self.log,
            stderr=self.log,
            start_new_session=True,
        )
        for _ in range(150):
            if self.url not in (self.root / "server.log").read_text(errors="replace"):
                await asyncio.sleep(0.1)
                continue
            if self.process.returncode is not None:
                raise RuntimeError((self.root / "server.log").read_text())
            try:
                status, health = await self.api("GET", "/global/health")
                if status == 200:
                    self.check("version", health == {"healthy": True, "version": "1.18.18"}, health)
                    break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("OpenCode readiness")
        self.checks.setdefault("startup_seconds", []).append(round(time.monotonic() - started, 3))
        self.record_rss()
        await self.connect_events()

    def record_rss(self):
        status = Path(f"/proc/{self.process.pid}/status").read_text()
        rss = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:"))
        self.checks.setdefault("server_rss_kib", []).append(rss)

    async def connect_events(self):
        offset = len(self.events)
        self.pump = asyncio.create_task(self.events_loop())
        for _ in range(100):
            if self.pump.done():
                await self.pump
                raise RuntimeError("event stream closed before ready")
            if any(event.get("type") == "server.connected" for event in self.events[offset:]):
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("SSE readiness")

    async def stop(self):
        if self.pump:
            self.pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.pump
            self.pump = None
        if self.process:
            pid = self.process.pid
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except TimeoutError:
                os.killpg(pid, signal.SIGKILL)
                await self.process.wait()
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                self.check("owned_process_group_reaped", True)
            else:
                os.killpg(pid, signal.SIGKILL)
                self.check("owned_process_group_reaped", False)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None

    async def api(self, method, path, body=None):
        async with self.client.request(method, self.url + path, json=body) as response:
            data = await response.read()
            return response.status, json.loads(data) if data else None

    async def events_loop(self):
        async with self.client.get(self.url + "/event", timeout=aiohttp.ClientTimeout(total=None)) as response:
            response.raise_for_status()
            async for line in response.content:
                if line.startswith(b"data: "):
                    self.events.append(json.loads(line[6:]))

    async def new(self, permission=None):
        body = {"title": "OC0 probe"}
        if permission is not None:
            body["permission"] = permission
        status, result = await self.api("POST", "/session", body)
        assert status == 200, result
        return result["id"]

    async def submit(self, session, text, **options):
        status, _ = await self.api(
            "POST",
            f"/session/{session}/prompt_async",
            {
                "model": {"providerID": "oc0", "modelID": "fixture"},
                "parts": [{"type": "text", "text": text}],
                **options,
            },
        )
        assert status == 204, status

    async def pending(self, kind, session):
        for _ in range(240):
            status, result = await self.api("GET", "/" + kind)
            assert status == 200, result
            found = [x for x in result if x["sessionID"] == session]
            if found:
                return found[0]
            await asyncio.sleep(0.1)
        raise TimeoutError(f"{kind} for {session}")

    async def terminal(self, session, after=0, *, rejected=False):
        for _ in range(240):
            status, messages = await self.api("GET", f"/session/{session}/message")
            if status != 200:
                self.save("message-read-error.json", {"status": status, "body": messages})
                raise RuntimeError(f"message read HTTP {status}")
            assistants = [x for x in messages[after:] if x["info"]["role"] == "assistant"]
            if assistants and assistants[-1]["info"].get("time", {}).get("completed"):
                last = assistants[-1]
                if last["info"].get("finish") != "tool-calls" or last["info"].get("error"):
                    return messages
                if rejected and any(p.get("state", {}).get("status") == "error" for p in last["parts"]):
                    _, statuses = await self.api("GET", "/session/status")
                    if session not in statuses or statuses[session].get("type") == "idle":
                        return messages
            await asyncio.sleep(0.1)
        raise TimeoutError(f"terminal for {session}")

    async def run(self):
        await self.setup()
        status, schema = await self.api("GET", "/doc")
        assert status == 200
        self.save("schema.json", schema)
        self.checks["cli_sha256"] = hashlib.sha256(self.cli.read_bytes()).hexdigest()
        self.checks["schema_sha256"] = hashlib.sha256((self.output / "schema.json").read_bytes()).hexdigest()
        async with aiohttp.ClientSession() as anonymous:
            async with anonymous.get(self.url + "/global/health") as response:
                self.check("server_auth_required", response.status == 401, response.status)
        _, config = await self.api("GET", "/config")
        self.check("project_config_plugin_isolation", config.get("model") == "oc0/fixture" and not config.get("plugin"))
        self.check("no_dependency_install", not list((self.root / "config").rglob("node_modules")))
        _, mcp = await self.api("GET", "/mcp")
        self.check("mcp_authenticated_ready", mcp.get("probe", {}).get("status") == "connected", mcp)
        session = await self.new()
        await self.submit(session, "OC0 text")
        messages = await self.terminal(session)
        self.save("text.json", messages)
        self.check("text_output", any(p.get("text") == "OC0-LOCAL-OK" for x in messages for p in x["parts"]))
        self.check(
            "usage_after_model_step",
            messages[-1]["info"]["tokens"]["input"] == 17 and messages[-1]["info"]["tokens"]["output"] == 5,
        )
        self.checks["first_session"] = session
        # Native v1 replies are probed for scoping/idempotency; the adapter must
        # enforce its own protocol interaction ledger regardless of HTTP status.
        self.actions = [
            {"tool": "bash", "args": {"command": "printf OC0-APPROVED", "description": "OC0 permission probe"}}
        ]
        before = len(messages)
        await self.submit(session, "OC0 permission allow")
        permission = await self.pending("permission", session)
        self.save("permission.json", permission)
        status, _ = await self.api("POST", f"/session/{session}/permissions/{permission['id']}", {"response": "once"})
        self.check("permission_allow", status == 200, status)
        status, _ = await self.api("POST", f"/session/{session}/permissions/{permission['id']}", {"response": "once"})
        self.check("permission_duplicate_rejected", status == 404, status)
        messages = await self.terminal(session, before)
        self.save("tool.json", messages[before:])
        self.check(
            "native_tool_output",
            any(p.get("state", {}).get("output") == "OC0-APPROVED" for x in messages[before:] for p in x["parts"]),
        )
        # Separate request: demonstrate that legacy native scoping is insufficient.
        self.actions = [{"tool": "bash", "args": {"command": "printf OC0-CROSS", "description": "OC0 scoping probe"}}]
        before = len(messages)
        await self.submit(session, "OC0 native cross-session behavior")
        permission = await self.pending("permission", session)
        other = await self.new()
        status, _ = await self.api("POST", f"/session/{other}/permissions/{permission['id']}", {"response": "once"})
        self.checks["native_cross_session_reply_status"] = status
        if status != 200:
            await self.api("POST", f"/session/{session}/permissions/{permission['id']}", {"response": "once"})
        messages = await self.terminal(session, before)
        self.save("cross-session-native.json", messages[before:])
        self.actions = [
            {
                "tool": "question",
                "args": {
                    "questions": [
                        {
                            "header": "OC0",
                            "question": "Select probe answer",
                            "options": [{"label": "Yes", "description": "Continue"}],
                        }
                    ]
                },
            }
        ]
        before = len(messages)
        await self.submit(session, "OC0 question")
        question = await self.pending("question", session)
        self.save("question.json", question)
        self.pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.pump
        self.pump = None
        _, pending_questions = await self.api("GET", "/question")
        self.check("question_survives_observer_disconnect", question in pending_questions)
        await self.connect_events()
        status, _ = await self.api("POST", f"/question/{question['id']}/reply", {"answers": [["Yes"]]})
        self.check("question_reply", status == 200, status)
        status, _ = await self.api("POST", f"/question/{question['id']}/reply", {"answers": [["Yes"]]})
        self.check("question_duplicate_rejected", status == 404, status)
        messages = await self.terminal(session, before)
        self.save("question-result.json", messages[before:])
        self.check(
            "question_resumed",
            any(
                p.get("state", {}).get("status") == "completed" and "Yes" in p["state"].get("output", "")
                for x in messages[before:]
                for p in x["parts"]
            ),
        )
        self.actions = [{"tool": "probe_echo", "args": {"value": "SUCCESS"}}]
        before = len(messages)
        await self.submit(session, "OC0 MCP")
        permission = await self.pending("permission", session)
        status, _ = await self.api("POST", f"/session/{session}/permissions/{permission['id']}", {"response": "once"})
        assert status == 200
        messages = await self.terminal(session, before)
        self.check("mcp_actual_call", any(x["method"] == "tools/call" for x in self.mcp_calls))
        self.save("mcp-result.json", messages[before:])
        self.check(
            "mcp_result",
            any(p.get("state", {}).get("output") == "OC0-MCP-SUCCESS" for x in messages[before:] for p in x["parts"]),
        )
        # Denial is judged by tool outcome and absence of the requested write.
        denied_path = self.root / "work" / "must-not-exist"
        self.actions = [
            {"tool": "bash", "args": {"command": f"printf denied > {denied_path}", "description": "Denied probe"}}
        ]
        before = len(messages)
        await self.submit(session, "OC0 permission reject")
        permission = await self.pending("permission", session)
        status, _ = await self.api("POST", f"/session/{session}/permissions/{permission['id']}", {"response": "reject"})
        assert status == 200
        messages = await self.terminal(session, before, rejected=True)
        self.save("denied.json", messages[before:])
        self.check(
            "permission_reject_prevents_execution",
            not denied_path.exists()
            and any(p.get("state", {}).get("status") == "error" for x in messages[before:] for p in x["parts"]),
        )
        full = await self.new([{"permission": "*", "pattern": "*", "action": "allow"}])
        self.actions = [
            {"tool": "bash", "args": {"command": "printf OC0-FULL", "description": "Full access probe"}},
            {"tool": "probe_echo", "args": {"value": "FULL"}},
        ]
        await self.submit(full, "OC0 full access")
        full_messages = await self.terminal(full)
        self.save("full-access.json", full_messages)
        self.check(
            "full_access_native_and_mcp_without_ask",
            all(
                any(p.get("state", {}).get("output") == value for x in full_messages for p in x["parts"])
                for value in ("OC0-FULL", "OC0-MCP-FULL")
            ),
        )
        structured = await self.new()
        self.actions = [{"tool": "StructuredOutput", "args": {"ok": True}}]
        await self.submit(
            structured,
            "OC0 structured output",
            format={
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        )
        try:
            result = await self.terminal(structured)
            self.save("structured.json", result)
            self.checks["structured_result_observation"] = result[-1]["info"].get("structured")
        except (RuntimeError, TimeoutError) as error:
            self.checks["structured_result_observation"] = {"error": str(error)}
            await self.api("POST", f"/session/{structured}/abort")
        self.actions = []
        self.check("no_dependency_install_after_turns", not list((self.root / "config").rglob("node_modules")))
        self.checks["event_types"] = sorted({event["type"] for event in self.events})
        status, forked = await self.api("POST", f"/session/{session}/fork", {})
        self.check("native_fork_identity", status == 200 and forked["id"] != session)
        _, fork_messages = await self.api("GET", f"/session/{forked['id']}/message")
        self.check("native_fork_history", len(fork_messages) == len(messages))
        await self.stop()
        await self.start()
        _, restored = await self.api("GET", f"/session/{session}/message")
        self.check("restart_preserves_messages", restored == messages)
        before = len(restored)
        await self.submit(session, "OC0 after restart")
        messages = await self.terminal(session, before)
        self.check("restart_continues_same_session", len(messages) > before)
        self.actions = [{"slow": True}]
        before = len(messages)
        await self.submit(session, "OC0 abort")
        await asyncio.wait_for(self.slow.wait(), 20)
        status, result = await self.api("POST", f"/session/{session}/abort")
        self.check("abort_ack", status == 200 and result is True)
        aborted = await self.terminal(session, before)
        self.save("abort.json", aborted[before:])
        self.check(
            "abort_not_success",
            any(x["info"].get("error", {}).get("name") == "MessageAbortedError" for x in aborted[before:]),
        )

    async def close(self):
        try:
            await self.stop()
        finally:
            if self.client:
                await self.client.close()
            if self.runner:
                await self.runner.cleanup()
            self.save("checks.json", self.checks)
            self.save("events.json", self.events)
            self.save("model-requests.json", self.requests)
            self.save("mcp-calls.json", self.mcp_calls)
            print("Private runtime:", self.root, flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default=str(Path.home() / ".opencode" / "bin" / "opencode"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    probe = Probe(args.cli, args.output)
    try:
        async with asyncio.timeout(180):
            await probe.run()
    finally:
        await probe.close()


if __name__ == "__main__":
    asyncio.run(main())
