import pytest
from conftest import FakeResponse

import web


@pytest.fixture
def client(isolated_files, monkeypatch):
    monkeypatch.setattr(web, "USERS_FILE", str(isolated_files / "users.json"))
    def auth(cookie):
        return {"id": 7, "displayName": "Ann"} if cookie == "good" else None
    monkeypatch.setattr(web, "authenticate_seerr_user", auth)
    web.sync_status.clear()
    web._preview_cache.clear()
    return web.app.test_client()


def test_preview_requires_login(client):
    assert client.get("/api/preview?username=alice").status_code == 401


def test_preview_rejects_bad_username(client):
    client.set_cookie("connect.sid", "good")
    assert client.get("/api/preview?username=a/../b").status_code == 400


def test_preview_returns_slugs_without_fetching_film_pages(client, monkeypatch):
    client.set_cookie("connect.sid", "good")
    monkeypatch.setattr(web, "fetch_watchlist_slugs", lambda u, c: ["/film/the-godfather/"])
    data = client.get("/api/preview?username=alice").get_json()
    assert data["movies"] == [{"slug": "/film/the-godfather/", "title": "The Godfather", "year": None}]


def test_seerr_down_returns_503(client, monkeypatch):
    def down(cookie):
        raise web.SeerrUnavailable()
    monkeypatch.setattr(web, "authenticate_seerr_user", down)
    client.set_cookie("connect.sid", "good")
    assert client.get("/api/me").status_code == 503


def test_link_rejects_non_ok_letterboxd_response(client, monkeypatch):
    client.set_cookie("connect.sid", "good")

    class C:
        def get(self, url, **kw):
            return FakeResponse(403)
    monkeypatch.setattr(web, "make_client", lambda: C())
    assert client.post("/api/link", json={"letterboxd": "alice"}).status_code == 502


def test_sync_error_is_reported_and_persisted(client, monkeypatch):
    client.set_cookie("connect.sid", "good")
    web.update_users(lambda u: u.update({"7": {"letterboxd": "alice"}}))

    def fail(*a, **k):
        raise web.WatchlistFetchError("Letterboxd blocked the request (403)")
    monkeypatch.setattr(web, "sync_watchlist", fail)

    assert web._claim_sync("7")
    assert not web._claim_sync("7")
    web._run_user_sync("7", "alice")

    status = client.get("/api/status").get_json()
    assert status["status"] == "error" and "403" in status["error"]
    web.sync_status.clear()
    assert web.load_users()["7"]["last_result"]["status"] == "error"


def test_login_falls_back_to_local_auth(client, monkeypatch):
    calls = []

    def post(url, json, timeout):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/local"):
            return FakeResponse(200, cookies={"connect.sid": "sid"})
        return FakeResponse(401)
    monkeypatch.setattr(web.http_requests, "post", post)
    resp = client.post("/api/login", json={"username": "a@b.c", "password": "x"})
    assert resp.status_code == 200
    assert calls == ["jellyfin", "local"]
    assert "connect.sid=sid" in resp.headers["Set-Cookie"]


def _link(monkeypatch, client):
    client.set_cookie("connect.sid", "good")

    class C:
        def get(self, url, **kw):
            return FakeResponse(200)
    monkeypatch.setattr(web, "make_client", lambda: C())
    started = []
    monkeypatch.setattr(web, "_start_sync", lambda uid, name: started.append((uid, name)) or True)
    resp = client.post("/api/link", json={"letterboxd": "alice"})
    return resp, started


def test_link_starts_a_sync(client, monkeypatch):
    resp, started = _link(monkeypatch, client)
    assert resp.get_json()["syncing"] is True
    assert started == [("7", "alice")]


def test_settings_validate_and_persist(client, monkeypatch):
    _link(monkeypatch, client)
    resp = client.post("/api/settings", json={
        "lists": ["https://letterboxd.com/bob/list/faves/", "bob/faves"],
        "auto_request": True,
        "remove_missing": True,
    })
    assert resp.status_code == 200
    assert web.load_users()["7"]["lists"] == ["bob/faves"]
    assert web.user_settings(web.load_users()["7"]) == {
        "lists": ["bob/faves"], "auto_request": True, "remove_missing": True,
    }
    assert client.post("/api/settings", json={"lists": ["not a list"]}).status_code == 400


def test_settings_survive_relink(client, monkeypatch):
    _link(monkeypatch, client)
    client.post("/api/settings", json={"lists": ["bob/faves"]})
    client.post("/api/link", json={"letterboxd": "alice"})
    assert web.load_users()["7"]["lists"] == ["bob/faves"]


def test_auto_request_can_be_disabled_server_side(client, monkeypatch):
    _link(monkeypatch, client)
    monkeypatch.setattr(web, "ALLOW_AUTO_REQUEST", False)
    assert client.post("/api/settings", json={"auto_request": True}).status_code == 403
    web.update_users(lambda u: u["7"].update({"auto_request": True}))
    assert web.user_settings(web.load_users()["7"])["auto_request"] is False


def test_plex_pin_is_bound_to_its_browser(client, monkeypatch):
    def post(url, headers=None, timeout=None, json=None):
        if "plex.tv" in url:
            return FakeResponse(201, json_data={"id": 42, "code": "abc"})
        assert url.endswith("/api/v1/auth/plex") and json == {"authToken": "tok"}
        return FakeResponse(200, cookies={"connect.sid": "plexsid"})

    def get(url, headers=None, timeout=None):
        return FakeResponse(200, json_data={"authToken": "tok"})
    monkeypatch.setattr(web.http_requests, "post", post)
    monkeypatch.setattr(web.http_requests, "get", get)
    web._plex_pins.clear()

    resp = client.post("/api/plex/pin")
    assert "code=abc" in resp.get_json()["auth_url"]

    other = web.app.test_client()
    assert other.get("/api/plex/pin/42").status_code == 404

    resp = client.get("/api/plex/pin/42")
    assert resp.status_code == 200
    assert "connect.sid=plexsid" in resp.headers["Set-Cookie"]
    assert client.get("/api/plex/pin/42").status_code == 404  # single use
