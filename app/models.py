from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
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
    role: str = Field(default="user")  # "admin" | "user"
    is_active: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = None


class UserSession(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    token_hash: str = Field(index=True)
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
