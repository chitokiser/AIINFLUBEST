"""실행 설정.

API 키나 비밀번호는 코드에 넣지 않고 환경 변수(또는 .env 파일)로만 받습니다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """의존성 없이 간단한 KEY=VALUE 형식의 .env 를 읽습니다. 이미 설정된 환경 변수는 덮어쓰지 않습니다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path
    db_path: Path
    media_dir: Path
    export_dir: Path
    backup_dir: Path
    outbox_dir: Path

    # 언어 모델: auto(Ollama 사용 가능 시 Ollama, 아니면 템플릿) | ollama | template
    llm_provider: str = "auto"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    ollama_embed_model: str = ""  # 비워두면 내장 해시 임베딩 사용
    llm_timeout: int = 180
    ollama_num_gpu: int | None = None  # 0 이면 CPU 로만 실행 (GPU 드라이버 문제 시)

    # 이미지: auto(Gemini 키가 있으면 Gemini → ComfyUI → 자리표시) | gemini | comfyui | placeholder
    image_provider: str = "auto"
    gemini_api_key: str = ""
    gemini_image_model: str = "gemini-nano-banana-2.1"
    gemini_api_version: str = "v1"
    gemini_image_size: str = "1K"
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_workflow: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "comfyui_workflow.json")
    ffmpeg_path: str = "ffmpeg"

    # 자동화 정책 (11장)
    agent_can_approve_stories: bool = True       # 모순 검사를 통과한 스토리에 한해 에이전트 승인 허용
    agent_can_approve_content: bool = False      # 공개 게시용 콘텐츠 승인은 사람만
    agent_can_approve_character_changes: bool = False
    daily_image_budget: int = 30                 # 하루 이미지 생성 한도
    run_worker: bool = True                      # 이 프로세스에서 작업 워커 실행

    admin_token: str = ""                        # 설정 시 웹 API 쓰기 요청에 X-Admin-Token 필요
    web_host: str = "127.0.0.1"
    web_port: int = 8765


def load_settings(data_dir: str | os.PathLike | None = None) -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    base = Path(data_dir or os.environ.get("AIS_DATA_DIR") or PROJECT_ROOT / "data").resolve()
    s = Settings(
        data_dir=base,
        db_path=base / "studio.db",
        media_dir=base / "media",
        export_dir=base / "exports",
        backup_dir=base / "backups",
        outbox_dir=base / "outbox",
        llm_provider=os.environ.get("AIS_LLM_PROVIDER", "auto"),
        ollama_url=os.environ.get("AIS_OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.environ.get("AIS_OLLAMA_MODEL", "qwen2.5-coder:7b"),
        ollama_embed_model=os.environ.get("AIS_OLLAMA_EMBED_MODEL", ""),
        llm_timeout=int(os.environ.get("AIS_LLM_TIMEOUT", "180")),
        ollama_num_gpu=int(os.environ["AIS_OLLAMA_NUM_GPU"]) if os.environ.get("AIS_OLLAMA_NUM_GPU", "").strip() else None,
        image_provider=os.environ.get("AIS_IMAGE_PROVIDER", "auto"),
        comfyui_url=os.environ.get("AIS_COMFYUI_URL", "http://127.0.0.1:8188"),
        gemini_api_key=os.environ.get("AIS_GEMINI_API_KEY", "").strip(),
        gemini_image_model=os.environ.get("AIS_GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1").strip(),
        gemini_api_version=os.environ.get("AIS_GEMINI_API_VERSION", "v1").strip(),
        gemini_image_size=os.environ.get("AIS_GEMINI_IMAGE_SIZE", "1K").strip().upper(),
        ffmpeg_path=os.environ.get("AIS_FFMPEG", "ffmpeg"),
        agent_can_approve_stories=_bool("AIS_AGENT_APPROVE_STORIES", True),
        agent_can_approve_content=_bool("AIS_AGENT_APPROVE_CONTENT", False),
        agent_can_approve_character_changes=_bool("AIS_AGENT_APPROVE_CHARACTER", False),
        daily_image_budget=int(os.environ.get("AIS_DAILY_IMAGE_BUDGET", "30")),
        run_worker=_bool("AIS_RUN_WORKER", True),
        admin_token=os.environ.get("AIS_ADMIN_TOKEN", ""),
        web_host=os.environ.get("AIS_WEB_HOST", "127.0.0.1"),
        web_port=int(os.environ.get("AIS_WEB_PORT", "8765")),
    )
    wf = os.environ.get("AIS_COMFYUI_WORKFLOW")
    if wf:
        s.comfyui_workflow = Path(wf)
    for d in (s.data_dir, s.media_dir, s.export_dir, s.backup_dir, s.outbox_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
