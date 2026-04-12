from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import DEFAULT_STEAM_PATHS, FLATPAK_STEAM_PATH


@dataclass(slots=True)
class SteamUser:
    user_id: str
    userdata_dir: Path
    config_dir: Path
    shortcuts_path: Path
    grid_dir: Path
    last_modified: datetime


def find_steam_install_path() -> Path | None:
    # 1. Honour explicit override
    env_value = os.environ.get("STEAM_PATH")
    if env_value:
        env_path = Path(env_value).expanduser()
        if env_path.exists():
            return env_path

    # 2. Flatpak Steam
    if FLATPAK_STEAM_PATH.exists():
        return FLATPAK_STEAM_PATH

    # 3. Standard XDG / distribution locations
    for path in DEFAULT_STEAM_PATHS:
        expanded = path.expanduser()
        if expanded.exists():
            return expanded

    # 4. Ask the OS where the steam binary lives and derive from there
    try:
        result = subprocess.run(
            ["which", "steam"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            steam_bin = Path(result.stdout.strip())
            # Resolve symlinks (e.g. /usr/bin/steam -> real location)
            real = steam_bin.resolve()
            # Heuristic: walk up to find the Steam data dir
            for candidate in (real.parent, real.parent.parent):
                steam_dir = candidate / "steamapps"
                if steam_dir.exists():
                    return candidate
    except OSError:
        pass

    return None


def _steam3_to_account_id(steam_id_64: str) -> str | None:
    try:
        steam_id = int(str(steam_id_64).strip())
    except (TypeError, ValueError):
        return None
    if steam_id < 76561197960265728:
        return None
    return str(steam_id - 76561197960265728)


def _load_login_user_ids(steam_path: Path) -> set[str] | None:
    loginusers_path = steam_path / "config" / "loginusers.vdf"
    if not loginusers_path.exists():
        return None

    try:
        import importlib
        vdf = importlib.import_module("vdf")
        with loginusers_path.open("r", encoding="utf-8") as handle:
            data = vdf.load(handle)
    except (ImportError, OSError, UnicodeDecodeError, ValueError) as error:
        import logging
        logging.getLogger(__name__).warning(
            "Could not parse %s (%s); falling back to userdata directory scan",
            loginusers_path,
            error,
        )
        return None

    users = data.get("users", {})
    if not isinstance(users, dict):
        return None

    account_ids = {
        account_id
        for steam_id_64 in users
        for account_id in [_steam3_to_account_id(str(steam_id_64))]
        if account_id
    }
    return account_ids or None


def list_steam_users(steam_path: Path) -> list[SteamUser]:
    userdata_root = steam_path / "userdata"
    if not userdata_root.exists():
        return []

    login_user_ids = _load_login_user_ids(steam_path)
    users: list[SteamUser] = []
    for entry in userdata_root.iterdir():
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        if login_user_ids is not None and entry.name not in login_user_ids:
            continue
        config_dir = entry / "config"
        stat_source = config_dir if config_dir.exists() else entry
        users.append(
            SteamUser(
                user_id=entry.name,
                userdata_dir=entry,
                config_dir=config_dir,
                shortcuts_path=config_dir / "shortcuts.vdf",
                grid_dir=config_dir / "grid",
                last_modified=datetime.fromtimestamp(stat_source.stat().st_mtime),
            )
        )

    return sorted(users, key=lambda item: item.last_modified, reverse=True)


def is_steam_running() -> bool:
    """Return True if Steam is currently running (Linux version)."""
    # pgrep is available on all Linux distros including CachyOS
    try:
        result = subprocess.run(
            ["pgrep", "-x", "steam"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return True
    except OSError:
        pass

    # Fallback: check /proc for steam processes
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            comm_file = pid_dir / "comm"
            try:
                if comm_file.exists() and comm_file.read_text().strip().lower() == "steam":
                    return True
            except OSError:
                continue
    except OSError:
        pass

    return False


def load_libraryfolders(steam_path: Path) -> list[Path]:
    import importlib
    vdf = importlib.import_module("vdf")

    library_file = steam_path / "steamapps" / "libraryfolders.vdf"
    libraries: set[Path] = {steam_path}
    if not library_file.exists():
        return sorted(libraries, key=lambda path: str(path).lower())

    with library_file.open("r", encoding="utf-8") as handle:
        data = vdf.load(handle)

    libraryfolders = data.get("libraryfolders", {})
    for value in libraryfolders.values():
        if isinstance(value, dict):
            path_value = value.get("path")
        else:
            path_value = value
        if not path_value:
            continue
        candidate = Path(str(path_value)).expanduser()
        libraries.add(candidate)

    return sorted(libraries, key=lambda path: str(path).lower())


def get_steam_common_directories(steam_path: Path) -> list[Path]:
    common_dirs: list[Path] = []
    for library in load_libraryfolders(steam_path):
        common_path = library / "steamapps" / "common"
        if common_path.exists():
            common_dirs.append(common_path)
    return common_dirs


def path_is_in_steam_library(exe_path: str | Path, common_dirs: list[Path]) -> bool:
    candidate = Path(str(exe_path))
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        candidate = candidate.absolute()

    for common_dir in common_dirs:
        try:
            resolved_common = common_dir.resolve(strict=False)
        except OSError:
            resolved_common = common_dir.absolute()
        try:
            candidate.relative_to(resolved_common)
            return True
        except ValueError:
            continue
    return False
