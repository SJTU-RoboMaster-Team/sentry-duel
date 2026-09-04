"""store.py - 持久化层

SQLite 单文件存 AI 元数据 + 房间状态。
uploads/<author>/<name>.{cpp,so} 存编译产物和源码。

部署提示:把 DATA_DIR 环境变量指到云盘路径即可跨机器持久化。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

SERVER_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SERVER_DIR.parent

DATA_DIR = Path(os.environ.get("DATA_DIR", str(PROJECT_DIR / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

REPLAYS_DIR = DATA_DIR / "replays"
REPLAYS_DIR.mkdir(parents=True, exist_ok=True)

TECHNICAL_DOCS_DIR = DATA_DIR / "technical_documents"
TECHNICAL_DOCS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "app.db"

# 每个上传者一个 UUID,匿名也用 UUID(浏览器每次刷新不丢失)。
# 实战: 用户填 author 名或自动生成 author_token(浏览器持久化)。
_lock = threading.Lock()


@contextmanager
def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    """启动时调用一次,建表。"""
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            name TEXT NOT NULL,
            display TEXT NOT NULL,
            cpp_path TEXT NOT NULL,
            so_path TEXT NOT NULL,
            created_at REAL NOT NULL,
            upload_count INTEGER NOT NULL DEFAULT 1,
            is_public INTEGER NOT NULL DEFAULT 0,
            UNIQUE(author, name)
        );
        CREATE INDEX IF NOT EXISTS idx_ais_author ON ais(author);

        CREATE TABLE IF NOT EXISTS rooms (
            code TEXT PRIMARY KEY,
            red_slot TEXT,    -- JSON {author,name,display,ai_id} 或 NULL
            blue_slot TEXT,
            status TEXT NOT NULL,    -- waiting_red / waiting_blue / ready / running / done / abandoned
            creator_token TEXT NOT NULL,
            game_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);

        CREATE TABLE IF NOT EXISTS competition_jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            author TEXT NOT NULL,
            login TEXT NOT NULL,
            ai_a_ref TEXT NOT NULL,
            ai_a_name TEXT NOT NULL,
            ai_b_ref TEXT,
            ai_b_name TEXT,
            status TEXT NOT NULL,
            total_games INTEGER NOT NULL,
            completed_games INTEGER NOT NULL DEFAULT 0,
            ai_a_score INTEGER NOT NULL DEFAULT 0,
            ai_b_score INTEGER NOT NULL DEFAULT 0,
            ai_a_wins INTEGER NOT NULL DEFAULT 0,
            ai_b_wins INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            winner TEXT,
            details TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            ended_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_competition_jobs_author
            ON competition_jobs(author, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_competition_jobs_status
            ON competition_jobs(status);

        CREATE TABLE IF NOT EXISTS qualifications (
            author TEXT NOT NULL,
            login TEXT NOT NULL,
            jaccount TEXT NOT NULL,
            ai_id INTEGER NOT NULL,
            ai_name TEXT NOT NULL,
            job_id TEXT NOT NULL,
            qualified INTEGER NOT NULL,
            baseline_wins INTEGER NOT NULL,
            baseline_losses INTEGER NOT NULL,
            baseline_draws INTEGER NOT NULL,
            hunter_wins INTEGER NOT NULL,
            hunter_losses INTEGER NOT NULL,
            hunter_draws INTEGER NOT NULL,
            evaluated_at REAL NOT NULL,
            PRIMARY KEY(author, ai_id)
        );
        CREATE INDEX IF NOT EXISTS idx_qualifications_qualified
            ON qualifications(qualified, evaluated_at DESC);

        CREATE TABLE IF NOT EXISTS technical_documents (
            author TEXT PRIMARY KEY,
            login TEXT NOT NULL,
            jaccount TEXT NOT NULL,
            original_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            media_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            uploaded_at REAL NOT NULL
        );
        """)
        columns = {row["name"] for row in c.execute("PRAGMA table_info(ais)")}
        if "is_public" not in columns:
            c.execute("ALTER TABLE ais ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ais_public ON ais(is_public)")
        c.execute("""
            UPDATE competition_jobs
            SET status='failed', error='服务重启，对局中断', ended_at=?
            WHERE status IN ('pending', 'running')
        """, (time.time(),))


def gen_author_token() -> str:
    """匿名 / 临时身份 - 16 字符十六进制"""
    return secrets.token_hex(8)


def gen_room_code() -> str:
    """6 位大写字母 + 数字,排除易混字符"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        with _conn() as c:
            row = c.execute("SELECT 1 FROM rooms WHERE code=?", (code,)).fetchone()
            if not row:
                return code


# --- AI 元数据 ---------------------------------------------------------

def record_ai(author: str, name: str, display: str,
              cpp_path: str, so_path: str) -> int:
    """新增一份 AI,返回 ai_id。已存在同名则累加 upload_count + 覆盖产物。"""
    now = time.time()
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM ais WHERE author=? AND name=?", (author, name)
        ).fetchone()
        if existing:
            c.execute("""
                UPDATE ais SET cpp_path=?, so_path=?, display=?, upload_count=upload_count+1, created_at=?
                WHERE author=? AND name=?
            """, (cpp_path, so_path, display, now, author, name))
            return existing["id"]
        c.execute("""
            INSERT INTO ais (author, name, display, cpp_path, so_path, created_at, upload_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (author, name, display, cpp_path, so_path, now))
        return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def get_ai(ai_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM ais WHERE id=?", (ai_id,)).fetchone()
        return dict(row) if row else None


def get_ai_by_author_name(author: str, name: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM ais WHERE author=? AND name=?", (author, name)
        ).fetchone()
        return dict(row) if row else None


def list_ais_for_author(author: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM ais WHERE author=? ORDER BY created_at DESC", (author,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_ais() -> list[dict]:
    """平台可见的全部 AI(供房间选对手)"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM ais ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        return [dict(r) for r in rows]


def list_selectable_ais(author: str) -> list[dict]:
    """当前账号可选择的 AI：本人全部作品及其他人的公开作品。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM ais WHERE author=? OR is_public=1 "
            "ORDER BY created_at DESC LIMIT 200", (author,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_public_ais() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM ais WHERE is_public=1 ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        return [dict(r) for r in rows]


def set_ai_public(ai_id: int, author: str, is_public: bool) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE ais SET is_public=? WHERE id=? AND author=?",
            (1 if is_public else 0, ai_id, author),
        )
        return cur.rowcount > 0


def delete_ai(ai_id: int, author: str) -> bool:
    """删除指定作者的 AI 元数据,避免跨账号删除。"""
    with _conn() as c:
        cur = c.execute("DELETE FROM ais WHERE id=? AND author=?", (ai_id, author))
        return cur.rowcount > 0


def so_path_for(ai_id: int) -> Optional[Path]:
    ai = get_ai(ai_id)
    if not ai:
        return None
    p = Path(ai["so_path"])
    return p if p.exists() else None


# --- 正式比赛与入围赛 -------------------------------------------------

_COMPETITION_FIELDS = {
    "status", "completed_games", "ai_a_score", "ai_b_score",
    "ai_a_wins", "ai_b_wins", "draws", "winner", "details",
    "error", "started_at", "ended_at",
}


def _competition_dict(row: sqlite3.Row) -> dict:
    result = dict(row)
    try:
        result["details"] = json.loads(result["details"] or "{}")
    except json.JSONDecodeError:
        result["details"] = {}
    return result


def create_competition_job(job_id: str, kind: str, author: str, login: str,
                           ai_a_ref: str, ai_a_name: str,
                           ai_b_ref: Optional[str], ai_b_name: Optional[str],
                           total_games: int) -> dict:
    return create_competition_jobs([{
        "id": job_id, "kind": kind, "author": author, "login": login,
        "ai_a_ref": ai_a_ref, "ai_a_name": ai_a_name,
        "ai_b_ref": ai_b_ref, "ai_b_name": ai_b_name,
        "total_games": total_games, "details": {},
    }])[0]


def create_competition_jobs(specs: list[dict]) -> list[dict]:
    """Atomically reserve one or two competition slots for one account."""
    if not specs or len(specs) > 2:
        raise ValueError("每次只能创建一到两场比赛")
    authors = {spec["author"] for spec in specs}
    if len(authors) != 1:
        raise ValueError("批量比赛必须属于同一账号")
    author = specs[0]["author"]
    now = time.time()
    with _lock, _conn() as c:
        active_for_user = c.execute(
            "SELECT COUNT(*) FROM competition_jobs "
            "WHERE author=? AND status IN ('pending','running')", (author,)
        ).fetchone()[0]
        active_total = c.execute(
            "SELECT COUNT(*) FROM competition_jobs "
            "WHERE status IN ('pending','running')"
        ).fetchone()[0]
        if active_for_user:
            raise ValueError("当前账号已有批量比赛正在运行")
        if active_total + len(specs) > 2:
            raise ValueError("比赛队列已满，请稍后再试")
        for spec in specs:
            c.execute("""
                INSERT INTO competition_jobs
                    (id,kind,author,login,ai_a_ref,ai_a_name,
                     ai_b_ref,ai_b_name,status,total_games,details,created_at)
                VALUES (?,?,?,?,?,?,?,?, 'pending',?,?,?)
            """, (
                spec["id"], spec["kind"], spec["author"], spec["login"],
                spec["ai_a_ref"], spec["ai_a_name"], spec.get("ai_b_ref"),
                spec.get("ai_b_name"), spec["total_games"],
                json.dumps(spec.get("details") or {}, ensure_ascii=False), now,
            ))
    return [get_competition_job(spec["id"]) for spec in specs]


def get_competition_job(job_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM competition_jobs WHERE id=?", (job_id,)
        ).fetchone()
        return _competition_dict(row) if row else None


def list_competition_jobs(author: str, kind: Optional[str] = None,
                          limit: int = 20) -> list[dict]:
    with _conn() as c:
        if kind:
            rows = c.execute(
                "SELECT * FROM competition_jobs WHERE author=? AND kind=? "
                "ORDER BY created_at DESC LIMIT ?", (author, kind, limit)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM competition_jobs WHERE author=? "
                "ORDER BY created_at DESC LIMIT ?", (author, limit)
            ).fetchall()
        return [_competition_dict(row) for row in rows]


def update_competition_job(job_id: str, **fields) -> Optional[dict]:
    invalid = set(fields) - _COMPETITION_FIELDS
    if invalid:
        raise ValueError(f"不支持的比赛字段: {', '.join(sorted(invalid))}")
    if "details" in fields and not isinstance(fields["details"], str):
        fields["details"] = json.dumps(fields["details"], ensure_ascii=False)
    if not fields:
        return get_competition_job(job_id)
    assignments = ",".join(f"{key}=?" for key in fields)
    with _conn() as c:
        c.execute(
            f"UPDATE competition_jobs SET {assignments} WHERE id=?",
            [*fields.values(), job_id],
        )
    return get_competition_job(job_id)


def upsert_qualification(author: str, login: str, jaccount: str,
                         ai_id: int, ai_name: str, job_id: str,
                         qualified: bool, baseline: dict,
                         hunter: dict) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO qualifications
                (author,login,jaccount,ai_id,ai_name,job_id,qualified,
                 baseline_wins,baseline_losses,baseline_draws,
                 hunter_wins,hunter_losses,hunter_draws,evaluated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(author,ai_id) DO UPDATE SET
                login=excluded.login, jaccount=excluded.jaccount,
                ai_name=excluded.ai_name, job_id=excluded.job_id,
                qualified=excluded.qualified,
                baseline_wins=excluded.baseline_wins,
                baseline_losses=excluded.baseline_losses,
                baseline_draws=excluded.baseline_draws,
                hunter_wins=excluded.hunter_wins,
                hunter_losses=excluded.hunter_losses,
                hunter_draws=excluded.hunter_draws,
                evaluated_at=excluded.evaluated_at
        """, (
            author, login, jaccount, ai_id, ai_name, job_id,
            1 if qualified else 0,
            baseline["wins"], baseline["losses"], baseline["draws"],
            hunter["wins"], hunter["losses"], hunter["draws"], time.time(),
        ))


def is_author_qualified(author: str) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM qualifications WHERE author=? AND qualified=1 LIMIT 1",
            (author,),
        ).fetchone() is not None


def qualified_user_count() -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(DISTINCT author) FROM qualifications WHERE qualified=1"
        ).fetchone()[0]


def record_technical_document(author: str, login: str, jaccount: str,
                              original_name: str, stored_path: str,
                              media_type: str, size: int) -> dict:
    uploaded_at = time.time()
    with _conn() as c:
        c.execute("""
            INSERT INTO technical_documents
                (author,login,jaccount,original_name,stored_path,media_type,size,uploaded_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(author) DO UPDATE SET
                login=excluded.login, jaccount=excluded.jaccount,
                original_name=excluded.original_name,
                stored_path=excluded.stored_path, media_type=excluded.media_type,
                size=excluded.size, uploaded_at=excluded.uploaded_at
        """, (
            author, login, jaccount, original_name, stored_path,
            media_type, size, uploaded_at,
        ))
    return get_technical_document(author)


def get_technical_document(author: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM technical_documents WHERE author=?", (author,)
        ).fetchone()
        return dict(row) if row else None


# --- 房间 ---------------------------------------------------------------

def create_room(creator_token: str) -> dict:
    code = gen_room_code()
    now = time.time()
    with _conn() as c:
        c.execute("""
            INSERT INTO rooms (code, status, creator_token, created_at, updated_at)
            VALUES (?, 'waiting_red', ?, ?, ?)
        """, (code, creator_token, now, now))
    return get_room(code)


def get_room(code: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["red_slot"] = json.loads(d["red_slot"]) if d["red_slot"] else None
        d["blue_slot"] = json.loads(d["blue_slot"]) if d["blue_slot"] else None
        return d


def update_room(code: str, **fields) -> Optional[dict]:
    """更新房间字段;支持 red_slot / blue_slot (dict→JSON) / status / game_id。"""
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("red_slot", "blue_slot"):
            v = json.dumps(v) if v is not None else None
        sets.append(f"{k}=?")
        vals.append(v)
    sets.append("updated_at=?")
    vals.append(time.time())
    vals.append(code)
    with _conn() as c:
        c.execute(f"UPDATE rooms SET {','.join(sets)} WHERE code=?", vals)
    return get_room(code)


def delete_room(code: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM rooms WHERE code=?", (code,))
