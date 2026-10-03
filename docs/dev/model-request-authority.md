# Instance-bound model request authority

`Model`, `init_model`, `create_model_client`, and the built-in `OpenAIModelClient`
accept an optional keyword-only `request_authority`. Without it, existing model
configuration, callbacks, authentication and shared-client behavior remain intact.

The initial supported protected path is the exact built-in OpenAI-compatible
Chat Completions client with API-key authentication, for `invoke` and `stream`.
Providing an authority for an unsupported implementation/API/auth/cache mode
fails before the client is created. Protected media generation and cache management
also fail explicitly. Responses, account OAuth, Anthropic, affinity, IntelliRouter,
and custom clients are not covered by this initial contract.

## Host callback

```python
from openjiuwen.core.foundation.llm import Model, ModelRequestTarget

async def authorize(target: ModelRequestTarget):
    # This closure owns the trusted resource reference and execution identity.
    # Recheck current authorization and resolve a credential for this exact URL.
    secret = await host_binding.resolve_for_request(destination=target.url)
    return {"Authorization": f"Bearer {secret}"}

model = Model(client_config, request_config, request_authority=authorize)
```

The example's `host_binding` belongs to the embedding application. Core defines
no organization, project, session identity, credential registry or ambient
context lookup. The host must check the requested model/operation as well as
its destination, and must recheck its current authorization after asynchronous
credential resolution. A callback supplied to a model is not automatically
propagated to other models or auxiliary-model factories.

`ModelRequestTarget` is immutable and contains only:

| Field | Meaning |
| --- | --- |
| `method` | Actual HTTP method; `POST` for this implementation |
| `url` | Actual final HTTP URL |
| `model` | Model from the final serialized HTTP body, after input transforms and overrides |
| `api_mode` | `chat_completions` |
| `operation` | `invoke` or `stream`, from the actual request body |
| `implementation` | `OpenAIModelClient`, with exact implementation validation |

The callback must be asynchronous and return exactly one header containing a
nonempty ASCII `Authorization: Bearer ...` value. `None`, other return shapes,
invalid headers and exceptions deny the request. Cancellation is preserved.
Denial produces `ModelRequestDenied`, a framework error with `fatal=True` and
`recoverable=False`; callers must not retry this denial as a transient failure.
Host exception messages are not attached to the denial or emitted to callbacks.
The agent rail preserves this denial before exception-recovery callbacks, so a
backup model, retry directive or force-finish callback cannot replace it. The
existing after-call observers still run without overriding the original denial.

ModelClientConfig's legacy validation still requires a nonempty API-key field.
For a protected model, construct it using an explicit **non-secret placeholder**;
never resolve the real credential merely to populate this configuration. The
protected Model/client retains a sanitized placeholder and obtains the real
header only at the final HTTP boundary. The callback and resolved credential are
not written to model configuration, request JSON, or the shared client cache.

## HTTP and retry behavior

Each actual HTTP request, including every OpenAI SDK retry, invokes the authority
after SDK request construction. The request method, URL and body are compared
again after the callback completes; changes deny the send. The authorized header
is injected only after that comparison. A denial is translated through the SDK's
non-retryable error path and remains a dedicated framework denial outside it.

Protected requests use a private per-call client, never the process-wide client
cache. HTTP redirects and underlying connection retries are disabled. URL
userinfo, query strings and fragments are unsupported and rejected at construction.
Ambient HTTP proxy settings, OpenAI API key, organization, project and webhook-secret
settings are not inherited by this protected client. No callback means the
legacy behavior continues. An embedding application requiring other proxy/auth
modes must add and verify those modes rather than silently bypass this boundary.

This is request admission: it does not recall a request already sent, cancel an
existing response stream when a grant later changes, or establish ownership of
auxiliary models, Team members or background tasks. Those lifecycle decisions
remain with the host. Opening one supported model transport does not establish
complete Native or Provider acceptance.

## Verification

`tests/unit_tests/core/foundation/llm/test_model_request_authority.py` drives the
real SDK and HTTPX request hooks with an in-memory network peer. It covers current
credential resolution, SDK retries, revocation, instance isolation, transformed
input, request mutation during an await, errors, unsupported modes and legacy
compatibility.

The optional loopback probe makes actual local HTTP and SSE requests with only
synthetic credentials and a dynamically allocated listener:

```sh
PYTHONPATH=. timeout 40s .venv/bin/python tests/system_tests/foundation/llm/_model_request_authority_probe.py --output /tmp/model-authority-probe.json
```

It has an internal 30-second timeout and closes its owned listener/connections.
It does not call an external model service or read user credentials.

## Binding a dynamic host authority once per call

An embedding application whose active execution identity changes over time can
pass a `ModelRequestAuthorityFactory` instead of a static asynchronous callback:

```python
class HostAuthorityFactory:
    def bind_for_call(self):
        binding = capture_current_host_execution()  # synchronous, trusted host

        async def authorize(target):
            secret = await binding.resolve_for_request(destination=target.url)
            return {"Authorization": f"Bearer {secret}"}

        return authorize
```

Core invokes `bind_for_call()` once at logical `Model.invoke/stream` entry,
before any input transform or other await. Every HTTP attempt then revalidates
that same captured callback. It never selects the newer Turn's authority while
an old call is waiting or retrying. The factory must bind synchronously; missing,
invalid, or failing bindings deny the call. Direct built-in client calls receive
the same protection; a client nested within its owning Model reuses that call's
binding. Independent Model calls have distinct bindings even when concurrent.

Stream bindings live until exhaustion/closure, but their task-local ContextVar
is set/reset around each internal `anext` or `aclose` slice, never across a yield
to the caller. Closing from a different task is supported. A background task
that inherited a closed binding cannot reuse it for HTTP admission. This does
not grant child tasks ownership of a parent identity; the host callback must
still prove its subject and execution generation on every request.
