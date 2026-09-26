# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in OC-P0 probe for the pinned OpenCode native JS plugin loader.

Run from the code-core repository with::

    python -m tests.system_tests.harness_providers._opencode_plugin_probe \
        --output /absolute/evidence/directory

The probe uses only a loopback model and a task-owned local plugin.  It does
not install packages or use user credentials.  The result describes the
native 1.18.18 contract; it is not the production OC-P adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from ._opencode_server_probe import Probe

_PLUGIN_SOURCE = r'''import { appendFile } from "node:fs/promises"

const ledger = process.env.TMPDIR + "/oc-p-plugin-events.jsonl"
const record = async (value) => {
  await appendFile(ledger, JSON.stringify(value) + "\n")
}

export const OcPProbePlugin = async ({ directory, worktree }) => {
  await record({ hook: "init", directory, worktree })
  return {
    event: async ({ event }) => {
      if (["permission.asked", "permission.replied", "session.idle", "session.error"].includes(event.type)) {
        await record({ hook: "event", type: event.type })
      }
    },
    "tool.execute.before": async (input, output) => {
      await record({ hook: "before", tool: input.tool, callID: input.callID, args: output.args })
      if (String(output.args?.command || "").includes("OC-P-BLOCK")) {
        throw new Error("OC_P_BLOCKED_BY_PROBE")
      }
      if (String(output.args?.command || "").includes("OC-P-MUTATE")) {
        output.args.command = "printf OC-P-MUTATED"
      }
    },
    "tool.execute.after": async (input, output) => {
      await record({ hook: "after", tool: input.tool, callID: input.callID, output: output.output })
      output.output = String(output.output) + "|OC-P-AFTER"
    },
    dispose: async () => {
      await record({ hook: "dispose" })
    },
  }
}
'''


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PluginProbe(Probe):
    def __init__(self, cli: str, output: str):
        super().__init__(cli, output)
        self.checks["source_sha256"] = _sha256(Path(__file__))
        self.ledger: Path | None = None
        self.plugin: Path | None = None

    def ledger_rows(self) -> list[dict]:
        if self.ledger is None or not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line]

    async def wait_for_hook(self, hook: str, *, after: int = 0) -> list[dict]:
        for _ in range(200):
            rows = self.ledger_rows()
            if any(row.get("hook") == hook for row in rows[after:]):
                return rows
            await asyncio.sleep(0.05)
        raise TimeoutError(f"OpenCode plugin hook {hook}")

    async def prepare(self):
        # Reuse the OC0 loopback model, authenticated server client, isolated
        # XDG roots and hostile ambient project canary, then restart with the
        # explicit local plugin added to OPENCODE_CONFIG_CONTENT.
        await super().setup()
        await self.stop()
        package = self.root / "managed-plugin"
        package.mkdir(mode=0o700)
        self.plugin = package / "oc-p-probe.js"
        self.plugin.write_text(_PLUGIN_SOURCE)
        self.plugin.chmod(0o400)
        package.chmod(0o500)
        self.ledger = self.root / "tmp/oc-p-plugin-events.jsonl"
        self.config["plugin"] = [self.plugin.as_uri()]
        self.env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.config)

    async def probe_pure_gate(self):
        await self.start()
        _, config = await self.api("GET", "/config")
        self.check(
            "pure_readback_lists_declared_plugin",
            config.get("plugin") == [self.plugin.as_uri()],
            config.get("plugin"),
        )
        await asyncio.sleep(0.2)
        self.check("pure_skips_external_plugin", not self.ledger_rows())
        await self.stop()

    async def probe_explicit_local_plugin(self):
        self.env.pop("OPENCODE_PURE")
        await self.start()
        rows = await self.wait_for_hook("init")
        self.check("explicit_local_plugin_initialized", rows[0].get("hook") == "init", rows)
        _, config = await self.api("GET", "/config")
        self.check(
            "effective_config_retains_exact_file_url",
            config.get("plugin") == [self.plugin.as_uri()],
            config.get("plugin"),
        )
        log = (self.root / "server.log").read_text(errors="replace")
        self.check("ambient_project_plugin_still_disabled", "OC0_PROJECT_PLUGIN_EXECUTED" not in log)
        self.check("no_dependency_install_at_plugin_start", not list((self.root / "config").rglob("node_modules")))

        allowed = await self.new([{"permission": "*", "pattern": "*", "action": "allow"}])
        self.actions = [
            {"tool": "bash", "args": {"command": "printf OC-P-MUTATE", "description": "OC-P0 hook order"}},
            {"text": "OC-P-ALLOW-DONE"},
        ]
        before = len(self.ledger_rows())
        await self.submit(allowed, "OC-P0 allow and mutate")
        messages = await self.terminal(allowed)
        rows = await self.wait_for_hook("after", after=before)
        hooks = [row.get("hook") for row in rows[before:] if row.get("hook") in {"before", "after"}]
        self.check("tool_hooks_are_ordered", hooks[:2] == ["before", "after"], hooks)
        self.check(
            "before_mutation_and_after_output_are_effective",
            any(
                part.get("state", {}).get("output") == "OC-P-MUTATED|OC-P-AFTER"
                for message in messages
                for part in message["parts"]
            ),
        )

        denied_path = self.root / "work/oc-p-denied"
        denied = await self.new()
        self.actions = [
            {
                "tool": "bash",
                "args": {"command": f"printf denied > {denied_path}", "description": "OC-P0 rejection"},
            },
            {"text": "OC-P-DENIED-DONE"},
        ]
        before = len(self.ledger_rows())
        await self.submit(denied, "OC-P0 reject after before-hook")
        permission = await self.pending("permission", denied)
        rows = self.ledger_rows()
        denied_call = next(row["callID"] for row in rows[before:] if row.get("hook") == "before")
        self.check("before_hook_precedes_host_permission_answer", bool(denied_call))
        status, _ = await self.api(
            "POST", f"/session/{denied}/permissions/{permission['id']}", {"response": "reject"}
        )
        assert status == 200
        await self.terminal(denied, rejected=True)
        rows = self.ledger_rows()
        self.check(
            "host_rejection_prevents_execution_and_after_hook",
            not denied_path.exists()
            and not any(row.get("hook") == "after" and row.get("callID") == denied_call for row in rows),
        )

        blocked_path = self.root / "work/oc-p-blocked"
        self.actions = [
            {
                "tool": "bash",
                "args": {
                    "command": f"printf OC-P-BLOCK > {blocked_path}",
                    "description": "OC-P0 hook failure",
                },
            },
            {"text": "OC-P-BLOCK-DONE"},
        ]
        before = len(self.ledger_rows())
        await self.submit(allowed, "OC-P0 block in hook")
        blocked_messages = await self.terminal(allowed)
        rows = self.ledger_rows()
        blocked_call = next(row["callID"] for row in rows[before:] if row.get("hook") == "before")
        self.check(
            "before_hook_error_fails_tool_without_side_effect_or_after_hook",
            not blocked_path.exists()
            and not any(row.get("hook") == "after" and row.get("callID") == blocked_call for row in rows)
            and any(
                part.get("state", {}).get("status") == "error"
                for message in blocked_messages
                for part in message["parts"]
            ),
        )
        self.check("event_observer_receives_native_events", any(row.get("hook") == "event" for row in rows))
        self.check("no_dependency_install_after_turns", not list((self.root / "config").rglob("node_modules")))
        self.save("plugin-ledger-before-stop.json", rows)
        await self.stop()
        # The pinned server is stopped by terminating its owned process group.
        # Record whether the optional native callback ran, but do not use it as
        # a cleanup guarantee: cgroup emptiness remains the authoritative exit.
        await asyncio.sleep(0.2)
        rows = self.ledger_rows()
        self.checks["plugin_dispose_observed_on_sigterm"] = any(row.get("hook") == "dispose" for row in rows)
        self.save("plugin-ledger.json", rows)

    async def probe_native_loader_failure_semantics(self):
        before = sum(row.get("hook") == "init" for row in self.ledger_rows())
        missing = self.root / "managed-plugin/missing.js"
        self.config["plugin"] = [missing.as_uri()]
        self.env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.config)
        await self.start()
        _, config = await self.api("GET", "/config")
        await asyncio.sleep(0.2)
        after = sum(row.get("hook") == "init" for row in self.ledger_rows())
        self.check(
            "native_loader_missing_file_is_fail_open_without_host_handshake",
            config.get("plugin") == [missing.as_uri()] and after == before,
            config.get("plugin"),
        )
        await self.stop()

    async def run(self):
        await self.prepare()
        self.checks["cli_sha256"] = await asyncio.to_thread(_sha256, self.cli)
        self.checks["plugin_sha256"] = await asyncio.to_thread(_sha256, self.plugin)
        await self.probe_pure_gate()
        await self.probe_explicit_local_plugin()
        await self.probe_native_loader_failure_semantics()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default=str(Path.home() / ".opencode/bin/opencode"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    probe = PluginProbe(args.cli, args.output)
    try:
        async with asyncio.timeout(180):
            await probe.run()
    finally:
        await probe.close()


if __name__ == "__main__":
    asyncio.run(main())
