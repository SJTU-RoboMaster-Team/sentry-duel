"""app.py 端到端测试:FastAPI TestClient,真实编译 + 真实引擎对局。"""
import time

import pytest
from fastapi.testclient import TestClient

import store
from app import app
from conftest import MINIMAL_AI, make_zip

ZIP_OK = make_zip({"my_ai.cpp": MINIMAL_AI.encode()})


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _create_room(client):
    r = client.post("/api/rooms")
    assert r.status_code == 200, r.text
    d = r.json()
    return d["code"], d["creator_token"]


def _upload_pack(client, code, side, token, name, data=ZIP_OK,
                 filename="pack.zip"):
    return client.post(
        f"/api/rooms/{code}/upload_pack",
        data={"side": side, "upload_token": token, "name": name,
              "display": name},
        files={"file": (filename, data)},
    )


class TestUploadPackAPI:
    def test_full_two_player_flow(self, client):
        code, token = _create_room(client)

        r = _upload_pack(client, code, "red", token, "red-ai")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ai_id"]
        assert d["room"]["status"] == "waiting_blue"
        # 响应不得泄露 upload_token(私密凭证)
        assert "upload_token" not in d["room"]["red_slot"]

        r = _upload_pack(client, code, "blue", "guest-token", "blue-ai")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["room"]["status"] == "ready"
        assert "upload_token" not in d["room"]["blue_slot"]

        # GET 房间同样不泄露 token
        r = client.get(f"/api/rooms/{code}")
        assert r.status_code == 200
        room = r.json()
        assert "upload_token" not in room["red_slot"]
        assert "upload_token" not in room["blue_slot"]

    def test_wrong_token_red_rejected(self, client):
        code, token = _create_room(client)
        r = _upload_pack(client, code, "red", "not-the-creator", "x")
        assert r.status_code == 400
        assert "只有房主能上传红方" in r.json()["detail"]

    def test_blue_before_red_rejected(self, client):
        code, token = _create_room(client)
        r = _upload_pack(client, code, "blue", "guest", "x")
        assert r.status_code == 400
        assert "等待房主" in r.json()["detail"]

    def test_bad_extension_rejected(self, client):
        code, token = _create_room(client)
        r = _upload_pack(client, code, "red", token, "x",
                         data=b"junk", filename="pack.rar")
        assert r.status_code == 400
        assert "仅支持" in r.json()["detail"]

    def test_oversized_archive_rejected(self, client):
        code, token = _create_room(client)
        big = b"x" * (20 * 1024 * 1024 + 1)
        r = _upload_pack(client, code, "red", token, "x", data=big)
        assert r.status_code == 400
        assert "20MB" in r.json()["detail"]

    def test_broken_source_compile_error(self, client):
        code, token = _create_room(client)
        bad = make_zip({"my_ai.cpp": b"not valid c++ ;;;"})
        r = _upload_pack(client, code, "red", token, "x", data=bad)
        assert r.status_code == 400
        assert "编译失败" in r.json()["detail"]
        # 编译失败不应污染房间状态
        room = client.get(f"/api/rooms/{code}").json()
        assert room["red_slot"] is None
        assert room["status"] == "waiting_red"

    def test_room_not_found(self, client):
        r = _upload_pack(client, "ZZZZZZ", "red", "t", "x")
        assert r.status_code == 400

    def test_case_insensitive_room_code(self, client):
        code, token = _create_room(client)
        r = _upload_pack(client, code.lower(), "red", token, "red-ai")
        assert r.status_code == 200, r.text


class TestFullMatchE2E:
    """双方用源码包上传 → 开赛 → 引擎跑完 → 录像产出。"""

    def test_match_runs_to_done(self, client):
        code, token = _create_room(client)
        assert _upload_pack(client, code, "red", token, "e2e-red").status_code == 200
        assert _upload_pack(client, code, "blue", "guest", "e2e-blue").status_code == 200

        r = client.post(f"/api/rooms/{code}/start",
                        data={"requester_token": token, "max_turns": 4})
        assert r.status_code == 200, r.text
        game_id = r.json()["game_id"]

        deadline = time.time() + 60
        status = None
        while time.time() < deadline:
            status = client.get(f"/api/rooms/{code}").json()["status"]
            if status == "done":
                break
            time.sleep(0.5)
        assert status == "done", f"对局未在 60s 内结束,最终状态: {status}"

        # 录像文件已产出且非空
        replays = list(store.REPLAYS_DIR.glob(f"game_{game_id}.json"))
        assert replays, "录像文件不存在"
        assert replays[0].stat().st_size > 0

        # 录像能通过 state 接口读取
        r = client.get(f"/api/games/{game_id}/state")
        assert r.status_code == 200, r.text
