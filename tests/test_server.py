import os
import re
import stat
from pathlib import Path

import httpx
import pytest

import server

FAKE_ENDPOINT = "https://abcdefghijklmnopqrstuvwxyz.appsync-api.eu-west-2.amazonaws.com/graphql"
FAKE_KEY = "da2-aaaaaaaaaaaaaaaaaaaaaaaaaa"
ROTATED_KEY = "da2-bbbbbbbbbbbbbbbbbbbbbbbbbb"

# Shapes copied from https://datacatalogue.ukdataservice.ac.uk on 2026-09-29.
INDEX_HTML = '<script type="module" crossorigin src="/assets/index-tYcjTpRN.js"></script>'
BUNDLE_JS = 'async function fV(){await gR(()=>import("./amplifyconfig_prod-B3oC6KUH.js"),[]);}'
CONFIG_JS = (
    'Lt.configure({API:{GraphQL:{endpoint:"' + FAKE_ENDPOINT + '",region:"eu-west-2",'
    'defaultAuthMode:"apiKey",apiKey:"' + FAKE_KEY + '"}}});'
)


def catalogue_client(routes: dict[str, str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

    return httpx.Client(base_url=server.CATALOGUE_SITE, transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv(server.API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv("GRAPHQL_API_KEY", raising=False)
    monkeypatch.setattr(server, "_discovered_catalogue_api", None)


class _Posts(list):
    rejected: set[str]
    reject_status: int


@pytest.fixture
def posts(monkeypatch):
    """Record GraphQL POSTs; answer `reject_status` for keys in `rejected`, else 200."""
    calls = _Posts()
    calls.rejected = set()
    calls.reject_status = 401

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers["x-api-key"]))
        status = calls.reject_status if headers["x-api-key"] in calls.rejected else 200
        return httpx.Response(status, json={"data": {"ok": True}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "post", fake_post)
    return calls


def test_no_appsync_key_is_committed():
    source = Path(server.__file__).read_text()
    assert not re.search(r"da2-[a-z0-9]{20,}", source)


def test_discovers_key_from_lazy_config_chunk():
    client = catalogue_client({
        "/": INDEX_HTML,
        "/assets/index-tYcjTpRN.js": BUNDLE_JS,
        "/assets/amplifyconfig_prod-B3oC6KUH.js": CONFIG_JS,
    })
    assert server._discover_catalogue_api(client) == (FAKE_ENDPOINT, FAKE_KEY)


def test_discovers_key_inlined_in_entry_bundle():
    client = catalogue_client({"/": INDEX_HTML, "/assets/index-tYcjTpRN.js": CONFIG_JS})
    assert server._discover_catalogue_api(client) == (FAKE_ENDPOINT, FAKE_KEY)


@pytest.mark.parametrize(
    "routes",
    [
        {"/": "<html>no scripts</html>"},
        {"/": INDEX_HTML, "/assets/index-tYcjTpRN.js": "no config here"},
        {
            "/": INDEX_HTML,
            "/assets/index-tYcjTpRN.js": BUNDLE_JS,
            "/assets/amplifyconfig_prod-B3oC6KUH.js": "Lt.configure({})",
        },
    ],
)
def test_discovery_fails_loudly_when_the_site_changes(routes):
    with pytest.raises(RuntimeError):
        server._discover_catalogue_api(catalogue_client(routes))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example.com/graphql",
        "http://abcdefghijklmnopqrstuvwxyz.appsync-api.eu-west-2.amazonaws.com/graphql",
        "https://abcdefghijklmnopqrstuvwxyz.appsync-api.eu-west-2.amazonaws.com.evil.example/graphql",
        "https://evil.example/x.appsync-api.eu-west-2.amazonaws.com/graphql",
    ],
)
def test_discovery_only_accepts_appsync_endpoints(endpoint):
    config = CONFIG_JS.replace(FAKE_ENDPOINT, endpoint)
    client = catalogue_client({"/": INDEX_HTML, "/assets/index-tYcjTpRN.js": config})
    with pytest.raises(RuntimeError):
        server._discover_catalogue_api(client)


def test_configured_key_wins_and_skips_discovery(monkeypatch, posts):
    monkeypatch.setenv("UKDS_GRAPHQL_API_KEY", "  configured-key  ")
    monkeypatch.setattr(server, "_discover_catalogue_api", lambda: pytest.fail("discovery ran"))
    assert server._gql("{ ok }") == {"data": {"ok": True}}
    assert posts == [(server.GRAPHQL_URL, "configured-key")]


def test_generic_graphql_api_key_is_never_sent_to_ukds(monkeypatch, posts):
    # A GRAPHQL_API_KEY exported for some other service must not leak to UKDS.
    monkeypatch.setenv("GRAPHQL_API_KEY", "someone-elses-secret")
    monkeypatch.setattr(server, "_discover_catalogue_api", lambda: (FAKE_ENDPOINT, FAKE_KEY))
    server._gql("{ ok }")
    assert posts == [(FAKE_ENDPOINT, FAKE_KEY)]


def test_discovered_key_is_cached(monkeypatch, posts):
    discoveries = []
    monkeypatch.setattr(
        server, "_discover_catalogue_api",
        lambda: discoveries.append(1) or (FAKE_ENDPOINT, FAKE_KEY),
    )
    server._gql("{ ok }")
    server._gql("{ ok }")
    assert discoveries == [1]
    assert posts == [(FAKE_ENDPOINT, FAKE_KEY)] * 2


@pytest.mark.parametrize("status", [401, 403])
def test_rotated_key_is_rediscovered_once(monkeypatch, posts, status):
    keys = iter([FAKE_KEY, ROTATED_KEY])
    monkeypatch.setattr(server, "_discover_catalogue_api", lambda: (FAKE_ENDPOINT, next(keys)))
    posts.rejected.add(FAKE_KEY)
    posts.reject_status = status
    assert server._gql("{ ok }") == {"data": {"ok": True}}
    assert posts == [(FAKE_ENDPOINT, FAKE_KEY), (FAKE_ENDPOINT, ROTATED_KEY)]


def test_still_rejected_after_rediscovery_raises(monkeypatch, posts):
    monkeypatch.setattr(server, "_discover_catalogue_api", lambda: (FAKE_ENDPOINT, FAKE_KEY))
    posts.rejected.add(FAKE_KEY)
    with pytest.raises(httpx.HTTPStatusError):
        server._gql("{ ok }")
    assert len(posts) == 2


def test_discovery_failure_names_the_env_var(monkeypatch, posts):
    def boom():
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(server, "_discover_catalogue_api", boom)
    with pytest.raises(RuntimeError, match="UKDS_GRAPHQL_API_KEY"):
        server._gql("{ ok }")
    assert posts == []


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_session_is_saved_owner_only(monkeypatch, tmp_path):
    session_file = tmp_path / "ukds-mcp" / "session.json"
    monkeypatch.setattr(server, "SESSION_FILE", session_file)
    server._save_session({"cookie": "value"})
    assert _mode(session_file) == 0o600
    assert _mode(session_file.parent) == 0o700
    assert server._load_session() == {"cookie": "value"}


def test_existing_loose_session_dir_is_tightened(monkeypatch, tmp_path):
    session_dir = tmp_path / "ukds-mcp"
    session_dir.mkdir(mode=0o755)
    os.chmod(session_dir, 0o755)
    monkeypatch.setattr(server, "SESSION_FILE", session_dir / "session.json")
    server._save_session({"cookie": "value"})
    assert _mode(session_dir) == 0o700
    assert _mode(session_dir / "session.json") == 0o600


def test_loose_session_file_is_tightened_before_cookies_are_written(monkeypatch, tmp_path):
    session_file = tmp_path / "session.json"
    session_file.write_text("{}")
    os.chmod(session_file, 0o644)
    monkeypatch.setattr(server, "SESSION_FILE", session_file)
    real_fdopen = os.fdopen
    modes_at_write = []

    def spy_fdopen(fd, *args, **kwargs):
        modes_at_write.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(server.os, "fdopen", spy_fdopen)
    server._save_session({"cookie": "value"})
    assert modes_at_write == [0o600]


def test_existing_session_file_is_tightened(monkeypatch, tmp_path):
    session_file = tmp_path / "session.json"
    session_file.write_text('{"cookie": "old"}')
    os.chmod(session_file, 0o644)
    monkeypatch.setattr(server, "SESSION_FILE", session_file)
    assert server._load_session() == {"cookie": "old"}
    assert _mode(session_file) == 0o600

    server._save_session({"cookie": "new"})
    assert _mode(session_file) == 0o600
    assert server._load_session() == {"cookie": "new"}


@pytest.mark.skipif(not os.environ.get("UKDS_LIVE_TESTS"), reason="set UKDS_LIVE_TESTS=1 to hit the live UKDS site")
def test_live_discovery_and_search():
    endpoint, key = server._discover_catalogue_api()
    assert endpoint == server.GRAPHQL_URL
    assert key.startswith("da2-")
    result = server._gql('query { __typename }')
    assert "errors" not in result or result.get("data")
