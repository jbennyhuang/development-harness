# Verification record

The first release was exercised on a Windows x86-64 host with Docker Desktop Linux containers on 2026-10-11. Real Codex development and Antigravity review completed two self-improvement issues through the installed harness. This record distinguishes actual provider runs from controlled recovery tests.

## Environment

- uv 0.13.0 with uv-managed CPython 3.14.3; mise 2026.9.18.
- Beads 1.3.1 with Dolt 2.4.2 in server mode, installed in the private project tool cache through mise.
- Perles 0.8.97, built from pinned upstream source with mise-managed Go 1.27.2. Its native Windows binary starts and reports its version.
- Worker image: Linux x86-64, pinned Node 24.21.0, Codex ACP 2.2.2, Antigravity ACP 1.3.0, locked Python dependencies, and the installed harness worker CLI.
- Host runtime: an installed wheel in `.harness/runtime`, separate from the source checkout. Source merges do not reload this service.

## Actual self-improvement

| Beads issue | Result | Automatically integrated commit |
| --- | --- | --- |
| `development-harness-dft` | Added a read-only `harness status` command, catalog/schema integration, documentation, regression checks, and version 0.1.1. | `dfdc7c929e0dbc639ae106c6e554511f6adb8370` |
| `development-harness-tth` | Added role/stage/binding checks for worker credentials and container quiescence before exporting or applying a result, plus cancellation/background-process regression checks and version 0.1.2. | `b9c206c1a17846b11753604c9d5cb281a2f69cc7` |

For both issues, Codex authenticated with the authorized existing sign-in, edited an isolated container clone, committed changes, and submitted evidence through the worker CLI. Antigravity authenticated through its official Google OAuth flow, independently reviewed the changes, prepared a single rebased squash commit, and approved that exact candidate. The host ran configured checks in a fresh validation container, integrated automatically, cleared both issue contexts, closed the Beads issue, and removed its container, worktree, and issue branch. No remote push occurred.

The final status candidate passed 32 tests with the Docker test skipped inside validation. The rebased hardening candidate passed 75 tests with two Docker tests skipped inside validation. Ruff passed for both. Skipping nested Docker is intentional: worker and validation containers have no host Docker socket.

The same durable developer and reviewer identities were reused for the second issue with new issue-specific sessions. During the first review, execution was paused, the service stopped and restarted, and the reviewer retained its identity, issue lock, and ACP session reference. Review and merge preparation subsequently continued through Antigravity's native session restoration.

Two integration failures were found and corrected during the real run:

- Project mise configuration was initially evaluated before Codex startup. Pinning the container executable PATH independently of project mise trust allowed startup and has a real Docker regression check.
- Beads rejected closure under the default actor because the issue belonged to the developer. The harness now closes validated issues as their assigned actor. Resuming the already-merged issue completed closure and cleanup without repeating integration; failure records also preserve structured command diagnostics.

The second issue was based on main before the Beads fix. Its reviewer refreshed target history and rebased onto the newer main commit before validation and integration, exercising coexistence with an intervening host change.

## Regression and release checks

The hardening development candidate passed all 76 tests on Windows with real Docker enabled, including termination of a detached background writer and preservation of its files across container restart.

Final version 0.1.2 checks passed:

- Windows regression suite with `HARNESS_TEST_DOCKER=1`: **77 passed**, including both actual Docker checks.
- Ruff, `uv lock --check`, wheel build, and source distribution build passed.
- The versioned `development-harness-worker:0.1.2` image built successfully. Both authenticated provider adapters negotiated ACP and created sessions with that image; probes removed their temporary containers/worktrees.
- The installed host package reports 0.1.2 and restarted from the existing journal without dispatching work while paused. `doctor` reports all configured prerequisites healthy.
- Final journal state: two completed executions, two idle agents, no issue locks, and no pending guidance. No completed-issue or probe containers, worktrees, or issue branches remain in this project.

Controlled tests also exercise concurrent development/review, revision routing and lock preservation, specification changes, provider quota pausing all dispatch, stalled issues, polling pagination/cursors and unanswered requests, idempotency fencing/reconciliation, journal migration and future-schema rejection, dirty checkout protection, local/remote target movement, exact one-commit approval, already-completed integration recovery, and post-merge closure while dispatch is paused. CLI tests launch a real service subprocess and verify persistence and authenticated access. Git tests use real repositories.

The installed service was explicitly paused and backed up before the upgrade to 0.1.2. The pre-upgrade journal/config backup is `.harness/backups/20261011-031547-21b23d36`; prior wheels remain in `dist`. Beads/Dolt backup is separate from the harness journal backup.

## Remaining limits

- Native macOS/Linux hosts and additional providers have not been verified. Linux-container execution was verified; a Windows/Ubuntu CI matrix is supplied but was not run remotely here.
- Interactive Perles rendering has not been verified. The execution environment could not create an interactive Windows PTY; the compiled binary's startup/help/version were checked.
- Provider session restoration is capability-dependent. Durable workflow recovery preserves recorded state and files; exact continuation in the middle of a model generation or external command cannot be guaranteed.
- Worker containers have outbound network access and the explicitly selected provider credentials. Isolation uses Docker without host project mounts or privileged access; stronger network restrictions and alternative sandbox backends remain extensions.
- Cancellation retains an archive of unfinished work rather than deleting it. Some uncertain administrative mutations require manual inspection and remain fenced; supported reconciliation commands are documented in the README.
- Real runs used one developer and one reviewer. Concurrent multi-issue scheduling and conflict routing were tested with controlled workers and real Git, rather than multiple simultaneous paid provider assignments.
- The external orchestrator interface was used from this Codex session; both providers invoked scoped worker CLI commands. A separate Antigravity orchestrator chat was not exercised.
