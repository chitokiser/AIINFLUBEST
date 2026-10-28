"""⑤ MCP 실행 엔진 (기획서 7장).

기능별 도구를 제공하고, AI 에이전트가 이를 조합해 작업합니다.
이 서버로 연결된 행위자는 'agent' 이며, 자동화 정책(11장)에 따라
공개 게시용 콘텐츠 승인·캐릭터 핵심 설정 변경 승인은 운영자(웹 대시보드)만 할 수 있습니다.
"""

from __future__ import annotations

import functools
from typing import Any

from mcp.server.mcpserver import MCPServer

from .studio import Studio
from .util import StudioError

INSTRUCTIONS = """AI Influencer Studio: 가상 인플루언서의 캐릭터·세계관·스토리·장기 기억을 관리하고 콘텐츠를 만듭니다.
작업 순서 원칙:
1) influencer_get 으로 프로필을 읽고, memory_search / story_timeline 으로 과거를 확인합니다.
2) story_generate (또는 story_continue / season_plan) 로 초안을 만들고 story_validate 결과를 확인합니다.
3) 스토리를 승인(story_approve)한 뒤에만 콘텐츠를 파생합니다 (content_generate_text / content_generate_image).
4) 이미지·영상 생성은 작업 ID 를 돌려주므로 job_status 로 상태를 확인합니다.
5) 게시는 운영자가 검수함에서 승인한 콘텐츠만 publish_prepare → publish_execute 로 진행합니다.
모순이 발견되면 기존 기록을 덮어쓰지 말고 story_revise / memory_update 로 새 버전을 만드세요."""

mcp = MCPServer("AI Influencer Studio", instructions=INSTRUCTIONS, version="0.1.0")
_studio: Studio | None = None
ACTOR = "agent"


def studio() -> Studio:
    global _studio
    if _studio is None:
        _studio = Studio()
        _studio.start_background()
    return _studio


def tool(fn):
    """StudioError 를 예외 대신 {ok: false, error, message} 로 돌려줍니다."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except StudioError as e:
            return e.to_dict()
    return mcp.tool()(wrapper)


# ---------------------------------------------------------------- A. 인플루언서
@tool
def influencer_create(description: str, overrides: dict | None = None) -> dict:
    """자연어 설명으로 새 AI 인플루언서 초안(DRAFT)을 만듭니다. 이름·나이·활동 지역·관심사·말투·장기 목표 등을
    설명에 넣으세요. overrides 로 특정 항목(예: {"name": "김하린"})을 고정할 수 있습니다.
    확인 후 influencer_update(status="ACTIVE") 로 등록을 완료해야 스토리를 만들 수 있습니다."""
    return studio().influencers.create(description, overrides, actor=ACTOR)


@tool
def influencer_get(influencer_id: str) -> dict:
    """인물 설정(프로필·외모·말투·콘텐츠 정책), 세계관, 통계, 미해결 사건, 관계, 대기 중인 설정 변경안을 조회합니다."""
    return studio().influencers.get(influencer_id)


@tool
def influencer_update(influencer_id: str, changes: dict | None = None, reason: str = "", status: str | None = None) -> dict:
    """프로필을 수정하거나 status 를 ACTIVE/ARCHIVED 로 바꿉니다. 이름·나이·직업·성격·외모 등 핵심 설정 변경은
    사유(reason)가 필요하며 운영자 승인 후 반영됩니다. 모든 변경은 버전으로 남습니다."""
    return studio().influencers.update(influencer_id, changes, reason, status, actor=ACTOR)


@tool
def influencer_list(status: str | None = None) -> dict:
    """전체 인플루언서 목록 (status: DRAFT | ACTIVE | ARCHIVED 로 필터)."""
    return studio().influencers.list(status)


@tool
def influencer_archive(influencer_id: str, reason: str = "") -> dict:
    """인플루언서 활동을 중지하고 보관합니다 (데이터 삭제 없음, 스케줄 중지)."""
    return studio().influencers.archive(influencer_id, reason or "운영 중지", actor=ACTOR)


@tool
def influencer_relate(influencer_id: str, other_influencer_id: str, relation_type: str) -> dict:
    """두 인플루언서 사이의 관계(친구·동료·가족·사업 파트너 등)를 설정합니다. 공동 콘텐츠(co_influencers)의 전제 조건입니다."""
    return studio().influencers.relate(influencer_id, other_influencer_id, relation_type, actor=ACTOR)


# ---------------------------------------------------------------- B. 스토리
@tool
def story_generate(influencer_id: str, topic: str = "", story_date: str | None = None, story_type: str = "SERIAL",
                   is_major: bool = False, co_influencers: list[str] | None = None, is_flashback: bool = False) -> dict:
    """프로필·과거 기억·미해결 사건을 반영해 새 스토리 초안을 만들고 모순 검사 결과와 함께 돌려줍니다.
    topic 예: '미딩에서 방문했던 식당의 이전에 소개하지 않은 메뉴'. story_date 생략 시 마지막 스토리 다음 날."""
    return studio().stories.generate(influencer_id, topic, story_date, story_type, is_major=is_major,
                                     co_influencers=co_influencers, is_flashback=is_flashback, actor=ACTOR)


@tool
def story_continue(story_id: str, topic: str = "") -> dict:
    """확정된 스토리의 다음 이야기(next_hooks 를 이어받음)를 초안으로 만듭니다."""
    return studio().stories.continue_story(story_id, topic, actor=ACTOR)


@tool
def story_get(story_id: str) -> dict:
    """스토리 원문·에피소드·검증 결과·파생 콘텐츠·자산·연결된 스토리를 조회합니다."""
    return studio().stories.get(story_id)


@tool
def story_search(influencer_id: str, query: str = "", status: str | None = None, date_from: str | None = None,
                 date_to: str | None = None, limit: int = 20) -> dict:
    """과거 스토리를 검색합니다. query 가 있으면 의미·키워드·관계 기반 검색, 없으면 상태·기간으로 조회합니다."""
    return studio().stories.search(influencer_id, query, status, limit, date_from, date_to)


@tool
def story_validate(story_id: str) -> dict:
    """시간·인물·관계·장소·미해결 사건·프로필·금지 주제·다른 인플루언서 혼입을 검사합니다. 기록은 바꾸지 않습니다."""
    return studio().stories.validate(story_id)


@tool
def story_approve(story_id: str) -> dict:
    """스토리를 확정하고 장소·인물·사건·기억을 아카이브에 저장합니다. 모순 검사 ERROR 가 있으면 승인되지 않습니다."""
    return studio().stories.approve(story_id, actor=ACTOR)


@tool
def story_revise(story_id: str, changes: dict | None = None, instruction: str = "") -> dict:
    """스토리 수정안을 만듭니다. 확정된 스토리는 덮어쓰지 않고 새 버전을 만들며, 승인 시 원본은 SUPERSEDED 가 됩니다.
    changes 예: {"body": "...", "characters": [{"name": "린", "relation": "친구"}]}"""
    return studio().stories.revise(story_id, changes, instruction, actor=ACTOR)


@tool
def story_timeline(influencer_id: str, date_from: str | None = None, date_to: str | None = None,
                   include_drafts: bool = False) -> dict:
    """인물별 스토리와 사건(설정 변경 포함)을 시간순으로 조회합니다."""
    return studio().stories.timeline(influencer_id, date_from, date_to, include_drafts)


@tool
def season_plan(influencer_id: str, total_days: int = 30, theme: str = "", start_date: str | None = None,
                chunk_days: int = 7) -> dict:
    """여러 날짜의 시즌(예: 30일, 7일마다 중요한 사건, 마지막 날 완결)을 계획하고 첫 구간(chunk_days)만 초안으로 만듭니다."""
    return studio().stories.plan_season(influencer_id, total_days, theme, start_date, chunk_days, actor=ACTOR)


@tool
def season_next_chunk(season_id: str) -> dict:
    """이전 구간이 모두 승인된 경우에만 다음 구간 에피소드 초안을 만듭니다."""
    return studio().stories.generate_season_chunk(season_id, actor=ACTOR)


@tool
def season_approve_chunk(season_id: str) -> dict:
    """검수 대기 중인 시즌 에피소드를 날짜 순서대로 승인합니다 (오류가 있는 에피소드에서 멈춤)."""
    return studio().stories.approve_season_chunk(season_id, actor=ACTOR)


# ---------------------------------------------------------------- C. 아카이브·기억
@tool
def memory_search(influencer_id: str, query: str, types: list[str] | None = None, date_from: str | None = None,
                  date_to: str | None = None, entity: str | None = None, limit: int = 10) -> dict:
    """해당 인플루언서의 확정된 기억·스토리·사건만 검색합니다 (키워드+의미+관계+시간). types: MEMORY|STORY|EVENT."""
    return studio().memory.search(influencer_id, query, types, limit, date_from, date_to, entity)


@tool
def memory_get(memory_id: str) -> dict:
    """기억 상세와 수정 이력(이전 버전·새 버전)을 조회합니다."""
    return studio().memory.get(memory_id)


@tool
def memory_create(influencer_id: str, content: str, memory_type: str = "FACT", fact_type: str = "FICTION",
                  source_story_id: str | None = None, valid_from: str | None = None) -> dict:
    """새 기억 '후보'를 만듭니다 (검수 후 ACTIVE). fact_type: FICTION(가상 설정) | REAL(현실에서 확인된 사실)."""
    return studio().memory.create(influencer_id, content, memory_type, source_story_id, "agent", "CANDIDATE",
                                  fact_type, 0.8, None, valid_from, actor=ACTOR)


@tool
def memory_update(memory_id: str, content: str, reason: str) -> dict:
    """검증된 기억을 수정합니다. 원본은 SUPERSEDED 로 보존되고 새 버전이 만들어집니다."""
    return studio().memory.update(memory_id, content, reason, actor=ACTOR)


@tool
def archive_asset(influencer_id: str, source_path: str, asset_type: str = "IMAGE", story_id: str | None = None,
                  prompt: str = "", model: str = "") -> dict:
    """이미지·영상·음성 파일을 아카이브에 등록합니다. asset_type=REFERENCE 는 캐릭터 참조 이미지로 이미지 생성에 쓰입니다.
    허용된 폴더(data 폴더, AIS_IMPORT_DIRS) 안의 파일만 가져올 수 있습니다."""
    return studio().archive.archive_asset(influencer_id, source_path, asset_type, story_id, None, prompt, model,
                                          actor=ACTOR)


@tool
def archive_export(influencer_id: str) -> dict:
    """인물별 프로필·버전·스토리·사건·관계·기억·콘텐츠·자산·성과를 JSON 하나로 내보냅니다."""
    return studio().archive.export(influencer_id)


@tool
def archive_backup() -> dict:
    """DB 스냅샷과 미디어 파일을 zip 으로 백업합니다."""
    return studio().archive.backup(actor=ACTOR)


# ---------------------------------------------------------------- D. 콘텐츠
@tool
def content_plan(influencer_id: str, days: int = 7) -> dict:
    """다가오는 기간의 제작 계획(날짜별 주제·결과물)을 제안합니다."""
    return studio().content.plan(influencer_id, days)


@tool
def content_generate_text(story_id: str, formats: list[str] | None = None, languages: list[str] | None = None,
                          variants: int = 1, shorts_duration: int = 30) -> dict:
    """확정된 스토리에서 텍스트 콘텐츠를 만듭니다. formats: instagram_post | shorts_script | blog,
    languages 예: ["ko","vi"], variants: 인스타그램 문구 개수. 결과는 검수 대기(REVIEW_REQUIRED)로 저장됩니다."""
    return studio().content.generate_text(story_id, formats, languages, variants, shorts_duration, actor=ACTOR)


@tool
def content_generate_image(story_id: str, count: int = 3, aspect: str = "4:5", idempotency_key: str | None = None) -> dict:
    """캐릭터 참조 정보(외모 프롬프트·고정 시드·참조 이미지)로 이미지 생성 작업을 등록하고 job_id 를 돌려줍니다.
    aspect: 4:5 | 9:16 | 1:1 | 16:9. 같은 idempotency_key 로 다시 요청하면 중복 생성하지 않습니다."""
    return studio().content.generate_image(story_id, count, aspect, idempotency_key, actor=ACTOR)


@tool
def content_generate_video(story_id: str, idempotency_key: str | None = None) -> dict:
    """스토리 이미지와 쇼츠 대본으로 세로 영상 작업을 등록합니다 (FFmpeg 필요, 없으면 NOT_CONFIGURED 로 실패)."""
    return studio().content.generate_video(story_id, idempotency_key, actor=ACTOR)


@tool
def content_generate_audio(story_id: str, language: str = "ko") -> dict:
    """내레이션 음성 작업을 등록합니다. 현재 TTS 어댑터가 연결되지 않아 NOT_CONFIGURED 로 끝납니다."""
    return studio().content.generate_audio(story_id, language, actor=ACTOR)


@tool
def content_translate(content_id: str, target_language: str) -> dict:
    """텍스트 콘텐츠를 다른 언어로 번역·현지화한 새 콘텐츠를 만듭니다 (원본과 같은 story_id)."""
    return studio().content.translate(content_id, target_language, actor=ACTOR)


@tool
def content_validate(content_id: str) -> dict:
    """원본 스토리 확정 여부, AI 생성·광고 표시, 금지 주제, 길이, 스토리 밖 인물·장소, 자리표시 이미지를 검사합니다."""
    return studio().content.validate(content_id)


@tool
def content_get(content_id: str) -> dict:
    """콘텐츠 현재 버전·수정 이력·자산·게시 기록을 조회합니다."""
    return studio().content.get(content_id)


@tool
def content_list(influencer_id: str | None = None, status: str | None = None, story_id: str | None = None) -> dict:
    """콘텐츠 목록 (status: REVIEW_REQUIRED | APPROVED | PUBLISHED ...)."""
    return studio().content.list(influencer_id, status, story_id)


@tool
def content_revise(content_id: str, changes: dict, comment: str = "") -> dict:
    """콘텐츠 수정본(새 버전)을 만들고 다시 검수 대기로 돌립니다. changes 예: {"caption": "..."}"""
    return studio().content.revise(content_id, changes, actor=ACTOR, comment=comment)


@tool
def content_package(story_id: str, platform: str | None = None) -> dict:
    """스토리에서 파생된 콘텐츠 파일과 manifest.json 을 채널별 폴더로 묶습니다."""
    return studio().content.package(story_id, platform)


# ---------------------------------------------------------------- 검수·작업
@tool
def review_queue(target_type: str | None = None, influencer_id: str | None = None) -> dict:
    """검수 대기 목록 (STORY | CONTENT | CHARACTER_VERSION | INFLUENCER | MEMORY)."""
    return studio().approvals.list("PENDING", target_type, influencer_id)


@tool
def review_decide(approval_id: str, decision: str, comment: str = "") -> dict:
    """검수 결정을 기록합니다 (APPROVED | REJECTED | CHANGES_REQUESTED). 콘텐츠·캐릭터 변경 승인은 정책상 운영자만 가능합니다."""
    return studio().approvals.decide(approval_id, decision, ACTOR, comment)


@tool
def job_status(job_id: str | None = None, status: str | None = None) -> dict:
    """작업 하나(job_id) 또는 상태별 작업 목록을 조회합니다."""
    s = studio()
    return s.jobs.get(job_id) if job_id else s.jobs.list(status)


@tool
def job_retry(job_id: str) -> dict:
    """실패·취소된 작업을 다시 대기열에 넣습니다 (이미 만든 결과물은 다시 만들지 않음)."""
    return studio().jobs.retry(job_id, actor=ACTOR)


@tool
def job_cancel(job_id: str) -> dict:
    """대기 중이거나 실행 중인 작업을 취소합니다."""
    return studio().jobs.cancel(job_id, actor=ACTOR)


@tool
def schedule_set(influencer_id: str, run_time: str, formats: list[str] | None = None, images: int = 3,
                 languages: list[str] | None = None, enabled: bool = True) -> dict:
    """매일 run_time(HH:MM)에 스토리 초안을 만들고, 자동 승인된 경우 텍스트·이미지 초안까지 만드는 스케줄. 게시는 하지 않습니다."""
    task = {"formats": formats or ["instagram_post", "shorts_script"], "images": images, "languages": languages}
    return studio().scheduler.set(influencer_id, run_time, task, enabled, actor=ACTOR)


# ---------------------------------------------------------------- E. 배포·성과
@tool
def publish_prepare(content_id: str, platform: str | None = None) -> dict:
    """승인된 콘텐츠의 게시 패키지를 만들고 게시 기록(PREPARED)을 등록합니다. 플랫폼별 자동 게시 지원 여부를 함께 돌려줍니다."""
    return studio().publish.prepare(content_id, platform, actor=ACTOR)


@tool
def publish_schedule(publication_id: str, scheduled_at: str) -> dict:
    """게시를 예약합니다 (ISO 시간, 예: 2026-11-01T09:00)."""
    return studio().publish.schedule(publication_id, scheduled_at, actor=ACTOR)


@tool
def publish_execute(publication_id: str) -> dict:
    """승인된 게시 작업을 실행합니다. API 가 연결되지 않은 플랫폼은 MANUAL_REQUIRED 와 패키지 경로를 돌려줍니다.
    이미 게시된 경우 다시 게시하지 않습니다."""
    return studio().publish.execute(publication_id, actor=ACTOR)


@tool
def publish_status(publication_id: str | None = None, content_id: str | None = None,
                   influencer_id: str | None = None) -> dict:
    """게시 상태를 조회합니다."""
    return studio().publish.status(publication_id, content_id, influencer_id)


@tool
def analytics_record(content_id: str, metrics: dict, measured_at: str | None = None) -> dict:
    """콘텐츠 성과를 기록합니다. metrics 예: {"views": 1200, "likes": 80, "comments": 12, "shares": 5, "saves": 9}"""
    return studio().analytics.record(content_id, metrics, measured_at, "agent", actor=ACTOR)


@tool
def analytics_get(content_id: str | None = None, influencer_id: str | None = None, days: int = 30) -> dict:
    """기록된 성과 데이터를 조회합니다."""
    return studio().analytics.get(content_id, influencer_id, days)


@tool
def analytics_report(influencer_id: str, days: int = 30) -> dict:
    """기간별 성과 보고서 (주제·플랫폼·스토리별 점수)."""
    return studio().analytics.report(influencer_id, days)


@tool
def content_strategy_update(influencer_id: str, days: int = 30) -> dict:
    """성과 데이터를 바탕으로 다음 제작 계획을 제안합니다. 캐릭터의 기본 성격·세계관은 바꾸지 않습니다."""
    return studio().analytics.strategy_update(influencer_id, days, actor=ACTOR)


@tool
def system_status() -> dict:
    """연결된 언어 모델·이미지·영상·음성 도구 상태, 자동화 정책, 대시보드 요약을 보여줍니다."""
    s = studio()
    return {"ok": True, "models": s.models_status(), "dashboard": s.dashboard()}


def main() -> None:
    studio()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
