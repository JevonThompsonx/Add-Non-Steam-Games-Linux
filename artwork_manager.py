from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

from config import (
    API_KEY_ENV_NAMES,
    ARTWORK_MAX_RETRIES,
    ARTWORK_REQUEST_DELAY_SECONDS,
    ARTWORK_REQUESTS,
    get_env_value,
)
from shortcut_builder import (
    build_search_aliases,
    get_shortcut_exe_value,
    get_unsigned_id,
    normalize_lookup_text,
    normalize_posix_path,
    similarity_score,
)

API_BASE_URL = "https://www.steamgriddb.com/api/v2"
ARTWORK_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.steamgriddb.com/",
}


@dataclass(slots=True)
class ArtworkResult:
    downloaded: int = 0
    skipped: int = 0
    failures: int = 0
    downloaded_files: list[Path] = field(default_factory=list)


def _mask_api_key(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def resolve_api_key(prompt_if_missing: bool = True) -> str | None:
    value = get_env_value(*API_KEY_ENV_NAMES)
    if value:
        return value.strip()

    if not prompt_if_missing:
        return None

    import getpass
    response = getpass.getpass("Enter your SteamGridDB API key: ").strip()
    return response or None


class SteamGridDBClient:
    def __init__(self, api_key: str, logger):
        import requests

        self._requests = requests
        self.api_key = api_key
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        self._download_session = requests.Session()
        self._last_request_at = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self.session.close()
        self._download_session.close()

    def validate_api_key(self) -> None:
        payload = self._request_json("/search/autocomplete/test")
        if payload is None:
            raise RuntimeError("SteamGridDB API validation failed. Check connectivity and confirm the API key is valid.")

    def _authorized_get(self, url: str, timeout: int):
        response = self._download_session.get(url, timeout=timeout)
        if response.status_code != 401:
            return response

        response = self._download_session.get(
            url,
            timeout=timeout,
            headers=ARTWORK_DOWNLOAD_HEADERS,
        )
        if response.status_code != 401:
            return response

        response = self.session.get(
            url,
            timeout=timeout,
            headers=ARTWORK_DOWNLOAD_HEADERS,
        )
        if response.status_code != 401:
            return response

        return self._requests.get(
            url,
            timeout=timeout,
            headers=ARTWORK_DOWNLOAD_HEADERS,
        )

    def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_at
        if elapsed < ARTWORK_REQUEST_DELAY_SECONDS:
            time.sleep(ARTWORK_REQUEST_DELAY_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _request_json(self, endpoint: str, params: dict[str, str] | None = None) -> dict | None:
        url = f"{API_BASE_URL}{endpoint}"
        for attempt in range(ARTWORK_MAX_RETRIES):
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=30)
            except self._requests.RequestException as error:
                self.logger.error("SteamGridDB request failed for %s: %s", url, error)
                if attempt == ARTWORK_MAX_RETRIES - 1:
                    return None
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 401:
                raise RuntimeError("SteamGridDB API returned 401. Check the API key in your environment or .env file.")
            if response.status_code == 404:
                self.logger.info("SteamGridDB returned 404 for %s", url)
                return None
            if response.status_code == 429:
                wait_time = 5 * (2 ** attempt)
                self.logger.warning("SteamGridDB rate limit hit for %s; retrying in %s seconds", url, wait_time)
                time.sleep(wait_time)
                continue
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success", False):
                self.logger.warning("SteamGridDB request was not successful for %s", url)
                return None
            return payload
        return None

    def search_game(self, term: str) -> list[dict]:
        payload = self._request_json(f"/search/autocomplete/{quote(term, safe='')}")
        return payload.get("data", []) if payload else []

    def search_game_variants(self, terms: list[str]) -> list[dict]:
        merged: dict[int, dict] = {}
        for term in terms:
            if not term:
                continue
            for match in self.search_game(term):
                raw_id = match.get("id")
                try:
                    match_id = int(raw_id) if raw_id is not None else -1
                except (TypeError, ValueError):
                    continue
                if match_id < 0:
                    continue
                if match_id not in merged:
                    merged[match_id] = match
        return list(merged.values())

    def choose_best_match(self, app_name: str, matches: list[dict]) -> dict | None:
        if not matches:
            return None

        exact = [item for item in matches if normalize_lookup_text(str(item.get("name", ""))) == normalize_lookup_text(app_name)]
        if exact:
            verified = [item for item in exact if item.get("verified")]
            return verified[0] if verified else exact[0]

        def score(item: dict) -> tuple:
            candidate_name = str(item.get("name", "")).strip()
            normalized_candidate = normalize_lookup_text(candidate_name)
            normalized_app = normalize_lookup_text(app_name)
            sim = similarity_score(candidate_name, app_name)
            contains = normalized_app in normalized_candidate or normalized_candidate in normalized_app
            return (
                -(1 if contains else 0),
                -round(sim, 4),
                -(1 if item.get("verified") else 0),
                abs(len(candidate_name) - len(app_name)),
            )

        return sorted(matches, key=score)[0]

    def fetch_best_artwork(self, game_id: int, art_type: str) -> dict | None:
        request = ARTWORK_REQUESTS[art_type]
        payload = self._request_json(request["endpoint"].format(game_id=game_id), request["params"])
        if not payload:
            return None
        data = payload.get("data", [])
        if not data:
            return None
        return sorted(data, key=lambda item: item.get("score", 0), reverse=True)[0]

    def download_to_file(self, url: str, destination: Path) -> bool:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(ARTWORK_MAX_RETRIES):
            self._throttle()
            try:
                response = self._authorized_get(url, timeout=60)
            except self._requests.RequestException as error:
                self.logger.error("Artwork download failed for %s: %s", url, error)
                if attempt == ARTWORK_MAX_RETRIES - 1:
                    return False
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 429:
                wait_time = 5 * (2 ** attempt)
                self.logger.warning("Artwork download rate-limited for %s; retrying in %s seconds", url, wait_time)
                time.sleep(wait_time)
                continue
            if response.status_code == 401:
                self.logger.error("Artwork CDN returned 401 for %s", url)
                if attempt == ARTWORK_MAX_RETRIES - 1:
                    return False
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 404:
                return False
            try:
                response.raise_for_status()
            except self._requests.HTTPError as error:
                self.logger.error("Artwork download failed for %s: %s", url, error)
                return False
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, suffix=".tmp") as handle:
                    handle.write(response.content)
                    temp_path = Path(handle.name)
                temp_path.replace(destination)
                return True
            except OSError as error:
                self.logger.error("Failed to persist artwork to %s: %s", destination, error)
                if temp_path is not None and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                return False
        return False


def _detect_extension(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix == ".jpeg":
        return ".jpg"
    if suffix in {".png", ".jpg"}:
        return suffix
    return ".png"


def _existing_artwork(grid_dir: Path, unsigned_appid: int, art_type: str) -> Path | None:
    filename_pattern = ARTWORK_REQUESTS[art_type]["filename"].format(appid=unsigned_appid, ext="*")
    matches = sorted(grid_dir.glob(filename_pattern))
    return matches[0] if matches else None


def download_artwork_for_shortcut(shortcut: dict, grid_dir: Path, client: SteamGridDBClient, logger) -> ArtworkResult:
    result = ArtworkResult()
    app_name = str(shortcut.get("AppName", "")).strip()
    if not app_name:
        logger.info("Skipping artwork because AppName is blank")
        result.skipped += 1
        return result

    unsigned_appid = get_unsigned_id(int(shortcut.get("appid", 0)))
    existing_icon = _existing_artwork(grid_dir, unsigned_appid, "icon")
    all_present = all(_existing_artwork(grid_dir, unsigned_appid, art_type) is not None for art_type in ARTWORK_REQUESTS)

    if all_present and existing_icon:
        shortcut["icon"] = normalize_posix_path(str(existing_icon))
        result.skipped += len(ARTWORK_REQUESTS)
        logger.info("Artwork already present for %s", app_name)
        return result

    search_terms = build_search_aliases(app_name, get_shortcut_exe_value(shortcut), str(shortcut.get("StartDir", "")))
    logger.info("Artwork search terms for %s: %s", app_name, ", ".join(search_terms[:5]))
    matches = client.search_game_variants(search_terms)
    best_match = client.choose_best_match(app_name, matches)
    if not best_match:
        logger.info("No SteamGridDB match found for %s", app_name)
        result.failures += len(ARTWORK_REQUESTS)
        return result

    game_id = int(best_match["id"])
    logger.info("Using SteamGridDB match '%s' (%s) for %s", best_match.get("name", app_name), game_id, app_name)

    for art_type in ARTWORK_REQUESTS:
        existing = _existing_artwork(grid_dir, unsigned_appid, art_type)
        if existing is not None:
            result.skipped += 1
            if art_type == "icon" and not shortcut.get("icon"):
                shortcut["icon"] = normalize_posix_path(str(existing))
            continue

        art = client.fetch_best_artwork(game_id, art_type)
        if not art or not art.get("url"):
            logger.info("No %s artwork found for %s", art_type, app_name)
            result.failures += 1
            continue

        extension = _detect_extension(str(art["url"]))
        filename = ARTWORK_REQUESTS[art_type]["filename"].format(appid=unsigned_appid, ext=extension)
        destination = grid_dir / filename

        if client.download_to_file(str(art["url"]), destination):
            result.downloaded += 1
            result.downloaded_files.append(destination)
            logger.info("Downloaded %s artwork for %s to %s", art_type, app_name, destination)
            if art_type == "icon":
                shortcut["icon"] = normalize_posix_path(str(destination))
        else:
            result.failures += 1
            logger.error("Failed to download %s artwork for %s", art_type, app_name)

    return result


def download_artwork_for_shortcuts(shortcuts: dict[str, dict], grid_dir: Path, api_key: str, logger) -> ArtworkResult:
    with SteamGridDBClient(api_key, logger) as client:
        logger.info("Using SteamGridDB API key %s", _mask_api_key(api_key))
        client.validate_api_key()
        total = ArtworkResult()
        grid_dir.mkdir(parents=True, exist_ok=True)

        for shortcut in shortcuts.values():
            result = download_artwork_for_shortcut(shortcut, grid_dir, client, logger)
            total.downloaded += result.downloaded
            total.skipped += result.skipped
            total.failures += result.failures
            total.downloaded_files.extend(result.downloaded_files)
    return total


def cleanup_downloaded_artwork(paths: list[Path], logger) -> None:
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except OSError as error:
            logger.warning("Failed to remove staged artwork %s: %s", path, error)
