"""AI 排行榜提交、排名和源码快照行为。"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import store

REAL_BOOTSTRAP_BUILTINS = app_module._bootstrap_builtin_leaderboard


@pytest.fixture(autouse=True)
def clean_leaderboard(monkeypatch, tmp_path):
    directory = tmp_path / "leaderboard"
    directory.mkdir()
    monkeypatch.setattr(app_module, "LEADERBOARD_DIR", directory)
    monkeypatch.setattr(store, "LEADERBOARD_DIR", directory)

    async def skip_builtin_bootstrap():
        pass

    monkeypatch.setattr(
        app_module, "_bootstrap_builtin_leaderboard", skip_builtin_bootstrap
    )
    with store._conn() as connection:
        connection.execute("DELETE FROM leaderboard_matches")
        connection.execute("DELETE FROM leaderboard_entries")
        connection.execute("DELETE FROM leaderboard_submissions")
        connection.execute("DELETE FROM leaderboard_daily_snapshots")
        connection.execute("DELETE FROM competition_jobs")
    yield
    for task in list(app_module.active_leaderboard.values()):
        task.cancel()
    app_module.active_leaderboard.clear()


@pytest.fixture
def client(monkeypatch):
    async def fake_user(request):
        user_id = request.headers.get("x-test-user", "owner")
        return {
            "id": user_id,
            "login": f"login-{user_id}",
            "jaccount": f"ja-{user_id}",
            "name": f"选手-{user_id}",
        }

    monkeypatch.setattr(app_module, "_contest_user", fake_user)
    with TestClient(app_module.app) as test_client:
        yield test_client


def _headers(user: str) -> dict[str, str]:
    return {"x-test-user": user}


def _record_ai(user: str, name: str, source: str = "// ranked source") -> dict:
    author = f"contest-{user}"
    directory = store.UPLOADS_DIR / author
    directory.mkdir(parents=True, exist_ok=True)
    cpp_path = directory / f"{name}.cpp"
    so_path = directory / f"{name}.so"
    cpp_path.write_text(source)
    so_path.write_bytes(f"binary-{user}-{name}".encode())
    ai_id = store.record_ai(author, name, name, str(cpp_path), str(so_path))
    store.upsert_user_profile(author, f"选手-{user}")
    return store.get_ai(ai_id)


def _submit(client: TestClient, user: str, ai_id: int,
            open_source: bool = False, anonymous: bool = False):
    return client.post(
        "/api/leaderboard/submissions",
        headers=_headers(user),
        json={
            "ai_id": ai_id,
            "is_open_source": open_source,
            "is_anonymous": anonymous,
        },
    )


def _wait_for_submission(client: TestClient, user: str,
                         submission_id: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        response = client.get(
            f"/api/leaderboard/submissions/{submission_id}",
            headers=_headers(user),
        )
        assert response.status_code == 200, response.text
        submission = response.json()
        if submission["status"] in ("done", "failed"):
            return submission
        time.sleep(0.02)
    raise AssertionError("leaderboard submission did not finish")


def _series(a_wins: int, b_wins: int, draws: int) -> dict:
    return {
        "games": a_wins + b_wins + draws,
        "a_score": a_wins * 2,
        "b_score": b_wins * 2,
        "a_wins": a_wins,
        "b_wins": b_wins,
        "draws": draws,
        "outcomes": ["a"] * a_wins + ["b"] * b_wins + ["draw"] * draws,
    }


def test_first_entry_and_exact_open_source_snapshot(client):
    ai = _record_ai("first", "first-ai", "// source at submission")
    response = _submit(client, "first", ai["id"], open_source=True)
    assert response.status_code == 200, response.text
    Path(ai["cpp_path"]).write_text("// source changed later")
    submission = _wait_for_submission(client, "first", response.json()["id"])
    assert submission["status"] == "done"
    assert submission["total_opponents"] == 0

    listing = client.get("/api/leaderboard", headers=_headers("first")).json()
    assert listing["my_entry_ai_id"] == ai["id"]
    assert listing["my_entry_ai_ids"] == [ai["id"]]
    assert listing["allow_multiple"] is False
    assert len(listing["standings"]) == 1
    assert listing["standings"][0]["score_rate"] is None
    source = client.get(
        f"/api/leaderboard/entries/{ai['id']}/source",
        headers=_headers("other"),
    )
    assert source.status_code == 200
    assert source.content == b"// source at submission"


def test_second_entry_updates_both_sides_and_ranks_by_score_rate(
        client, monkeypatch):
    first = _record_ai("alpha", "alpha-ai")
    first_response = _submit(client, "alpha", first["id"])
    _wait_for_submission(client, "alpha", first_response.json()["id"])

    calls = []

    def fake_series(a, b, games_per_side, progress):
        calls.append((Path(a).name, Path(b).name, games_per_side))
        return _series(12, 6, 2)

    monkeypatch.setattr(app_module, "run_balanced_series", fake_series)
    second = _record_ai("beta", "beta-ai")
    second_response = _submit(client, "beta", second["id"])
    assert second_response.status_code == 200, second_response.text
    done = _wait_for_submission(client, "beta", second_response.json()["id"])
    assert done["completed_opponents"] == 1
    assert calls == [("candidate.so", "candidate.so", 10)]

    standings = client.get(
        "/api/leaderboard", headers=_headers("beta")
    ).json()["standings"]
    assert [entry["ai_id"] for entry in standings] == [second["id"], first["id"]]
    assert standings[0]["score_rate"] == pytest.approx(0.65)
    assert standings[1]["score_rate"] == pytest.approx(0.35)
    assert standings[0]["games"] == standings[1]["games"] == 20


def test_closed_source_and_submission_ownership(client):
    ai = _record_ai("owner", "closed-ai")
    response = _submit(client, "owner", ai["id"])
    submission_id = response.json()["id"]
    _wait_for_submission(client, "owner", submission_id)

    assert client.get(
        f"/api/leaderboard/entries/{ai['id']}/source",
        headers=_headers("other"),
    ).status_code == 404
    assert client.get(
        f"/api/leaderboard/submissions/{submission_id}",
        headers=_headers("other"),
    ).status_code == 404
    assert _submit(client, "other", ai["id"]).status_code == 404


def test_anonymous_entry_hides_owner_but_keeps_ai_identity(client):
    ai = _record_ai("anonymous", "anonymous-ai")
    response = _submit(client, "anonymous", ai["id"], anonymous=True)
    assert response.status_code == 200, response.text
    _wait_for_submission(client, "anonymous", response.json()["id"])

    standings = client.get(
        "/api/leaderboard", headers=_headers("other")
    ).json()["standings"]
    assert standings[0]["ai_name"] == "anonymous-ai"
    assert standings[0]["owner_name"] == "匿名选手"
    assert standings[0]["is_anonymous"] is True


def test_replacement_is_one_entry_and_failure_keeps_previous_entry(
        client, monkeypatch):
    opponent = _record_ai("opponent", "opponent-ai")
    opponent_response = _submit(client, "opponent", opponent["id"])
    _wait_for_submission(client, "opponent", opponent_response.json()["id"])

    old_ai = _record_ai("owner", "old-ai")
    monkeypatch.setattr(
        app_module, "run_balanced_series",
        lambda *args, **kwargs: _series(11, 7, 2),
    )
    old_response = _submit(client, "owner", old_ai["id"])
    _wait_for_submission(client, "owner", old_response.json()["id"])
    old_entry = store.get_leaderboard_entry("contest-owner")

    new_ai = _record_ai("owner", "new-ai")

    def fail_series(*args, **kwargs):
        raise RuntimeError("engine failed")

    monkeypatch.setattr(app_module, "run_balanced_series", fail_series)
    failed_response = _submit(client, "owner", new_ai["id"])
    failed = _wait_for_submission(client, "owner", failed_response.json()["id"])
    assert failed["status"] == "failed"
    assert "engine failed" in failed["error"]
    retained = store.get_leaderboard_entry("contest-owner")
    assert retained["ai_id"] == old_ai["id"]
    assert retained["binary_sha256"] == old_entry["binary_sha256"]
    assert len(store.list_leaderboard_entries()) == 2

    monkeypatch.setattr(
        app_module, "run_balanced_series",
        lambda *args, **kwargs: _series(8, 10, 2),
    )
    replacement_response = _submit(client, "owner", new_ai["id"])
    replacement = _wait_for_submission(
        client, "owner", replacement_response.json()["id"]
    )
    assert replacement["status"] == "done"
    assert store.get_leaderboard_entry("contest-owner")["ai_id"] == new_ai["id"]
    assert len(store.list_leaderboard_entries()) == 2


def test_admin_can_keep_multiple_entries_that_play_each_other(
        client, monkeypatch):
    monkeypatch.setenv("SENTRY_DUEL_ADMIN_JACCOUNTS", "ja-admin")
    first = _record_ai("admin", "admin-first")
    first_response = _submit(client, "admin", first["id"])
    assert first_response.status_code == 200, first_response.text
    _wait_for_submission(client, "admin", first_response.json()["id"])

    calls = []

    def fake_series(a, b, games_per_side, progress):
        calls.append((Path(a), Path(b), games_per_side))
        return _series(9, 9, 2)

    monkeypatch.setattr(app_module, "run_balanced_series", fake_series)
    second = _record_ai("admin", "admin-second")
    second_response = _submit(client, "admin", second["id"])
    assert second_response.status_code == 200, second_response.text
    done = _wait_for_submission(client, "admin", second_response.json()["id"])

    assert done["status"] == "done"
    assert done["total_opponents"] == done["completed_opponents"] == 1
    assert len(calls) == 1
    assert calls[0][2] == 10
    entries = store.list_leaderboard_entries()
    assert {entry["ai_id"] for entry in entries} == {first["id"], second["id"]}
    assert {entry["account_author"] for entry in entries} == {"contest-admin"}
    assert {entry["author"] for entry in entries} == {
        f"contest-admin:ai:{first['id']}",
        f"contest-admin:ai:{second['id']}",
    }

    listing = client.get("/api/leaderboard", headers=_headers("admin")).json()
    assert listing["allow_multiple"] is True
    assert set(listing["my_entry_ai_ids"]) == {first["id"], second["id"]}
    assert len(listing["standings"]) == 2
    assert all(entry["games"] == 20 for entry in listing["standings"])


def test_daily_limit_is_ten_and_privileged_account_is_unlimited(
        client, monkeypatch):
    regular = _record_ai("regular-limit", "regular-ai")
    privileged = _record_ai("admin", "admin-unlimited")
    now = time.time()
    with store._conn() as connection:
        for user, ai in (("regular-limit", regular), ("admin", privileged)):
            author = f"contest-{user}"
            for index in range(10):
                connection.execute("""
                    INSERT INTO leaderboard_submissions
                        (id,author,entry_key,login,jaccount,ai_id,ai_name,
                         is_open_source,is_anonymous,status,total_opponents,
                         snapshot_dir,binary_sha256,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'done',0,?,?,?)
                """, (
                    f"past-{user}-{index}", author, author, f"login-{user}",
                    f"ja-{user}", ai["id"], ai["name"], 0, 0,
                    f"/tmp/past-{user}-{index}", "hash", now,
                ))

    limited = _submit(client, "regular-limit", regular["id"])
    assert limited.status_code == 409
    assert "每天最多提交 10 次" in limited.json()["detail"]

    monkeypatch.setenv("SENTRY_DUEL_ADMIN_JACCOUNTS", "ja-admin")
    unlimited = _submit(client, "admin", privileged["id"])
    assert unlimited.status_code == 200, unlimited.text
    _wait_for_submission(client, "admin", unlimited.json()["id"])


def test_builtin_ais_are_ranked_and_play_each_other(
        client, monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    ai_dir = project_dir / "ai"
    ai_dir.mkdir(parents=True)
    for stem in ("baseline_ai", "hunter_ai"):
        (ai_dir / f"{stem}.cpp").write_text(f"// {stem}")
        (ai_dir / f"{stem}.so").write_bytes(f"binary-{stem}".encode())
    monkeypatch.setattr(store, "PROJECT_DIR", project_dir)
    calls = []

    def fake_series(a, b, games_per_side, progress):
        calls.append((Path(a), Path(b), games_per_side))
        return _series(10, 8, 2)

    monkeypatch.setattr(app_module, "run_balanced_series", fake_series)
    asyncio.run(REAL_BOOTSTRAP_BUILTINS())

    entries = store.list_leaderboard_entries()
    assert {entry["ai_id"] for entry in entries} == {-1, -2}
    assert all(entry["is_open_source"] for entry in entries)
    assert len(calls) == 1
    assert calls[0][2] == 10
    standings = client.get(
        "/api/leaderboard", headers=_headers("viewer")
    ).json()["standings"]
    assert {entry["public_id"] for entry in standings} == {
        "BASELINE", "HUNTER",
    }
    assert all(entry["games"] == 20 for entry in standings)


def test_init_db_migrates_existing_leaderboard_rows(monkeypatch, tmp_path):
    old_db = tmp_path / "old-app.db"
    with sqlite3.connect(old_db) as connection:
        connection.executescript("""
            CREATE TABLE leaderboard_entries (
                author TEXT PRIMARY KEY, login TEXT NOT NULL,
                ai_id INTEGER NOT NULL, ai_name TEXT NOT NULL,
                binary_path TEXT NOT NULL, source_path TEXT,
                source_filename TEXT, binary_sha256 TEXT NOT NULL,
                source_sha256 TEXT, is_open_source INTEGER NOT NULL,
                submitted_at REAL NOT NULL, ranked_at REAL NOT NULL
            );
            CREATE TABLE leaderboard_submissions (
                id TEXT PRIMARY KEY, author TEXT NOT NULL, login TEXT NOT NULL,
                jaccount TEXT NOT NULL, ai_id INTEGER NOT NULL,
                ai_name TEXT NOT NULL, is_open_source INTEGER NOT NULL,
                status TEXT NOT NULL, total_opponents INTEGER NOT NULL,
                completed_opponents INTEGER NOT NULL DEFAULT 0,
                snapshot_dir TEXT NOT NULL, binary_sha256 TEXT NOT NULL,
                source_sha256 TEXT, error TEXT, created_at REAL NOT NULL,
                started_at REAL, ended_at REAL
            );
            INSERT INTO leaderboard_entries VALUES
                ('contest-old','old-login',7,'old-ai','/tmp/old.so',NULL,NULL,
                 'binary-hash',NULL,0,1.0,2.0);
            INSERT INTO leaderboard_submissions VALUES
                ('old-job','contest-old','old-login','old-ja',7,'old-ai',0,
                 'done',0,0,'/tmp/old-job','binary-hash',NULL,NULL,1.0,1.0,2.0);
        """)

    monkeypatch.setattr(store, "DB_PATH", old_db)
    store.init_db()

    with store._conn() as connection:
        entry_columns = {
            row["name"] for row in connection.execute(
                "PRAGMA table_info(leaderboard_entries)"
            )
        }
        submission_columns = {
            row["name"] for row in connection.execute(
                "PRAGMA table_info(leaderboard_submissions)"
            )
        }
        entry = connection.execute(
            "SELECT account_author FROM leaderboard_entries"
        ).fetchone()
        submission = connection.execute(
            "SELECT entry_key FROM leaderboard_submissions"
        ).fetchone()

    assert "account_author" in entry_columns
    assert "entry_key" in submission_columns
    assert "is_anonymous" in entry_columns
    assert "is_anonymous" in submission_columns
    assert entry["account_author"] == "contest-old"
    assert submission["entry_key"] == "contest-old"

def test_daily_snapshot_is_frozen_for_the_date():
    assert store.save_daily_leaderboard_snapshot("2026-09-07", [{"rank": 1}])
    assert not store.save_daily_leaderboard_snapshot("2026-09-07", [{"rank": 2}])
    snapshot = store.latest_daily_leaderboard_snapshot()
    assert snapshot["snapshot_date"] == "2026-09-07"
    assert snapshot["standings"] == [{"rank": 1}]
