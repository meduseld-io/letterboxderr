#!/usr/bin/env python3
"""
Letterboxderr - Sync Letterboxd watchlists to Seerr/Jellyseerr/Overseerr

Fetches a user's Letterboxd watchlist by scraping the watchlist page,
resolves TMDb IDs, and adds movies and TV shows to their Seerr watchlist.
"""

import html as html_lib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("letterboxderr")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def env_int(name, default, minimum=0):
    """Read an integer env var, exiting with a clear message if it's invalid."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"{name} must be an integer, got {raw!r}")
    if value < minimum:
        sys.exit(f"{name} must be >= {minimum}, got {value}")
    return value


SEERR_URL = os.environ.get("SEERR_URL", "http://localhost:5055").rstrip("/")
SEERR_API_KEY = os.environ.get("SEERR_API_KEY", "")
SYNC_INTERVAL = env_int("SYNC_INTERVAL", 3600)  # seconds
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Letterboxd usernames are letters, digits and underscores
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,30}$")

USER_AGENT = "Mozilla/5.0 (compatible; Letterboxderr; +https://github.com/meduseld-io/letterboxderr)"


class WatchlistFetchError(Exception):
    """Raised when a Letterboxd watchlist can't be fetched."""


def is_valid_username(username):
    return bool(username) and bool(USERNAME_RE.match(username))


def make_client():
    """
    Create an HTTP client for Letterboxd. A fresh one per sync run, because
    sessions aren't safe to share between threads.
    """
    if cloudscraper:
        return cloudscraper.create_scraper()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def http_get(client, url, retries=3, **kwargs):
    """GET with retries and backoff on 429 and 5xx, honouring Retry-After."""
    delay = 2
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, **kwargs)
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code != 429 and resp.status_code < 500:
            return resp
        if attempt == retries:
            return resp
        wait = delay
        retry_after = resp.headers.get("Retry-After", "")
        if retry_after.isdigit():
            wait = min(int(retry_after), 60)
        logger.warning("Got %d from %s, retrying in %ds", resp.status_code, url, wait)
        time.sleep(wait)
        delay *= 2
    return resp


# ---------------------------------------------------------------------------
# State persistence
#
# {
#   "synced": {"<letterboxd>:<seerr_user>": ["movie:123", "tv:456", ...]},
#   "films":  {"/film/slug/": {"tmdb_id": 123, "media_type": "movie",
#                              "title": "...", "year": 2024}}
# }
#
# "films" caches each film page lookup, since a film's TMDb ID never changes.
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()


def media_key(media_type, tmdb_id):
    """Dedup key. Movie and TV TMDb IDs are separate number spaces."""
    return f"{media_type}:{tmdb_id}"


def _migrate_state(raw):
    """Convert the pre-0.2 format {key: [tmdb_id, ...]} to the current one."""
    if not isinstance(raw, dict):
        return {"synced": {}, "films": {}}
    if "synced" in raw or "films" in raw:
        raw.setdefault("synced", {})
        raw.setdefault("films", {})
        return raw
    synced = {}
    for key, ids in raw.items():
        if isinstance(ids, list):
            # Old state only ever stored movie IDs in practice
            synced[key] = [i if isinstance(i, str) else media_key("movie", i) for i in ids]
    return {"synced": synced, "films": {}}


def load_state():
    """Load sync state from disk."""
    path = Path(STATE_FILE)
    if path.exists():
        try:
            return _migrate_state(json.loads(path.read_text()))
        except Exception as e:
            logger.error("Failed to load state file %s: %s", STATE_FILE, e)
            backup = path.with_suffix(path.suffix + ".corrupt")
            try:
                path.replace(backup)
                logger.error("Moved unreadable state file to %s", backup)
            except OSError:
                pass
    return {"synced": {}, "films": {}}


def write_json_atomic(path, data):
    """Write JSON to a temp file then rename, so a crash never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(state):
    """Persist sync state to disk."""
    try:
        write_json_atomic(STATE_FILE, state)
    except Exception as e:
        logger.error("Failed to save state file: %s", e)


def update_state(fn):
    """
    Read-modify-write the state file under a lock. Syncs hold their own
    snapshot while they run and merge their results back through this,
    so concurrent syncs can't overwrite each other.
    """
    with _state_lock:
        state = load_state()
        fn(state)
        save_state(state)
        return state


def read_state():
    with _state_lock:
        return load_state()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config():
    """
    Load user mappings from config file.

    Config format:
    {
        "seerr_url": "http://localhost:5055",
        "seerr_api_key": "your-api-key",
        "sync_interval": 3600,
        "users": [
            {
                "letterboxd": "username",
                "seerr_user_id": 1
            }
        ]
    }

    Environment variables, when set, override config file values for
    seerr_url, seerr_api_key, and sync_interval.
    """
    config = {
        "seerr_url": SEERR_URL,
        "seerr_api_key": SEERR_API_KEY,
        "sync_interval": SYNC_INTERVAL,
        "users": [],
    }

    path = Path(CONFIG_FILE)
    if path.exists():
        try:
            file_config = json.loads(path.read_text())
        except Exception as e:
            logger.error("Failed to load config file %s: %s", CONFIG_FILE, e)
            sys.exit(1)
        if "SEERR_URL" not in os.environ and file_config.get("seerr_url"):
            config["seerr_url"] = file_config["seerr_url"].rstrip("/")
        if "SEERR_API_KEY" not in os.environ:
            config["seerr_api_key"] = file_config.get("seerr_api_key", "")
        if "SYNC_INTERVAL" not in os.environ and "sync_interval" in file_config:
            interval = file_config["sync_interval"]
            if not isinstance(interval, int) or interval < 0:
                logger.error("sync_interval in %s must be a non-negative integer", CONFIG_FILE)
                sys.exit(1)
            config["sync_interval"] = interval
        config["users"] = file_config.get("users", [])
    else:
        logger.warning("No config file found at %s, using environment variables only", CONFIG_FILE)

    return config


# ---------------------------------------------------------------------------
# Letterboxd watchlist scraping
# ---------------------------------------------------------------------------
def slug_to_title(slug):
    """'/film/the-godfather/' -> 'The Godfather'. Best effort, for display and search."""
    name = slug.strip("/").split("/")[-1]
    return name.replace("-", " ").title()


def fetch_watchlist_slugs(username, client=None):
    """
    Fetch the film slugs ("/film/slug/") on a user's watchlist, in order,
    without duplicates. Raises WatchlistFetchError on failure.
    """
    if not is_valid_username(username):
        raise WatchlistFetchError(f"Invalid Letterboxd username: {username!r}")

    client = client or make_client()
    slugs = []
    seen = set()
    page = 1

    logger.info("Fetching Letterboxd watchlist for '%s'", username)

    while True:
        url = f"https://letterboxd.com/{username}/watchlist/page/{page}/"
        try:
            resp = http_get(client, url, timeout=30)
        except requests.RequestException as e:
            logger.error("Request to %s failed: %s", url, e)
            raise WatchlistFetchError("Could not reach Letterboxd") from e

        if resp.status_code == 404:
            if page == 1:
                raise WatchlistFetchError(f"Letterboxd user '{username}' not found")
            break
        if resp.status_code == 403:
            raise WatchlistFetchError(
                "Letterboxd blocked the request (403). This is usually Cloudflare "
                "bot protection; try again later."
            )
        if not resp.ok:
            raise WatchlistFetchError(f"Letterboxd returned {resp.status_code} for page {page}")

        page_html = resp.text
        # Pattern: data-target-link="/film/movie-slug/"
        page_slugs = re.findall(r'data-target-link="(/film/[^"/]+/)"', page_html)

        if not page_slugs:
            break

        for slug in page_slugs:
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)

        # Check if there's a next page
        if 'class="next"' not in page_html:
            break

        page += 1
        time.sleep(0.5)  # Be polite

    logger.info("Found %d films in watchlist for '%s'", len(slugs), username)
    return slugs


def parse_film_page(page_html):
    """Extract TMDb ID, media type, title and year from a Letterboxd film page."""
    info = {"tmdb_id": None, "media_type": "movie", "title": None, "year": None}

    # Film pages carry the TMDb ID on the <body> element
    body_id = re.search(r'data-tmdb-id="(\d+)"', page_html)
    body_type = re.search(r'data-tmdb-type="(movie|tv)"', page_html)
    if body_id:
        info["tmdb_id"] = int(body_id.group(1))
        if body_type:
            info["media_type"] = body_type.group(1)
    else:
        # Fall back to the TMDb link on the page
        link = re.search(
            r'href="https?://(?:www\.)?themoviedb\.org/(movie|tv)/(\d+)', page_html
        )
        if link:
            info["media_type"] = link.group(1)
            info["tmdb_id"] = int(link.group(2))

    # Title from og:title. Format: "Movie Title (2024)" or just "Movie Title"
    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', page_html)
    if title_match:
        raw = html_lib.unescape(title_match.group(1)).strip()
        year_match = re.match(r"^(.+?)\s*\((\d{4})\)\s*$", raw)
        if year_match:
            info["title"] = year_match.group(1).strip()
            info["year"] = int(year_match.group(2))
        else:
            info["title"] = raw

    return info


def fetch_film_info(slug, client):
    """Fetch one film page. Returns parse_film_page() output, or None on failure."""
    url = f"https://letterboxd.com{slug}"
    try:
        resp = http_get(client, url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch film page %s: %s", url, e)
        return None
    return parse_film_page(resp.text)


def fetch_letterboxd_watchlist(username, film_cache=None, client=None):
    """
    Fetch a user's watchlist and resolve each film's TMDb ID.

    film_cache maps slug -> film info. Known slugs skip the film page fetch;
    newly resolved films are added to it. Raises WatchlistFetchError.
    Returns a list of dicts: [{letterboxd_url, slug, title, year, tmdb_id, media_type}, ...]
    """
    if film_cache is None:
        film_cache = {}
    client = client or make_client()
    slugs = fetch_watchlist_slugs(username, client)

    movies = []
    fetched = 0
    for slug in slugs:
        info = film_cache.get(slug)
        if not info:
            info = fetch_film_info(slug, client)
            fetched += 1
            time.sleep(0.3)  # Be polite
            if info and info["tmdb_id"]:
                film_cache[slug] = info
        movie = {
            "letterboxd_url": f"https://letterboxd.com{slug}",
            "slug": slug,
            "title": None,
            "year": None,
            "tmdb_id": None,
            "media_type": "movie",
        }
        if info:
            movie.update(info)
        movies.append(movie)

    logger.info(
        "Resolved %d films for '%s' (%d from cache, %d fetched)",
        len(movies), username, len(movies) - fetched, fetched,
    )
    return movies


def resolve_tmdb_id_via_seerr(title, year, seerr_url, api_key):
    """
    Fall back to Seerr's search to find a TMDb ID when the film page didn't
    give one. Returns (tmdb_id, media_type) or (None, None).

    Only accepts a result whose year is within one of the Letterboxd year,
    so a common title never resolves to a different film. Without a year,
    only an exact title match is accepted.
    """
    try:
        resp = requests.get(
            f"{seerr_url}/api/v1/search",
            params={"query": title, "page": 1, "language": "en"},
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        logger.warning("Seerr search failed for '%s': %s", title, e)
        return None, None

    for result in results:
        media_type = result.get("mediaType")
        if media_type not in ("movie", "tv"):
            continue
        if year:
            date = result.get("releaseDate") or result.get("firstAirDate") or ""
            if len(date) >= 4 and date[:4].isdigit() and abs(int(date[:4]) - year) <= 1:
                return result.get("id"), media_type
        else:
            name = result.get("title") or result.get("name") or ""
            if name.strip().lower() == title.strip().lower():
                return result.get("id"), media_type

    return None, None


# ---------------------------------------------------------------------------
# Seerr watchlist API
# ---------------------------------------------------------------------------
def add_to_seerr_watchlist(tmdb_id, title, seerr_url, api_key, seerr_user_id=None, media_type="movie"):
    """
    Add a movie or TV show to a user's Seerr watchlist.
    Uses the admin API key and optionally impersonates a specific user.
    """
    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    }
    # Impersonate the target user if specified
    if seerr_user_id:
        headers["X-Api-User"] = str(seerr_user_id)

    payload = {
        "tmdbId": tmdb_id,
        "mediaType": media_type,
        "title": title,
    }

    try:
        resp = requests.post(f"{seerr_url}/api/v1/watchlist", json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info("Added to watchlist: %s (TMDb %s: %d)", title, media_type, tmdb_id)
            return True
        elif resp.status_code == 409:
            logger.debug("Already in watchlist: %s (TMDb %s: %d)", title, media_type, tmdb_id)
            return True  # Already exists, still a success
        else:
            logger.error(
                "Failed to add '%s' (TMDb %s: %d) to watchlist: %d %s",
                title, media_type, tmdb_id, resp.status_code, resp.text[:200],
            )
            return False
    except requests.RequestException as e:
        logger.error("Request failed adding '%s' to watchlist: %s", title, e)
        return False


def get_seerr_watchlist(seerr_url, api_key, seerr_user_id):
    """Fetch existing watchlist keys ("movie:123") for a user to avoid duplicates."""
    url = f"{seerr_url}/api/v1/user/{seerr_user_id}/watchlist"
    headers = {"X-Api-Key": api_key}
    existing = set()

    try:
        page = 1
        while True:
            resp = requests.get(url, headers=headers, params={"page": page}, timeout=10)
            if not resp.ok:
                logger.warning("Failed to fetch watchlist for user %s: %d", seerr_user_id, resp.status_code)
                break
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            for item in results:
                tmdb_id = item.get("tmdbId")
                if tmdb_id:
                    existing.add(media_key(item.get("mediaType", "movie"), tmdb_id))
            if page >= data.get("pageInfo", {}).get("pages", 1):
                break
            page += 1
    except Exception as e:
        logger.warning("Failed to fetch existing watchlist: %s", e)

    return existing


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------
def sync_key(lb_username, seerr_user_id):
    return f"{lb_username}:{seerr_user_id or 'default'}"


def sync_watchlist(lb_username, seerr_user_id, seerr_url, api_key, dry_run=None):
    """
    Sync one Letterboxd watchlist to one Seerr user's watchlist.

    Returns {added, skipped, failed, total, failures}. Raises
    WatchlistFetchError if the watchlist can't be fetched.
    """
    if dry_run is None:
        dry_run = DRY_RUN

    snapshot = read_state()
    key = sync_key(lb_username, seerr_user_id)
    film_cache = dict(snapshot["films"])
    synced = set(snapshot["synced"].get(key, []))

    movies = fetch_letterboxd_watchlist(lb_username, film_cache)

    # Also check the existing Seerr watchlist to avoid duplicates
    if seerr_user_id:
        synced.update(get_seerr_watchlist(seerr_url, api_key, seerr_user_id))

    added = skipped = failed = 0
    failures = []
    newly_synced = set()

    for movie in movies:
        tmdb_id = movie.get("tmdb_id")
        media_type = movie.get("media_type", "movie")
        title = movie.get("title")

        # Fall back to Seerr search if the film page didn't give an ID
        if not tmdb_id:
            query = title or slug_to_title(movie["slug"])
            tmdb_id, found_type = resolve_tmdb_id_via_seerr(query, movie.get("year"), seerr_url, api_key)
            if tmdb_id:
                media_type = found_type

        display = title or slug_to_title(movie["slug"])

        if not tmdb_id:
            logger.warning("Could not resolve TMDb ID for: %s", movie["letterboxd_url"])
            failed += 1
            failures.append({"title": display, "reason": "Could not find TMDb ID"})
            continue

        mkey = media_key(media_type, tmdb_id)
        if mkey in synced:
            skipped += 1
            continue

        if dry_run:
            logger.info("[DRY RUN] Would add to watchlist: %s (TMDb %s: %d)", display, media_type, tmdb_id)
            added += 1
            continue

        if add_to_seerr_watchlist(tmdb_id, display, seerr_url, api_key, seerr_user_id, media_type):
            synced.add(mkey)
            newly_synced.add(mkey)
            added += 1
        else:
            failed += 1
            failures.append({"title": display, "reason": "Seerr rejected the request"})

        # Small delay between API calls
        time.sleep(0.3)

    def commit(state):
        state["films"].update(film_cache)
        if not dry_run:
            merged = set(state["synced"].get(key, [])) | synced | newly_synced
            state["synced"][key] = sorted(merged)

    update_state(commit)

    logger.info(
        "Sync complete for '%s': %d added, %d skipped, %d failed%s",
        lb_username, added, skipped, failed, " (dry run)" if dry_run else "",
    )
    return {
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "total": len(movies),
        "failures": failures,
        "dry_run": dry_run,
    }


def forget_user(lb_username, seerr_user_id):
    """Drop a user's synced IDs from state, e.g. after they unlink."""
    key = sync_key(lb_username, seerr_user_id)
    update_state(lambda state: state["synced"].pop(key, None))


def run_sync(config):
    """Run a single sync cycle for all configured users."""
    seerr_url = config["seerr_url"]
    api_key = config["seerr_api_key"]

    if not api_key:
        logger.error("No Seerr API key configured. Set SEERR_API_KEY or add to config.json.")
        return

    logger.info("Starting sync cycle (%d users)", len(config["users"]))

    for user_config in config["users"]:
        lb_username = user_config.get("letterboxd", "")
        if not lb_username:
            logger.warning("Skipping user config with no letterboxd username")
            continue
        seerr_user_id = user_config.get("seerr_user_id")
        logger.info("Syncing %s -> Seerr user %s", lb_username, seerr_user_id or "default")
        try:
            sync_watchlist(lb_username, seerr_user_id, seerr_url, api_key)
        except WatchlistFetchError as e:
            logger.error("Could not fetch watchlist for %s: %s", lb_username, e)
        except Exception as e:
            logger.exception("Error syncing user %s: %s", lb_username, e)

    logger.info("Sync cycle complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logger.info("Letterboxderr starting up")

    config = load_config()

    if not config["users"]:
        logger.error("No users configured. Add users to %s or set environment variables.", CONFIG_FILE)
        sys.exit(1)

    if not config.get("seerr_api_key"):
        logger.error("No Seerr API key. Set SEERR_API_KEY env var or add to config file.")
        sys.exit(1)

    interval = config["sync_interval"]
    logger.info(
        "Config: seerr=%s, users=%d, interval=%ds, dry_run=%s",
        config["seerr_url"], len(config["users"]), interval, DRY_RUN,
    )

    # Run once if the interval is 0 or --once is passed
    if "--once" in sys.argv or interval == 0:
        run_sync(config)
        return

    while True:
        run_sync(config)
        logger.info("Next sync in %d seconds", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
