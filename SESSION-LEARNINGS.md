# Session Learnings: 2026-05-22 — Add Non-Steam Games Linux Scanner Improvements

## Context
Improved false flag detection in Steam non-Steam game scanner (Linux). Added Wine prefix filtering, overlay/injector detection, engine data file filtering, and .env setup message.

## Technical Lessons

### 1. Python `Path.with_suffix()` Gotcha
`Path(".env").with_suffix(".env.example")` produces `.env.env.example` not `.env.example`.
- `.env` has no suffix (just a leading dot), so `with_suffix()` appends to an empty suffix
- Use `env_path.parent / ".env.example"` instead

### 2. Multi-Layer Filtering Strategy
False flag filtering needs multiple layers:
1. **Executable-level**: Filter by stem name (`WINE_SYSTEM_EXE_STEMS`, `SKIP_EXE_STEMS`)
2. **Path-level**: Filter by path patterns (Wine prefix dirs, overlay dirs, engine data)
3. **Scoring**: Low-score items auto-flagged `[needs review]` in UI
4. **Composite check**: `_is_game_executable()` combines all filters

### 3. Game Executable Detection Complexity
- `.exe` files are accepted unconditionally (no execute bit needed on Linux)
- Extensionless files require execute bits (but "unity default resources" can have them)
- `.bin` files in `Engine/Content/` paths are shader caches, not games
- Level data files (level0, level46, level220) are Unity data, not executables

### 4. Wine Prefix Structure
Wine prefixes have `drive_c/windows/system32/`, `drive_c/windows/syswow64/`, etc. Real game exes may also be inside `drive_c/Program Files/`. Only skip Windows system directories, not the entire Wine prefix. Also skip `/windows/microsoft.net/` (Framework tools) and individual system tools like `msbuild`, `hh`.

### 5. .env Security
- `.env` file must not be world-readable: check `st_mode & S_IROTH` and raise `PermissionError`
- `load_local_env()` must not use `@lru_cache` — user may edit `.env` during session
- `check_env_setup()` must validate actual non-empty key value, not just string presence

### 6. Test Strategy
- Unit tests for individual filter functions (`_is_game_executable`)
- Integration tests with `discover_games()` on real `~/Games` directory
- E2E tests verify specific false positive files are filtered
- Use `tempfile.TemporaryDirectory()` for all path tests (never hardcode `/home/user/` paths)
- Never use `os.chdir()` in tests — use `unittest.mock` instead to avoid global side effects
- Module-level imports preferred over per-function imports in tests

### 7. Profile-Specific Filtering
On this system, Crypt of the NecroDancer ships `beatdown.exe` and `beattracker.exe` in `data/custom_music/` and `data/essentia/` — these are audio tools, not games. Add `custom_music` and `essentia` to `SKIP_PATH_KEYWORDS` to filter them.

### Files Modified
- `config.py` — Added WINE_SYSTEM_EXE_STEMS, OVERLAY_DIR_NAMES, ENGINE_DATA_FILE_STEMS; expanded SKIP_PATH_KEYWORDS; removed `@lru_cache`; added `.env` permission check; removed dead `ENV_FILE_NAMES`; removed `HOME = Path.home()` inconsistency; added `GAME_HINT_FILES_LOWER` optimization
- `game_scanner.py` — Added `_is_game_executable()`, `_path_is_in_wine_prefix()`, `_path_is_in_overlay_dir()`, `_path_is_in_steam_downloading()`, `_path_is_engine_data()`; expanded engine data detection; case-insensitive system path check; moved `MAX_EXTENDED_SCAN_DEPTH` after imports
- `main.py` — Added `check_env_setup()` for .env startup message with actual key validation; removed redundant `from pathlib import Path`
- `shortcut_builder.py` — Removed dead Windows-path aliases (`normalize_windows_path`, `quote_windows_path`, `unquote_windows_path`)
- `tests/test_safety.py` — Added 12 new tests; module-level `_is_game_executable` import; removed per-function re-imports; removed `os.chdir` in env test; moved test classes before `if __name__`; fixed `import stat as st` to use module-level `stat`
- `README.md` — Created

### Commands Worth Remembering
```bash
# Run all tests
python -m pytest tests/ -v

# Test specific class
python -m pytest tests/test_safety.py::FalseFlagDetectionTests -v

# Run E2E scan against ~/Games
python -c "from game_scanner import discover_games; from pathlib import Path; import logging; results = discover_games(set(), [], logging.getLogger(), set()); print(f'Found {len(results)} candidates')"

# Test if a specific file is filtered
python -c "from game_scanner import _is_game_executable; print(_is_game_executable(Path('/path/to/file.exe')))"

# Dry-run validation (safe, no writes)
python main.py --dry-run-check

# Flow validation (safe, no writes)
python main.py --validate-flows
```
