from datetime import datetime

from sqlmodel import Field, SQLModel


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    user_id: int | None = Field(default=None, foreign_key="user.id")
    strategy: str = "bouncify_only"
    providers: str = "bouncify"  # comma-separated
    total: int = 0
    processed: int = 0
    status: str = "queued"  # queued | running | done | failed
    filename: str | None = None
    error: str | None = None
    csv_data: str = ""  # raw CSV content — read by GitHub Actions processor


class EmailResult(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int | None = Field(default=None, foreign_key="job.id")
    email: str
    verdict: str  # valid | invalid | risky | unknown
    provider_data: str = "{}"  # JSON string of per-provider results
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EmailCache(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    verdict: str
    provider_data: str = "{}"  # JSON of per-provider ProviderResult dicts
    providers_used: str = ""   # comma-separated provider names
    strategy: str = ""
    validated_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime


class ApiUsage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    provider: str
    date: str  # YYYY-MM-DD
    calls: int = 0


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(default="user")  # "user" | "admin" | "superadmin"
    is_active: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login: datetime | None = None
    validation_limit: int | None = Field(default=None)  # monthly limit; None = unlimited


class UserSession(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    token_hash: str = Field(index=True)
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Team(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str = Field(default="")
    created_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeamMembership(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    status: str = Field(default="pending")  # "pending" | "active" | "rejected"
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    approved_at: datetime | None = None
    approved_by: int | None = Field(default=None, foreign_key="user.id")


class AuditLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    action: str = Field(index=True)   # e.g. "user.activate", "user.invite.send"
    actor_id: int | None = None
    actor_email: str = ""
    target_type: str = ""   # "user" | "invite" | "session"
    target_id: str = ""     # stringified id or email
    details: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class SystemSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class UserInvite(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    token_hash: str = Field(index=True, unique=True)
    role: str = Field(default="user")  # "user" | "admin"
    invited_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    used_at: datetime | None = None
