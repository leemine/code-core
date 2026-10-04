# Read-only evidence during actual Tool execution

`openjiuwen.core.foundation.tool.current_tool_execution()` returns a
`ToolExecution` only inside the registered original `Tool.invoke` method, on
that method's owning asyncio Task, after every mandatory final authorizer has
returned exact `True`. Ungoverned legacy invocations still work and return no
certificate. This API creates no authorization, Agent, registry or execution
queue, and does not change the final permission gate.

The original `current_tool_invocation()` API retains its existing lifetime:
its proof exists only inside final authorization callbacks. It is already
expired when the original Tool method starts. The two APIs answer different
questions and their evidence cannot be substituted.

```python
from openjiuwen.core.foundation.tool import current_tool_execution

# Inside the original method of a Tool with mandatory authority:
execution = current_tool_execution()
if execution is None or execution.executor is not self:
    raise PermissionError("owned execution required")

# Validate the host-owned client/resource binding and actual internal arguments.
# Do not infer permission from the certificate alone.
await owned_client.consume(
    actual_arguments,
    original_execution=execution,
)
```

The read-only fields are `operation`, `executor`, `agent_context`,
`original_invoke`, `owning_task`, and the `source_operation` property. The latter
retains the original host object for `invoke_tool_with_authority` calls; it is
None for ordinary AbilityManager calls. The frozen `operation` is the context
that passed final authorization, including outer input transforms. It does not
change if the Tool subsequently parses or transforms its internal inputs.
A consumer must validate those later actual arguments and all required resources
at its own I/O boundary. No credential, child subject, remote operation, or
filesystem confinement is granted by this certificate.

`execution.is_current()` requires the original Task and rechecks its live
call/execution, actual executor/card/registered method, exact agent/session and
call identity, runtime kwargs, original host predicate, and cancellation state.
The certificate is unavailable during input, started, finished, error and output
callbacks. A nested Tool's callbacks cannot borrow the parent certificate. The
existing four-layer Tool unwrap chain still resolves the same original bound
method, and existing callback execution is retained. MCPTool also masks its
internal TOOL_PARSE_STARTED and TOOL_PARSE_FINISHED dispatch: neither the
getter nor a previously captured certificate can validate inside those callback
contexts. Parsing and callback argument transforms still run. The original
method regains its certificate only after dispatch returns, for its actual
private-client call; actual post-parse resource checks remain the host consumer's
responsibility. An exception or cancellation during parsing cannot leave a
usable certificate for later work.

An already captured `execution.is_current_origin()` may be checked in an
inherited SDK child task while the original method remains live. This checks
origin only: the child cannot acquire a certificate through
`current_tool_execution()` or make `is_current()` succeed. A host-owned consumer
must capture its fixed operation on the original Task before spawning SDK work,
retain exact actual arguments and its own current authorization, and close its
operation lifetime. Passing origin evidence is not permission for another tool
or a new child execution. If the host's original current predicate is itself
task-bound, origin checks from the child conservatively fail; the API does not
bypass that predicate.

Method return, exception or cancellation invalidates the certificate, including
copies retained in a Context or child task. Pending cancellation also invalidates
it before cancellation unwinds the method. Replacing executor/card/invoke,
changing source/session/call identity, or changing required runtime dependencies
invalidates it immediately. Protected Tool.stream remains unsupported.

The API reuses the existing `_Call` and `_Invocation` lifecycle. It is local
runtime evidence, not serializable protocol data or a persisted grant. MCP
registration, private credential factories and provider-specific final resource
mapping must be separately integrated and validated; this API alone does not
establish those capabilities.
