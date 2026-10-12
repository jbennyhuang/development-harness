from __future__ import annotations

import asyncio
import importlib.resources
import os
import uuid
from pathlib import Path

from .adapters import CodingHarnessAdapter, LaunchContext, resolve_adapter_factory
from .adapters.antigravity import login as login
from .backend import Docker
from .config import Config
from .domain import HarnessError
from .git import Git
from .process import run


async def setup_tools(root: Path):
    config = Config.load(root)
    state = root / ".harness"
    env = {**os.environ, "MISE_DATA_DIR": str(state / "tool-cache")}
    tools = [("github:gastownhall/beads", "1.3.1"), ("github:dolthub/dolt", "2.4.2")]
    tools.append(("go", "1.27.2") if os.name == "nt" else ("github:zjrosen/perles", "0.8.97"))
    paths = []
    for tool, version in tools:
        await run(["mise", "install", tool + "@" + version], root, env=env, timeout=900)
        _, out, _ = await run(["mise", "where", tool + "@" + version], root, env=env)
        folder = Path(out.strip())
        paths += [str(folder), str(folder / "bin")]
        if "beads" in tool:
            config.bd_command = [str(folder / ("bd.exe" if os.name == "nt" else "bd"))]
        if tool == "go":
            source = state / "perles-source"
            if not source.exists():
                await run(["git", "-c", "core.hooksPath=", "clone", "--depth", "1", "--branch", "v0.8.97",
                           "https://github.com/zjrosen/perles.git", str(source)], timeout=300)
            bin_dir = state / "bin"
            bin_dir.mkdir(exist_ok=True)
            build_env = {**env, "GOROOT": str(folder), "GOPATH": str(state / "go"), "GOCACHE": str(state / "go-build-cache")}
            await run([str(folder / "bin" / "go.exe"), "build", "-ldflags", "-X main.version=0.8.97",
                       "-o", str(bin_dir / "perles.exe"), "."],
                      source, env=build_env, timeout=900)
            paths += [str(bin_dir)]
    config.tool_paths = list(dict.fromkeys([*paths, *config.tool_paths]))
    config.save(root)
    return {"tools": [tool + "@" + version for tool, version in tools], "tool_paths": config.tool_paths,
            "perles": "Built pinned upstream source for Windows" if os.name == "nt" else "Pinned release binary"}


async def build_image(root: Path):
    config = Config.load(root)
    context = root / ".harness" / "image-build"
    context.mkdir(parents=True, exist_ok=True)
    resource = importlib.resources.files("development_harness").joinpath("resources/worker.Dockerfile")
    (context / "Dockerfile").write_text(resource.read_text())
    # Build the installed source version explicitly; candidate code is never substituted into live workers.
    source = Path(__file__).resolve().parents[2]
    if not (source / "pyproject.toml").exists():
        raise HarnessError("source_required", "Image build requires a source checkout; build before installing the wheel")
    await run(["uv", "build", "--wheel", "--out-dir", str(context)], source)
    wheels = list(context.glob("development_harness-*.whl"))
    wheel = max(wheels, key=lambda x: x.stat().st_mtime)
    # Preserve a valid wheel filename (pip validates distribution/version tags).
    dockerfile = (context / "Dockerfile").read_text().replace("harness.whl", wheel.name)
    (context / "Dockerfile").write_text(dockerfile)
    await run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--output-file", str(context / "requirements.txt")], source)
    code, out, err = await run(["docker", "build", "--platform", "linux/amd64", "--tag", config.image, str(context)],
                               timeout=1800, max_output=1_000_000, check=False)
    if code:
        raise HarnessError("image_build_failed", "Trusted worker image build failed", stdout=out, stderr=err)
    return {"image": config.image, "context": str(context)}


async def probe(root: Path, profile_name: str, session: bool):
    config = Config.load(root)
    if profile_name not in config.profiles:
        raise HarnessError("unknown_profile", profile_name)
    git, backend = Git(root, config.target_branch, config.remote), Docker(root, config)
    id = "probe-" + uuid.uuid4().hex
    profile = config.profiles[profile_name]
    diagnostics = []
    adapter: CodingHarnessAdapter = resolve_adapter_factory(profile.provider)(profile, lambda data: diagnostics.append(data) if len(diagnostics) < 20 else None)
    try:
        work = await git.prepare(id)
        await backend.prepare(id, await git.bundle(id), work["branch"])
        caps = await adapter.start(LaunchContext(backend, id, {}))
        result = {"profile": profile_name, "capabilities": caps, "prompt_verified": False}
        if session:
            session_id, _ = await asyncio.wait_for(adapter.session(None), 45)
            result["session_created"] = bool(session_id)
        return result
    except Exception as exc:
        raise HarnessError("provider_probe_failed", str(exc), capabilities=adapter.capabilities,
                           diagnostics=diagnostics, prompt_verified=False) from exc
    finally:
        await adapter.close()
        await backend.cleanup(id)
        await git.cleanup(id)
