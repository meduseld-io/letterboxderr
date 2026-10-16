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
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

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
    parse_list_ref,
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
ALLOW_AUTO_REQUEST = os.environ.get("ALLOW_AUTO_REQUEST", "true").lower() == "true"
MAX_LISTS = env_int("MAX_LISTS", 10)
# Identifies this install to Plex during sign-in; stable across restarts
PLEX_CLIENT_ID = os.environ.get("PLEX_CLIENT_ID") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"letterboxderr:{SEERR_URL}"))
PLEX_PRODUCT = "Letterboxderr"


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


def user_settings(user_data):
    """A user's sync options, with server-side limits applied."""
    return {
        "lists": list(user_data.get("lists", [])),
        "auto_request": bool(user_data.get("auto_request")) and ALLOW_AUTO_REQUEST,
        "remove_missing": bool(user_data.get("remove_missing")),
    }


def _run_user_sync(seerr_user_id, lb_username):
    """Sync one user (already claimed) and record the result."""
    try:
        settings = user_settings(load_users().get(seerr_user_id, {}))
        result = sync_watchlist(lb_username, int(seerr_user_id), SEERR_URL, SEERR_API_KEY, **settings)
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


def _start_sync(seerr_user_id, lb_username):
    """Start a sync in the background. Returns False if one is already running."""
    if not _claim_sync(seerr_user_id):
        return False
    threading.Thread(target=_run_user_sync, args=(seerr_user_id, lb_username), daemon=True).start()
    return True


next_sync_at = None  # epoch seconds of the next background sync, if enabled


def sync_loop():
    """Background thread that syncs all users on an interval."""
    global next_sync_at
    next_sync_at = time.time() + FIRST_SYNC_DELAY
    time.sleep(FIRST_SYNC_DELAY)
    while True:
        next_sync_at = None
        try:
            background_sync_all()
        except Exception:
            logger.exception("Sync loop error")
        next_sync_at = time.time() + SYNC_INTERVAL
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
        "settings": user_settings(user_data),
        "allow_auto_request": ALLOW_AUTO_REQUEST,
        "max_lists": MAX_LISTS,
        "next_sync": _iso(next_sync_at),
        "seerr_url": SEERR_PUBLIC_URL or SEERR_URL,
        "dry_run": DRY_RUN,
    })


def _iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat() if epoch else None


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
        entry = {
            "letterboxd": lb_username,
            "seerr_display_name": g.user.get("displayName") or g.user.get("username", ""),
            "linked_at": _now(),
        }
        # Keep list and option choices across a relink
        for field in ("lists", "auto_request", "remove_missing"):
            if field in previous:
                entry[field] = previous[field]
        users[seerr_user_id] = entry
    update_users(link)

    old_username = previous.get("letterboxd")
    if old_username and old_username != lb_username:
        forget_user(old_username, int(seerr_user_id))
        with sync_lock:
            if sync_status.get(seerr_user_id, {}).get("status") != "syncing":
                sync_status.pop(seerr_user_id, None)

    logger.info("User %s (Seerr #%s) linked Letterboxd: %s", g.user.get("displayName", "?"), seerr_user_id, lb_username)
    syncing = _start_sync(seerr_user_id, lb_username)
    return jsonify({"ok": True, "letterboxd": lb_username, "syncing": syncing})


@app.route("/api/settings", methods=["POST"])
@require_user
def api_settings():
    """Update the current user's lists and sync options."""
    data = request.get_json(silent=True) or {}
    seerr_user_id = g.seerr_user_id
    if not load_users().get(seerr_user_id, {}).get("letterboxd"):
        return jsonify({"error": "No Letterboxd username linked"}), 400

    changes = {}
    if "lists" in data:
        if not isinstance(data["lists"], list):
            return jsonify({"error": "lists must be a list"}), 400
        lists = []
        for value in data["lists"]:
            ref = parse_list_ref(str(value))
            if not ref:
                return jsonify({"error": f"Not a Letterboxd list: {value}"}), 400
            if ref not in lists:
                lists.append(ref)
        if len(lists) > MAX_LISTS:
            return jsonify({"error": f"You can sync up to {MAX_LISTS} lists"}), 400
        changes["lists"] = lists
    if "auto_request" in data:
        if data["auto_request"] and not ALLOW_AUTO_REQUEST:
            return jsonify({"error": "Auto-request is turned off on this server"}), 403
        changes["auto_request"] = bool(data["auto_request"])
    if "remove_missing" in data:
        changes["remove_missing"] = bool(data["remove_missing"])

    def apply(users):
        if seerr_user_id in users:
            users[seerr_user_id].update(changes)
    users = update_users(apply)
    return jsonify({"ok": True, "settings": user_settings(users.get(seerr_user_id, {}))})


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

    if not _start_sync(seerr_user_id, lb_username):
        return jsonify({"error": "Sync already in progress"}), 429
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
    return _session_response(resp)


def _session_response(seerr_resp):
    """Pass Seerr's session cookie from a successful login on to the browser."""
    connect_sid = seerr_resp.cookies.get("connect.sid")
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


# ---------------------------------------------------------------------------
# Plex sign-in (PIN flow): the browser opens Plex's auth page for a PIN we
# created, we poll the PIN until Plex attaches a token, then log in to
# Seerr with that token. Each PIN is bound to the browser that created it.
# ---------------------------------------------------------------------------
_PLEX_PIN_TTL = 900
_plex_pins = {}  # {pin_id: {"nonce": str, "created": float}}
_plex_lock = threading.Lock()


def _plex_headers():
    return {
        "Accept": "application/json",
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
    }


@app.route("/api/plex/pin", methods=["POST"])
def api_plex_pin():
    """Create a Plex PIN and return the URL the user should sign in at."""
    try:
        resp = http_requests.post("https://plex.tv/api/v2/pins?strong=true", headers=_plex_headers(), timeout=10)
        resp.raise_for_status()
        pin = resp.json()
    except (http_requests.RequestException, ValueError) as e:
        logger.error("Could not create Plex PIN: %s", e)
        return jsonify({"error": "Could not reach Plex"}), 502

    nonce = secrets.token_urlsafe(24)
    now = time.time()
    with _plex_lock:
        for pin_id in [k for k, v in _plex_pins.items() if now - v["created"] > _PLEX_PIN_TTL]:
            del _plex_pins[pin_id]
        _plex_pins[pin["id"]] = {"nonce": nonce, "created": now}

    auth_url = "https://app.plex.tv/auth#?" + urlencode({
        "clientID": PLEX_CLIENT_ID,
        "code": pin["code"],
        "context[device][product]": PLEX_PRODUCT,
    })
    response = make_response(jsonify({"id": pin["id"], "auth_url": auth_url}))
    response.set_cookie(
        "lbx_plex_pin", nonce, max_age=_PLEX_PIN_TTL, httponly=True, secure=COOKIE_SECURE, samesite="Lax", path="/",
    )
    return response


@app.route("/api/plex/pin/<int:pin_id>")
def api_plex_pin_check(pin_id):
    """Check a PIN. Once Plex has authorised it, sign in to Seerr with the token."""
    with _plex_lock:
        entry = _plex_pins.get(pin_id)
    nonce = request.cookies.get("lbx_plex_pin", "")
    if not entry or not secrets.compare_digest(entry["nonce"], nonce):
        return jsonify({"error": "Unknown or expired sign-in attempt"}), 404

    try:
        resp = http_requests.get(f"https://plex.tv/api/v2/pins/{pin_id}", headers=_plex_headers(), timeout=10)
        resp.raise_for_status()
        token = resp.json().get("authToken")
    except (http_requests.RequestException, ValueError) as e:
        logger.error("Could not check Plex PIN: %s", e)
        return jsonify({"error": "Could not reach Plex"}), 502

    if not token:
        return jsonify({"status": "pending"})

    with _plex_lock:
        _plex_pins.pop(pin_id, None)

    try:
        seerr_resp = http_requests.post(f"{SEERR_URL}/api/v1/auth/plex", json={"authToken": token}, timeout=15)
    except http_requests.RequestException as e:
        logger.error("Seerr Plex login failed: %s", e)
        return jsonify({"error": "Could not reach Seerr"}), 502
    if not seerr_resp.ok:
        return jsonify({"error": "Seerr didn't accept this Plex account"}), 401
    return _session_response(seerr_resp)


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
