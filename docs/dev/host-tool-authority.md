# Host-owned Tool final authority

`openjiuwen.core.foundation.tool.invoke_tool_with_authority` lets an explicitly
owned gateway invoke an existing `Tool` with mandatory authorization at its real
final invocation boundary. It reuses `_ToolMeta` and the existing per-call
execution scopes; it creates no Agent, Runner registration, event consumer or
execution queue. Ordinary AbilityManager and unguarded Tool behavior is retained.

```python
result = await invoke_tool_with_authority(
    tool,
    clean_inputs,
    operation=original_operation,
    authorizer=authorize_final,
    runtime_kwargs={"session": owned_session},
    is_current=original_source_is_current,
    resolve_executor=lambda: owned_catalog[local_tool_name],
)
```

The keyword arguments are trusted live host objects, never serialized config or
model-provided authorization. `operation` is the original frozen
`BeforeToolContext`; clean input arguments must initially match it exactly.
Provider names may differ from the Tool's local card name. The host owns that
mapping and the original subject/session/resource decision.

Existing before/input-transform/started callbacks still run. Immediately before
the registered underlying Tool method, `authorizer` receives a new frozen context
with the **actual final inputs** and the original source agent, Provider session,
Turn, call ID and tool name. Only exact `True` permits invocation. Denial,
exception, changed implementation, changed runtime kwargs or closed source fail
before the underlying method. This does not authorize side effects implemented
inside arbitrary host callbacks or provide OS/path isolation.

During the callback, `current_tool_invocation()` exposes the exact executor,
registered original method, and `source_operation`, which is the original host
object (None for Native AbilityManager calls). Use that object for ticket or
source identity checks. The final transformed `operation` is distinct and cannot
replace an original ticket identity. Its live proof checks actual inputs again
after awaited authorization, as well as executor/card, source liveness and
runtime kwargs identities. Host policy must map every actual executor resource,
including sessions, credentials or delegation; a tool-name grant is insufficient.

The host path is bound to the invoking task. Protected stream, arbitrary invoke
wrappers, unregistered executors, direct recursive execution, nested
AbilityManager calls and cross-task/inherited use are rejected. Top-level
concurrent invocations have separate scopes. A new delegated execution requires
its own trusted source and resources outside the inherited parent call; context
inheritance grants no child authority. Scope exit/cancellation closes the proof.

The resource owner must still enforce revocation at its own asynchronous resource
use boundaries inside a running tool. This API validates final admission, not all
internal I/O of arbitrary code. Product MCP transport, framework tool mappings,
Team/subagent credentials and real Provider acceptance are independent checks.
