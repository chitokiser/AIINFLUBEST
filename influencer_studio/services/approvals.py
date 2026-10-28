"""검수·승인 기록 (Decision Archive)."""

from __future__ import annotations

from ..util import StudioError, new_id, now_iso

DECISIONS = {"APPROVED", "REJECTED", "CHANGES_REQUESTED"}


class ApprovalService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    def request(self, target_type: str, target_id: str, influencer_id: str | None, payload: dict | None = None) -> str:
        existing = self.db.one("SELECT approval_id FROM approvals WHERE target_type = ? AND target_id = ? "
                               "AND decision = 'PENDING'", (target_type, target_id))
        if existing:
            return existing["approval_id"]
        aid = new_id("apr")
        self.db.insert("approvals", {"approval_id": aid, "target_type": target_type, "target_id": target_id,
                                     "influencer_id": influencer_id, "reviewer_id": None, "decision": "PENDING",
                                     "comment": None, "payload_json": payload or {}, "created_at": now_iso(),
                                     "decided_at": None})
        return aid

    def close(self, target_type: str, target_id: str, decision: str, actor: str, comment: str = "") -> None:
        pending = self.db.one("SELECT approval_id FROM approvals WHERE target_type = ? AND target_id = ? "
                              "AND decision = 'PENDING'", (target_type, target_id))
        if pending:
            self.db.update("approvals", "approval_id", pending["approval_id"],
                           {"decision": decision, "reviewer_id": actor, "comment": comment, "decided_at": now_iso()})
        else:
            self.db.insert("approvals", {"approval_id": new_id("apr"), "target_type": target_type,
                                         "target_id": target_id, "influencer_id": None, "reviewer_id": actor,
                                         "decision": decision, "comment": comment, "payload_json": {},
                                         "created_at": now_iso(), "decided_at": now_iso()})

    def list(self, decision: str = "PENDING", target_type: str | None = None, influencer_id: str | None = None,
             limit: int = 100) -> dict:
        sql = ("SELECT a.*, i.name AS influencer_name FROM approvals a LEFT JOIN influencers i "
               "ON i.influencer_id = a.influencer_id WHERE a.decision = ?")
        params: list = [decision]
        if target_type:
            sql += " AND a.target_type = ?"
            params.append(target_type)
        if influencer_id:
            sql += " AND a.influencer_id = ?"
            params.append(influencer_id)
        return {"ok": True, "approvals": self.db.query(sql + " ORDER BY a.created_at LIMIT ?", [*params, limit])}

    def decide(self, approval_id: str, decision: str, actor: str, comment: str = "", force_reason: str = "") -> dict:
        a = self.db.get("approvals", "approval_id", approval_id)
        if not a:
            raise StudioError("NOT_FOUND", f"승인 요청을 찾을 수 없습니다: {approval_id}")
        if a["decision"] != "PENDING":
            raise StudioError("ALREADY_DECIDED", f"이미 처리된 요청입니다 ({a['decision']}).")
        decision = decision.upper()
        if decision not in DECISIONS:
            raise StudioError("INVALID_INPUT", f"decision 은 {sorted(DECISIONS)} 중 하나입니다.")
        self.st.policy.require_decide(actor, a["target_type"])
        tt, tid = a["target_type"], a["target_id"]
        if tt == "STORY":
            if decision == "APPROVED":
                return self.st.stories.approve(tid, actor=actor, force_reason=force_reason)
            if decision == "REJECTED":
                return self.st.stories.reject(tid, actor, comment)
            self.close(tt, tid, decision, actor, comment)
            self.db.update("stories", "story_id", tid, {"status": "DRAFT", "updated_at": now_iso()})
            return {"ok": True, "story_id": tid, "status": "DRAFT", "comment": comment}
        if tt == "CONTENT":
            return self.st.content.decide(tid, decision, actor, comment)
        if tt == "CHARACTER_VERSION":
            if decision == "APPROVED":
                self.st.influencers.apply_proposed_version(tid, actor)
            else:
                self.st.influencers.reject_proposed_version(tid, actor)
            self.close(tt, tid, decision, actor, comment)
            return {"ok": True, "version_id": tid, "decision": decision}
        if tt == "INFLUENCER":
            if decision == "APPROVED":
                self.st.influencers.activate(tid, actor)
            else:
                self.close(tt, tid, decision, actor, comment)
            return {"ok": True, "influencer_id": tid, "decision": decision}
        if tt == "MEMORY":
            self.st.memory.set_status(tid, "ACTIVE" if decision == "APPROVED" else "REJECTED", actor)
            self.close(tt, tid, decision, actor, comment)
            return {"ok": True, "memory_id": tid, "decision": decision}
        raise StudioError("INVALID_INPUT", f"알 수 없는 승인 대상: {tt}")
