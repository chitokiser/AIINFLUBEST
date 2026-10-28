"""스토리 연속성 검증 (기획서 4.3).

모순이 발견되어도 기존 기록을 덮어쓰지 않습니다. 결과는 issues 목록으로만 돌려주며,
- ERROR  : 승인 전에 반드시 해결해야 하는 충돌 (시간 역행, 프로필 모순, 금지 주제)
- WARNING: 확인이 필요한 차이 (관계 불일치, 이미 소개한 메뉴 반복, 다른 인플루언서 언급)
- INFO   : 연결 정보 (재방문, 기존 인물, 미해결 사건, 해결된 사건)
"""

from __future__ import annotations

import re


class ContinuityService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    def validate(self, influencer_id: str, draft: dict, story_id: str | None = None) -> dict:
        inf = self.st.influencers._require(influencer_id)
        profile = inf["profile_json"]
        issues: list[dict] = []

        def add(severity, code, message, **detail):
            issues.append({"severity": severity, "code": code, "message": message, **detail})

        story_date = draft.get("story_date")
        flashback = bool(draft.get("is_flashback"))

        # 1) 시간: 확정된 마지막 스토리보다 과거 날짜인지
        last = self.db.one(
            "SELECT story_id, title, story_date FROM stories WHERE influencer_id = ? AND status IN ('APPROVED','PUBLISHED') "
            "AND story_id != ? ORDER BY story_date DESC LIMIT 1", (influencer_id, story_id or ""))
        if story_date and last and last["story_date"] and story_date < last["story_date"] and not flashback:
            add("ERROR", "TIMELINE_CONFLICT",
                f"스토리 날짜({story_date})가 이미 확정된 마지막 스토리({last['story_date']} '{last['title']}')보다 이릅니다. "
                "회상 스토리라면 is_flashback=true 로 표시하세요.",
                existing={"story_id": last["story_id"], "story_date": last["story_date"]}, proposed=story_date,
                suggestion="날짜를 마지막 스토리 이후로 바꾸거나 회상(flashback)으로 표시")
        if draft.get("season_id") and draft.get("day_no"):
            dup = self.db.one("SELECT story_id FROM stories WHERE season_id = ? AND day_no = ? AND story_id != ? "
                              "AND status IN ('APPROVED','PUBLISHED','REVIEW_REQUIRED','DRAFT')",
                              (draft["season_id"], draft["day_no"], story_id or ""))
            if dup:
                add("WARNING", "DUPLICATE_DAY", f"같은 시즌의 {draft['day_no']}일 차 스토리가 이미 있습니다.",
                    existing={"story_id": dup["story_id"]})

        # 2) 인물: 기존 인물 여부와 관계 비교
        people = {p["name"]: p for p in self.st.memory.known_people(influencer_id)}
        for ch in draft.get("characters") or []:
            name = (ch.get("name") or "").strip()
            if not name:
                continue
            if name == profile.get("name"):
                continue
            known = people.get(name)
            if known:
                add("INFO", "KNOWN_CHARACTER", f"{name}: 기존 등장인물입니다 (관계: {known.get('relation') or '미정'}, "
                    f"등장 {known.get('appearances', 0)}회).", entity_id=known["entity_id"])
                if ch.get("relation") and known.get("relation") and ch["relation"] != known["relation"]:
                    add("WARNING", "RELATIONSHIP_MISMATCH",
                        f"{name}과(와)의 관계가 기록({known['relation']})과 다릅니다(제안: {ch['relation']}). "
                        "실제 관계 변화라면 승인 시 새로운 사건으로 기록됩니다.",
                        existing=known["relation"], proposed=ch["relation"])
            else:
                add("INFO", "NEW_CHARACTER", f"{name}: 새로운 등장인물입니다.")

        # 3) 장소와 항목: 재방문, 이미 소개한 항목 반복
        places = {p["name"]: p for p in self.st.memory.known_places(influencer_id)}
        for pl in draft.get("places") or []:
            known = places.get(pl.get("name"))
            if known:
                add("INFO", "REVISIT", f"{pl['name']}: 이전 방문 기록이 있습니다 ({', '.join(v for v in known['visits'] if v)}).",
                    previous_items=known["items"])
        for it in draft.get("items") or []:
            known = places.get(it.get("place"))
            if known and it.get("name") in known["items"]:
                add("WARNING", "REPEATED_ITEM", f"'{it['name']}'은(는) {it['place']}에서 이미 소개한 항목입니다.",
                    suggestion=f"아직 소개하지 않은 항목을 선택 (이미 소개: {', '.join(known['items'])})")

        # 4) 미해결 사건
        open_events = self.db.query("SELECT event_id, title, occurred_at FROM story_events WHERE influencer_id = ? "
                                    "AND status = 'OPEN' ORDER BY occurred_at", (influencer_id,))
        open_ids = {e["event_id"] for e in open_events}
        resolved_here = set()
        for ev in draft.get("events") or []:
            rid = ev.get("resolves_event_id")
            if rid:
                if rid in open_ids:
                    resolved_here.add(rid)
                    add("INFO", "RESOLVES_THREAD", f"미해결 사건을 해결합니다: {ev.get('title')}", event_id=rid)
                else:
                    add("WARNING", "UNKNOWN_THREAD", f"해결하려는 사건({rid})이 열려 있는 사건이 아닙니다.")
            if ev.get("occurred_at") and story_date and ev["occurred_at"] < story_date and not flashback:
                add("WARNING", "EVENT_DATE", f"사건 '{ev.get('title')}'의 날짜가 스토리 날짜보다 이릅니다.")
        remaining = [e for e in open_events if e["event_id"] not in resolved_here]
        if remaining:
            add("INFO", "OPEN_THREADS", f"아직 해결되지 않은 사건 {len(remaining)}개가 있습니다.",
                threads=[{"event_id": e["event_id"], "title": e["title"], "since": e["occurred_at"]} for e in remaining[:5]])

        # 5) 프로필 모순
        body = " ".join([draft.get("title", ""), draft.get("summary", ""), draft.get("body", "")])
        for key, value in (draft.get("profile_assertions") or {}).items():
            current = profile.get(key)
            if current is not None and str(value) != str(current):
                add("ERROR", "PROFILE_CONFLICT",
                    f"프로필의 {key}({current})와 스토리 내용({value})이 다릅니다. 실제 설정 변경이라면 "
                    "influencer_update 로 변경안을 올리세요.", field=key, existing=current, proposed=value)
        age = profile.get("age")
        for m in re.finditer(r"(?:나는|저는|제가|내가|올해)\s*(\d{2})\s*(?:살|세)", body):
            if age and int(m.group(1)) != int(age):
                add("ERROR", "PROFILE_CONFLICT", f"본문의 나이({m.group(1)})가 프로필 나이({age})와 다릅니다.",
                    field="age", existing=age, proposed=int(m.group(1)))
        occupation = profile.get("occupation") or ""
        m = re.search(r"(?:제 직업은|내 직업은|저는 직업이)\s*([^.,!\s]+)", body)
        if m and m.group(1) not in occupation:
            add("ERROR", "PROFILE_CONFLICT", f"본문의 직업({m.group(1)})이 프로필 직업({occupation})과 다릅니다.",
                field="occupation", existing=occupation, proposed=m.group(1))

        # 6) 콘텐츠 정책: 금지 주제
        for banned in (profile.get("content_policy") or {}).get("banned_topics", []):
            if banned and banned in body:
                add("ERROR", "POLICY_VIOLATION", f"금지 주제 '{banned}'이(가) 포함되어 있습니다.")

        # 7) 다른 인플루언서의 기억 혼입 방지
        mine = {e["name"] for e in self.db.query("SELECT name FROM entities WHERE influencer_id = ?", (influencer_id,))}
        co = set(draft.get("co_influencers") or [])
        others = self.db.query("SELECT influencer_id, name FROM influencers WHERE influencer_id != ?", (influencer_id,))
        for o in others:
            if o["name"] in body and o["name"] not in mine and o["influencer_id"] not in co:
                add("WARNING", "CROSS_INFLUENCER_REFERENCE",
                    f"관계가 설정되지 않은 다른 인플루언서({o['name']})가 언급됩니다. 공동 콘텐츠라면 "
                    "influencer_relate 로 관계를 먼저 설정하고 co_influencers 에 지정하세요.")
        foreign_names = self.db.query(
            "SELECT DISTINCT e.name, i.name AS owner FROM entities e JOIN influencers i ON i.influencer_id = e.influencer_id "
            "WHERE e.influencer_id != ? AND e.entity_type = 'PERSON'", (influencer_id,))
        draft_people = {c.get("name") for c in draft.get("characters") or []}
        for f in foreign_names:
            if f["name"] in draft_people and f["name"] not in mine:
                add("INFO", "NAME_USED_BY_OTHER",
                    f"'{f['name']}'은(는) {f['owner']}의 스토리에도 있는 이름입니다. 같은 인물이 아니라면 문제없습니다.")

        errors = [i for i in issues if i["severity"] == "ERROR"]
        warnings = [i for i in issues if i["severity"] == "WARNING"]
        return {"ok": not errors, "errors": len(errors), "warnings": len(warnings), "issues": issues}
