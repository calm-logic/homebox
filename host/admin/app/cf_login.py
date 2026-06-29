"""Browser "Connect with Cloudflare" — drive `cloudflared tunnel login`.

We run Cloudflare's official client to handle the (undocumented) browser
authorize → cert.pem transfer, then extract the embedded API token
(cf.parse_cert_token) and store it exactly like a pasted token. This keeps the
fragile transfer protocol owned by Cloudflare while giving an OAuth-like UX.

Flow:
  start()  → spawn `cloudflared tunnel login` in an isolated HOME, capture the
             dash.cloudflare.com authorize URL it prints, return it. cloudflared
             keeps running and polls Cloudflare for the cert.
  poll()   → has cert.pem landed yet? returns pending | ready(+cert text) |
             failed | expired. The route persists the token on "ready".
  finalize()/cancel() → kill the subprocess + remove the temp dir.

Single-worker assumption (default uvicorn): in-flight sessions live in a
module-level dict.
"""

import asyncio
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

CLOUDFLARED = "/usr/local/bin/cloudflared"
_URL_RE = re.compile(r"https://dash\.cloudflare\.com/argotunnel\?\S+")
_URL_WAIT_SECONDS = 25      # how long to wait for the authorize URL to appear
_SESSION_TTL_SECONDS = 600  # a started login is abandoned after this


@dataclass
class _Session:
    proc: asyncio.subprocess.Process
    home: str
    url: str
    started_at: float = field(default_factory=time.monotonic)

    @property
    def cert_path(self) -> Path:
        return Path(self.home) / ".cloudflared" / "cert.pem"

    @property
    def log_path(self) -> Path:
        return Path(self.home) / "login.log"


_sessions: dict[str, _Session] = {}


def available() -> bool:
    return os.path.exists(CLOUDFLARED)


def _cleanup(session_id: str) -> None:
    sess = _sessions.pop(session_id, None)
    if not sess:
        return
    try:
        if sess.proc.returncode is None:
            sess.proc.kill()
    except ProcessLookupError:
        pass
    shutil.rmtree(sess.home, ignore_errors=True)


def _sweep_stale() -> None:
    now = time.monotonic()
    for sid, sess in list(_sessions.items()):
        if now - sess.started_at > _SESSION_TTL_SECONDS:
            _cleanup(sid)


async def start() -> dict:
    """Spawn cloudflared login and return {session_id, url}. Raises RuntimeError
    if the binary is missing or the authorize URL never appears."""
    if not available():
        raise RuntimeError("cloudflared binary not found in this image.")
    _sweep_stale()

    home = tempfile.mkdtemp(prefix="cf-login-")
    log_path = Path(home) / "login.log"
    logf = open(log_path, "wb")
    # Isolated HOME so cloudflared's "cert already exists" guard never trips and
    # the cert lands at <home>/.cloudflared/cert.pem. Inherit PATH for the binary.
    env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    proc = await asyncio.create_subprocess_exec(
        CLOUDFLARED, "tunnel", "login",
        stdout=logf, stderr=asyncio.subprocess.STDOUT, env=env,
    )
    logf.close()

    # Poll the log for the authorize URL.
    deadline = time.monotonic() + _URL_WAIT_SECONDS
    url = ""
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            break
        try:
            text = log_path.read_text(errors="replace")
        except OSError:
            text = ""
        m = _URL_RE.search(text)
        if m:
            url = m.group(0)
            break
        await asyncio.sleep(0.4)

    if not url:
        tail = ""
        try:
            tail = log_path.read_text(errors="replace")[-500:]
        except OSError:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        shutil.rmtree(home, ignore_errors=True)
        raise RuntimeError(f"cloudflared did not print an authorize URL. {tail}".strip())

    session_id = secrets.token_urlsafe(16)
    _sessions[session_id] = _Session(proc=proc, home=home, url=url)
    return {"session_id": session_id, "url": url}


async def poll(session_id: str) -> dict:
    """Return the login status. On 'ready' includes the cert.pem text for the
    caller to extract + persist (the caller then calls finalize)."""
    sess = _sessions.get(session_id)
    if not sess:
        return {"status": "unknown"}

    if sess.cert_path.exists():
        try:
            cert_text = sess.cert_path.read_text(errors="replace")
        except OSError as e:
            return {"status": "failed", "error": f"could not read cert: {e}"}
        return {"status": "ready", "cert_pem": cert_text}

    if sess.proc.returncode is not None:
        # Exited without writing a cert → failed/cancelled at the dashboard.
        tail = ""
        try:
            tail = sess.log_path.read_text(errors="replace")[-500:]
        except OSError:
            pass
        _cleanup(session_id)
        return {"status": "failed", "error": tail or "cloudflared exited before authorizing."}

    if time.monotonic() - sess.started_at > _SESSION_TTL_SECONDS:
        _cleanup(session_id)
        return {"status": "expired"}

    return {"status": "pending"}


def finalize(session_id: str) -> None:
    """Kill the subprocess + remove the temp dir (after a successful persist)."""
    _cleanup(session_id)


def cancel(session_id: str) -> dict:
    _cleanup(session_id)
    return {"ok": True}
