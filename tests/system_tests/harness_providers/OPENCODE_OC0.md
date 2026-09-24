# OpenCode 1.18.18 Server probe (OC0)

`_opencode_server_probe.py` is an explicitly invoked native Server probe. It
uses the installed CLI with a local Chat Completions fixture and authenticated
local HTTP MCP. It does not implement or register an OpenJiuwen provider.

Run with the repository Python environment containing aiohttp:

```bash
python tests/system_tests/harness_providers/_opencode_server_probe.py --cli /absolute/path/to/opencode --output /tmp/opencode-oc0-evidence
```

The probe requires non-root Linux and rejects a system `/etc/opencode` source.
It creates a private temporary HOME/XDG/TMPDIR, a read-only configuration
folder, synthetic credentials, explicit loopback ports, and its own process
group. No user environment credentials are inherited, no dependency is
installed, and no external model is called. A 180-second timeout bounds the
run; owned server groups and fixture listeners are closed in `finally`.
Private native database/logs remain in the printed temporary directory for
inspection. Generated JSON evidence belongs in the requested output directory.

On 2026-09-24, core 87bbbf9b4337bbc41466dc840d4f254fc3c67292 and CLI 1.18.18
(binary SHA-256 bb71f45b564f9234a97f54d6252a4a41d2f4388ae4b078918f691824cc3b3e54),
26 distinct positive checks passed: authenticated startup, project source
isolation, no installation, text/usage, native tool approval/rejection,
question reply after observer disconnect, duplicate reply rejection, MCP calls,
full-access rules, native fork, completed-session restart, abort, and owned
process-group exit. Python 3.13.15 used the local editable core, not a locked
release installation. Startup RSS was approximately 365 MiB per server.

Two observations are deliberately separate from successful assertions:

- The legacy permission response endpoint accepts a mismatched session ID in
  its URL and executes the pending request. The future provider must validate
  request ownership through the existing SerializedTurnHarness interaction
  ledger and own an isolated server. Native HTTP routing is insufficient.
- Submitting `format.type=json_schema` is accepted, but reading that session's
  messages returns HTTP 400 (`OutputFormatJsonSchema`, including native
  `retryCount:2`). Structured workflow output remains unqualified. An exit code
  of zero means the probe recorded the observation, not that this feature passed.

Use the actual `/session` + `/event` generation. The v2 `/api/session` reply
endpoint cannot answer these legacy session requests. Rejecting a permission
ends with tool error, completed `finish=tool-calls`, and idle, with no ordinary
stop message; neither bare idle nor stream EOF proves a successful turn.
The probe's polling helpers are test instrumentation, not a provider state
machine or reconnect/replay implementation.

Production authorization compilation, storage ownership, source-policy
preflight, normalized failures, bounded SSE parsing and protocol lifecycle
mapping remain implementation work. Native fork does not imply public fork;
completed-session restart does not imply active-turn recovery. No remote-model,
product UI/channel, product subagent, Skills/plugin or CI release claim is made.

## Source and service ownership qualification

Run `_opencode_isolation_probe.py --output /tmp/opencode-oc0-isolation` separately.
It reuses the same pinned CLI and loopback fixture, requires non-root Linux,
cgroup v2 and a running **user systemd manager**, and creates only randomly named
`r1-05-oc0-*.service` transient units. Each unit uses `KillMode=control-group`,
`TimeoutStopSec=2s`, a test-only runtime limit of 90 seconds and automatic
collection. Configuration credentials stay in 0600 files inside 0700 temporary
roots, never in command arguments. All units are stopped in `finally`.

This second probe qualifies the startup sequence: sealed HOME/config snapshots,
explicit environment, absent managed/auth sources, exact binary digest, exclusive
storage lease, authenticated health **and** effective configuration readback,
then SSE readiness. Negative cases run before spawning; malformed native config
can pass health and is rejected by the subsequent config check. The pinned
config codec inserts empty `options`/`permission` objects into the disabled title
agent; readback accounts for exactly those defaults and rejects other differences.

A first raw-process-group experiment found that the actual bash tool creates a
separate process group and survives a Server-group stop. A Server PID/group check
alone is therefore insufficient. The isolated service probe verifies detached
and TERM-resistant descendants are still in the owned cgroup, are removed on
stop and Server crash, and another service remains alive. The original probe's
`owned_process_group_reaped` assertion never established this stronger guarantee.

The supervisor strategy and admission checks are **test implementations**, not
an installed Provider. OC1 must implement them, retain ownership on unconfirmed
exit and refuse unqualified environments rather than silently degrade to raw
`killpg`. Transient cgroups and same-user read-only config are lifecycle/source
controls for a trusted local user; they are not filesystem/network isolation or
protection against hostile processes with that same user's authority. macOS,
Windows, root and non-systemd Linux remain outside this initial qualification.
