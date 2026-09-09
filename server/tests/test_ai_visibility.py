"""AI 公开状态、选择权限与源码下载。"""
import io
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

import app as app_module
import store


@pytest.fixture
def client(monkeypatch):
    async def fake_user(request):
        user_id = request.headers.get("x-test-user", "owner")
        return {"id": user_id, "login": user_id, "jaccount": user_id}

    monkeypatch.setattr(app_module, "_contest_user", fake_user)
    with TestClient(app_module.app) as test_client:
        yield test_client


def _headers(user: str) -> dict[str, str]:
    return {"x-test-user": user}


def _record_ai(author: str, name: str, source: str = "// source") -> dict:
    directory = store.UPLOADS_DIR / author
    directory.mkdir(parents=True, exist_ok=True)
    cpp_path = directory / f"{name}.cpp"
    so_path = directory / f"{name}.so"
    cpp_path.write_text(source)
    so_path.write_bytes(b"\x7fELF-test")
    ai_id = store.record_ai(author, name, name, str(cpp_path), str(so_path))
    return store.get_ai(ai_id)


def _legacy_ref(ai: dict) -> str:
    return f"uploaded/{ai['author']}/{ai['name']}.so"


def test_my_ais_does_not_expose_account_identity(client):
    response = client.get("/api/my-ais", headers=_headers("owner"))
    assert response.status_code == 200
    assert set(response.json()) == {"ais"}
    assert response.headers["cache-control"] == "no-store"


def test_existing_database_migrates_to_private(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            CREATE TABLE ais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,
                name TEXT NOT NULL,
                display TEXT NOT NULL,
                cpp_path TEXT NOT NULL,
                so_path TEXT NOT NULL,
                created_at REAL NOT NULL,
                upload_count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(author, name)
            )
        """)
        connection.execute(
            "INSERT INTO ais (author,name,display,cpp_path,so_path,created_at) "
            "VALUES ('legacy','old','Old','old.cpp','old.so',0)"
        )
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT is_public FROM ais WHERE author='legacy' AND name='old'"
        ).fetchone()
        profiles_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_profiles'"
        ).fetchone()
    assert row == (0,)
    assert profiles_table == ("user_profiles",)


def test_private_ai_is_owner_only_until_published(client):
    ai = _record_ai("contest-owner", "visibility-test", "// private source")
    ref = _legacy_ref(ai)

    owner_choices = client.get("/api/ais", headers=_headers("owner")).json()["ais"]
    other_choices = client.get("/api/ais", headers=_headers("other")).json()["ais"]
    assert ref in {item["name"] for item in owner_choices}
    assert ref not in {item["name"] for item in other_choices}

    denied = client.post(
        "/api/human/start",
        params={"ai": ref, "side": "R", "max_turns": 1},
        headers=_headers("other"),
    )
    assert denied.status_code == 404

    denied = client.patch(
        f"/api/my-ais/{ai['id']}/visibility",
        json={"is_public": True},
        headers=_headers("other"),
    )
    assert denied.status_code == 404
    invalid = client.patch(
        f"/api/my-ais/{ai['id']}/visibility",
        json={"is_public": "yes"},
        headers=_headers("owner"),
    )
    assert invalid.status_code == 400

    published = client.patch(
        f"/api/my-ais/{ai['id']}/visibility",
        json={"is_public": True},
        headers=_headers("owner"),
    )
    assert published.status_code == 200
    assert published.json()["is_public"] is True

    store.upsert_user_profile(ai["author"], "测试选手")
    public_ais = client.get("/api/public-ais", headers=_headers("other")).json()["ais"]
    published_ai = next(item for item in public_ais if item["ai_id"] == ai["id"])
    assert published_ai["owner_name"] == "测试选手"
    assert published_ai["public_id"] == f"AI-{ai['id']:04d}"
    assert "author" not in published_ai
    other_choices = client.get("/api/ais", headers=_headers("other")).json()["ais"]
    assert ref in {item["name"] for item in other_choices}

    source = client.get(
        f"/api/public-ais/{ai['id']}/source", headers=_headers("other")
    )
    assert source.status_code == 200
    assert source.content == b"// private source"

    hidden = client.patch(
        f"/api/my-ais/{ai['id']}/visibility",
        json={"is_public": False},
        headers=_headers("owner"),
    )
    assert hidden.status_code == 200
    assert client.get(
        f"/api/public-ais/{ai['id']}/source", headers=_headers("other")
    ).status_code == 404
    other_choices = client.get("/api/ais", headers=_headers("other")).json()["ais"]
    assert ref not in {item["name"] for item in other_choices}

def test_public_package_download_contains_complete_source(client):
    ai = _record_ai("contest-pack-owner", "public-package")
    package_dir = store.UPLOADS_DIR / ai["author"] / f"pkg_{ai['name']}"
    package_dir.mkdir()
    (package_dir / "my_ai.cpp").write_text("// main")
    (package_dir / "helper.h").write_text("// helper")
    (package_dir / "build.so").write_bytes(b"compiled")
    assert store.set_ai_public(ai["id"], ai["author"], True)

    response = client.get(
        f"/api/public-ais/{ai['id']}/source", headers=_headers("other")
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"helper.h", "my_ai.cpp"}
