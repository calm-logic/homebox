"""Homebox CLI — manage routing and databases for your Internal PaaS."""

from __future__ import annotations

from typing import Annotated

import typer
from rich import print as rprint

from homebox_cli.config import HomeboxConfig

app = typer.Typer(
    name="homebox",
    help="Manage Homebox Internal PaaS routing and deployments.",
    no_args_is_help=True,
)

# ── Sub-command groups ───────────────────────────────────────────────────────
db_app = typer.Typer(help="Database operations.")
app.add_typer(db_app, name="db")


# ── homebox init ─────────────────────────────────────────────────────────────
@app.command()
def init() -> None:
    """Interactive setup — saves connection details to ~/.homebox.json."""
    from homebox_cli.config import CONFIG_PATH

    rprint("[bold]Homebox Init[/bold]\n")

    host_ip = typer.prompt("Host server LAN IP (e.g. 192.168.1.100)")
    ssh_user = typer.prompt("SSH username on the Host")
    domain = typer.prompt("Base Cloudflare domain (e.g. example.com)")

    ssh_key = typer.prompt(
        "Path to SSH private key",
        default=str(HomeboxConfig.ssh_key_path),
    )
    traefik_conf = typer.prompt(
        "Traefik dynamic config path on Host",
        default=HomeboxConfig.traefik_conf_path,
    )
    projects_dir = typer.prompt(
        "Projects data directory on Host",
        default=HomeboxConfig.projects_dir,
    )

    cfg = HomeboxConfig(
        host_ip=host_ip,
        ssh_user=ssh_user,
        domain=domain,
        ssh_key_path=ssh_key,
        traefik_conf_path=traefik_conf,
        projects_dir=projects_dir,
    )
    cfg.save()
    rprint(f"\n[green]Config saved to {CONFIG_PATH}[/green]")


# ── homebox switch <project> <mode> ─────────────────────────────────────────
@app.command()
def switch(
    project: Annotated[str, typer.Argument(help="Project name (e.g. myapp)")],
    mode: Annotated[
        str,
        typer.Argument(help="Routing target: 'dev' (Devbox) or 'pub' (publisher)"),
    ],
    port: Annotated[
        int,
        typer.Option(help="Port the app listens on (dev mode only)"),
    ] = 8000,
) -> None:
    """Switch traffic routing for a project between Devbox and publisher."""
    if mode not in ("dev", "pub"):
        raise typer.BadParameter("Mode must be 'dev' or 'pub'.")

    from homebox_cli.network import get_local_ip
    from homebox_cli.ssh import get_client
    from homebox_cli.traefik import set_dev_route, set_pub_route

    cfg = HomeboxConfig.load()
    client = get_client(cfg)

    try:
        if mode == "dev":
            devbox_ip = get_local_ip()
            rprint(
                f"[cyan]Routing {project}.{cfg.domain} → "
                f"http://{devbox_ip}:{port}[/cyan]"
            )
            set_dev_route(
                client,
                cfg.traefik_conf_path,
                project=project,
                domain=cfg.domain,
                devbox_ip=devbox_ip,
                port=port,
            )
        else:
            rprint(
                f"[cyan]Routing {project}.{cfg.domain} → "
                f"publisher container (Docker labels)[/cyan]"
            )
            set_pub_route(client, cfg.traefik_conf_path, project=project)

        rprint(f"[green]Switched {project} to {mode} mode.[/green]")
    finally:
        client.close()


# ── homebox db sync <project> ───────────────────────────────────────────────
@db_app.command("sync")
def db_sync(
    project: Annotated[str, typer.Argument(help="Project name")],
    db_name: Annotated[
        str,
        typer.Option("--db", help="Database name inside the container"),
    ] = "",
    db_user: Annotated[
        str,
        typer.Option("--user", help="Postgres user inside the container"),
    ] = "postgres",
    local_db: Annotated[
        str,
        typer.Option("--local-db", help="Local database name to restore into"),
    ] = "",
    container: Annotated[
        str,
        typer.Option(
            "--container",
            help="Override the postgres container name on the Host",
        ),
    ] = "",
) -> None:
    """Pull a project's database from the Host to the local Devbox.

    By convention the postgres container is named ``<project>-postgres-1``.
    Use ``--container`` to override.
    """
    import subprocess
    import tempfile

    from homebox_cli.ssh import get_client, run

    cfg = HomeboxConfig.load()
    client = get_client(cfg)

    resolved_db = db_name or project
    resolved_container = container or f"{project}-postgres-1"
    resolved_local_db = local_db or resolved_db

    try:
        # Dump on the Host via docker exec
        rprint(
            f"[cyan]Dumping {resolved_db} from "
            f"{resolved_container} on Host…[/cyan]"
        )
        dump_cmd = (
            f"docker exec {resolved_container} "
            f"pg_dump -U {db_user} -d {resolved_db} --no-owner --no-acl"
        )
        dump_sql = run(client, dump_cmd)

        # Write dump to a temp file locally
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sql", delete=False
        ) as tmp:
            tmp.write(dump_sql)
            tmp_path = tmp.name

        rprint(f"[cyan]Restoring into local database {resolved_local_db}…[/cyan]")

        # Recreate the local database and restore
        subprocess.run(
            ["dropdb", "--if-exists", resolved_local_db],
            check=False,
        )
        subprocess.run(["createdb", resolved_local_db], check=True)
        subprocess.run(
            ["psql", "-d", resolved_local_db, "-f", tmp_path],
            check=True,
        )

        rprint(f"[green]Database {resolved_local_db} synced successfully.[/green]")
    finally:
        client.close()
