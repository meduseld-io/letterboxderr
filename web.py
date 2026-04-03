#!/usr/bin/env python3
"""
Letterboxderr Web UI - A self-hosted web interface for managing
Letterboxd-to-Seerr watchlist syncing.

Users authenticate with their Seerr credentials, link their Letterboxd
username, and trigger syncs from the browser.
"""

import os
import json
import threading
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, request, jsonify, send_from_directory, make_response

import requests as http_requests

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper()
except ImportError:
    _scraper = None

from letterboxderr import (
    fetch_letterboxd_watchlist,
    resolve_tmdb_id_via_seerr,
    add_to_seerr_watchlist,
    get_seerr_watchlist,
    load_state,
    save_state,
)

logger = logging.getLogger("letterboxderr.web")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEERR_URL = os.environ.get("SEERR_URL", "http://localhost:5055").rstrip("/")
SEERR_API_KEY = os.environ.get("SEERR_API_KEY", "")
SEERR_PUBLIC_URL = os.environ.get("SEERR_PUBLIC_URL", "").rstrip("/")
WEB_PORT = int(os.environ.get("WEB_PORT", "8484"))
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
USERS_FILE = os.environ.get("USERS_FILE", "users.json")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "3600"))


# ---------------------------------------------------------------------------
# User storage - persists linked Letterboxd usernames per Seerr user
# ---------------------------------------------------------------------------
def load_users():
    """Load user mappings from disk. Returns {seerr_user_id: {letterboxd, ...}}."""
    path = Path(USERS_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            logger.error("Failed to load users file: %s", e)
    return {}


def save_users(users):
    """Persist user mappings to disk."""
    try:
        Path(USERS_FILE).write_text(json.dumps(users, indent=2))
    except Exception as e:
        logger.error("Failed to save users file: %s", e)


# ---------------------------------------------------------------------------
# Seerr auth helper
# ---------------------------------------------------------------------------
def authenticate_seerr_user(cookie_value):
    """
    Validate a Seerr session cookie by calling /api/v1/auth/me.
    Returns the Seerr user dict or None.
    """
    try:
        resp = http_requests.get(
            f"{SEERR_URL}/api/v1/auth/me",
            headers={"Cookie": f"connect.sid={cookie_value}"},
            timeout=10,
        )
        if resp.ok:
            return resp.json()
    except Exception as e:
        logger.error("Seerr auth check failed: %s", e)
    return None


def get_seerr_user_from_request():
    """Extract and validate Seerr user from the request cookie."""
    cookie = request.cookies.get("connect.sid")
    if not cookie:
        return None
    return authenticate_seerr_user(cookie)


# ---------------------------------------------------------------------------
# Background sync thread
# ---------------------------------------------------------------------------
sync_status = {}  # {seerr_user_id: {status, last_sync, added, skipped, failed}}
sync_lock = threading.Lock()

# Watchlist cache - stores scraped results so sync can reuse preview data
# {username: {"movies": [...], "fetched_at": timestamp}}
_watchlist_cache = {}
_CACHE_TTL = 300  # 5 minutes


def _get_watchlist_cached(username):
    """Fetch watchlist with caching. Returns movie list."""
    now = time.time()
    cached = _watchlist_cache.get(username)
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL:
        logger.info("Using cached watchlist for '%s' (%d movies)", username, len(cached["movies"]))
        return cached["movies"]

    movies = fetch_letterboxd_watchlist(username)
    _watchlist_cache[username] = {"movies": movies, "fetched_at": now}
    return movies


def background_sync_all():
    """Run sync for all registered users."""
    users = load_users()
    if not users:
        return

    state = load_state()

    for seerr_user_id, user_data in users.items():
        lb_username = user_data.get("letterboxd")
        if not lb_username:
            continue

        with sync_lock:
            sync_status[seerr_user_id] = {"status": "syncing", "last_sync": None}

        try:
            result = _sync_single_user(lb_username, int(seerr_user_id), state)
            with sync_lock:
                sync_status[seerr_user_id] = {
                    "status": "complete",
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                    **result,
                }
        except Exception as e:
            logger.error("Background sync failed for %s: %s", lb_username, e)
            with sync_lock:
                sync_status[seerr_user_id] = {
                    "status": "error",
                    "error": str(e),
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                }

    save_state(state)


def _sync_single_user(lb_username, seerr_user_id, state):
    """Sync one user, returns {added, skipped, failed, total, failures}."""
    movies = _get_watchlist_cached(lb_username)
    if not movies:
        return {"added": 0, "skipped": 0, "failed": 0, "total": 0, "failures": []}

    synced_key = f"{lb_username}:{seerr_user_id}"
    synced_ids = set(state.get(synced_key, []))
    existing_ids = get_seerr_watchlist(SEERR_URL, SEERR_API_KEY, seerr_user_id)
    synced_ids.update(existing_ids)

    added = 0
    skipped = 0
    failed = 0
    failures = []

    for movie in movies:
        tmdb_id = movie.get("tmdb_id")

        # Fall back to Seerr search if scraping didn't find it
        if not tmdb_id and movie.get("title"):
            tmdb_id = resolve_tmdb_id_via_seerr(
                movie["title"], movie.get("year"), SEERR_URL, SEERR_API_KEY,
            )

        if not tmdb_id:
            failed += 1
            failures.append({
                "title": movie.get("title") or movie.get("letterboxd_url", "Unknown"),
                "reason": "Could not find TMDb ID",
            })
            continue

        if tmdb_id in synced_ids:
            skipped += 1
            continue

        title = movie.get("title") or f"TMDb:{tmdb_id}"
        media_type = movie.get("media_type", "movie")
        success = add_to_seerr_watchlist(
            tmdb_id, title, SEERR_URL, SEERR_API_KEY, seerr_user_id, media_type,
        )
        if success:
            synced_ids.add(tmdb_id)
            added += 1
        else:
            failed += 1
            failures.append({
                "title": title,
                "reason": "Seerr API rejected the request",
            })

        time.sleep(0.3)

    state[synced_key] = list(synced_ids)
    return {"added": added, "skipped": skipped, "failed": failed, "total": len(movies), "failures": failures}


def sync_loop():
    """Background thread that syncs all users on an interval."""
    # Wait for the first interval before syncing (don't sync on startup)
    time.sleep(SYNC_INTERVAL)
    while True:
        try:
            background_sync_all()
        except Exception as e:
            logger.error("Sync loop error: %s", e)
        time.sleep(SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static")


@app.route("/")
def index():
    """Serve the main UI page."""
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def api_health():
    """Health check endpoint."""
    users = load_users()
    return jsonify({"status": "ok", "users": len(users)})


@app.route("/api/me")
def api_me():
    """Get current user info from Seerr session."""
    user = get_seerr_user_from_request()
    if not user:
        return jsonify({"authenticated": False}), 401

    seerr_user_id = str(user.get("id", ""))
    users = load_users()
    user_data = users.get(seerr_user_id, {})

    return jsonify({
        "authenticated": True,
        "id": user.get("id"),
        "displayName": user.get("displayName", user.get("username", "")),
        "avatar": user.get("avatar"),
        "letterboxd": user_data.get("letterboxd", ""),
        "sync_status": sync_status.get(seerr_user_id, {}),
        "seerr_url": SEERR_PUBLIC_URL or SEERR_URL,
    })


@app.route("/api/link", methods=["POST"])
def api_link():
    """Link or update a Letterboxd username for the current user."""
    user = get_seerr_user_from_request()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    if not data or not data.get("letterboxd"):
        return jsonify({"error": "letterboxd username required"}), 400

    lb_username = data["letterboxd"].strip().lower()

    # Validate the Letterboxd username exists by checking the watchlist page
    try:
        client = _scraper if _scraper else http_requests
        resp = client.get(
            f"https://letterboxd.com/{lb_username}/watchlist/",
            timeout=10,
        )
        if resp.status_code == 404:
            return jsonify({"error": "Letterboxd user not found"}), 404
    except Exception as e:
        logger.error("Failed to validate Letterboxd user '%s': %s", lb_username, e)
        return jsonify({"error": "Could not validate Letterboxd username"}), 502

    seerr_user_id = str(user["id"])
    users = load_users()
    users[seerr_user_id] = {
        "letterboxd": lb_username,
        "seerr_display_name": user.get("displayName", user.get("username", "")),
        "linked_at": datetime.now(timezone.utc).isoformat(),
    }
    save_users(users)

    logger.info(
        "User %s (Seerr #%s) linked Letterboxd: %s",
        user.get("displayName", "?"), seerr_user_id, lb_username,
    )

    return jsonify({"ok": True, "letterboxd": lb_username})


@app.route("/api/unlink", methods=["POST"])
def api_unlink():
    """Remove the Letterboxd link for the current user."""
    user = get_seerr_user_from_request()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    seerr_user_id = str(user["id"])
    users = load_users()
    if seerr_user_id in users:
        del users[seerr_user_id]
        save_users(users)
        logger.info("User %s unlinked Letterboxd", user.get("displayName", "?"))

    return jsonify({"ok": True})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Trigger an immediate sync for the current user."""
    user = get_seerr_user_from_request()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    seerr_user_id = str(user["id"])
    users = load_users()
    user_data = users.get(seerr_user_id, {})

    if not user_data.get("letterboxd"):
        return jsonify({"error": "No Letterboxd username linked"}), 400

    # Check if already syncing
    with sync_lock:
        current = sync_status.get(seerr_user_id, {})
        if current.get("status") == "syncing":
            return jsonify({"error": "Sync already in progress"}), 429

        sync_status[seerr_user_id] = {"status": "syncing"}

    # Run sync in background thread
    def do_sync():
        state = load_state()
        try:
            result = _sync_single_user(
                user_data["letterboxd"], int(seerr_user_id), state,
            )
            save_state(state)
            with sync_lock:
                sync_status[seerr_user_id] = {
                    "status": "complete",
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                    **result,
                }
        except Exception as e:
            logger.error("Manual sync failed for %s: %s", user_data["letterboxd"], e)
            with sync_lock:
                sync_status[seerr_user_id] = {
                    "status": "error",
                    "error": str(e),
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                }

    threading.Thread(target=do_sync, daemon=True).start()
    return jsonify({"ok": True, "status": "syncing"})


@app.route("/api/status")
def api_status():
    """Get sync status for the current user."""
    user = get_seerr_user_from_request()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    seerr_user_id = str(user["id"])
    with sync_lock:
        status = sync_status.get(seerr_user_id, {})

    return jsonify(status)


@app.route("/api/preview")
def api_preview():
    """Preview a Letterboxd watchlist without syncing."""
    username = request.args.get("username", "").strip().lower()
    if not username:
        return jsonify({"error": "username required"}), 400

    movies = _get_watchlist_cached(username)
    return jsonify({"movies": movies, "count": len(movies)})


@app.route("/api/login", methods=["POST"])
def api_login():
    """
    Authenticate with Seerr using Jellyfin credentials.
    Proxies the login to Seerr and returns the session cookie.
    """
    data = request.get_json()
    if not data or not data.get("username") or not data.get("password"):
        return jsonify({"error": "username and password required"}), 400

    try:
        resp = http_requests.post(
            f"{SEERR_URL}/api/v1/auth/jellyfin",
            json={"username": data["username"], "password": data["password"]},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except Exception as e:
        logger.error("Seerr login proxy failed: %s", e)
        return jsonify({"error": "Could not reach Seerr"}), 502

    if not resp.ok:
        return jsonify({"error": "Invalid credentials"}), 401

    # Extract connect.sid cookie from Seerr response
    connect_sid = None
    for cookie in resp.cookies:
        if cookie.name == "connect.sid":
            connect_sid = cookie.value
            break

    if not connect_sid:
        return jsonify({"error": "Login succeeded but no session cookie returned"}), 500

    # Set the cookie on our response so subsequent requests include it
    response = make_response(jsonify({"ok": True, "user": resp.json()}))
    response.set_cookie(
        "connect.sid",
        connect_sid,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return response


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """Clear the session cookie."""
    response = make_response(jsonify({"ok": True}))
    response.set_cookie(
        "connect.sid",
        "",
        expires=0,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not SEERR_API_KEY:
        logger.error("SEERR_API_KEY is required. Set it as an environment variable.")
        return

    logger.info("Letterboxderr Web UI starting on %s:%d", WEB_HOST, WEB_PORT)
    logger.info("Seerr URL: %s", SEERR_URL)

    # Clear any stale sync status from previous runs
    sync_status.clear()

    # Start background sync thread
    if SYNC_INTERVAL > 0:
        logger.info("Background sync enabled (every %ds)", SYNC_INTERVAL)
        sync_thread = threading.Thread(target=sync_loop, daemon=True)
        sync_thread.start()

    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)


if __name__ == "__main__":
    main()
