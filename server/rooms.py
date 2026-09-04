"""rooms.py - 房间调度

两个 slot(red_slot / blue_slot),各自装一份 AI 元数据。
AI 由上传者绑定,双方都到位后状态: ready,可开赛。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from store import (
    REPLAYS_DIR, DATA_DIR,
    get_ai, get_ai_by_author_name, get_room, record_ai, update_room,
)
from jobs import ENGINE_BIN
from upload import compile_package, compile_source, sanitize_author, sanitize_name

PROJECT_DIR = Path(__file__).parent.parent.resolve()
AI_DIR = PROJECT_DIR / "ai"


def _slot_is_owner(slot: dict, token: str) -> bool:
    """判断这个 token 是否绑定了这个 slot(上传者 token 一致)。"""
    return bool(slot) and slot.get("upload_token") == token


def _check_slot_writeable(code: str, side: str, upload_token: str) -> dict:
    """上传/绑定的公共校验,返回房间 dict。"""
    if side not in ("red", "blue"):
        raise ValueError(f"side 必须是 red/blue: {side}")
    room = get_room(code)
    if not room:
        raise ValueError(f"房间不存在: {code}")
    if room["status"] in ("running", "done", "abandoned"):
        raise ValueError(f"房间已 {room['status']},不能再上传")

    # 创建者固定红方;蓝方只能在红方到位后由另一位玩家占用。
    if side == "red" and upload_token != room["creator_token"]:
        raise ValueError("只有房主能上传红方 AI")
    if side == "blue" and not room.get("red_slot"):
        raise ValueError("请等待房主先上传红方 AI")
    if side == "blue" and upload_token == room["creator_token"]:
        raise ValueError("房主不能占用蓝方,请将房间码交给对手")

    # 已绑定的 slot 仅允许其拥有者覆盖上传。
    slot_data = room.get(f"{side}_slot") or {}
    if slot_data and slot_data.get("upload_token") != upload_token:
        raise ValueError(f"{side} 已被另一位用户占用,不能覆盖")
    return room


def _bind_slot(room: dict, code: str, side: str, upload_token: str,
               name: str, display: str, ai_id, so_path: Path,
               author: Optional[str] = None) -> dict:
    """把编译产物绑定到房间 slot 并推进状态,返回更新后的 room。"""
    new_slot = {
        "ai_id": ai_id,
        "author": sanitize_author(author or upload_token),
        "name": name,
        "display": display,
        "upload_token": upload_token,
        "so_path": str(so_path),
        "uploaded_at": time.time(),
    }
    updates = {f"{side}_slot": new_slot}
    other_slot = room.get(f"{'blue' if side == 'red' else 'red'}_slot")
    if other_slot:
        updates["status"] = "ready"
    else:
        updates["status"] = "waiting_blue" if side == "red" else "waiting_red"
    return update_room(code, **updates)


def upload_to_room(code: str, side: str, upload_token: str,
                   name: str, source: str, display: Optional[str] = None,
                   author: Optional[str] = None) -> dict:
    """把一份 AI 绑定到房间的某个 slot。

    side: 'red' / 'blue'
    upload_token: 这次上传的身份(房间创建者/加入者各自的 token)
    name: AI 选手名
    source: C++ 源码
    display: 显示名(可选)
    """
    name = sanitize_name(name)
    if len(source) > 200_000:
        raise ValueError("源码超过 200KB")

    room = _check_slot_writeable(code, side, upload_token)

    # author 隔离目录 - author 取自 upload_token
    author = sanitize_author(author or upload_token)
    try:
        cpp_path, so_path = compile_source(source, author, name)
    except RuntimeError as e:
        raise RuntimeError(str(e)) from None

    display = display or name
    ai_id = record_ai(author, name, display, str(cpp_path), str(so_path))
    room = _bind_slot(room, code, side, upload_token, name, display,
                      ai_id, so_path, author)
    return {"room": room, "ai_id": ai_id}


def upload_pack_to_room(code: str, side: str, upload_token: str,
                        name: str, data: bytes, filename: str,
                        display: Optional[str] = None,
                        author: Optional[str] = None) -> dict:
    """把多文件源码包(zip/tar.gz)编译并绑定到房间 slot。

    包根有 Makefile 用 Makefile,否则编译项目根全部 .cpp;
    权限/状态/覆盖规则与 upload_to_room 相同。
    """
    name = sanitize_name(name)
    room = _check_slot_writeable(code, side, upload_token)

    author = sanitize_author(author or upload_token)
    try:
        cpp_path, so_path = compile_package(data, filename, author, name)
    except RuntimeError as e:
        raise RuntimeError(str(e)) from None

    display = display or name
    ai_id = record_ai(author, name, display, str(cpp_path), str(so_path))
    room = _bind_slot(room, code, side, upload_token, name, display,
                      ai_id, so_path, author)
    return {"room": room, "ai_id": ai_id}


def bind_existing_to_room(code: str, side: str, upload_token: str,
                          ai_ref: str, requester_author: str) -> dict:
    """Bind a built-in or previously compiled AI to a room slot."""
    if side not in ("red", "blue"):
        raise ValueError(f"side 必须是 red/blue: {side}")
    room = get_room(code)
    if not room:
        raise ValueError(f"房间不存在: {code}")
    if room["status"] in ("running", "done", "abandoned"):
        raise ValueError(f"房间已 {room['status']},不能再绑定")
    if side == "red" and upload_token != room["creator_token"]:
        raise ValueError("只有房主能绑定红方 AI")
    if side == "blue" and not room.get("red_slot"):
        raise ValueError("请等待房主先绑定红方 AI")
    if side == "blue" and upload_token == room["creator_token"]:
        raise ValueError("房主不能占用蓝方")
    old_slot = room.get(f"{side}_slot") or {}
    if old_slot and old_slot.get("upload_token") != upload_token:
        raise ValueError(f"{side} 已被另一位用户占用,不能覆盖")

    if ai_ref.startswith("ai/"):
        filename = Path(ai_ref[3:]).name
        so_path = AI_DIR / filename
        if not so_path.is_file() or so_path.suffix != ".so":
            raise ValueError("内置 AI 不存在")
        name = so_path.stem
        display = name.replace("_ai", "").replace("_", " ").title()
        author = "builtin"
    elif ai_ref.startswith("uploaded/"):
        parts = ai_ref.split("/")
        if len(parts) != 3 or not parts[2].endswith(".so"):
            raise ValueError("已有 AI 标识无效")
        author, filename = parts[1], parts[2]
        name = Path(filename).stem
        ai = get_ai_by_author_name(author, name)
        if not ai or not Path(ai["so_path"]).is_file():
            raise ValueError("上传的 AI 不存在或编译产物已失效")
        if ai["author"] != requester_author and not ai["is_public"]:
            raise ValueError("该 AI 未公开")
        so_path = Path(ai["so_path"])
        display = ai["display"]
    else:
        raise ValueError("已有 AI 标识无效")

    new_slot = {
        "ai_id": ai_ref,
        "author": author,
        "name": name,
        "display": display,
        "upload_token": upload_token,
        "so_path": str(so_path),
        "uploaded_at": time.time(),
    }
    other_slot = room.get(f"{'blue' if side == 'red' else 'red'}_slot")
    status = "ready" if other_slot else ("waiting_blue" if side == "red" else "waiting_red")
    room = update_room(code, **{f"{side}_slot": new_slot, "status": status})
    return {"room": room, "ai_id": ai_ref, "display": display}


def start_match(code: str, requester_token: str, max_turns: int = 20) -> dict:
    """状态: ready → running,启动 subprocess,返回 {game_id, room}"""
    if max_turns < 1 or max_turns > 50:
        raise ValueError("max_turns 1-50")

    room = get_room(code)
    if not room:
        raise ValueError(f"房间不存在: {code}")
    if requester_token != room["creator_token"]:
        raise ValueError("只有房主能开始对局")
    if room["status"] == "running":
        raise ValueError("房间已在运行")
    if room["status"] != "ready":
        raise ValueError(f"房间未就绪: {room['status']}")
    red = room["red_slot"]
    blue = room["blue_slot"]
    if not (red and blue):
        raise ValueError("双方 AI 还没到位")
    if not Path(red["so_path"]).exists():
        raise ValueError(f"红方 .so 缺失: {red['so_path']}")
    if not Path(blue["so_path"]).exists():
        raise ValueError(f"蓝方 .so 缺失: {blue['so_path']}")

    if not ENGINE_BIN.exists():
        raise RuntimeError(f"引擎未构建: {ENGINE_BIN}")

    # 生成 game_id,写录像
    game_id = f"{int(time.time() * 1000)}-{os.urandom(3).hex()}"
    out_path = REPLAYS_DIR / f"game_{game_id}.json"
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(ENGINE_BIN.parent) + ":" + env.get("LD_LIBRARY_PATH", "")
    replay_file = out_path.open("w")
    proc = subprocess.Popen(
        [str(ENGINE_BIN), "--red", red["so_path"], "--blue", blue["so_path"],
         "--game-id", game_id, "--max-turns", str(max_turns)],
        stdout=replay_file, stderr=subprocess.DEVNULL,
        env=env, cwd=str(str(ENGINE_BIN.parent.parent.parent)),
    )

    update_room(code, status="running", game_id=game_id)
    # 等结束再改 done - 复用 app.py 的 asyncio.create_task 风格
    return {"game_id": game_id, "pid": proc.pid, "room_code": code,
            "red": red, "blue": blue, "proc": proc, "replay_file": replay_file}


def finish_match(code: str, game_id: str, proc, replay_file) -> None:
    """等进程退出,关文件,房间状态 → done"""
    proc.wait()
    replay_file.close()
    update_room(code, status="done")
