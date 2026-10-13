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
        _, node_version, _ = await run(backend.agent_command(id, Profile(provider="codex", command=["node", "--version"]), {}))
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


@pytest.mark.skipif(os.getenv("HARNESS_TEST_DOCKER") != "1", reason="Opt-in actual Docker integration")
async def test_quiesce_kills_background_writer_and_restart_preserves_files(repo):
    import asyncio

    root, git = repo
    backend = Docker(root, Config(remote=None, command_timeout=15))
    id = "background-writer"
    try:
        work = await git.prepare(id)
        await backend.prepare(id, await git.bundle(id), work["branch"])
        # Detached exec outlives the client; stopping that client cannot stop this writer.
        await run(["docker", "exec", "--detach", backend.name(id), "python", "-c",
                   "import time\nfrom pathlib import Path\np=Path('/workspace/writer.txt')\n"
                   "while True:\n with p.open('a') as f: f.write('tick\\n')\n time.sleep(.05)"], timeout=15)
        async def written():
            while True:
                code, out, _ = await backend.exec(backend.name(id), ["cat", "writer.txt"], check=False)
                if not code and len(out.splitlines()) >= 3:
                    return
                await asyncio.sleep(.05)
        await asyncio.wait_for(written(), 10)
        await backend.quiesce(id)
        assert not (await backend.inspect(backend.name(id)))["State"]["Running"]
        await backend.prepare(id, await git.bundle(id), work["branch"])
        _, before, _ = await backend.exec(backend.name(id), ["cat", "writer.txt"])
        await asyncio.sleep(.3)
        _, after, _ = await backend.exec(backend.name(id), ["cat", "writer.txt"])
        assert before == after and len(after.splitlines()) >= 3
        await backend.quiesce(id)
    finally:
        await backend.cleanup(id)
        await git.cleanup(id)
