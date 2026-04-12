from __future__ import annotations

import io
import logging
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import game_scanner
from artwork_manager import ARTWORK_DOWNLOAD_HEADERS, SteamGridDBClient, cleanup_downloaded_artwork, download_artwork_for_shortcut
from config import ARTWORK_REQUESTS
from config import _parse_scan_dirs
from fixer import diagnose_shortcuts
from game_scanner import discover_games
from main import list_shortcuts
from shortcut_builder import build_shortcut, clean_game_name, get_unsigned_id, is_concrete_exe_path, normalize_posix_path
from steam_paths import SteamUser, list_steam_users
from vdf_manager import backup_shortcuts, load_shortcuts, reindex_shortcuts, verify_persisted_shortcuts, write_shortcuts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executable(path: Path) -> None:
    """Set executable bits on a file (simulates a real Linux binary)."""
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Steam path tests
# ---------------------------------------------------------------------------

class SteamPathSafetyTests(unittest.TestCase):
    def test_list_steam_users_does_not_create_missing_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            steam_root = Path(temp_dir)
            user_dir = steam_root / "userdata" / "123456"
            user_dir.mkdir(parents=True)

            users = list_steam_users(steam_root)

            self.assertEqual(len(users), 1)
            self.assertFalse((user_dir / "config").exists())
            self.assertEqual(users[0].shortcuts_path, user_dir / "config" / "shortcuts.vdf")

    def test_list_steam_users_prefers_loginusers_vdf_over_stale_userdata_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            steam_root = Path(temp_dir)
            config_dir = steam_root / "config"
            userdata_root = steam_root / "userdata"
            config_dir.mkdir(parents=True)
            (userdata_root / "22202").mkdir(parents=True)
            (userdata_root / "33333").mkdir(parents=True)
            (config_dir / "loginusers.vdf").write_text(
                '"users"\n{\n\t"76561197960287930"\n\t{\n\t\t"AccountName"\t\t"test"\n\t\t"MostRecent"\t\t"1"\n\t}\n}\n',
                encoding="utf-8",
            )

            users = list_steam_users(steam_root)

            self.assertEqual([user.user_id for user in users], ["22202"])


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------

class ConfigParsingTests(unittest.TestCase):
    def test_parse_scan_dirs_deduplicates_entries(self) -> None:
        dirs = _parse_scan_dirs("/home/user/Games, /home/user/Games, /opt/games")

        self.assertEqual(len(dirs), 2)
        self.assertIn(Path("/home/user/Games"), dirs)
        self.assertIn(Path("/opt/games"), dirs)

    def test_parse_scan_dirs_expands_home(self) -> None:
        dirs = _parse_scan_dirs("~/Games")

        self.assertEqual(len(dirs), 1)
        self.assertFalse(str(dirs[0]).startswith("~"), "Home directory should be expanded")

    def test_parse_scan_dirs_returns_empty_for_none(self) -> None:
        dirs = _parse_scan_dirs(None)
        self.assertEqual(dirs, [])


# ---------------------------------------------------------------------------
# Artwork tests
# ---------------------------------------------------------------------------

class ArtworkCleanupTests(unittest.TestCase):
    def test_cleanup_downloaded_artwork_removes_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "grid" / "123_icon.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"data")

            cleanup_downloaded_artwork([target], logging.getLogger("test"))

            self.assertFalse(target.exists())

    def test_authorized_get_falls_back_to_plain_download_headers_after_401(self) -> None:
        class FakeResponse:
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[dict | None] = []

            def get(self, url: str, timeout: int, headers: dict | None = None):
                self.calls.append(headers)
                return FakeResponse(401)

        class FakeRequests:
            def __init__(self) -> None:
                self.calls: list[dict | None] = []

            def get(self, url: str, timeout: int, headers: dict | None = None):
                self.calls.append(headers)
                return FakeResponse(200)

        fake_requests = FakeRequests()
        fake_download_session = FakeSession()
        fake_auth_session = FakeSession()

        client = SteamGridDBClient.__new__(SteamGridDBClient)
        setattr(client, "_requests", fake_requests)
        client.api_key = "secret"
        client.logger = logging.getLogger("test")
        setattr(client, "_download_session", fake_download_session)
        setattr(client, "session", fake_auth_session)

        response = client._authorized_get("https://example.com/test.png", timeout=30)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_requests.calls, [ARTWORK_DOWNLOAD_HEADERS])


# ---------------------------------------------------------------------------
# Shortcut builder tests
# ---------------------------------------------------------------------------

class ShortcutBuilderTests(unittest.TestCase):
    def test_build_shortcut_quotes_exe_and_startdir_posix(self) -> None:
        shortcut = build_shortcut("Example Game", "/home/jevonx/Games/Example/game")

        self.assertEqual(shortcut["Exe"], '"/home/jevonx/Games/Example/game"')
        self.assertEqual(shortcut["StartDir"], '"/home/jevonx/Games/Example/"')

    def test_is_concrete_exe_path_accepts_absolute_path_without_extension(self) -> None:
        self.assertTrue(is_concrete_exe_path("/home/jevonx/Games/HollowKnight/hollow_knight"))

    def test_is_concrete_exe_path_accepts_sh_script(self) -> None:
        self.assertTrue(is_concrete_exe_path("/home/jevonx/Games/Game/launch.sh"))

    def test_is_concrete_exe_path_rejects_glob(self) -> None:
        self.assertFalse(is_concrete_exe_path("!(*uninst*|*launcher*)"))

    def test_is_concrete_exe_path_rejects_empty(self) -> None:
        self.assertFalse(is_concrete_exe_path(""))

    def test_normalize_posix_path_strips_quotes(self) -> None:
        self.assertEqual(normalize_posix_path('"/home/user/game"'), "/home/user/game")

    def test_normalize_posix_path_does_not_convert_slashes(self) -> None:
        result = normalize_posix_path("/home/user/My Games/hollow knight")
        self.assertNotIn("\\", result)


# ---------------------------------------------------------------------------
# Game scanner tests
# ---------------------------------------------------------------------------

class GameScannerTests(unittest.TestCase):
    def test_discover_games_skips_existing_app_name_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            game_dir = root / "Hollow Knight"
            game_dir.mkdir(parents=True)
            exe = game_dir / "hollow_knight"
            exe.write_bytes(b"stub")
            _make_executable(exe)

            original_known_dirs = game_scanner.KNOWN_GAME_DIRS
            try:
                game_scanner.KNOWN_GAME_DIRS = [root]
                results = discover_games(
                    existing_exe_paths=set(),
                    steam_common_dirs=[],
                    logger=logging.getLogger("test"),
                    existing_app_names={"hollow knight"},
                )
            finally:
                game_scanner.KNOWN_GAME_DIRS = original_known_dirs

            self.assertEqual(results, [])

    def test_discover_games_finds_executable_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            game_dir = root / "Disco Elysium"
            game_dir.mkdir(parents=True)
            exe = game_dir / "disco_elysium"
            exe.write_bytes(b"\x7fELF" + b"\x00" * 60)  # ELF magic
            _make_executable(exe)

            original_known_dirs = game_scanner.KNOWN_GAME_DIRS
            try:
                game_scanner.KNOWN_GAME_DIRS = [root]
                results = discover_games(
                    existing_exe_paths=set(),
                    steam_common_dirs=[],
                    logger=logging.getLogger("test"),
                    existing_app_names=set(),
                )
            finally:
                game_scanner.KNOWN_GAME_DIRS = original_known_dirs

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].exe_path, exe)

    def test_discover_games_skips_non_executable_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            game_dir = root / "SomeGame"
            game_dir.mkdir(parents=True)
            # Write a file without executable bits
            not_exe = game_dir / "somegame"
            not_exe.write_bytes(b"\x7fELF" + b"\x00" * 60)
            # Do NOT call _make_executable — no execute bits

            original_known_dirs = game_scanner.KNOWN_GAME_DIRS
            try:
                game_scanner.KNOWN_GAME_DIRS = [root]
                results = discover_games(
                    existing_exe_paths=set(),
                    steam_common_dirs=[],
                    logger=logging.getLogger("test"),
                    existing_app_names=set(),
                )
            finally:
                game_scanner.KNOWN_GAME_DIRS = original_known_dirs

            self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Fixer tests
# ---------------------------------------------------------------------------

class FixerTests(unittest.TestCase):
    def test_diagnose_shortcuts_flags_unfixable_wildcard_exe(self) -> None:
        shortcut = build_shortcut("Wildcard Game", "/home/jevonx/Games/Wildcard/game")
        shortcut["Exe"] = "!(*uninst*|*launcher*)"

        issues = diagnose_shortcuts({"0": shortcut})

        self.assertIn("0", issues)
        self.assertTrue(any("cannot be safely fixed automatically" in issue for issue in issues["0"]))


# ---------------------------------------------------------------------------
# VDF workflow tests
# ---------------------------------------------------------------------------

class VdfWorkflowTests(unittest.TestCase):
    def test_write_shortcuts_round_trip_persists_expected_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shortcuts_path = Path(temp_dir) / "shortcuts.vdf"
            data = {
                "shortcuts": reindex_shortcuts(
                    [
                        build_shortcut("Example Game", "/home/jevonx/Games/Example/game"),
                    ]
                )
            }

            write_shortcuts(shortcuts_path, data)
            verified, _ = verify_persisted_shortcuts(shortcuts_path, data)

            self.assertTrue(verified)
            self.assertEqual(load_shortcuts(shortcuts_path), data)

    def test_backup_shortcuts_creates_empty_backup_when_source_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shortcuts_path = Path(temp_dir) / "shortcuts.vdf"

            backup_path = backup_shortcuts(shortcuts_path)

            self.assertTrue(backup_path.exists())
            self.assertEqual(load_shortcuts(backup_path), {"shortcuts": {}})


# ---------------------------------------------------------------------------
# Main workflow tests
# ---------------------------------------------------------------------------

class MainWorkflowTests(unittest.TestCase):
    def test_list_shortcuts_prints_exe_path_from_steam_shortcut_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shortcuts_path = Path(temp_dir) / "config" / "shortcuts.vdf"
            shortcuts_path.parent.mkdir(parents=True)
            shortcut = build_shortcut("Example Game", "/home/jevonx/Games/Example/game")
            write_shortcuts(shortcuts_path, {"shortcuts": {"0": shortcut}})
            user = SteamUser(
                user_id="123456",
                userdata_dir=shortcuts_path.parent.parent,
                config_dir=shortcuts_path.parent,
                shortcuts_path=shortcuts_path,
                grid_dir=shortcuts_path.parent / "grid",
                last_modified=datetime.now(),
            )

            output = io.StringIO()
            with redirect_stdout(output):
                list_shortcuts([user], logging.getLogger("test"))

            self.assertIn('"/home/jevonx/Games/Example/game"', output.getvalue())


# ---------------------------------------------------------------------------
# Artwork workflow tests
# ---------------------------------------------------------------------------

class ArtworkWorkflowTests(unittest.TestCase):
    def test_download_artwork_for_shortcut_uses_existing_files_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            grid_dir = Path(temp_dir)
            shortcut = build_shortcut("Existing Art Game", "/home/jevonx/Games/Art/game")
            unsigned_appid = get_unsigned_id(int(shortcut["appid"]))

            for art_type, request in ARTWORK_REQUESTS.items():
                filename = request["filename"].format(appid=unsigned_appid, ext=".png")
                (grid_dir / filename).write_bytes(b"art")

            dummy_client = SteamGridDBClient.__new__(SteamGridDBClient)
            result = download_artwork_for_shortcut(shortcut, grid_dir, dummy_client, logging.getLogger("test"))

            self.assertEqual(result.downloaded, 0)
            self.assertEqual(result.failures, 0)
            self.assertEqual(result.skipped, len(ARTWORK_REQUESTS))
            self.assertTrue(str(shortcut.get("icon", "")).endswith("_icon.png"))


# ---------------------------------------------------------------------------
# Name cleaning / alias tests
# ---------------------------------------------------------------------------

class ShortcutBuilderNoiseTests(unittest.TestCase):
    def test_chrono_trigger_alias_resolves_correctly(self) -> None:
        result = clean_game_name("Chrono-Trigger-SteamRIP.com")
        self.assertEqual(result, "Chrono Trigger")

    def test_cross_blitz_alias_resolves_correctly(self) -> None:
        result = clean_game_name("Cross.Blitz.Early.Access")
        self.assertEqual(result, "Cross Blitz")

    def test_dragon_quest_i_ii_alias_resolves_correctly(self) -> None:
        result = clean_game_name("Dragon Quest I-II-SteamGG.NET")
        self.assertEqual(result, "Dragon Quest I & II")

    def test_neon_abyss_2_version_suffix_stripped(self) -> None:
        result = clean_game_name("Neon.Abyss.2.v2025.08.01")
        self.assertEqual(result, "Neon Abyss 2")

    def test_hollow_knight_silksong_alias_resolves_correctly(self) -> None:
        result = clean_game_name("Hollow Knight - Silksong")
        self.assertEqual(result, "Hollow Knight: Silksong")

    def test_darksiders_title_not_stripped_by_noise_patterns(self) -> None:
        result = clean_game_name("Darksiders 2")
        self.assertIn("darksiders", result.lower())


if __name__ == "__main__":
    unittest.main()
