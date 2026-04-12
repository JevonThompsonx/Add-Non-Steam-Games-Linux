from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config import REQUIRED_SHORTCUT_DEFAULTS
from shortcut_builder import (
    derive_app_name_from_path,
    get_shortcut_exe_value,
    get_shortcut_openvr_value,
    has_glob_pattern,
    is_concrete_exe_path,
    is_probably_invalid_app_name,
    normalize_shortcut,
    normalized_exe_identity,
    quote_posix_path,
    shortcut_completeness_score,
    unquote_posix_path,
)


@dataclass(slots=True)
class FixResult:
    shortcuts: dict[str, dict]
    fixed_count: int = 0
    removed_count: int = 0
    skipped_count: int = 0
    issues_found: int = 0
    changed: bool = False
    touched_indices: set[str] = field(default_factory=set)


def _ordered_shortcuts(shortcuts_map: dict[str, dict]) -> list[tuple[str, dict]]:
    return sorted(shortcuts_map.items(), key=lambda item: int(str(item[0])))


def diagnose_shortcuts(shortcuts_map: dict[str, dict]) -> dict[str, list[str]]:
    issues: dict[str, list[str]] = {}

    for index, shortcut in _ordered_shortcuts(shortcuts_map):
        shortcut_issues: list[str] = []
        exe_value = get_shortcut_exe_value(shortcut)
        exe_path = unquote_posix_path(exe_value)
        start_dir_value = str(shortcut.get("StartDir", ""))
        app_name = str(shortcut.get("AppName", "")).strip()

        if not exe_value:
            shortcut_issues.append("Missing exe path")
        elif has_glob_pattern(exe_value):
            shortcut_issues.append("Exe path contains a wildcard pattern and cannot be safely fixed automatically")
        elif not (exe_value.startswith('"') and exe_value.endswith('"')):
            shortcut_issues.append("Exe path is not wrapped in double quotes")

        if is_concrete_exe_path(exe_value) and exe_path and not Path(exe_path).exists():
            shortcut_issues.append("Exe path does not exist on disk")

        if not start_dir_value:
            shortcut_issues.append("Missing StartDir")
        elif not (start_dir_value.startswith('"') and start_dir_value.endswith('"')):
            shortcut_issues.append("StartDir is not wrapped in double quotes")

        if is_concrete_exe_path(exe_value) and exe_path:
            expected_start_dir = quote_posix_path(str(Path(exe_path).parent), trailing_slash=True)
            if start_dir_value != expected_start_dir:
                shortcut_issues.append("StartDir does not match the exe parent directory")

        if is_probably_invalid_app_name(app_name):
            shortcut_issues.append("AppName is empty or malformed")

        missing_fields = [
            f for f in REQUIRED_SHORTCUT_DEFAULTS
            if f not in shortcut and not (f == "OpenVR" and "openvr" in shortcut)
        ]
        if missing_fields:
            shortcut_issues.append(f"Missing required fields: {', '.join(missing_fields)}")

        if get_shortcut_openvr_value(shortcut) not in {0, 1}:
            shortcut_issues.append("OpenVR has an invalid value")

        if shortcut.get("appid") is None:
            shortcut_issues.append("AppID is missing")

        if shortcut_issues:
            issues[index] = shortcut_issues

    duplicates: dict[str, list[str]] = {}
    for index, shortcut in _ordered_shortcuts(shortcuts_map):
        exe_value = get_shortcut_exe_value(shortcut)
        if not is_concrete_exe_path(exe_value):
            continue
        identity = normalized_exe_identity(shortcut)
        if not identity:
            continue
        duplicates.setdefault(identity, []).append(index)

    for indices in duplicates.values():
        if len(indices) < 2:
            continue
        for index in indices:
            issues.setdefault(index, []).append(f"Duplicate shortcut detected for exe path shared by {', '.join(indices)}")

    return issues


def _prompt_action(prompt: str, allowed: set[str], default: str) -> str:
    while True:
        response = input(prompt).strip().lower() or default
        if response in allowed:
            return response
        print(f"Please choose one of: {', '.join(sorted(allowed))}")


def _pick_duplicate_keep_index(indices: list[str], shortcuts_map: dict[str, dict]) -> str | None:
    ranked = sorted(indices, key=lambda item: shortcut_completeness_score(shortcuts_map[item]), reverse=True)
    recommended = ranked[0]

    print("\nDuplicate shortcuts detected:")
    for index in indices:
        shortcut = shortcuts_map[index]
        exe_value = get_shortcut_exe_value(shortcut)
        print(
            f"  [{index}] {shortcut.get('AppName', 'Unknown')} -> {exe_value} "
            f"(score {shortcut_completeness_score(shortcut)})"
        )

    response = input(
        f"Keep [{recommended}] and remove the others? "
        "[Enter=yes, s=skip, or type an index to keep]: "
    ).strip().lower()
    if response == "s":
        return None
    if response and response in indices:
        return response
    return recommended


def fix_shortcuts_interactively(shortcuts_map: dict[str, dict], logger) -> FixResult:
    working = {index: dict(shortcut) for index, shortcut in shortcuts_map.items()}
    issues = diagnose_shortcuts(working)
    result = FixResult(shortcuts=working, issues_found=sum(len(value) for value in issues.values()))

    duplicate_groups: dict[str, list[str]] = {}
    for index, shortcut in _ordered_shortcuts(working):
        exe_value = get_shortcut_exe_value(shortcut)
        if not is_concrete_exe_path(exe_value):
            continue
        identity = normalized_exe_identity(shortcut)
        if identity:
            duplicate_groups.setdefault(identity, []).append(index)

    processed_duplicates: set[str] = set()
    for identity, indices in duplicate_groups.items():
        if len(indices) < 2 or identity in processed_duplicates:
            continue
        keep_index = _pick_duplicate_keep_index(indices, working)
        if keep_index is None:
            result.skipped_count += len(indices)
            processed_duplicates.add(identity)
            logger.info("Skipped duplicate resolution for exe identity %s", identity)
            continue

        for index in indices:
            if index == keep_index:
                normalized = normalize_shortcut(working[index])
                if normalized != working[index]:
                    working[index] = normalized
                    result.fixed_count += 1
                    result.changed = True
                    result.touched_indices.add(index)
                continue
            logger.info("Removed duplicate shortcut index %s in favor of %s", index, keep_index)
            working.pop(index, None)
            result.removed_count += 1
            result.changed = True
            result.touched_indices.add(index)
        processed_duplicates.add(identity)

    for index, shortcut in _ordered_shortcuts(dict(working)):
        if index not in issues:
            continue

        current_issues = [issue for issue in issues[index] if not issue.startswith("Duplicate shortcut detected")]
        if not current_issues:
            continue

        print(f"\n[{index}] {shortcut.get('AppName', 'Unknown Shortcut')}")
        for issue in current_issues:
            print(f"  - {issue}")

        has_unfixable_wildcard = any("cannot be safely fixed automatically" in issue for issue in current_issues)
        if has_unfixable_wildcard:
            action = _prompt_action("Action [r=remove, s=skip]: ", {"r", "s"}, "s")
        else:
            default = "s" if any("does not exist on disk" in issue for issue in current_issues) else "f"
            action = _prompt_action("Action [f=fix, r=remove, s=skip]: ", {"f", "r", "s"}, default)
        if action == "s":
            logger.info("Skipped shortcut index %s", index)
            result.skipped_count += 1
            continue

        if action == "r":
            logger.info("Removed shortcut index %s after user confirmation", index)
            working.pop(index, None)
            result.removed_count += 1
            result.changed = True
            result.touched_indices.add(index)
            continue

        normalized = normalize_shortcut(shortcut)
        if normalized != shortcut:
            working[index] = normalized
            result.fixed_count += 1
            result.changed = True
            result.touched_indices.add(index)
            logger.info("Fixed shortcut index %s (%s)", index, normalized.get("AppName", "Unknown"))
        else:
            logger.info("Shortcut index %s already matched the normalization rules", index)
            result.skipped_count += 1

    result.shortcuts = working
    return result
