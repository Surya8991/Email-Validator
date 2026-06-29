import pytest
import respx
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

# Use in-memory SQLite for tests
TEST_DB_URL = "sqlite://"


@pytest.fixture(scope="session", autouse=True)
def patch_db(tmp_path_factory):
    from app import db
    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.engine = engine
    SQLModel.metadata.create_all(engine)
    yield engine


@pytest.fixture
def client(patch_db):
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_bouncify():
    with respx.mock(base_url="https://api.bouncify.io") as mock:
        yield mock


@pytest.fixture
def mock_zerobounce():
    with respx.mock(base_url="https://api.zerobounce.net") as mock:
        yield mock


@pytest.fixture
def mock_hunter():
    with respx.mock(base_url="https://api.hunter.io") as mock:
        yield mock


@pytest.fixture
def auth_client(patch_db):
    import bcrypt
    from sqlmodel import Session

    from app.main import app
    from app.models import User
    from app.security import rate_limit as rl

    # Reset the per-IP login bucket between tests — every auth_client fixture
    # call hits POST /login, and 10 fixture uses in 60s otherwise trip the
    # login rate limiter with HTTP 429.
    rl._buckets.clear()

    pw_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt(rounds=4)).decode()
    with Session(patch_db) as db:
        existing = db.exec(
            __import__("sqlmodel").select(User).where(User.email == "test@example.com")
        ).first()
        if not existing:
            db.add(User(
                email="test@example.com",
                password_hash=pw_hash,
                role="admin",
                is_active=True,
            ))
            db.commit()

    with TestClient(app) as c:
        resp = c.post("/login", data={"email": "test@example.com", "password": "testpass123"})
        assert resp.status_code in (200, 302), f"Login failed: {resp.status_code}"
        yield c
