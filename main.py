from __future__ import annotations

import logging
import sys
import shutil
import tempfile
import importlib.util
from pathlib import Path

from artwork_manager import SteamGridDBClient, cleanup_downloaded_artwork, download_artwork_for_shortcuts, resolve_api_key
from config import LOG_FILE_NAME, MIN_PYTHON
from fixer import diagnose_shortcuts, fix_shortcuts_interactively
from game_scanner import DiscoveredGame, discover_games
from logger_setup import setup_logging
from shortcut_builder import build_shortcut, get_shortcut_exe_value, normalize_lookup_text, normalized_exe_identity
from steam_paths import (
    SteamUser,
    find_steam_install_path,
    get_steam_common_directories,
    is_steam_running,
    list_steam_users,
)
from vdf_manager import (
    add_one_and_verify_test,
    backup_shortcuts,
    collect_existing_exe_paths,
    load_shortcuts,
    reindex_shortcuts,
    round_trip_integrity_test,
    verify_field_quoting,
    verify_persisted_shortcuts,
    write_shortcuts,
)


def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        print(f"Python {required}+ is required.")
        raise SystemExit(1)


def check_dependencies() -> None:
    missing = [name for name in ("vdf", "requests") if importlib.util.find_spec(name) is None]

    if missing:
        print(f"Missing required packages: {', '.join(missing)}")
        print(f"Install them with: pip install {' '.join(missing)}")
        raise SystemExit(1)


def validate_writable_directory(path: Path, description: str, logger, create: bool = False) -> bool:
    target = path
    try:
        if create:
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            logger.error("Required %s does not exist: %s", description, target)
            print(f"Required {description} does not exist: {target}")
            return False

        with tempfile.NamedTemporaryFile(dir=target, prefix="steam-game-manager-", suffix=".tmp", delete=True):
            pass
    except OSError as error:
        logger.error("%s is not writable: %s", description, error)
        print(f"Cannot write to {description}: {target} ({error})")
        return False
    return True


def load_shortcuts_safe(shortcuts_path: Path, logger, context: str) -> dict:
    try:
        return load_shortcuts(shortcuts_path)
    except Exception as error:
        logger.error("Failed to load shortcuts for %s: %s", context, error)
        raise RuntimeError(f"Could not load shortcuts for {context}: {error}") from error


def load_existing_sets(users: list[SteamUser], logger) -> list[set[str]]:
    existing_sets: list[set[str]] = []
    for user in users:
        existing_sets.append(
            collect_existing_exe_paths(load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}"))
        )
    return existing_sets


def count_shortcuts_for_user(user: SteamUser, logger) -> int:
    data = load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}")
    return len(data.get("shortcuts", {}))


def collect_existing_app_names(shortcuts_data: dict) -> set[str]:
    names: set[str] = set()
    for shortcut in shortcuts_data.get("shortcuts", {}).values():
        app_name = str(shortcut.get("AppName", "")).strip()
        if not app_name:
            continue
        names.add(normalize_lookup_text(app_name))
    return names


def load_existing_app_name_sets(users: list[SteamUser], logger) -> list[set[str]]:
    app_name_sets: list[set[str]] = []
    for user in users:
        app_name_sets.append(collect_existing_app_names(load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}")))
    return app_name_sets


def validate_user_artwork_target(user: SteamUser, logger) -> bool:
    return validate_writable_directory(user.grid_dir, f"Steam artwork directory for user {user.user_id}", logger, create=True)


def download_and_persist_artwork(user: SteamUser, shortcuts_map: dict[str, dict], api_key: str, logger) -> int:
    if not validate_user_artwork_target(user, logger):
        return 0

    result = download_artwork_for_shortcuts(shortcuts_map, user.grid_dir, api_key, logger)
    if write_user_shortcuts(user, shortcuts_map, logger):
        print(
            f"Artwork for user {user.user_id}: {result.downloaded} downloaded, "
            f"{result.skipped} skipped, {result.failures} failed."
        )
        return result.downloaded

    cleanup_downloaded_artwork(result.downloaded_files, logger)
    print(f"Artwork changes for user {user.user_id} were reverted because shortcuts.vdf could not be updated.")
    return 0


def format_check(name: str, passed: bool, detail: str) -> None:
    state = "OK" if passed else "FAIL"
    print(f"{name:<22} {state:<4} {detail}")


def mask_value(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def discover_games_quietly(
    existing_exe_paths: set[str],
    steam_common_dirs: list[Path],
    logger,
    existing_app_names: set[str] | None = None,
) -> list[DiscoveredGame]:
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        return discover_games(existing_exe_paths, steam_common_dirs, logger, existing_app_names=existing_app_names)
    finally:
        logger.setLevel(previous_level)


def run_diagnostics(logger) -> int:
    print("Steam Game Manager Diagnostics (Linux)")
    print("=" * 38)
    print(f"Platform:    {sys.platform}")
    print(f"Python:      {sys.version.split()[0]}")

    steam_path = find_steam_install_path()
    if steam_path is None:
        print("Steam path:  not found")
        logger.error("Diagnostics failed: Steam installation path not found")
        return 1

    print(f"Steam path:  {steam_path}")

    steam_running = is_steam_running()
    print(f"Steam open:  {'yes' if steam_running else 'no'}")

    users = list_steam_users(steam_path)
    print(f"Steam users: {len(users)}")
    for user in users:
        print(f"  - {user.user_id}")

    if not users:
        logger.error("Diagnostics failed: no Steam users found")
        return 1

    for user in users:
        try:
            shortcut_count = len(load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}").get("shortcuts", {}))
        except RuntimeError:
            return 1
        print(f"Shortcuts:   {user.user_id} -> {shortcut_count}")

    if steam_running:
        logger.error("Diagnostics failed: Steam is running")
        print("Status:      not ready - close Steam before making changes")
        return 1

    print("Status:      ready for interactive use")
    return 0


def run_dry_run_validation(logger) -> int:
    print("Steam Game Manager Dry Run (Linux)")
    print("=" * 34)

    steam_path = find_steam_install_path()
    if steam_path is None:
        logger.error("Dry run failed: Steam installation path not found")
        format_check("Steam path", False, "Steam installation not found")
        return 1
    format_check("Steam path", True, str(steam_path))

    steam_running = is_steam_running()
    format_check("Steam process", not steam_running, "Steam is running" if steam_running else "Steam is closed")

    users = list_steam_users(steam_path)
    format_check("Steam users", bool(users), f"{len(users)} detected")
    if not users:
        logger.error("Dry run failed: no Steam users found")
        return 1

    shortcuts_ok = True
    for user in users:
        try:
            data = load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}")
        except RuntimeError:
            return 1

        format_check(f"Shortcuts {user.user_id}", True, f"{len(data.get('shortcuts', {}))} entries")
        config_ok = validate_writable_directory(user.config_dir, f"Steam config directory for user {user.user_id}", logger, create=False)
        shortcuts_ok = shortcuts_ok and config_ok
        format_check(f"Config dir {user.user_id}", config_ok, str(user.config_dir))

        grid_dir_state = user.grid_dir.exists()
        grid_message = str(user.grid_dir) if grid_dir_state else f"missing (will be created when artwork is downloaded): {user.grid_dir}"
        format_check(f"Artwork dir {user.user_id}", True, grid_message)

    api_key = resolve_api_key(prompt_if_missing=False)
    if api_key:
        format_check("Artwork API key", True, f"configured as {mask_value(api_key)}")
    else:
        format_check("Artwork API key", False, "not configured; artwork downloads will be skipped")

    steam_common_dirs = get_steam_common_directories(steam_path)
    existing_sets = load_existing_sets(users, logger)
    existing_app_name_sets = load_existing_app_name_sets(users, logger)
    candidates = discover_games_quietly(
        intersection_of_sets(existing_sets),
        steam_common_dirs,
        logger,
        existing_app_names=intersection_of_sets(existing_app_name_sets),
    )
    format_check("Game scan", True, f"{len(candidates)} candidate(s) found")
    for candidate in candidates[:5]:
        print(f"  - {candidate.app_name}: {candidate.exe_path}")

    blocking_issue = steam_running or not shortcuts_ok
    if blocking_issue:
        print("Status:             not ready - fix the failed checks before making changes")
        return 1

    print("Status:             ready for an interactive run; this validation made no changes")
    return 0


def validate_artwork_api_key(api_key: str, logger) -> tuple[bool, str]:
    try:
        client = SteamGridDBClient(api_key, logger)
        client.validate_api_key()
    except Exception as error:
        logger.error("Artwork API validation failed: %s", error)
        return False, str(error)
    return True, "SteamGridDB API key validated successfully"


def run_flow_validation(logger) -> int:
    print("Steam Game Manager Flow Validation (Linux)")
    print("=" * 42)

    steam_path = find_steam_install_path()
    if steam_path is None:
        format_check("Startup", False, "Steam installation not found")
        return 1

    users = list_steam_users(steam_path)
    if not users:
        format_check("Startup", False, "No Steam users detected")
        return 1

    steam_running = is_steam_running()
    startup_ready = not steam_running
    format_check("Startup", startup_ready, "Steam is closed" if startup_ready else "Steam is running")

    total_issue_count = 0
    fix_ready = True
    for user in users:
        data = load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}")
        issues = diagnose_shortcuts(data.get("shortcuts", {}))
        issue_count = sum(len(entries) for entries in issues.values())
        total_issue_count += issue_count
        config_ok = validate_writable_directory(user.config_dir, f"Steam config directory for user {user.user_id}", logger, create=False)
        fix_ready = fix_ready and config_ok
        format_check(f"Fix flow {user.user_id}", config_ok, f"{issue_count} issue(s) available for review")

    steam_common_dirs = get_steam_common_directories(steam_path)
    existing_sets = load_existing_sets(users, logger)
    existing_app_name_sets = load_existing_app_name_sets(users, logger)
    add_candidates = discover_games_quietly(
        intersection_of_sets(existing_sets),
        steam_common_dirs,
        logger,
        existing_app_names=intersection_of_sets(existing_app_name_sets),
    )
    format_check("Add flow", True, f"{len(add_candidates)} candidate(s) available to review")
    for candidate in add_candidates[:5]:
        print(f"  - add candidate: {candidate.app_name} -> {candidate.exe_path}")

    api_key = resolve_api_key(prompt_if_missing=False)
    artwork_ready = False
    if api_key:
        artwork_ready, artwork_detail = validate_artwork_api_key(api_key, logger)
        format_check("Artwork flow", artwork_ready, artwork_detail)
    else:
        format_check("Artwork flow", False, "No SteamGridDB API key configured")

    full_run_ready = startup_ready and fix_ready and artwork_ready
    full_run_detail = (
        f"fix issues={total_issue_count}, add candidates={len(add_candidates)}, artwork={'ok' if artwork_ready else 'blocked'}"
    )
    format_check("Full run", full_run_ready, full_run_detail)

    if not full_run_ready:
        print("Status:             not ready for a full interactive run")
        return 1

    print("Status:             ready for fix/add/artwork/full-run workflows")
    return 0


def prompt_for_users(users: list[SteamUser]) -> list[SteamUser]:
    if not users:
        print("No Steam userdata directories were found. Open Steam once and sign in first.")
        raise SystemExit(1)
    if len(users) == 1:
        return users

    print("Steam user IDs found:")
    for index, user in enumerate(users, start=1):
        print(f"  [{index}] {user.user_id} (last modified {user.last_modified:%Y-%m-%d %H:%M:%S})")
    print("  [a] All users")

    while True:
        choice = input("Choose a user or 'a' for all [1]: ").strip().lower() or "1"
        if choice == "a":
            return users
        if choice.isdigit() and 1 <= int(choice) <= len(users):
            return [users[int(choice) - 1]]
        print("Please enter a valid number or 'a'.")


def format_user_label(selected_users: list[SteamUser]) -> str:
    if len(selected_users) == 1:
        return selected_users[0].user_id
    return f"all ({', '.join(user.user_id for user in selected_users)})"


def intersection_of_sets(values: list[set[str]]) -> set[str]:
    if not values:
        return set()
    result = set(values[0])
    for value in values[1:]:
        result &= value
    return result


def print_menu(steam_path: Path, selected_users: list[SteamUser], logger) -> None:
    counts = [count_shortcuts_for_user(user, logger) for user in selected_users]
    shortcut_display = str(counts[0]) if len(selected_users) == 1 else f"{sum(counts)} total across {len(selected_users)} users"

    print("\nSteam Non-Steam Game Manager (Linux / CachyOS)")
    print("=" * 46)
    print(f"Steam path:  {steam_path}")
    print(f"User ID:     {format_user_label(selected_users)}")
    print(f"Shortcuts:   {shortcut_display}")
    print()
    print("[1] Scan for broken entries and fix them")
    print("[2] Scan ~/Games and known dirs for new games to add")
    print("[3] Download artwork for all shortcuts (SteamGridDB)")
    print("[4] Full run (fix -> add -> artwork)")
    print("[5] List all current non-Steam shortcuts")
    print("[0] Exit")


def ensure_ready_to_write(shortcuts_path: Path, logger) -> bool:
    if is_steam_running():
        print("Steam is running. Close Steam completely before writing shortcuts.vdf.")
        logger.error("Refused to write because Steam is running")
        return False

    if not validate_writable_directory(shortcuts_path.parent, "Steam config directory", logger, create=True):
        return False

    try:
        round_trip_ok, round_trip_message = round_trip_integrity_test(shortcuts_path)
    except Exception as error:
        logger.error("Round-trip validation failed for %s: %s", shortcuts_path, error)
        print(f"Could not validate {shortcuts_path.name}: {error}")
        return False
    logger.info(round_trip_message)
    if not round_trip_ok:
        print(round_trip_message)
        return False

    try:
        add_one_ok, add_one_message = add_one_and_verify_test(shortcuts_path)
    except Exception as error:
        logger.error("Add-one validation failed for %s: %s", shortcuts_path, error)
        print(f"Could not run write validation for {shortcuts_path.name}: {error}")
        return False
    logger.info(add_one_message)
    if not add_one_ok:
        print(add_one_message)
        return False

    for warning in verify_field_quoting(load_shortcuts_safe(shortcuts_path, logger, str(shortcuts_path))):
        logger.warning(warning)

    return True


def write_user_shortcuts(user: SteamUser, shortcuts_map: dict[str, dict], logger) -> bool:
    data = {"shortcuts": reindex_shortcuts(shortcuts_map)}
    if not ensure_ready_to_write(user.shortcuts_path, logger):
        return False

    backup_path: Path | None = None
    try:
        backup_path = backup_shortcuts(user.shortcuts_path)
    except OSError as error:
        logger.error("Failed to back up %s: %s", user.shortcuts_path, error)
        print(f"Backup failed for {user.user_id}: {error}")
        return False

    logger.info("Created backup at %s", backup_path)
    try:
        write_shortcuts(user.shortcuts_path, data)
        verified, message = verify_persisted_shortcuts(user.shortcuts_path, data)
    except Exception as error:
        logger.error("Failed to write shortcuts for %s: %s", user.user_id, error)
        if backup_path is not None and backup_path.exists():
            shutil.copy2(backup_path, user.shortcuts_path)
            logger.error("Restored %s from backup after write failure", user.shortcuts_path)
        print(f"Failed to update shortcuts for user {user.user_id}: {error}")
        return False
    logger.info(message)
    if not verified:
        if backup_path is not None and backup_path.exists():
            shutil.copy2(backup_path, user.shortcuts_path)
            logger.error("Restored %s from backup after verification failure", user.shortcuts_path)
        print(message)
        return False
    return True


def list_shortcuts(selected_users: list[SteamUser], logger) -> None:
    for user in selected_users:
        print(f"\nUser {user.user_id}")
        shortcuts = load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}").get("shortcuts", {})
        if not shortcuts:
            print("  (no non-Steam shortcuts)")
            continue
        for index, shortcut in sorted(shortcuts.items(), key=lambda item: int(str(item[0]))):
            print(f"  [{index}] {shortcut.get('AppName', 'Unknown')} -> {get_shortcut_exe_value(shortcut)}")


def select_games_to_add(candidates: list[DiscoveredGame]) -> list[DiscoveredGame]:
    if not candidates:
        print("No new non-Steam game candidates were found.")
        return []

    selected = [not candidate.ambiguous for candidate in candidates]
    while True:
        print(f"\nFound {len(candidates)} games to add:\n")
        for index, candidate in enumerate(candidates, start=1):
            mark = "x" if selected[index - 1] else " "
            suffix = " [needs review]" if candidate.ambiguous else ""
            print(f" [{mark}] {index:2d}. {candidate.app_name:<30} -> {candidate.exe_path}{suffix}")
        print("\nCommands: [a] Select all  [n] Select none  [1-#] Toggle  [c] Confirm  [q] Quit")
        response = input("Choose: ").strip().lower()
        if response == "a":
            selected = [True] * len(candidates)
            continue
        if response == "n":
            selected = [False] * len(candidates)
            continue
        if response == "c":
            break
        if response == "q":
            return []

        tokens = [token for token in response.replace(",", " ").split() if token]
        toggled = False
        for token in tokens:
            if token.isdigit() and 1 <= int(token) <= len(candidates):
                idx = int(token) - 1
                selected[idx] = not selected[idx]
                toggled = True
        if not toggled:
            print("Please enter one of the listed commands.")

    confirmed: list[DiscoveredGame] = []
    for is_selected, candidate in zip(selected, candidates):
        if not is_selected:
            continue
        if candidate.ambiguous:
            print(f"\n{candidate.app_name} has multiple possible executables:")
            for option_index, option in enumerate(candidate.candidates, start=1):
                print(f"  [{option_index}] {option}")
            while True:
                response = input(
                    f"Choose [1-{len(candidate.candidates)}], press Enter/y for the detected executable, or n to skip: "
                ).strip().lower()
                if response in {"", "y", "yes"}:
                    confirmed.append(candidate)
                    break
                if response in {"n", "no"}:
                    break
                if response.isdigit() and 1 <= int(response) <= len(candidate.candidates):
                    chosen_path = candidate.candidates[int(response) - 1]
                    confirmed.append(
                        DiscoveredGame(
                            app_name=candidate.app_name,
                            exe_path=chosen_path,
                            source_dir=candidate.source_dir,
                            score=candidate.score,
                            ambiguous=False,
                            candidates=candidate.candidates,
                        )
                    )
                    break
                print("Please enter a number, y, or n.")
            continue
        confirmed.append(candidate)
    return confirmed


def fix_existing_shortcuts(selected_users: list[SteamUser], logger) -> tuple[int, int]:
    total_fixed = 0
    total_removed = 0
    for user in selected_users:
        data = load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}")
        result = fix_shortcuts_interactively(data.get("shortcuts", {}), logger)
        if result.changed:
            if write_user_shortcuts(user, result.shortcuts, logger):
                total_fixed += result.fixed_count
                total_removed += result.removed_count
                print(f"Updated shortcuts for user {user.user_id}: {result.fixed_count} fixed, {result.removed_count} removed.")
        else:
            print(f"No changes needed for user {user.user_id}.")
    return total_fixed, total_removed


def scan_and_add_games(steam_path: Path, selected_users: list[SteamUser], logger) -> int:
    existing_sets = load_existing_sets(selected_users, logger)
    existing_app_name_sets = load_existing_app_name_sets(selected_users, logger)
    common_existing = intersection_of_sets(existing_sets)
    steam_common_dirs = get_steam_common_directories(steam_path)
    candidates = discover_games(
        common_existing,
        steam_common_dirs,
        logger,
        existing_app_names=intersection_of_sets(existing_app_name_sets),
    )
    chosen_games = select_games_to_add(candidates)
    if not chosen_games:
        print("No games selected.")
        return 0

    total_added = 0
    for user in selected_users:
        data = load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}")
        shortcuts_map = data.get("shortcuts", {})
        existing_for_user = collect_existing_exe_paths(data)
        combined = list(shortcuts_map.values())
        added_here = 0

        for game in chosen_games:
            identity = normalized_exe_identity(str(game.exe_path))
            if identity in existing_for_user:
                continue
            combined.append(build_shortcut(game.app_name, str(game.exe_path)))
            existing_for_user.add(identity)
            added_here += 1
            logger.info("Queued new shortcut '%s' for user %s", game.app_name, user.user_id)

        if added_here and write_user_shortcuts(user, reindex_shortcuts(combined), logger):
            total_added += added_here
            print(f"Added {added_here} game(s) for user {user.user_id}.")
        elif added_here == 0:
            print(f"No new games needed for user {user.user_id}.")

    return total_added


def download_all_artwork(selected_users: list[SteamUser], logger) -> int:
    api_key = resolve_api_key(prompt_if_missing=True)
    if not api_key:
        print("A SteamGridDB API key is required to download artwork.")
        return 0

    total_downloaded = 0
    for user in selected_users:
        data = load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}")
        shortcuts_map = data.get("shortcuts", {})
        if not shortcuts_map:
            print(f"No shortcuts found for user {user.user_id}.")
            continue

        try:
            total_downloaded += download_and_persist_artwork(user, shortcuts_map, api_key, logger)
        except RuntimeError as error:
            print(str(error))
            logger.error("Artwork download aborted: %s", error)
            return total_downloaded
    return total_downloaded


def full_run(steam_path: Path, selected_users: list[SteamUser], logger) -> None:
    total_fixed = 0
    total_removed = 0
    total_added = 0
    per_user_shortcuts: dict[str, dict[str, dict]] = {}

    for user in selected_users:
        data = load_shortcuts_safe(user.shortcuts_path, logger, f"user {user.user_id}")
        result = fix_shortcuts_interactively(data.get("shortcuts", {}), logger)
        total_fixed += result.fixed_count
        total_removed += result.removed_count
        per_user_shortcuts[user.user_id] = result.shortcuts

    existing_sets = [
        {normalized_exe_identity(shortcut) for shortcut in shortcuts.values() if normalized_exe_identity(shortcut)}
        for shortcuts in per_user_shortcuts.values()
    ]
    existing_app_name_sets = [collect_existing_app_names({"shortcuts": shortcuts}) for shortcuts in per_user_shortcuts.values()]
    steam_common_dirs = get_steam_common_directories(steam_path)
    candidates = discover_games(
        intersection_of_sets(existing_sets),
        steam_common_dirs,
        logger,
        existing_app_names=intersection_of_sets(existing_app_name_sets),
    )
    chosen_games = select_games_to_add(candidates)

    for user in selected_users:
        shortcuts_map = per_user_shortcuts[user.user_id]
        existing = {normalized_exe_identity(shortcut) for shortcut in shortcuts_map.values() if normalized_exe_identity(shortcut)}
        combined = list(shortcuts_map.values())
        added_here = 0
        for game in chosen_games:
            identity = normalized_exe_identity(str(game.exe_path))
            if identity in existing:
                continue
            combined.append(build_shortcut(game.app_name, str(game.exe_path)))
            existing.add(identity)
            added_here += 1
            logger.info("Queued '%s' for user %s during full run", game.app_name, user.user_id)
        per_user_shortcuts[user.user_id] = reindex_shortcuts(combined)
        total_added += added_here

    if not chosen_games:
        print("No new games selected during full run.")

    api_key = resolve_api_key(prompt_if_missing=True)
    total_art = 0
    for user in selected_users:
        shortcuts_map = per_user_shortcuts[user.user_id]
        if api_key:
            try:
                total_art += download_and_persist_artwork(user, shortcuts_map, api_key, logger)
                continue
            except RuntimeError as error:
                print(str(error))
                logger.error("Artwork download aborted during full run for user %s: %s", user.user_id, error)

        if write_user_shortcuts(user, shortcuts_map, logger):
            continue

    if not api_key:
        print("Skipping artwork because no SteamGridDB API key was provided.")

    print(
        f"Full run complete: {total_fixed} fixed, {total_removed} removed, "
        f"{total_added} additions requested, {total_art} artwork files downloaded."
    )
    print("Start Steam to see your changes.")


def main() -> int:
    check_python_version()
    check_dependencies()
    logger = setup_logging(LOG_FILE_NAME)

    if "--diagnose" in sys.argv[1:]:
        return run_diagnostics(logger)
    if "--dry-run-check" in sys.argv[1:]:
        return run_dry_run_validation(logger)
    if "--validate-flows" in sys.argv[1:]:
        return run_flow_validation(logger)

    steam_path = find_steam_install_path()
    if steam_path is None:
        print("Could not find your Steam installation path.")
        print("Set the STEAM_PATH environment variable if Steam is installed to a non-standard location.")
        logger.error("Steam installation path not found")
        return 1

    if is_steam_running():
        print("Steam is running. Close Steam completely before using this script.")
        logger.error("Refused to start because Steam is running")
        return 1

    selected_users = prompt_for_users(list_steam_users(steam_path))
    logger.info("Using Steam path %s", steam_path)
    logger.info("Selected Steam users: %s", ", ".join(user.user_id for user in selected_users))

    while True:
        try:
            print_menu(steam_path, selected_users, logger)
            choice = input("Choose an option: ").strip()

            if choice == "0":
                print(f"Log written to {Path(LOG_FILE_NAME).resolve()}")
                return 0
            if choice == "1":
                fix_existing_shortcuts(selected_users, logger)
                continue
            if choice == "2":
                added = scan_and_add_games(steam_path, selected_users, logger)
                print(f"Added {added} new shortcut(s) in total.")
                continue
            if choice == "3":
                downloaded = download_all_artwork(selected_users, logger)
                print(f"Downloaded {downloaded} artwork file(s).")
                print("Start Steam to see your changes.")
                continue
            if choice == "4":
                full_run(steam_path, selected_users, logger)
                continue
            if choice == "5":
                list_shortcuts(selected_users, logger)
                continue

            print("Please choose one of the listed options.")
        except RuntimeError as error:
            print(str(error))
            logger.error("Operation failed: %s", error)
        except Exception as error:
            print(f"Unexpected error: {error}")
            logger.exception("Unexpected error during menu operation")


if __name__ == "__main__":
    raise SystemExit(main())
