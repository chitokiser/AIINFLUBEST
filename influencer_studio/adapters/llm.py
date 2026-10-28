"""언어 모델 어댑터.

`OllamaLLM` 은 로컬 Ollama 서버에 JSON 형식 출력을 요청합니다.
`TemplateLLM` 은 모델 없이 동작하는 결정적(재현 가능한) 생성기로, Ollama 가 꺼져 있거나
테스트할 때 사용됩니다. 두 어댑터 모두 같은 `complete_json(task, payload)` 인터페이스를 가집니다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from . import templates


class LLMError(Exception):
    pass


class LLMAdapter:
    name = "base"

    def available(self) -> bool:
        return True

    def complete_json(self, task: str, payload: dict) -> dict:
        raise NotImplementedError


class TemplateLLM(LLMAdapter):
    """규칙 기반 생성기. 결과에 provider='template' 이 기록되어 실제 모델 출력과 구분됩니다."""

    name = "template"

    def complete_json(self, task: str, payload: dict) -> dict:
        handler = getattr(templates, f"task_{task}", None)
        if handler is None:
            raise LLMError(f"템플릿 생성기가 지원하지 않는 작업입니다: {task}")
        return handler(payload)


TASK_INSTRUCTIONS = {
    "influencer_profile": (
        "You design a fictional virtual influencer. Return ONLY a JSON object with keys: "
        "name (fictional Korean-style full name, must not be a real celebrity), handle, age (int), gender, "
        "nationality, occupation, location {city, district, country}, personality [..], values [..], "
        "appearance {face, hair, body, style, signature_items}, voice {tone, speed, accent}, languages [..], "
        "speech_style, interests [..], background, long_term_goal, "
        "content_policy {allowed_topics [..], banned_topics [..], ad_disclosure, ai_disclosure}, "
        "production {image_style, aspect_ratios, resolution}, initial_story_ideas [3 strings]. "
        "Write values in Korean except handle. Respect every fact stated in the request."
    ),
    "story_draft": (
        "You are the story engine for a fictional influencer. Using the profile, memories, known characters, "
        "places and open threads, write the NEXT story. Keep every established fact consistent. "
        "Prefer resolving or advancing an open thread when it fits. Do not repeat items already introduced. "
        "Return ONLY JSON with keys: title, summary, story_date (YYYY-MM-DD, provided), body (Korean, 5-10 sentences, "
        "first person in the influencer's speech style), characters [{name, role, relation}], "
        "places [{name, type, area}], items [{name, place}], "
        "events [{title, description, event_type (PROMISE|MILESTONE|CONFLICT|DISCOVERY|MEETING), status (OPEN|RESOLVED|NONE), resolves_event_id}], "
        "memories [short Korean facts worth remembering], tags [..], next_hooks [..], profile_assertions {}."
    ),
    "season_plan": (
        "Plan a multi-day story season for a fictional influencer. Return ONLY JSON: "
        "{title, goal, arc_summary, days: [{day_no, theme, beat, is_major_event (bool)}]}. "
        "Put a major event every 7 days and conclude the story on the last day. Korean text."
    ),
    "content_text": (
        "Convert ONE approved story into a platform-specific text. Do not add events, people or places that are "
        "not in the story. Return ONLY JSON with keys appropriate to the format: "
        "instagram_post -> {caption, hashtags[], alt_texts[]}; shorts_script -> {title, duration_sec, scenes:[{t, visual, narration, subtitle}]}; "
        "blog -> {title, body_markdown, tags[]}; image_prompts -> {prompts:[{scene, prompt}]}. "
        "Write in the requested language. Always include the AI-generated disclosure text provided."
    ),
    "translate": (
        "Translate and localize the given JSON content values into the target language. Keep keys and structure "
        "identical. Do not add or remove facts. Return ONLY the JSON."
    ),
    "strategy": (
        "Given performance stats per topic, propose next month's story topics for the influencer WITHOUT changing "
        "their core personality or world. Return ONLY JSON: {summary, keep[], try[], reduce[], plan:[{week, theme, idea}]}. Korean."
    ),
}


class OllamaLLM(LLMAdapter):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: int = 180, num_gpu: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_gpu = num_gpu

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=3) as r:
                tags = json.loads(r.read().decode("utf-8"))
            names = {m.get("name") for m in tags.get("models", [])}
            return self.model in names or f"{self.model}:latest" in names
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def complete_json(self, task: str, payload: dict) -> dict:
        instruction = TASK_INSTRUCTIONS.get(task)
        if instruction is None:
            raise LLMError(f"알 수 없는 작업: {task}")
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.7, **({"num_gpu": self.num_gpu} if self.num_gpu is not None else {})},
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = " " + e.read().decode("utf-8", "replace")[:300]
                except OSError:
                    pass
            raise LLMError(f"Ollama 호출 실패: {e}{detail}") from e
        text = data.get("message", {}).get("content", "")
        try:
            result = json.loads(text)
        except ValueError as e:
            raise LLMError(f"모델이 JSON 이 아닌 응답을 반환했습니다: {text[:200]}") from e
        if not isinstance(result, dict):
            raise LLMError("모델 응답이 JSON 객체가 아닙니다.")
        return result


class FallbackLLM(LLMAdapter):
    """주 모델이 실패하면 템플릿 생성기로 대체하고, 어떤 생성기를 썼는지 결과에 남깁니다."""

    def __init__(self, primary: LLMAdapter, fallback: LLMAdapter):
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+{fallback.name}"
        self._primary_ok: bool | None = None

    def complete_json(self, task: str, payload: dict) -> dict:
        if self._primary_ok is None:
            self._primary_ok = self.primary.available()
        if self._primary_ok:
            try:
                result = self.primary.complete_json(task, payload)
                result.setdefault("_provider", self.primary.name)
                return result
            except LLMError as e:
                result = self.fallback.complete_json(task, payload)
                result["_provider"] = self.fallback.name
                result["_fallback_reason"] = str(e)
                return result
        result = self.fallback.complete_json(task, payload)
        result["_provider"] = self.fallback.name
        return result


def build_llm(settings) -> LLMAdapter:
    template = TemplateLLM()
    if settings.llm_provider == "template":
        return template
    ollama = OllamaLLM(settings.ollama_url, settings.ollama_model, settings.llm_timeout, settings.ollama_num_gpu)
    if settings.llm_provider == "ollama":
        return ollama
    return FallbackLLM(ollama, template)


def merge_defaults(result: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """모델 출력에 빠진 키를 기본값으로 채웁니다 (모델 출력이 불완전할 때 대비)."""
    out = dict(defaults)
    for k, v in result.items():
        if v not in (None, "", [], {}):
            out[k] = v
    return out
