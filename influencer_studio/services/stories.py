"""② 세계관·스토리 엔진 (기획서 4장).

스토리 하나를 만드는 순서 (4.2)
1. 프로필 읽기  2. 관련 과거 기억 검색  3. 시간·장소·인물 확인  4. 미해결 사건·안 다룬 소재 분석
5. 초안 생성    6. 모순 검사            7. 검수 정책에 따라 자동/수동 승인
8. 승인 시 아카이브 저장                9. 확정된 스토리에서 콘텐츠 파생
"""

from __future__ import annotations

from datetime import date, timedelta

from ..adapters.llm import LLMError, merge_defaults
from ..adapters.templates import task_story_draft
from ..util import StudioError, new_id, now_iso, today

EVENT_TYPES = {"PROMISE", "MILESTONE", "CONFLICT", "DISCOVERY", "MEETING", "CHANGE"}
EVENT_STATUS = {"OPEN", "RESOLVED", "NONE"}


class StoryService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    # ------------------------------------------------------------------
    def _require(self, story_id: str) -> dict:
        s = self.db.get("stories", "story_id", story_id)
        if not s:
            raise StudioError("NOT_FOUND", f"스토리를 찾을 수 없습니다: {story_id}")
        return s

    def _next_date(self, influencer_id: str) -> str:
        last = self.db.one("SELECT MAX(story_date) AS d FROM stories WHERE influencer_id = ? "
                           "AND status IN ('APPROVED','PUBLISHED','REVIEW_REQUIRED','DRAFT')", (influencer_id,))["d"]
        if not last:
            return today()
        return (date.fromisoformat(last) + timedelta(days=1)).isoformat()

    def build_context(self, influencer_id: str, topic: str = "", pending: list[dict] | None = None) -> dict:
        """초안 생성에 넣을 맥락. 모든 기록을 넣지 않고 필요한 것만 골라 넣습니다 (5.2)."""
        profile = self.st.influencers._require(influencer_id)["profile_json"]
        recent = self.db.query(
            "SELECT story_id, title, summary, story_date FROM stories WHERE influencer_id = ? "
            "AND status IN ('APPROVED','PUBLISHED') ORDER BY story_date DESC LIMIT 5", (influencer_id,))
        relevant = self.st.memory.search(influencer_id, topic, types=["MEMORY", "STORY"], limit=6)["results"] if topic else []
        people = [{"name": p["name"], "relation": p.get("relation")} for p in self.st.memory.known_people(influencer_id)]
        places = self.st.memory.known_places(influencer_id)
        threads = self.db.query("SELECT event_id, title, description, occurred_at FROM story_events "
                                "WHERE influencer_id = ? AND status = 'OPEN' ORDER BY occurred_at", (influencer_id,))
        # 같은 묶음에서 아직 승인되지 않은 앞선 초안도 연속성 맥락에 포함한다 (시즌 구간 작성).
        for d in pending or []:
            for c in d.get("characters", []):
                if c.get("name") and all(p["name"] != c["name"] for p in people):
                    people.append({"name": c["name"], "relation": c.get("relation")})
            for pl in d.get("places", []):
                hit = next((p for p in places if p["name"] == pl.get("name")), None)
                if not hit:
                    hit = {"name": pl.get("name"), "type": pl.get("type"), "area": pl.get("area"), "visits": [], "items": []}
                    places.append(hit)
                hit["items"] = list({*hit["items"], *[i["name"] for i in d.get("items", []) if i.get("place") == pl.get("name")]})
            # 아직 승인 전인 초안의 약속은 ID 가 없으므로 해결 대상에서 제외하고, 이미 해결한 사건만 뺀다.
            resolved = {ev.get("resolves_event_id") for ev in d.get("events", []) if ev.get("resolves_event_id")}
            threads = [t for t in threads if t["event_id"] not in resolved]
        return {
            "profile": {k: profile.get(k) for k in ("name", "age", "occupation", "location", "personality",
                                                    "speech_style", "interests", "long_term_goal", "content_policy")},
            "recent_stories": recent, "relevant_memories": relevant, "known_people": people,
            "known_places": places, "open_threads": threads,
        }

    def _normalize(self, raw: dict, base: dict) -> dict:
        d = merge_defaults(raw, base)
        for key in ("characters", "places", "items", "events", "memories", "tags", "next_hooks"):
            if not isinstance(d.get(key), list):
                d[key] = base.get(key, [])
        d["characters"] = [c for c in d["characters"] if isinstance(c, dict) and c.get("name")]
        d["places"] = [p for p in d["places"] if isinstance(p, dict) and p.get("name")]
        d["items"] = [i for i in d["items"] if isinstance(i, dict) and i.get("name")]
        events = []
        for ev in d["events"]:
            if not isinstance(ev, dict) or not ev.get("title"):
                continue
            ev["event_type"] = ev.get("event_type") if ev.get("event_type") in EVENT_TYPES else "MILESTONE"
            ev["status"] = ev.get("status") if ev.get("status") in EVENT_STATUS else "NONE"
            if ev.get("resolves_event_id") in ("", "null", None):
                ev.pop("resolves_event_id", None)
            events.append(ev)
        d["events"] = events
        d["memories"] = [m for m in d["memories"] if isinstance(m, str) and m.strip()]
        if not isinstance(d.get("profile_assertions"), dict):
            d["profile_assertions"] = {}
        d["story_date"] = base["story_date"]  # 날짜는 엔진이 정한다 (모델이 바꾸지 못함)
        return d

    # ------------------------------------------------------------------
    def generate(self, influencer_id: str, topic: str = "", story_date: str | None = None, story_type: str = "SERIAL",
                 season_id: str | None = None, day_no: int | None = None, beat: str = "", is_major: bool = False,
                 continue_from: str | None = None, co_influencers: list[str] | None = None, is_flashback: bool = False,
                 pending: list[dict] | None = None, actor: str = "agent") -> dict:
        self.st.policy.check(actor, "draft")
        inf = self.st.influencers.require_active(influencer_id)
        for co in co_influencers or []:
            rel = self.db.one("SELECT 1 FROM relationships r JOIN entities e ON e.entity_id = r.target_id "
                              "WHERE r.influencer_id = ? AND r.status = 'ACTIVE' AND e.entity_type = 'INFLUENCER' "
                              "AND json_extract(e.attributes_json, '$.influencer_id') = ?", (influencer_id, co))
            if not rel:
                raise StudioError("NO_RELATIONSHIP", f"{co} 와(과)의 관계가 없습니다. influencer_relate 로 먼저 설정하세요.")
        story_date = story_date or self._next_date(influencer_id)
        ctx = self.build_context(influencer_id, topic or beat, pending)
        prev = None
        if continue_from:
            prev = self._require(continue_from)
            if prev["influencer_id"] != influencer_id:
                raise StudioError("INVALID_INPUT", "다른 인플루언서의 스토리는 이어 쓸 수 없습니다.")
            ctx["continue_from"] = {"title": prev["title"], "summary": prev["summary"],
                                    "next_hooks": prev["data_json"].get("next_hooks", [])}
            topic = topic or "; ".join(prev["data_json"].get("next_hooks", []))
        seed = self.db.one("SELECT COUNT(*) AS n FROM stories WHERE influencer_id = ?", (influencer_id,))["n"] + (day_no or 0)
        payload = {**ctx, "profile": inf["profile_json"], "topic": topic, "beat": beat, "is_major": is_major,
                   "story_date": story_date, "seed": seed,
                   "exclude_items": [i for p in ctx["known_places"] for i in p["items"]] if "소개하지 않은" in topic else []}
        base = task_story_draft(payload)
        try:
            raw = self.st.llm.complete_json("story_draft", payload)
        except LLMError as e:
            raw = {**base, "_provider": "template", "_fallback_reason": str(e)}
        provider = raw.pop("_provider", self.st.llm.name)
        fallback_reason = raw.pop("_fallback_reason", None)
        draft = self._normalize(raw, base)
        draft.update({"season_id": season_id, "day_no": day_no, "is_flashback": is_flashback,
                      "co_influencers": co_influencers or [], "topic": topic, "beat": beat,
                      "continues_story_id": continue_from})
        validation = self.st.continuity.validate(influencer_id, draft)
        story_id = new_id("sty")
        ts = now_iso()
        with self.db.tx():
            self.db.insert("stories", {
                "story_id": story_id, "influencer_id": influencer_id, "season_id": season_id, "title": draft["title"],
                "summary": draft.get("summary"), "story_date": story_date, "day_no": day_no, "story_type": story_type,
                "status": "REVIEW_REQUIRED", "data_json": draft, "validation_json": validation, "revision_of": None,
                "provider": provider, "created_at": ts, "updated_at": ts, "approved_at": None,
            })
            self.db.insert("episodes", {"episode_id": new_id("epi"), "story_id": story_id, "sequence_no": 1,
                                        "title": draft["title"], "body": draft.get("body", ""),
                                        "summary": draft.get("summary"), "created_at": ts})
            approval_id = self.st.approvals.request("STORY", story_id, influencer_id,
                                                    {"title": draft["title"], "validation_ok": validation["ok"]})
            self.db.audit(actor, "story.generate", "STORY", story_id, {"provider": provider, "fallback": fallback_reason})
        result = {"ok": True, "story_id": story_id, "status": "REVIEW_REQUIRED", "provider": provider,
                  "story": draft, "validation": validation, "approval_id": approval_id}
        if fallback_reason:
            result["provider_note"] = f"언어 모델 대신 템플릿 생성기를 사용했습니다: {fallback_reason}"
        policy = (inf["profile_json"].get("review_policy") or {}).get("story", "manual")
        if policy == "auto_if_clean" and validation["ok"] and validation["warnings"] == 0:
            approved = self.approve(story_id, actor="system")
            result["status"] = approved["status"]
            result["auto_approved"] = True
        return result

    def continue_story(self, story_id: str, topic: str = "", actor: str = "agent") -> dict:
        prev = self._require(story_id)
        if prev["status"] not in ("APPROVED", "PUBLISHED"):
            raise StudioError("INVALID_STATE", "확정(APPROVED)된 스토리에서만 이어 쓸 수 있습니다.")
        return self.generate(prev["influencer_id"], topic=topic, continue_from=story_id, season_id=prev["season_id"],
                             day_no=(prev["day_no"] + 1) if prev["day_no"] else None, actor=actor)

    def get(self, story_id: str) -> dict:
        s = self._require(story_id)
        episodes = self.db.query("SELECT * FROM episodes WHERE story_id = ? ORDER BY sequence_no", (story_id,))
        contents = self.db.query("SELECT content_id, platform, content_type, language, status FROM contents "
                                 "WHERE story_id = ?", (story_id,))
        assets = self.db.query("SELECT asset_id, asset_type, file_path, is_placeholder, review_status FROM assets "
                               "WHERE story_id = ?", (story_id,))
        links = self.db.query("SELECT * FROM story_links WHERE source_story_id = ? OR target_story_id = ?",
                              (story_id, story_id))
        return {"ok": True, "story": s, "episodes": episodes, "contents": contents, "assets": assets, "links": links}

    def search(self, influencer_id: str, query: str = "", status: str | None = None, limit: int = 20,
               date_from: str | None = None, date_to: str | None = None) -> dict:
        if query:
            res = self.st.memory.search(influencer_id, query, types=["STORY"], limit=limit, date_from=date_from,
                                        date_to=date_to)
            return {"ok": True, "stories": res["results"]}
        sql = "SELECT story_id, title, summary, story_date, day_no, status, season_id FROM stories WHERE influencer_id = ?"
        params: list = [influencer_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if date_from:
            sql += " AND story_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND story_date <= ?"
            params.append(date_to)
        return {"ok": True, "stories": self.db.query(sql + " ORDER BY story_date DESC LIMIT ?", [*params, limit])}

    def validate(self, story_id: str) -> dict:
        s = self._require(story_id)
        v = self.st.continuity.validate(s["influencer_id"], s["data_json"], story_id=story_id)
        self.db.update("stories", "story_id", story_id, {"validation_json": v, "updated_at": now_iso()})
        return {"ok": True, "story_id": story_id, "validation": v}

    # ------------------------------------------------------------------
    def approve(self, story_id: str, actor: str = "agent", force_reason: str = "") -> dict:
        s = self._require(story_id)
        if s["status"] in ("APPROVED", "PUBLISHED"):
            return {"ok": True, "story_id": story_id, "status": s["status"], "message": "이미 확정된 스토리입니다."}
        if s["status"] not in ("REVIEW_REQUIRED", "DRAFT"):
            raise StudioError("INVALID_STATE", f"{s['status']} 상태의 스토리는 승인할 수 없습니다.")
        self.st.policy.require_decide(actor if actor != "system" else "agent", "STORY")
        self.st.policy.check(actor if actor != "system" else "agent", "archive_write")
        v = self.st.continuity.validate(s["influencer_id"], s["data_json"], story_id=story_id)
        if not v["ok"]:
            if not force_reason:
                raise StudioError("VALIDATION_FAILED", "모순 검사에서 오류가 있어 승인할 수 없습니다.",
                                  validation=v)
            if actor != "admin":
                raise StudioError("FORBIDDEN", "오류가 있는 스토리의 강제 승인은 운영자만 할 수 있습니다.")
        with self.db.tx():
            self._archive_story(s, actor)
            ts = now_iso()
            self.db.update("stories", "story_id", story_id,
                           {"status": "APPROVED", "approved_at": ts, "updated_at": ts, "validation_json": v})
            if s["revision_of"]:
                self._supersede(s["revision_of"])
                self.db.insert("story_links", {"source_story_id": story_id, "target_story_id": s["revision_of"],
                                               "relation_type": "REVISES", "created_at": ts}, or_ignore=True)
            self.st.approvals.close("STORY", story_id, "APPROVED", actor,
                                    f"강제 승인: {force_reason}" if force_reason else "")
            self.db.audit(actor, "story.approve", "STORY", story_id, {"force_reason": force_reason or None})
        return {"ok": True, "story_id": story_id, "status": "APPROVED", "validation": v}

    def reject(self, story_id: str, actor: str, comment: str = "") -> dict:
        s = self._require(story_id)
        if s["status"] in ("APPROVED", "PUBLISHED"):
            raise StudioError("INVALID_STATE", "확정된 스토리는 반려할 수 없습니다. story_revise 로 수정안을 만드세요.")
        self.db.update("stories", "story_id", story_id, {"status": "REJECTED", "updated_at": now_iso()})
        self.st.approvals.close("STORY", story_id, "REJECTED", actor, comment)
        self.db.audit(actor, "story.reject", "STORY", story_id, {"comment": comment})
        return {"ok": True, "story_id": story_id, "status": "REJECTED"}

    def _supersede(self, old_story_id: str) -> None:
        """수정안이 승인되면 이전 버전과 거기서 나온 기억·사건을 SUPERSEDED 로 표시합니다 (삭제하지 않음)."""
        self.db.update("stories", "story_id", old_story_id, {"status": "SUPERSEDED", "updated_at": now_iso()})
        for m in self.db.query("SELECT memory_id FROM memories WHERE source_story_id = ? AND status = 'ACTIVE'",
                               (old_story_id,)):
            self.db.update("memories", "memory_id", m["memory_id"], {"status": "SUPERSEDED", "updated_at": now_iso()})
            self.db.search_index_delete("MEMORY", m["memory_id"])
        for e in self.db.query("SELECT event_id FROM story_events WHERE story_id = ?", (old_story_id,)):
            self.db.update("story_events", "event_id", e["event_id"], {"status": "SUPERSEDED"})
            self.db.search_index_delete("EVENT", e["event_id"])
        self.db.execute("DELETE FROM entity_mentions WHERE story_id = ?", (old_story_id,))
        self.db.search_index_delete("STORY", old_story_id)

    def _archive_story(self, s: dict, actor: str) -> None:
        """승인된 스토리에서 장소·인물·항목·사건·기억을 추출해 아카이브에 저장합니다."""
        inf_id, sid, d = s["influencer_id"], s["story_id"], s["data_json"]
        when = s["story_date"]
        ts = now_iso()
        mem = self.st.memory
        for pl in d.get("places", []):
            eid = mem.upsert_entity(inf_id, "PLACE", pl["name"], {"type": pl.get("type"), "area": pl.get("area")}, sid)
            items = [i["name"] for i in d.get("items", []) if i.get("place") == pl["name"]]
            self.db.insert("entity_mentions", {"entity_id": eid, "story_id": sid, "occurred_at": when,
                                               "detail_json": {"items": items}}, or_ignore=True)
        for it in d.get("items", []):
            eid = mem.upsert_entity(inf_id, "ITEM", it["name"], {"place": it.get("place")}, sid)
            self.db.insert("entity_mentions", {"entity_id": eid, "story_id": sid, "occurred_at": when,
                                               "detail_json": {"place": it.get("place")}}, or_ignore=True)
        for ch in d.get("characters", []):
            if ch["name"] == self.st.influencers._require(inf_id)["name"]:
                continue
            existing_inf = self.db.one("SELECT entity_id FROM entities WHERE influencer_id = ? AND entity_type = 'INFLUENCER' "
                                       "AND name = ?", (inf_id, ch["name"]))
            eid = existing_inf["entity_id"] if existing_inf else mem.upsert_entity(
                inf_id, "PERSON", ch["name"], {"role": ch.get("role")}, sid)
            self.db.insert("entity_mentions", {"entity_id": eid, "story_id": sid, "occurred_at": when,
                                               "detail_json": {"role": ch.get("role")}}, or_ignore=True)
            rel = self.db.one("SELECT relationship_id, relation_type FROM relationships WHERE influencer_id = ? "
                              "AND target_id = ? AND status = 'ACTIVE'", (inf_id, eid))
            relation = ch.get("relation")
            if relation and (not rel or rel["relation_type"] != relation):
                if rel:
                    self.db.update("relationships", "relationship_id", rel["relationship_id"], {"status": "SUPERSEDED"})
                    self.db.insert("story_events", {
                        "event_id": new_id("evt"), "influencer_id": inf_id, "story_id": sid, "occurred_at": when,
                        "title": f"{ch['name']}과(와)의 관계 변화: {rel['relation_type']} → {relation}",
                        "description": "스토리 승인으로 관계가 바뀌었습니다.", "event_type": "CHANGE", "status": "NONE",
                        "fact_type": "FICTION", "created_at": ts, "event_data": {}})
                self.db.insert("relationships", {
                    "relationship_id": new_id("rel"), "influencer_id": inf_id, "source_id": inf_id, "target_id": eid,
                    "relation_type": relation, "sentiment": "positive", "since": when, "story_id": sid,
                    "status": "ACTIVE", "created_at": ts})
        shared_id = new_id("shr") if d.get("co_influencers") else None
        for ev in d.get("events", []):
            eid = new_id("evt")
            resolves = ev.get("resolves_event_id")
            if resolves:
                target = self.db.one("SELECT event_id FROM story_events WHERE event_id = ? AND influencer_id = ? "
                                     "AND status = 'OPEN'", (resolves, inf_id))
                if target:
                    self.db.update("story_events", "event_id", resolves, {"status": "RESOLVED"})
                else:
                    resolves = None
            self.db.insert("story_events", {
                "event_id": eid, "influencer_id": inf_id, "story_id": sid, "occurred_at": ev.get("occurred_at") or when,
                "title": ev["title"], "description": ev.get("description"), "event_type": ev["event_type"],
                "status": ev["status"] if ev["status"] != "RESOLVED" else "NONE", "resolves_event_id": resolves,
                "shared_event_id": shared_id, "fact_type": "FICTION", "created_at": ts, "event_data": {}})
            mem.index("EVENT", eid, inf_id, f"{ev['title']} {ev.get('description') or ''}", when)
        for text in d.get("memories", []):
            mem.create(inf_id, text, "EVENT", sid, "story_approval", "ACTIVE", "FICTION", 0.9, d.get("tags"), when,
                       actor=actor)
        mem.index("STORY", sid, inf_id, mem.story_text(s), when)
        if d.get("continues_story_id"):
            self.db.insert("story_links", {"source_story_id": sid, "target_story_id": d["continues_story_id"],
                                           "relation_type": "CONTINUES", "created_at": ts}, or_ignore=True)
        # 공동 경험: 상대 인플루언서의 기억에 무조건 복사하지 않고, 상대 관점의 '기억 후보'로만 남긴다.
        me = self.st.influencers._require(inf_id)
        for co in d.get("co_influencers") or []:
            mem.create(co, f"[공동 경험 · {me['name']}의 스토리 기준] {when} {s['title']}", "EVENT", sid, "shared_event",
                       "CANDIDATE", "FICTION", 0.6, ["shared", shared_id], when, actor=actor)

    # ------------------------------------------------------------------
    def revise(self, story_id: str, changes: dict | None = None, instruction: str = "", actor: str = "agent") -> dict:
        """확정된 스토리를 덮어쓰지 않고 수정안(새 버전)을 만듭니다."""
        old = self._require(story_id)
        if old["status"] in ("SUPERSEDED", "REJECTED"):
            raise StudioError("INVALID_STATE", f"{old['status']} 상태의 스토리는 수정할 수 없습니다.")
        data = dict(old["data_json"])
        allowed = {"title", "summary", "body", "characters", "places", "items", "events", "memories", "tags",
                   "next_hooks", "is_flashback", "profile_assertions"}
        bad = set(changes or {}) - allowed
        if bad:
            raise StudioError("INVALID_INPUT", f"수정할 수 없는 항목: {sorted(bad)}")
        data.update(changes or {})
        if instruction:
            data["revision_note"] = instruction
        if old["status"] in ("DRAFT", "REVIEW_REQUIRED"):
            # 아직 확정 전이면 같은 초안을 고친다 (이력은 감사 로그에 남김)
            v = self.st.continuity.validate(old["influencer_id"], data, story_id=story_id)
            self.db.update("stories", "story_id", story_id, {"data_json": data, "title": data["title"],
                                                             "summary": data.get("summary"), "validation_json": v,
                                                             "updated_at": now_iso()})
            self.db.execute("UPDATE episodes SET body = ?, title = ?, summary = ? WHERE story_id = ? AND sequence_no = 1",
                            (data.get("body", ""), data["title"], data.get("summary"), story_id))
            self.db.audit(actor, "story.edit_draft", "STORY", story_id, {"fields": sorted(changes or {})})
            return {"ok": True, "story_id": story_id, "status": old["status"], "validation": v}
        new_sid = new_id("sty")
        ts = now_iso()
        v = self.st.continuity.validate(old["influencer_id"], data, story_id=story_id)
        with self.db.tx():
            self.db.insert("stories", {
                **{k: old[k] for k in ("influencer_id", "season_id", "story_date", "day_no", "story_type", "provider")},
                "story_id": new_sid, "title": data["title"], "summary": data.get("summary"), "status": "REVIEW_REQUIRED",
                "data_json": data, "validation_json": v, "revision_of": story_id, "created_at": ts, "updated_at": ts,
                "approved_at": None})
            self.db.insert("episodes", {"episode_id": new_id("epi"), "story_id": new_sid, "sequence_no": 1,
                                        "title": data["title"], "body": data.get("body", ""),
                                        "summary": data.get("summary"), "created_at": ts})
            approval_id = self.st.approvals.request("STORY", new_sid, old["influencer_id"],
                                                    {"title": data["title"], "revision_of": story_id})
            self.db.audit(actor, "story.revise", "STORY", new_sid, {"revision_of": story_id, "instruction": instruction})
        return {"ok": True, "story_id": new_sid, "revision_of": story_id, "status": "REVIEW_REQUIRED",
                "validation": v, "approval_id": approval_id,
                "message": "원본은 그대로 유지되며, 수정안이 승인되면 원본은 SUPERSEDED 로 표시됩니다."}

    def timeline(self, influencer_id: str, date_from: str | None = None, date_to: str | None = None,
                 include_drafts: bool = False) -> dict:
        statuses = ("APPROVED", "PUBLISHED", "REVIEW_REQUIRED", "DRAFT") if include_drafts else ("APPROVED", "PUBLISHED")
        sql = (f"SELECT story_id, title, summary, story_date, day_no, status, season_id FROM stories WHERE influencer_id = ? "
               f"AND status IN ({','.join('?' for _ in statuses)})")
        params: list = [influencer_id, *statuses]
        esql = ("SELECT event_id, story_id, title, event_type, status, occurred_at FROM story_events "
                "WHERE influencer_id = ? AND status != 'SUPERSEDED'")
        eparams: list = [influencer_id]
        if date_from:
            sql += " AND story_date >= ?"
            esql += " AND occurred_at >= ?"
            params.append(date_from)
            eparams.append(date_from)
        if date_to:
            sql += " AND story_date <= ?"
            esql += " AND occurred_at <= ?"
            params.append(date_to)
            eparams.append(date_to)
        stories = self.db.query(sql + " ORDER BY story_date, day_no", params)
        events = self.db.query(esql + " ORDER BY occurred_at", eparams)
        by_story: dict = {}
        for e in events:
            by_story.setdefault(e["story_id"], []).append(e)
        timeline = [{**s, "events": by_story.pop(s["story_id"], [])} for s in stories]
        loose = [e for evs in by_story.values() for e in evs]  # 설정 변경 등 스토리에 속하지 않은 사건
        return {"ok": True, "influencer_id": influencer_id, "timeline": timeline, "other_events": loose}

    # ------------------------------------------------------------------
    def plan_season(self, influencer_id: str, total_days: int = 30, theme: str = "", start_date: str | None = None,
                    chunk_days: int = 7, actor: str = "agent") -> dict:
        """시즌 전체 계획을 세우고 첫 구간의 에피소드 초안만 만듭니다 (8.3)."""
        self.st.policy.check(actor, "draft")
        inf = self.st.influencers.require_active(influencer_id)
        if not 1 <= total_days <= 120:
            raise StudioError("INVALID_INPUT", "total_days 는 1~120 사이여야 합니다.")
        try:
            plan = self.st.llm.complete_json("season_plan", {"profile": inf["profile_json"], "total_days": total_days,
                                                             "theme": theme})
            plan.pop("_provider", None)
            plan.pop("_fallback_reason", None)
            days = plan.get("days")
            if not isinstance(days, list) or len(days) != total_days:
                raise LLMError("계획의 일수가 맞지 않습니다")
        except LLMError:
            from ..adapters.templates import task_season_plan
            plan = task_season_plan({"profile": inf["profile_json"], "total_days": total_days, "theme": theme})
        start = start_date or self._next_date(influencer_id)
        season_id = new_id("ssn")
        self.db.insert("seasons", {"season_id": season_id, "influencer_id": influencer_id,
                                   "title": plan.get("title") or theme or "새 시즌", "goal": plan.get("goal"),
                                   "total_days": total_days, "status": "ACTIVE", "created_at": now_iso(),
                                   "plan_json": {**plan, "start_date": start, "chunk_days": chunk_days}})
        self.db.audit(actor, "season.plan", "SEASON", season_id, {"days": total_days})
        chunk = self.generate_season_chunk(season_id, actor=actor)
        return {"ok": True, "season_id": season_id, "plan": plan, "start_date": start, "first_chunk": chunk}

    def generate_season_chunk(self, season_id: str, actor: str = "agent") -> dict:
        season = self.db.get("seasons", "season_id", season_id)
        if not season:
            raise StudioError("NOT_FOUND", f"시즌을 찾을 수 없습니다: {season_id}")
        plan = season["plan_json"]
        written = self.db.query("SELECT day_no, status FROM stories WHERE season_id = ? AND status NOT IN "
                                "('REJECTED','SUPERSEDED') ORDER BY day_no", (season_id,))
        unapproved = [w["day_no"] for w in written if w["status"] not in ("APPROVED", "PUBLISHED")]
        if unapproved:
            raise StudioError("PREVIOUS_CHUNK_NOT_APPROVED",
                              f"이전 구간의 검수가 끝나지 않았습니다 (미승인 {unapproved}일 차). 검수를 먼저 마치세요.")
        done = max([w["day_no"] for w in written], default=0)
        if done >= season["total_days"]:
            return {"ok": True, "season_id": season_id, "message": "모든 에피소드가 작성되었습니다.", "stories": []}
        first, last = done + 1, min(done + plan.get("chunk_days", 7), season["total_days"])
        start = date.fromisoformat(plan["start_date"])
        days = {d["day_no"]: d for d in plan.get("days", [])}
        created, pending = [], []
        for day in range(first, last + 1):
            spec = days.get(day, {})
            res = self.generate(season["influencer_id"], topic=spec.get("theme", ""), beat=spec.get("beat", ""),
                                is_major=bool(spec.get("is_major_event")), season_id=season_id, day_no=day,
                                story_date=(start + timedelta(days=day - 1)).isoformat(), pending=pending, actor=actor)
            pending.append(res["story"])
            created.append({"day_no": day, "story_id": res["story_id"], "title": res["story"]["title"],
                            "status": res["status"], "validation_ok": res["validation"]["ok"]})
        if last == season["total_days"]:
            self.db.update("seasons", "season_id", season_id, {"status": "COMPLETED"})
        return {"ok": True, "season_id": season_id, "days": [first, last], "stories": created,
                "next_step": "이 구간을 검수·승인한 뒤 season_next_chunk 로 다음 구간을 만드세요."}

    def approve_season_chunk(self, season_id: str, actor: str = "agent") -> dict:
        """검수 대기 중인 시즌 에피소드를 날짜 순서대로 승인합니다 (순서가 바뀌면 시간 충돌이 생기므로)."""
        rows = self.db.query("SELECT story_id, day_no FROM stories WHERE season_id = ? AND status = 'REVIEW_REQUIRED' "
                             "ORDER BY day_no", (season_id,))
        approved, failed = [], []
        for r in rows:
            try:
                self.approve(r["story_id"], actor=actor)
                approved.append(r["day_no"])
            except StudioError as e:
                failed.append({"day_no": r["day_no"], "story_id": r["story_id"], **e.to_dict()})
                break  # 앞 에피소드가 막히면 뒤 에피소드도 멈춘다
        return {"ok": not failed, "approved_days": approved, "failed": failed}
