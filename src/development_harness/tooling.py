from __future__ import annotations

import asyncio
import importlib.resources
import os
import platform
import sys
import uuid
import zipfile
from pathlib import Path

import httpx

from .adapters import ADAPTERS
from .backend import Docker
from .config import Config
from .domain import HarnessError
from .git import Git
from .process import run


async def login(root: Path, profile_name: str):
    config = Config.load(root)
    if profile_name not in config.profiles or config.profiles[profile_name].provider != "agy":
        raise HarnessError("login_configuration", "This OAuth bootstrap supports Antigravity profiles; configure Codex auth files or API-key env_names separately")
    os_name = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    machine = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    release_os = "macos" if os_name == "darwin" else os_name
    if not os_name:
        raise HarnessError("unsupported_platform", "No official native ACP helper for this platform")
    tools = root / ".harness" / "tool-cache" / "agy-acp" / "1.3.0"
    tools.mkdir(parents=True, exist_ok=True)
    executable = tools / ("agy_acp_server.exe" if os_name == "windows" else "agy_acp_server.par")
    if not executable.exists():
        archive = tools / "agy.zip"
        url = f"https://dl.google.com/agy-extensions/releases/{release_os}/agy-acp-server-1.3.0-{os_name}-{machine}.zip"
        async with httpx.AsyncClient(timeout=120) as transport:
            response = await transport.get(url)
            response.raise_for_status()
            archive.write_bytes(response.content)
        with zipfile.ZipFile(archive) as files:
            for member in files.infolist():
                target = (tools / member.filename).resolve()
                if not target.is_relative_to(tools.resolve()):
                    raise HarnessError("unsafe_archive", "Archive member escaped the owned tool directory")
            files.extractall(tools)
        if os.name != "nt":
            executable.chmod(0o755)
            for helper in tools.glob("localharness_external*"):
                helper.chmod(0o755)
    cache = root / ".harness" / "credentials" / "gemini"
    cache.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        cache.chmod(0o700)
    else:
        account = os.environ.get("USERDOMAIN", ".") + "\\" + os.environ["USERNAME"]
        await run(["icacls", str(cache), "/inheritance:r", "/grant:r", account + ":(OI)(CI)(F)"])

    def emit(data):
        diagnostic = data.get("diagnostic", "")
        if "Open the following link" in diagnostic:
            print(diagnostic, file=sys.stderr, end="", flush=True)
        elif data.get("unsupported_extension"):
            print("Authentication helper requested unsupported extension: " + data["unsupported_extension"], file=sys.stderr)

    profile = config.profiles[profile_name]
    adapter = ADAPTERS["agy"](profile.model_copy(update={"auth_method": None, "mode": None, "model": None}), emit)
    adapter.workspace = str(root)
    try:
        command = [str(executable), *(["--uid="] if os_name == "linux" else [])]
        await adapter.connect(command, {"GEMINI_HOME": str(cache)})
        await asyncio.wait_for(adapter.connection.authenticate(method_id="oauth-personal"), 600)
        await asyncio.wait_for(adapter.session(None), 60)
        folder = cache / "antigravity-acp"
        required = [folder / "acp_token.json", folder / "settings.json"]
        if not all(file.is_file() for file in required):
            raise HarnessError("credential_location_changed", "Authentication succeeded but expected ACP cache files are absent")
        profile.credential_files = {str(file): "/root/.gemini/antigravity-acp/" + file.name for file in required}
        profile.auth_method = "oauth-personal"
        config.save(root)
        if os.name != "nt":
            for file in required:
                file.chmod(0o600)
        return {"profile": profile_name, "authenticated": True, "credential_directory": str(cache),
                "next": "harness adapters probe --profile " + profile_name + " --session --json"}
    finally:
        await adapter.close()


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
    adapter = ADAPTERS[profile.provider](profile, lambda data: diagnostics.append(data) if len(diagnostics) < 20 else None)
    try:
        work = await git.prepare(id)
        await backend.prepare(id, await git.bundle(id), work["branch"])
        await backend.credentials(id, profile)
        caps = await adapter.connect(backend.agent_command(id, profile, {}), {})
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
