from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from config import (
    ENGINE_DATA_FILE_STEMS,
    GENERIC_CONTAINER_DIRS,
    GAME_HINT_FILES_LOWER,
    GAME_HINT_PREFIXES,
    KNOWN_GAME_DIRS,
    MAX_EXECUTABLE_SCAN_DEPTH,
    MAX_SCAN_DEPTH,
    OVERLAY_DIR_NAMES,
    SKIP_CANDIDATE_APP_NAMES,
    SKIP_DIRS,
    SKIP_EXE_PATTERNS,
    SKIP_EXE_STEMS,
    SKIP_PATH_KEYWORDS,
    SYSTEM_SKIP_DIRS,
    WINE_SYSTEM_EXE_STEMS,
)
from shortcut_builder import clean_game_name, normalize_lookup_text, normalized_exe_identity, prettify_exe_stem, similarity_score
from steam_paths import path_is_in_steam_library

MAX_EXTENDED_SCAN_DEPTH = 8

@dataclass(slots=True)
class DiscoveredGame:
    app_name: str
    exe_path: Path
    source_dir: Path
    score: float
    ambiguous: bool = False
    candidates: list[Path] = field(default_factory=list)



def _path_is_in_wine_prefix(path: Path) -> bool:
    """Return True if path is inside a Wine/Proton prefix system directory."""
    lowered = str(path).lower()
    # Wine prefixes have drive_c/ structure (Linux-only tool, forward slashes)
    if "/drive_c/" not in lowered:
        return False
    # Check for Windows system directories inside the Wine prefix
    wine_system_patterns = (
        "/windows/system32/",
        "/windows/syswow64/",
        "/windows/system/",
        "/windows/command/",
        "/windows/winhelp.exe",
        "/program files (x86)/internet explorer/",
        "/program files (x86)/windows media player/",
        "/program files/internet explorer/",
        "/program files/windows media player/",
        "/windows/winsxs/",
        "/windows/microsoft.net/",
    )
    for pattern in wine_system_patterns:
        if pattern in lowered:
            return True
    return False


def _path_is_in_overlay_dir(path: Path) -> bool:
    """Return True if path is inside an overlay/trainer/injector directory."""
    for part in path.parts:
        if part.lower() in OVERLAY_DIR_NAMES:
            return True
    return False


def _path_is_in_steam_downloading(path: Path) -> bool:
    """Return True if path is inside a Steam library downloading directory."""
    return "/steamapps/downloading/" in str(path).lower()


def _path_is_engine_data(path: Path) -> bool:
    """Return True if path looks like game engine data, not a game executable.

    Engine data files are typically .bin files inside Engine/Content/ paths,
    or extensionless files with known engine data names.
    """
    lowered = str(path).lower()
    # .bin files inside Engine/Content/ or Engine/Binaries/ are shader caches / engine data
    if path.suffix.lower() == ".bin":
        for engine_pattern in ("/engine/content/", "/engine/binaries/", "/engine/plugins/"):
            if engine_pattern in lowered:
                return True
    # Check known engine data file stems (extensionless false positives)
    if path.stem.lower() in ENGINE_DATA_FILE_STEMS:
        return True
    # Unity level data files: "level" + digits (e.g. level0, level46, level220)
    stem = path.stem.lower()
    if stem.startswith("level") and stem[5:].isdigit() and len(stem) > 5:
        return True
    return False


def _is_game_executable(path: Path) -> bool:
    """High-level check: is this file a game executable worth scanning?

    Combines all skip checks: system exes, Wine prefix, overlay dirs,
    Steam downloading, engine data files, and the basic executable check.
    """
    if not _is_executable_file(path):
        return False
    # Skip Windows system tools
    if path.stem.lower() in WINE_SYSTEM_EXE_STEMS:
        return False
    # Skip Wine prefix system executables
    if _path_is_in_wine_prefix(path):
        return False
    # Skip overlay/injector executables
    if _path_is_in_overlay_dir(path):
        return False
    # Skip Steam downloading executables
    if _path_is_in_steam_downloading(path):
        return False
    # Skip engine data files
    if _path_is_engine_data(path):
        return False
    # Skip paths with known non-game keywords
    if _path_contains_skip_keyword(path):
        return False
    return True


def _is_executable_file(path: Path) -> bool:
    """Return True if the file is an executable (native binary, script, or Windows .exe).

    Windows .exe files copied onto a Linux filesystem rarely have execute bits set
    because chmod is never applied during extraction.  They are accepted
    unconditionally (after name-based filtering) without requiring execute bits.
    All native Linux formats (.sh, bare binaries, .elf/.bin/.run) still require at
    least one executable bit as a meaningful sanity check.
    """
    try:
        file_stat = path.stat()
    except OSError:
        return False

    # Skip directories
    if stat.S_ISDIR(file_stat.st_mode):
        return False

    # Skip dangling symlinks
    if path.is_symlink() and not path.exists():
        return False

    name_lower = path.name.lower()
    stem_lower = path.stem.lower()

    # Skip common non-game executables by name (applies to all types)
    if stem_lower in SKIP_EXE_STEMS:
        return False

    # Skip by glob patterns (applies to all types)
    if any(fnmatch.fnmatch(name_lower, pattern) for pattern in SKIP_EXE_PATTERNS):
        return False

    # Skip system paths
    path_lower = str(path).lower()
    if path_lower.startswith("/usr/") or path_lower.startswith("/bin/") or path_lower.startswith("/sbin/"):
        return False

    # Windows PE binaries: no execute-bit requirement — run via Proton/Wine.
    # This MUST come before the execute-bit check below.
    if name_lower.endswith(".exe"):
        return True

    # For all native Linux formats require at least one executable bit
    if not (file_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
        return False

    # Accept .sh scripts (many Linux game launchers are shell scripts)
    if name_lower.endswith(".sh"):
        return True

    # Accept files with no extension (likely native binaries)
    if "." not in path.name or name_lower.endswith(".x86_64") or name_lower.endswith(".x86"):
        return True

    # Accept common Linux game binary extensions
    if any(name_lower.endswith(ext) for ext in (".elf", ".bin", ".run")):
        return True

    return False


def _is_skip_dir(directory_name: str) -> bool:
    lowered = directory_name.lower()
    # .startswith(".") catches any hidden directory (.config, .cache, etc.)
    return lowered in SKIP_DIRS or lowered in SYSTEM_SKIP_DIRS or lowered.startswith(".")


def _path_contains_skip_keyword(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    return any(keyword in part for part in lowered_parts for keyword in SKIP_PATH_KEYWORDS)


def _has_game_hints(files: list[str]) -> bool:
    lowered = {file_name.lower() for file_name in files}
    if lowered.intersection(GAME_HINT_FILES_LOWER):
        return True
    return any(file_name.lower().startswith(GAME_HINT_PREFIXES) for file_name in files)


def _depth(root: Path, current: Path) -> int:
    try:
        return len(current.relative_to(root).parts)
    except ValueError:
        return MAX_SCAN_DEPTH + 1


def _iter_candidate_directories(scan_root: Path, max_depth: int) -> list[Path]:
    candidates: list[Path] = []
    try:
        walk_iter = os.walk(scan_root, followlinks=False)
    except OSError:
        return candidates

    for current_root, dir_names, file_names in walk_iter:
        current_path = Path(current_root)
        current_depth = _depth(scan_root, current_path)

        dir_names[:] = [name for name in dir_names if not _is_skip_dir(name)]
        if current_depth >= max_depth:
            dir_names[:] = [name for name in dir_names if name.lower() in GENERIC_CONTAINER_DIRS]
        if current_depth >= MAX_EXTENDED_SCAN_DEPTH:
            dir_names[:] = []

        exe_files = [name for name in file_names if _is_game_executable(current_path / name)]
        if exe_files or _has_game_hints(file_names):
            candidates.append(current_path)
    return candidates


def _candidate_score(game_dir: Path, exe_path: Path) -> float:
    score = 0.0
    dir_name = clean_game_name(game_dir.name)
    exe_name = clean_game_name(exe_path.stem)
    score += similarity_score(dir_name, exe_name) * 100

    try:
        relative_parts = exe_path.relative_to(game_dir).parts
    except ValueError:
        relative_parts = ()

    if len(relative_parts) == 1:
        score += 25
    elif len(relative_parts) == 2:
        score += 10

    if any(part.lower() in SKIP_DIRS for part in relative_parts[:-1]):
        score -= 25

    if _path_contains_skip_keyword(exe_path):
        score -= 100

    if exe_path.stem.lower() in SKIP_EXE_STEMS:
        score -= 120

    # Prefer native binaries over shell scripts
    name_lower = exe_path.name.lower()
    if name_lower.endswith(".sh"):
        score -= 5  # slight penalty — often launchers
    elif name_lower.endswith(".exe"):
        score -= 10  # Wine/Proton EXE — still valid but prefer native

    try:
        size_mb = exe_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    score += min(size_mb, 50.0)
    return score


def _derive_display_name(game_dir: Path, exe_path: Path) -> str:
    exe_name = prettify_exe_stem(exe_path.stem)
    if exe_name.strip().lower() not in SKIP_CANDIDATE_APP_NAMES and len(exe_name.strip()) >= 6:
        if similarity_score(clean_game_name(game_dir.name), exe_name) >= 0.65:
            return exe_name

    for parent in [exe_path.parent, *exe_path.parents[1:]]:
        if parent == game_dir.parent:
            break
        name = parent.name.strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in GENERIC_CONTAINER_DIRS or lowered in SKIP_DIRS:
            continue
        if any(keyword in lowered for keyword in SKIP_PATH_KEYWORDS):
            continue
        return clean_game_name(name)
    return clean_game_name(game_dir.name or exe_path.stem)


def _is_valid_candidate(game_dir: Path, exe_path: Path, app_name: str, score: float) -> bool:
    if score <= 0:
        return False
    if app_name.strip().lower() in SKIP_CANDIDATE_APP_NAMES:
        return False
    if len(app_name.strip()) <= 3:
        return False
    if exe_path.stem.lower() in SKIP_EXE_STEMS:
        return False
    if _path_contains_skip_keyword(exe_path):
        return False
    return True


def _select_best_executable(game_dir: Path, steam_common_dirs: list[Path]) -> tuple[Path | None, bool, list[Path], float]:
    candidates: list[tuple[float, Path]] = []

    try:
        walk_iter = os.walk(game_dir, followlinks=False)
    except OSError:
        return None, False, [], 0.0

    for current_root, dir_names, file_names in walk_iter:
        current_path = Path(current_root)
        if _depth(game_dir, current_path) > MAX_EXECUTABLE_SCAN_DEPTH:
            dir_names[:] = []
            continue

        dir_names[:] = [name for name in dir_names if not _is_skip_dir(name)]
        for file_name in file_names:
            candidate_path = current_path / file_name
            if not _is_game_executable(candidate_path):
                continue
            if path_is_in_steam_library(candidate_path, steam_common_dirs):
                continue
            candidates.append((_candidate_score(game_dir, candidate_path), candidate_path))

    if not candidates:
        return None, False, [], 0.0

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_path = candidates[0]
    top_paths = [path for _, path in candidates[:3]]

    ambiguous = False
    if len(candidates) > 1:
        second_score = candidates[1][0]
        ambiguous = (best_score - second_score) < 15 or best_score < 55

    return best_path, ambiguous, top_paths, best_score


def discover_games(
    existing_exe_paths: set[str],
    steam_common_dirs: list[Path],
    logger,
    existing_app_names: set[str] | None = None,
) -> list[DiscoveredGame]:
    candidate_dirs: list[Path] = []
    seen_dirs: set[str] = set()

    for known_dir in KNOWN_GAME_DIRS:
        expanded = known_dir.expanduser()
        if not expanded.exists():
            continue
        for candidate in _iter_candidate_directories(expanded, MAX_SCAN_DEPTH):
            identity = str(candidate)
            if identity not in seen_dirs:
                seen_dirs.add(identity)
                candidate_dirs.append(candidate)

    discovered: list[DiscoveredGame] = []
    seen_exes = set(existing_exe_paths)
    seen_app_names = set(existing_app_names or set())

    for game_dir in sorted(candidate_dirs, key=lambda path: str(path).lower()):
        best_exe, ambiguous, candidates, score = _select_best_executable(game_dir, steam_common_dirs)
        if best_exe is None:
            continue

        exe_identity = normalized_exe_identity(str(best_exe))
        if not exe_identity or exe_identity in seen_exes:
            continue

        app_name = _derive_display_name(game_dir, best_exe)
        normalized_app_name = normalize_lookup_text(app_name)
        if normalized_app_name in seen_app_names:
            continue
        if not _is_valid_candidate(game_dir, best_exe, app_name, score):
            continue
        discovered.append(
            DiscoveredGame(
                app_name=app_name,
                exe_path=best_exe,
                source_dir=game_dir,
                score=score,
                ambiguous=ambiguous,
                candidates=candidates,
            )
        )
        seen_exes.add(exe_identity)
        seen_app_names.add(normalized_app_name)
        logger.info("Discovered candidate game '%s' at %s", app_name, best_exe)

    return sorted(discovered, key=lambda item: item.app_name.lower())
