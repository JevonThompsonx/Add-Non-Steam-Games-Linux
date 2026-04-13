from __future__ import annotations

from functools import lru_cache
import os
import re
from pathlib import Path


@lru_cache(maxsize=1)
def load_local_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().with_name(".env")
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_env_value(*names: str) -> str | None:
    local_env = load_local_env()
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
        value = local_env.get(name)
        if value:
            return value.strip()
    return None


def _parse_scan_dirs(value: str | None) -> list[Path]:
    """Parse a colon- or comma-separated list of directories to scan."""
    if not value:
        return []

    dirs: list[Path] = []
    seen: set[str] = set()
    for raw_part in re.split(r"[;:,]", value):
        part = raw_part.strip().strip('"').strip("'")
        if not part:
            continue
        path = Path(part).expanduser()
        identity = str(path)
        if identity in seen:
            continue
        seen.add(identity)
        dirs.append(path)
    return dirs


MIN_PYTHON = (3, 10)
LOG_FILE_NAME = "steam-game-manager.log"

# Linux Steam install locations (checked in order)
DEFAULT_STEAM_PATHS = [
    Path.home() / ".local" / "share" / "Steam",
    Path.home() / ".steam" / "steam",
    Path.home() / ".steam" / "debian-installation",
    Path("/usr/share/steam"),
    Path("/usr/games/steam"),
]

# Flatpak Steam
FLATPAK_STEAM_PATH = Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"

# Known game directories on this CachyOS system
HOME = Path.home()
KNOWN_GAME_DIRS = [
    HOME / "Games",
    HOME / ".local" / "share" / "lutris" / "games",
    HOME / "snap" / "steam" / "common" / ".local" / "share" / "Steam" / "steamapps" / "common",
    Path("/mnt/games"),
    Path("/opt/games"),
    Path("/opt/GOG Games"),
]

# Additional scan paths from env
_extra_scan_dirs = _parse_scan_dirs(get_env_value("SCAN_DIRS", "GAME_SCAN_DIRS"))
if _extra_scan_dirs:
    KNOWN_GAME_DIRS = _extra_scan_dirs + KNOWN_GAME_DIRS

SYSTEM_SKIP_DIRS = {
    "bin",
    "boot",
    "dev",
    "etc",
    "lib",
    "lib32",
    "lib64",
    "lost+found",
    "mnt",
    "proc",
    "root",
    "run",
    "sbin",
    "srv",
    "sys",
    "tmp",
    "usr",
    "var",
}

# Directories inside game folders to skip when scanning for executables
SKIP_DIRS = {
    "_commonredist",
    "_emulators",
    "__installer",
    "__support",
    "_redist",
    "artbookost",
    "directx",
    "dotnet",
    "easyanticheat",
    "emu",
    "md5",
    "nodvd",
    "plugins",
    "prerequisites",
    "redist",
    "soundtrack",
    "streamingassets",
    "support",
    "tools",
    "vcredist",
    ".git",
    ".github",
    "node_modules",
    "__pycache__",
    "add-non-steam-games-linux",   # skip this tool's own directory if inside ~/Games
}

# Executable stems/names to skip (Linux: no .exe, just stem)
SKIP_EXE_STEMS = {
    "cemu",
    "citron",
    "dolphin",
    "eden",
    "ryujinx",
    "sudachi",
    "install",
    "uninstall",
    "setup",
    "crashreporter",
    "bugreporter",
    "updater",
    "patcher",
    "update",
    "configure",
    "config",        # catches config.exe (e.g. Ys I/II GOG releases)
    "python",
    "python3",
    "bash",
    "sh",
    "zsh",
    "fish",
}

# Glob patterns for executable filenames to skip
SKIP_EXE_PATTERNS: set[str] = {
    "unins*",
    "install*",
    "uninstall*",
    "setup*",
    "crash*",
    "bug_reporter*",
    "update*",
    "patcher*",
}

SKIP_CANDIDATE_APP_NAMES = {
    "game",
    "launcher",
    "ldc",
    "ls",
    "emu",
    "tool",
    "bin",
    "codex",
    "redist",
    "steam",
    "proton",
    "wine",
    "x64",
    "x86",
    "start",
    "run",
    "app",
}

SKIP_PATH_KEYWORDS = {
    "artbook",
    "easyanticheat",
    "emu",
    "emulator",
    "goldberg",
    "md5",
    "nodvd",
    "plugin",
    "redist",
    "soundtrack",
    "streamingassets",
    "tool",
}

GENERIC_CONTAINER_DIRS = {
    "app",
    "bin",
    "binaries",
    "game",
    "games",
    "x64",
    "x86",
    "linux",
    "linux64",
    "linux32",
}

GAME_HINT_FILES = {
    "gameinfo.txt",
    "libsteam_api.so",
    "steam_api.so",
    "UnityPlayer.so",
    "UnityPlayer.dll",   # Windows Unity games running via Proton
    "libunity.so",
    "game.pck",    # Godot games
    "data.pck",    # Godot games
}

GAME_HINT_PREFIXES = (
    "ue4-",
    "ue5-",
)

MAX_SCAN_DEPTH = 3
MAX_EXECUTABLE_SCAN_DEPTH = 3

API_KEY_ENV_NAMES = ("STEAMGRIDDB_API_KEY",)
ENV_FILE_NAMES = (".env",)

ARTWORK_REQUEST_DELAY_SECONDS = 0.5
ARTWORK_MAX_RETRIES = 3

ARTWORK_REQUESTS = {
    "portrait": {
        "endpoint": "/grids/game/{game_id}",
        "params": {
            "dimensions": "600x900",
            "styles": "alternate",
            "types": "static",
            "nsfw": "false",
        },
        "filename": "{appid}p{ext}",
    },
    "horizontal": {
        "endpoint": "/grids/game/{game_id}",
        "params": {
            "dimensions": "920x430",
            "styles": "alternate",
            "types": "static",
            "nsfw": "false",
        },
        "filename": "{appid}{ext}",
    },
    "hero": {
        "endpoint": "/heroes/game/{game_id}",
        "params": {
            "types": "static",
            "nsfw": "false",
        },
        "filename": "{appid}_hero{ext}",
    },
    "logo": {
        "endpoint": "/logos/game/{game_id}",
        "params": {
            "types": "static",
            "nsfw": "false",
            "mimes": "image/png",
        },
        "filename": "{appid}_logo{ext}",
    },
    "icon": {
        "endpoint": "/icons/game/{game_id}",
        "params": {
            "types": "static",
            "nsfw": "false",
        },
        "filename": "{appid}_icon{ext}",
    },
}

DUMMY_SHORTCUT_NAME = "Steam Game Manager Validation"
# On Linux, point to a real executable that always exists
DUMMY_SHORTCUT_EXE = "/usr/bin/env"

REQUIRED_SHORTCUT_DEFAULTS = {
    "ShortcutPath": "",
    "LaunchOptions": "",
    "IsHidden": 0,
    "AllowDesktopConfig": 1,
    "AllowOverlay": 1,
    "OpenVR": 0,
    "Devkit": 0,
    "DevkitGameID": "",
    "DevkitOverrideAppID": 0,
    "LastPlayTime": 0,
    "FlatpakAppID": "",
}

KNOWN_SHORTCUT_FIELDS = {
    "appid",
    "AppName",
    "StartDir",
    "icon",
    "ShortcutPath",
    "LaunchOptions",
    "IsHidden",
    "AllowDesktopConfig",
    "AllowOverlay",
    "Exe",
    "OpenVR",
    "openvr",
    "sortas",
    "Devkit",
    "DevkitGameID",
    "DevkitOverrideAppID",
    "LastPlayTime",
    "FlatpakAppID",
    "tags",
}
