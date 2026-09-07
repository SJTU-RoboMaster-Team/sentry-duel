"""AI 排行榜提交、排名和源码快照行为。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import store


@pytest.fixture(autouse=True)
def clean_leaderboard(monkeypatch, tmp_path):
    directory = tmp_path / "leaderboard"
    directory.mkdir()
    monkeypatch.setattr(app_module, "LEADERBOARD_DIR", directory)
    monkeypatch.setattr(store, "LEADERBOARD_DIR", directory)
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
            open_source: bool = False):
    return client.post(
        "/api/leaderboard/submissions",
        headers=_headers(user),
        json={"ai_id": ai_id, "is_open_source": open_source},
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


def test_daily_snapshot_is_frozen_for_the_date():
    assert store.save_daily_leaderboard_snapshot("2026-09-07", [{"rank": 1}])
    assert not store.save_daily_leaderboard_snapshot("2026-09-07", [{"rank": 2}])
    snapshot = store.latest_daily_leaderboard_snapshot()
    assert snapshot["snapshot_date"] == "2026-09-07"
    assert snapshot["standings"] == [{"rank": 1}]
