"""Formal competition and qualifier behavior."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import competitions
import store


@pytest.fixture
def client(monkeypatch):
    async def fake_user(request):
        user_id = request.headers.get("x-test-user", "owner")
        return {"id": user_id, "login": f"login-{user_id}",
                "jaccount": f"ja-{user_id}"}

    monkeypatch.setattr(app_module, "_contest_user", fake_user)
    with TestClient(app_module.app) as test_client:
        yield test_client


def _headers(user: str = "owner") -> dict[str, str]:
    return {"x-test-user": user}


def _record_ai(user: str, name: str) -> dict:
    author = f"contest-{user}"
    directory = store.UPLOADS_DIR / author
    directory.mkdir(parents=True, exist_ok=True)
    cpp_path = directory / f"{name}.cpp"
    so_path = directory / f"{name}.so"
    cpp_path.write_text("// test")
    so_path.write_bytes(b"\x7fELF-test")
    ai_id = store.record_ai(
        author, name, name, str(cpp_path), str(so_path)
    )
    return store.get_ai(ai_id)


def _upload_document(client: TestClient, user: str,
                     filename: str = "technical-report.pdf") -> dict:
    response = client.post(
        "/api/qualifiers/document", headers=_headers(user),
        files={"file": (filename, b"%PDF-1.7 test report", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()["technical_document"]


def _wait_for_job(client: TestClient, job_id: str, user: str = "owner") -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        response = client.get(
            f"/api/competitions/{job_id}", headers=_headers(user)
        )
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("batch job did not finish")


def _wait_for_qualified(client: TestClient, user: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        listing = client.get("/api/qualifiers", headers=_headers(user)).json()
        if listing["my_qualified"]:
            return listing
        time.sleep(0.02)
    raise AssertionError("qualification result was not persisted")


def _result(a_score: int, b_score: int, a_wins: int,
            b_wins: int, draws: int) -> dict:
    return {
        "games": 100, "a_score": a_score, "b_score": b_score,
        "a_wins": a_wins, "b_wins": b_wins, "draws": draws,
    }


def test_balanced_series_maps_scores_back_to_ai(monkeypatch, tmp_path):
    ai_a = tmp_path / "a.so"
    ai_b = tmp_path / "b.so"
    ai_a.write_bytes(b"a")
    ai_b.write_bytes(b"b")
    calls = []

    def fake_game(red: Path, blue: Path, max_turns: int = 20):
        calls.append((red.name, blue.name))
        return {"type": "game_over", "red_score": 3, "blue_score": 1}

    monkeypatch.setattr(competitions, "play_game", fake_game)
    monkeypatch.setattr(competitions, "ENGINE_BIN", ai_a)
    result = competitions.run_balanced_series(ai_a, ai_b, games_per_side=2)

    assert calls == [("a.so", "b.so"), ("a.so", "b.so"),
                     ("b.so", "a.so"), ("b.so", "a.so")]
    assert result == {
        "games": 4, "a_score": 8, "b_score": 8,
        "a_wins": 2, "b_wins": 2, "draws": 0,
        "outcomes": ["a", "a", "b", "b"],
    }


def test_batch_competition_uses_game_wins_and_enforces_access(
        client, monkeypatch):
    own_a = _record_ai("owner", "formal-a")
    own_b = _record_ai("owner", "formal-b")
    private = _record_ai("other", "formal-private")

    def fake_series(a, b, games_per_side, progress):
        result = _result(400, 390, 40, 50, 10)
        progress(result)
        return result

    monkeypatch.setattr(app_module, "run_balanced_series", fake_series)
    ref_a = f"uploaded/{own_a['author']}/{own_a['name']}.so"
    ref_b = f"uploaded/{own_b['author']}/{own_b['name']}.so"
    response = client.post(
        "/api/competitions/batch", headers=_headers(),
        json={"ai_a": ref_a, "ai_b": ref_b},
    )
    assert response.status_code == 200, response.text
    job = _wait_for_job(client, response.json()["id"])
    assert job["winner"] == "ai_b"
    assert job["ai_a_score"] == 400
    assert job["ai_a_wins"] == 40

    private_ref = f"uploaded/{private['author']}/{private['name']}.so"
    denied = client.post(
        "/api/competitions/batch", headers=_headers(),
        json={"ai_a": ref_a, "ai_b": private_ref},
    )
    assert denied.status_code == 404


def test_qualifier_creates_two_series_and_uses_game_wins(client, monkeypatch):
    candidate = _record_ai("qualifier-owner", "candidate")
    document = _upload_document(client, "qualifier-owner")
    assert document["name"] == "technical-report.pdf"

    def fake_series(a, b, games_per_side, progress):
        result = (_result(400, 500, 60, 30, 10)
                  if "baseline" in b.name
                  else _result(300, 600, 55, 40, 5))
        progress(result)
        return result

    monkeypatch.setattr(app_module, "run_balanced_series", fake_series)
    response = client.post(
        "/api/qualifiers", headers=_headers("qualifier-owner"),
        json={"ai_id": candidate["id"]},
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert len(created["jobs"]) == 2
    assert {job["total_games"] for job in created["jobs"]} == {100}
    jobs = [
        _wait_for_job(client, job["id"], user="qualifier-owner")
        for job in created["jobs"]
    ]
    assert {job["details"]["benchmark"] for job in jobs} == {
        "baseline", "hunter"
    }
    assert {job["winner"] for job in jobs} == {"ai_a"}
    listing = _wait_for_qualified(client, "qualifier-owner")
    assert listing["my_qualified"] is True


def test_qualifier_requires_winning_both_series(client, monkeypatch):
    candidate = _record_ai("tie-owner", "candidate")
    _upload_document(client, "tie-owner")

    def fake_series(a, b, games_per_side, progress):
        result = (_result(700, 300, 50, 50, 0)
                  if "baseline" in b.name
                  else _result(600, 300, 80, 20, 0))
        progress(result)
        return result

    monkeypatch.setattr(app_module, "run_balanced_series", fake_series)
    response = client.post(
        "/api/qualifiers", headers=_headers("tie-owner"),
        json={"ai_id": candidate["id"]},
    )
    assert response.status_code == 200, response.text
    jobs = [
        _wait_for_job(client, job["id"], user="tie-owner")
        for job in response.json()["jobs"]
    ]
    assert {job["winner"] for job in jobs} == {"ai_a", "draw"}
    listing = client.get(
        "/api/qualifiers", headers=_headers("tie-owner")
    ).json()
    assert listing["my_qualified"] is False


def test_qualifier_records_qualified_user_and_builtin_sources(client, monkeypatch):
    candidate = _record_ai("qualified-owner", "winner")
    _upload_document(client, "qualified-owner")

    def fake_series(a, b, games_per_side, progress):
        result = _result(700, 300, 51, 49, 0)
        progress(result)
        return result

    monkeypatch.setattr(app_module, "run_balanced_series", fake_series)
    response = client.post(
        "/api/qualifiers", headers=_headers("qualified-owner"),
        json={"ai_id": candidate["id"]},
    )
    jobs = [
        _wait_for_job(client, job["id"], user="qualified-owner")
        for job in response.json()["jobs"]
    ]
    assert {job["winner"] for job in jobs} == {"ai_a"}

    legacy = store.create_competition_job(
        "legacy-200-qualified-owner", "qualifier", "contest-qualified-owner",
        "login-qualified-owner", "candidate", "winner", "benchmarks",
        "Baseline + Hunter", 200,
    )
    store.update_competition_job(
        legacy["id"], status="done", completed_games=200, ended_at=time.time()
    )
    listing = _wait_for_qualified(client, "qualified-owner")
    assert set(listing) == {
        "qualified_count", "my_qualified", "technical_document", "my_jobs"
    }
    assert listing["technical_document"]["name"] == "technical-report.pdf"
    assert listing["my_qualified"] is True
    assert {job["total_games"] for job in listing["my_jobs"]} == {100}
    assert listing["qualified_count"] >= 1
    outsider = client.get(
        "/api/qualifiers", headers=_headers("outsider")
    ).json()
    assert set(outsider) == {
        "qualified_count", "my_qualified", "technical_document", "my_jobs"
    }
    assert outsider["qualified_count"] == listing["qualified_count"]
    assert outsider["my_qualified"] is False
    assert outsider["technical_document"] is None
    assert outsider["my_jobs"] == []

    public = client.get(
        "/api/public-ais", headers=_headers("qualified-owner")
    ).json()["ais"]
    assert {"builtin:baseline_ai", "builtin:hunter_ai"} <= {
        item["ai_id"] for item in public
    }
    source = client.get(
        "/api/public-ais/builtin:baseline_ai/source",
        headers=_headers("qualified-owner"),
    )
    assert source.status_code == 200
    assert b'extern "C" void act' in source.content


def test_qualifier_status_and_start_do_not_require_document(client, monkeypatch):
    candidate = _record_ai("document-owner", "candidate")
    passed = {"wins": 60, "losses": 30, "draws": 10}
    store.upsert_qualification(
        candidate["author"], "login-document-owner", "ja-document-owner",
        candidate["id"], candidate["name"], "prior-attempt", True,
        passed, passed,
    )
    assert store.is_author_qualified(candidate["author"]) is True
    listing = client.get(
        "/api/qualifiers", headers=_headers("document-owner")
    ).json()
    assert listing["my_qualified"] is True
    assert listing["technical_document"] is None

    def fake_series(a, b, games_per_side, progress):
        result = _result(600, 300, 60, 30, 10)
        progress(result)
        return result

    monkeypatch.setattr(app_module, "run_balanced_series", fake_series)
    started = client.post(
        "/api/qualifiers", headers=_headers("document-owner"),
        json={"ai_id": candidate["id"]},
    )
    assert started.status_code == 200, started.text
    assert all(
        job["details"]["technical_document"] is None
        for job in started.json()["jobs"]
    )

    invalid = client.post(
        "/api/qualifiers/document", headers=_headers("document-owner"),
        files={"file": ("report.txt", b"plain text", "text/plain")},
    )
    assert invalid.status_code == 400

    metadata = _upload_document(client, "document-owner", "report.pdf")
    assert metadata["size"] == len(b"%PDF-1.7 test report")
    downloaded = client.get(
        "/api/qualifiers/document/download",
        headers=_headers("document-owner"),
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"%PDF-1.7 test report"


def test_admin_can_review_qualifiers_and_download_documents(client, monkeypatch):
    monkeypatch.setenv("SENTRY_DUEL_ADMIN_JACCOUNTS", "ja-admin")
    candidate = _record_ai("admin-review-candidate", "reviewed-ai")
    store.upsert_user_profile(candidate["author"], "参赛选手")
    _upload_document(client, "admin-review-candidate", "review.md")
    passed = {"wins": 61, "losses": 31, "draws": 8}
    store.upsert_qualification(
        candidate["author"], "login-admin-review-candidate",
        "ja-admin-review-candidate", candidate["id"], candidate["name"],
        "admin-review-attempt", True, passed, passed,
    )

    denied = client.get("/api/admin/qualifiers", headers=_headers("outsider"))
    assert denied.status_code == 403
    assert client.get(
        f"/api/admin/technical-documents/{candidate['author']}/download",
        headers=_headers("outsider"),
    ).status_code == 403

    me = client.get("/api/me", headers=_headers("admin")).json()
    assert me["is_admin"] is True
    response = client.get("/api/admin/qualifiers", headers=_headers("admin"))
    assert response.status_code == 200
    reviewed = next(
        item for item in response.json()["participants"]
        if item["participant_id"] == candidate["author"]
    )
    assert reviewed["name"] == "参赛选手"
    assert reviewed["qualified"] is True
    assert reviewed["qualifications"][0]["baseline_wins"] == 61
    assert reviewed["technical_document"]["name"] == "review.md"
    assert "stored_path" not in reviewed["technical_document"]

    downloaded = client.get(
        f"/api/admin/technical-documents/{candidate['author']}/download",
        headers=_headers("admin"),
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"%PDF-1.7 test report"
