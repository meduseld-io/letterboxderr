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
    assert state == {**lb.empty_state(), "synced": {"alice:1": ["movie:10", "movie:20"]}}


def test_corrupt_state_is_moved_aside(isolated_files):
    (isolated_files / "state.json").write_text("{not json")
    assert lb.load_state() == lb.empty_state()
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


@pytest.mark.parametrize("value, expected", [
    ("https://letterboxd.com/Dave/list/official-top-250/", "dave/official-top-250"),
    ("letterboxd.com/dave/list/top-250", "dave/top-250"),
    ("dave/top-250", "dave/top-250"),
    ("dave/list/top-250", "dave/top-250"),
    ("https://evil.com/dave/list/x/", None),
    ("dave/../x", None),
    ("", None),
])
def test_parse_list_ref(value, expected):
    assert lb.parse_list_ref(value) == expected


def test_lists_are_merged_with_watchlist(monkeypatch):
    client = FakeClient({
        WL.format(1): watchlist_page(["/film/a/"]),
        "https://letterboxd.com/bob/list/faves/page/1/": watchlist_page(["/film/a/", "/film/b/"]),
        "https://letterboxd.com/film/a/": film_page(1, "A (2000)"),
        "https://letterboxd.com/film/b/": film_page(2, "B (2000)"),
    })
    movies = lb.fetch_letterboxd_watchlist("alice", {}, client, lists=["bob/faves"])
    assert [m["tmdb_id"] for m in movies] == [1, 2]


class FeatureHarness(SyncHarness):
    def __init__(self, monkeypatch, films):
        self.films = dict(films)
        self.requested = []
        self.removed = []
        super().__init__(monkeypatch, self.films)
        harness = self

        def fake_request(tmdb_id, title, url, key, user, media_type):
            harness.requested.append(lb.media_key(media_type, tmdb_id))
            return True, None

        def fake_remove(tmdb_id, title, url, key, user):
            harness.removed.append(tmdb_id)
            return True
        monkeypatch.setattr(lb, "request_media", fake_request)
        monkeypatch.setattr(lb, "remove_from_seerr_watchlist", fake_remove)
        self.monkeypatch = monkeypatch

    def set_watchlist(self, films):
        """Replace the watchlist contents; film pages stay resolvable."""
        self.films.update(films)
        pages = {WL.format(1): watchlist_page(list(films))}
        for slug, (tmdb_id, media_type) in self.films.items():
            pages[f"https://letterboxd.com{slug}"] = film_page(tmdb_id, f"{slug} (2000)", media_type)
        client = FakeClient(pages)
        self.monkeypatch.setattr(lb, "make_client", lambda: client)


def test_auto_request_first_run_is_a_baseline(monkeypatch):
    h = FeatureHarness(monkeypatch, {"/film/a/": (1, "movie")})
    h.sync()  # plain sync, a is on the watchlist
    result = h.sync(auto_request=True)
    assert result["requested"] == 0 and h.requested == []

    h.set_watchlist({"/film/a/": (1, "movie"), "/film/b/": (2, "tv")})
    result = h.sync(auto_request=True)
    assert h.requested == ["tv:2"]
    assert result["requested"] == 1

    h.sync(auto_request=True)
    assert h.requested == ["tv:2"]  # not requested twice


def test_auto_request_failure_is_retried(monkeypatch):
    h = FeatureHarness(monkeypatch, {})
    h.sync(auto_request=True)  # baseline with an empty watchlist
    h.set_watchlist({"/film/a/": (1, "movie")})
    monkeypatch.setattr(lb, "request_media", lambda *a: (False, "quota exceeded"))
    result = h.sync(auto_request=True)
    assert result["failures"] == [{"title": "/film/a/", "reason": "quota exceeded"}]

    calls = []
    monkeypatch.setattr(lb, "request_media", lambda *a: (calls.append(a[0]) or (True, None)))
    h.sync(auto_request=True)
    assert calls == [1]


def test_remove_missing_only_removes_titles_we_added(monkeypatch):
    h = FeatureHarness(monkeypatch, {"/film/a/": (1, "movie"), "/film/b/": (2, "movie")})
    monkeypatch.setattr(lb, "get_seerr_watchlist", lambda *a: {"movie:2", "movie:99"})
    h.sync(remove_missing=True)  # adds a; b and 99 were already there
    assert h.added == ["movie:1"]

    h.set_watchlist({"/film/c/": (3, "movie")})
    result = h.sync(remove_missing=True)
    assert h.removed == [1]
    assert result["removed"] == 1
    assert "movie:1" not in lb.load_state()["synced"]["alice:1"]

    # Re-adding it on Letterboxd adds it to Seerr again
    h.set_watchlist({"/film/a/": (1, "movie"), "/film/c/": (3, "movie")})
    h.sync(remove_missing=True)
    assert h.added.count("movie:1") == 2


def test_remove_missing_skips_when_nothing_found(monkeypatch):
    h = FeatureHarness(monkeypatch, {"/film/a/": (1, "movie")})
    h.sync(remove_missing=True)
    h.set_watchlist({})
    h.sync(remove_missing=True)
    assert h.removed == []


def test_remove_missing_off_removes_nothing(monkeypatch):
    h = FeatureHarness(monkeypatch, {"/film/a/": (1, "movie")})
    h.sync()
    h.set_watchlist({"/film/b/": (2, "movie")})
    h.sync()
    assert h.removed == []
