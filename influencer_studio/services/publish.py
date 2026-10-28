"""⑥ 콘텐츠 배포 엔진.

- 승인(APPROVED)된 콘텐츠만 준비·예약·실행할 수 있습니다.
- (content_id, platform) 마다 게시 기록은 하나뿐이므로 같은 콘텐츠가 두 번 게시되지 않습니다.
- API 가 연결되지 않은 플랫폼은 MANUAL_REQUIRED 로 표시하고 게시 패키지만 만듭니다.
"""

from __future__ import annotations

import shutil
from datetime import datetime

from ..adapters.publish import PLATFORMS, platform_info
from ..util import StudioError, new_id, now_iso, safe_path


class PublishService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    def _approved_content(self, content_id: str) -> dict:
        c = self.st.content._content(content_id)
        if c["status"] not in ("APPROVED", "PUBLISHED"):
            raise StudioError("CONTENT_NOT_APPROVED", "승인된 콘텐츠만 게시할 수 있습니다. 검수함에서 먼저 승인하세요.",
                              content_status=c["status"])
        human = self.db.one("SELECT reviewer_id FROM approvals WHERE target_type = 'CONTENT' AND target_id = ? "
                            "AND decision = 'APPROVED' ORDER BY decided_at DESC LIMIT 1", (content_id,))
        if not human:
            raise StudioError("CONTENT_NOT_APPROVED", "승인 기록이 없습니다.")
        return c

    def _pub(self, publication_id: str) -> dict:
        p = self.db.get("publications", "publication_id", publication_id)
        if not p:
            raise StudioError("NOT_FOUND", f"게시 기록을 찾을 수 없습니다: {publication_id}")
        return p

    def prepare(self, content_id: str, platform: str | None = None, actor: str = "agent") -> dict:
        c = self._approved_content(content_id)
        platform = platform or c["platform"]
        try:
            info = platform_info(platform)
        except KeyError:
            raise StudioError("INVALID_INPUT", f"알 수 없는 플랫폼: {platform} (가능: {sorted(PLATFORMS)})")
        key = f"{content_id}:{platform}"
        existing = self.db.one("SELECT * FROM publications WHERE idempotency_key = ?", (key,))
        if existing:
            return {"ok": True, "publication": existing, "deduplicated": True}
        pkg = self.st.content.package(c["story_id"], platform=c["platform"], only_approved=True)
        pid = new_id("pub")
        ts = now_iso()
        self.db.insert("publications", {"publication_id": pid, "content_id": content_id, "platform": platform,
                                        "status": "PREPARED", "scheduled_at": None, "published_at": None,
                                        "external_id": None, "idempotency_key": key, "package_path": pkg["path"],
                                        "detail_json": {"auto_publish": info["auto_publish"], "note": info["note"]},
                                        "created_at": ts, "updated_at": ts})
        self.db.audit(actor, "publish.prepare", "PUBLICATION", pid, {"platform": platform})
        return {"ok": True, "publication": self._pub(pid), "platform_support": info}

    def schedule(self, publication_id: str, scheduled_at: str, actor: str = "agent") -> dict:
        p = self._pub(publication_id)
        self._approved_content(p["content_id"])
        if p["status"] not in ("PREPARED", "SCHEDULED"):
            raise StudioError("INVALID_STATE", f"{p['status']} 상태에서는 예약할 수 없습니다.")
        try:
            datetime.fromisoformat(scheduled_at)
        except ValueError:
            raise StudioError("INVALID_INPUT", "scheduled_at 은 ISO 형식(예: 2026-11-01T09:00)이어야 합니다.")
        self.db.update("publications", "publication_id", publication_id,
                       {"status": "SCHEDULED", "scheduled_at": scheduled_at, "updated_at": now_iso()})
        self.db.audit(actor, "publish.schedule", "PUBLICATION", publication_id, {"at": scheduled_at})
        info = PLATFORMS[p["platform"]]
        note = None if info["auto_publish"] else "이 플랫폼은 자동 게시가 연결되지 않아 예약 시간에 MANUAL_REQUIRED 로 바뀝니다."
        return {"ok": True, "publication": self._pub(publication_id), "note": note}

    def execute(self, publication_id: str, actor: str = "agent") -> dict:
        self.st.policy.check(actor, "publish")
        p = self._pub(publication_id)
        if p["status"] == "PUBLISHED":
            return {"ok": True, "publication": p, "deduplicated": True, "message": "이미 게시된 콘텐츠입니다."}
        if p["status"] == "CANCELLED":
            raise StudioError("INVALID_STATE", "취소된 게시 작업입니다.")
        c = self._approved_content(p["content_id"])
        info = PLATFORMS[p["platform"]]
        ts = now_iso()
        if not info["auto_publish"]:
            self.db.update("publications", "publication_id", publication_id, {
                "status": "MANUAL_REQUIRED", "updated_at": ts,
                "detail_json": {**(p["detail_json"] or {}), "instructions":
                                f"{info['label']} 자동 게시가 연결되지 않았습니다. 패키지 폴더의 파일로 직접 게시한 뒤 "
                                "대시보드에서 '게시 완료'를 눌러 주세요.", "note": info["note"]}})
            self.db.audit(actor, "publish.manual_required", "PUBLICATION", publication_id)
            return {"ok": True, "publication": self._pub(publication_id), "supported": False,
                    "package_path": p["package_path"]}
        # local_export: outbox 폴더로 내보내기
        dest = safe_path(self.st.settings.outbox_dir, ts[:10], publication_id)
        if not dest.exists():
            shutil.copytree(p["package_path"], dest)
        with self.db.tx():
            self.db.update("publications", "publication_id", publication_id, {
                "status": "PUBLISHED", "published_at": ts, "external_id": str(dest), "updated_at": ts})
            self._mark_published(c)
            self.db.audit(actor, "publish.execute", "PUBLICATION", publication_id, {"dest": str(dest)})
        return {"ok": True, "publication": self._pub(publication_id), "supported": True}

    def _mark_published(self, content: dict) -> None:
        self.db.update("contents", "content_id", content["content_id"], {"status": "PUBLISHED", "updated_at": now_iso()})
        self.db.execute("UPDATE stories SET status = 'PUBLISHED', updated_at = ? WHERE story_id = ? AND status = 'APPROVED'",
                        (now_iso(), content["story_id"]))

    def confirm_manual(self, publication_id: str, external_url: str = "", actor: str = "admin") -> dict:
        """사람이 직접 게시한 뒤 결과를 기록합니다."""
        if actor != "admin":
            raise StudioError("FORBIDDEN", "수동 게시 완료 확인은 운영자만 할 수 있습니다.")
        p = self._pub(publication_id)
        if p["status"] not in ("MANUAL_REQUIRED", "SCHEDULED", "PREPARED"):
            raise StudioError("INVALID_STATE", f"{p['status']} 상태입니다.")
        c = self._approved_content(p["content_id"])
        with self.db.tx():
            self.db.update("publications", "publication_id", publication_id, {
                "status": "PUBLISHED", "published_at": now_iso(), "external_id": external_url, "updated_at": now_iso()})
            self._mark_published(c)
            self.db.audit(actor, "publish.confirm_manual", "PUBLICATION", publication_id, {"url": external_url})
        return {"ok": True, "publication": self._pub(publication_id)}

    def cancel(self, publication_id: str, actor: str = "agent") -> dict:
        p = self._pub(publication_id)
        if p["status"] == "PUBLISHED":
            raise StudioError("INVALID_STATE", "이미 게시된 콘텐츠는 취소할 수 없습니다.")
        self.db.update("publications", "publication_id", publication_id, {"status": "CANCELLED", "updated_at": now_iso()})
        self.db.audit(actor, "publish.cancel", "PUBLICATION", publication_id)
        return {"ok": True, "publication": self._pub(publication_id)}

    def status(self, publication_id: str | None = None, content_id: str | None = None,
               influencer_id: str | None = None) -> dict:
        if publication_id:
            return {"ok": True, "publications": [self._pub(publication_id)]}
        sql = "SELECT p.*, c.influencer_id, c.content_type, c.language FROM publications p JOIN contents c ON c.content_id = p.content_id WHERE 1=1"
        params: list = []
        if content_id:
            sql += " AND p.content_id = ?"
            params.append(content_id)
        if influencer_id:
            sql += " AND c.influencer_id = ?"
            params.append(influencer_id)
        return {"ok": True, "publications": self.db.query(sql + " ORDER BY p.created_at DESC LIMIT 200", params)}

    def run_due(self) -> list[dict]:
        now = datetime.now().isoformat(timespec="minutes")
        due = self.db.query("SELECT publication_id FROM publications WHERE status = 'SCHEDULED' AND scheduled_at <= ?",
                            (now,))
        results = []
        for d in due:
            try:
                results.append(self.execute(d["publication_id"], actor="system"))
            except StudioError as e:
                self.db.update("publications", "publication_id", d["publication_id"],
                               {"status": "FAILED", "detail_json": e.to_dict(), "updated_at": now_iso()})
                results.append(e.to_dict())
        return results
