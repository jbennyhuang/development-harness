import asyncio
from contextlib import asynccontextmanager

import pytest
from acp.schema import (
    AgentCapabilities,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
)

from development_harness.config import Config
from development_harness.engine import Engine
from development_harness.git import Git
from development_harness.process import run
from development_harness.store import Store


@pytest.fixture
async def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    await run(["git", "init", "-b", "main", str(root)])
    git = Git(root, remote=None)
    await git.call("config", "user.name", "Harness Tests")
    await git.call("config", "user.email", "tests@localhost")
    (root / ".gitignore").write_text(".harness/\n")
    (root / "base.txt").write_text("baseline\n")
    await git.call("add", ".")
    await git.call("commit", "-m", "baseline")
    return root, git


class MemoryTracker:
    def __init__(self):
        self.issues = {}
        self.command = []

    async def create(self, title="Feature", description="Implement it", acceptance="feature.txt exists", priority=2, key="test"):
        id = "issue-" + key
        self.issues.setdefault(id, {"id": id, "title": title, "description": description,
                                   "acceptance_criteria": acceptance, "priority": priority, "status": "open"})
        return dict(self.issues[id])

    async def read(self, id):
        return dict(self.issues[id])

    async def list(self):
        return [dict(x) for x in self.issues.values()]

    async def ready(self):
        return [x for x in await self.list() if x["status"] == "open"]

    async def update(self, id, changes):
        self.issues[id].update(changes)
        return await self.read(id)

    async def close(self, id, reason):
        return await self.update(id, {"status": "closed"})

    async def delete(self, id):
        self.issues.pop(id)
        return {"id": id, "deleted": True}


class IsolatedTestBackend:
    """Test-only Git clone backend. Production always uses an isolated container."""
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.stopped = []
        self.deleted = []
        self.validations = 0
        self.validation_failure = False

    def path(self, id):
        return self.root / ".harness" / "test-containers" / Git.slug(id)

    async def prepare(self, id, bundle, branch):
        path = self.path(id)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        await run(["git", "clone", str(bundle), str(path)])
        git = Git(path)
        await git.call("checkout", branch)
        await git.call("config", "user.name", "Fixture Worker")
        await git.call("config", "user.email", "fixture@localhost")

    async def export(self, id):
        path = self.root / ".harness" / "artifacts" / Git.slug(id) / "test-candidate.bundle"
        if path.exists():
            path.unlink()
        await Git(self.path(id)).call("bundle", "create", str(path), "HEAD")
        return path

    async def refresh(self, id, bundle):
        await Git(self.path(id)).call("fetch", str(bundle), "+refs/heads/*:refs/remotes/harness/*")

    async def validate(self, id, bundle):
        self.validations += 1
        return [{"command": ["fixture-check"], "returncode": int(self.validation_failure), "stdout": "", "stderr": ""}]

    async def quiesce(self, id):
        self.stopped.append(id)

    async def cleanup(self, id):
        self.deleted.append(id)

    def agent_command(self, id, profile, env):
        return [id]


class FixtureAdapter:
    def __init__(self, engine, profile, emit):
        self.profile = profile
        self.engine, self.emit = engine, emit
        self.env = {}
        self.session_calls = []

    capabilities = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}

    async def initialize(self, **kwargs):
        return InitializeResponse(protocol_version=1, agent_capabilities=AgentCapabilities(load_session=True))

    async def new_session(self, **kwargs):
        self.session_calls.append(None)
        return NewSessionResponse(session_id="fixture-session")

    async def load_session(self, session_id, **kwargs):
        self.session_calls.append(session_id)
        return LoadSessionResponse()

    async def authenticate(self, **kwargs):
        pass

    async def set_session_mode(self, **kwargs):
        pass

    async def set_config_option(self, **kwargs):
        pass

    async def prompt(self, session_id, prompt):
        assignment = self.engine.worker_assignment(self.env["HARNESS_WORKER_TOKEN"])
        e = assignment["execution"]
        git = Git(self.engine.backend.path(e["id"]))
        if e["stage"] == "developing":
            (git.root / "feature.txt").write_text("implemented " + e["id"] + "\n")
            await git.call("add", ".")
            await git.call("commit", "--allow-empty", "-m", "Implement " + e["id"])
            result = {"kind": "development", "commit": await git.oid("HEAD"), "summary": "Implemented"}
        elif e["stage"] == "reviewing":
            result = {"kind": "review", "verdict": "approved", "commit": await git.oid("HEAD"), "summary": "Reviewed"}
        else:
            base = e["target"]["local"]
            await git.call("rebase", base)
            await git.call("reset", "--soft", base)
            await git.call("commit", "--allow-empty", "-m", "Squash " + e["id"])
            result = {"kind": "merge", "verdict": "approved", "base": base,
                      "commit": await git.oid("HEAD"), "summary": "Prepared and reviewed"}
        self.engine.submit(self.env["HARNESS_WORKER_TOKEN"], result)
        return PromptResponse(stop_reason="end_turn")

    async def close(self):
        pass

    async def cancel(self, session_id):
        pass


@asynccontextmanager
async def fixture_provider(agent, context):
    agent.env = context.env
    try:
        yield agent
    finally:
        await agent.close()


@pytest.fixture
async def engine(repo):
    root, git = repo
    config = Config(developers=1, reviewers=1, max_agents=2, remote=None,
                    validation_commands=[["python", "-m", "pytest"]], reconcile_interval=1)
    tracker = MemoryTracker()
    backend = IsolatedTestBackend(root, config)
    store = Store(root / ".harness" / "journal.sqlite")
    e = Engine(root, config, store, tracker, backend, git)
    e.adapter_factory = lambda profile, emit, context: fixture_provider(FixtureAdapter(e, profile, emit), context)
    yield e
    await e.close()
    store.close()


async def advance(engine, predicate, timeout=20):
    async def drive():
        while not predicate():
            await engine.tick()
            await asyncio.sleep(0.01)
    await asyncio.wait_for(drive(), timeout)
