# F_41 OpenCode abort event reconciliation

| 项 | 值 |
|---|---|
| 日期 | 2026-10-05 |
| 关联 spec | `S_19_harness-providers.md` |
| 关联模块 | `openjiuwen/harness_providers/opencode/mapping.py`, `harness.py` |

## Problem and decision

The fixed CLI can emit `session.error(MessageAbortedError)`, native idle,
then the current root's completed aborted assistant snapshot, then another idle.
Treating the first event as a fatal provider error closes the original stream
before the correlated terminal evidence arrives. The shared supervisor can
subsequently become idle while the durable checkpoint correctly remains active.

For the exact Session and an already requested abort, this one error name is now
a nonterminal hint. It does not establish message identity, completion, idle, or
a checkpoint. Other errors retain the original failure path; foreign Session
events retain the original ignore behavior.

An aborted terminal candidate must be the latest assistant associated with the
current user root, have a completion timestamp and exact `MessageAbortedError`.
A newer assistant supersedes an older candidate; an older late snapshot cannot
restore it. Only a subsequent native idle triggers GET of that exact candidate.
Readback must match message ID, Session, user root, assistant role, completion,
and error name before the existing checkpoint publication marks idle. This
reuses the existing reconciliation and serialized Turn lifecycle.

## Rejected alternatives and compatibility

No error event or abort HTTP acknowledgment can create a resumable checkpoint.
No synthetic idle, history reset, silent NEW fallback, second stream consumer,
or permissive cold-resume path was added. An incomplete/unmatched message,
missing idle, failed readback, or unrelated native error retains an uncertain
checkpoint and the existing fail-closed resume behavior.

Public protocol and Provider APIs are unchanged. Existing aborted fixture
transports now supply the native persisted message readback, as real HTTP does.
Native idle does not guarantee that the host checkpoint sink successfully saved;
that existing independent persistence condition still applies.

## Verification and limits

The original baseline rejects the ordinary session-error cancellation ordering.
Controlled native transport tests exercise the real OpenCode harness, including
the captured error → early idle → correlated message → idle order, reverse
error/message order, latest-message selection, malformed/foreign readback,
missing proof, terminal invariants and strict cold resume. The Provider test
suite passed 695 cases with one existing conditional skip during implementation.

These are deterministic components, not new CLI acceptance. The integrating
host must keep its original observer alive during graceful abort, then confirm
actual resource exit. Exact installed-source stable and real fixed-CLI
cancellation/continuation verification remain separate integration gates.
