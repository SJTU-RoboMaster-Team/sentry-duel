"""FastAPI 哨兵大战对局平台 - 单页集成

房间 API(双人对战):
  POST /api/rooms                       - 创建房间,返回 code + creator_token
  GET  /api/rooms/{code}                - 查房间状态
  POST /api/rooms/{code}/upload         - 上传 AI 到某侧(multipart: name, source, side, display?)
  POST /api/rooms/{code}/start          - 双方 AI 到位后开赛,返回 SSE 用 game_id
  POST /api/rooms/{code}/abandon        - 放弃房间(creator_token 验证)

历史(单方跑一场,本地兼容):
  POST /api/run                         - 提交一场(red/blue 为 ai_id,内置或 uploaded)
  POST /api/human/start                 - 启动人机对战(ai + R/B)
  POST /api/human/{game_id}/action      - 写入人类 move/turn/fire/scan

录像:
  GET  /api/games                       - 录像列表
  GET  /api/games/{game_id}/state       - 录像全部事件
  GET  /api/games/{game_id}/stream      - SSE 实时事件

AI 列表(本地兼容):
  GET  /api/ais                         - 内置 + uploaded 列表

用法:
  cd server && python3 -m uvicorn app:app --port 8000
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import AsyncIterator, Optional, TextIO

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from store import (
    init_db, REPLAYS_DIR, TECHNICAL_DOCS_DIR,
    UPLOADS_DIR as STORE_UPLOAD_DIR,
    create_room, get_room, update_room,
    create_competition_job, create_competition_jobs,
    get_competition_job, list_competition_jobs,
    delete_ai, gen_author_token, get_ai, get_ai_by_author_name,
    get_technical_document,
    is_author_qualified, list_ais_for_author, list_public_ais, list_selectable_ais,
    qualified_user_count, record_ai, record_technical_document, set_ai_public,
    update_competition_job, upsert_qualification, upsert_user_profile,
)
from rooms import (upload_to_room, upload_pack_to_room, bind_existing_to_room,
                   start_match, finish_match)
from jobs import ENGINE_BIN, HUMAN_ENGINE_BIN
from competitions import run_balanced_series

# 旧 API 兼容 - 保留 /api/upload + /api/run 用于本地单方模式
from upload import compile_package, compile_source, sanitize_author, sanitize_name

SERVER_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SERVER_DIR.parent
AI_DIR = PROJECT_DIR / "ai"
DOCS_DIR = PROJECT_DIR / "sentry_duel_docs"

app = FastAPI(title="哨兵大战对局平台")

# 全局进程追踪(room 模式下用)
active_runs: dict[str, tuple] = {}
active_human: dict[str, tuple] = {}
active_competitions: dict[str, asyncio.Task] = {}


def _stop_human_session(game_id: str, session: tuple) -> None:
    """Terminate a human game and close its replay file."""
    proc, replay_file, _ = session
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not replay_file.closed:
        replay_file.close()
    active_human.pop(game_id, None)

PUBLIC_BUILTINS = {
    "builtin:baseline_ai": ("Baseline", AI_DIR / "baseline_ai.cpp"),
    "builtin:hunter_ai": ("Hunter", AI_DIR / "hunter_ai.cpp"),
}

TECHNICAL_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
}
MAX_TECHNICAL_DOCUMENT_SIZE = 20 * 1024 * 1024

# 启动时建表
init_db()


async def _contest_user(request: Request) -> dict:
    """通过 Contest Astro session 获取当前 JAccount 用户。"""
    # 本地 demo 默认使用固定身份，不依赖交龙账号服务；部署时可显式打开鉴权。
    if os.environ.get("SENTRY_DUEL_AUTH_REQUIRED", "0") == "0":
        return {"id": "test", "login": "test", "jaccount": "test"}
    cookie = request.headers.get("cookie", "")
    if not cookie:
        raise HTTPException(401, "请先登录交龙账号")

    def fetch_profile() -> dict:
        req = urllib.request.Request(
            "http://127.0.0.1:3000/contest/api/team/me",
            headers={"Cookie": cookie, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                if response.status != 200:
                    raise HTTPException(401, "登录已失效")
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise HTTPException(401, "请先登录交龙账号") from None
            raise HTTPException(503, "登录服务暂不可用") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise HTTPException(503, "登录服务暂不可用") from None

    profile = await asyncio.to_thread(fetch_profile)
    personal = profile.get("personalInfo") or {}
    if personal.get("id") is None:
        raise HTTPException(401, "登录信息无效")
    # Contest 现在已经在 personalInfo 里返回 name(真实姓名)与 jaccountName。
    # name 优先来自 Users 表(JAccount OAuth 首次登录时写入);login/GitHub 用户名作为 fallback。
    real_name = (personal.get("name")
                 or personal.get("jaccountName")
                 or personal.get("jaccountAccount")
                 or personal.get("login")
                 or str(personal["id"]))
    user = {"id": str(personal["id"]),
            "login": personal.get("login") or str(personal["id"]),
            "jaccount": (personal.get("jaccountAccount")
                         or personal.get("jaccountId")
                         or str(personal["id"])),
            "name": real_name,
            "display": real_name}
    upsert_user_profile(_user_author(user), real_name)
    return user


def _user_author(user: dict) -> str:
    return sanitize_author(f"contest-{user['id']}")


# ---- 兼容旧 API: 内置 AI + uploaded AI 全列 ----

def _builtin_ais() -> list[dict]:
    out = []
    for path in sorted(AI_DIR.glob("*.so")):
        name = path.stem
        out.append({
            "name": f"ai/{path.name}",
            "display": name.replace("_ai", "").replace("_", " ").title(),
            "kind": "builtin",
            "author": "builtin",
        })
    return out


def _legacy_uploaded_ais(author: str) -> list[dict]:
    """返回当前账号可选择的上传 AI。"""
    out = []
    for ai in list_selectable_ais(author):
        mine = ai["author"] == author
        out.append({
            "name": f"uploaded/{ai['author']}/{ai['name']}.so",
            "display": f"{ai['name']} ({'我的 AI' if mine else '公开'})",
            "kind": "uploaded",
            "author": ai["author"],
            "ai_id": ai["id"],
            "is_public": bool(ai["is_public"]),
        })
    return out


def _all_legacy_ais(author: str) -> list[dict]:
    return _legacy_uploaded_ais(author) + _builtin_ais()


def _selectable_ai(ref: str, author: str) -> tuple[Path, str]:
    path = _legacy_ai_path(ref, author)
    item = next((ai for ai in _all_legacy_ais(author) if ai["name"] == ref), None)
    if item is None:
        raise HTTPException(404, "AI 不存在或未公开")
    return path, item["display"]


def _legacy_ai_path(name: str, requester_author: str) -> Path:
    """ai/xxx.so  → ai/*.so,uploaded/<author>/<name>.so → uploads/<author>/<name>.so"""
    if not name or "/" not in name:
        raise HTTPException(400, f"AI id 无效: {name}")
    kind, rest = name.split("/", 1)
    if kind == "ai":
        p = AI_DIR / Path(rest).name
    elif kind == "uploaded":
        # uploaded/<author>/<name>.so
        parts = rest.split("/")
        if len(parts) != 2 or not parts[1].endswith(".so"):
            raise HTTPException(400, f"uploaded 路径格式: uploaded/<author>/<name>.so")
        author, fname = parts
        ai = get_ai_by_author_name(author, Path(fname).stem)
        if not ai or (ai["author"] != requester_author and not ai["is_public"]):
            raise HTTPException(404, "AI 不存在或未公开")
        p = Path(ai["so_path"])
    else:
        raise HTTPException(400, f"未知 AI 类型: {kind}")
    if not p.exists():
        raise HTTPException(404, f"AI 不存在: {p}")
    return p


def _load_replay_events(path: Path) -> list:
    text = path.read_text()
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _replay_path(game_id: str) -> Path:
    safe = "".join(c for c in game_id if c.isalnum() or c in "-_.")
    return REPLAYS_DIR / f"game_{safe}.json"


@app.get("/")
async def index():
    return HTMLResponse((SERVER_DIR / "viewer" / "index.html").read_text())


@app.get("/downloads/{document}")
async def download_document(document: str):
    documents = {
        "rules.md": DOCS_DIR / "rules.md",
        "api.md": DOCS_DIR / "api.md",
    }
    path = documents.get(document)
    if path is None or not path.is_file():
        raise HTTPException(404, "文档不存在")
    return FileResponse(path, media_type="text/markdown; charset=utf-8",
                        filename=path.name)


# ---- 房间 API ----

@app.post("/api/rooms")
async def api_create_room(request: Request):
    await _contest_user(request)
    token = gen_author_token()
    room = create_room(token)
    return {
        "code": room["code"],
        "creator_token": token,
        "status": room["status"],
        "red_slot": room["red_slot"],
        "blue_slot": room["blue_slot"],
    }


@app.get("/api/rooms/{code}")
async def api_get_room(code: str):
    room = get_room(code.upper())
    if not room:
        raise HTTPException(404, f"房间 {code} 不存在")
    # 不返回 upload_token(那是私密凭证)
    for slot_key in ("red_slot", "blue_slot"):
        slot = room.get(slot_key)
        if slot and "upload_token" in slot:
            slot = {k: v for k, v in slot.items() if k != "upload_token"}
            room[slot_key] = slot
    return room


@app.post("/api/rooms/{code}/upload")
async def api_upload_to_room(code: str,
                              request: Request,
                              side: str = Form(...),
                              upload_token: str = Form(...),
                              name: str = Form(...),
                              source: str = Form(...),
                              display: str = Form("")):
    code = code.upper()
    try:
        user = await _contest_user(request)
        result = upload_to_room(code, side, upload_token, name, source,
                                display or None, _user_author(user))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    room = result["room"]
    # 清掉对方 slot 的 upload_token 后再返回
    for slot_key in ("red_slot", "blue_slot"):
        slot = room.get(slot_key)
        if slot and "upload_token" in slot:
            slot = {k: v for k, v in slot.items() if k != "upload_token"}
            room[slot_key] = slot
    return {"room": room, "ai_id": result["ai_id"]}


@app.post("/api/rooms/{code}/upload_pack")
async def api_upload_pack_to_room(code: str,
                                  request: Request,
                                  side: str = Form(...),
                                  upload_token: str = Form(...),
                                  name: str = Form(...),
                                  display: str = Form(""),
                                  file: UploadFile = File(...)):
    """上传多文件源码包(zip/tar.gz)到房间某侧:有 Makefile 用 Makefile,
    否则编译项目根全部 .cpp。权限规则与 /upload 相同。"""
    code = code.upper()
    user = await _contest_user(request)
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "压缩包超过 20MB")
    try:
        result = upload_pack_to_room(code, side, upload_token, name, data,
                                     file.filename or "pack.zip", display or None,
                                     _user_author(user))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    room = result["room"]
    # 清掉对方 slot 的 upload_token 后再返回
    for slot_key in ("red_slot", "blue_slot"):
        slot = room.get(slot_key)
        if slot and "upload_token" in slot:
            slot = {k: v for k, v in slot.items() if k != "upload_token"}
            room[slot_key] = slot
    return {"room": room, "ai_id": result["ai_id"]}


@app.post("/api/rooms/{code}/bind")
async def api_bind_existing_to_room(code: str,
                                    request: Request,
                                    side: str = Form(...),
                                    upload_token: str = Form(...),
                                    ai_id: str = Form(...)):
    user = await _contest_user(request)
    code = code.upper()
    try:
        result = bind_existing_to_room(
            code, side, upload_token, ai_id, _user_author(user)
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    room = result["room"]
    for slot_key in ("red_slot", "blue_slot"):
        slot = room.get(slot_key)
        if slot and "upload_token" in slot:
            room[slot_key] = {k: v for k, v in slot.items() if k != "upload_token"}
    return {"room": room, "ai_id": result["ai_id"], "display": result["display"]}


@app.post("/api/rooms/{code}/start")
async def api_start_match(code: str, request: Request,
                          requester_token: str = Form(...),
                          max_turns: int = Form(20)):
    await _contest_user(request)
    code = code.upper()
    try:
        result = start_match(code, requester_token, max_turns)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    game_id = result["game_id"]
    proc = result["proc"]
    replay_file = result["replay_file"]

    active_runs[code] = (proc, replay_file)

    async def _wait_then_done():
        await asyncio.to_thread(proc.wait)
        replay_file.close()
        update_room(code, status="done")
        active_runs.pop(code, None)

    asyncio.create_task(_wait_then_done())
    return {
        "code": code,
        "game_id": game_id,
        "pid": proc.pid,
        "red": result["red"]["name"],
        "blue": result["blue"]["name"],
    }


@app.post("/api/rooms/{code}/abandon")
async def api_abandon_room(code: str, request: Request,
                           requester_token: str = Form(...)):
    # 房间 token 仍用于校验房主,登录校验在路由参数中补充。
    await _contest_user(request)
    code = code.upper()
    room = get_room(code)
    if not room:
        raise HTTPException(404, "房间不存在")
    if room["creator_token"] != requester_token:
        raise HTTPException(403, "只有创建者能放弃房间")
    update_room(code, status="abandoned")
    return {"ok": True}


# ---- 旧兼容 ----

@app.get("/api/ais")
async def list_ais(request: Request):
    user = await _contest_user(request)
    return {"ais": _all_legacy_ais(_user_author(user))}


@app.post("/api/upload")
async def upload_ai(request: Request, name: str = Form(...),
                    source: str = Form(...), author: str = Form("anonymous")):
    """上传 AI 供主指挥台与内置 AI 对战。"""
    user = await _contest_user(request)
    try:
        safe = sanitize_name(name)
        safe_author = _user_author(user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if len(source) > 200_000:
        raise HTTPException(400, "源码超过 200KB")
    try:
        cpp_path, so_path = compile_source(source, safe_author, safe)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    record_ai(safe_author, safe, safe, str(cpp_path), str(so_path))
    return {
        "ai_id": f"uploaded/{safe_author}/{safe}.so",
        "display": f"📤 {safe}",
        "name": safe,
        "kind": "uploaded",
    }


@app.post("/api/upload_pack")
async def upload_ai_pack(request: Request, name: str = Form(...),
                         file: UploadFile = File(...)):
    """上传多文件源码包到 AI 代码库,归属当前 JAccount。"""
    user = await _contest_user(request)
    try:
        safe_name = sanitize_name(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "压缩包超过 20MB")
    author = _user_author(user)
    try:
        cpp_path, so_path = compile_package(data, file.filename or "pack.zip",
                                            author, safe_name)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None
    record_ai(author, safe_name, safe_name, str(cpp_path), str(so_path))
    return {
        "ai_id": f"uploaded/{author}/{safe_name}.so",
        "display": f"📤 {safe_name}",
        "name": safe_name,
        "kind": "uploaded",
    }


@app.get("/api/me")
async def api_me(request: Request):
    """右上角登录状态用:返回当前 Contest JAccount 用户。未登录抛 401。
    display 字段优先级:name(真实姓名) > jaccountAccount > login(GitHub)"""
    user = await _contest_user(request)
    return {
        "id": user["id"],
        "login": user["login"],
        "jaccount": user["jaccount"],
        "name": user.get("name") or user["login"],
        "display": user.get("name") or user["login"],
    }


@app.get("/api/my-ais")
async def list_my_ais(request: Request):
    user = await _contest_user(request)
    author = _user_author(user)
    return {
        "author": author,
        "login": user["login"],
        "jaccount": user["jaccount"],
        "ais": [
            {
                "name": ai["name"],
                "display": ai["display"],
                "ai_id": ai["id"],
                "created_at": ai["created_at"],
                "upload_count": ai["upload_count"],
                "is_public": bool(ai["is_public"]),
            }
            for ai in list_ais_for_author(author)
        ],
    }


@app.patch("/api/my-ais/{ai_id}/visibility")
async def update_my_ai_visibility(ai_id: int, request: Request):
    user = await _contest_user(request)
    payload = await request.json()
    is_public = payload.get("is_public")
    if not isinstance(is_public, bool):
        raise HTTPException(400, "is_public 必须是布尔值")
    if not set_ai_public(ai_id, _user_author(user), is_public):
        raise HTTPException(404, "AI 不存在或不属于当前账号")
    return {"ok": True, "ai_id": ai_id, "is_public": is_public}


@app.get("/api/public-ais")
async def public_ais(request: Request):
    await _contest_user(request)
    builtins = [
        {
            "ai_id": ai_id,
            "name": source.stem,
            "display": display,
            "owner_name": "交龙官方",
            "public_id": ai_id.removeprefix("builtin:").upper(),
            "created_at": source.stat().st_mtime,
        }
        for ai_id, (display, source) in PUBLIC_BUILTINS.items()
        if source.is_file()
    ]
    return {
        "ais": builtins + [
            {
                "ai_id": ai["id"],
                "name": ai["name"],
                "display": ai["display"],
                "owner_name": ai["owner_name"] or "参赛者",
                "public_id": f"AI-{ai['id']:04d}",
                "created_at": ai["created_at"],
            }
            for ai in list_public_ais()
        ]
    }


@app.get("/api/public-ais/{ai_id}/source")
async def download_public_ai_source(ai_id: str, request: Request):
    await _contest_user(request)
    builtin = PUBLIC_BUILTINS.get(ai_id)
    if builtin:
        _, source = builtin
        if not source.is_file():
            raise HTTPException(404, "公开 AI 源码不存在")
        return FileResponse(source, media_type="text/x-c++src; charset=utf-8",
                            filename=source.name)
    try:
        numeric_ai_id = int(ai_id)
    except ValueError:
        raise HTTPException(404, "公开 AI 不存在") from None
    ai = get_ai(numeric_ai_id)
    if not ai or not ai["is_public"]:
        raise HTTPException(404, "公开 AI 不存在")

    package_dir = STORE_UPLOAD_DIR / ai["author"] / f"pkg_{ai['name']}"
    if package_dir.is_dir():
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for path in sorted(package_dir.rglob("*")):
                if path.is_file() and not path.is_symlink() and path.suffix != ".so":
                    output.write(path, path.relative_to(package_dir))
        filename = f"{ai['name']}-source.zip"
        return Response(
            archive.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition":
                     f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"},
        )

    source_path = Path(ai["cpp_path"])
    if not source_path.is_file():
        raise HTTPException(404, "源码文件不存在")
    return FileResponse(source_path, media_type="text/x-c++src; charset=utf-8",
                        filename=f"{ai['name']}.cpp")


# ---- 正式比赛与入围赛 ----

def _competition_snapshot(job_id: str, label: str, source: Path) -> Path:
    directory = STORE_UPLOAD_DIR.parent / "competitions" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{label}.so"
    shutil.copy2(source, target)
    return target


def _series_summary(result: dict) -> dict:
    return {
        "wins": result["a_wins"],
        "losses": result["b_wins"],
        "draws": result["draws"],
        "score": result["a_score"],
        "opponent_score": result["b_score"],
    }


async def _run_official_series(job_id: str, ai_a: Path, ai_b: Path,
                               details: Optional[dict] = None) -> Optional[dict]:
    """Run the one shared 100-game interface and persist its live score."""
    update_competition_job(job_id, status="running", started_at=time.time())

    def progress(result: dict) -> None:
        update_competition_job(
            job_id, completed_games=result["games"],
            ai_a_score=result["a_score"], ai_b_score=result["b_score"],
            ai_a_wins=result["a_wins"], ai_b_wins=result["b_wins"],
            draws=result["draws"],
            details={"games_per_side": 50, **(details or {}),
                     "outcomes": result.get("outcomes", [])},
        )
        time.sleep(0.04)

    try:
        result = await asyncio.to_thread(
            run_balanced_series, ai_a, ai_b, 50, progress
        )
        winner = "ai_a" if result["a_wins"] > result["b_wins"] else "ai_b"
        if result["a_wins"] == result["b_wins"]:
            winner = "draw"
        update_competition_job(
            job_id, status="done", winner=winner, ended_at=time.time(),
            completed_games=result["games"],
            ai_a_score=result["a_score"], ai_b_score=result["b_score"],
            ai_a_wins=result["a_wins"], ai_b_wins=result["b_wins"],
            draws=result["draws"],
            details={"games_per_side": 50, **(details or {}),
                     "outcomes": result.get("outcomes", [])},
        )
        return result
    except Exception as exc:
        update_competition_job(
            job_id, status="failed", error=str(exc)[:500], ended_at=time.time()
        )
        return None


async def _run_formal_competition(job_id: str, ai_a: Path, ai_b: Path) -> None:
    try:
        await _run_official_series(job_id, ai_a, ai_b)
    finally:
        active_competitions.pop(job_id, None)
        shutil.rmtree(ai_a.parent, ignore_errors=True)


async def _run_qualifier(attempt_id: str, jobs_for_benchmark: dict[str, str],
                         candidate: Path, user: dict, ai_id: int,
                         ai_name: str) -> None:
    try:
        baseline, hunter = await asyncio.gather(
            _run_official_series(
                jobs_for_benchmark["baseline"], candidate,
                AI_DIR / "baseline_ai.so",
                {"attempt_id": attempt_id, "benchmark": "baseline"},
            ),
            _run_official_series(
                jobs_for_benchmark["hunter"], candidate,
                AI_DIR / "hunter_ai.so",
                {"attempt_id": attempt_id, "benchmark": "hunter"},
            ),
        )
        if baseline is None or hunter is None:
            return
        baseline_summary = _series_summary(baseline)
        hunter_summary = _series_summary(hunter)
        qualified = (
            baseline["a_wins"] > baseline["b_wins"]
            and hunter["a_wins"] > hunter["b_wins"]
        )
        upsert_qualification(
            _user_author(user), user["login"], user["jaccount"], ai_id,
            ai_name, attempt_id, qualified, baseline_summary, hunter_summary,
        )
    finally:
        active_competitions.pop(attempt_id, None)
        shutil.rmtree(candidate.parent, ignore_errors=True)


def _technical_document_metadata(document: Optional[dict]) -> Optional[dict]:
    if not document:
        return None
    return {
        "name": document["original_name"],
        "media_type": document["media_type"],
        "size": document["size"],
        "uploaded_at": document["uploaded_at"],
    }


@app.post("/api/competitions/formal")
async def start_formal_competition(request: Request):
    user = await _contest_user(request)
    payload = await request.json()
    ai_a_ref = str(payload.get("ai_a") or "")
    ai_b_ref = str(payload.get("ai_b") or "")
    if not ai_a_ref or not ai_b_ref:
        raise HTTPException(400, "请选择两个 AI")
    # 允许 self-play:两个 AI 相同也可比赛(常见于评估自我博弈稳定性)
    author = _user_author(user)
    ai_a_path, ai_a_name = _selectable_ai(ai_a_ref, author)
    ai_b_path, ai_b_name = _selectable_ai(ai_b_ref, author)
    job_id = f"formal-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    try:
        job = create_competition_job(
            job_id, "formal", author, user["login"], ai_a_ref, ai_a_name,
            ai_b_ref, ai_b_name, 100,
        )
        ai_a_snapshot = _competition_snapshot(job_id, "ai_a", ai_a_path)
        ai_b_snapshot = _competition_snapshot(job_id, "ai_b", ai_b_path)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:
        update_competition_job(
            job_id, status="failed", error=f"创建快照失败: {exc}",
            ended_at=time.time(),
        )
        raise HTTPException(500, "创建参赛 AI 快照失败") from exc
    task = asyncio.create_task(
        _run_formal_competition(job_id, ai_a_snapshot, ai_b_snapshot)
    )
    active_competitions[job_id] = task
    return job


@app.get("/api/competitions/formal")
async def formal_competitions(request: Request):
    user = await _contest_user(request)
    return {"jobs": list_competition_jobs(_user_author(user), "formal")}


@app.get("/api/competitions/{job_id}")
async def competition_status(job_id: str, request: Request):
    user = await _contest_user(request)
    job = get_competition_job(job_id)
    if not job or job["author"] != _user_author(user):
        raise HTTPException(404, "批量比赛不存在")
    return job


@app.post("/api/qualifiers/document")
async def upload_qualifier_document(request: Request,
                                    file: UploadFile = File(...)):
    user = await _contest_user(request)
    author = _user_author(user)
    original_name = Path(file.filename or "").name
    suffix = Path(original_name).suffix.lower()
    media_type = TECHNICAL_DOCUMENT_TYPES.get(suffix)
    if not original_name or not media_type:
        raise HTTPException(400, "技术文档仅支持 PDF、DOCX 或 Markdown")
    data = await file.read(MAX_TECHNICAL_DOCUMENT_SIZE + 1)
    if not data:
        raise HTTPException(400, "技术文档不能为空")
    if len(data) > MAX_TECHNICAL_DOCUMENT_SIZE:
        raise HTTPException(400, "技术文档超过 20MB")

    directory = TECHNICAL_DOCS_DIR / author
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"technical_document{suffix}"
    temporary = directory / f".upload-{secrets.token_hex(6)}{suffix}"
    previous = get_technical_document(author)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        document = record_technical_document(
            author, user["login"], user["jaccount"], original_name,
            str(destination), media_type, len(data),
        )
    finally:
        temporary.unlink(missing_ok=True)

    if previous and Path(previous["stored_path"]) != destination:
        old_path = Path(previous["stored_path"])
        if old_path.parent == directory:
            old_path.unlink(missing_ok=True)
    return {"technical_document": _technical_document_metadata(document)}


@app.get("/api/qualifiers/document/download")
async def download_qualifier_document(request: Request):
    user = await _contest_user(request)
    document = get_technical_document(_user_author(user))
    if not document:
        raise HTTPException(404, "尚未上传技术文档")
    path = Path(document["stored_path"])
    if not path.is_file():
        raise HTTPException(404, "技术文档文件不存在，请重新上传")
    return FileResponse(
        path, media_type=document["media_type"],
        filename=document["original_name"],
    )


@app.post("/api/qualifiers")
async def start_qualifier(request: Request):
    user = await _contest_user(request)
    payload = await request.json()
    ai_id = payload.get("ai_id")
    if not isinstance(ai_id, int):
        raise HTTPException(400, "ai_id 必须是整数")
    author = _user_author(user)
    ai = get_ai(ai_id)
    if not ai or ai["author"] != author:
        raise HTTPException(404, "只能提交当前账号拥有的 AI")
    ai_path = Path(ai["so_path"])
    if not ai_path.is_file():
        raise HTTPException(404, "AI 编译产物不存在")
    technical_document = get_technical_document(author)
    attempt_id = f"qualifier-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    job_ids = {
        "baseline": f"{attempt_id}-baseline",
        "hunter": f"{attempt_id}-hunter",
    }
    try:
        ai_ref = f"uploaded/{author}/{ai['name']}.so"
        jobs = create_competition_jobs([
            {
                "id": job_ids[benchmark], "kind": "qualifier",
                "author": author, "login": user["login"],
                "ai_a_ref": ai_ref, "ai_a_name": ai["display"],
                "ai_b_ref": f"ai/{benchmark}_ai.so",
                "ai_b_name": benchmark.title(), "total_games": 100,
                "details": {
                    "attempt_id": attempt_id,
                    "benchmark": benchmark,
                    "technical_document": (
                        technical_document["original_name"]
                        if technical_document else None
                    ),
                },
            }
            for benchmark in ("baseline", "hunter")
        ])
        snapshot = _competition_snapshot(attempt_id, "candidate", ai_path)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:
        update_competition_job(
            job_ids["baseline"], status="failed", error=f"创建快照失败: {exc}",
            ended_at=time.time(),
        )
        update_competition_job(
            job_ids["hunter"], status="failed", error=f"创建快照失败: {exc}",
            ended_at=time.time(),
        )
        raise HTTPException(500, "创建参赛 AI 快照失败") from exc
    task = asyncio.create_task(
        _run_qualifier(
            attempt_id, job_ids, snapshot, dict(user), ai_id, ai["name"]
        )
    )
    active_competitions[attempt_id] = task
    return {"attempt_id": attempt_id, "jobs": jobs}


@app.get("/api/qualifiers")
async def qualifiers(request: Request):
    user = await _contest_user(request)
    author = _user_author(user)
    return {
        "qualified_count": qualified_user_count(),
        "my_qualified": is_author_qualified(author),
        "technical_document": _technical_document_metadata(
            get_technical_document(author)
        ),
        "my_jobs": [
            job for job in list_competition_jobs(author, "qualifier")
            if job["total_games"] == 100
        ],
    }


@app.delete("/api/my-ais/{ai_name}")
async def delete_my_ai(ai_name: str, request: Request):
    user = await _contest_user(request)
    author = _user_author(user)
    try:
        safe_name = sanitize_name(ai_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    ai = get_ai_by_author_name(author, safe_name)
    if not ai:
        raise HTTPException(404, "AI 不存在或不属于当前账号")
    Path(ai["cpp_path"]).unlink(missing_ok=True)
    Path(ai["so_path"]).unlink(missing_ok=True)
    package_dir = STORE_UPLOAD_DIR / author / f"pkg_{safe_name}"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    if not delete_ai(ai["id"], author):
        raise HTTPException(404, "AI 不存在或不属于当前账号")
    return {"ok": True}


@app.get("/api/games")
async def list_games():
    games = []
    for p in sorted(REPLAYS_DIR.glob("game_*.json"), reverse=True):
        gid = p.stem.removeprefix("game_")
        try:
            events = _load_replay_events(p)
            over = next((e for e in events if e.get("type") == "game_over"), None)
            start = next((e for e in events if e.get("type") == "start"), None)
            games.append({
                "game_id": gid,
                "events": len(events),
                "winner": over.get("winner") if over else None,
                "red_score": over.get("red_score") if over else None,
                "blue_score": over.get("blue_score") if over else None,
                "turns": over.get("turns") if over else None,
                "red_ai": start.get("red", "").rsplit("/", 1)[-1] if start else None,
                "blue_ai": start.get("blue", "").rsplit("/", 1)[-1] if start else None,
                "size_kb": p.stat().st_size // 1024,
                "running": over is None,
            })
        except Exception as e:
            games.append({"game_id": gid, "error": str(e)})
    return {"games": games[:50]}


@app.get("/api/games/{game_id}/state")
async def get_state(game_id: str):
    path = _replay_path(game_id)
    if not path.exists():
        raise HTTPException(404, f"录像 {game_id} 不存在")
    return JSONResponse(_load_replay_events(path))


@app.get("/api/games/{game_id}/stream")
async def stream(game_id: str, request: Request, since: int = 0):
    path = _replay_path(game_id)
    if not path.exists():
        raise HTTPException(404, f"录像 {game_id} 不存在")

    since = max(0, since)
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None:
        try:
            since = max(since, int(last_event_id) + 1)
        except ValueError:
            pass

    async def gen() -> AsyncIterator[bytes]:
        nonlocal since
        last_size = path.stat().st_size
        while not await request.is_disconnected():
            current = _load_replay_events(path)
            while since < len(current):
                event = current[since]
                yield f"id: {since}\ndata: {json.dumps(event)}\n\n".encode()
                since += 1
                if event.get("type") == "game_over":
                    return

            if current and current[-1].get("type") == "game_over":
                return

            await asyncio.sleep(1)
            if await request.is_disconnected():
                return
            cur_size = path.stat().st_size
            if cur_size != last_size:
                last_size = cur_size
            else:
                yield b": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/run")
async def run_match(request: Request):
    """旧单方跑一场 - 仅本地兼容,新流程请用 /api/rooms/*"""
    user = await _contest_user(request)
    requester_author = _user_author(user)
    red = request.query_params.get("red") or None
    blue = request.query_params.get("blue") or None
    max_turns_q = request.query_params.get("max_turns")
    if not red or not blue:
        try:
            form = await request.form()
            red = red or form.get("red")
            blue = blue or form.get("blue")
            max_turns_q = max_turns_q or form.get("max_turns")
        except Exception:
            pass
    max_turns = int(max_turns_q) if max_turns_q else 20
    if max_turns < 1 or max_turns > 50:
        raise HTTPException(400, "回合数 1-50")
    if not ENGINE_BIN.exists():
        raise HTTPException(500, f"引擎未构建: {ENGINE_BIN}")
    try:
        red_path = _legacy_ai_path(red, requester_author)
        blue_path = _legacy_ai_path(blue, requester_author)
    except HTTPException:
        raise

    game_id = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    out_path = _replay_path(game_id)
    if out_path.exists():
        raise HTTPException(409, "生成的 ID 冲突,重试")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(ENGINE_BIN.parent) + ":" + env.get("LD_LIBRARY_PATH", "")
    replay_file = out_path.open("w")
    proc = subprocess.Popen(
        [str(ENGINE_BIN), "--red", str(red_path), "--blue", str(blue_path),
         "--game-id", game_id, "--max-turns", str(max_turns)],
        stdout=replay_file, stderr=subprocess.DEVNULL,
        env=env, cwd=str(PROJECT_DIR),
    )
    active_runs[game_id] = (proc, replay_file)

    async def _wait_then_done():
        await asyncio.to_thread(proc.wait)
        replay_file.close()
        active_runs.pop(game_id, None)

    asyncio.create_task(_wait_then_done())
    return {"game_id": game_id, "pid": proc.pid, "file": str(out_path.name)}


@app.post("/api/human/start")
async def start_human_match(request: Request):
    """启动一局人机对战；人类动作通过 /action 写入引擎 stdin。"""
    user = await _contest_user(request)
    for old_game_id, old_session in list(active_human.items()):
        if old_session[2] == user["id"]:
            if old_session[0].poll() is None:
                raise HTTPException(409, "你已有一局人机对战进行中")
            _stop_human_session(old_game_id, old_session)
    if not HUMAN_ENGINE_BIN.exists():
        raise HTTPException(500, f"人机引擎未构建: {HUMAN_ENGINE_BIN}")
    ai = request.query_params.get("ai") or ""
    side = (request.query_params.get("side") or "R").upper()
    try:
        max_turns = int(request.query_params.get("max_turns") or 20)
    except ValueError:
        raise HTTPException(400, "回合数必须是整数") from None
    if side not in ("R", "B"):
        raise HTTPException(400, "side 必须是 R 或 B")
    if max_turns < 1 or max_turns > 50:
        raise HTTPException(400, "回合数 1-50")
    try:
        ai_path = _legacy_ai_path(ai, _user_author(user))
    except HTTPException:
        raise
    game_id = f"human-{int(time.time() * 1000)}-{secrets.token_hex(2)}"
    out_path = _replay_path(game_id)
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(HUMAN_ENGINE_BIN.parent) + ":" + env.get("LD_LIBRARY_PATH", "")
    replay_file = out_path.open("w")
    try:
        proc = subprocess.Popen(
            [str(HUMAN_ENGINE_BIN), "--ai", str(ai_path), "--human-side", side,
             "--game-id", str(int(time.time() * 1000) % 2147483647),
             "--max-turns", str(max_turns)],
            stdin=subprocess.PIPE, stdout=replay_file, stderr=subprocess.DEVNULL,
            env=env, cwd=str(PROJECT_DIR), text=True, bufsize=1,
        )
    except Exception as exc:
        replay_file.close()
        raise HTTPException(500, f"启动失败: {exc}") from exc
    active_human[game_id] = (proc, replay_file, user["id"])

    async def _wait_human():
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=30 * 60)
        except asyncio.TimeoutError:
            _stop_human_session(game_id, (proc, replay_file, user["id"]))
            return
        _stop_human_session(game_id, (proc, replay_file, user["id"]))

    asyncio.create_task(_wait_human())
    return {"game_id": game_id, "pid": proc.pid, "side": side,
            "ai": ai, "max_turns": max_turns}


@app.post("/api/human/{game_id}/action")
async def human_action(game_id: str, request: Request):
    user = await _contest_user(request)
    session = active_human.get(game_id)
    if not session:
        raise HTTPException(404, "人机对局不存在或已结束")
    proc, _, owner_id = session
    if owner_id != user["id"]:
        raise HTTPException(403, "只有创建者能操作这局人机对战")
    payload = await request.json()
    action = str(payload.get("action") or "").lower()
    arg = str(payload.get("arg") or "").upper()
    if action not in ("move", "back", "turn", "fire", "scan", "end", "quit"):
        raise HTTPException(400, "action 必须是 move/back/turn/fire/scan/end/quit")
    line = action if action != "turn" else f"turn {arg}"
    if action == "turn" and arg not in ("N", "E", "S", "W"):
        raise HTTPException(400, "turn 方向必须是 N/E/S/W")
    if proc.poll() is not None or proc.stdin is None:
        raise HTTPException(409, "对局已结束")
    try:
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise HTTPException(409, "对局已结束") from exc
    return {"ok": True, "action": action, "arg": arg or None}


app.mount("/assets", StaticFiles(directory=str(SERVER_DIR / "assets")), name="assets")
app.mount("/static", StaticFiles(directory=str(SERVER_DIR / "viewer")), name="static")
