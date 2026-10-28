"""① AI 인플루언서 관리 (기획서 3장)."""

from __future__ import annotations

import copy
import hashlib

from ..adapters.llm import merge_defaults
from ..adapters.templates import task_influencer_profile
from ..util import StudioError, new_id, now_iso, today
from .policy import CORE_PROFILE_FIELDS

REQUIRED_KEYS = [
    "name", "age", "occupation", "location", "personality", "appearance", "voice", "languages",
    "speech_style", "interests", "background", "long_term_goal", "content_policy", "production",
]


def _deep_merge(base: dict, changes: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class InfluencerService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    # ------------------------------------------------------------------
    def create(self, description: str, overrides: dict | None = None, actor: str = "agent") -> dict:
        self.st.policy.check(actor, "draft")
        if not description or not description.strip():
            raise StudioError("INVALID_INPUT", "인플루언서 설명을 입력하세요.")
        count = self.db.one("SELECT COUNT(*) AS n FROM influencers")["n"]
        payload = {"description": description, "overrides": overrides or {}, "seed": count}
        raw = self.st.llm.complete_json("influencer_profile", payload)
        provider = raw.pop("_provider", self.st.llm.name)
        raw.pop("_fallback_reason", None)
        # 모델 출력이 불완전해도 필수 항목이 비지 않도록 규칙 기반 결과로 보완한다.
        defaults = task_influencer_profile(payload)
        profile = merge_defaults(raw, defaults)
        if overrides:
            profile = _deep_merge(profile, overrides)
        missing = [k for k in REQUIRED_KEYS if not profile.get(k)]
        if missing:
            raise StudioError("PROFILE_INCOMPLETE", f"프로필 필수 항목이 비어 있습니다: {missing}")
        if not isinstance(profile.get("age"), int):
            try:
                profile["age"] = int(str(profile["age"]).strip("세살 "))
            except ValueError:
                profile["age"] = defaults["age"]
        profile["is_virtual"] = True
        profile.setdefault("review_policy", {"story": "manual", "content": "manual"})
        # 이미지 일관성: 인물마다 고정 시드와 캐릭터 프롬프트 블록을 둔다.
        influencer_id = new_id("inf")
        profile.setdefault("production", {})
        profile["production"].setdefault("seed", int(hashlib.sha256(influencer_id.encode()).hexdigest()[:8], 16))
        profile["production"]["character_prompt"] = self.character_prompt(profile)

        ts = now_iso()
        with self.db.tx():
            self.db.insert("influencers", {
                "influencer_id": influencer_id, "name": profile["name"], "handle": profile.get("handle"),
                "status": "DRAFT", "profile_json": profile, "current_version": 1, "created_at": ts, "updated_at": ts,
            })
            self.db.insert("character_versions", {
                "version_id": new_id("cv"), "influencer_id": influencer_id, "version_no": 1, "profile_json": profile,
                "change_reason": "최초 생성", "changed_fields": [], "status": "ACTIVE", "created_by": actor,
                "created_at": ts, "effective_at": ts,
            })
            self.db.insert("world_settings", {
                "world_id": new_id("wld"), "influencer_id": influencer_id, "version_no": 1, "created_at": ts,
                "settings_json": {"location": profile.get("location"), "background": profile.get("background"),
                                  "long_term_goal": profile.get("long_term_goal"), "factions": [], "rules": []},
            })
            approval_id = self.st.approvals.request("INFLUENCER", influencer_id, influencer_id,
                                                    {"summary": f"{profile['name']} 등록 확인"})
            self.db.audit(actor, "influencer.create", "INFLUENCER", influencer_id, {"provider": provider})
        return {
            "ok": True, "influencer_id": influencer_id, "status": "DRAFT", "provider": provider,
            "profile": profile, "approval_id": approval_id,
            "next_step": "설정을 확인한 뒤 influencer_update(status='ACTIVE') 또는 검수함 승인으로 등록을 완료하세요.",
        }

    @staticmethod
    def character_prompt(profile: dict) -> str:
        ap = profile.get("appearance") or {}
        parts = [
            f"{profile.get('age', '')} year old {('woman' if profile.get('gender') == '여성' else 'man')}",
            ap.get("face", ""), ap.get("hair", ""), ap.get("body", ""), ap.get("style", ""),
            ", ".join(ap.get("signature_items") or []),
        ]
        return ", ".join(p for p in parts if p)

    # ------------------------------------------------------------------
    def _require(self, influencer_id: str) -> dict:
        row = self.db.get("influencers", "influencer_id", influencer_id)
        if not row:
            raise StudioError("NOT_FOUND", f"인플루언서를 찾을 수 없습니다: {influencer_id}")
        return row

    def require_active(self, influencer_id: str) -> dict:
        row = self._require(influencer_id)
        if row["status"] != "ACTIVE":
            raise StudioError("INFLUENCER_NOT_ACTIVE",
                              f"{row['name']}은(는) {row['status']} 상태입니다. 먼저 등록을 승인(ACTIVE)하세요.")
        return row

    def get(self, influencer_id: str) -> dict:
        row = self._require(influencer_id)
        world = self.db.one("SELECT * FROM world_settings WHERE influencer_id = ? ORDER BY version_no DESC LIMIT 1",
                            (influencer_id,))
        stats = self.db.one(
            "SELECT COUNT(*) AS stories, SUM(status = 'APPROVED' OR status = 'PUBLISHED') AS approved, "
            "MAX(CASE WHEN status IN ('APPROVED','PUBLISHED') THEN story_date END) AS last_story_date "
            "FROM stories WHERE influencer_id = ?", (influencer_id,))
        open_threads = self.db.query(
            "SELECT event_id, title, occurred_at FROM story_events WHERE influencer_id = ? AND status = 'OPEN' "
            "ORDER BY occurred_at", (influencer_id,))
        pending = self.db.query(
            "SELECT version_id, version_no, changed_fields, change_reason, created_at FROM character_versions "
            "WHERE influencer_id = ? AND status = 'PROPOSED'", (influencer_id,))
        rels = self.db.query(
            "SELECT r.relation_type, e.name, e.entity_type FROM relationships r JOIN entities e ON e.entity_id = r.target_id "
            "WHERE r.influencer_id = ? AND r.status = 'ACTIVE'", (influencer_id,))
        return {"ok": True, "influencer": row, "world": world, "stats": stats, "open_threads": open_threads,
                "relationships": rels, "pending_character_changes": pending}

    def list(self, status: str | None = None) -> dict:
        sql = "SELECT influencer_id, name, handle, status, current_version, created_at, updated_at FROM influencers"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        return {"ok": True, "influencers": self.db.query(sql + " ORDER BY created_at", params)}

    # ------------------------------------------------------------------
    def update(self, influencer_id: str, changes: dict | None = None, reason: str = "", status: str | None = None,
               actor: str = "agent") -> dict:
        row = self._require(influencer_id)
        result: dict = {"ok": True, "influencer_id": influencer_id}
        if status:
            if status == "ACTIVE":
                self.activate(influencer_id, actor)
                result["status"] = "ACTIVE"
            elif status == "ARCHIVED":
                return self.archive(influencer_id, reason or "운영 중지", actor)
            else:
                raise StudioError("INVALID_INPUT", "status 는 ACTIVE 또는 ARCHIVED 만 가능합니다.")
        if not changes:
            return result
        if row["status"] == "ARCHIVED":
            raise StudioError("INFLUENCER_ARCHIVED", "보관된 인플루언서는 수정할 수 없습니다.")
        current = row["profile_json"]
        core = sorted(k for k in changes if k in CORE_PROFILE_FIELDS)
        new_profile = _deep_merge(current, changes)
        new_profile.setdefault("production", {})["character_prompt"] = self.character_prompt(new_profile)
        ts = now_iso()
        version_no = (self.db.one("SELECT MAX(version_no) AS v FROM character_versions WHERE influencer_id = ?",
                                  (influencer_id,))["v"] or 0) + 1
        version_id = new_id("cv")
        needs_review = bool(core) and row["status"] == "ACTIVE" and not self.st.policy.can_decide(actor, "CHARACTER_VERSION")
        if core and row["status"] == "ACTIVE" and not reason:
            raise StudioError("REASON_REQUIRED", "캐릭터 핵심 설정을 바꿀 때는 변경 사유(reason)를 남겨야 합니다.")
        with self.db.tx():
            self.db.insert("character_versions", {
                "version_id": version_id, "influencer_id": influencer_id, "version_no": version_no,
                "profile_json": new_profile, "change_reason": reason or "설정 수정", "changed_fields": sorted(changes),
                "status": "PROPOSED" if needs_review else "ACTIVE", "created_by": actor, "created_at": ts,
                "effective_at": None if needs_review else ts,
            })
            if needs_review:
                approval_id = self.st.approvals.request(
                    "CHARACTER_VERSION", version_id, influencer_id,
                    {"changed_fields": sorted(changes), "core_fields": core, "reason": reason,
                     "before": {k: current.get(k) for k in changes}, "after": {k: new_profile.get(k) for k in changes}})
                self.db.audit(actor, "influencer.propose_change", "CHARACTER_VERSION", version_id, {"fields": core})
                result.update({"status": "PROPOSED", "version_id": version_id, "approval_id": approval_id,
                               "message": "핵심 설정 변경은 운영자 승인 후 반영됩니다."})
                return result
            self._apply_version(influencer_id, version_id, actor, current, new_profile, core, reason)
        result.update({"status": "APPLIED", "version_id": version_id, "version_no": version_no})
        return result

    def _apply_version(self, influencer_id, version_id, actor, before, after, core_fields, reason):
        ts = now_iso()
        v = self.db.get("character_versions", "version_id", version_id)
        self.db.execute("UPDATE character_versions SET status = 'SUPERSEDED' WHERE influencer_id = ? AND status = 'ACTIVE' "
                        "AND version_id != ?", (influencer_id, version_id))
        self.db.update("character_versions", "version_id", version_id, {"status": "ACTIVE", "effective_at": ts})
        self.db.update("influencers", "influencer_id", influencer_id, {
            "profile_json": after, "name": after.get("name"), "handle": after.get("handle"),
            "current_version": v["version_no"], "updated_at": ts})
        if core_fields:
            # 과거 기록을 지우지 않고, 변경 시점과 사유를 사건으로 남긴다 (4.3).
            desc = "; ".join(f"{k}: {before.get(k)} → {after.get(k)}" for k in core_fields)
            self.db.insert("story_events", {
                "event_id": new_id("evt"), "influencer_id": influencer_id, "story_id": None, "occurred_at": today(),
                "title": f"설정 변경 ({', '.join(core_fields)})", "description": f"{desc}. 사유: {reason}",
                "event_type": "CHANGE", "status": "NONE", "fact_type": "FICTION", "created_at": ts,
                "event_data": {"version_id": version_id},
            })
        self.db.audit(actor, "influencer.apply_version", "CHARACTER_VERSION", version_id, {"fields": core_fields})

    def apply_proposed_version(self, version_id: str, actor: str) -> None:
        v = self.db.get("character_versions", "version_id", version_id)
        if not v or v["status"] != "PROPOSED":
            raise StudioError("INVALID_STATE", "승인 대기 중인 캐릭터 변경안이 아닙니다.")
        row = self._require(v["influencer_id"])
        core = [k for k in (v["changed_fields"] or []) if k in CORE_PROFILE_FIELDS]
        with self.db.tx():
            self._apply_version(v["influencer_id"], version_id, actor, row["profile_json"], v["profile_json"], core,
                                v["change_reason"])

    def reject_proposed_version(self, version_id: str, actor: str) -> None:
        self.db.update("character_versions", "version_id", version_id, {"status": "REJECTED"})
        self.db.audit(actor, "influencer.reject_version", "CHARACTER_VERSION", version_id)

    def activate(self, influencer_id: str, actor: str) -> None:
        row = self._require(influencer_id)
        if row["status"] == "ACTIVE":
            return
        if row["status"] == "ARCHIVED" and actor != "admin":
            raise StudioError("FORBIDDEN", "보관된 인플루언서 복구는 운영자만 할 수 있습니다.")
        with self.db.tx():
            self.db.update("influencers", "influencer_id", influencer_id, {"status": "ACTIVE", "updated_at": now_iso()})
            self.db.execute("UPDATE approvals SET decision = 'APPROVED', reviewer_id = ?, decided_at = ? "
                            "WHERE target_type = 'INFLUENCER' AND target_id = ? AND decision = 'PENDING'",
                            (actor, now_iso(), influencer_id))
            self.db.audit(actor, "influencer.activate", "INFLUENCER", influencer_id)

    def archive(self, influencer_id: str, reason: str, actor: str) -> dict:
        self._require(influencer_id)
        with self.db.tx():
            self.db.update("influencers", "influencer_id", influencer_id, {"status": "ARCHIVED", "updated_at": now_iso()})
            self.db.execute("UPDATE schedules SET enabled = 0 WHERE influencer_id = ?", (influencer_id,))
            self.db.audit(actor, "influencer.archive", "INFLUENCER", influencer_id, {"reason": reason})
        return {"ok": True, "influencer_id": influencer_id, "status": "ARCHIVED",
                "message": "활동이 중지되었습니다. 데이터는 삭제되지 않고 보관됩니다."}

    # ------------------------------------------------------------------
    def relate(self, influencer_id: str, other_influencer_id: str, relation_type: str, actor: str = "agent") -> dict:
        """인플루언서 사이의 관계(친구·동료 등). 각자의 아카이브에 상대를 INFLUENCER 엔티티로 등록합니다."""
        a, b = self._require(influencer_id), self._require(other_influencer_id)
        if influencer_id == other_influencer_id:
            raise StudioError("INVALID_INPUT", "자기 자신과는 관계를 설정할 수 없습니다.")
        ts = now_iso()
        with self.db.tx():
            for me, other in ((a, b), (b, a)):
                ent = self.st.memory.upsert_entity(me["influencer_id"], "INFLUENCER", other["name"],
                                                   {"influencer_id": other["influencer_id"]}, None)
                self.db.execute("UPDATE relationships SET status = 'SUPERSEDED' WHERE influencer_id = ? AND target_id = ? "
                                "AND status = 'ACTIVE'", (me["influencer_id"], ent))
                self.db.insert("relationships", {
                    "relationship_id": new_id("rel"), "influencer_id": me["influencer_id"],
                    "source_id": me["influencer_id"], "target_id": ent, "relation_type": relation_type,
                    "sentiment": "positive", "since": today(), "story_id": None, "status": "ACTIVE", "created_at": ts})
            self.db.audit(actor, "influencer.relate", "INFLUENCER", influencer_id,
                          {"other": other_influencer_id, "relation": relation_type})
        return {"ok": True, "relation": relation_type, "between": [a["name"], b["name"]]}
