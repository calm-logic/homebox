from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://homebox_admin:homebox_admin@db:5432/homebox_admin"
    app_secret: str = "dev-secret-change-me"
    encryption_key: str = "0" * 64
    homebox_base_dir: Path = Path("/host/homebox")
    # Deployed-project working tree. Bind-mounted at the SAME path on host and
    # inside the admin container so docker-compose bind mounts resolve identically
    # on both sides (the daemon interprets host paths). See app/deploy.py.
    projects_host_dir: Path = Path("/opt/homebox/projects")
    homebox_secrets_path: Path = Path("/host/secrets/secrets.json")
    homebox_admin_username: str = "homebox"
    dashboard_auth: str = ""
    homebox_domain: str = ""        # primary root domain seeded at install
    admin_domain: str = ""          # full FQDN of the admin (homebox.<root>)
    session_cookie: str = "homebox_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 14  # 14 days
    homebox_oauth_proxy_url: str = "https://oauth.homebox.sh"


settings = Settings()
