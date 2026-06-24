from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bouncify_api_key: str = ""
    zerobounce_api_key: str = ""
    neverbounce_api_key: str = ""
    hunter_api_key: str = ""

    enable_smtp_probe: bool = False
    smtp_probe_from: str = "probe@example.com"

    bouncify_daily_cap: int = 500
    zerobounce_daily_cap: int = 0
    neverbounce_daily_cap: int = 0
    hunter_daily_cap: int = 0

    cache_ttl_days: int = 30

    # Storage — override DATABASE_URL for Postgres/Neon on Vercel
    database_url: str = ""
    # Upload directory — set to /tmp/uploads on Vercel automatically
    upload_dir: str = ""

    # HTTP client timeout (seconds). Keep below your host's function limit.
    # Vercel Hobby=10s, Pro=60s. Default 10s is safe for both.
    httpx_timeout: float = 10.0

    # Max emails per bulk CSV (0 = unlimited). Recommended: 50-100 on Vercel.
    max_bulk_emails: int = 0

    # GitHub Actions bulk processing
    # Set GITHUB_PAT to a PAT with 'actions:write' scope to enable GHA bulk jobs
    github_pat: str = ""
    github_repo: str = "Layruss98266/Email-Validator"  # owner/repo

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "info"

    secret_key: str = "dev-secret-change-me-in-production"
    production: bool = False

    # Bootstrap admin — used only if User table is empty on startup
    admin_email: str = ""
    admin_password: str = ""

    # Superadmin — sets this user's role to superadmin on every startup
    # Can be an existing user or will be created (requires admin_password)
    superadmin_email: str = ""


settings = Settings()
