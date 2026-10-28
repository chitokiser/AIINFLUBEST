"""④ 콘텐츠 제작 엔진 (기획서 6장).

모든 콘텐츠는 '확정된(APPROVED) 스토리'에서만 파생되며, 같은 story_id 를 참조합니다.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import date, timedelta
from pathlib import Path

from ..adapters.image import ImageError
from ..adapters.llm import LLMError
from ..adapters.media import MediaNotConfigured
from ..adapters.publish import CONTENT_TYPE_PLATFORM, PLATFORMS
from ..adapters.templates import DISCLOSURE, task_content_text
from ..util import StudioError, dumps, new_id, now_iso, safe_path, today

TEXT_FORMATS = {"instagram_post", "shorts_script", "blog"}
REQUIRED_KEYS = {
    "instagram_post": ("caption",),
    "shorts_script": ("scenes",),
    "blog": ("title", "body_markdown"),
}
NEGATIVE_PROMPT = ("different person, changed face, inconsistent hairstyle, extra fingers, deformed, text, watermark, "
                   "logo, celebrity likeness")


def _text_of(data: dict) -> str:
    parts = [data.get("caption", ""), data.get("title", ""), data.get("body_markdown", ""), data.get("disclosure", "")]
    for sc in data.get("scenes") or []:
        parts += [sc.get("narration", ""), sc.get("subtitle", ""), sc.get("visual", "")]
    return "\n".join(p for p in parts if p)


class ContentService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    def _story(self, story_id: str, require_approved: bool = True) -> dict:
        s = self.db.get("stories", "story_id", story_id)
        if not s:
            raise StudioError("NOT_FOUND", f"스토리를 찾을 수 없습니다: {story_id}")
        if require_approved and s["status"] not in ("APPROVED", "PUBLISHED"):
            raise StudioError("STORY_NOT_APPROVED",
                              "콘텐츠는 확정(APPROVED)된 스토리에서만 만들 수 있습니다. 먼저 스토리를 승인하세요.",
                              story_status=s["status"])
        return s

    def _content(self, content_id: str) -> dict:
        c = self.db.get("contents", "content_id", content_id)
        if not c:
            raise StudioError("NOT_FOUND", f"콘텐츠를 찾을 수 없습니다: {content_id}")
        return c

    def current_data(self, content_id: str) -> dict:
        c = self._content(content_id)
        v = self.db.one("SELECT content_data FROM content_versions WHERE content_id = ? AND version_no = ?",
                        (content_id, c["current_version"]))
        return v["content_data"] if v else {}

    # ------------------------------------------------------------------
    def plan(self, influencer_id: str, days: int = 7, outputs: dict | None = None) -> dict:
        """다가오는 기간의 제작 계획 (시즌 계획·미해결 사건·초기 아이디어를 근거로)."""
        inf = self.st.influencers._require(influencer_id)
        profile = inf["profile_json"]
        outputs = outputs or {"images": 3, "texts": ["instagram_post", "shorts_script"], "languages": profile.get("languages", ["ko"])}
        threads = self.db.query("SELECT event_id, title FROM story_events WHERE influencer_id = ? AND status = 'OPEN' "
                                "ORDER BY occurred_at", (influencer_id,))
        season = self.db.one("SELECT * FROM seasons WHERE influencer_id = ? AND status = 'ACTIVE' ORDER BY created_at DESC "
                             "LIMIT 1", (influencer_id,))
        ideas = list(profile.get("initial_story_ideas") or []) + list(profile.get("interests") or [])
        start = date.fromisoformat(self.st.stories._next_date(influencer_id))
        plan = []
        for i in range(days):
            if season:
                written = self.db.one("SELECT MAX(day_no) AS d FROM stories WHERE season_id = ?", (season["season_id"],))["d"] or 0
                spec = next((d for d in season["plan_json"].get("days", []) if d["day_no"] == written + i + 1), None)
                topic = spec["beat"] if spec else ideas[i % len(ideas)]
            elif i < len(threads):
                topic = f"미해결 사건 이어가기: {threads[i]['title']}"
            else:
                topic = ideas[i % len(ideas)] if ideas else "일상"
            plan.append({"date": (start + timedelta(days=i)).isoformat(), "topic": topic, "outputs": outputs})
        return {"ok": True, "influencer_id": influencer_id, "plan": plan,
                "note": "계획은 제안입니다. 각 날짜에 story_generate 로 스토리를 만들고 승인 후 콘텐츠를 파생하세요."}

    # ------------------------------------------------------------------
    def _llm_text(self, fmt: str, language: str, story: dict, profile: dict, variant: int, extra: dict) -> tuple[dict, str]:
        payload = {"format": fmt, "language": language, "variant": variant,
                   "story": {k: story["data_json"].get(k) for k in ("title", "summary", "body", "characters", "places",
                                                                     "items", "tags")},
                   "profile": {k: profile.get(k) for k in ("name", "handle", "speech_style")},
                   "disclosure": DISCLOSURE.get(language, DISCLOSURE["en"]), **extra}
        try:
            data = self.st.llm.complete_json("content_text", payload)
        except LLMError:
            data = {"_provider": "template"}
        provider = data.pop("_provider", self.st.llm.name)
        data.pop("_fallback_reason", None)
        if data.get("_needs_human_translation") or not all(data.get(k) for k in REQUIRED_KEYS[fmt]):
            data = task_content_text(payload)
            provider = "template"
        if data.get("_needs_human_translation"):
            raise StudioError("LANGUAGE_NOT_SUPPORTED",
                              f"'{language}' 언어는 템플릿 생성기가 지원하지 않습니다. 언어 모델(Ollama)을 연결하세요.")
        disclosure = payload["disclosure"]
        if fmt == "instagram_post" and disclosure not in data.get("caption", ""):
            data["caption"] = data["caption"].rstrip() + "\n\n" + disclosure
        if fmt == "blog" and disclosure not in data.get("body_markdown", ""):
            data["body_markdown"] = data["body_markdown"].rstrip() + "\n\n" + disclosure + "\n"
        if fmt == "shorts_script":
            data["disclosure"] = disclosure
        return data, provider

    def _insert_content(self, story: dict, platform: str, content_type: str, language: str, title: str, data: dict,
                        actor: str, translated_from: str | None = None) -> str:
        cid = new_id("cnt")
        ts = now_iso()
        self.db.insert("contents", {"content_id": cid, "story_id": story["story_id"],
                                    "influencer_id": story["influencer_id"], "platform": platform,
                                    "content_type": content_type, "language": language, "status": "REVIEW_REQUIRED",
                                    "current_version": 1, "title": title, "translated_from": translated_from,
                                    "created_at": ts, "updated_at": ts})
        self.db.insert("content_versions", {"version_id": new_id("cvr"), "content_id": cid, "version_no": 1,
                                            "content_data": data, "created_by": actor, "created_at": ts})
        self.st.approvals.request("CONTENT", cid, story["influencer_id"],
                                  {"story_id": story["story_id"], "type": content_type, "language": language})
        return cid

    def generate_text(self, story_id: str, formats: list[str] | None = None, languages: list[str] | None = None,
                      variants: int = 1, shorts_duration: int = 30, actor: str = "agent") -> dict:
        self.st.policy.check(actor, "draft")
        story = self._story(story_id)
        profile = self.st.influencers._require(story["influencer_id"])["profile_json"]
        formats = formats or ["instagram_post", "shorts_script", "blog"]
        languages = languages or profile.get("languages") or ["ko"]
        bad = set(formats) - TEXT_FORMATS
        if bad:
            raise StudioError("INVALID_INPUT", f"지원하지 않는 형식: {sorted(bad)} (가능: {sorted(TEXT_FORMATS)})")
        created = []
        for fmt in formats:
            for lang in languages:
                for variant in range(max(1, variants if fmt == "instagram_post" else 1)):
                    data, provider = self._llm_text(fmt, lang, story, profile, variant,
                                                    {"duration_sec": shorts_duration})
                    data["_provider"] = provider
                    title = data.get("title") or f"{story['title']} ({fmt}/{lang}{'#' + str(variant + 1) if variants > 1 else ''})"
                    with self.db.tx():
                        cid = self._insert_content(story, CONTENT_TYPE_PLATFORM[fmt], fmt, lang, title, data, actor)
                        validation = self.validate(cid, persist=True)
                        self.db.audit(actor, "content.generate_text", "CONTENT", cid, {"provider": provider})
                    created.append({"content_id": cid, "content_type": fmt, "language": lang, "variant": variant + 1,
                                    "provider": provider, "validation_ok": validation["ok"]})
        return {"ok": True, "story_id": story_id, "contents": created, "status": "REVIEW_REQUIRED"}

    def translate(self, content_id: str, target_language: str, actor: str = "agent") -> dict:
        c = self._content(content_id)
        story = self._story(c["story_id"])
        profile = self.st.influencers._require(c["influencer_id"])["profile_json"]
        data = self.current_data(content_id)
        if c["content_type"] not in TEXT_FORMATS:
            raise StudioError("INVALID_INPUT", "텍스트 콘텐츠만 번역할 수 있습니다.")
        src = {k: v for k, v in data.items() if not k.startswith("_") and k != "validation"}
        try:
            out = self.st.llm.complete_json("translate", {"target_language": target_language, "content": src})
        except LLMError:
            out = {"_needs_human_translation": True}
        provider = out.pop("_provider", self.st.llm.name)
        out.pop("_fallback_reason", None)
        if out.get("_needs_human_translation") or not all(out.get(k) for k in REQUIRED_KEYS[c["content_type"]]):
            # 단순 번역 대신 같은 스토리에서 대상 언어로 다시 현지화한다.
            out, provider = self._llm_text(c["content_type"], target_language, story, profile, 0, {})
        out["_provider"] = provider
        with self.db.tx():
            cid = self._insert_content(story, c["platform"], c["content_type"], target_language,
                                       f"{c['title']} [{target_language}]", out, actor, translated_from=content_id)
            validation = self.validate(cid, persist=True)
            self.db.audit(actor, "content.translate", "CONTENT", cid, {"from": content_id})
        return {"ok": True, "content_id": cid, "translated_from": content_id, "language": target_language,
                "provider": provider, "validation_ok": validation["ok"]}

    def revise(self, content_id: str, changes: dict, actor: str = "agent", comment: str = "") -> dict:
        c = self._content(content_id)
        if c["status"] == "PUBLISHED":
            raise StudioError("INVALID_STATE", "게시된 콘텐츠는 수정하지 않고, 새 콘텐츠로 만들어야 합니다.")
        data = {**self.current_data(content_id), **changes}
        version = c["current_version"] + 1
        with self.db.tx():
            self.db.insert("content_versions", {"version_id": new_id("cvr"), "content_id": content_id,
                                                "version_no": version, "content_data": data, "created_by": actor,
                                                "created_at": now_iso()})
            self.db.update("contents", "content_id", content_id, {"current_version": version, "status": "REVIEW_REQUIRED",
                                                                  "updated_at": now_iso()})
            self.st.approvals.request("CONTENT", content_id, c["influencer_id"], {"version": version, "comment": comment})
            v = self.validate(content_id, persist=True)
            self.db.audit(actor, "content.revise", "CONTENT", content_id, {"version": version})
        return {"ok": True, "content_id": content_id, "version": version, "status": "REVIEW_REQUIRED", "validation": v}

    # ------------------------------------------------------------------
    def validate(self, content_id: str, persist: bool = False) -> dict:
        """설정·품질·권리·정책 검사 (6.2, 11.2)."""
        c = self._content(content_id)
        story = self._story(c["story_id"], require_approved=False)
        profile = self.st.influencers._require(c["influencer_id"])["profile_json"]
        issues = []

        def add(sev, code, msg):
            issues.append({"severity": sev, "code": code, "message": msg})

        if story["status"] not in ("APPROVED", "PUBLISHED"):
            add("ERROR", "STORY_NOT_APPROVED", "원본 스토리가 확정 상태가 아닙니다.")
        if c["content_type"] in TEXT_FORMATS:
            data = self.current_data(content_id)
            text = _text_of(data)
            disclosure = DISCLOSURE.get(c["language"], "")
            policy = profile.get("content_policy") or {}
            if disclosure and disclosure not in text:
                add("ERROR", "AI_DISCLOSURE_MISSING", "AI 생성 콘텐츠 표시가 없습니다.")
            if data.get("is_ad") and not any(tag in text for tag in ("#광고", "#협찬", "#ad", "#quảngcáo")):
                add("ERROR", "AD_DISCLOSURE_MISSING", "광고·협찬 콘텐츠 표시가 없습니다.")
            for banned in policy.get("banned_topics", []):
                if banned and banned in text:
                    add("ERROR", "POLICY_VIOLATION", f"금지 주제 '{banned}' 포함")
            limit = PLATFORMS.get(c["platform"], {}).get("max_caption")
            if limit and c["content_type"] == "instagram_post" and len(data.get("caption", "")) > limit:
                add("ERROR", "TOO_LONG", f"캡션이 {limit}자를 넘습니다.")
            if c["content_type"] == "shorts_script":
                dur = data.get("duration_sec") or 0
                if not 15 <= int(dur) <= 60:
                    add("WARNING", "DURATION", f"쇼츠 길이 {dur}초 (권장 15~60초)")
            # 스토리에 없는 인물·장소가 들어갔는지 (다른 스토리의 엔티티 이름을 기준으로 검사)
            story_names = {x.get("name") for k in ("characters", "places", "items") for x in story["data_json"].get(k, [])}
            for ent in self.db.query("SELECT DISTINCT name, entity_type FROM entities WHERE influencer_id = ? "
                                     "AND entity_type IN ('PERSON','PLACE')", (c["influencer_id"],)):
                if len(ent["name"]) >= 2 and ent["name"] in text and ent["name"] not in story_names:
                    add("WARNING", "OUTSIDE_STORY", f"원본 스토리에 없는 {ent['entity_type']} '{ent['name']}'이(가) 언급됩니다.")
            if data.get("_provider") == "template":
                add("INFO", "TEMPLATE_TEXT", "템플릿 생성기로 만든 문구입니다. 게시 전에 자연스러운지 확인하세요.")
        else:
            assets = self.db.query("SELECT asset_id, is_placeholder FROM assets WHERE content_id = ?", (content_id,))
            if not assets:
                add("ERROR", "NO_ASSETS", "생성된 파일이 없습니다.")
            if any(a["is_placeholder"] for a in assets):
                add("ERROR", "PLACEHOLDER_ASSET", "자리표시 이미지가 포함되어 있습니다 (이미지 생성 모델 미연결). 게시할 수 없습니다.")
            add("INFO", "FACE_CONSISTENCY_REVIEW", "인물 얼굴·헤어·의상이 캐릭터 설정과 같은지 사람이 확인해야 합니다.")
        errors = sum(1 for i in issues if i["severity"] == "ERROR")
        result = {"ok": errors == 0, "errors": errors, "issues": issues}
        if persist and c["content_type"] in TEXT_FORMATS:
            data = self.current_data(content_id)
            data["validation"] = result
            self.db.execute("UPDATE content_versions SET content_data = ? WHERE content_id = ? AND version_no = ?",
                            (dumps(data), content_id, c["current_version"]))
        return result

    def decide(self, content_id: str, decision: str, actor: str, comment: str = "") -> dict:
        c = self._content(content_id)
        self.st.policy.require_decide(actor, "CONTENT")
        if decision == "APPROVED":
            v = self.validate(content_id)
            if not v["ok"]:
                raise StudioError("VALIDATION_FAILED", "콘텐츠 검사 오류가 있어 승인할 수 없습니다.", validation=v)
            status = "APPROVED"
        elif decision == "REJECTED":
            status = "REJECTED"
        else:
            status = "CHANGES_REQUESTED"
        with self.db.tx():
            self.db.update("contents", "content_id", content_id, {"status": status, "updated_at": now_iso()})
            self.db.execute("UPDATE assets SET review_status = ? WHERE content_id = ?", (status, content_id))
            self.st.approvals.close("CONTENT", content_id, decision, actor, comment)
            self.db.audit(actor, f"content.{decision.lower()}", "CONTENT", content_id, {"comment": comment})
        return {"ok": True, "content_id": content_id, "status": status}

    def get(self, content_id: str) -> dict:
        c = self._content(content_id)
        versions = self.db.query("SELECT version_no, created_by, created_at FROM content_versions WHERE content_id = ? "
                                 "ORDER BY version_no", (content_id,))
        assets = self.db.query("SELECT * FROM assets WHERE content_id = ?", (content_id,))
        pubs = self.db.query("SELECT * FROM publications WHERE content_id = ?", (content_id,))
        return {"ok": True, "content": c, "data": self.current_data(content_id), "versions": versions,
                "assets": assets, "publications": pubs}

    def list(self, influencer_id: str | None = None, status: str | None = None, story_id: str | None = None,
             limit: int = 100) -> dict:
        sql = "SELECT c.*, s.title AS story_title FROM contents c JOIN stories s ON s.story_id = c.story_id WHERE 1=1"
        params: list = []
        for col, val in (("c.influencer_id", influencer_id), ("c.status", status), ("c.story_id", story_id)):
            if val:
                sql += f" AND {col} = ?"
                params.append(val)
        return {"ok": True, "contents": self.db.query(sql + " ORDER BY c.created_at DESC LIMIT ?", [*params, limit])}

    # ---- 미디어 작업 (비동기) -----------------------------------------
    def generate_image(self, story_id: str, count: int = 3, aspect: str = "4:5", idempotency_key: str | None = None,
                       actor: str = "agent") -> dict:
        self.st.policy.check(actor, "generate_media")
        story = self._story(story_id)
        if not 1 <= count <= 10:
            raise StudioError("INVALID_INPUT", "count 는 1~10 입니다.")
        used = self.db.one("SELECT COUNT(*) AS n FROM assets WHERE asset_type = 'IMAGE' AND created_at >= ?",
                           (today(),))["n"]
        if used + count > self.st.settings.daily_image_budget:
            raise StudioError("BUDGET_EXCEEDED", f"오늘 이미지 생성 한도({self.st.settings.daily_image_budget}장)를 넘습니다.",
                              used_today=used)
        key = idempotency_key or f"image:{story_id}:{count}:{aspect}"
        existing = self.db.one("SELECT target_id FROM jobs WHERE idempotency_key = ?", (key,))
        if existing:  # 같은 요청이면 새 콘텐츠·작업을 만들지 않는다
            job = self.st.jobs.enqueue("content_image", {}, story["influencer_id"], existing["target_id"], key)
            return {"ok": True, "job": job, "content_id": existing["target_id"]}
        with self.db.tx():
            cid = self._insert_content(story, "instagram", "image_set", "none", f"{story['title']} · 이미지 {count}장",
                                       {"count": count, "aspect": aspect, "idempotency_key": key}, actor)
            job = self.st.jobs.enqueue("content_image", {"story_id": story_id, "content_id": cid, "count": count,
                                                         "aspect": aspect}, story["influencer_id"], cid, key)
        return {"ok": True, "job": job, "content_id": cid,
                "message": "이미지 생성 작업을 등록했습니다. job_status 로 진행 상태를 확인하세요."}

    def generate_video(self, story_id: str, idempotency_key: str | None = None, actor: str = "agent") -> dict:
        self.st.policy.check(actor, "generate_media")
        story = self._story(story_id)
        key = idempotency_key or f"video:{story_id}"
        existing = self.db.one("SELECT target_id FROM jobs WHERE idempotency_key = ?", (key,))
        if existing:
            job = self.st.jobs.enqueue("content_video", {}, story["influencer_id"], existing["target_id"], key)
            return {"ok": True, "job": job, "content_id": existing["target_id"]}
        with self.db.tx():
            cid = self._insert_content(story, "youtube_shorts", "video", "none", f"{story['title']} · 쇼츠 영상",
                                       {"idempotency_key": key}, actor)
            job = self.st.jobs.enqueue("content_video", {"story_id": story_id, "content_id": cid},
                                       story["influencer_id"], cid, key, max_retries=0)
        return {"ok": True, "job": job, "content_id": cid}

    def generate_audio(self, story_id: str, language: str = "ko", idempotency_key: str | None = None,
                       actor: str = "agent") -> dict:
        self.st.policy.check(actor, "generate_media")
        story = self._story(story_id)
        key = idempotency_key or f"audio:{story_id}:{language}"
        job = self.st.jobs.enqueue("content_audio", {"story_id": story_id, "language": language},
                                   story["influencer_id"], story_id, key, max_retries=0)
        return {"ok": True, "job": job}

    # ---- 작업 처리기 -------------------------------------------------
    def run_image_job(self, job: dict, progress) -> dict:
        p = job["params_json"]
        story = self._story(p["story_id"])
        inf = self.st.influencers._require(story["influencer_id"])
        profile = inf["profile_json"]
        prod = profile.get("production") or {}
        w, h = {"4:5": (1080, 1350), "9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}.get(p["aspect"], (1080, 1350))
        try:
            scenes = self.st.llm.complete_json("content_text", {
                "format": "image_prompts", "count": p["count"], "language": "en",
                "story": {k: story["data_json"].get(k) for k in ("title", "summary", "characters", "places", "items")}})
            prompts = [x["prompt"] for x in scenes.get("prompts", []) if isinstance(x, dict) and x.get("prompt")]
        except LLMError:
            prompts = []
        if len(prompts) < p["count"]:
            fallback = task_content_text({"format": "image_prompts", "count": p["count"], "story": story["data_json"]})
            prompts = [x["prompt"] for x in fallback["prompts"]]
        ref = self.db.one("SELECT file_path FROM assets WHERE influencer_id = ? AND asset_type = 'REFERENCE' "
                          "ORDER BY created_at LIMIT 1", (inf["influencer_id"],))
        adapter = self.st.image
        out_dir = safe_path(self.st.settings.media_dir, inf["influencer_id"], "images", story["story_id"])
        out_dir.mkdir(parents=True, exist_ok=True)
        produced = []
        for i in range(p["count"]):
            slot = str(i)
            done = self.db.one("SELECT asset_id FROM assets WHERE job_id = ? AND slot = ?", (job["job_id"], slot))
            if done:  # 재시도 시 이미 만든 이미지는 다시 만들지 않는다 (멱등성)
                produced.append(done["asset_id"])
                continue
            if self.st.jobs.is_cancelled(job["job_id"]):
                break
            prompt = (f"{prod.get('character_prompt', '')}, {prompts[i]}, {prod.get('image_style', '')}, "
                      f"photorealistic, consistent character")
            seed = int(prod.get("seed", 0)) + i
            path = out_dir / f"{job['job_id']}_{i + 1}.png"
            try:
                meta = adapter.generate(prompt, NEGATIVE_PROMPT, seed, w, h, path, ref["file_path"] if ref else None)
            except ImageError as e:
                raise RuntimeError(f"{i + 1}번째 이미지 생성 실패: {e}") from e
            aid = new_id("ast")
            self.db.insert("assets", {
                "asset_id": aid, "influencer_id": inf["influencer_id"], "story_id": story["story_id"],
                "content_id": p["content_id"], "job_id": job["job_id"], "slot": slot, "asset_type": "IMAGE",
                "file_path": str(path), "prompt": prompt, "model": meta.get("model"),
                "params_json": {"seed": seed, "negative": NEGATIVE_PROMPT, "width": w, "height": h,
                                "reference": ref["file_path"] if ref else None},
                "is_placeholder": 1 if adapter.is_placeholder else 0, "review_status": "REVIEW_REQUIRED",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "created_at": now_iso()})
            produced.append(aid)
            progress((i + 1) / p["count"])
        return {"assets": produced, "provider": adapter.name, "placeholder": adapter.is_placeholder,
                "review_required": True}

    def run_video_job(self, job: dict, progress) -> dict:
        p = job["params_json"]
        story = self._story(p["story_id"])
        if not self.st.video.available():
            raise MediaNotConfigured("FFmpeg 이 설치되어 있지 않아 영상을 만들 수 없습니다 (NOT_CONFIGURED).")
        images = [Path(a["file_path"]) for a in self.db.query(
            "SELECT file_path FROM assets WHERE story_id = ? AND asset_type = 'IMAGE' AND is_placeholder = 0 "
            "ORDER BY created_at", (story["story_id"],))]
        script = self.db.one("SELECT content_id FROM contents WHERE story_id = ? AND content_type = 'shorts_script' "
                             "AND language = 'ko' ORDER BY created_at LIMIT 1", (story["story_id"],))
        scenes = self.current_data(script["content_id"]).get("scenes", []) if script else []
        dur = [max(2.0, 30 / max(len(images), 1))] * len(images)
        if scenes and len(scenes) == len(images):
            dur = [4.0 for _ in scenes]
        out = safe_path(self.st.settings.media_dir, story["influencer_id"], "videos", f"{job['job_id']}.mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        meta = self.st.video.render(images, dur, out)
        aid = new_id("ast")
        self.db.insert("assets", {"asset_id": aid, "influencer_id": story["influencer_id"], "story_id": story["story_id"],
                                  "content_id": p["content_id"], "job_id": job["job_id"], "slot": "0",
                                  "asset_type": "VIDEO", "file_path": str(out), "prompt": None, "model": meta["model"],
                                  "params_json": meta, "is_placeholder": 0, "review_status": "REVIEW_REQUIRED",
                                  "sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "created_at": now_iso()})
        return {"assets": [aid], "review_required": True}

    def run_audio_job(self, job: dict, progress) -> dict:
        story = self._story(job["params_json"]["story_id"])
        profile = self.st.influencers._require(story["influencer_id"])["profile_json"]
        self.st.audio.synthesize(story["data_json"].get("body", ""), profile.get("voice") or {}, Path("unused"))
        return {}

    # ------------------------------------------------------------------
    def package(self, story_id: str, platform: str | None = None, only_approved: bool = False) -> dict:
        """채널별 콘텐츠 파일과 메타데이터를 한 폴더로 구성합니다."""
        story = self._story(story_id, require_approved=False)
        contents = self.db.query("SELECT * FROM contents WHERE story_id = ?", (story_id,))
        if platform:
            contents = [c for c in contents if c["platform"] == platform]
        if only_approved:
            contents = [c for c in contents if c["status"] in ("APPROVED", "PUBLISHED")]
        if not contents:
            raise StudioError("NOTHING_TO_PACKAGE", "패키지로 만들 콘텐츠가 없습니다.")
        base = safe_path(self.st.settings.export_dir, "packages", story_id, platform or "all")
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)
        manifest = {"story_id": story_id, "influencer_id": story["influencer_id"], "story_title": story["title"],
                    "story_date": story["story_date"], "created_at": now_iso(), "items": []}
        for c in contents:
            folder = base / f"{c['content_type']}_{c['language']}_{c['content_id'][-6:]}"
            folder.mkdir()
            entry = {"content_id": c["content_id"], "platform": c["platform"], "type": c["content_type"],
                     "language": c["language"], "status": c["status"], "files": []}
            if c["content_type"] in TEXT_FORMATS:
                data = self.current_data(c["content_id"])
                text = _text_of(data)
                if data.get("hashtags"):
                    text += "\n\n" + " ".join(data["hashtags"])
                (folder / "text.txt").write_text(text, encoding="utf-8")
                (folder / "data.json").write_text(dumps({k: v for k, v in data.items() if k != "validation"}),
                                                  encoding="utf-8")
                entry["files"] += ["text.txt", "data.json"]
            for a in self.db.query("SELECT * FROM assets WHERE content_id = ?", (c["content_id"],)):
                src = Path(a["file_path"])
                if src.exists():
                    dst = folder / src.name
                    shutil.copy2(src, dst)
                    entry["files"].append(src.name)
                    entry.setdefault("placeholder_files", []).append(src.name) if a["is_placeholder"] else None
            manifest["items"].append(entry)
        (base / "manifest.json").write_text(dumps(manifest), encoding="utf-8")
        return {"ok": True, "story_id": story_id, "path": str(base), "items": len(manifest["items"])}
