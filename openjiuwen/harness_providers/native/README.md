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
these options do not adopt a running agent, an existing session, or output lease.
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
