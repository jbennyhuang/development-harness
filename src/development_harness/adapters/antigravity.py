from __future__ import annotations

import asyncio
import os
import platform
import sys
import zipfile
from pathlib import Path

import httpx

from ..config import Config, Profile
from ..domain import HarnessError
from ..process import run
from ._acp import ACPAdapter
from ._context import LaunchContext


class AntigravityAdapter(ACPAdapter):
    """Antigravity ACP launch with fenced copies of the native OAuth cache."""

    async def start(self, context: LaunchContext) -> dict:
        self.workspace = context.workspace
        await context.backend.credentials(context.issue_id, self.profile)
        command = context.backend.agent_command(context.issue_id, self.profile, context.env)
        return await self.connect(command, context.env)


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
    adapter = AntigravityAdapter(profile.model_copy(update={"auth_method": None, "mode": None, "model": None}), emit)
    adapter.workspace = str(root)
    try:
        command = [str(executable), *(["--uid="] if os_name == "linux" else [])]
        await adapter.connect(command, {"GEMINI_HOME": str(cache)})
        await adapter.authenticate("oauth-personal", timeout=600)
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


def default_profile() -> Profile:
    """Pinned container helper; Linux uses an empty UID for the isolated root user."""
    return Profile(provider="agy", command=["/opt/agy/agy_acp_server.par", "--uid="])
