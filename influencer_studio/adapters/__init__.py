"""제작 도구 어댑터.

특정 AI 모델에 묶이지 않도록, 서비스는 아래 추상 인터페이스만 사용합니다.
- LLMAdapter: 스토리·대본·번역 (Ollama / 템플릿)
- Embedder: 의미 검색용 벡터 (내장 해시 임베딩 / Ollama 임베딩)
- ImageAdapter: 이미지 생성 (ComfyUI / 자리표시 이미지)
- VideoAdapter, AudioAdapter: 영상·음성 (FFmpeg 슬라이드쇼 / 미설정)
- PublishAdapter: 게시 (로컬 내보내기 / 수동 처리 필요)
"""
