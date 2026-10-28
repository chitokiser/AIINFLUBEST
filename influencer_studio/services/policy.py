"""자동화 권한 정책 (기획서 11장).

행위자(actor)
- admin  : 웹 대시보드의 운영자 (사람)
- agent  : MCP 로 연결된 AI 에이전트
- system : 내부 스케줄러·워커

스토리를 만들 수 있다고 해서 게시나 비용 발생 작업까지 할 수 있는 것은 아닙니다.
"""

from __future__ import annotations

from ..util import StudioError

# 행위자별로 항상 허용되는 작업 단계
LEVELS = {
    "search": 1,            # 검색·요약·분석
    "draft": 2,             # 스토리·콘텐츠 초안
    "generate_media": 3,    # 이미지·영상·음성 (예산 한도 내)
    "archive_write": 4,     # 검증 후 아카이브 저장
    "publish": 5,           # 공개 게시 (승인된 콘텐츠만)
    "spend": 6,             # 광고비·유료 프로모션
    "core_change": 7,       # 캐릭터 핵심 설정 변경
}

CORE_PROFILE_FIELDS = {
    "name", "age", "gender", "nationality", "occupation", "personality", "values", "appearance",
    "background", "long_term_goal", "location", "speech_style", "voice",
}


class Policy:
    def __init__(self, settings):
        self.s = settings

    def check(self, actor: str, action: str) -> None:
        if action == "spend":
            raise StudioError("FORBIDDEN", "광고비 지출·유료 프로모션은 이 시스템에서 자동 실행하지 않습니다. 별도 승인 절차가 필요합니다.")
        if actor == "admin":
            return
        allowed = {"search", "draft", "generate_media", "archive_write", "publish"}
        if action not in allowed:
            raise StudioError("FORBIDDEN", f"'{actor}' 행위자는 '{action}' 작업을 직접 실행할 수 없습니다.")

    def can_decide(self, actor: str, target_type: str) -> bool:
        if actor == "admin":
            return True
        if target_type == "STORY":
            return self.s.agent_can_approve_stories
        if target_type == "CONTENT":
            return self.s.agent_can_approve_content
        if target_type == "CHARACTER_VERSION":
            return self.s.agent_can_approve_character_changes
        if target_type in ("INFLUENCER", "MEMORY"):
            return True
        return False

    def require_decide(self, actor: str, target_type: str) -> None:
        if not self.can_decide(actor, target_type):
            raise StudioError(
                "APPROVAL_REQUIRES_HUMAN",
                f"{target_type} 승인은 운영자가 웹 대시보드의 검수함에서 직접 해야 합니다.",
            )
