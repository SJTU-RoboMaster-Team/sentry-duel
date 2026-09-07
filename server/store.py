"""store.py - 持久化层

SQLite 单文件存 AI 元数据、评测结果与排行榜。
uploads/<author>/<name>.{cpp,so} 存编译产物和源码。

部署提示:把 DATA_DIR 环境变量指到云盘路径即可跨机器持久化。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
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

LEADERBOARD_DIR = DATA_DIR / "leaderboard"
LEADERBOARD_DIR.mkdir(parents=True, exist_ok=True)

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

        CREATE TABLE IF NOT EXISTS user_profiles (
            author TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

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

        CREATE TABLE IF NOT EXISTS leaderboard_entries (
            author TEXT PRIMARY KEY,
            login TEXT NOT NULL,
            ai_id INTEGER NOT NULL,
            ai_name TEXT NOT NULL,
            binary_path TEXT NOT NULL,
            source_path TEXT,
            source_filename TEXT,
            binary_sha256 TEXT NOT NULL,
            source_sha256 TEXT,
            is_open_source INTEGER NOT NULL,
            submitted_at REAL NOT NULL,
            ranked_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leaderboard_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenger_author TEXT NOT NULL,
            opponent_author TEXT NOT NULL,
            challenger_wins INTEGER NOT NULL,
            opponent_wins INTEGER NOT NULL,
            draws INTEGER NOT NULL,
            challenger_score INTEGER NOT NULL,
            opponent_score INTEGER NOT NULL,
            played_at REAL NOT NULL,
            UNIQUE(challenger_author, opponent_author)
        );
        CREATE INDEX IF NOT EXISTS idx_leaderboard_matches_opponent
            ON leaderboard_matches(opponent_author);

        CREATE TABLE IF NOT EXISTS leaderboard_submissions (
            id TEXT PRIMARY KEY,
            author TEXT NOT NULL,
            login TEXT NOT NULL,
            jaccount TEXT NOT NULL,
            ai_id INTEGER NOT NULL,
            ai_name TEXT NOT NULL,
            is_open_source INTEGER NOT NULL,
            status TEXT NOT NULL,
            total_opponents INTEGER NOT NULL,
            completed_opponents INTEGER NOT NULL DEFAULT 0,
            snapshot_dir TEXT NOT NULL,
            binary_sha256 TEXT NOT NULL,
            source_sha256 TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            ended_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_leaderboard_submissions_author
            ON leaderboard_submissions(author, created_at DESC);

        CREATE TABLE IF NOT EXISTS leaderboard_daily_snapshots (
            snapshot_date TEXT PRIMARY KEY,
            standings TEXT NOT NULL,
            created_at REAL NOT NULL
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
        c.execute("""
            UPDATE leaderboard_submissions
            SET status='failed', error='服务重启，排行榜评测中断', ended_at=?
            WHERE status IN ('pending', 'running')
        """, (time.time(),))


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
            """
            SELECT ais.*, user_profiles.display_name AS owner_name
            FROM ais
            LEFT JOIN user_profiles ON user_profiles.author=ais.author
            WHERE ais.is_public=1
            ORDER BY ais.created_at DESC LIMIT 200
            """
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_user_profile(author: str, display_name: str) -> None:
    """记录公开列表所需的作者姓名，不存储或对外暴露 JAccount ID。"""
    name = display_name.strip()
    if not name:
        return
    with _conn() as c:
        c.execute("""
            INSERT INTO user_profiles (author, display_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(author) DO UPDATE SET
                display_name=excluded.display_name,
                updated_at=excluded.updated_at
        """, (author, name, time.time()))


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


# --- 批量测试与入围赛 -------------------------------------------------

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
        leaderboard_active = c.execute(
            "SELECT 1 FROM leaderboard_submissions "
            "WHERE status IN ('pending','running') LIMIT 1"
        ).fetchone()
        if active_for_user:
            raise ValueError("当前账号已有批量比赛正在运行")
        if leaderboard_active:
            raise ValueError("排行榜评测正在运行，请稍后再试")
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


# --- AI 排行榜 ---------------------------------------------------------

_LEADERBOARD_SUBMISSION_FIELDS = {
    "status", "completed_opponents", "error", "started_at", "ended_at",
}


def create_leaderboard_submission(submission_id: str, author: str, login: str,
                                  jaccount: str, ai_id: int, ai_name: str,
                                  is_open_source: bool, snapshot_dir: str,
                                  binary_sha256: str,
                                  source_sha256: Optional[str]) -> dict:
    now = time.time()
    with _lock, _conn() as c:
        if c.execute(
            "SELECT 1 FROM leaderboard_submissions "
            "WHERE status IN ('pending','running') LIMIT 1"
        ).fetchone():
            raise ValueError("已有排行榜评测正在运行，请稍后再试")
        if c.execute(
            "SELECT 1 FROM competition_jobs "
            "WHERE status IN ('pending','running') LIMIT 1"
        ).fetchone():
            raise ValueError("当前有批量测试或入围评测正在运行，请稍后再试")
        recent = c.execute(
            "SELECT COUNT(*) FROM leaderboard_submissions "
            "WHERE author=? AND created_at>=?", (author, now - 24 * 60 * 60)
        ).fetchone()[0]
        if recent >= 3:
            raise ValueError("每个账号 24 小时内最多提交 3 次排行榜评测")
        opponents = c.execute(
            "SELECT COUNT(*) FROM leaderboard_entries WHERE author<>?", (author,)
        ).fetchone()[0]
        c.execute("""
            INSERT INTO leaderboard_submissions
                (id,author,login,jaccount,ai_id,ai_name,is_open_source,status,
                 total_opponents,snapshot_dir,binary_sha256,source_sha256,created_at)
            VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?,?)
        """, (
            submission_id, author, login, jaccount, ai_id, ai_name,
            1 if is_open_source else 0, opponents, snapshot_dir,
            binary_sha256, source_sha256, now,
        ))
    return get_leaderboard_submission(submission_id)


def get_leaderboard_submission(submission_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM leaderboard_submissions WHERE id=?", (submission_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["is_open_source"] = bool(result["is_open_source"])
        return result


def latest_leaderboard_submission(author: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM leaderboard_submissions WHERE author=? "
            "ORDER BY created_at DESC LIMIT 1", (author,)
        ).fetchone()
    return get_leaderboard_submission(row["id"]) if row else None


def update_leaderboard_submission(submission_id: str, **fields) -> Optional[dict]:
    invalid = set(fields) - _LEADERBOARD_SUBMISSION_FIELDS
    if invalid:
        raise ValueError(f"不支持的排行榜任务字段: {', '.join(sorted(invalid))}")
    if not fields:
        return get_leaderboard_submission(submission_id)
    assignments = ",".join(f"{key}=?" for key in fields)
    with _conn() as c:
        c.execute(
            f"UPDATE leaderboard_submissions SET {assignments} WHERE id=?",
            [*fields.values(), submission_id],
        )
    return get_leaderboard_submission(submission_id)


def list_leaderboard_entries(exclude_author: Optional[str] = None) -> list[dict]:
    with _conn() as c:
        if exclude_author is None:
            rows = c.execute(
                "SELECT * FROM leaderboard_entries ORDER BY ranked_at"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM leaderboard_entries WHERE author<>? ORDER BY ranked_at",
                (exclude_author,),
            ).fetchall()
        return [dict(row) for row in rows]


def get_leaderboard_entry(author: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM leaderboard_entries WHERE author=?", (author,)
        ).fetchone()
        return dict(row) if row else None


def get_leaderboard_entry_by_ai_id(ai_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM leaderboard_entries WHERE ai_id=?", (ai_id,)
        ).fetchone()
        return dict(row) if row else None


def promote_leaderboard_submission(submission_id: str,
                                   source_path: Optional[str],
                                   source_filename: Optional[str],
                                   matches: list[dict]) -> None:
    submission = get_leaderboard_submission(submission_id)
    if not submission:
        raise ValueError("排行榜提交不存在")
    author = submission["author"]
    now = time.time()
    binary_path = str(Path(submission["snapshot_dir"]) / "candidate.so")
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM leaderboard_matches "
            "WHERE challenger_author=? OR opponent_author=?", (author, author)
        )
        c.execute("""
            INSERT INTO leaderboard_entries
                (author,login,ai_id,ai_name,binary_path,source_path,source_filename,
                 binary_sha256,source_sha256,is_open_source,submitted_at,ranked_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(author) DO UPDATE SET
                login=excluded.login, ai_id=excluded.ai_id,
                ai_name=excluded.ai_name, binary_path=excluded.binary_path,
                source_path=excluded.source_path,
                source_filename=excluded.source_filename,
                binary_sha256=excluded.binary_sha256,
                source_sha256=excluded.source_sha256,
                is_open_source=excluded.is_open_source,
                submitted_at=excluded.submitted_at, ranked_at=excluded.ranked_at
        """, (
            author, submission["login"], submission["ai_id"],
            submission["ai_name"], binary_path, source_path, source_filename,
            submission["binary_sha256"], submission["source_sha256"],
            1 if submission["is_open_source"] else 0,
            submission["created_at"], now,
        ))
        for match in matches:
            c.execute("""
                INSERT INTO leaderboard_matches
                    (challenger_author,opponent_author,challenger_wins,
                     opponent_wins,draws,challenger_score,opponent_score,played_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                author, match["opponent_author"], match["challenger_wins"],
                match["opponent_wins"], match["draws"],
                match["challenger_score"], match["opponent_score"], now,
            ))
        c.execute("""
            UPDATE leaderboard_submissions
            SET status='done', completed_opponents=total_opponents, ended_at=?
            WHERE id=?
        """, (now, submission_id))


def leaderboard_standings() -> list[dict]:
    with _conn() as c:
        entry_rows = c.execute("""
            SELECT e.*, p.display_name AS owner_name
            FROM leaderboard_entries e
            LEFT JOIN user_profiles p ON p.author=e.author
        """).fetchall()
        match_rows = c.execute("SELECT * FROM leaderboard_matches").fetchall()
    standings: dict[str, dict] = {}
    for row in entry_rows:
        entry = dict(row)
        standings[entry["author"]] = {
            "author": entry["author"], "ai_id": entry["ai_id"],
            "ai_name": entry["ai_name"],
            "public_id": f"AI-{entry['ai_id']:04d}",
            "owner_name": entry["owner_name"] or entry["login"],
            "is_open_source": bool(entry["is_open_source"]),
            "wins": 0, "losses": 0, "draws": 0,
            "score_for": 0, "score_against": 0, "opponents": 0,
            "ranked_at": entry["ranked_at"],
        }
    for row in match_rows:
        match = dict(row)
        challenger = standings.get(match["challenger_author"])
        opponent = standings.get(match["opponent_author"])
        if challenger is None or opponent is None:
            continue
        challenger["wins"] += match["challenger_wins"]
        challenger["losses"] += match["opponent_wins"]
        challenger["draws"] += match["draws"]
        challenger["score_for"] += match["challenger_score"]
        challenger["score_against"] += match["opponent_score"]
        challenger["opponents"] += 1
        opponent["wins"] += match["opponent_wins"]
        opponent["losses"] += match["challenger_wins"]
        opponent["draws"] += match["draws"]
        opponent["score_for"] += match["opponent_score"]
        opponent["score_against"] += match["challenger_score"]
        opponent["opponents"] += 1
    rows = list(standings.values())
    for row in rows:
        games = row["wins"] + row["losses"] + row["draws"]
        row["games"] = games
        row["score_rate"] = ((row["wins"] + 0.5 * row["draws"]) / games
                             if games else None)
        row["win_rate"] = row["wins"] / games if games else None
        row["average_margin"] = (
            (row["score_for"] - row["score_against"]) / games if games else None
        )
    rows.sort(key=lambda row: (
        -(row["score_rate"] if row["score_rate"] is not None else -1),
        -(row["win_rate"] if row["win_rate"] is not None else -1),
        -(row["average_margin"] if row["average_margin"] is not None else -10**9),
        row["ranked_at"], row["public_id"],
    ))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
        row.pop("author")
    return rows


def save_daily_leaderboard_snapshot(snapshot_date: str,
                                    standings: list[dict]) -> bool:
    with _conn() as c:
        cursor = c.execute("""
            INSERT OR IGNORE INTO leaderboard_daily_snapshots
                (snapshot_date,standings,created_at) VALUES (?,?,?)
        """, (
            snapshot_date,
            json.dumps(standings, ensure_ascii=False, separators=(",", ":")),
            time.time(),
        ))
        return cursor.rowcount > 0


def latest_daily_leaderboard_snapshot() -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM leaderboard_daily_snapshots "
            "ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["standings"] = json.loads(result["standings"])
        return result


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


def list_admin_qualifier_participants() -> list[dict]:
    """按参赛者聚合 AI、入围结果、评测任务和技术文档。"""
    with _conn() as c:
        authors = [row["author"] for row in c.execute("""
            SELECT author FROM (
                SELECT author FROM ais
                UNION SELECT author FROM qualifications
                UNION SELECT author FROM technical_documents
                UNION SELECT author FROM competition_jobs WHERE kind='qualifier'
            )
            WHERE author LIKE 'contest-%'
            ORDER BY author
        """).fetchall()]
        participants = []
        for author in authors:
            profile = c.execute(
                "SELECT display_name FROM user_profiles WHERE author=?", (author,)
            ).fetchone()
            ais = [dict(row) for row in c.execute("""
                SELECT id, name, display, created_at, upload_count, is_public
                FROM ais WHERE author=? ORDER BY created_at DESC
            """, (author,)).fetchall()]
            qualifications = [dict(row) for row in c.execute("""
                SELECT login, jaccount, ai_id, ai_name, qualified,
                       baseline_wins, baseline_losses, baseline_draws,
                       hunter_wins, hunter_losses, hunter_draws, evaluated_at
                FROM qualifications WHERE author=? ORDER BY evaluated_at DESC
            """, (author,)).fetchall()]
            document_row = c.execute(
                "SELECT * FROM technical_documents WHERE author=?", (author,)
            ).fetchone()
            document = dict(document_row) if document_row else None
            job_rows = c.execute("""
                SELECT * FROM competition_jobs
                WHERE author=? AND kind='qualifier'
                ORDER BY created_at DESC LIMIT 10
            """, (author,)).fetchall()
            jobs = [_competition_dict(row) for row in job_rows]
            identity = (qualifications[0] if qualifications else None) or document
            if identity is None and jobs:
                identity = jobs[0]
            participants.append({
                "participant_id": author,
                "name": profile["display_name"] if profile else "",
                "login": identity.get("login", "") if identity else "",
                "jaccount": identity.get("jaccount", "") if identity else "",
                "ais": ais,
                "qualifications": qualifications,
                "qualified": any(row["qualified"] for row in qualifications),
                "technical_document": document,
                "qualifier_jobs": jobs,
            })
        return participants
