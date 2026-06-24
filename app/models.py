from datetime import datetime

from sqlmodel import Field, SQLModel


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    strategy: str = "bouncify_only"
    providers: str = "bouncify"  # comma-separated
    total: int = 0
    processed: int = 0
    status: str = "queued"  # queued | running | done | failed
    filename: str | None = None
    error: str | None = None


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
