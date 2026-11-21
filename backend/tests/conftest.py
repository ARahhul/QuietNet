import httpx
import pytest

from devtools.fake_adguard import create_app
from pahra.adguard.client import AdGuardClient
from pahra.adguard.sync import AdGuardSync
from pahra.config import Settings
from pahra.db import ensure_seed_user, init_sqlite_wal, new_id, now_iso


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        db_path=str(tmp_path / "test.db"),
        lists_cache_dir=str(tmp_path / "lists"),
        offline_lists=True,
        gemini_api_key="",
        companion_enabled=False,
        adguard_url="http://fake",
    )


@pytest.fixture
def conn(settings):
    c = init_sqlite_wal(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def user_id(conn, settings):
    return ensure_seed_user(conn, settings.seed_user_email)


@pytest.fixture
def fake_app():
    return create_app(generate=False)


@pytest.fixture
def fake(fake_app):
    return fake_app.state.ag


@pytest.fixture
async def ag(fake_app):
    client = AdGuardClient("http://fake", "admin", "x", transport=httpx.ASGITransport(app=fake_app))
    yield client
    await client.aclose()


@pytest.fixture
def sync(conn, ag, settings):
    return AdGuardSync(conn, ag, settings)


@pytest.fixture
def add_device(conn, user_id):
    def _add(client_id: str, type_: str = "phone", brand: str | None = None, uid: str | None = None) -> str:
        did = new_id()
        conn.execute(
            "INSERT INTO devices(id, user_id, client_id, name, type, brand, setup_method, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'dot', ?)",
            (did, uid or user_id, client_id, client_id, type_, brand, now_iso()),
        )
        return did

    return _add
