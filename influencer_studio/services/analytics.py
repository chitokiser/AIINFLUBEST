"""⑦ 분석·학습 엔진 (기획서 8.4).

플랫폼 API 로 성과를 가져오는 연동은 아직 없으므로, 성과는 analytics_record (CSV/수동 입력)로 기록합니다.
성과 분석은 다음 스토리 계획의 참고 자료일 뿐, 캐릭터의 기본 성격이나 세계관을 바꾸지 않습니다.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from ..adapters.llm import LLMError
from ..adapters.templates import task_strategy
from ..util import StudioError, new_id, now_iso

WEIGHTS = {"views": 1, "likes": 5, "comments": 10, "shares": 15, "saves": 15, "watch_time_sec": 0.1, "followers_gained": 30}


def score(metrics: dict) -> float:
    return round(sum(float(metrics.get(k, 0) or 0) * w for k, w in WEIGHTS.items()), 2)


class AnalyticsService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    def record(self, content_id: str, metrics: dict, measured_at: str | None = None, source: str = "manual",
               actor: str = "agent") -> dict:
        c = self.st.content._content(content_id)
        clean = {}
        for k, v in metrics.items():
            try:
                clean[k] = float(v)
            except (TypeError, ValueError):
                raise StudioError("INVALID_INPUT", f"지표 '{k}' 값이 숫자가 아닙니다: {v}")
        aid = new_id("anl")
        self.db.insert("analytics", {"analytics_id": aid, "content_id": content_id, "influencer_id": c["influencer_id"],
                                     "platform": c["platform"], "measured_at": measured_at or date.today().isoformat(),
                                     "metric_data": clean, "source": source, "created_at": now_iso()})
        self.db.audit(actor, "analytics.record", "CONTENT", content_id, {"source": source})
        return {"ok": True, "analytics_id": aid, "score": score(clean)}

    def import_csv(self, csv_text: str, actor: str = "admin") -> dict:
        """열: content_id, measured_at, views, likes, comments, shares, saves, watch_time_sec, followers_gained"""
        reader = csv.DictReader(io.StringIO(csv_text))
        ok, errors = 0, []
        for i, row in enumerate(reader, start=2):
            cid = (row.pop("content_id", "") or "").strip()
            when = (row.pop("measured_at", "") or "").strip() or None
            metrics = {k: v for k, v in row.items() if k and v not in (None, "")}
            try:
                self.record(cid, metrics, when, "csv_import", actor)
                ok += 1
            except StudioError as e:
                errors.append({"line": i, "error": e.message})
        return {"ok": not errors, "imported": ok, "errors": errors}

    def get(self, content_id: str | None = None, influencer_id: str | None = None, days: int = 30) -> dict:
        since = (date.today() - timedelta(days=days)).isoformat()
        sql = "SELECT * FROM analytics WHERE measured_at >= ?"
        params: list = [since]
        if content_id:
            sql += " AND content_id = ?"
            params.append(content_id)
        if influencer_id:
            sql += " AND influencer_id = ?"
            params.append(influencer_id)
        rows = self.db.query(sql + " ORDER BY measured_at", params)
        for r in rows:
            r["score"] = score(r["metric_data"])
        return {"ok": True, "rows": rows}

    def report(self, influencer_id: str, days: int = 30) -> dict:
        self.st.influencers._require(influencer_id)
        since = (date.today() - timedelta(days=days)).isoformat()
        rows = self.db.query(
            "SELECT a.content_id, a.metric_data, a.measured_at, c.platform, c.language, c.content_type, s.story_id, "
            "s.title, s.data_json FROM analytics a JOIN contents c ON c.content_id = a.content_id "
            "JOIN stories s ON s.story_id = c.story_id WHERE a.influencer_id = ? AND a.measured_at >= ?",
            (influencer_id, since))
        # 같은 콘텐츠의 여러 측정값 중 가장 최근 값만 사용
        latest: dict = {}
        for r in rows:
            if r["content_id"] not in latest or r["measured_at"] > latest[r["content_id"]]["measured_at"]:
                latest[r["content_id"]] = r
        totals: dict = {}
        by_topic: dict = {}
        by_platform: dict = {}
        by_story: dict = {}
        for r in latest.values():
            m = r["metric_data"]
            sc = score(m)
            for k, v in m.items():
                totals[k] = totals.get(k, 0) + v
            for tag in (r["data_json"] or {}).get("tags", [])[:3]:
                t = by_topic.setdefault(tag, {"topic": tag, "score": 0, "contents": 0})
                t["score"] += sc
                t["contents"] += 1
            p = by_platform.setdefault(r["platform"], {"platform": r["platform"], "score": 0, "contents": 0})
            p["score"] += sc
            p["contents"] += 1
            s = by_story.setdefault(r["story_id"], {"story_id": r["story_id"], "title": r["title"], "score": 0})
            s["score"] += sc
        topics = sorted(({**t, "score": round(t["score"] / t["contents"], 2)} for t in by_topic.values()),
                        key=lambda x: x["score"], reverse=True)
        return {"ok": True, "influencer_id": influencer_id, "period_days": days, "contents_measured": len(latest),
                "totals": totals, "by_topic": topics,
                "by_platform": sorted(by_platform.values(), key=lambda x: x["score"], reverse=True),
                "top_stories": sorted(by_story.values(), key=lambda x: x["score"], reverse=True)[:5],
                "data_note": "성과 데이터는 수동/CSV 로 기록된 값만 포함합니다." if latest else
                             "기록된 성과 데이터가 없습니다. analytics_record 또는 CSV 가져오기로 입력하세요."}

    def strategy_update(self, influencer_id: str, days: int = 30, actor: str = "agent") -> dict:
        rep = self.report(influencer_id, days)
        profile = self.st.influencers._require(influencer_id)["profile_json"]
        payload = {"profile": {k: profile.get(k) for k in ("name", "personality", "interests", "long_term_goal")},
                   "topic_stats": rep["by_topic"], "platform_stats": rep["by_platform"]}
        try:
            plan = self.st.llm.complete_json("strategy", payload)
            if not plan.get("plan"):
                raise LLMError("계획 없음")
        except LLMError:
            plan = task_strategy(payload)
        plan.pop("_fallback_reason", None)
        provider = plan.pop("_provider", "template")
        self.db.audit(actor, "analytics.strategy", "INFLUENCER", influencer_id, {"plan": plan})
        return {"ok": True, "influencer_id": influencer_id, "provider": provider, "proposal": plan, "report": rep,
                "guardrail": "이 제안은 캐릭터 프로필·세계관을 변경하지 않았습니다. 설정 변경이 필요하면 "
                             "influencer_update 로 별도 변경안을 올리세요."}
