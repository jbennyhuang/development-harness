# Development Harness

A project CLI that runs a durable development and review pipeline through Agent Client Protocol (ACP). Your orchestrator uses CLI commands; the host service assigns Beads issues to coding agents, runs trusted checks in isolated containers, and integrates approved changes. Perles provides the issue TUI.

This is an initial implementation. See [the design and operational policies](docs/plan.md) and [verification evidence](docs/verification.md) for the scope actually exercised.

## Install and prepare

Prerequisites: Git, uv, mise, and Docker with Linux containers. The first worker image targets Linux x86-64; Windows host execution is supported. Native macOS/Linux host support uses the same Python interfaces but requires separate verification.

From this repository:

```powershell
uv sync --frozen
uv run harness --help
uv run harness tools setup --json
uv run harness init --target-branch main --json
uv run harness image build --json
```

For another project, use `--project C:/path/to/project`. Missing Git requires an explicit `harness init --initialize-git`. Beads initialization may create its own tracking-metadata commit. Commit your starting project before dispatching workers. If no target branch is supplied for a new configuration, initialization selects the current branch.

`tools setup` installs pinned Beads and Dolt in `.harness/tool-cache` through mise. On Windows it also installs Go through mise and builds the pinned upstream Perles source because upstream does not publish a Windows binary. On Linux/macOS it installs the Perles release. No global PATH changes are needed.

Build/install an executable distribution with `uv build` and `uv tool install dist/development_harness-0.1.7-py3-none-any.whl`. A `harness` entrypoint and `python -m development_harness` are both provided. Build the worker image from this source checkout before installing; image building needs the lockfile and trusted build resources. For self-improvement, run an installed wheel rather than an editable source environment.

## Configure providers and validation

`harness config show --json` reads `.harness/config.json`. Pause execution before updating settings. Structured inputs avoid shell quoting problems:

```json
{
  "changes": {
    "developers": 1,
    "reviewers": 1,
    "max_agents": 2,
    "validation_commands": [
      ["uv", "run", "--frozen", "ruff", "check", "."],
      ["uv", "run", "--frozen", "pytest", "-q"]
    ]
  }
}
```

Save this as a JSON file and run `harness config update --input config-update.json --json`. Checks are configured by the host, run in a fresh validation container, and must be present before execution starts. Candidate code and its dependencies are still executed as part of those checks; the container is their isolation boundary.

Profiles require an explicit registered provider and configure argv, optional ACP mode/model/auth method, allowed environment-variable names, and explicitly selected credential files. The defaults use Codex ACP 2.2.2 and Antigravity ACP 1.3.0 in the worker image. API keys are inherited only when named in `env_names`, and their values are not placed in process arguments. `HARNESS_MAX_AGENTS` overrides pool capacity when the service starts; restart the service to change its environment.

Example Codex sign-in configuration inside its profile:

```json
"credential_files": {"~/.codex/auth.json": "/root/.codex/auth.json"}
```

Credential copying is opt-in. Agents can read the selected credentials inside their container; the harness never mounts your home directory. Antigravity uses its own ACP authentication cache, not the legacy Gemini CLI OAuth files. Run `harness adapters login --profile agy --json` while the service is stopped. It uses the official native ACP server and a project-local `GEMINI_HOME`, displays Google's sign-in link on stderr, and configures the two specific ACP cache files after successful sign-in. The helper comes from [the official ACP registry](https://github.com/agentclientprotocol/registry/blob/main/antigravity-acp/agent.json).

`harness adapters probe --profile codex --session --json` negotiates real capabilities and tests session creation. The same command supports `agy`. A successful probe does not establish successful model prompting, editing, cancellation, or review; those require an actual issue run.

## Operate a project

```powershell
harness inspect --json
harness status --json
harness instructions --json
harness schema --json
harness issue create --input issue.json --key feature-123 --json
harness execution start --json
harness updates poll --consumer orchestrator --json
harness updates ack --consumer orchestrator --cursor 12 --json
harness tui
```

`harness status --json` reads only the durable host journal, without starting the service or calling Beads, Docker, or providers. It reports the project path, execution enabled state and pause reason (`execution.reason`), journaled issue counts by stage with paused and terminal totals, agent counts by role and state with locked totals, and pending guidance count. Terminal issues are done or cancelled; stopped agent identities remain counted. Polling cursors and guidance requests are unchanged. A project without a journal reports empty counts and execution disabled (`not_started`).

An issue input contains `title`, `description`, `acceptance`, and optional priority (0 is highest, 4 lowest). Use `dependency add --id ISSUE --depends-on OTHER` to order work. The orchestrator delegates implementation through issues and agent messages; it does not drive Git or bypass the lifecycle.

The service starts automatically when a control command needs it and keeps working after the CLI or chat exits. `service start/status/stop` provides explicit control. Closing the service interrupts workers and preserves its recorded enabled/paused state, so a restart recovers that intent. To stop dispatch until an explicit resume, use `execution pause` before stopping the service.

The pipeline is development → validation → review → reviewer rebase/squash → validation → host integration → context reset → Beads closure → cleanup. Each issue has a host worktree and branch; workers have an independent Git clone inside a dedicated container. Git bundles transfer candidates and updated target history. Workers have no writable host project mount, shared Git administration, Docker socket, or privileged container.

Developers and reviewers retain their issue lock until merge, including while waiting on revisions. Their logical identities are reusable; issue conversations stay separate. Native session restoration is negotiated. If the provider cannot restore an old session, the harness supplies retained conversation history. Exact continuation mid-command or mid-generation cannot be promised.

Polling returns durable updates, unanswered requests, issue/agent summaries, pagination, and the recommended interval. Acknowledge only updates you handled. Acknowledgment does not answer a request; use `request respond --id REQUEST --response TEXT`. The service continues normal transitions without waiting for chat polls.

Stalled issues pause individually and keep both locks. Provider usage exhaustion pauses the entire project without provider failover. Use `execution resume` after usage is available. `execution pause/resume --id ISSUE` controls one issue. Cancellation stops work and releases locks without closing the issue as successfully merged; its stopped container and worktree remain as an archive to preserve uncommitted changes.

Approved integration fetches current remote history and includes current local history. Any preparation conflict returns to the locked developer. Target movement invalidates the prepared candidate. Dirty target checkouts pause integration; the harness never automatically stashes or resets your checkout. Final target updates are serialized; automatic remote push is disabled.

## Recovery, evidence, and self-improvement

The host journal is `.harness/journal.sqlite`. It persists assignments, stages, operation intent, transcripts, updates, requests, and consumer cursors. The service uses a kernel lock so only one scheduler owns a project. On restart it stops prior issue containers, invalidates old assignment credentials, and reconciles an integration that may already have happened.

Worker submission uses an assignment-scoped bearer credential and generation. Stale workers, wrong result types, mismatched commits, stale specifications, and unapproved merge candidates are rejected. Worker commands are separate from the authenticated host administration interface.

Use `agent messages`, `execution show`, and `artifact show --id ISSUE:validation` to inspect evidence. `backup` creates an online journal/config backup; Beads/Dolt must be backed up separately through Beads. Pending CLI idempotency keys prevent blind repetition after an uncertain side effect. `command inspect/reconcile --idempotency-key KEY` supports interrupted issue/agent creation, agent retirement, cursor acknowledgment, guidance responses, and already-applied issue updates. Other uncertain mutations remain fenced for inspection; completed commands safely replay their recorded result.

For self-improvement, an installed known-good release manages an issue in this source repository. Candidate tests run in disposable containers. Merging source does not hot-reload that running release. Back up state, stop the service, install the new wheel, and restart explicitly. Journal schema mismatches are rejected rather than silently rewritten. Keep the prior wheel and backups for recovery.

## Development checks

```powershell
uv run --frozen ruff check .
uv run --frozen pyright
uv run --frozen pytest -q
$env:HARNESS_TEST_DOCKER = "1"
uv run pytest tests/test_docker.py -q
uv build
```

The ordinary suite uses controlled provider fixtures plus real Git and real CLI/service subprocesses. The Docker check is opt-in. Real provider runs use account quota and are recorded separately in `docs/verification.md`.

At every ACP turn boundary the host revokes the scoped credential, closes the adapter, and stops the issue container before exporting or applying a result. Export briefly restarts only the inert container and stops it again. Errors and cancellations also stop residual commands while retaining files, issue locks, sessions, transcripts, and submitted evidence for retry. Credentials authorize only the currently bound role and workflow stage.


## SDK Agent providers and extension

`adapters/` contains only a package docstring in `__init__.py` and the independent
`codex.py` and `antigravity.py` providers. `provider_types.py` defines `LaunchContext`
and update/emitter/factory types; `providers.py` holds the typed `ADAPTERS` registry.
`sessions.py` holds bounded SDK negotiation and restoration policy used by Engine,
probes and native login. Quota error classification lives in `quota.py`; the general
cancellation shield lives with process utilities in `process.py`. No shared ACP
transport or adapter base is used.
The Engine stores SDK Agent proxies and calls SDK initialize, authenticate,
new_session/load_session, set_session_mode, set_config_option, prompt and cancel.
Initialization occurs once per connection with stable ACP v1 and empty client
capabilities. Session restoration falls back only for a missing session; configured
authentication, mode and model use bounded SDK requests. Recorded issue history
is included when native restoration is unavailable.

Codex and Antigravity independently implement SDK Client callbacks and own their
stdio transport/process lifecycle. Profiles must explicitly name a registered provider.
Their managed contexts yield the actual SDK Agent connection, while callback
receivers remain separate from Agent proxies. Each emitter is instance-local;
permission selection stays inside the issue container, unadvertised capabilities
are rejected, and unsupported extension diagnostics expose parameter keys only.
Antigravity also retains managed native OAuth login and restricted native cache ACLs.

A provider factory accepts `(Profile, Emit, LaunchContext)` and returns
`AsyncContextManager[acp.Agent]`. Register it in `providers.ADAPTERS` before validating a
profile, or inject it through `Engine(adapter_factory=...)`. Third-party providers
need no project inheritance: they can implement SDK Client callbacks and yield
`connect_to_agent(client, writer, reader)` from their own async context manager.
Unknown providers fail before pool, generation, credential or workspace mutation.

The backend owns isolation, credential copies and fenced launch commands. Engine
teardown synchronously revokes the worker credential, then shields transport
closure and backend quiescence against repeated cancellation before integration.
Provider errors, stderr and explicit notices can pause all work for usage limits;
normal response text never triggers that policy. Strict Pyright checks production
and a structural third-party Agent factory, with negative contract tests. Validation
includes Ruff, Pyright, pytest, lock checks and wheel/sdist builds.

`LaunchContext` contains the prepared issue's backend, issue ID, scoped environment,
and workspace. Adapters use `Backend.credentials` for explicitly selected credential
copies and `Backend.agent_command` for a fenced container subprocess command.
The harness/backend retain container ownership checks, isolation/resource policy,
Git bundles, credential revocation, cancellation-shielded shutdown, quiescence and
cleanup. Provider modules must use these facilities rather than bypass them.
Pyright checks all production source and the adapter conformance fixture in strict mode; Ruff remains the lint check.

## License and publication privacy

This project is licensed under the [MIT License](LICENSE), copyright 2026
jbennyhuang. Published package metadata retains the author name without a personal
email address.

Runtime credentials, provider sign-in state, private configuration, and `.harness/`
state are local and ignored by Git. Environment templates such as `.env.example`,
`.env.sample`, and `.env.production.example` may be tracked only with safe
placeholder values. Ignore rules do not protect files already tracked by Git.

Before publication, review personal information in package metadata, historical
blobs, and commit author/committer headers, and scan Git history for secrets;
deleting a present file does not remove its historical contents. The independent
CI secret check uses Gitleaks 8.30.1 with redaction across all fetched Git history.
It does not audit unreachable objects or detect every kind of personal information.
A separate publication copy will be sanitized to remove local Beads metadata and
historical personal emails, then audited before publication. Active repository
history and local runtime state must remain intact during this preparation.
