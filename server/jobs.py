"""jobs.py - 进程隔离的 N 场并发对局管理

每场对局 = 一个 subprocess 跑 runner + 一个 NDJSON 录像文件 + 状态。
多个 job 可以同时跑(各自独立 .so,独立 game_id)。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Optional

PROJECT_DIR = Path(__file__).parent.parent.resolve()
ENGINE_BIN = PROJECT_DIR / "engine" / "build" / "runner"
HUMAN_ENGINE_BIN = PROJECT_DIR / "engine" / "build" / "human_runner"
# 数据目录统一从 store 获取。
from store import REPLAYS_DIR, UPLOADS_DIR as UPLOAD_DIR


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    job_id: str
    red_name: str
    blue_name: str
    red_so: str
    blue_so: str
    max_turns: int = 20
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    proc: Optional[subprocess.Popen] = None
    replay_path: Path = None
    status: JobStatus = JobStatus.PENDING
    error: Optional[str] = None
    winner: Optional[str] = None
    red_score: int = 0
    blue_score: int = 0
    turns: int = 0

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "red_name": self.red_name,
            "blue_name": self.blue_name,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "winner": self.winner,
            "red_score": self.red_score,
            "blue_score": self.blue_score,
            "turns": self.turns,
            "replay_file": self.replay_path.name if self.replay_path else None,
            "error": self.error,
        }


class JobManager:
    """线程安全的对局管理器,所有 job 在内存里追踪。"""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def create_job(self, red_name: str, blue_name: str,
                   red_so: str, blue_so: str,
                   max_turns: int = 20) -> Job:
        job_id = f"{int(time.time() * 1000)}_{len(self._jobs)}"
        replay = REPLAYS_DIR / f"game_{job_id}.json"
        job = Job(
            job_id=job_id,
            red_name=red_name, blue_name=blue_name,
            red_so=red_so, blue_so=blue_so,
            max_turns=max_turns,
            replay_path=replay,
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def start(self, job: Job) -> None:
        if not ENGINE_BIN.exists():
            job.status = JobStatus.FAILED
            job.error = f"引擎未构建: {ENGINE_BIN}"
            return
        if not Path(job.red_so).exists():
            job.status = JobStatus.FAILED
            job.error = f"红方 .so 不存在: {job.red_so}"
            return
        if not Path(job.blue_so).exists():
            job.status = JobStatus.FAILED
            job.error = f"蓝方 .so 不存在: {job.blue_so}"
            return

        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(ENGINE_BIN.parent) + ":" + env.get("LD_LIBRARY_PATH", "")
        # 录像文件 - 引擎 stdout NDJSON 直写
        replay = job.replay_path
        replay.parent.mkdir(parents=True, exist_ok=True)
        out = open(replay, "w")

        try:
            proc = subprocess.Popen(
                [str(ENGINE_BIN), "--red", job.red_so, "--blue", job.blue_so,
                 "--max-turns", str(job.max_turns), "--game-id", job.job_id],
                stdout=out, stderr=subprocess.PIPE, env=env,
                cwd=str(PROJECT_DIR),
            )
            job.proc = proc
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = f"启动失败: {e}"
            out.close()

    def poll(self, job: Job) -> JobStatus:
        """检查 job 是否结束,更新状态。从主线程或后台 poll 线程调用。"""
        if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            return job.status
        if job.proc is None:
            return job.status
        rc = job.proc.poll()
        if rc is None:
            return job.status  # 还在跑
        # 已结束
        job.ended_at = time.time()
        if rc != 0:
            err = job.proc.stderr.read().decode(errors="replace") if job.proc.stderr else ""
            job.status = JobStatus.FAILED
            job.error = f"runner 退出码 {rc}: {err[:500]}"
            return job.status
        # 成功 - 读录像最后一帧拿结果
        try:
            events = _load_replay(job.replay_path)
            over = next((e for e in events if e.get("type") == "game_over"), None)
            if over:
                job.winner = over.get("winner")
                job.red_score = over.get("red_score", 0)
                job.blue_score = over.get("blue_score", 0)
                job.turns = over.get("turns", 0)
        except Exception as e:
            job.error = f"读录像失败: {e}"
        job.status = JobStatus.DONE
        return job.status

    def wait(self, job: Job, timeout: float = 30.0) -> JobStatus:
        """阻塞等到结束或超时。"""
        if job.proc is None:
            return job.status
        try:
            rc = job.proc.wait(timeout=timeout)
            job.ended_at = time.time()
            if rc != 0:
                err = job.proc.stderr.read().decode(errors="replace") if job.proc.stderr else ""
                job.status = JobStatus.FAILED
                job.error = f"runner 退出码 {rc}: {err[:500]}"
            else:
                events = _load_replay(job.replay_path)
                over = next((e for e in events if e.get("type") == "game_over"), None)
                if over:
                    job.winner = over.get("winner")
                    job.red_score = over.get("red_score", 0)
                    job.blue_score = over.get("blue_score", 0)
                    job.turns = over.get("turns", 0)
                job.status = JobStatus.DONE
        except subprocess.TimeoutExpired:
            pass
        return job.status


def _load_replay(path: Path) -> list:
    text = path.read_text().strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


jobs = JobManager()
