"""Unit tests for the mirror fast-failover pieces: the shared FailoverCounter,
the cold-standby container filters in app.host, and the mirror config knobs.

No Postgres or Docker needed — docker socket calls are monkeypatched. (The
loops themselves are integration-tested on a real cluster; what's covered here
is the decision logic they share.)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the `app` package importable (tests/ sits beside app/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clusterlib import FailoverCounter  # noqa: E402
from app import host  # noqa: E402
from app.config import Settings  # noqa: E402


# ── FailoverCounter ───────────────────────────────────────────────────────────

def test_counter_promotes_after_consecutive_failures():
    c = FailoverCounter(promote_after=3, demote_after=2)
    assert c.record(False, promoted=False) is None
    assert c.record(False, promoted=False) is None
    assert c.record(False, promoted=False) == "promote"


def test_counter_success_resets_failure_streak():
    c = FailoverCounter(promote_after=3, demote_after=2)
    c.record(False, promoted=False)
    c.record(False, promoted=False)
    assert c.record(True, promoted=False) is None    # healthy resets
    c.record(False, promoted=False)
    c.record(False, promoted=False)
    assert c.record(False, promoted=False) == "promote"


def test_counter_demotes_only_when_promoted():
    c = FailoverCounter(promote_after=3, demote_after=2)
    assert c.record(True, promoted=False) is None
    assert c.record(True, promoted=False) is None    # healthy while drained: no-op
    # Streaks accumulate regardless of promotion state (matches the original
    # module-global counters): once promoted, an ongoing healthy streak demotes
    # at the threshold.
    assert c.record(True, promoted=True) == "demote"
    c.reset()
    assert c.record(True, promoted=True) is None
    assert c.record(True, promoted=True) == "demote"


def test_counter_never_promotes_while_promoted():
    c = FailoverCounter(promote_after=1, demote_after=2)
    assert c.record(False, promoted=True) is None


def test_counter_reset():
    c = FailoverCounter(promote_after=2, demote_after=2)
    c.record(False, promoted=False)
    c.reset()
    assert c.record(False, promoted=False) is None
    assert c.record(False, promoted=False) == "promote"


def test_fast_probe_counter_with_huge_demote_threshold_never_demotes():
    # The fast loop instantiates the counter with demote_after=1<<30 so it can
    # only ever promote.
    c = FailoverCounter(promote_after=3, demote_after=1 << 30)
    for _ in range(1000):
        assert c.record(True, promoted=True) is None


# ── app.host cold-standby container filters ──────────────────────────────────

CONTAINERS = [
    # app web container (should be stopped/started)
    {"Names": ["/blog-web-1"], "State": "running", "Image": "blog-web:latest",
     "Labels": {"com.docker.compose.project": "blog-prod"}},
    # app db container — pgEdge: MUST be exempt (live Spock subscriber)
    {"Names": ["/blog-db-1"], "State": "running",
     "Image": "ghcr.io/pgedge/pgedge-postgres:16-spock5-standard",
     "Labels": {"com.docker.compose.project": "blog-prod"}},
    # plain-postgres db in a non-replicated stack — also exempt
    {"Names": ["/wiki-db-1"], "State": "running", "Image": "postgres:15-alpine",
     "Labels": {"com.docker.compose.project": "wiki-prod"}},
    # a stopped app container (start_app_containers should pick it up)
    {"Names": ["/wiki-app-1"], "State": "exited", "Image": "wiki:latest",
     "Labels": {"com.docker.compose.project": "wiki-prod"}},
    # homebox infra: excluded by name
    {"Names": ["/homebox-traefik"], "State": "running", "Image": "traefik:v3.6",
     "Labels": {"com.docker.compose.project": "base-infrastructure"}},
    # admin stack: excluded by project
    {"Names": ["/admin-app-1"], "State": "running", "Image": "admin:latest",
     "Labels": {"com.docker.compose.project": "admin"}},
    # unlabeled container (docker run, not compose): excluded
    {"Names": ["/random"], "State": "running", "Image": "х:latest", "Labels": {}},
]


@pytest.fixture
def fake_docker(monkeypatch):
    """Monkeypatch host._docker_request with a canned /containers/json list and
    a recorder for stop/start posts."""
    calls: list[str] = []

    def _fake(method: str, path: str):
        if method == "GET" and path.startswith("/containers/json"):
            return 200, json.dumps(CONTAINERS).encode()
        calls.append(f"{method} {path}")
        return 204, b""

    monkeypatch.setattr(host, "_docker_request", _fake)
    return calls


def test_list_app_containers_filters(fake_docker):
    got = {c["name"] for c in host.list_app_containers()}
    assert got == {"blog-web-1", "wiki-app-1"}


def test_stop_app_containers_stops_only_running_apps(fake_docker):
    n = host.stop_app_containers()
    assert n == 1
    assert fake_docker == ["POST /containers/blog-web-1/stop?t=10"]


def test_start_app_containers_starts_only_stopped_apps(fake_docker):
    n = host.start_app_containers()
    assert n == 1
    assert fake_docker == ["POST /containers/wiki-app-1/start"]


def test_db_image_detection():
    assert host._is_db_image("postgres:16")
    assert host._is_db_image("ghcr.io/pgedge/pgedge-postgres:17-spock5-standard")
    assert host._is_db_image("postgis/postgis:15-3.4")
    assert not host._is_db_image("redis:7")
    assert not host._is_db_image("ghost:5")


# ── config knobs ──────────────────────────────────────────────────────────────

def test_node_role_binds_homebox_prefixed_env(monkeypatch):
    # docker-compose passes HOMEBOX_NODE_ROLE; the field must bind it (this was
    # a real bug: the un-aliased field only bound NODE_ROLE, so mirrors came up
    # as peers).
    monkeypatch.setenv("HOMEBOX_NODE_ROLE", "mirror")
    assert Settings().node_role == "mirror"


def test_mirror_settings_defaults():
    s = Settings()
    assert s.mirror_probe_interval == 2.0
    assert s.mirror_probe_failures == 3
    assert s.mirror_cold_apps is False
    assert s.wg_advertise_port == 51820


def test_mirror_settings_env_binding(monkeypatch):
    monkeypatch.setenv("HOMEBOX_MIRROR_PROBE_INTERVAL", "1.5")
    monkeypatch.setenv("HOMEBOX_MIRROR_PROBE_FAILURES", "5")
    monkeypatch.setenv("HOMEBOX_MIRROR_COLD_APPS", "1")
    monkeypatch.setenv("HOMEBOX_WG_ADVERTISE_PORT", "52123")
    s = Settings()
    assert s.mirror_probe_interval == 1.5
    assert s.mirror_probe_failures == 5
    assert s.mirror_cold_apps is True
    assert s.wg_advertise_port == 52123


@pytest.mark.parametrize("env,val", [
    ("HOMEBOX_MIRROR_PROBE_INTERVAL", "0.1"),
    ("HOMEBOX_MIRROR_PROBE_FAILURES", "0"),
    ("HOMEBOX_WG_ADVERTISE_PORT", "70000"),
    ("HOMEBOX_NODE_ROLE", "standby"),
])
def test_mirror_settings_rejects_bad_values(monkeypatch, env, val):
    monkeypatch.setenv(env, val)
    with pytest.raises(Exception):
        Settings()
