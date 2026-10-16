#!/usr/bin/env python3
"""
Letterboxderr Web UI - A self-hosted web interface for managing
Letterboxd-to-Seerr watchlist syncing.

Users authenticate with their Seerr credentials, link their Letterboxd
username, and trigger syncs from the browser.
"""

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

import requests as http_requests
from flask import Flask, g, jsonify, make_response, request, send_from_directory

from letterboxderr import (
    DRY_RUN,
    WatchlistFetchError,
    env_int,
    fetch_watchlist_slugs,
    forget_user,
    is_valid_username,
    make_client,
    read_state,
    slug_to_title,
    sync_watchlist,
    write_json_atomic,
)

logger = logging.getLogger("letterboxderr.web")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEERR_URL = os.environ.get("SEERR_URL", "http://localhost:5055").rstrip("/")
SEERR_API_KEY = os.environ.get("SEERR_API_KEY", "")
SEERR_PUBLIC_URL = os.environ.get("SEERR_PUBLIC_URL", "").rstrip("/")
WEB_PORT = env_int("WEB_PORT", 8484, minimum=1)
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
USERS_FILE = os.environ.get("USERS_FILE", "users.json")
SYNC_INTERVAL = env_int("SYNC_INTERVAL", 3600)
FIRST_SYNC_DELAY = env_int("FIRST_SYNC_DELAY", 60)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"


class SeerrUnavailable(Exception):
    """Seerr couldn't be reached, as opposed to rejecting the user."""


# ---------------------------------------------------------------------------
# User storage - persists linked Letterboxd usernames and last sync result
# per Seerr user: {seerr_user_id: {letterboxd, seerr_display_name, linked_at, last_result}}
# ---------------------------------------------------------------------------
_users_lock = threading.Lock()


def load_users():
    """Load user mappings from disk."""
    path = Path(USERS_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            logger.error("Failed to load users file: %s", e)
    return {}


def update_users(fn):
    """Read-modify-write the users file under a lock."""
    with _users_lock:
        users = load_users()
        fn(users)
        try:
            write_json_atomic(USERS_FILE, users)
        except Exception as e:
            logger.error("Failed to save users file: %s", e)
        return users


# ---------------------------------------------------------------------------
# Seerr auth helpers
# ---------------------------------------------------------------------------
def authenticate_seerr_user(cookie_value):
    """
    Validate a Seerr session cookie by calling /api/v1/auth/me.
    Returns the Seerr user dict, or None if the session isn't valid.
    Raises SeerrUnavailable if Seerr can't be reached.
    """
    try:
        resp = http_requests.get(
            f"{SEERR_URL}/api/v1/auth/me",
            headers={"Cookie": f"connect.sid={cookie_value}"},
            timeout=10,
        )
    except http_requests.RequestException as e:
        logger.error("Seerr auth check failed: %s", e)
        raise SeerrUnavailable() from e
    if resp.status_code >= 500:
        raise SeerrUnavailable()
    if resp.ok:
        return resp.json()
    return None


def require_user(view):
    """Resolve the Seerr user from the session cookie into g.user, or return 401/503."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        cookie = request.cookies.get("connect.sid")
        if not cookie:
            return jsonify({"authenticated": False, "error": "Not authenticated"}), 401
        try:
            user = authenticate_seerr_user(cookie)
        except SeerrUnavailable:
            return jsonify({"error": "Seerr is unreachable"}), 503
        if not user:
            return jsonify({"authenticated": False, "error": "Not authenticated"}), 401
        g.user = user
        g.seerr_user_id = str(user.get("id", ""))
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
sync_status = {}  # {seerr_user_id: {status, last_sync, added, skipped, failed, ...}}
sync_lock = threading.Lock()


def _now():
    return datetime.now(UTC).isoformat()


def get_status(seerr_user_id):
    """Live status if a sync ran since startup, else the last persisted result."""
    with sync_lock:
        status = sync_status.get(seerr_user_id)
    if status:
        return status
    return load_users().get(seerr_user_id, {}).get("last_result", {})


def _claim_sync(seerr_user_id):
    """Mark a user as syncing. Returns False if a sync is already running."""
    with sync_lock:
        if sync_status.get(seerr_user_id, {}).get("status") == "syncing":
            return False
        sync_status[seerr_user_id] = {"status": "syncing"}
        return True


def _run_user_sync(seerr_user_id, lb_username):
    """Sync one user (already claimed) and record the result."""
    try:
        result = sync_watchlist(lb_username, int(seerr_user_id), SEERR_URL, SEERR_API_KEY)
        status = {"status": "complete", "last_sync": _now(), **result}
    except WatchlistFetchError as e:
        logger.error("Sync failed for %s: %s", lb_username, e)
        status = {"status": "error", "error": str(e), "last_sync": _now()}
    except Exception as e:
        logger.exception("Sync failed for %s", lb_username)
        status = {"status": "error", "error": f"Unexpected error: {e}", "last_sync": _now()}

    with sync_lock:
        sync_status[seerr_user_id] = status

    def persist(users):
        # Only record it if the user is still linked to the same username
        if users.get(seerr_user_id, {}).get("letterboxd") == lb_username:
            users[seerr_user_id]["last_result"] = status
    update_users(persist)


def background_sync_all():
    """Run sync for all registered users, skipping any already syncing."""
    for seerr_user_id, user_data in load_users().items():
        lb_username = user_data.get("letterboxd")
        if not lb_username:
            continue
        if not _claim_sync(seerr_user_id):
            logger.info("Skipping %s, a sync is already running", lb_username)
            continue
        _run_user_sync(seerr_user_id, lb_username)


def sync_loop():
    """Background thread that syncs all users on an interval."""
    time.sleep(FIRST_SYNC_DELAY)
    while True:
        try:
            background_sync_all()
        except Exception:
            logger.exception("Sync loop error")
        time.sleep(SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# Preview cache - watchlist slugs only, bounded, short-lived
# ---------------------------------------------------------------------------
_PREVIEW_TTL = 300  # 5 minutes
_PREVIEW_MAX = 50
_preview_cache = OrderedDict()  # {username: (fetched_at, slugs)}
_preview_lock = threading.Lock()


def _get_slugs_cached(username):
    now = time.time()
    with _preview_lock:
        cached = _preview_cache.get(username)
        if cached and now - cached[0] < _PREVIEW_TTL:
            _preview_cache.move_to_end(username)
            return cached[1]

    slugs = fetch_watchlist_slugs(username, make_client())

    with _preview_lock:
        _preview_cache[username] = (now, slugs)
        _preview_cache.move_to_end(username)
        while len(_preview_cache) > _PREVIEW_MAX:
            _preview_cache.popitem(last=False)
    return slugs


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
    return jsonify({"status": "ok", "users": len(load_users())})


@app.route("/api/me")
@require_user
def api_me():
    """Get current user info from Seerr session."""
    user = g.user
    user_data = load_users().get(g.seerr_user_id, {})

    return jsonify({
        "authenticated": True,
        "id": user.get("id"),
        "displayName": user.get("displayName") or user.get("username") or user.get("email", ""),
        "avatar": user.get("avatar"),
        "letterboxd": user_data.get("letterboxd", ""),
        "sync_status": get_status(g.seerr_user_id) if user_data else {},
        "seerr_url": SEERR_PUBLIC_URL or SEERR_URL,
        "dry_run": DRY_RUN,
    })


@app.route("/api/link", methods=["POST"])
@require_user
def api_link():
    """Link or update a Letterboxd username for the current user."""
    data = request.get_json(silent=True)
    if not data or not data.get("letterboxd"):
        return jsonify({"error": "letterboxd username required"}), 400

    lb_username = str(data["letterboxd"]).strip().lower()
    if not is_valid_username(lb_username):
        return jsonify({"error": "Letterboxd usernames only contain letters, numbers and underscores"}), 400

    # Validate the Letterboxd username exists by checking the watchlist page
    try:
        resp = make_client().get(f"https://letterboxd.com/{lb_username}/watchlist/", timeout=10)
    except Exception as e:
        logger.error("Failed to validate Letterboxd user '%s': %s", lb_username, e)
        return jsonify({"error": "Could not reach Letterboxd to check the username"}), 502
    if resp.status_code == 404:
        return jsonify({"error": "Letterboxd user not found"}), 404
    if not resp.ok:
        return jsonify({"error": f"Letterboxd returned {resp.status_code}, try again later"}), 502

    seerr_user_id = g.seerr_user_id
    previous = {}

    def link(users):
        previous.update(users.get(seerr_user_id, {}))
        users[seerr_user_id] = {
            "letterboxd": lb_username,
            "seerr_display_name": g.user.get("displayName") or g.user.get("username", ""),
            "linked_at": _now(),
        }
    update_users(link)

    old_username = previous.get("letterboxd")
    if old_username and old_username != lb_username:
        forget_user(old_username, int(seerr_user_id))
        with sync_lock:
            if sync_status.get(seerr_user_id, {}).get("status") != "syncing":
                sync_status.pop(seerr_user_id, None)

    logger.info("User %s (Seerr #%s) linked Letterboxd: %s", g.user.get("displayName", "?"), seerr_user_id, lb_username)
    return jsonify({"ok": True, "letterboxd": lb_username})


@app.route("/api/unlink", methods=["POST"])
@require_user
def api_unlink():
    """Remove the Letterboxd link for the current user."""
    seerr_user_id = g.seerr_user_id
    removed = {}

    def unlink(users):
        removed.update(users.pop(seerr_user_id, {}))
    update_users(unlink)

    if removed.get("letterboxd"):
        forget_user(removed["letterboxd"], int(seerr_user_id))
        logger.info("User %s unlinked Letterboxd", g.user.get("displayName", "?"))
    with sync_lock:
        if sync_status.get(seerr_user_id, {}).get("status") != "syncing":
            sync_status.pop(seerr_user_id, None)

    return jsonify({"ok": True})


@app.route("/api/sync", methods=["POST"])
@require_user
def api_sync():
    """Trigger an immediate sync for the current user."""
    seerr_user_id = g.seerr_user_id
    lb_username = load_users().get(seerr_user_id, {}).get("letterboxd")

    if not lb_username:
        return jsonify({"error": "No Letterboxd username linked"}), 400

    if not _claim_sync(seerr_user_id):
        return jsonify({"error": "Sync already in progress"}), 429

    threading.Thread(target=_run_user_sync, args=(seerr_user_id, lb_username), daemon=True).start()
    return jsonify({"ok": True, "status": "syncing"})


@app.route("/api/status")
@require_user
def api_status():
    """Get sync status for the current user."""
    return jsonify(get_status(g.seerr_user_id))


@app.route("/api/preview")
@require_user
def api_preview():
    """
    Preview a Letterboxd watchlist without syncing. Only fetches the
    watchlist pages; titles come from the film cache where known.
    """
    username = request.args.get("username", "").strip().lower()
    if not username:
        return jsonify({"error": "username required"}), 400
    if not is_valid_username(username):
        return jsonify({"error": "Letterboxd usernames only contain letters, numbers and underscores"}), 400

    try:
        slugs = _get_slugs_cached(username)
    except WatchlistFetchError as e:
        return jsonify({"error": str(e)}), 502

    films = read_state()["films"]
    movies = []
    for slug in slugs:
        info = films.get(slug, {})
        movies.append({
            "slug": slug,
            "title": info.get("title") or slug_to_title(slug),
            "year": info.get("year"),
        })
    return jsonify({"movies": movies, "count": len(movies)})


def _seerr_login(data):
    """
    Log in to Seerr. Tries Jellyfin/Emby auth first, then local (email)
    auth, so Jellyfin users and local Seerr users can both sign in.
    Returns the Seerr response, or None if every method was rejected.
    """
    attempts = [
        ("jellyfin", {"username": data["username"], "password": data["password"]}),
        ("local", {"email": data["username"], "password": data["password"]}),
    ]
    for method, body in attempts:
        resp = http_requests.post(f"{SEERR_URL}/api/v1/auth/{method}", json=body, timeout=10)
        if resp.ok:
            return resp
        if resp.status_code in (502, 503, 504):
            # Seerr itself is down, no point trying the next method
            raise SeerrUnavailable()
    return None


@app.route("/api/login", methods=["POST"])
def api_login():
    """
    Authenticate with Seerr (Jellyfin/Emby or local account).
    Proxies the login to Seerr and returns the session cookie.
    """
    data = request.get_json(silent=True)
    if not data or not data.get("username") or not data.get("password"):
        return jsonify({"error": "username and password required"}), 400

    try:
        resp = _seerr_login(data)
    except (http_requests.RequestException, SeerrUnavailable) as e:
        logger.error("Seerr login proxy failed: %s", e)
        return jsonify({"error": "Could not reach Seerr"}), 502

    if resp is None:
        return jsonify({"error": "Invalid credentials"}), 401

    connect_sid = resp.cookies.get("connect.sid")
    if not connect_sid:
        return jsonify({"error": "Login succeeded but no session cookie returned"}), 500

    response = make_response(jsonify({"ok": True}))
    response.set_cookie(
        "connect.sid",
        connect_sid,
        httponly=True,
        secure=COOKIE_SECURE,
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
        secure=COOKIE_SECURE,
        samesite="Lax",
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not SEERR_API_KEY:
        logger.error("SEERR_API_KEY is required. Set it as an environment variable.")
        return

    logger.info("Letterboxderr Web UI starting on %s:%d", WEB_HOST, WEB_PORT)
    logger.info("Seerr URL: %s", SEERR_URL)
    if DRY_RUN:
        logger.info("Dry run mode: nothing will be added to Seerr")

    if SYNC_INTERVAL > 0:
        logger.info("Background sync enabled (every %ds, first run in %ds)", SYNC_INTERVAL, FIRST_SYNC_DELAY)
        threading.Thread(target=sync_loop, daemon=True).start()

    from waitress import serve
    serve(app, host=WEB_HOST, port=WEB_PORT, threads=8)


if __name__ == "__main__":
    main()
