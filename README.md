# AI Influencer Studio MCP

가상 인플루언서 생성 → 세계관·스토리 축적 → 콘텐츠 자동 제작 → 채널별 배포 → 성과 분석 → 다음 스토리 반영.
Windows 로컬 설치형 MVP 입니다 (Python 3.11+, SQLite, MCP, FastAPI 웹 대시보드).

## 빠른 시작

```bat
install.bat          :: 가상환경 생성 + 패키지 설치 + 테스트
run_web.bat          :: 관리자 대시보드 → http://127.0.0.1:8765
```

MCP 서버는 Claude Desktop / Claude Code 같은 MCP 클라이언트가 실행합니다. 설정 예시는 `config/claude_desktop_config.example.json` 에 있습니다.

```json
{
  "mcpServers": {
    "ai-influencer-studio": {
      "command": "D:\\AI인플루언서_MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "influencer_studio", "mcp"],
      "cwd": "D:\\AI인플루언서_MCP"
    }
  }
}
```

Claude Code 에서는 다음 한 줄로 등록할 수 있습니다.

```bat
claude mcp add ai-influencer-studio -- D:\AI인플루언서_MCP\.venv\Scripts\python.exe -m influencer_studio mcp
```

웹 대시보드와 MCP 서버는 같은 `data/studio.db` 를 함께 사용하므로 동시에 켜 두어도 됩니다.

## 사용 흐름

1. **인플루언서 만들기** — 대시보드 `인플루언서 관리` 또는 MCP `influencer_create` 에 자연어 설명을 넣으면 초안(DRAFT)이 만들어집니다. 설정을 확인하고 `등록 승인`(ACTIVE)을 누릅니다.
2. **캐릭터 참조 이미지** — 얼굴 일관성을 위해 참조 이미지를 등록하면 이후 모든 이미지 생성에 함께 전달됩니다 (외모 프롬프트·고정 시드도 자동 적용).
3. **스토리** — `story_generate` 는 프로필 → 관련 기억 검색 → 미해결 사건 확인 → 초안 → 모순 검사 순서로 동작합니다. 승인하면 장소·인물·메뉴·사건·기억이 아카이브에 저장됩니다.
4. **콘텐츠** — 확정된 스토리에서만 인스타그램 문구·쇼츠 대본·블로그(한국어/베트남어/영어)와 이미지를 만듭니다. 모두 같은 `story_id` 로 연결됩니다.
5. **검수함** — 콘텐츠는 운영자가 승인해야 게시할 수 있습니다 (AI 에이전트는 승인 불가).
6. **게시** — 자동 게시 API 가 연결되지 않은 플랫폼은 게시 패키지를 만들고 `수동 게시 필요`로 표시합니다. 직접 올린 뒤 `게시 완료 기록`을 누릅니다.
7. **성과** — 조회수·좋아요 등을 직접 입력하거나 CSV 로 가져오면 주제별 보고서와 다음 달 계획을 제안합니다 (캐릭터 설정은 바뀌지 않음).

30일 시리즈는 `season_plan` → 첫 7일 검수(`season_approve_chunk`) → `season_next_chunk` 순서로 진행합니다. 앞 구간 검수가 끝나지 않으면 다음 구간을 만들지 않습니다.

## 모듈 구성

| 모듈 | 위치 |
|---|---|
| ① 인플루언서 관리 (버전·승인) | `services/influencers.py` |
| ② 스토리 엔진·시즌 | `services/stories.py` |
| 연속성 검증 | `services/continuity.py` |
| ③ 아카이브·장기 기억 검색 | `services/memory.py`, `services/archive.py` |
| ④ 콘텐츠 제작 | `services/content.py` |
| ⑤ MCP 서버 (50개 도구) | `mcp_server.py` |
| ⑥ 배포 | `services/publish.py`, `adapters/publish.py` |
| ⑦ 분석 | `services/analytics.py` |
| 작업 큐·스케줄러 | `services/jobs.py`, `services/scheduler.py` |
| 자동화 정책 | `services/policy.py` |
| 제작 도구 어댑터 | `adapters/` (Ollama, 템플릿, ComfyUI, FFmpeg, 임베딩) |
| 웹 대시보드 | `web/app.py`, `web/static/index.html` |
| DB 스키마 (18개 테이블 + 검색 색인) | `db.py` |

## 제작 도구 연결 상태 (현재 PC 기준, 2026-10-28 확인)

| 도구 | 상태 | 메모 |
|---|---|---|
| 언어 모델 (Ollama) | ⚠ GPU 실행 실패 | `qwen2.5-coder:7b` 가 "CUDA error: the provided PTX was compiled with an unsupported toolchain" 로 종료됩니다. NVIDIA 드라이버(현재 560.94)를 최신으로 올리면 해결될 가능성이 높습니다. 그 전까지는 `.env` 에 `AIS_OLLAMA_NUM_GPU=0` 으로 CPU 실행이 가능하지만 호출당 약 2분 걸립니다. |
| 모델 선택 | ⚠ 권장 변경 | 설치된 모델은 코드용(coder)이라 한국어 스토리 품질이 낮습니다. `ollama pull qwen2.5:7b` 후 `AIS_OLLAMA_MODEL=qwen2.5:7b` 를 권장합니다. |
| 언어 모델이 없을 때 | ✅ | 템플릿 생성기로 자동 대체되고, 결과에 `provider=template` 이 표시됩니다. |
| 이미지 (ComfyUI) | ❌ 미실행 | ComfyUI 를 켜고 `config/comfyui_workflow.example.json` 을 참고해 `config/comfyui_workflow.json` 을 만드세요. 그 전에는 자리표시 이미지가 만들어지며 이 이미지는 승인·게시가 막힙니다. GTX 1070(8GB)은 SD1.5 계열이 현실적입니다. |
| 영상 (FFmpeg) | ❌ 미설치 | FFmpeg 설치 후 실제 이미지로 세로 슬라이드쇼 영상을 만듭니다. |
| 음성 (TTS) | ❌ 어댑터 없음 | 호출하면 `NOT_CONFIGURED` 를 그대로 반환합니다. |
| SNS 자동 게시 | ❌ API 미연동 | 모든 플랫폼이 `MANUAL_REQUIRED` 로 처리됩니다. `local_export` 만 자동(outbox 폴더). |

## 안전장치

- 확정된 스토리·기억은 덮어쓰지 않고 새 버전을 만듭니다 (이전 버전은 SUPERSEDED 로 보존).
- 캐릭터 핵심 설정 변경은 사유 + 운영자 승인 후 반영되고, 변경 시점이 사건으로 기록됩니다.
- 공동 스토리는 상대 인플루언서에게 '기억 후보'로만 남습니다 (자동 복사 없음).
- AI 에이전트(MCP)는 공개 게시용 콘텐츠 승인, 캐릭터 핵심 설정 승인, 오류 있는 스토리 강제 승인을 할 수 없습니다.
- 모든 텍스트에 AI 생성 표시가 들어가며, 없으면 승인되지 않습니다. 광고 콘텐츠(`is_ad`)는 #광고/#협찬 표시가 필요합니다.
- 작업·게시는 멱등성 키로 중복을 막고, 실패한 작업은 오류와 재시도 횟수를 기록합니다.
- 파일 가져오기는 `data` 폴더와 `AIS_IMPORT_DIRS` 안으로 제한되며, 웹 미디어 경로도 `data` 밖으로 나갈 수 없습니다.
- 웹 대시보드는 127.0.0.1 에서만 열리며, `AIS_ADMIN_TOKEN` 을 설정하면 쓰기 요청에 토큰이 필요합니다.
- 모든 주요 작업은 `audit_logs` 에 남습니다.

## 백업·복구

```bat
.venv\Scripts\python.exe -m influencer_studio backup
.venv\Scripts\python.exe -m influencer_studio restore data\backups\backup_YYYYMMDD_HHMMSS.zip D:\새폴더\data
.venv\Scripts\python.exe -m influencer_studio check
```

복구 후 `AIS_DATA_DIR` 을 새 폴더로 지정하면 됩니다. 미디어 파일 경로는 자동으로 새 위치로 바뀝니다.

## 테스트

```bat
.venv\Scripts\python.exe -m pytest -q
```

기획서 15장의 인수 테스트 1~7(독립성, 30일 연속성, 장기 기억 검색, 콘텐츠 파생, 작업 복구, 자동화·승인, 백업 복구)과 웹 API·MCP 도구 테스트가 들어 있습니다. 테스트는 외부 모델 없이(템플릿 생성기·자리표시 이미지) 실행됩니다.

## 확장 방향

- PostgreSQL + pgvector: `db.Store` 와 같은 메서드를 가진 저장소로 교체 (SQL 은 호환 범위로 작성)
- 실제 SNS 게시: `adapters/publish.py` 의 플랫폼별 어댑터 추가
- TTS·자막(Whisper): `adapters/media.py` 에 어댑터 추가
