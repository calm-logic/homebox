"""SSH helpers using Paramiko."""

from __future__ import annotations

from pathlib import Path

import paramiko

from homebox_cli.config import HomeboxConfig


def get_client(cfg: HomeboxConfig) -> paramiko.SSHClient:
    """Return an authenticated SSHClient to the Host."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_path = Path(cfg.ssh_key_path).expanduser()
    connect_kwargs: dict = dict(
        hostname=cfg.host_ip,
        username=cfg.ssh_user,
    )

    if key_path.exists():
        connect_kwargs["key_filename"] = str(key_path)
    else:
        # Fall back to the SSH agent
        connect_kwargs["allow_agent"] = True

    client.connect(**connect_kwargs)
    return client


def run(client: paramiko.SSHClient, cmd: str, *, check: bool = True) -> str:
    """Execute *cmd* on the remote Host and return stdout.

    Raises ``SystemExit`` on non-zero exit status when *check* is True.
    """
    _stdin, stdout, stderr = client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode()
    err = stderr.read().decode()

    if check and exit_code != 0:
        raise SystemExit(f"Remote command failed (exit {exit_code}):\n{cmd}\n{err}")
    return out


def read_remote_file(client: paramiko.SSHClient, path: str) -> str:
    sftp = client.open_sftp()
    try:
        with sftp.open(path, "r") as f:
            return f.read().decode()
    finally:
        sftp.close()


def write_remote_file(client: paramiko.SSHClient, path: str, content: str) -> None:
    sftp = client.open_sftp()
    try:
        with sftp.open(path, "w") as f:
            f.write(content)
    finally:
        sftp.close()
