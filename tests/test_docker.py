import os

import pytest

from development_harness.backend import Docker
from development_harness.config import Config, Profile
from development_harness.process import run


@pytest.mark.skipif(os.getenv("HARNESS_TEST_DOCKER") != "1", reason="Opt-in actual Docker integration")
async def test_isolated_container_candidate_roundtrip_validation_and_recovery(repo):
    root, git = repo
    config = Config(remote=None, validation_commands=[["git", "rev-parse", "--verify", "HEAD"]])
    backend = Docker(root, config)
    id = "docker-fixture"
    try:
        (root / "mise.toml").write_text('[tools]\nnode = "24.21.0"\n')
        await git.call("add", "mise.toml")
        await git.call("commit", "-m", "Project has an untrusted mise configuration")
        work = await git.prepare(id)
        await backend.prepare(id, await git.bundle(id), work["branch"])
        info = await backend.inspect(backend.name(id))
        assert not info["Mounts"] and not info["HostConfig"]["Privileged"]
        _, out, _ = await backend.exec(backend.name(id), ["git", "rev-parse", "--git-dir"])
        assert out.strip() == ".git"
        _, node_version, _ = await run(backend.agent_command(id, Profile(command=["node", "--version"]), {}))
        assert node_version.strip() == "v24.21.0"
        await backend.exec(backend.name(id), ["git", "commit", "--allow-empty", "-m", "container candidate"])
        _, head, _ = await backend.exec(backend.name(id), ["git", "rev-parse", "HEAD"])
        candidate = await git.import_candidate(id, await backend.export(id))
        assert candidate == head.strip()
        await backend.quiesce(id)
        await backend.prepare(id, await git.bundle(id), work["branch"])
        _, resumed, _ = await backend.exec(backend.name(id), ["git", "rev-parse", "HEAD"])
        assert resumed.strip() == candidate
        await backend.refresh(id, await git.bundle(id, "target", ["refs/heads/main"]))
        checks = await backend.validate(id, await git.bundle(id, "validation"))
        assert checks[0]["returncode"] == 0 and candidate in checks[0]["stdout"]
    finally:
        await backend.cleanup(id)
        await git.cleanup(id)
    assert await backend.inspect(backend.name(id)) is None
