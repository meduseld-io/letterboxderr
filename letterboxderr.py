#!/usr/bin/env python3
"""
Letterboxderr - Sync Letterboxd watchlists to Seerr/Jellyseerr/Overseerr

Fetches a user's Letterboxd watchlist by scraping the watchlist page,
resolves TMDb IDs, and adds movies and TV shows to their Seerr watchlist.
"""

import os
import sys
import json
import re
import time
import logging
from pathlib import Path

import requests

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper()
except ImportError:
    _scraper = None

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
SEERR_URL = os.environ.get("SEERR_URL", "http://localhost:5055").rstrip("/")
SEERR_API_KEY = os.environ.get("SEERR_API_KEY", "")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "3600"))  # seconds
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


# ---------------------------------------------------------------------------
# State persistence - tracks which movies have already been synced
# ---------------------------------------------------------------------------
def load_state():
    """Load sync state from disk. Returns {letterboxd_user: [tmdb_id, ...]}."""
    path = Path(STATE_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            logger.error("Failed to load state file: %s", e)
    return {}


def save_state(state):
    """Persist sync state to disk."""
    try:
        Path(STATE_FILE).write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.error("Failed to save state file: %s", e)


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

    Environment variables override config file values for seerr_url,
    seerr_api_key, and sync_interval.
    """
    config = {"seerr_url": SEERR_URL, "seerr_api_key": SEERR_API_KEY, "users": []}

    path = Path(CONFIG_FILE)
    if path.exists():
        try:
            file_config = json.loads(path.read_text())
            # File values are defaults, env vars override
            if not SEERR_URL or SEERR_URL == "http://localhost:5055":
                config["seerr_url"] = file_config.get("seerr_url", config["seerr_url"])
            if not SEERR_API_KEY:
                config["seerr_api_key"] = file_config.get("seerr_api_key", "")
            config["users"] = file_config.get("users", [])
            if "sync_interval" in file_config and SYNC_INTERVAL == 3600:
                config["sync_interval"] = file_config["sync_interval"]
            else:
                config["sync_interval"] = SYNC_INTERVAL
        except Exception as e:
            logger.error("Failed to load config file %s: %s", CONFIG_FILE, e)
            sys.exit(1)
    else:
        logger.warning("No config file found at %s, using environment variables only", CONFIG_FILE)
        config["sync_interval"] = SYNC_INTERVAL

    return config


# ---------------------------------------------------------------------------
# Letterboxd watchlist scraping
# ---------------------------------------------------------------------------
def fetch_letterboxd_watchlist(username):
    """
    Fetch a user's Letterboxd watchlist by scraping the HTML pages.
    Letterboxd removed the watchlist RSS feed, so we scrape the watchlist
    pages directly and extract film URLs from poster elements.
    Returns a list of dicts: [{title, year, tmdb_id, letterboxd_url}, ...]
    """
    client = _scraper if _scraper else requests
    movies = []
    page = 1

    logger.info("Fetching Letterboxd watchlist for '%s'", username)

    while True:
        url = f"https://letterboxd.com/{username}/watchlist/page/{page}/"
        try:
            resp = client.get(url, timeout=30)
            if resp.status_code == 404:
                if page == 1:
                    logger.error("Watchlist not found for '%s' (404)", username)
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to fetch watchlist page %d for '%s': %s", page, username, e)
            break

        html = resp.text

        # Extract film slugs from poster elements
        # Pattern: data-target-link="/film/movie-slug/"
        film_slugs = re.findall(r'data-target-link="(/film/[^"]+/)"', html)

        if not film_slugs:
            if page == 1:
                logger.info("No movies found in watchlist for '%s'", username)
            break

        for slug in film_slugs:
            film_url = f"https://letterboxd.com{slug}"
            movies.append({
                "letterboxd_url": film_url,
                "title": None,
                "year": None,
                "tmdb_id": None,
                "media_type": "movie",  # Default, updated by _enrich_from_film_page
            })

        # Check if there's a next page
        if 'class="next"' not in html:
            break

        page += 1
        time.sleep(0.5)  # Be polite

    logger.info("Found %d movies in watchlist for '%s'", len(movies), username)

    # Resolve TMDb IDs and titles from each film page
    for movie in movies:
        try:
            _enrich_from_film_page(movie, client)
        except Exception as e:
            logger.warning("Failed to enrich %s: %s", movie["letterboxd_url"], e)
        time.sleep(0.3)  # Be polite

    return movies


def _enrich_from_film_page(movie, client):
    """Scrape a Letterboxd film page to get TMDb ID, title, year, and media type."""
    try:
        resp = client.get(movie["letterboxd_url"], timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch film page %s: %s", movie["letterboxd_url"], e)
        return

    html = resp.text

    # Extract TMDb movie ID
    tmdb_match = re.search(
        r'href="https?://(?:www\.)?themoviedb\.org/movie/(\d+)', html
    )
    if tmdb_match:
        movie["tmdb_id"] = int(tmdb_match.group(1))
        movie["media_type"] = "movie"
    else:
        # Check for TV show TMDb link
        tv_match = re.search(
            r'href="https?://(?:www\.)?themoviedb\.org/tv/(\d+)', html
        )
        if tv_match:
            movie["tmdb_id"] = int(tv_match.group(1))
            movie["media_type"] = "tv"

    # Extract title from og:title meta tag
    # Format: "Movie Title (2024)" or just "Movie Title"
    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if title_match:
        raw = title_match.group(1).strip()
        # Try to split "Title (Year)" format
        year_match = re.match(r'^(.+?)\s*\((\d{4})\)\s*$', raw)
        if year_match:
            movie["title"] = year_match.group(1).strip()
            movie["year"] = int(year_match.group(2))
        else:
            movie["title"] = raw


def resolve_tmdb_id_via_seerr(title, year, seerr_url, api_key):
    """
    Fall back to searching Seerr's own search endpoint to find the TMDb ID.
    This avoids scraping Letterboxd pages when possible.
    """
    try:
        search_url = f"{seerr_url}/api/v1/search"
        params = {"query": title, "page": 1, "language": "en"}
        headers = {"X-Api-Key": api_key}
        resp = requests.get(search_url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        for result in data.get("results", []):
            if result.get("mediaType") != "movie":
                continue
            # Match by year if available
            release_date = result.get("releaseDate", "")
            if year and release_date:
                result_year = int(release_date[:4]) if len(release_date) >= 4 else None
                if result_year and abs(result_year - year) <= 1:
                    return result.get("id")
            elif not year:
                # No year to match, take first movie result
                return result.get("id")

        # If year matching failed, try first movie result
        for result in data.get("results", []):
            if result.get("mediaType") == "movie":
                return result.get("id")

    except Exception as e:
        logger.warning("Seerr search failed for '%s': %s", title, e)

    return None


# ---------------------------------------------------------------------------
# Seerr watchlist API
# ---------------------------------------------------------------------------
def add_to_seerr_watchlist(tmdb_id, title, seerr_url, api_key, seerr_user_id=None, media_type="movie"):
    """
    Add a movie or TV show to a user's Seerr watchlist.
    Uses the admin API key and optionally impersonates a specific user.
    """
    url = f"{seerr_url}/api/v1/watchlist"
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

    if DRY_RUN:
        logger.info("[DRY RUN] Would add to watchlist: %s (TMDb: %d)", title, tmdb_id)
        return True

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info("Added to watchlist: %s (TMDb: %d)", title, tmdb_id)
            return True
        elif resp.status_code == 409:
            logger.debug("Already in watchlist: %s (TMDb: %d)", title, tmdb_id)
            return True  # Already exists, still a success
        else:
            logger.error(
                "Failed to add '%s' (TMDb: %d) to watchlist: %d %s",
                title, tmdb_id, resp.status_code, resp.text[:200],
            )
            return False
    except requests.RequestException as e:
        logger.error("Request failed adding '%s' to watchlist: %s", title, e)
        return False


def get_seerr_watchlist(seerr_url, api_key, seerr_user_id):
    """Fetch existing watchlist for a user to avoid duplicates."""
    url = f"{seerr_url}/api/v1/user/{seerr_user_id}/watchlist"
    headers = {"X-Api-Key": api_key}
    existing_ids = set()

    try:
        page = 1
        while True:
            resp = requests.get(
                url, headers=headers, params={"page": page}, timeout=10,
            )
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
                    existing_ids.add(tmdb_id)
            # Check if there are more pages
            page_info = data.get("pageInfo", {})
            if page >= page_info.get("pages", 1):
                break
            page += 1
    except Exception as e:
        logger.warning("Failed to fetch existing watchlist: %s", e)

    return existing_ids


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------
def sync_user(user_config, seerr_url, api_key, state):
    """Sync a single user's Letterboxd watchlist to Seerr."""
    lb_username = user_config.get("letterboxd", "")
    seerr_user_id = user_config.get("seerr_user_id")

    if not lb_username:
        logger.warning("Skipping user config with no letterboxd username")
        return

    logger.info("Syncing %s -> Seerr user %s", lb_username, seerr_user_id or "default")

    # Fetch Letterboxd watchlist
    movies = fetch_letterboxd_watchlist(lb_username)
    if not movies:
        logger.info("No movies found for '%s', skipping", lb_username)
        return

    # Load previously synced TMDb IDs for this user
    synced_key = f"{lb_username}:{seerr_user_id or 'default'}"
    synced_ids = set(state.get(synced_key, []))

    # Also fetch existing Seerr watchlist to avoid duplicates
    if seerr_user_id:
        existing_ids = get_seerr_watchlist(seerr_url, api_key, seerr_user_id)
        synced_ids.update(existing_ids)

    added = 0
    skipped = 0
    failed = 0

    for movie in movies:
        # Use TMDb ID from film page scraping if available
        tmdb_id = movie.get("tmdb_id")

        # Fall back to Seerr search if scraping didn't find it
        if not tmdb_id and movie.get("title"):
            tmdb_id = resolve_tmdb_id_via_seerr(
                movie["title"], movie.get("year"), seerr_url, api_key,
            )

        if not tmdb_id:
            logger.warning(
                "Could not resolve TMDb ID for: %s (%s)",
                movie.get("title", movie.get("letterboxd_url", "?")),
                movie.get("year"),
            )
            failed += 1
            continue

        # Skip if already synced
        if tmdb_id in synced_ids:
            skipped += 1
            continue

        # Add to Seerr watchlist
        title = movie.get("title") or f"TMDb:{tmdb_id}"
        media_type = movie.get("media_type", "movie")
        success = add_to_seerr_watchlist(
            tmdb_id, title, seerr_url, api_key, seerr_user_id, media_type,
        )

        if success:
            synced_ids.add(tmdb_id)
            added += 1
        else:
            failed += 1

        # Small delay between API calls
        time.sleep(0.3)

    # Update state
    state[synced_key] = list(synced_ids)

    logger.info(
        "Sync complete for '%s': %d added, %d skipped, %d failed",
        lb_username, added, skipped, failed,
    )


def run_sync(config, state):
    """Run a single sync cycle for all configured users."""
    seerr_url = config["seerr_url"]
    api_key = config["seerr_api_key"]

    if not api_key:
        logger.error("No Seerr API key configured. Set SEERR_API_KEY or add to config.json.")
        return

    logger.info("Starting sync cycle (%d users)", len(config["users"]))

    for user_config in config["users"]:
        try:
            sync_user(user_config, seerr_url, api_key, state)
        except Exception as e:
            logger.error("Error syncing user %s: %s", user_config.get("letterboxd", "?"), e)

    save_state(state)
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

    logger.info(
        "Config: seerr=%s, users=%d, interval=%ds, dry_run=%s",
        config["seerr_url"],
        len(config["users"]),
        config.get("sync_interval", SYNC_INTERVAL),
        DRY_RUN,
    )

    state = load_state()

    # Run once if SYNC_INTERVAL is 0 or --once flag
    once = "--once" in sys.argv or config.get("sync_interval", SYNC_INTERVAL) == 0

    if once:
        run_sync(config, state)
        return

    # Run on a loop
    while True:
        run_sync(config, state)
        interval = config.get("sync_interval", SYNC_INTERVAL)
        logger.info("Next sync in %d seconds", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
