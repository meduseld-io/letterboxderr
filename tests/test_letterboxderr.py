import json

import pytest
from conftest import FakeClient, FakeResponse, film_page, watchlist_page

import letterboxderr as lb

WL = "https://letterboxd.com/alice/watchlist/page/{}/"


def test_parse_film_page_unescapes_title_and_reads_body_attrs():
    info = lb.parse_film_page(film_page(424, "Schindler&#039;s List (1993)"))
    assert info == {"tmdb_id": 424, "media_type": "movie", "title": "Schindler's List", "year": 1993}


def test_parse_film_page_tv_via_link_fallback():
    info = lb.parse_film_page(film_page(1399, "Tom &amp; Jerry", media_type="tv", body_attrs=False))
    assert info["tmdb_id"] == 1399
    assert info["media_type"] == "tv"
    assert info["title"] == "Tom & Jerry"
    assert info["year"] is None


def test_watchlist_slugs_paginate_and_dedupe():
    client = FakeClient({
        WL.format(1): watchlist_page(["/film/a/", "/film/b/"], has_next=True),
        WL.format(2): watchlist_page(["/film/b/", "/film/c/"]),
    })
    assert lb.fetch_watchlist_slugs("alice", client) == ["/film/a/", "/film/b/", "/film/c/"]


@pytest.mark.parametrize("status, message", [(404, "not found"), (403, "blocked"), (500, "500")])
def test_watchlist_fetch_errors_are_raised(status, message):
    client = FakeClient({WL.format(1): FakeResponse(status)})
    with pytest.raises(lb.WatchlistFetchError, match=message):
        lb.fetch_watchlist_slugs("alice", client)


def test_invalid_username_rejected():
    with pytest.raises(lb.WatchlistFetchError):
        lb.fetch_watchlist_slugs("a/../film/x", FakeClient({}))


def test_http_get_retries_on_429():
    responses = [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, "ok")]

    class Client:
        def get(self, url, **kw):
            return responses.pop(0)

    assert lb.http_get(Client(), "https://x", timeout=1).text == "ok"


def test_film_cache_skips_known_film_pages():
    pages = {
        WL.format(1): watchlist_page(["/film/a/", "/film/b/"]),
        "https://letterboxd.com/film/b/": film_page(2, "B (2001)"),
    }
    client = FakeClient(pages)
    cache = {"/film/a/": {"tmdb_id": 1, "media_type": "movie", "title": "A", "year": 2000}}
    movies = lb.fetch_letterboxd_watchlist("alice", cache, client)
    assert [m["tmdb_id"] for m in movies] == [1, 2]
    assert "https://letterboxd.com/film/a/" not in client.requested
    assert cache["/film/b/"]["tmdb_id"] == 2


def test_state_migrates_old_format(isolated_files):
    (isolated_files / "state.json").write_text(json.dumps({"alice:1": [10, 20]}))
    state = lb.load_state()
    assert state == {"synced": {"alice:1": ["movie:10", "movie:20"]}, "films": {}}


def test_corrupt_state_is_moved_aside(isolated_files):
    (isolated_files / "state.json").write_text("{not json")
    assert lb.load_state() == {"synced": {}, "films": {}}
    assert (isolated_files / "state.json.corrupt").exists()


def test_seerr_search_rejects_wrong_year(monkeypatch):
    results = {"results": [{"mediaType": "movie", "id": 5, "releaseDate": "1995-01-01", "title": "Heat"}]}
    monkeypatch.setattr(lb.requests, "get", lambda *a, **k: FakeResponse(200, json_data=results))
    assert lb.resolve_tmdb_id_via_seerr("Heat", 1986, "http://s", "k") == (None, None)
    assert lb.resolve_tmdb_id_via_seerr("Heat", 1995, "http://s", "k") == (5, "movie")


class SyncHarness:
    def __init__(self, monkeypatch, films, seerr_existing=()):
        self.added = []
        pages = {WL.format(1): watchlist_page(list(films))}
        for slug, (tmdb_id, media_type) in films.items():
            pages[f"https://letterboxd.com{slug}"] = film_page(tmdb_id, f"{slug} (2000)", media_type)
        client = FakeClient(pages)
        monkeypatch.setattr(lb, "make_client", lambda: client)
        monkeypatch.setattr(lb, "get_seerr_watchlist", lambda *a: set(seerr_existing))

        def fake_add(tmdb_id, title, url, key, user, media_type):
            self.added.append(lb.media_key(media_type, tmdb_id))
            return True
        monkeypatch.setattr(lb, "add_to_seerr_watchlist", fake_add)

    def sync(self, **kw):
        return lb.sync_watchlist("alice", 1, "http://s", "k", **kw)


def test_dry_run_does_not_mark_items_synced(monkeypatch):
    h = SyncHarness(monkeypatch, {"/film/a/": (1, "movie")})
    result = h.sync(dry_run=True)
    assert result["added"] == 1 and h.added == []
    result = h.sync(dry_run=False)
    assert result["added"] == 1 and h.added == ["movie:1"]


def test_movie_and_tv_with_same_id_do_not_collide(monkeypatch):
    h = SyncHarness(monkeypatch, {"/film/show/": (1399, "tv")}, seerr_existing={"movie:1399"})
    result = h.sync()
    assert result["added"] == 1
    assert h.added == ["tv:1399"]


def test_second_sync_skips_and_uses_cache(monkeypatch):
    h = SyncHarness(monkeypatch, {"/film/a/": (1, "movie")})
    h.sync()
    result = h.sync()
    assert result == {**result, "added": 0, "skipped": 1}
    assert lb.load_state()["films"]["/film/a/"]["tmdb_id"] == 1


def test_concurrent_commits_merge_instead_of_overwriting(monkeypatch):
    lb.update_state(lambda s: s["synced"].update({"bob:2": ["movie:9"]}))
    h = SyncHarness(monkeypatch, {"/film/a/": (1, "movie")})
    h.sync()
    synced = lb.load_state()["synced"]
    assert synced["bob:2"] == ["movie:9"]
    assert synced["alice:1"] == ["movie:1"]


def test_config_env_wins_even_when_equal_to_default(isolated_files, monkeypatch):
    cfg = isolated_files / "config.json"
    cfg.write_text(json.dumps({"seerr_url": "http://file:5055", "sync_interval": 60, "users": []}))
    monkeypatch.setattr(lb, "CONFIG_FILE", str(cfg))
    monkeypatch.setenv("SYNC_INTERVAL", "3600")
    monkeypatch.setattr(lb, "SYNC_INTERVAL", 3600)
    monkeypatch.setenv("SEERR_URL", "http://localhost:5055")
    monkeypatch.setattr(lb, "SEERR_URL", "http://localhost:5055")
    config = lb.load_config()
    assert config["sync_interval"] == 3600
    assert config["seerr_url"] == "http://localhost:5055"


def test_config_file_used_when_env_unset(isolated_files, monkeypatch):
    cfg = isolated_files / "config.json"
    cfg.write_text(json.dumps({"seerr_url": "http://file:5055/", "sync_interval": 60, "users": []}))
    monkeypatch.setattr(lb, "CONFIG_FILE", str(cfg))
    for name in ("SYNC_INTERVAL", "SEERR_URL", "SEERR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config = lb.load_config()
    assert config["sync_interval"] == 60
    assert config["seerr_url"] == "http://file:5055"
