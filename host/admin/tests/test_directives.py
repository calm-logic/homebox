"""Tests for typed remote-op directives (W4) + the D7 clustering gate.

Directive execution: the account poll's "directives" list drives
set_serving / split_off / split_cluster locally (via the existing flows) and
acks each one — idempotently, and without ever letting a bad directive crash
the loop. Route side: cluster create is gated on a linked account (412), the
topology/directive proxies pass through, and POST /join stays open.

Runs on in-memory sqlite with clusterlib._cp monkeypatched; routes are
exercised through a minimal FastAPI app with overridden dependencies.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base, get_session  # noqa: E402
from app.auth import require_session_api  # noqa: E402
from app import clusterlib, crypto  # noqa: E402
from app.models import Setting  # noqa: E402

T0 = datetime(2026, 7, 1, 12, 0, 0)

_ENGINES: list = []


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            while _ENGINES:
                await _ENGINES.pop().dispose()
    clusterlib._handled_directives.clear()  # module state: isolate tests
    return asyncio.run(main())


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


async def link_account(session, cp_url="https://cp.test"):
    session.add(Setting(key=clusterlib.ACCOUNT_KEY, value={
        "control_plane_url": cp_url,
        "token_encrypted": crypto.encrypt("acct-token"),
        "node_name": "test-node", "peer_url": "http://n1",
        "linked_at": T0.isoformat(),
    }))
    await session.commit()


async def join_fake_cluster(session, roster=None):
    self_id = await clusterlib.get_node_id(session)
    await clusterlib.save_cluster(session, {
        "cluster_id": "c1", "name": "home",
        "control_plane_url": "https://cp.test",
        "peer_url": "http://n1", "node_name": "test-node",
        "cluster_secret_encrypted": crypto.encrypt("shhh"),
        "node_token_encrypted": crypto.encrypt("ntok"),
        "account_token_encrypted": crypto.encrypt("acct-token"),
        "roster": roster if roster is not None else [
            {"node_id": self_id, "ordinal": 1, "role": "peer", "online": True},
        ],
        "initial_sync_done": True,
    })
    await session.commit()
    return self_id


class CPRecorder:
    """Captures every clusterlib._cp call; programmable responses."""

    def __init__(self, monkeypatch, responses=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}

        async def _cp(method, base, path, *, token=None, body=None):
            self.calls.append((method, path, body))
            key = (method, path)
            if key in self.responses:
                resp = self.responses[key]
                if isinstance(resp, Exception):
                    raise resp
                return resp
            return {}
        monkeypatch.setattr(clusterlib, "_cp", _cp)

    def acks(self):
        return [(p.rsplit("/", 2)[-2], b) for m, p, b in self.calls
                if m == "POST" and p.endswith("/ack")]


# ───── set_serving ───────────────────────────────────────────────────────────


def test_set_serving_executes_and_acks(monkeypatch):
    async def body():
        session = await make_session()
        await link_account(session)
        cp = CPRecorder(monkeypatch)
        applied = []

        async def fake_apply(sess, serving):
            applied.append(serving)
            return {"serving": serving, "connector": "stopped" if not serving else "started"}
        monkeypatch.setattr(clusterlib, "apply_app_serving", fake_apply)
        # Not clustered → no last-serving guard applies; the drain executes.
        overview = {"directives": [
            {"id": "d1", "type": "set_serving", "payload": {"serving": True}},
        ]}
        await clusterlib.handle_directives(session, overview)
        assert applied == [True]
        acks = cp.acks()
        assert acks == [("d1", {"status": "done", "detail": acks[0][1]["detail"]})]
        assert acks[0][1]["status"] == "done"
    run(body())


def test_set_serving_last_node_guard_refuses_and_acks_error(monkeypatch):
    async def body():
        session = await make_session()
        await link_account(session)
        await join_fake_cluster(session)
        cp = CPRecorder(monkeypatch)
        applied = []

        async def fake_apply(sess, serving):
            applied.append(serving)
            return {"serving": serving}

        async def no_peers(sess, state, target):
            return []

        async def no_mirror(sess, state, target):
            return False
        monkeypatch.setattr(clusterlib, "apply_app_serving", fake_apply)
        monkeypatch.setattr(clusterlib, "serving_peers_excluding", no_peers)
        monkeypatch.setattr(clusterlib, "online_mirror_standby", no_mirror)

        overview = {"directives": [
            {"id": "d2", "type": "set_serving", "payload": {"serving": False}},
        ]}
        await clusterlib.handle_directives(session, overview)
        assert applied == []                       # never drained
        (did, ack), = cp.acks()
        assert did == "d2" and ack["status"] == "error"
        assert "last serving node" in ack["detail"]
    run(body())


def test_set_serving_disable_allowed_with_serving_peer(monkeypatch):
    async def body():
        session = await make_session()
        await link_account(session)
        await join_fake_cluster(session)
        cp = CPRecorder(monkeypatch)
        applied = []

        async def fake_apply(sess, serving):
            applied.append(serving)
            return {"serving": serving, "connector": "stopped"}

        async def one_peer(sess, state, target):
            return [{"node_id": "other", "role": "peer"}]
        monkeypatch.setattr(clusterlib, "apply_app_serving", fake_apply)
        monkeypatch.setattr(clusterlib, "serving_peers_excluding", one_peer)

        overview = {"directives": [
            {"id": "d3", "type": "set_serving", "payload": {"serving": False}},
        ]}
        await clusterlib.handle_directives(session, overview)
        assert applied == [False]
        (did, ack), = cp.acks()
        assert did == "d3" and ack["status"] == "done"
    run(body())


# ───── split_off / split_cluster ─────────────────────────────────────────────


def test_split_off_triggers_flow_and_acks(monkeypatch):
    async def body():
        session = await make_session()
        await link_account(session)
        await join_fake_cluster(session)
        cp = CPRecorder(monkeypatch)
        splits = []

        async def fake_split(sess, *, name, peer_url=None):
            splits.append(name)
            return {"cluster_id": "c-new", "name": name}
        monkeypatch.setattr(clusterlib, "split_off_flow", fake_split)

        overview = {"directives": [
            {"id": "d4", "type": "split_off", "payload": {}},
        ]}
        await clusterlib.handle_directives(session, overview)
        assert splits == ["test-node-cluster"]     # default name from node_name
        (did, ack), = cp.acks()
        assert did == "d4" and ack["status"] == "done"
        assert "c-new" in ack["detail"]
    run(body())


def test_split_cluster_founder_splits_and_invites(monkeypatch):
    async def body():
        session = await make_session()
        await link_account(session)
        self_id = await clusterlib.get_node_id(session)
        await join_fake_cluster(session, roster=[
            {"node_id": self_id, "ordinal": 2, "role": "peer", "online": True},
            {"node_id": "node-x", "ordinal": 3, "role": "peer", "online": True},
            {"node_id": "node-y", "ordinal": 1, "role": "peer", "online": True},
        ])
        cp = CPRecorder(monkeypatch)
        splits = []

        async def fake_split(sess, *, name, peer_url=None):
            splits.append(name)
            return {"cluster_id": "c-split", "name": name}
        monkeypatch.setattr(clusterlib, "split_off_flow", fake_split)

        # The recipient founds the new cluster regardless of ordinal.
        overview = {"directives": [{
            "id": "d5", "type": "split_cluster",
            "payload": {"node_ids": [self_id, "node-x"], "name": "staging"},
        }]}
        await clusterlib.handle_directives(session, overview)
        assert splits == ["staging"]
        invites = [(p, b) for m, p, b in cp.calls
                   if m == "POST" and p == "/v1/clusters/c-split/invite"]
        assert invites == [("/v1/clusters/c-split/invite", {"node_id": "node-x"})]
        (did, ack), = cp.acks()
        assert did == "d5" and ack["status"] == "done"
    run(body())


def test_split_cluster_recipient_founds_even_at_higher_ordinal(monkeypatch):
    # The UI targets an arbitrary member of the split set; the recipient must
    # found the cluster itself — an ordinal election would wait on a node that
    # never received the directive.
    async def body():
        session = await make_session()
        await link_account(session)
        self_id = await clusterlib.get_node_id(session)
        await join_fake_cluster(session, roster=[
            {"node_id": self_id, "ordinal": 5, "role": "peer", "online": True},
            {"node_id": "node-y", "ordinal": 1, "role": "peer", "online": True},
        ])
        cp = CPRecorder(monkeypatch)
        splits = []

        async def fake_split(sess, *, name, peer_url=None):
            splits.append(name)
            return {"cluster_id": "c-split", "name": name}
        monkeypatch.setattr(clusterlib, "split_off_flow", fake_split)

        overview = {"directives": [{
            "id": "d6", "type": "split_cluster",
            "payload": {"node_ids": [self_id, "node-y"], "name": "staging"},
        }]}
        await clusterlib.handle_directives(session, overview)
        assert splits == ["staging"]
        invites = [b for m, p, b in cp.calls
                   if m == "POST" and p == "/v1/clusters/c-split/invite"]
        assert invites == [{"node_id": "node-y"}]
        (did, ack), = cp.acks()
        assert did == "d6" and ack["status"] == "done"
    run(body())


def test_split_cluster_recipient_outside_set_errors(monkeypatch):
    async def body():
        session = await make_session()
        await link_account(session)
        self_id = await clusterlib.get_node_id(session)
        await join_fake_cluster(session, roster=[
            {"node_id": self_id, "ordinal": 1, "role": "peer", "online": True},
            {"node_id": "node-y", "ordinal": 2, "role": "peer", "online": True},
        ])
        cp = CPRecorder(monkeypatch)
        splits = []

        async def fake_split(sess, *, name, peer_url=None):
            splits.append(name)
            return {"cluster_id": "x", "name": name}
        monkeypatch.setattr(clusterlib, "split_off_flow", fake_split)

        overview = {"directives": [{
            "id": "d7", "type": "split_cluster",
            "payload": {"node_ids": ["node-y"], "name": "staging"},
        }]}
        await clusterlib.handle_directives(session, overview)
        assert splits == []
        (did, ack), = cp.acks()
        assert did == "d7" and ack["status"] == "error"
        assert "split set" in ack["detail"]
    run(body())


# ───── robustness: idempotency + bad directives ──────────────────────────────


def test_directive_idempotent_reack_without_reexecution(monkeypatch):
    async def body():
        session = await make_session()
        await link_account(session)
        cp = CPRecorder(monkeypatch)
        applied = []

        async def fake_apply(sess, serving):
            applied.append(serving)
            return {"serving": serving}
        monkeypatch.setattr(clusterlib, "apply_app_serving", fake_apply)

        overview = {"directives": [
            {"id": "d7", "type": "set_serving", "payload": {"serving": True}},
        ]}
        await clusterlib.handle_directives(session, overview)
        # Re-delivered (e.g. the ack was lost): re-acked, never re-executed.
        await clusterlib.handle_directives(session, overview)
        assert applied == [True]
        assert [d for d, _ in cp.acks()] == ["d7", "d7"]
    run(body())


def test_bad_directives_never_crash_and_ack_errors(monkeypatch):
    async def body():
        session = await make_session()
        await link_account(session)
        cp = CPRecorder(monkeypatch)

        async def boom(sess, *, name, peer_url=None):
            raise ValueError("This node is not part of a cluster.")
        monkeypatch.setattr(clusterlib, "split_off_flow", boom)

        overview = {"directives": [
            "not-a-dict",                                        # ignored
            {"no": "id"},                                        # ignored
            {"id": "d8", "type": "warp_drive", "payload": {}},   # unknown type
            {"id": "d9", "type": "split_off", "payload": {}},    # raises inside
        ]}
        await clusterlib.handle_directives(session, overview)    # must not raise
        acks = dict(cp.acks())
        assert acks["d8"]["status"] == "error" and "unknown directive" in acks["d8"]["detail"]
        assert acks["d9"]["status"] == "error" and "not part of a cluster" in acks["d9"]["detail"]
    run(body())


def test_legacy_join_directive_field_still_works(monkeypatch):
    """_maybe_autojoin (legacy overview['directive']) is untouched by the new
    typed-directives path."""
    async def body():
        session = await make_session()
        await link_account(session)
        joined = []

        async def fake_join(sess, **kw):
            joined.append(kw["join_token"])
            return {"cluster_id": "cj"}
        monkeypatch.setattr(clusterlib, "join_cluster_flow", fake_join)
        await clusterlib._maybe_autojoin(session, {
            "directive": {"cluster_id": "cj", "cluster_name": "home",
                          "join_token": "hbj.cj.secret"},
        })
        assert joined == ["hbj.cj.secret"]
    run(body())


# ───── routes: D7 gating + topology/directive proxies ────────────────────────


def make_client(setup=None):
    """A TestClient over the cluster router with sqlite + no-auth overrides.
    The engine is created lazily INSIDE the app's event loop."""
    from app.routes import cluster as cluster_routes
    holder: dict = {}

    async def _get_session():
        if "maker" not in holder:
            engine = create_async_engine("sqlite+aiosqlite://")
            holder["engine"] = engine
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            holder["maker"] = async_sessionmaker(engine, expire_on_commit=False)
            if setup is not None:
                async with holder["maker"]() as s:
                    await setup(s)
        async with holder["maker"]() as s:
            yield s

    app = FastAPI()
    app.include_router(cluster_routes.router)
    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[require_session_api] = lambda: "tester@test"
    return TestClient(app)


def test_create_cluster_gated_412_when_unlinked():
    with make_client() as client:
        r = client.post("/api/cluster/create", json={
            "name": "home", "account_token": "tok",
            "peer_url": "http://n1", "node_name": "n1",
        })
        assert r.status_code == 412
        assert "homebox.sh account" in r.json()["detail"]


def test_account_create_cluster_gated_412_when_unlinked():
    with make_client() as client:
        r = client.post("/api/cluster/account/create-cluster", json={"name": "home"})
        assert r.status_code == 412


def test_token_join_stays_open(monkeypatch):
    """POST /join must NOT require a linked account (mirror VMs bootstrap
    through it) — it proceeds straight into the join flow."""
    attempted = []

    async def fake_join(sess, **kw):
        attempted.append(kw["join_token"])
        return {"cluster_id": "c9"}
    monkeypatch.setattr(clusterlib, "join_cluster_flow", fake_join)
    with make_client() as client:
        r = client.post("/api/cluster/join", json={
            "join_token": "hbj.c9.secret", "peer_url": "http://n2",
        })
        assert r.status_code == 200 and attempted == ["hbj.c9.secret"]


def test_topology_412_when_unlinked():
    with make_client() as client:
        r = client.get("/api/cluster/account/topology")
        assert r.status_code == 412


TOPOLOGY = {
    "account": {"email": "al@test", "plan": "premium", "features": ["cluster"]},
    "clusters": [{"cluster_id": "c1", "name": "home", "license": {},
                  "mirror": {"status": "none"},
                  "nodes": [{"node_id": "n-1", "name": "a", "online": True}]}],
    "standalone_nodes": [{"node_id": "n-2", "name": "b", "online": False}],
    "vault": {"version": 4, "updated_at": T0.isoformat()},
    "directives": [],
}


def test_topology_proxy_shape(monkeypatch):
    async def setup(s):
        await link_account(s)
        s.add(Setting(key="node_provisions", value=[{"id": "p1", "status": "booting"}]))
        s.add(Setting(key="vault_state", value={
            "version": 4, "pushed_at": "2026-07-17T00:00:00",
            "pulled_at": "2026-07-17T00:01:00", "error": None}))
        await s.commit()

    async def _cp(method, base, path, *, token=None, body=None):
        assert (method, path) == ("GET", "/v1/accounts/topology")
        assert token == "acct-token"
        return dict(TOPOLOGY)
    monkeypatch.setattr(clusterlib, "_cp", _cp)

    with make_client(setup) as client:
        r = client.get("/api/cluster/account/topology")
        assert r.status_code == 200
        data = r.json()
        assert data["account"]["email"] == "al@test"
        assert data["clusters"][0]["cluster_id"] == "c1"
        assert data["standalone_nodes"][0]["node_id"] == "n-2"
        assert data["vault"]["version"] == 4
        assert data["this_node_id"]                        # local annotation
        assert data["provisions"] == [{"id": "p1", "status": "booting"}]
        assert data["vault_state"] == {"pushed_at": "2026-07-17T00:00:00",
                                       "pulled_at": "2026-07-17T00:01:00",
                                       "error": None}


def test_directive_proxy_passes_through_and_maps_4xx(monkeypatch):
    calls = []

    async def _cp(method, base, path, *, token=None, body=None):
        calls.append((method, path, body))
        if body and body.get("type") == "split_cluster":
            raise clusterlib.ControlPlaneError(
                "plan gate", status_code=402,
                detail="Your plan doesn't include clustering.")
        return {"id": "d100", "status": "pending"}
    monkeypatch.setattr(clusterlib, "_cp", _cp)

    with make_client(link_account) as client:
        r = client.post("/api/cluster/account/directives", json={
            "node_id": "n-2", "type": "set_serving", "payload": {"serving": False}})
        assert r.status_code == 200 and r.json()["id"] == "d100"
        assert calls[-1] == ("POST", "/v1/accounts/directives",
                             {"node_id": "n-2", "type": "set_serving",
                              "payload": {"serving": False}})
        # CP 4xx (plan gate) passes through with its detail.
        r = client.post("/api/cluster/account/directives", json={
            "node_id": "n-2", "type": "split_cluster", "payload": {}})
        assert r.status_code == 402
        assert "plan" in r.json()["detail"].lower()


def test_directive_proxy_412_when_unlinked():
    with make_client() as client:
        r = client.post("/api/cluster/account/directives", json={
            "node_id": "n-2", "type": "set_serving", "payload": {}})
        assert r.status_code == 412


# ───── cluster rename proxy + "home" name coalescing ─────────────────────────


def test_rename_proxy_happy_path(monkeypatch):
    calls = []

    async def _cp(method, base, path, *, token=None, body=None):
        calls.append((method, path, body, token))
        return {"ok": True, "cluster_id": "c-far", "name": "attic"}
    monkeypatch.setattr(clusterlib, "_cp", _cp)

    with make_client(link_account) as client:
        r = client.patch("/api/cluster/account/clusters/c-far/name",
                         json={"name": "attic"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "name": "attic"}
        assert calls == [("PATCH", "/v1/clusters/c-far", {"name": "attic"}, "acct-token")]


def test_rename_proxy_412_when_unlinked():
    with make_client() as client:
        r = client.patch("/api/cluster/account/clusters/c1/name", json={"name": "x"})
        assert r.status_code == 412


def test_rename_proxy_passes_cp_4xx_through(monkeypatch):
    async def _cp(method, base, path, *, token=None, body=None):
        if body["name"].strip() == "":
            raise clusterlib.ControlPlaneError(
                "bad name", status_code=400, detail="Cluster name must not be empty")
        raise clusterlib.ControlPlaneError(
            "not yours", status_code=404, detail="Unknown cluster")
    monkeypatch.setattr(clusterlib, "_cp", _cp)

    with make_client(link_account) as client:
        r = client.patch("/api/cluster/account/clusters/c-other/name",
                         json={"name": "steal"})
        assert r.status_code == 404
        assert r.json()["detail"] == "Unknown cluster"
        r = client.patch("/api/cluster/account/clusters/c-other/name",
                         json={"name": "  "})
        assert r.status_code == 400
        assert "empty" in r.json()["detail"]


def test_rename_own_cluster_updates_local_state(monkeypatch):
    """Renaming THIS node's own cluster also rewrites the local state blob —
    heartbeats never refresh the name (only roster/license), so without the
    local write /api/cluster would show the old name forever."""
    async def setup(s):
        await link_account(s)
        await join_fake_cluster(s)

    async def _cp(method, base, path, *, token=None, body=None):
        assert (method, path) == ("PATCH", "/v1/clusters/c1")
        return {"ok": True, "cluster_id": "c1", "name": body["name"].strip()}
    monkeypatch.setattr(clusterlib, "_cp", _cp)

    with make_client(setup) as client:
        r = client.patch("/api/cluster/account/clusters/c1/name",
                         json={"name": "renamed-home"})
        assert r.status_code == 200 and r.json()["name"] == "renamed-home"
        status = client.get("/api/cluster").json()
        assert status["active"] is True
        assert status["name"] == "renamed-home"


def test_rename_other_cluster_leaves_local_state_alone(monkeypatch):
    async def setup(s):
        await link_account(s)
        await join_fake_cluster(s)  # cluster_id c1, name "home"

    async def _cp(method, base, path, *, token=None, body=None):
        return {"ok": True, "cluster_id": "c-far", "name": "attic"}
    monkeypatch.setattr(clusterlib, "_cp", _cp)

    with make_client(setup) as client:
        r = client.patch("/api/cluster/account/clusters/c-far/name",
                         json={"name": "attic"})
        assert r.status_code == 200
        assert client.get("/api/cluster").json()["name"] == "home"


def test_cluster_status_coalesces_empty_name_to_home():
    async def setup(s):
        self_id = await clusterlib.get_node_id(s)
        await clusterlib.save_cluster(s, {
            "cluster_id": "c1", "name": "",  # pre-default blob
            "control_plane_url": "https://cp.test",
            "peer_url": "http://n1", "node_name": "test-node",
            "cluster_secret_encrypted": crypto.encrypt("shhh"),
            "node_token_encrypted": crypto.encrypt("ntok"),
            "roster": [{"node_id": self_id, "ordinal": 1, "role": "peer"}],
            "initial_sync_done": True,
        })
        await s.commit()

    with make_client(setup) as client:
        assert client.get("/api/cluster").json()["name"] == "home"


def test_topology_proxy_coalesces_empty_cluster_name(monkeypatch):
    async def _cp(method, base, path, *, token=None, body=None):
        topo = dict(TOPOLOGY)
        topo["clusters"] = [{"cluster_id": "c1", "name": "", "license": {},
                             "mirror": None, "nodes": []}]
        return topo
    monkeypatch.setattr(clusterlib, "_cp", _cp)

    with make_client(link_account) as client:
        r = client.get("/api/cluster/account/topology")
        assert r.status_code == 200
        assert r.json()["clusters"][0]["name"] == "home"


def test_account_status_carries_vault_freshness():
    async def setup(s):
        await link_account(s)
        s.add(Setting(key="vault_state", value={
            "version": 2, "pushed_at": "2026-07-17T01:00:00",
            "pulled_at": None, "error": None, "pushed_hash": "xyz"}))
        await s.commit()

    with make_client(setup) as client:
        r = client.get("/api/cluster/account")
        assert r.status_code == 200
        data = r.json()
        assert data["linked"] is True
        assert data["vault"] == {"version": 2, "pushed_at": "2026-07-17T01:00:00",
                                 "pulled_at": None, "error": None}
