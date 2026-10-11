# Verification record

Baseline verification completed on a Windows x86-64 host with Docker Desktop Linux containers (2026-10-11). Self-improvement verification is in progress.

- Python runtime: uv-managed CPython 3.14.3.
- Tool management: mise 2026.9.18; Beads 1.3.1 and Dolt 2.4.2 installed in the private project tool cache.
- Perles 0.8.97: built from its pinned upstream source using mise-managed Go 1.27.2; native Windows executable starts and reports version information. Interactive TUI operation remains to be checked.
- Codex ACP 2.2.2: real protocol negotiation and authenticated session creation succeeded inside an isolated container. An actual development assignment remains to be checked.
- Antigravity ACP 1.3.0: real protocol negotiation succeeded; official Google OAuth sign-in and session creation succeeded on the host and in an isolated container. An actual review assignment remains to be checked.
- Journal/Git/workflow tests: atomic transition records, polling pagination/reconnect, unresolved guidance, idempotency fencing, future-schema rejection, issue locks, revision routing, quota pause, completed-merge recovery, dirty target protection, manual target movement, and one-commit integration have been exercised.
- CLI/service: real subprocess startup, persistence across client exit, restart, durable agent/cursor state, authenticated access, explicit Git initialization, and JSON errors have been exercised.
- Docker: no host mounts or privileged execution; candidate bundle round-trip, clean validation checkout, container restart, target refresh, and cleanup passed.
- Baseline regression result: 31 tests passed with the real Docker check enabled; Ruff and wheel/source distribution builds passed. The suite also checks concurrent development/review, specification invalidation, interrupted command reconciliation, and completion of already-merged issues while dispatch is paused.

Fixture success does not establish provider compatibility. The self-improvement run and remaining limitations will be recorded here after actual execution.
