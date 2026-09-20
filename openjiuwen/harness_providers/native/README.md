# Native output compatibility

`DeepAgentHarness` keeps the original `DeepAgent` factory and the shared
`SerializedTurnHarness` lifecycle. Two constructor-only integration options
support hosts that already own Native stream rails:

```python
harness = DeepAgentHarness(
    build_unstarted_agent,
    observe_tools=False,           # the supplied agent already emits tool chunks
    preserve_output_chunks=True,
)
io = HarnessIOAdapter(
    harness,
    preserve_native_chunks=True,
    auto_approve_tools=False,
)
```

Defaults remain `observe_tools=True`, `preserve_output_chunks=False`, and
`preserve_native_chunks=False`. A factory must return an **unstarted** agent;
these options do not adopt a running agent or an existing output lease.
`NativeHostHooks` may supply a fresh product Session as described below.
They are host integration choices, not JSON provider configuration fields.

- Text and tools retain their standard OutputEvent / ItemLifecycleEvent semantics.
  Optional `data["native.output_chunk.v1"]` stores the original JSON-safe
  `{type, index, payload}`. Core flat and swarm nested tool frames are accepted.
  Swarm `raw_output` maps to the structured result; `rendered_result` remains
  separate from the legacy string `result`.
- Other chunks keep their existing Native ProviderEvent type and payload, with
  the same optional compatibility snapshot. In preservation mode, `answer` and
  `execution.error` also emit a snapshot before the ordinary Turn terminal.
- `HarnessIOAdapter` restores each snapshot **instead of** its generic projection,
  through the same single consumer. Its event observer still receives the
  standard event envelope and terminal result. The original chunk index is
  preserved, not replaced with protocol sequence numbers.
- Interaction chunks are never mirrored. The existing request/response handler
  remains authoritative, avoiding duplicate questions or approvals.
- Snapshots are JSON-safe observations, not retained Python object identities.
  They do not expose callbacks, take over control, or define shared mode/Step/
  subagent contracts. These common semantics still belong in harness_protocol.
- Goal/subagent sample frames test transport preservation only; this is not
  evidence that the swarm Single route or its Goal scheduler has migrated.

## Validation

`tests/unit_tests/harness_providers/test_native_output_preservation.py` exercises
real SerializedTurnHarness and IO pump behavior around a fake DeepAgent:
field/order and codec round trips, nested tools, original default projection,
one final answer/terminal, error terminals, ask-user same-Turn resume, abort
while waiting, and tool rail ownership. Existing IO/base/rendered-result tests
are retained and included in the `pr-stable` native-output suite. These tests
require no model credentials or vendor SDK and are not live-provider E2E tests.

## Host session and input ports

`NativeHostHooks` is an immutable constructor-only object. Its callbacks never
enter the protocol config, event payload or checkpoint:

- `create_session(context, agent)` returns a fresh Session matching the configured
  Native session id. The harness owns pre_run, agent start/stop and post_run.
- `before_start(agent, session)` runs after pre_run and before start, allowing
  the original host input guard to be installed.
- `dispatch_input(agent, default_request, content, resuming)` translates a
  protocol input to the original host request, retaining Python permission
  handoff/context objects. It executes with the sole output lease already held;
  steering uses the active lease. Return True to drain output or False for a
  command that produces no execution (e.g. Goal confirmation required).

Agent ownership is claimed before initialization; another harness cannot claim
the same instance concurrently. A running legacy instance is rejected without
stopping it. Partially started owned resources are unwound on failure, including
cancellation. The public agent property is available only after successful start.

Host Goal set/resume callbacks must execute through an accepted protocol Turn,
not by starting work alongside an IDLE provider. Read/pause/clear remain direct
controls of the original GoalManager so they can stop running work; they must
not enqueue behind that work. Hosts must also release transient Python request
references on terminal events, failed submissions and stop. These ports do not
provide durable restoration of live Python objects.

In Native preservation mode the IO interaction handler retains the JSON-safe
original question/card value from the authoritative UserInputRequest. It does
not infer prompts from observations or auto-approve permissions.

`test_native_host.py` verifies lifecycle order, host request identity, rollback,
wrong-session cleanup, concurrent ownership, and no-output commands. These
deterministic tests do not establish a migrated swarm chat/UI route.
