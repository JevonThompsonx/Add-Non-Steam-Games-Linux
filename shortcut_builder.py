from __future__ import annotations

import binascii
import os
import re
import struct
from difflib import SequenceMatcher
from pathlib import Path

from config import GENERIC_CONTAINER_DIRS, REQUIRED_SHORTCUT_DEFAULTS

RELEASE_TAG_PATTERNS = [
    r"-gog$",
    r"-codex$",
    r"-plaza$",
    r"-fitgirl$",
    r"-repack$",
    r"\(gog\)$",
    r"\(epic\)$",
    r"\[gog\]$",
]

NOISE_PATTERNS = [
    r"[-_. ]*steamgg[-_. ]*net\b",
    r"[-_. ]*steamrip[-_. ]*com\b",
    r"\bsteamgg(?:\.net)?\b",
    r"\bsteamrip(?:\.com)?\b",
    r"\bfitgirl(?:-repacks(?:\.site)?)?\b",
    r"\bcodex\b",
    r"\bprophet\b",
    r"\bgoldberg(?:\s+emu)?\b",
    r"\breloaded\b",
    r"\brune\b",
    r"\btenoke\b",
    r"\bflt\b",
    r"\bportable\b",
    r"\bno\s*dvd\b",
    r"\bdx\s*11\b",
    r"\bdx\s*12\b",
    r"\bsteam\b",
    r"\blinux\b",          # strip "linux" suffix from folder names
    r"\bx86_64\b",
    r"\bx86\b",
    r"\bx64\b",
]

TRAILING_NOISE_PATTERNS = [
    r"\blauncher\b$",
    r"\bgame\b$",
]

ROMAN_NUMERALS = {
    "1": "I",
    "2": "II",
    "3": "III",
    "4": "IV",
    "5": "V",
    "6": "VI",
    "7": "VII",
    "8": "VIII",
    "9": "IX",
    "10": "X",
}

ROMAN_TO_ARABIC = {value: key for key, value in ROMAN_NUMERALS.items()}

MANUAL_TITLE_ALIASES = {
    "acodyssey": ["Assassin's Creed Odyssey"],
    "ac odyssey": ["Assassin's Creed Odyssey"],
    "digimon world next order": ["Digimon World: Next Order"],
    "ghost of tsushima dc": ["Ghost of Tsushima Director's Cut"],
    "horizon zero dawn": ["Horizon Zero Dawn"],
    "lego the incredibles": ["LEGO The Incredibles"],
    "smt iii nocturne hd remaster": ["Shin Megami Tensei III: Nocturne - HD Remaster"],
    "ys viii lacrimosa of dana": ["Ys VIII: Lacrimosa of Dana"],
    # ── Games on this CachyOS system ─────────────────────────────────────────
    "absolum": ["Absolum"],
    # BALLxPIT — exe stem "Balls" weakly matches dir name; alias ensures correct title
    "ballxpit": ["BALLxPIT"],
    "celeste": ["Celeste"],
    "chrono trigger steamrip com": ["Chrono Trigger"],
    "chrono trigger steamrip": ["Chrono Trigger"],
    "chrono trigger": ["Chrono Trigger"],
    "constance": ["Constance"],
    "cross blitz early access": ["Cross Blitz"],
    "cross blitz": ["Cross Blitz"],
    "cuphead": ["Cuphead"],
    "disco elysium": ["Disco Elysium"],
    "dragon quest i ii steamgg net": ["Dragon Quest I & II"],
    "dragon quest i ii": ["Dragon Quest I & II"],
    "dragon quest iii hd 2d remake": ["Dragon Quest III HD-2D Remake"],
    "etrian odyssey origins collection": ["Etrian Odyssey Origins Collection"],
    # Final Fantasy Pixel Remaster — folder names use the "PR" abbreviation
    "final fantasy i pr": ["Final Fantasy I Pixel Remaster"],
    "final fantasy ii pr": ["Final Fantasy II Pixel Remaster"],
    "final fantasy iii pr": ["Final Fantasy III Pixel Remaster"],
    "final fantasy iv pr": ["Final Fantasy IV Pixel Remaster"],
    "final fantasy v pr": ["Final Fantasy V Pixel Remaster"],
    "final fantasy vi pr": ["Final Fantasy VI Pixel Remaster"],
    "furi": ["Furi"],
    "hollow knight silksong": ["Hollow Knight: Silksong"],
    "monster hunter stories": ["Monster Hunter Stories"],
    "neon abyss 2": ["Neon Abyss 2"],
    # Possessor(s) — parentheses survive clean_game_name; alias restores them
    "possessor s": ["Possessor(s)"],
    "possessor": ["Possessor(s)"],
    "risk of rain 2": ["Risk of Rain 2"],
    "shape of dreams": ["Shape of Dreams"],
    # Suikoden — folder name uses "&", exe stem uses "and"
    "suikoden i and ii hd remaster": ["Suikoden I & II HD Remaster"],
    "suikoden i ii hd remaster": ["Suikoden I & II HD Remaster"],
    # Ys I / II — GOG releases; long subtitles from the alias
    "ys i": ["Ys I: Ancient Ys Vanished"],
    "ys ii": ["Ys II: Ancient Ys Vanished – The Final Chapter"],
}


def normalize_lookup_text(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(text.split())


def _smart_title(value: str) -> str:
    words: list[str] = []
    small_words = {"a", "an", "and", "of", "the", "to", "for", "in"}
    for index, raw_word in enumerate(value.split()):
        lowered = raw_word.lower()
        if raw_word.isupper() and len(raw_word) <= 6:
            words.append(raw_word)
            continue
        if lowered in small_words and index != 0:
            words.append(lowered)
            continue
        if lowered == "vs":
            words.append("vs.")
            continue
        words.append(lowered.capitalize())
    titled = " ".join(words)
    titled = re.sub(r"\bIi\b", "II", titled)
    titled = re.sub(r"\bIii\b", "III", titled)
    titled = re.sub(r"\bIv\b", "IV", titled)
    titled = re.sub(r"\bVi\b", "VI", titled)
    titled = re.sub(r"\bVii\b", "VII", titled)
    titled = re.sub(r"\bViii\b", "VIII", titled)
    titled = re.sub(r"\bIx\b", "IX", titled)
    return titled


def normalize_posix_path(path: str) -> str:
    """Normalize a POSIX path string without ever converting forward slashes.

    ``pathlib.Path`` converts ``/`` to ``\\`` on Windows, which is wrong here
    because this code always produces Linux paths (even when tested on Windows).
    We therefore do all normalization at the string level.
    """
    value = str(path or "").strip().strip('"').strip()
    if not value:
        return ""
    # Expand ~ at the string level so we avoid Path() slash conversion
    if value.startswith("~/") or value == "~":
        home = Path.home().as_posix()
        value = home + value[1:]
    # Collapse double slashes but do NOT call Path() — keep forward slashes
    while "//" in value:
        value = value.replace("//", "/")
    return value


def quote_posix_path(path: str, trailing_slash: bool = False) -> str:
    normalized = normalize_posix_path(path)
    if not normalized:
        return ""
    if trailing_slash and not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return f'"{normalized}"'


def unquote_posix_path(path: str) -> str:
    return normalize_posix_path(path)


# Keep Windows-named aliases so shared code continues to work
normalize_windows_path = normalize_posix_path
quote_windows_path = quote_posix_path
unquote_windows_path = unquote_posix_path


def has_glob_pattern(path: str) -> bool:
    value = str(path or "")
    return any(token in value for token in ("*", "?", "[", "]", "(", ")", "|"))


def is_concrete_exe_path(path: str) -> bool:
    """On Linux, a concrete exe path is any absolute path that doesn't contain globs."""
    value = unquote_posix_path(path)
    if not value:
        return False
    if has_glob_pattern(value):
        return False
    # Accept absolute paths to any file (no .exe requirement on Linux)
    return value.startswith("/") or value.startswith("~")


def generate_shortcut_id(exe: str, app_name: str) -> int:
    unique_id = f"{exe}{app_name}"
    crc = binascii.crc32(unique_id.encode("utf-8")) & 0xFFFFFFFF
    shortcut_id = crc | 0x80000000
    return struct.unpack("i", struct.pack("I", shortcut_id))[0]


def get_unsigned_id(signed_appid: int) -> int:
    return struct.unpack("I", struct.pack("i", int(signed_appid)))[0]


def clean_game_name(folder_name: str) -> str:
    name = folder_name.strip()
    # Strip distribution-site suffixes BEFORE camelCase splitting so patterns
    # like "SteamRIP.com" and "SteamGG.NET" are removed while still contiguous.
    name = re.sub(r"[-_. ]*steamrip(?:\.com)?", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[-_. ]*steamgg(?:\.net)?", "", name, flags=re.IGNORECASE)
    name = re.sub(r"(?<=[a-z])(?=[A-Z]{2,}\b)", " ", name)
    for pattern in RELEASE_TAG_PATTERNS:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    name = re.sub(r"[-_.]v?\d+(?:\.\d+)+.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    name = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", name)
    name = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][A-Z][a-z])", " ", name)
    name = name.replace("_", " ")
    name = re.sub(r"\.(?!\d)", " ", name)
    name = re.sub(r"\s+-\s+", " - ", name)
    for pattern in NOISE_PATTERNS:
        name = re.sub(pattern, " ", name, flags=re.IGNORECASE)
    for pattern in TRAILING_NOISE_PATTERNS:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*[:]\s*", ": ", name)
    name = re.sub(r"\s+", " ", name).strip(" -_.")
    normalized_lookup = normalize_lookup_text(name)
    manual_aliases = MANUAL_TITLE_ALIASES.get(normalized_lookup)
    if manual_aliases:
        return manual_aliases[0]
    if name:
        name = _smart_title(name)
    return name or folder_name.strip()


def build_search_aliases(app_name: str, exe_path: str = "", start_dir: str = "") -> list[str]:
    candidates: list[str] = []

    def add(value: str) -> None:
        cleaned = clean_game_name(value)
        if not cleaned:
            return
        if cleaned not in candidates:
            candidates.append(cleaned)

    add(app_name)

    normalized_app_name = normalize_lookup_text(app_name)
    for alias in MANUAL_TITLE_ALIASES.get(normalized_app_name, []):
        add(alias)

    for raw_path in (exe_path, start_dir):
        normalized_exe = unquote_posix_path(raw_path)
        if not normalized_exe:
            continue
        path = Path(normalized_exe)
        # Strip common extensions
        stem = path.stem
        if path.suffix.lower() in (".sh", ".exe", ".bin", ".run", ".x86_64", ".x86"):
            add(stem)
        for parent in [path.parent, *path.parents[1:3]]:
            if not parent.name:
                continue
            if parent.name.lower() in GENERIC_CONTAINER_DIRS:
                continue
            add(parent.name)

    expanded: list[str] = []
    for candidate in candidates:
        if candidate not in expanded:
            expanded.append(candidate)
        normalized_candidate = normalize_lookup_text(candidate)
        for alias in MANUAL_TITLE_ALIASES.get(normalized_candidate, []):
            if alias not in expanded:
                expanded.append(alias)
        arabic_variant = candidate
        for roman, arabic in ROMAN_TO_ARABIC.items():
            arabic_variant = re.sub(rf"\b{roman}\b", arabic, arabic_variant)
        if arabic_variant != candidate and arabic_variant not in expanded:
            expanded.append(arabic_variant)
        roman_variant = candidate
        for arabic, roman in ROMAN_NUMERALS.items():
            roman_variant = re.sub(rf"\b{arabic}\b", roman, roman_variant)
        if roman_variant != candidate and roman_variant not in expanded:
            expanded.append(roman_variant)

    return expanded


def is_probably_invalid_app_name(name: str) -> bool:
    value = (name or "").strip()
    if not value:
        return True
    alnum_count = sum(character.isalnum() for character in value)
    return alnum_count == 0


def derive_app_name_from_path(exe_path: str) -> str:
    normalized = unquote_posix_path(exe_path)
    if not normalized:
        return "Unknown Game"
    path = Path(normalized)
    for parent in [path.parent, *path.parents[1:]]:
        if not parent.name:
            continue
        if parent.name.lower() in GENERIC_CONTAINER_DIRS:
            continue
        return clean_game_name(parent.name)
    return clean_game_name(path.stem)


def prettify_exe_stem(stem: str) -> str:
    value = clean_game_name(stem)
    manual = MANUAL_TITLE_ALIASES.get(normalize_lookup_text(value))
    if manual:
        return manual[0]
    return value


def get_shortcut_exe_value(shortcut: dict) -> str:
    for key in ("exe", "Exe"):
        value = str(shortcut.get(key, "")).strip()
        if value:
            return value
    return ""


def get_shortcut_openvr_value(shortcut: dict) -> int:
    for key in ("openvr", "OpenVR"):
        value = shortcut.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def normalize_tags(tags: object) -> dict[str, str]:
    if isinstance(tags, dict):
        ordered_values = [str(value) for _, value in sorted(tags.items(), key=lambda item: str(item[0])) if str(value).strip()]
    elif isinstance(tags, (list, tuple, set)):
        ordered_values = [str(value) for value in tags if str(value).strip()]
    else:
        ordered_values = []
    return {str(index): value for index, value in enumerate(ordered_values)}


def build_shortcut(app_name: str, exe_path: str, icon_path: str = "", launch_options: str = "") -> dict:
    normalized_exe = normalize_posix_path(exe_path)
    # Derive parent directory via string split to avoid Path() converting / to \ on Windows
    if normalized_exe and "/" in normalized_exe:
        start_dir_str = normalized_exe.rsplit("/", 1)[0] + "/"
    elif normalized_exe:
        start_dir_str = "./"
    else:
        start_dir_str = "./"
    quoted_exe = quote_posix_path(normalized_exe)
    quoted_start_dir = f'"{start_dir_str}"' if start_dir_str else ""
    final_name = clean_game_name(app_name) if app_name else derive_app_name_from_path(normalized_exe)
    appid = generate_shortcut_id(quoted_exe, final_name)

    shortcut = {
        "appid": appid,
        "AppName": final_name,
        "Exe": quoted_exe,
        "StartDir": quoted_start_dir,
        "icon": normalize_posix_path(icon_path),
        "sortas": "",
        "tags": {},
    }
    shortcut.update(REQUIRED_SHORTCUT_DEFAULTS)
    if launch_options:
        shortcut["LaunchOptions"] = launch_options
    return shortcut


def normalize_shortcut(shortcut: dict) -> dict:
    original = dict(shortcut)
    exe_path = unquote_posix_path(get_shortcut_exe_value(original))
    app_name = str(original.get("AppName", "")).strip()
    if is_probably_invalid_app_name(app_name):
        app_name = derive_app_name_from_path(exe_path or str(original.get("ShortcutPath", "")))
    app_name = clean_game_name(app_name)

    normalized = dict(original)
    normalized["AppName"] = app_name
    if is_concrete_exe_path(get_shortcut_exe_value(original)):
        normalized["Exe"] = quote_posix_path(exe_path)
    else:
        normalized["Exe"] = get_shortcut_exe_value(original)
    normalized.pop("exe", None)

    if is_concrete_exe_path(get_shortcut_exe_value(original)) and exe_path:
        normalized["StartDir"] = quote_posix_path(str(Path(exe_path).parent), trailing_slash=True)
    else:
        start_dir = unquote_posix_path(str(original.get("StartDir", "")))
        normalized["StartDir"] = quote_posix_path(start_dir, trailing_slash=True) if start_dir else ""

    normalized["icon"] = normalize_posix_path(str(original.get("icon", "")))
    normalized["sortas"] = str(original.get("sortas", ""))
    normalized["ShortcutPath"] = str(original.get("ShortcutPath", REQUIRED_SHORTCUT_DEFAULTS["ShortcutPath"]))
    normalized["LaunchOptions"] = str(original.get("LaunchOptions", REQUIRED_SHORTCUT_DEFAULTS["LaunchOptions"]))

    for field, default in REQUIRED_SHORTCUT_DEFAULTS.items():
        if field in {"ShortcutPath", "LaunchOptions"}:
            continue
        value = original.get(field, default)
        if isinstance(default, int):
            try:
                normalized[field] = int(value)
            except (TypeError, ValueError):
                normalized[field] = default
        else:
            normalized[field] = str(value)

    normalized["tags"] = normalize_tags(original.get("tags", {}))
    normalized["OpenVR"] = get_shortcut_openvr_value(original)
    normalized.pop("openvr", None)

    try:
        normalized["appid"] = int(original.get("appid", 0))
    except (TypeError, ValueError):
        quoted_exe = normalized.get("Exe", "")
        if quoted_exe and normalized["AppName"]:
            normalized["appid"] = generate_shortcut_id(quoted_exe, normalized["AppName"])
        else:
            normalized["appid"] = 0

    return normalized


def normalized_exe_identity(exe_or_shortcut: str | dict) -> str:
    if isinstance(exe_or_shortcut, dict):
        value = get_shortcut_exe_value(exe_or_shortcut)
    else:
        value = str(exe_or_shortcut)
    return unquote_posix_path(value)


def shortcut_completeness_score(shortcut: dict) -> int:
    score = 0
    app_name = str(shortcut.get("AppName", "")).strip()
    exe_value = get_shortcut_exe_value(shortcut).strip()
    start_dir = str(shortcut.get("StartDir", "")).strip()

    if app_name and not is_probably_invalid_app_name(app_name):
        score += 3
    if exe_value:
        score += 3
    if exe_value.startswith('"') and exe_value.endswith('"'):
        score += 1
    if start_dir:
        score += 2
    if start_dir.startswith('"') and start_dir.endswith('"'):
        score += 1
    if shortcut.get("AllowDesktopConfig") == 1:
        score += 1
    if shortcut.get("AllowOverlay") == 1:
        score += 1
    if get_shortcut_openvr_value(shortcut) == 0:
        score += 1
    if shortcut.get("icon"):
        score += 1

    exe_path = unquote_posix_path(exe_value)
    if exe_path and Path(exe_path).exists():
        score += 3
    return score


def similarity_score(left: str, right: str) -> float:
    normalized_left = re.sub(r"[^a-z0-9]+", "", left.lower())
    normalized_right = re.sub(r"[^a-z0-9]+", "", right.lower())
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()
