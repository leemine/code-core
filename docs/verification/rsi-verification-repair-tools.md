# RSI verification repair file tools

The verification repair agent was instructed to edit the role integration
worktree, but its factory supplied no filesystem tools. Unlike action execution,
this path has no structured output writer after `invoke`, so repair descriptions
could not change the failing package.

The repair factory now uses the existing read, write and edit tools with an
explicit sandbox root equal to that role's integration directory. It exposes no
shell tool. Failed agent construction releases the new system operation. The
repair caller also releases its operation after success, exception or cancellation
while preserving unrelated operations. Action execution retains its existing
structured-file flow. The deterministic verifier
still decides whether repairs succeeded.

Validation on the candidate based on `68bf2754`:

- `test_verification_repair_tools.py` and `test_member_optimizer.py`: 167 passed.
  Tests exercise real file reads/writes, other-role paths, `..`, symlink escape,
  public factory wiring, construction-failure cleanup, and invocation cleanup
  after success, exception and cancellation.
- Factory and new tests: targeted Ruff passed; `git diff --check` passed.
  The touched verification module reports two pre-existing ASYNC240 diagnostics
  at unrelated worktree output-path operations, reproduced against HEAD.
  No suppression or baseline relaxation was added.
- A real Volcano call against a copied synthetic failing package repaired the
  duplicated `files/files` reference. Static verification still rejected the
  remaining invalid `id` / `phase` fields. This is partial evidence, not a
  successful repair or a published RSI artifact.
- The next remote attempt exhausted the existing test relay's request budget.
  A further bounded remote verification was denied by automatic approval review
  pending explicit authorization to send the synthetic package contents.
- Before the additional invocation-cleanup change, local strict stable
  `20261009T131443-243c136d`: 2261 passed, with zero
  failures, errors, timeouts, skips, blocked or unrun tests; inventory closed.
  The updated candidate also passed strict stable `20261009T133632-88de4290`:
  2261 passed, all other totals zero, planned=executed and closed=true.
  Clean CI and end-to-end RSI artifact publication, download and installation
  have not passed for this candidate.
- `make check`: formatting and spelling passed using the installed CLI tools.
  It is still not a clean pass: Ruff reports the two existing diagnostics above;
  public lazy Runner imports trigger pylint E0611, and the verification module
  retains its legacy complexity diagnostics. The target ignores subprocess
  failures, so its exit status is not used as a successful quality check.

Local developer verification used an isolated HOME/cache/tmp and the Core
candidate on `PYTHONPATH`; it is not downstream Swarm locked-source acceptance.
The downstream lock remains on the already merged `68bf2754` until this separate
Core fix passes its own delivery checks and is merged.

The first invocation-cleanup test run stalled inside the execution sandbox and
exited 124 without a test result; the isolated host rerun passed all 167 tests.
A make invocation selected uv and attempted an unavailable Python download; it
was rerun using the already installed CLI executables on PATH with RUN_CMD empty.
The intervening python -m override incorrectly selected the nonexistent codespell
module; the installed command uses codespell_lib. No package or interpreter was
newly installed, and failed invocations remain recorded.


## 2026-10-09 authoring and initialization follow-up

The repair profile disables automatic general-purpose workspace scaffolding:
the role package must not acquire AGENT/identity/memory files during repair.
The original upstream factory already creates a system operation implicitly,
so this behavior was not first introduced by adding explicit file tools.

Prompt authoring now uses the runtime loader's existing normalization and strict
PromptSectionSpec during the original action retry loop, for both add and modify.
Invalid entries left beside a generated registration are rejected with actionable
errors rather than silently removed or reinterpreted. No schema is relaxed and
no additional retry loop is introduced. Repair context maps prompt-section
failures to sections.yaml. Author and repair prompts describe valid fields and
package-relative file references. Public APIs and other agent profiles are unchanged.

Repair isolation, prompt authoring, member optimizer and semantic authoring tests
pass (189 tests, isolated HOME, no model calls). This supersedes the earlier
167-test scope. Additional iterative/checkpoint/plugin-manifest regression:
117 passed. Strict stable `20261009T152058-1afcaa89`: 2261 passed, all other
counts zero, planned=executed=2261, closed=true, strict network isolation.

Real Volcano glm-5.2 repair used ReadFile/EditFile to replace the invalid
id/phase manifest with the required name/file fields in one repair attempt.
Static verification passed and the package retained exactly its original three
files (no identity/memory scaffold). A separate automatic image-capability
probe returned HTTP 400; it did not prevent the successful text repair.

With the Swarm instance-owner model snapshot fix in joint development, a real
synthetic experiment `rsi-9e514b47` completed in one iteration: exact-match
baseline 0.0, best 1.0, candidate accepted, publication_status=published.
This is not locked downstream acceptance: Swarm download/preview fixes and
the complete application acceptance remain open. Clean Core CI is pending.
Earlier fixture evaluation-method and placeholder-key failures remain recorded.

The four action_executor Ruff diagnostics are reproduced against HEAD (three
ASYNC240 and one ASYNC230); the two verification diagnostics and pylint legacy
diagnostics described above remain. No suppression or gate relaxation was added.
Earlier test-proxy quotas were superseded by user authorization; they do not
enter product code.
