import pytest

from development_harness.domain import HarnessError
from development_harness.git import Git
from development_harness.process import run


async def candidate(git, id="a"):
    work = await git.prepare(id)
    path = git.workspace(id)
    (path / "feature.txt").write_text("feature\n")
    await git.call("add", ".", cwd=path)
    await git.call("commit", "-m", "feature", cwd=path)
    return work, await git.oid("HEAD", path)


async def test_one_commit_integration_and_cleanup(repo):
    root, git = repo
    target = await git.latest()
    work, commit = await candidate(git)
    await git.validate_merge_candidate(commit, target["local"], target)
    await git.integrate(commit, target["local"])
    assert (root / "feature.txt").read_text() == "feature\n"
    assert await git.oid("main") == commit
    await git.cleanup("a")
    await git.cleanup("a")
    assert not git.workspace("a").exists()
    assert (await git.call("show-ref", "--verify", "refs/heads/" + work["branch"], check=False))[0] != 0


async def test_dirty_target_preserves_changes_and_ref(repo):
    root, git = repo
    target = await git.latest()
    _, commit = await candidate(git)
    (root / "base.txt").write_text("my uncommitted changes\n")
    with pytest.raises(HarnessError) as error:
        await git.integrate(commit, target["local"])
    assert error.value.code == "dirty_target"
    assert await git.oid("main") == target["local"]
    assert (root / "base.txt").read_text() == "my uncommitted changes\n"


async def test_target_movement_invalidates_prepared_candidate(repo):
    root, git = repo
    target = await git.latest()
    _, commit = await candidate(git)
    (root / "manual.txt").write_text("manual\n")
    await git.call("add", ".")
    await git.call("commit", "-m", "manual change")
    with pytest.raises(HarnessError) as error:
        await git.integrate(commit, target["local"])
    assert error.value.code == "target_moved"
    assert (root / "manual.txt").exists()


async def test_extra_issue_commit_cannot_be_hidden_in_merge_base(repo):
    _, git = repo
    target = await git.latest()
    _, first = await candidate(git)
    path = git.workspace("a")
    await git.call("commit", "--allow-empty", "-m", "second", cwd=path)
    second = await git.oid("HEAD", path)
    with pytest.raises(HarnessError) as error:
        await git.validate_merge_candidate(second, first, target)
    assert error.value.code == "invalid_base"


async def test_latest_fetches_remote_without_changing_local_or_dirty_checkout(repo, tmp_path):
    root, git = repo
    remote = tmp_path / "remote.git"
    await run(["git", "clone", "--bare", str(root), str(remote)])
    clone = tmp_path / "other"
    await run(["git", "clone", str(remote), str(clone)])
    other = Git(clone)
    await other.call("config", "user.name", "Manual")
    await other.call("config", "user.email", "manual@localhost")
    (clone / "remote.txt").write_text("remote\n")
    await other.call("add", ".")
    await other.call("commit", "-m", "remote advance")
    await other.call("push", "origin", "main")
    await git.call("remote", "add", "origin", str(remote))
    git.remote = "origin"
    local = await git.oid("main")
    (root / "base.txt").write_text("dirty\n")
    latest = await git.latest()
    assert latest["local"] == local and latest["remote"] != local
    assert await git.oid("main") == local
    assert (root / "base.txt").read_text() == "dirty\n"


async def test_subdirectory_is_not_silently_treated_as_its_parent_project(repo):
    root, _ = repo
    nested = root / "nested"
    nested.mkdir()
    assert not await Git(nested).initialized()
