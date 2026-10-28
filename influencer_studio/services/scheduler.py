"""⑧ 자동화 스케줄러.

정해진 시간에 스토리 초안(과 승인 시 텍스트·이미지 초안)을 만듭니다. 공개 게시는 하지 않습니다.
"""

from __future__ import annotations

from datetime import datetime

from ..util import StudioError, new_id, now_iso


class SchedulerService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    def set(self, influencer_id: str, run_time: str, task: dict | None = None, enabled: bool = True,
            actor: str = "agent") -> dict:
        self.st.influencers._require(influencer_id)
        try:
            datetime.strptime(run_time, "%H:%M")
        except ValueError:
            raise StudioError("INVALID_INPUT", "run_time 은 HH:MM 형식입니다 (예: 09:00).")
        task = task or {"formats": ["instagram_post", "shorts_script"], "images": 3}
        existing = self.db.one("SELECT schedule_id FROM schedules WHERE influencer_id = ?", (influencer_id,))
        if existing:
            self.db.update("schedules", "schedule_id", existing["schedule_id"],
                           {"run_time": run_time, "task_json": task, "enabled": int(enabled)})
            sid = existing["schedule_id"]
        else:
            sid = new_id("sch")
            self.db.insert("schedules", {"schedule_id": sid, "influencer_id": influencer_id, "run_time": run_time,
                                         "task_json": task, "enabled": int(enabled), "last_run_date": None,
                                         "created_at": now_iso()})
        self.db.audit(actor, "schedule.set", "SCHEDULE", sid, {"run_time": run_time, "task": task})
        return {"ok": True, "schedule_id": sid, "run_time": run_time, "task": task, "enabled": enabled,
                "note": "스케줄은 초안 생성까지만 자동으로 실행합니다. 게시는 승인 후에만 가능합니다."}

    def list(self) -> dict:
        return {"ok": True, "schedules": self.db.query(
            "SELECT s.*, i.name FROM schedules s JOIN influencers i ON i.influencer_id = s.influencer_id")}

    def run_due(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        today, hm = now.date().isoformat(), now.strftime("%H:%M")
        results = []
        for sch in self.db.query("SELECT * FROM schedules WHERE enabled = 1"):
            if sch["run_time"] > hm or sch["last_run_date"] == today:
                continue
            # 먼저 실행 날짜를 기록해 같은 날 중복 실행을 막는다
            n = self.db.execute("UPDATE schedules SET last_run_date = ? WHERE schedule_id = ? "
                                "AND (last_run_date IS NULL OR last_run_date != ?)", (today, sch["schedule_id"], today))
            if not n:
                continue
            results.append(self._run(sch))
        results += [{"publish": r} for r in self.st.publish.run_due()]
        return results

    def _run(self, sch: dict) -> dict:
        task = sch["task_json"] or {}
        out: dict = {"schedule_id": sch["schedule_id"], "influencer_id": sch["influencer_id"]}
        try:
            story = self.st.stories.generate(sch["influencer_id"], topic=task.get("topic", ""), actor="system")
            out["story_id"] = story["story_id"]
            out["story_status"] = story["status"]
            if story["status"] == "APPROVED":
                out["texts"] = self.st.content.generate_text(story["story_id"], task.get("formats"),
                                                             task.get("languages"), actor="system")["contents"]
                if task.get("images"):
                    out["image_job"] = self.st.content.generate_image(story["story_id"], int(task["images"]),
                                                                      actor="system")["job"]["job_id"]
            else:
                out["note"] = "스토리가 검수 대기 상태라 콘텐츠는 승인 후에 만듭니다."
        except StudioError as e:
            out["error"] = e.to_dict()
        self.db.audit("system", "schedule.run", "SCHEDULE", sch["schedule_id"], out)
        return out
