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
