"""FastAPI 哨兵大战对局平台 - 单页集成

对局:
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
import hashlib
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from store import (
    init_db, LEADERBOARD_DIR, REPLAYS_DIR, TECHNICAL_DOCS_DIR,
    UPLOADS_DIR as STORE_UPLOAD_DIR,
    create_competition_job, create_competition_jobs,
    create_leaderboard_submission,
    get_competition_job, list_competition_jobs,
    delete_ai, get_ai, get_ai_by_author_name, get_leaderboard_entry,
    get_leaderboard_entry_by_ai_id,
    get_leaderboard_submission, latest_daily_leaderboard_snapshot,
    latest_leaderboard_submission, leaderboard_standings,
    get_technical_document, list_admin_qualifier_participants,
    is_author_qualified, list_ais_for_author, list_leaderboard_entries,
    list_public_ais, list_selectable_ais,
    promote_leaderboard_submission,
    qualified_user_count, record_ai, record_technical_document, set_ai_public,
    save_daily_leaderboard_snapshot, update_competition_job,
    update_leaderboard_submission, upsert_qualification, upsert_user_profile,
)
from jobs import ENGINE_BIN, HUMAN_ENGINE_BIN
from competitions import run_balanced_series

# 旧 API 兼容 - 保留 /api/upload + /api/run 用于本地单方模式
from upload import compile_package, compile_source, sanitize_author, sanitize_name

SERVER_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SERVER_DIR.parent
AI_DIR = PROJECT_DIR / "ai"
DOCS_DIR = PROJECT_DIR / "sentry_duel_docs"

app = FastAPI(title="哨兵大战对局平台")

active_runs: dict[str, tuple] = {}
active_human: dict[str, tuple] = {}
active_competitions: dict[str, asyncio.Task] = {}
active_leaderboard: dict[str, asyncio.Task] = {}


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


def _is_admin_user(user: dict) -> bool:
    configured = os.environ.get("SENTRY_DUEL_ADMIN_JACCOUNTS", "")
    allowed = {item.strip().casefold() for item in configured.split(",") if item.strip()}
    return bool(allowed and str(user.get("jaccount") or "").casefold() in allowed)


async def _require_admin(request: Request) -> dict:
    user = await _contest_user(request)
    if not _is_admin_user(user):
        raise HTTPException(403, "仅赛事管理员可访问")
    return user


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
        "is_admin": _is_admin_user(user),
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

    data, filename, media_type = _ai_source_payload(ai)
    return Response(
        data, media_type=media_type,
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"},
    )


def _ai_source_payload(ai: dict) -> tuple[bytes, str, str]:
    """Read one exact user AI source tree as a downloadable payload."""

    package_dir = STORE_UPLOAD_DIR / ai["author"] / f"pkg_{ai['name']}"
    if package_dir.is_dir():
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for path in sorted(package_dir.rglob("*")):
                if path.is_file() and not path.is_symlink() and path.suffix != ".so":
                    output.write(path, path.relative_to(package_dir))
        filename = f"{ai['name']}-source.zip"
        return archive.getvalue(), filename, "application/zip"

    source_path = Path(ai["cpp_path"])
    if not source_path.is_file():
        raise HTTPException(404, "源码文件不存在")
    return source_path.read_bytes(), f"{ai['name']}.cpp", "text/x-c++src; charset=utf-8"


# ---- AI 排行榜 ----

LEADERBOARD_GAMES_PER_SIDE = 10
leaderboard_snapshot_task: Optional[asyncio.Task] = None


def _leaderboard_submission_metadata(submission: Optional[dict]) -> Optional[dict]:
    if not submission:
        return None
    return {
        "id": submission["id"], "ai_id": submission["ai_id"],
        "ai_name": submission["ai_name"],
        "is_open_source": bool(submission["is_open_source"]),
        "status": submission["status"],
        "total_opponents": submission["total_opponents"],
        "completed_opponents": submission["completed_opponents"],
        "error": submission["error"], "created_at": submission["created_at"],
        "started_at": submission["started_at"], "ended_at": submission["ended_at"],
    }


def _leaderboard_payload(author: str) -> dict:
    standings = leaderboard_standings()
    for entry in standings:
        entry["source_download_url"] = (
            f"api/leaderboard/entries/{entry['ai_id']}/source"
            if entry["is_open_source"] else None
        )
    daily = latest_daily_leaderboard_snapshot()
    my_entry = get_leaderboard_entry(author)
    return {
        "standings": standings,
        "my_entry_ai_id": my_entry["ai_id"] if my_entry else None,
        "my_submission": _leaderboard_submission_metadata(
            latest_leaderboard_submission(author)
        ),
        "latest_snapshot": ({
            "date": daily["snapshot_date"], "created_at": daily["created_at"],
        } if daily else None),
        "games_per_opponent": LEADERBOARD_GAMES_PER_SIDE * 2,
    }


def _snapshot_leaderboard_ai(submission_id: str, ai: dict) -> dict:
    directory = LEADERBOARD_DIR / submission_id
    directory.mkdir(parents=True, exist_ok=False)
    binary = directory / "candidate.so"
    shutil.copy2(Path(ai["so_path"]), binary)
    source_data, source_filename, source_media_type = _ai_source_payload(ai)
    source_suffix = ".zip" if source_media_type == "application/zip" else ".cpp"
    source = directory / f"source{source_suffix}"
    source.write_bytes(source_data)
    return {
        "directory": directory,
        "binary": binary,
        "source": source,
        "source_filename": source_filename,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(source_data).hexdigest(),
    }


async def _run_leaderboard_submission(submission_id: str,
                                      snapshot: dict) -> None:
    submission = get_leaderboard_submission(submission_id)
    if not submission:
        return
    matches = []
    try:
        update_leaderboard_submission(
            submission_id, status="running", started_at=time.time()
        )
        opponents = list_leaderboard_entries(submission["author"])
        for completed, opponent in enumerate(opponents, 1):
            opponent_binary = Path(opponent["binary_path"])
            if not opponent_binary.is_file():
                raise RuntimeError(f"榜内对手 {opponent['ai_name']} 的快照不存在")
            result = await asyncio.to_thread(
                run_balanced_series, snapshot["binary"], opponent_binary,
                LEADERBOARD_GAMES_PER_SIDE, None,
            )
            matches.append({
                "opponent_author": opponent["author"],
                "challenger_wins": result["a_wins"],
                "opponent_wins": result["b_wins"], "draws": result["draws"],
                "challenger_score": result["a_score"],
                "opponent_score": result["b_score"],
            })
            update_leaderboard_submission(
                submission_id, completed_opponents=completed
            )
        promote_leaderboard_submission(
            submission_id, str(snapshot["source"]),
            snapshot["source_filename"], matches,
        )
    except Exception as exc:
        update_leaderboard_submission(
            submission_id, status="failed", error=str(exc)[:500],
            ended_at=time.time(),
        )
    finally:
        active_leaderboard.pop(submission_id, None)


@app.get("/api/leaderboard")
async def get_leaderboard(request: Request):
    user = await _contest_user(request)
    standings = leaderboard_standings()
    save_daily_leaderboard_snapshot(datetime.now().date().isoformat(), standings)
    return _leaderboard_payload(_user_author(user))


@app.post("/api/leaderboard/submissions")
async def submit_to_leaderboard(request: Request):
    user = await _contest_user(request)
    payload = await request.json()
    ai_id = payload.get("ai_id")
    is_open_source = payload.get("is_open_source", False)
    if not isinstance(ai_id, int):
        raise HTTPException(400, "ai_id 必须是整数")
    if not isinstance(is_open_source, bool):
        raise HTTPException(400, "is_open_source 必须是布尔值")
    author = _user_author(user)
    ai = get_ai(ai_id)
    if not ai or ai["author"] != author:
        raise HTTPException(404, "只能提交当前账号拥有的 AI")
    if not Path(ai["so_path"]).is_file():
        raise HTTPException(404, "AI 编译产物不存在")
    submission_id = f"leaderboard-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    snapshot = None
    try:
        snapshot = _snapshot_leaderboard_ai(submission_id, ai)
        submission = create_leaderboard_submission(
            submission_id, author, user["login"], user["jaccount"],
            ai["id"], ai["name"], is_open_source,
            str(snapshot["directory"]), snapshot["binary_sha256"],
            snapshot["source_sha256"],
        )
    except ValueError as exc:
        if snapshot:
            shutil.rmtree(snapshot["directory"], ignore_errors=True)
        raise HTTPException(409, str(exc)) from None
    except HTTPException:
        if snapshot:
            shutil.rmtree(snapshot["directory"], ignore_errors=True)
        raise
    except Exception as exc:
        if snapshot:
            shutil.rmtree(snapshot["directory"], ignore_errors=True)
        raise HTTPException(500, f"创建排行榜快照失败: {exc}") from exc
    task = asyncio.create_task(_run_leaderboard_submission(submission_id, snapshot))
    active_leaderboard[submission_id] = task
    return _leaderboard_submission_metadata(submission)


@app.get("/api/leaderboard/submissions/{submission_id}")
async def leaderboard_submission_status(submission_id: str, request: Request):
    user = await _contest_user(request)
    submission = get_leaderboard_submission(submission_id)
    if not submission or submission["author"] != _user_author(user):
        raise HTTPException(404, "排行榜提交不存在")
    return _leaderboard_submission_metadata(submission)


@app.get("/api/leaderboard/entries/{ai_id}/source")
async def download_leaderboard_source(ai_id: int, request: Request):
    await _contest_user(request)
    entry = get_leaderboard_entry_by_ai_id(ai_id)
    if not entry or not entry["is_open_source"] or not entry["source_path"]:
        raise HTTPException(404, "该榜单 AI 未开源")
    path = Path(entry["source_path"]).resolve()
    if not path.is_relative_to(LEADERBOARD_DIR.resolve()) or not path.is_file():
        raise HTTPException(404, "排行榜源码快照不存在")
    return FileResponse(path, filename=entry["source_filename"])


async def _daily_leaderboard_snapshot_loop() -> None:
    while True:
        now = datetime.now()
        save_daily_leaderboard_snapshot(
            now.date().isoformat(), leaderboard_standings()
        )
        next_day = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        await asyncio.sleep(max(1, (next_day - now).total_seconds()))


@app.on_event("startup")
async def start_daily_leaderboard_snapshots() -> None:
    global leaderboard_snapshot_task
    leaderboard_snapshot_task = asyncio.create_task(
        _daily_leaderboard_snapshot_loop()
    )


@app.on_event("shutdown")
async def stop_daily_leaderboard_snapshots() -> None:
    global leaderboard_snapshot_task
    if leaderboard_snapshot_task:
        leaderboard_snapshot_task.cancel()
        try:
            await leaderboard_snapshot_task
        except asyncio.CancelledError:
            pass
        leaderboard_snapshot_task = None


# ---- 批量测试与入围赛 ----

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


async def _run_batch_competition(job_id: str, ai_a: Path, ai_b: Path) -> None:
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
@app.post("/api/competitions/batch")
async def start_batch_competition(request: Request):
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
    job_id = f"batch-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
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
        _run_batch_competition(job_id, ai_a_snapshot, ai_b_snapshot)
    )
    active_competitions[job_id] = task
    return job


@app.get("/api/competitions/formal")
@app.get("/api/competitions/batch")
async def batch_competitions(request: Request):
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


@app.get("/api/admin/qualifiers")
async def admin_qualifiers(request: Request):
    await _require_admin(request)
    participants = []
    for item in list_admin_qualifier_participants():
        qualifications = []
        for result in item["qualifications"]:
            qualifications.append({
                **result,
                "qualified": bool(result["qualified"]),
            })
        jobs = []
        for job in item["qualifier_jobs"]:
            details = job.get("details") or {}
            jobs.append({
                "id": job["id"],
                "status": job["status"],
                "benchmark": details.get("benchmark", ""),
                "ai_name": job["ai_a_name"],
                "completed_games": job["completed_games"],
                "total_games": job["total_games"],
                "ai_wins": job["ai_a_wins"],
                "opponent_wins": job["ai_b_wins"],
                "draws": job["draws"],
                "error": job["error"],
                "created_at": job["created_at"],
            })
        document = _technical_document_metadata(item["technical_document"])
        participants.append({
            "participant_id": item["participant_id"],
            "name": item["name"] or item["login"] or "未知选手",
            "login": item["login"],
            "jaccount": item["jaccount"],
            "qualified": item["qualified"],
            "ais": [
                {**ai, "is_public": bool(ai["is_public"])}
                for ai in item["ais"]
            ],
            "qualifications": qualifications,
            "technical_document": document,
            "qualifier_jobs": jobs,
        })
    return {
        "summary": {
            "participants": len(participants),
            "evaluated": sum(bool(item["qualifications"]) for item in participants),
            "qualified": sum(item["qualified"] for item in participants),
            "documents": sum(item["technical_document"] is not None for item in participants),
        },
        "participants": participants,
    }


@app.get("/api/admin/technical-documents/{participant_id}/download")
async def admin_download_technical_document(participant_id: str,
                                            request: Request):
    await _require_admin(request)
    try:
        author = sanitize_author(participant_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    document = get_technical_document(author)
    if not document:
        raise HTTPException(404, "该选手尚未上传技术文档")
    path = Path(document["stored_path"]).resolve()
    if not path.is_relative_to(TECHNICAL_DOCS_DIR.resolve()) or not path.is_file():
        raise HTTPException(404, "技术文档文件不存在")
    return FileResponse(
        path, media_type=document["media_type"],
        filename=document["original_name"],
    )


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
    """从当前账号可见的 AI 中选择双方并运行一场对局。"""
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
