from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty=True: empty-string env vars (e.g. an unset
    # GitHub `vars.CACHE_TTL_DAYS` rendered as "") are treated as unset
    # so int/float fields fall back to their declared defaults instead of
    # crashing with `int_parsing`. The previous `_drop_empty_env_values`
    # `model_validator(mode="before")` did NOT fix this — pydantic-settings
    # merges env values AFTER before-validators, so empty strings still
    # reached field validation. That bug killed every GitHub Actions
    # bulk_process run and every Vercel cold start with an unset numeric env.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

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

    cache_ttl_days: int = 365

    # Storage — override DATABASE_URL for Postgres/Neon on Vercel
    database_url: str = ""
    # Upload directory — set to /tmp/uploads on Vercel automatically
    upload_dir: str = ""

    # HTTP client timeout (seconds). Keep below your host's function limit.
    # Vercel Hobby=10s, Pro=60s. Default 10s is safe for both.
    httpx_timeout: float = 10.0

    # Per-upload cap on emails in a single CSV (0 = unlimited).
    # Default 1000 — past that the worker's 360-min runner cap starts
    # to bite on per-email mode, and Bouncify rate limits start
    # inflating the 'unknown' count.
    max_bulk_emails: int = 1000
    # Per-user concurrency caps. A new /api/bulk submission is rejected
    # when the user already has either >= max_user_active_jobs jobs in
    # status queued/running, OR the sum of `total` across those jobs
    # would reach max_user_active_emails after adding the new one.
    # 0 disables the respective check.
    max_user_active_jobs: int = 4
    max_user_active_emails: int = 2000
    # Global cap on workflow runs sitting in GitHub Actions' "queued"
    # state. The workflow's own concurrency group already limits how
    # many run in parallel (currently 3); this is the upstream gate
    # that refuses *new* dispatches when too many are already waiting.
    # 0 disables the check.
    max_queued_workflow_runs: int = 10

    # Google OAuth client ID (PUBLIC value — safe to embed in templates).
    # Used by the /admin/account-cleanup "Push to Google Sheets" button.
    # If blank, the button is hidden. Set this in Vercel env. No secret
    # required — Google's web OAuth client IDs are designed to be public
    # (the authorized-origin allowlist is the actual security boundary).
    google_oauth_client_id: str = ""

    # Target Google Sheets spreadsheet ID(s) for the "Push to Google Sheets"
    # button, comma-separated. Each push REPLACES the "Verified" tab in
    # target #1 (existing tab cleared + resized; other tabs left
    # untouched), and if Verified needs to split across multiple
    # spreadsheets to stay under Sheets' 10M cell cap, target #2, #3, ...
    # are used for the overflow parts in order. When blank, the push
    # creates fresh spreadsheets instead. When targets ARE configured,
    # pushing more parts than there are targets — or a configured target
    # being inaccessible (deleted, permissions revoked, wrong ID) — fails
    # the push loudly rather than silently falling back to a spreadsheet
    # outside this list. The authorizing Google account must have edit
    # access to every target spreadsheet.
    google_sheets_target_ids: str = ""

    # GitHub Actions bulk processing
    # Set GITHUB_PAT to a PAT with 'actions:write' scope to enable GHA bulk jobs
    github_pat: str = ""
    github_repo: str = "Surya8991/Email-Validator"  # owner/repo
    # Shared secret the bulk_process workflow uses to call back into the app
    # when a run finishes (success / failure / cancelled). MUST match the
    # JOB_CALLBACK_TOKEN secret configured in the GitHub repo.
    job_callback_token: str = ""

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "info"

    secret_key: str = "dev-secret-change-me-in-production"
    production: bool = False

    # Canonical public origin used for outbound links (password reset, invites).
    # MUST be set in production — never trust request.base_url, which derives from
    # the Host header an attacker can spoof through a misconfigured proxy.
    base_url: str = ""

    # Bootstrap admin — used only if User table is empty on startup
    admin_email: str = ""
    admin_password: str = ""

    # Superadmin — sets this user's role to superadmin on every startup
    # Can be an existing user or will be created (requires admin_password)
    superadmin_email: str = ""

    # ── SMTP (outbound mail for invites etc.) ─────────────────────────────────
    # Leave smtp_host blank to disable email sending (invite link still shown in UI)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True  # STARTTLS on port 587; ignored when port=465 (uses SSL)
    smtp_from: str = ""        # e.g. "no-reply@edstellar.com" (defaults to smtp_user)
    smtp_from_name: str = "Email Validator"
    smtp_timeout: float = 15.0


settings = Settings()
