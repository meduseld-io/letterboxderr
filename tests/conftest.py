import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import letterboxderr as lb  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, headers=None, cookies=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self.headers = headers or {}
        self.cookies = cookies or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise lb.requests.HTTPError(f"{self.status_code}")


class FakeClient:
    """Serves canned responses by URL and records what was requested."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        resp = self.pages.get(url)
        if resp is None:
            return FakeResponse(404)
        if isinstance(resp, FakeResponse):
            return resp
        return FakeResponse(200, resp)


def watchlist_page(slugs, has_next=False):
    posters = "".join(f'<div data-target-link="{s}"></div><div data-target-link="{s}"></div>' for s in slugs)
    nxt = '<a class="next" href="/x/">Next</a>' if has_next else ""
    return f"<html><body>{posters}{nxt}</body></html>"


def film_page(tmdb_id, title, media_type="movie", body_attrs=True):
    attrs = f'data-tmdb-id="{tmdb_id}" data-tmdb-type="{media_type}"' if body_attrs else ""
    link = f'<a href="https://www.themoviedb.org/{media_type}/{tmdb_id}/">TMDb</a>'
    return f'<html><head><meta property="og:title" content="{title}"></head><body {attrs}>{link}</body></html>'


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(lb, "DRY_RUN", False)
    monkeypatch.setattr(lb.time, "sleep", lambda s: None)
    return tmp_path


@pytest.fixture
def FakeResp():
    return FakeResponse
