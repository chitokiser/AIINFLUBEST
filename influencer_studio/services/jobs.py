"""장시간 작업 관리 (기획서 9.2).

QUEUED → RUNNING → REVIEW_REQUIRED → COMPLETED,  오류 시 FAILED, 취소 시 CANCELLED.
- 같은 idempotency_key 로 다시 요청하면 새 작업을 만들지 않고 기존 작업을 돌려줍니다.
- 실패한 작업은 오류 원인과 재시도 횟수를 기록하고, job_retry 로 다시 실행합니다.
- 재시도 시 이미 만든 결과물(슬롯)은 건너뛰어 중복 생성을 막습니다.
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from ..adapters.media import MediaNotConfigured
from ..util import StudioError, new_id, now_iso


class JobService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db
        self.handlers: dict[str, Callable] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, job_type: str, handler: Callable) -> None:
        self.handlers[job_type] = handler

    def enqueue(self, job_type: str, params: dict, influencer_id: str | None, target_id: str | None,
                idempotency_key: str | None = None, max_retries: int = 2) -> dict:
        if job_type not in self.handlers:
            raise StudioError("INVALID_INPUT", f"알 수 없는 작업 유형: {job_type}")
        if idempotency_key:
            existing = self.db.one("SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,))
            if existing:
                return {**self._public(existing), "deduplicated": True}
        jid = new_id("job")
        ts = now_iso()
        self.db.insert("jobs", {"job_id": jid, "job_type": job_type, "influencer_id": influencer_id,
                                "target_id": target_id, "status": "QUEUED", "idempotency_key": idempotency_key,
                                "params_json": params, "result_json": None, "error": None, "progress": 0,
                                "retry_count": 0, "max_retries": max_retries, "created_at": ts, "updated_at": ts})
        return self._public(self.db.get("jobs", "job_id", jid))

    @staticmethod
    def _public(job: dict) -> dict:
        return {k: job[k] for k in ("job_id", "job_type", "status", "progress", "retry_count", "max_retries", "error",
                                    "result_json", "target_id", "created_at", "updated_at")}

    def get(self, job_id: str) -> dict:
        j = self.db.get("jobs", "job_id", job_id)
        if not j:
            raise StudioError("NOT_FOUND", f"작업을 찾을 수 없습니다: {job_id}")
        return {"ok": True, "job": self._public(j)}

    def list(self, status: str | None = None, limit: int = 50) -> dict:
        sql = "SELECT * FROM jobs"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        rows = self.db.query(sql + " ORDER BY created_at DESC LIMIT ?", [*params, limit])
        return {"ok": True, "jobs": [self._public(r) for r in rows]}

    def cancel(self, job_id: str, actor: str = "agent") -> dict:
        n = self.db.execute("UPDATE jobs SET status = 'CANCELLED', updated_at = ? WHERE job_id = ? "
                            "AND status IN ('QUEUED','RUNNING')", (now_iso(), job_id))
        if not n:
            raise StudioError("INVALID_STATE", "대기 중이거나 실행 중인 작업만 취소할 수 있습니다.")
        self.db.audit(actor, "job.cancel", "JOB", job_id)
        return self.get(job_id)

    def retry(self, job_id: str, actor: str = "agent") -> dict:
        j = self.db.get("jobs", "job_id", job_id)
        if not j:
            raise StudioError("NOT_FOUND", job_id)
        if j["status"] not in ("FAILED", "CANCELLED"):
            raise StudioError("INVALID_STATE", f"{j['status']} 상태의 작업은 재시도할 수 없습니다.")
        if (j["error"] or "").startswith("NOT_CONFIGURED"):
            raise StudioError("NOT_CONFIGURED", "필요한 제작 도구가 연결되지 않은 작업입니다. 도구를 설정한 뒤 다시 요청하세요.")
        self.db.update("jobs", "job_id", job_id, {"status": "QUEUED", "error": None, "updated_at": now_iso(),
                                                  "retry_count": j["retry_count"] + 1})
        self.db.audit(actor, "job.retry", "JOB", job_id)
        return self.get(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        j = self.db.one("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        return bool(j and j["status"] == "CANCELLED")

    # ------------------------------------------------------------------
    def _claim(self) -> dict | None:
        row = self.db.one("SELECT job_id FROM jobs WHERE status = 'QUEUED' ORDER BY created_at LIMIT 1")
        if not row:
            return None
        n = self.db.execute("UPDATE jobs SET status = 'RUNNING', updated_at = ? WHERE job_id = ? AND status = 'QUEUED'",
                            (now_iso(), row["job_id"]))
        return self.db.get("jobs", "job_id", row["job_id"]) if n else None

    def run_one(self) -> dict | None:
        job = self._claim()
        if not job:
            return None
        handler = self.handlers[job["job_type"]]

        def progress(value: float) -> None:
            self.db.execute("UPDATE jobs SET progress = ?, updated_at = ? WHERE job_id = ? AND status = 'RUNNING'",
                            (round(value, 3), now_iso(), job["job_id"]))

        try:
            result = handler(job, progress)
            if self.is_cancelled(job["job_id"]):
                return self.get(job["job_id"])["job"]
            status = "REVIEW_REQUIRED" if result.get("review_required") else "COMPLETED"
            self.db.update("jobs", "job_id", job["job_id"], {"status": status, "result_json": result, "progress": 1,
                                                             "updated_at": now_iso()})
        except MediaNotConfigured as e:
            self.db.update("jobs", "job_id", job["job_id"], {"status": "FAILED", "error": f"NOT_CONFIGURED: {e}",
                                                             "updated_at": now_iso()})
        except Exception as e:  # 작업 하나의 실패가 워커 전체를 멈추지 않도록
            self.db.update("jobs", "job_id", job["job_id"], {
                "status": "FAILED", "error": f"{type(e).__name__}: {e}", "updated_at": now_iso(),
                "result_json": {"trace": traceback.format_exc(limit=3)}})
        return self.get(job["job_id"])["job"]

    def run_pending(self, max_jobs: int = 100) -> list[dict]:
        done = []
        for _ in range(max_jobs):
            r = self.run_one()
            if r is None:
                break
            done.append(r)
        return done

    def recover_stale(self) -> int:
        """프로세스가 비정상 종료되어 RUNNING 으로 남은 작업을 FAILED 로 돌립니다 (재시도 가능)."""
        return self.db.execute("UPDATE jobs SET status = 'FAILED', error = 'INTERRUPTED: 프로세스가 중단되었습니다', "
                               "updated_at = ? WHERE status = 'RUNNING'", (now_iso(),))

    def start_worker(self, tick: Callable[[], None] | None = None, interval: float = 1.0) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            last_tick = 0.0
            while not self._stop.is_set():
                try:
                    if self.run_one() is None:
                        time.sleep(interval)
                    if tick and time.time() - last_tick > 30:
                        last_tick = time.time()
                        tick()
                except Exception:  # 워커는 계속 살아 있어야 한다
                    traceback.print_exc()
                    time.sleep(interval)

        self._thread = threading.Thread(target=loop, name="studio-worker", daemon=True)
        self._thread.start()

    def stop_worker(self) -> None:
        self._stop.set()
