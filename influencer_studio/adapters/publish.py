"""게시 어댑터.

실제 SNS 게시 API 는 계정 유형·권한·심사 여부에 따라 사용 가능 여부가 다릅니다.
여기서는 연동되지 않은 플랫폼을 '지원되는 것처럼' 표시하지 않고 MANUAL_REQUIRED 로 반환합니다.
- local_export: 승인된 콘텐츠를 outbox 폴더에 게시 패키지로 내보내기 (자동 실행 지원)
- 그 외 플랫폼: 게시 패키지를 만든 뒤 사람이 직접 올리도록 안내
"""

from __future__ import annotations

PLATFORMS = {
    "instagram": {"label": "Instagram", "auto_publish": False, "max_caption": 2200, "aspect": "4:5",
                  "note": "Instagram Graph API(비즈니스/크리에이터 계정 + 앱 심사)가 연결되지 않았습니다."},
    "youtube_shorts": {"label": "YouTube Shorts", "auto_publish": False, "max_caption": 5000, "aspect": "9:16",
                       "note": "YouTube Data API(OAuth) 연동이 필요합니다."},
    "tiktok": {"label": "TikTok", "auto_publish": False, "max_caption": 2200, "aspect": "9:16",
               "note": "TikTok Content Posting API 승인이 필요합니다."},
    "facebook": {"label": "Facebook", "auto_publish": False, "max_caption": 63206, "aspect": "4:5",
                 "note": "Facebook 페이지 API 연동이 필요합니다."},
    "blog": {"label": "블로그", "auto_publish": False, "max_caption": 100000, "aspect": "16:9",
             "note": "블로그 플랫폼별 API 연동이 필요합니다."},
    "x": {"label": "X", "auto_publish": False, "max_caption": 280, "aspect": "16:9",
          "note": "X API 유료 등급 연동이 필요합니다."},
    "local_export": {"label": "로컬 내보내기", "auto_publish": True, "max_caption": 100000, "aspect": "any",
                     "note": "outbox 폴더에 게시 패키지를 저장합니다."},
}

CONTENT_TYPE_PLATFORM = {
    "instagram_post": "instagram",
    "shorts_script": "youtube_shorts",
    "blog": "blog",
}


def platform_info(platform: str) -> dict:
    info = PLATFORMS.get(platform)
    if info is None:
        raise KeyError(platform)
    return {"platform": platform, **info}
