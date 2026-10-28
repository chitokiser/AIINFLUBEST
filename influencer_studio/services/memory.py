"""③ 스토리 아카이브 및 장기 기억 (기획서 5장).

검색은 다섯 가지 방식을 결합합니다.
- 관계형: influencer_id, 상태, 유형, 날짜 등 정확한 조건 (항상 인플루언서별로 분리)
- 전문 검색: SQLite FTS5(trigram) + 짧은 단어 LIKE
- 벡터 검색: 임베딩 코사인 유사도
- 시간 기반: date_from / date_to, 최근성 가중치
- 관계 기반: entity(인물·장소) 이름으로 연결된 스토리·사건
"""

from __future__ import annotations

import json
import re
from datetime import date

from ..adapters.embedding import cosine
from ..util import StudioError, new_id, now_iso

STOPWORDS = {"관련", "스토리", "모두", "찾아서", "찾아", "대한", "있는", "했던", "에서", "그리고", "다음", "콘텐츠", "만들어"}


def _terms(query: str) -> list[str]:
    words = re.findall(r"[\w']+", query.lower())
    out = []
    for w in words:
        # 조사 제거(간단 규칙): 하린이 → 하린, 미딩에서 → 미딩
        w = re.sub(r"(에서|으로|이랑|하고|에게|까지|부터|이가|은|는|이|가|을|를|에|와|과|의|도|로)$", "", w)
        if len(w) >= 2 and w not in STOPWORDS:
            out.append(w)
    return list(dict.fromkeys(out))


class MemoryService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    # ---- 색인 --------------------------------------------------------
    def index(self, target_type: str, target_id: str, influencer_id: str, text: str, occurred_at: str | None) -> None:
        self.db.search_index_put(target_type, target_id, influencer_id, text, occurred_at)
        vec = self.st.embedder.embed(text)
        self.db.embedding_put(target_type, target_id, influencer_id, self.st.embedder.name, vec)

    def reindex_all(self) -> dict:
        n = 0
        for m in self.db.query("SELECT * FROM memories WHERE status = 'ACTIVE'"):
            self.index("MEMORY", m["memory_id"], m["influencer_id"], m["content"], m["valid_from"])
            n += 1
        for s in self.db.query("SELECT * FROM stories WHERE status IN ('APPROVED','PUBLISHED')"):
            self.index("STORY", s["story_id"], s["influencer_id"], self.story_text(s), s["story_date"])
            n += 1
        for e in self.db.query("SELECT * FROM story_events WHERE status != 'SUPERSEDED'"):
            self.index("EVENT", e["event_id"], e["influencer_id"], f"{e['title']} {e['description'] or ''}", e["occurred_at"])
            n += 1
        return {"ok": True, "indexed": n, "embedder": self.st.embedder.name}

    @staticmethod
    def story_text(story: dict) -> str:
        d = story.get("data_json") or {}
        names = [c.get("name", "") for c in d.get("characters", [])] + [p.get("name", "") for p in d.get("places", [])]
        items = [i.get("name", "") for i in d.get("items", [])]
        return " ".join([story.get("title") or "", story.get("summary") or "", d.get("body", ""), *names, *items,
                         *(d.get("tags") or [])])

    # ---- 엔티티 ------------------------------------------------------
    def upsert_entity(self, influencer_id: str, entity_type: str, name: str, attributes: dict | None,
                      story_id: str | None) -> str:
        name = (name or "").strip()
        if not name:
            raise StudioError("INVALID_INPUT", "엔티티 이름이 비어 있습니다.")
        row = self.db.one("SELECT entity_id, attributes_json FROM entities WHERE influencer_id = ? AND entity_type = ? "
                          "AND name = ?", (influencer_id, entity_type, name))
        if row:
            if attributes:
                merged = {**(row["attributes_json"] or {}), **attributes}
                self.db.update("entities", "entity_id", row["entity_id"], {"attributes_json": merged})
            return row["entity_id"]
        eid = new_id("ent")
        self.db.insert("entities", {"entity_id": eid, "influencer_id": influencer_id, "entity_type": entity_type,
                                    "name": name, "attributes_json": attributes or {}, "first_story_id": story_id,
                                    "created_at": now_iso()})
        return eid

    def known_people(self, influencer_id: str) -> list[dict]:
        return self.db.query(
            "SELECT e.entity_id, e.name, e.entity_type, e.attributes_json, r.relation_type AS relation, "
            "(SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = e.entity_id) AS appearances "
            "FROM entities e LEFT JOIN relationships r ON r.target_id = e.entity_id AND r.status = 'ACTIVE' "
            "WHERE e.influencer_id = ? AND e.entity_type IN ('PERSON','INFLUENCER') ORDER BY e.created_at",
            (influencer_id,))

    def known_places(self, influencer_id: str) -> list[dict]:
        places = self.db.query(
            "SELECT entity_id, name, attributes_json FROM entities WHERE influencer_id = ? AND entity_type = 'PLACE' "
            "ORDER BY created_at", (influencer_id,))
        out = []
        for p in places:
            visits = self.db.query("SELECT story_id, occurred_at, detail_json FROM entity_mentions WHERE entity_id = ? "
                                   "ORDER BY occurred_at", (p["entity_id"],))
            items = self.db.query(
                "SELECT name FROM entities WHERE influencer_id = ? AND entity_type = 'ITEM' "
                "AND json_extract(attributes_json, '$.place') = ?", (influencer_id, p["name"]))
            attrs = p["attributes_json"] or {}
            out.append({"entity_id": p["entity_id"], "name": p["name"], "type": attrs.get("type"),
                        "area": attrs.get("area"), "visits": [v["occurred_at"] for v in visits],
                        "items": [i["name"] for i in items]})
        return out

    # ---- 기억 CRUD ---------------------------------------------------
    def create(self, influencer_id: str, content: str, memory_type: str = "FACT", source_story_id: str | None = None,
               source: str = "manual", status: str = "CANDIDATE", fact_type: str = "FICTION", confidence: float = 1.0,
               tags: list | None = None, valid_from: str | None = None, supersedes: str | None = None,
               actor: str = "agent") -> dict:
        self.st.influencers._require(influencer_id)
        if fact_type not in ("FICTION", "REAL"):
            raise StudioError("INVALID_INPUT", "fact_type 은 FICTION(가상 설정) 또는 REAL(현실에서 확인된 사실)입니다.")
        if not content.strip():
            raise StudioError("INVALID_INPUT", "기억 내용이 비어 있습니다.")
        mid = new_id("mem")
        ts = now_iso()
        self.db.insert("memories", {
            "memory_id": mid, "influencer_id": influencer_id, "memory_type": memory_type, "content": content.strip(),
            "source_story_id": source_story_id, "source": source, "confidence": confidence, "fact_type": fact_type,
            "status": status, "supersedes": supersedes, "valid_from": valid_from, "tags": tags or [],
            "created_at": ts, "updated_at": ts,
        })
        if status == "ACTIVE":
            self.index("MEMORY", mid, influencer_id, content, valid_from)
        self.db.audit(actor, "memory.create", "MEMORY", mid, {"status": status, "source": source})
        result = {"ok": True, "memory_id": mid, "status": status}
        if status == "CANDIDATE":
            result["approval_id"] = self.st.approvals.request("MEMORY", mid, influencer_id, {"content": content})
        return result

    def get(self, memory_id: str) -> dict:
        m = self.db.get("memories", "memory_id", memory_id)
        if not m:
            raise StudioError("NOT_FOUND", f"기억을 찾을 수 없습니다: {memory_id}")
        history = []
        cur = m
        while cur and cur.get("supersedes"):
            cur = self.db.get("memories", "memory_id", cur["supersedes"])
            if cur:
                history.append(cur)
        newer = self.db.query("SELECT memory_id, content, status, created_at FROM memories WHERE supersedes = ?",
                              (memory_id,))
        return {"ok": True, "memory": m, "previous_versions": history, "superseded_by": newer}

    def update(self, memory_id: str, content: str, reason: str, actor: str = "agent") -> dict:
        """기존 기억을 덮어쓰지 않고 새 버전을 만들어 이전 버전을 SUPERSEDED 로 표시합니다."""
        old = self.db.get("memories", "memory_id", memory_id)
        if not old:
            raise StudioError("NOT_FOUND", f"기억을 찾을 수 없습니다: {memory_id}")
        if old["status"] != "ACTIVE":
            raise StudioError("INVALID_STATE", f"ACTIVE 상태의 기억만 수정할 수 있습니다 (현재 {old['status']}).")
        if not reason:
            raise StudioError("REASON_REQUIRED", "기억을 수정할 때는 사유(reason)를 남겨야 합니다.")
        with self.db.tx():
            self.db.update("memories", "memory_id", memory_id, {"status": "SUPERSEDED", "updated_at": now_iso()})
            self.db.search_index_delete("MEMORY", memory_id)
            res = self.create(old["influencer_id"], content, old["memory_type"], old["source_story_id"], "manual_edit",
                              "ACTIVE", old["fact_type"], old["confidence"], old["tags"], old["valid_from"],
                              supersedes=memory_id, actor=actor)
            self.db.audit(actor, "memory.update", "MEMORY", memory_id, {"new": res["memory_id"], "reason": reason})
        return {"ok": True, "memory_id": res["memory_id"], "supersedes": memory_id}

    def set_status(self, memory_id: str, status: str, actor: str) -> None:
        m = self.db.get("memories", "memory_id", memory_id)
        if not m:
            raise StudioError("NOT_FOUND", memory_id)
        self.db.update("memories", "memory_id", memory_id, {"status": status, "updated_at": now_iso()})
        if status == "ACTIVE":
            self.index("MEMORY", memory_id, m["influencer_id"], m["content"], m["valid_from"])
        else:
            self.db.search_index_delete("MEMORY", memory_id)
        self.db.audit(actor, f"memory.{status.lower()}", "MEMORY", memory_id)

    # ---- 검색 --------------------------------------------------------
    def search(self, influencer_id: str, query: str = "", types: list[str] | None = None, limit: int = 10,
               date_from: str | None = None, date_to: str | None = None, entity: str | None = None) -> dict:
        self.st.influencers._require(influencer_id)
        target_types = [t.upper() for t in (types or ["MEMORY", "STORY", "EVENT"])]
        terms = _terms(query)
        scores: dict[tuple, dict] = {}

        def bucket(tt, tid):
            return scores.setdefault((tt, tid), {"target_type": tt, "target_id": tid, "keyword": 0.0, "vector": 0.0,
                                                 "relation": 0.0, "occurred_at": None})

        # 1) 전문 검색
        for row in self.db.search_text(influencer_id, terms, target_types):
            text = row["text"].lower()
            hits = sum(1 for t in terms if t in text)
            b = bucket(row["target_type"], row["target_id"])
            b["keyword"] = max(b["keyword"], hits / max(len(terms), 1))
            b["occurred_at"] = row["occurred_at"]
        # 2) 벡터 검색
        if query.strip():
            qv = self.st.embedder.embed(query)
            for row in self.db.embeddings_for(influencer_id, target_types):
                vec = json.loads(row["vector"]) if isinstance(row["vector"], str) else row["vector"]
                sim = cosine(qv, vec)
                if sim > 0.05:
                    bucket(row["target_type"], row["target_id"])["vector"] = sim
        # 3) 관계 기반: 엔티티 이름이 질의에 있거나 entity 인자로 지정된 경우
        names = [entity] if entity else []
        for ent in self.db.query("SELECT entity_id, name FROM entities WHERE influencer_id = ?", (influencer_id,)):
            if ent["name"] in names or any(t == ent["name"].lower() or t in ent["name"].lower() for t in terms if len(t) >= 2):
                for m in self.db.query("SELECT story_id FROM entity_mentions WHERE entity_id = ?", (ent["entity_id"],)):
                    if "STORY" in target_types:
                        bucket("STORY", m["story_id"])["relation"] = 1.0
        # 4) 상세 조회 + 시간 필터 + 상태 필터 (확정된 기록만)
        results = []
        today_ord = date.today().toordinal()
        for (tt, tid), b in scores.items():
            item = self._load(tt, tid)
            if not item:
                continue
            when = item.get("occurred_at")
            if date_from and (not when or when < date_from):
                continue
            if date_to and (not when or when > date_to):
                continue
            recency = 0.0
            if when:
                try:
                    recency = max(0.0, 1 - (today_ord - date.fromisoformat(when[:10]).toordinal()) / 365)
                except ValueError:
                    pass
            score = 0.45 * b["keyword"] + 0.35 * b["vector"] + 0.15 * b["relation"] + 0.05 * recency
            if score <= 0.08:
                continue
            results.append({**item, "score": round(score, 4),
                            "match": {k: round(b[k], 3) for k in ("keyword", "vector", "relation")}})
        results.sort(key=lambda r: r["score"], reverse=True)
        return {"ok": True, "influencer_id": influencer_id, "query": query, "terms": terms,
                "results": results[:limit], "embedder": self.st.embedder.name}

    def _load(self, target_type: str, target_id: str) -> dict | None:
        if target_type == "MEMORY":
            m = self.db.one("SELECT * FROM memories WHERE memory_id = ? AND status = 'ACTIVE'", (target_id,))
            return m and {"type": "MEMORY", "id": m["memory_id"], "text": m["content"], "occurred_at": m["valid_from"],
                          "source_story_id": m["source_story_id"], "fact_type": m["fact_type"],
                          "confidence": m["confidence"]}
        if target_type == "STORY":
            s = self.db.one("SELECT * FROM stories WHERE story_id = ? AND status IN ('APPROVED','PUBLISHED')", (target_id,))
            return s and {"type": "STORY", "id": s["story_id"], "title": s["title"], "text": s["summary"],
                          "occurred_at": s["story_date"], "items": [i.get("name") for i in s["data_json"].get("items", [])],
                          "places": [p.get("name") for p in s["data_json"].get("places", [])]}
        if target_type == "EVENT":
            e = self.db.one("SELECT * FROM story_events WHERE event_id = ? AND status != 'SUPERSEDED'", (target_id,))
            return e and {"type": "EVENT", "id": e["event_id"], "title": e["title"], "text": e["description"],
                          "occurred_at": e["occurred_at"], "status": e["status"], "source_story_id": e["story_id"]}
        return None
