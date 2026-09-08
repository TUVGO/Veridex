from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


_ENV_ALLOW = {
    "PATH", "PATHEXT", "SystemRoot", "WINDIR", "TEMP", "TMP", "HOME",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC",
    "PYTHONUTF8", "PYTHONIOENCODING", "NO_COLOR", "CI",
}


def sanitized_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Pass runtime essentials only; never forward QA_* credentials."""
    env = {key: value for key, value in os.environ.items() if key in _ENV_ALLOW}
    if extra:
        env.update({key: str(value) for key, value in extra.items() if value is not None})
    return env


def resolve_executable(value: str) -> str:
    candidate = Path(value)
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if not resolved:
        raise ValueError(f"找不到外部工具：{value}")
    return resolved


def tool_version(executable: str, runner=subprocess.run) -> dict:
    try:
        path = resolve_executable(executable)
    except ValueError:
        return {"available": False, "path": None, "version": "not installed"}
    for args in ([path, "--version"], [path, "--help"]):
        result = runner(
            args,
            env=sanitized_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode == 0:
            first_line = output.splitlines()[0] if output else "available"
            return {"available": True, "path": path, "version": first_line}
    return {"available": False, "path": path, "version": "command failed"}
