"""rooms.py 权限矩阵 + 重构回归测试。

权限路径用 monkeypatch 的假编译(快),另有一个真实编译的集成用例。
"""
from pathlib import Path

import pytest

import rooms
import store
from conftest import MINIMAL_AI, make_zip


@pytest.fixture
def fake_compile(monkeypatch):
    """跳过真实 g++,直接写假产物。返回已编译记录。"""
    calls = []

    def _mk(author, name):
        d = store.UPLOADS_DIR / author
        d.mkdir(parents=True, exist_ok=True)
        cpp = d / f"{name}.cpp"
        so = d / f"{name}.so"
        cpp.write_text("// fake")
        so.write_bytes(b"\x7fELF-fake")
        calls.append((author, name))
        return cpp, so

    monkeypatch.setattr(rooms, "compile_package",
                        lambda data, filename, author, name: _mk(author, name))
    monkeypatch.setattr(rooms, "compile_source",
                        lambda source, author, name: _mk(author, name))
    return calls


def _mkroom():
    token = store.gen_author_token()
    room = store.create_room(token)
    return room["code"], token


ZIP_OK = make_zip({"my_ai.cpp": MINIMAL_AI.encode()})


class TestUploadPackPermissions:
    def test_room_not_found(self, fake_compile):
        with pytest.raises(ValueError, match="房间不存在"):
            rooms.upload_pack_to_room("ZZZZZZ", "red", "tok", "n", ZIP_OK,
                                      "pack.zip")

    def test_bad_side(self, fake_compile):
        code, token = _mkroom()
        with pytest.raises(ValueError, match="red/blue"):
            rooms.upload_pack_to_room(code, "green", token, "n", ZIP_OK,
                                      "pack.zip")

    def test_red_requires_creator(self, fake_compile):
        code, token = _mkroom()
        with pytest.raises(ValueError, match="只有房主能上传红方"):
            rooms.upload_pack_to_room(code, "red", "someone-else", "n",
                                      ZIP_OK, "pack.zip")

    def test_blue_requires_red_first(self, fake_compile):
        code, token = _mkroom()
        with pytest.raises(ValueError, match="等待房主先上传红方"):
            rooms.upload_pack_to_room(code, "blue", "guest", "n", ZIP_OK,
                                      "pack.zip")

    def test_creator_cannot_take_blue(self, fake_compile):
        code, token = _mkroom()
        rooms.upload_pack_to_room(code, "red", token, "r1", ZIP_OK, "p.zip")
        with pytest.raises(ValueError, match="房主不能占用蓝方"):
            rooms.upload_pack_to_room(code, "blue", token, "b1", ZIP_OK,
                                      "p.zip")

    def test_full_flow_red_then_blue_ready(self, fake_compile):
        code, token = _mkroom()
        r1 = rooms.upload_pack_to_room(code, "red", token, "r1", ZIP_OK,
                                       "p.zip")
        assert r1["room"]["status"] == "waiting_blue"
        assert r1["ai_id"]
        r2 = rooms.upload_pack_to_room(code, "blue", "guest", "b1", ZIP_OK,
                                       "p.zip")
        assert r2["room"]["status"] == "ready"
        slot = r2["room"]["blue_slot"]
        assert slot["name"] == "b1" and slot["author"] == "guest"
        assert Path(slot["so_path"]).exists()

    def test_slot_not_overwritable_by_other(self, fake_compile):
        code, token = _mkroom()
        rooms.upload_pack_to_room(code, "red", token, "r1", ZIP_OK, "p.zip")
        rooms.upload_pack_to_room(code, "blue", "guest", "b1", ZIP_OK, "p.zip")
        # 蓝方已被 guest 占用,第三方拿到房间码也不能覆盖
        with pytest.raises(ValueError, match="不能覆盖"):
            rooms.upload_pack_to_room(code, "blue", "third-party", "b2",
                                      ZIP_OK, "p.zip")

    def test_owner_can_overwrite_own_slot(self, fake_compile):
        code, token = _mkroom()
        rooms.upload_pack_to_room(code, "red", token, "r1", ZIP_OK, "p.zip")
        r = rooms.upload_pack_to_room(code, "red", token, "r2", ZIP_OK,
                                      "p.zip")
        assert r["room"]["red_slot"]["name"] == "r2"
        # 单方重传后另一侧仍空,状态应回到 waiting_blue
        assert r["room"]["status"] == "waiting_blue"

    def test_done_room_rejects_upload(self, fake_compile):
        code, token = _mkroom()
        store.update_room(code, status="done")
        with pytest.raises(ValueError, match="不能再上传"):
            rooms.upload_pack_to_room(code, "red", token, "n", ZIP_OK,
                                      "pack.zip")

    def test_compile_failure_does_not_touch_room(self, fake_compile, monkeypatch):
        def boom(data, filename, author, name):
            raise RuntimeError("编译失败: fake")
        monkeypatch.setattr(rooms, "compile_package", boom)
        code, token = _mkroom()
        with pytest.raises(RuntimeError, match="编译失败"):
            rooms.upload_pack_to_room(code, "red", token, "n", ZIP_OK,
                                      "pack.zip")
        room = store.get_room(code)
        assert room["red_slot"] is None
        assert room["status"] == "waiting_red"

    def test_real_compile_integration(self):
        """不走假编译,完整跑 compile_package → record_ai → 绑定。"""
        code, token = _mkroom()
        r = rooms.upload_pack_to_room(code, "red", token, "realai", ZIP_OK,
                                      "pack.zip")
        slot = r["room"]["red_slot"]
        assert Path(slot["so_path"]).exists()
        ai = store.get_ai(r["ai_id"])
        assert ai["name"] == "realai"
        assert Path(ai["cpp_path"]).exists()


class TestUploadToRoomRegression:
    """_check_slot_writeable/_bind_slot 重构后,旧单文件路径行为不变。"""

    def test_red_requires_creator(self, fake_compile):
        code, token = _mkroom()
        with pytest.raises(ValueError, match="只有房主能上传红方"):
            rooms.upload_to_room(code, "red", "nope", "n", MINIMAL_AI)

    def test_source_size_limit(self, fake_compile):
        code, token = _mkroom()
        with pytest.raises(ValueError, match="200KB"):
            rooms.upload_to_room(code, "red", token, "n", "x" * 200_001)

    def test_full_flow_and_overwrite_rules(self, fake_compile):
        code, token = _mkroom()
        r1 = rooms.upload_to_room(code, "red", token, "r1", MINIMAL_AI)
        assert r1["room"]["status"] == "waiting_blue"
        with pytest.raises(ValueError, match="房主不能占用蓝方"):
            rooms.upload_to_room(code, "blue", token, "b1", MINIMAL_AI)
        r2 = rooms.upload_to_room(code, "blue", "guest", "b1", MINIMAL_AI)
        assert r2["room"]["status"] == "ready"
        with pytest.raises(ValueError, match="不能覆盖"):
            rooms.upload_to_room(code, "blue", "third-party", "b2",
                                 MINIMAL_AI)

    def test_bad_name_rejected(self, fake_compile):
        code, token = _mkroom()
        with pytest.raises(ValueError, match="非法字符"):
            rooms.upload_to_room(code, "red", token, "a/b", MINIMAL_AI)
