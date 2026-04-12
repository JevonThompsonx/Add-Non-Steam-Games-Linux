from __future__ import annotations

import importlib
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from config import DUMMY_SHORTCUT_EXE, DUMMY_SHORTCUT_NAME
from shortcut_builder import build_shortcut, get_shortcut_exe_value, normalized_exe_identity


def empty_shortcuts_data() -> dict:
    return {"shortcuts": {}}


def _import_vdf():
    return importlib.import_module("vdf")


def serialize_shortcuts(data: dict) -> bytes:
    vdf = _import_vdf()
    return vdf.binary_dumps(data)


def load_shortcuts(shortcuts_path: str | Path) -> dict:
    path = Path(shortcuts_path)
    if not path.exists() or path.stat().st_size == 0:
        return empty_shortcuts_data()

    payload = path.read_bytes()
    vdf = _import_vdf()
    data = vdf.binary_loads(payload)
    if "shortcuts" not in data or not isinstance(data["shortcuts"], dict):
        return empty_shortcuts_data()
    return data


def reindex_shortcuts(shortcuts: dict | list[dict]) -> dict:
    if isinstance(shortcuts, dict):
        ordered_values = [value for _, value in sorted(shortcuts.items(), key=lambda item: int(str(item[0])))]
    else:
        ordered_values = list(shortcuts)
    return {str(index): shortcut for index, shortcut in enumerate(ordered_values)}


def backup_shortcuts(shortcuts_path: str | Path) -> Path:
    path = Path(shortcuts_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"shortcuts.vdf.bak.{timestamp}")
    if path.exists():
        shutil.copy2(path, backup_path)
    else:
        backup_path.write_bytes(serialize_shortcuts(empty_shortcuts_data()))
    return backup_path


def write_shortcuts(shortcuts_path: str | Path, data: dict) -> None:
    path = Path(shortcuts_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_shortcuts(data)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=path.parent, suffix=".tmp") as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        temp_path.replace(path)
    except OSError:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def round_trip_integrity_test(shortcuts_path: str | Path) -> tuple[bool, str]:
    path = Path(shortcuts_path)
    if not path.exists() or path.stat().st_size == 0:
        return True, "shortcuts.vdf is missing or empty; treating it as a fresh file"

    original_bytes = path.read_bytes()
    vdf = _import_vdf()
    data = vdf.binary_loads(original_bytes)
    reserialized = vdf.binary_dumps(data)

    if original_bytes == reserialized:
        return True, "PASS: round-trip produced byte-identical output"

    minimum_length = min(len(original_bytes), len(reserialized))
    first_diff = None
    for index in range(minimum_length):
        if original_bytes[index] != reserialized[index]:
            first_diff = index
            break
    if first_diff is None and len(original_bytes) != len(reserialized):
        first_diff = minimum_length
    detail = (
        f"FAIL: round-trip mismatch. original={len(original_bytes)} bytes, "
        f"reserialized={len(reserialized)} bytes, first_diff={first_diff}"
    )
    return False, detail


def verify_field_quoting(shortcuts_data: dict) -> list[str]:
    warnings: list[str] = []
    for index, shortcut in shortcuts_data.get("shortcuts", {}).items():
        app_name = shortcut.get("AppName", f"Shortcut {index}")
        exe = get_shortcut_exe_value(shortcut)
        start_dir = str(shortcut.get("StartDir", ""))
        if exe and not (exe.startswith('"') and exe.endswith('"')):
            warnings.append(f"{app_name}: exe is not quoted ({exe})")
        if start_dir and not (start_dir.startswith('"') and start_dir.endswith('"')):
            warnings.append(f"{app_name}: StartDir is not quoted ({start_dir})")
    return warnings


def add_one_and_verify_test(shortcuts_path: str | Path) -> tuple[bool, str]:
    path = Path(shortcuts_path)
    current_data = load_shortcuts(path)
    current_shortcuts = current_data.get("shortcuts", {})
    baseline_count = len(current_shortcuts)

    dummy_shortcut = build_shortcut(DUMMY_SHORTCUT_NAME, DUMMY_SHORTCUT_EXE)
    combined_shortcuts = reindex_shortcuts([*current_shortcuts.values(), dummy_shortcut])
    test_data = {"shortcuts": combined_shortcuts}

    with tempfile.TemporaryDirectory(prefix="steam-shortcuts-") as temp_dir:
        temp_path = Path(temp_dir) / "shortcuts.vdf"
        write_shortcuts(temp_path, test_data)
        reloaded = load_shortcuts(temp_path)

    reloaded_shortcuts = reloaded.get("shortcuts", {})
    if len(reloaded_shortcuts) != baseline_count + 1:
        return False, "FAIL: temporary add-one test changed the shortcut count unexpectedly"

    if normalized_exe_identity(reloaded_shortcuts[str(baseline_count)]) != normalized_exe_identity(dummy_shortcut):
        return False, "FAIL: temporary add-one test could not read back the dummy shortcut"

    return True, "PASS: temporary add-one validation succeeded"


def verify_persisted_shortcuts(shortcuts_path: str | Path, expected_data: dict) -> tuple[bool, str]:
    reloaded = load_shortcuts(shortcuts_path)
    if reloaded == expected_data:
        return True, "PASS: written shortcuts.vdf matches the expected data"
    return False, "FAIL: written shortcuts.vdf did not match the expected data after reload"


def collect_existing_exe_paths(shortcuts_data: dict) -> set[str]:
    shortcuts = shortcuts_data.get("shortcuts", {})
    return {
        normalized_exe_identity(shortcut)
        for shortcut in shortcuts.values()
        if normalized_exe_identity(shortcut)
    }
