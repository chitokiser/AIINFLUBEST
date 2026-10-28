"""모든 모듈을 묶는 진입점. MCP 서버와 웹 대시보드가 같은 Studio 를 사용합니다."""

from __future__ import annotations

from .adapters.embedding import build_embedder
from .adapters.image import ImageAdapter, build_image_adapter
from .adapters.llm import LLMAdapter, build_llm
from .adapters.media import FFmpegSlideshow, NotConfiguredAudio
from .config import Settings, load_settings
from .db import Store
from .services.analytics import AnalyticsService
from .services.approvals import ApprovalService
from .services.archive import ArchiveService
from .services.content import ContentService
from .services.continuity import ContinuityService
from .services.influencers import InfluencerService
from .services.jobs import JobService
from .services.memory import MemoryService
from .services.policy import Policy
from .services.publish import PublishService
from .services.scheduler import SchedulerService
from .services.stories import StoryService


class Studio:
    def __init__(self, settings: Settings | None = None, llm: LLMAdapter | None = None,
                 image: ImageAdapter | None = None):
        self.settings = settings or load_settings()
        self.db = Store(self.settings.db_path)
        self.llm = llm or build_llm(self.settings)
        self.embedder = build_embedder(self.settings)
        self.image = image or build_image_adapter(self.settings)
        self.video = FFmpegSlideshow(self.settings.ffmpeg_path)
        self.audio = NotConfiguredAudio()
        self.policy = Policy(self.settings)

        self.approvals = ApprovalService(self)
        self.influencers = InfluencerService(self)
        self.memory = MemoryService(self)
        self.continuity = ContinuityService(self)
        self.stories = StoryService(self)
        self.content = ContentService(self)
        self.jobs = JobService(self)
        self.publish = PublishService(self)
        self.analytics = AnalyticsService(self)
        self.archive = ArchiveService(self)
        self.scheduler = SchedulerService(self)

        self.jobs.register("content_image", self.content.run_image_job)
        self.jobs.register("content_video", self.content.run_video_job)
        self.jobs.register("content_audio", self.content.run_audio_job)

    def start_background(self) -> None:
        """작업 워커와 스케줄러(30초 간격)를 백그라운드 스레드로 실행합니다."""
        if self.settings.run_worker:
            self.jobs.recover_stale()
            self.jobs.start_worker(tick=self.scheduler.run_due)

    def models_status(self) -> dict:
        llm_primary = getattr(self.llm, "primary", self.llm)
        return {
            "llm": {"provider": self.settings.llm_provider, "adapter": self.llm.name,
                    "model": self.settings.ollama_model, "ollama_available": llm_primary.available()
                    if llm_primary.name == "ollama" else None},
            "embedding": self.embedder.name,
            "image": {"adapter": self.image.name, "placeholder": self.image.is_placeholder,
                      "comfyui_url": self.settings.comfyui_url, "workflow": str(self.settings.comfyui_workflow)},
            "video": {"adapter": self.video.name, "available": self.video.available()},
            "audio": {"adapter": self.audio.name, "available": False},
            "policy": {"agent_can_approve_stories": self.settings.agent_can_approve_stories,
                       "agent_can_approve_content": self.settings.agent_can_approve_content,
                       "agent_can_approve_character_changes": self.settings.agent_can_approve_character_changes,
                       "daily_image_budget": self.settings.daily_image_budget},
            "data_dir": str(self.settings.data_dir),
        }

    def dashboard(self) -> dict:
        one = self.db.one
        return {
            "influencers": one("SELECT COUNT(*) AS n FROM influencers WHERE status != 'ARCHIVED'")["n"],
            "stories": one("SELECT COUNT(*) AS n FROM stories")["n"],
            "pending_reviews": one("SELECT COUNT(*) AS n FROM approvals WHERE decision = 'PENDING'")["n"],
            "jobs_running": one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('QUEUED','RUNNING')")["n"],
            "jobs_failed": one("SELECT COUNT(*) AS n FROM jobs WHERE status = 'FAILED'")["n"],
            "published": one("SELECT COUNT(*) AS n FROM publications WHERE status = 'PUBLISHED'")["n"],
            "manual_required": one("SELECT COUNT(*) AS n FROM publications WHERE status = 'MANUAL_REQUIRED'")["n"],
            "recent_audit": self.db.query("SELECT actor_id, action, target_type, target_id, created_at FROM audit_logs "
                                          "ORDER BY created_at DESC LIMIT 15"),
        }

    def close(self) -> None:
        self.jobs.stop_worker()
        self.db.close()
