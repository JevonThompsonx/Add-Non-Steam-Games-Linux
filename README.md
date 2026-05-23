# Add Non-Steam Games Linux

Scan game directories, add non-Steam games to Steam, download artwork from SteamGridDB. Filters Wine prefix system files, overlay injectors, engine data, and Steam downloading dirs.

## Quick Start

```bash
cp .env.example .env        # then add your STEAMGRIDDB_API_KEY
pip install -r requirements.txt
python main.py
```

## CLI

| Flag | Description |
|------|-------------|
| `--diagnose` | Run diagnostics |
| `--dry-run-check` | Validate setup without making changes |
| `--validate-flows` | Validate all workflows |

## .env

| Variable | Description |
|----------|-------------|
| `STEAMGRIDDB_API_KEY` | SteamGridDB API key for artwork |
| `SCAN_DIRS` | Extra dirs to scan (colon/comma separated) |
| `STEAM_PATH` | Override Steam install path |

## Structure

```
main.py            CLI + workflow
config.py          Scan patterns + skip lists
game_scanner.py    Game discovery + false flag detection
artwork_manager.py SteamGridDB artwork downloads
shortcut_builder.py Shortcut VDF construction
vdf_manager.py     VDF read/write with validation
steam_paths.py     Steam installation detection
fixer.py           Interactive shortcut fixing
logger_setup.py    Logging config
tests/
  test_safety.py   41 tests
```
