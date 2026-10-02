# OpenCode safe terminal diagnostics

| Metadata | Value |
|---|---|
| Date | 2026-10-02 |
| Scope | OpenCode error mapping and existing Turn terminal projection |
| Tests | `tests/unit_tests/harness_providers/test_opencode.py` |
| Refs | S_19_harness-providers.md |

## Background

A native failure lost its error name during normalization. The remaining generic
failure message could not distinguish the native error families in Team history.

## Data and state

The existing immutable TurnError carries whitelisted names and event sources in
its OpenCode namespace and message. No new event, storage, or lifecycle exists.

## Decisions

Use fixed labels for native names and event sources. Unknown or malformed names
map to `unrecognized`; HTTP category mapping and requested-abort semantics remain
unchanged. Message-only host projections retain the same safe labels.

## Rejected alternatives

Do not copy native exception messages or response bodies: they can include prompts
and credentials. Do not add a second diagnostic history or replay failed input.

## Validation

Deterministic tests cover both native event paths, recognized and malformed names,
HTTP rate limits, terminal failure and exclusion of secret-bearing raw bodies.

## Known limitations

This improves future diagnosis. An old error without native records cannot be
reconstructed from the new fields; a safe error family alone is not a root cause.
