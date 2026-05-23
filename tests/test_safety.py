from __future__ import annotations

import io
import logging
import platform
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
from game_scanner import discover_games, _is_game_executable
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

    @unittest.skipIf(platform.system() == "Windows", "execute bits not honoured on Windows")
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

    def test_discover_games_finds_exe_without_execute_bits(self) -> None:
        """Windows .exe files on Linux typically lack execute bits — must still be found."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            game_dir = root / "Cuphead"
            game_dir.mkdir(parents=True)
            exe = game_dir / "Cuphead.exe"
            exe.write_bytes(b"MZ" + b"\x00" * 60)  # PE magic, no execute bits

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
        """Native binaries (no extension) without execute bits must NOT be detected."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            game_dir = root / "SomeGame"
            game_dir.mkdir(parents=True)
            # Write a file without executable bits and no .exe extension
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


# ---------------------------------------------------------------------------
# False flag detection tests (Wine/overlay/engine/steam-downloading)
# ---------------------------------------------------------------------------

class FalseFlagDetectionTests(unittest.TestCase):
    def test_wine_system_tool_stems_are_filtered(self) -> None:
        """Windows system tools like iexplore.exe must not be discovered as games."""
        stems = {"iexplore", "wmplayer", "winebrowser", "systeminfo", "notepad", "cmd", "explorer"}
        for stem in stems:
            with self.subTest(stem=stem):
                with tempfile.TemporaryDirectory() as temp_dir:
                    exe = Path(temp_dir) / f"{stem}.exe"
                    exe.write_bytes(b"MZ" + b"\x00" * 60)
                    self.assertFalse(_is_game_executable(exe), f"{stem}.exe should be filtered")

    def test_wine_prefix_system_paths_are_filtered(self) -> None:
        """Executables in Wine prefix system dirs must not be discovered."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wine_scenarios = [
                root / "Split Fiction" / "drive_c" / "windows" / "system32" / "iexplore.exe",
                root / "Game" / "drive_c" / "windows" / "syswow64" / "systeminfo.exe",
                root / "Game" / "drive_c" / "Program Files (x86)" / "Internet Explorer" / "iexplore.exe",
                root / "Game" / "drive_c" / "Program Files (x86)" / "Windows Media Player" / "wmplayer.exe",
                root / "Game" / "drive_c" / "windows" / "system32" / "winebrowser.exe",
            ]
            for exe in wine_scenarios:
                with self.subTest(path=str(exe)):
                    exe.parent.mkdir(parents=True, exist_ok=True)
                    exe.write_bytes(b"MZ" + b"\x00" * 60)
                    self.assertFalse(_is_game_executable(exe), f"{exe} should be filtered by Wine prefix check")

    def test_overlay_dir_stems_are_filtered(self) -> None:
        """Executables in __overlay directories must not be discovered."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overlay_scenarios = [
                root / "Game" / "__overlay" / "overlayinjector.exe",
                root / "Game" / "_overlay" / "hooks.dll",
                root / "Game" / "trainer" / "cheat.exe",
            ]
            for exe in overlay_scenarios:
                with self.subTest(path=str(exe)):
                    exe.parent.mkdir(parents=True, exist_ok=True)
                    exe.write_bytes(b"MZ" + b"\x00" * 60)
                    self.assertFalse(_is_game_executable(exe), f"{exe} should be filtered by overlay check")

    def test_steam_downloading_paths_are_filtered(self) -> None:
        """Executables in steamapps/downloading/ must not be discovered."""
        with tempfile.TemporaryDirectory() as temp_dir:
            dl_dir = Path(temp_dir) / "steamapps" / "downloading" / "2228030"
            dl_dir.mkdir(parents=True)
            exe = dl_dir / "game.exe"
            exe.write_bytes(b"MZ" + b"\x00" * 60)
            self.assertFalse(_is_game_executable(exe), "Steam downloading exe should be filtered")

    def test_engine_data_bin_files_are_filtered(self) -> None:
        """.bin files in Engine/Content/ paths must not be discovered."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine_scenarios = [
                root / "Rune Factory" / "Engine" / "Content" / "Renderer" / "TessellationTable.bin",
                root / "Game" / "Engine" / "Binaries" / "ShaderCache.bin",
                root / "Game" / "Engine" / "Plugins" / "PluginData.bin",
            ]
            for exe in engine_scenarios:
                with self.subTest(path=str(exe)):
                    exe.parent.mkdir(parents=True, exist_ok=True)
                    exe.write_bytes(b"\x00" * 100)
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
                    self.assertFalse(_is_game_executable(exe), f"{exe} should be filtered by engine data check")

    def test_engine_data_file_stems_are_filtered(self) -> None:
        """Extensionless files with known engine data names must not be discovered."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "unity default resources"
            data_file.write_bytes(b"\x00" * 100)
            data_file.chmod(data_file.stat().st_mode | stat.S_IXUSR)
            self.assertFalse(
                _is_game_executable(data_file),
                f"Engine data file '{data_file.name}' should be filtered"
            )

    def test_real_game_exe_not_filtered(self) -> None:
        """Actual game executables must still be discovered correctly."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Normal game .exe in regular path
            game_exe = Path(temp_dir) / "Card-en-Ciel.exe"
            game_exe.write_bytes(b"MZ" + b"\x00" * 60)
            self.assertTrue(
                _is_game_executable(game_exe),
                f"Real game exe '{game_exe.name}' should be discovered"
            )

    def test_linux_native_game_binary_not_filtered(self) -> None:
        """Linux native game binaries (no extension, execute bits) must still be found."""
        with tempfile.TemporaryDirectory() as temp_dir:
            native_exe = Path(temp_dir) / "hollow_knight"
            native_exe.write_bytes(b"\x7fELF" + b"\x00" * 60)
            native_exe.chmod(native_exe.stat().st_mode | stat.S_IXUSR)
            self.assertTrue(
                _is_game_executable(native_exe),
                f"Linux native binary '{native_exe.name}' should be discovered"
            )

    def test_game_sh_script_not_filtered(self) -> None:
        """Game shell scripts must still be discovered."""
        with tempfile.TemporaryDirectory() as temp_dir:
            sh_exe = Path(temp_dir) / "start_game.sh"
            sh_exe.write_bytes(b"#!/bin/bash\necho start")
            sh_exe.chmod(sh_exe.stat().st_mode | stat.S_IXUSR)
            self.assertTrue(
                _is_game_executable(sh_exe),
                f"Shell script '{sh_exe.name}' should be discovered"
            )

    def test_wine_prefix_regular_game_inside_not_filtered(self) -> None:
        """A real game exe inside a Wine prefix (but not in system dirs) must be found."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Real game in Wine prefix but NOT in system dirs
            prefix_dir = Path(temp_dir) / "drive_c" / "Program Files" / "Split Fiction"
            prefix_dir.mkdir(parents=True)
            game_exe = prefix_dir / "SplitFiction.exe"
            game_exe.write_bytes(b"MZ" + b"\x00" * 60)
            self.assertTrue(
                _is_game_executable(game_exe),
                "Real game exe in Wine prefix (non-system dir) should be discovered"
            )

    def test_discover_games_excludes_wine_system_exes(self) -> None:
        """Full discover_games pipeline must exclude Wine system executables."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            # Create a real game
            game_dir = root / "Hollow Knight"
            game_dir.mkdir(parents=True)
            real_exe = game_dir / "hollow_knight"
            real_exe.write_bytes(b"\x7fELF" + b"\x00" * 60)
            real_exe.chmod(real_exe.stat().st_mode | stat.S_IXUSR)

            # Create a Wine prefix with system exe
            wine_dir = root / "Split Fiction" / "drive_c" / "windows" / "system32"
            wine_dir.mkdir(parents=True)
            bad_exe = wine_dir / "iexplore.exe"
            bad_exe.write_bytes(b"MZ" + b"\x00" * 60)

            # Create an overlay dir
            overlay_dir = root / "It Takes Two" / "__overlay"
            overlay_dir.mkdir(parents=True)
            overlay_exe = overlay_dir / "overlayinjector.exe"
            overlay_exe.write_bytes(b"MZ" + b"\x00" * 60)

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

            # Should only find Hollow Knight, not iexplore or overlayinjector
            self.assertEqual(len(results), 1, "Should find exactly 1 game (Hollow Knight)")
            self.assertIn("hollow", results[0].app_name.lower())


# ---------------------------------------------------------------------------
# .env setup check tests
# ---------------------------------------------------------------------------

class EnvSetupTests(unittest.TestCase):
    def test_env_check_missing_file_shows_helpful_message(self) -> None:
        """When no .env file exists, check_env_setup should print a helpful message."""
        import logging
        from main import check_env_setup

        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        with redirect_stdout(output):
            check_env_setup(logging.getLogger("test"))

        # Should reference .env.example since it exists in the project dir
        result = output.getvalue()
        self.assertTrue(".env" in result or "Copy" in result or "Tip" in result, f"Unexpected output: {result}")


if __name__ == "__main__":
    unittest.main()
