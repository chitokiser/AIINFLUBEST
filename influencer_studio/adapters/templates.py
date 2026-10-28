"""모델 없이 동작하는 결정적 생성기 (TemplateLLM 의 실제 구현).

- 같은 입력이면 같은 결과가 나오므로 테스트와 오프라인 데모에 사용합니다.
- 품질은 실제 언어 모델보다 낮습니다. 결과에는 provider='template' 이 기록됩니다.
"""

from __future__ import annotations

import re
from datetime import date


def _has_batchim(word: str) -> bool:
    for ch in reversed(word.strip()):
        if "가" <= ch <= "힣":
            return (ord(ch) - 0xAC00) % 28 != 0
        if ch.isalnum():
            return ch.isdigit() and ch in "013678"
    return False


_JOSA = {"을(를)": ("을", "를"), "이(가)": ("이", "가"), "은(는)": ("은", "는"), "과(와)": ("과", "와"),
         "이라는": ("이라는", "라는"), "이에요": ("이에요", "예요")}


def fix_josa(text: str) -> str:
    """'김치찌개을(를)' → '김치찌개를' 처럼 받침에 맞는 조사로 바꿉니다."""
    def repl(m):
        word, josa = m.group(1), m.group(2)
        a, b = _JOSA[josa]
        return word + (a if _has_batchim(word) else b)
    return re.sub(r"([\w가-힣]+)(을\(를\)|이\(가\)|은\(는\)|과\(와\)|이라는|이에요)", repl, text)


def _fix_all(obj):
    if isinstance(obj, str):
        return fix_josa(obj)
    if isinstance(obj, list):
        return [_fix_all(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _fix_all(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# 인플루언서 프로필
# ---------------------------------------------------------------------------

FEMALE_NAMES = ["김하린", "이서아", "박지우", "최유나", "정다온", "한소율"]
MALE_NAMES = ["이민준", "박도윤", "최시우", "정하준", "강이안", "윤서진"]
INTEREST_KEYWORDS = {
    "한국 음식": ["한국 음식", "한식", "김치", "요리"],
    "음식": ["음식", "맛집", "먹방"],
    "여행": ["여행", "호텔"],
    "패션": ["패션", "옷", "스타일"],
    "뷰티": ["뷰티", "화장", "메이크업"],
    "카페": ["카페", "커피"],
    "문화 차이": ["문화", "교류"],
    "AI 기술": ["AI", "기술", "디지털", "테크"],
    "비즈니스": ["비즈니스", "창업", "사업"],
    "일상": ["일상", "라이프"],
}
CITY_HINTS = {
    "하노이": ("하노이", "미딩", "베트남"),
    "호치민": ("호치민", "1군", "베트남"),
    "서울": ("서울", "성수", "대한민국"),
    "부산": ("부산", "해운대", "대한민국"),
    "도쿄": ("도쿄", "시부야", "일본"),
    "방콕": ("방콕", "수쿰윗", "태국"),
}
LANG_KEYWORDS = {"한국어": "ko", "베트남어": "vi", "영어": "en", "일본어": "ja"}


def task_influencer_profile(payload: dict) -> dict:
    text = payload.get("description", "")
    overrides = payload.get("overrides") or {}
    seed = int(payload.get("seed", 0))

    age_m = re.search(r"(\d{2})\s*(세|살)", text)
    age = int(age_m.group(1)) if age_m else 27
    gender = "남성" if "남성" in text or "남자" in text else "여성"
    nationality = "한국" if "한국인" in text or "한국" in text else "미정"

    city, district, country = "서울", "성수", "대한민국"
    for key, (c, d, n) in CITY_HINTS.items():
        if key in text:
            city, district, country = c, d, n
            break
    d_m = re.search(r"([가-힣]{2,4})(?:에서|에) 활동", text)
    if d_m and d_m.group(1) not in CITY_HINTS:
        district = d_m.group(1) if d_m.group(1) != city else district

    interests = [k for k, kws in INTEREST_KEYWORDS.items() if any(w in text for w in kws)]
    if not interests:
        interests = ["일상", "여행"]
    languages = [code for word, code in LANG_KEYWORDS.items() if word in text] or ["ko"]
    if "ko" not in languages:
        languages.insert(0, "ko")

    friendly = any(w in text for w in ["밝", "친근", "솔직"])
    speech = "밝고 친근한 대화체 (~했어요, ~해요!)" if friendly or "전문" not in text else "차분하고 전문적인 설명체"
    names = FEMALE_NAMES if gender == "여성" else MALE_NAMES
    name = overrides.get("name") or names[seed % len(names)]

    occupation = "콘텐츠 크리에이터"
    if "K-컬처" in text or "한국 문화" in text:
        occupation = "K-컬처 콘텐츠 크리에이터"
    elif "AI" in text or "기술" in text:
        occupation = "테크 콘텐츠 크리에이터"
    elif "창업" in text or "비즈니스" in text:
        occupation = "스타트업 창업가 겸 크리에이터"

    goal = "자신의 관심 분야로 사람들을 연결하는 글로벌 인플루언서"
    g_m = re.search(r"장기적으로\s*(.+?)(?:를|을)?\s*(?:다루는|만들어|하는)", text)
    if g_m:
        goal = g_m.group(1).strip() + "를 다루는 인플루언서"
    elif country != "대한민국" and nationality == "한국":
        goal = f"한국과 {country}의 문화를 연결하는 글로벌 인플루언서"

    profile = {
        "name": name,
        "handle": overrides.get("handle") or f"{_romanize(name)}.{city_en(city)}",
        "age": age,
        "gender": gender,
        "nationality": nationality,
        "occupation": occupation,
        "location": {"city": city, "district": district, "country": country},
        "personality": ["솔직함", "밝음", "새로운 경험을 좋아함"] if friendly else ["차분함", "꼼꼼함", "호기심 많음"],
        "values": ["진정성", "문화 존중", "작은 행복"],
        "appearance": {
            "face": "둥근 눈매, 자연스러운 미소, 밝은 피부톤" if gender == "여성" else "또렷한 눈매, 짧은 턱수염 없음",
            "hair": "어깨 길이의 짙은 갈색 웨이브 머리" if gender == "여성" else "짧은 검은 머리",
            "body": "보통 체형, 키 약 163cm" if gender == "여성" else "보통 체형, 키 약 178cm",
            "style": "캐주얼한 데일리룩, 밝은 파스텔 톤",
            "signature_items": ["작은 은색 귀걸이", "베이지 크로스백"] if gender == "여성" else ["검은 뿔테 안경"],
        },
        "voice": {"tone": "맑고 밝은 중음", "speed": "보통보다 약간 빠름", "accent": "표준 한국어"},
        "languages": languages,
        "speech_style": speech,
        "interests": interests,
        "background": f"{nationality} 출신으로 {city} {district}에서 생활하며 콘텐츠를 만든다.",
        "long_term_goal": goal,
        "content_policy": {
            "allowed_topics": interests,
            "banned_topics": ["정치", "종교 비하", "도박", "의료 효능 주장", "실존 인물 사칭"],
            "ad_disclosure": "#광고 #협찬 표시를 본문 첫 줄에 넣는다",
            "ai_disclosure": "이 계정은 AI로 제작된 가상 인물입니다.",
        },
        "production": {"image_style": "자연광의 현실적인 라이프스타일 사진", "aspect_ratios": ["4:5", "9:16"],
                       "resolution": "1080x1350"},
        "initial_story_ideas": [
            f"{city}에 처음 도착한 날",
            f"{district}에서 {interests[0]} 관련 장소를 발견한 이야기",
            "현지 친구를 처음 사귄 이야기",
        ],
    }
    profile.update({k: v for k, v in overrides.items() if k not in ("name", "handle")})
    return profile


def _romanize(name: str) -> str:
    table = {"김": "kim", "이": "lee", "박": "park", "최": "choi", "정": "jung", "한": "han", "강": "kang", "윤": "yoon"}
    given = {"하린": "harin", "서아": "seoa", "지우": "jiu", "유나": "yuna", "다온": "daon", "소율": "soyul",
             "민준": "minjun", "도윤": "doyun", "시우": "siu", "하준": "hajun", "이안": "ian", "서진": "seojin"}
    if len(name) >= 3:
        return given.get(name[1:], "creator") + "_" + table.get(name[0], "k")
    return "creator"


def city_en(city: str) -> str:
    return {"하노이": "hanoi", "호치민": "hcmc", "서울": "seoul", "부산": "busan", "도쿄": "tokyo", "방콕": "bangkok"}.get(city, "life")


# ---------------------------------------------------------------------------
# 스토리
# ---------------------------------------------------------------------------

LOCAL_FRIENDS = {
    "베트남": ["린", "짱", "민", "호아", "남", "타오", "꾸안", "마이"],
    "대한민국": ["수빈", "현우", "예린", "도현", "나은", "준서"],
    "일본": ["유키", "하루토", "사쿠라", "렌"],
    "태국": ["프랑", "논", "민트", "빔"],
}

SCENARIOS = [
    {
        "interest": ["한국 음식", "음식"],
        "place_type": "한식당", "place_names": ["서울집", "한강식당", "엄마손", "김치하우스", "명동분식"],
        "items": ["김치찌개", "제육볶음", "순두부찌개", "떡볶이", "김밥", "비빔밥", "닭갈비", "잡채", "부대찌개", "해물파전"],
        "beat": "{area}에서 {place}이라는 한식당을 발견했다",
    },
    {
        "interest": ["음식", "여행"],
        "place_type": "현지 시장", "place_names": ["동쑤언 시장", "롱비엔 시장", "꽝안 시장", "하동 시장"],
        "items": ["분짜", "반쎄오", "넴잔", "짜까", "반미", "쏘이 쎄오", "껌 땀", "분보남보"],
        "beat": "{place}에서 처음 보는 현지 음식을 맛봤다",
    },
    {
        "interest": ["카페", "일상"],
        "place_type": "카페", "place_names": ["코코넛 하우스", "느린 오후", "그린 테라스", "에그 커피 공방"],
        "items": ["에그 커피", "코코넛 커피", "소금 커피", "연유 커피", "망고 스무디", "반 플란"],
        "beat": "{place} 카페에서 하루를 정리했다",
    },
    {
        "interest": ["패션", "뷰티"],
        "place_type": "편집숍", "place_names": ["아오자이 아틀리에", "실크 스트리트", "모던 하노이", "빈티지 상점 13"],
        "items": ["실크 스카프", "린넨 셔츠", "아오자이", "라탄 가방", "대나무 귀걸이"],
        "beat": "{place}에서 현지 스타일 아이템을 골랐다",
    },
    {
        "interest": ["문화 차이", "여행", "일상"],
        "place_type": "명소", "place_names": ["호안끼엠 호수", "문묘", "기찻길 마을", "서호"],
        "items": ["아침 산책", "전통 공연", "야시장 구경", "사진 촬영"],
        "beat": "{place}에서 한국과 다른 문화를 느꼈다",
    },
    {
        "interest": ["AI 기술", "비즈니스"],
        "place_type": "코워킹 스페이스", "place_names": ["업타운 허브", "블록 랩", "넥스트 데스크"],
        "items": ["AI 번역 앱 테스트", "스타트업 밋업", "노트북 세팅", "온라인 강의"],
        "beat": "{place}에서 새로운 프로젝트를 구상했다",
    },
]

MAJOR_BEATS = [
    "{friend}과(와) 함께 {item}을(를) 직접 만들어 보기로 약속했다",
    "{friend}의 소개로 새로운 사람들을 만나 작은 모임을 열기로 했다",
    "콘텐츠를 본 팔로워에게서 특별한 메시지를 받았다",
    "한국과 현지 음식의 공통점을 소개하는 프로젝트를 시작하기로 했다",
]


def _pick_scenario(profile: dict, topic: str, seed: int) -> dict:
    if topic:
        for sc in SCENARIOS:
            if any(k in topic for k in [sc["place_type"], *sc["items"], *sc["interest"]]):
                return sc
    interests = profile.get("interests") or []
    matched = [sc for sc in SCENARIOS if any(i in sc["interest"] for i in interests)] or SCENARIOS
    return matched[seed % len(matched)]


def task_story_draft(payload: dict) -> dict:
    profile = payload.get("profile", {})
    seed = int(payload.get("seed", 0))
    topic = payload.get("topic") or ""
    beat_hint = payload.get("beat") or ""
    is_major = bool(payload.get("is_major"))
    story_date = payload.get("story_date") or date.today().isoformat()
    loc = profile.get("location") or {}
    area = loc.get("district") or loc.get("city") or "동네"
    country = loc.get("country", "베트남")
    known_people = payload.get("known_people") or []
    known_places = payload.get("known_places") or []
    open_threads = payload.get("open_threads") or []
    exclude_items = set(payload.get("exclude_items") or [])

    sc = _pick_scenario(profile, topic + " " + beat_hint, seed)

    # 장소: 같은 종류의 기존 장소를 다시 방문하거나 새 장소를 만든다.
    same_type = [p for p in known_places if p.get("type") == sc["place_type"]]
    revisit = bool(same_type) and (seed % 3 == 1 or "다시" in topic or "방문했던" in topic)
    if revisit:
        place = same_type[seed % len(same_type)]
        place_name = place["name"]
        exclude_items |= set(place.get("items") or [])
    else:
        used = {p["name"] for p in known_places}
        candidates = [n for n in sc["place_names"] if n not in used] or sc["place_names"]
        place_name = candidates[seed % len(candidates)]
    items = [i for i in sc["items"] if i not in exclude_items] or sc["items"]
    item = items[seed % len(items)]

    # 인물: 기존 친구 재등장 또는 새 친구
    friends = [p for p in known_people if p.get("relation") in ("친구", "동료", "이웃")]
    pool = LOCAL_FRIENDS.get(country, LOCAL_FRIENDS["베트남"])
    if friends and seed % 3 != 2:
        friend = friends[seed % len(friends)]
        friend_name, relation, new_friend = friend["name"], friend.get("relation", "친구"), False
    else:
        known_names = {p["name"] for p in known_people}
        fresh = [n for n in pool if n not in known_names] or pool
        friend_name, relation, new_friend = fresh[seed % len(fresh)], "친구", True

    beat = (beat_hint or sc["beat"]).format(area=area, place=place_name, friend=friend_name, item=item)
    events = []
    resolved = None
    if open_threads and (seed % 3 != 1 or "약속" in topic):
        resolved = open_threads[0]
        events.append({
            "title": f"약속을 지킴: {resolved['title']}",
            "description": f"예전에 했던 약속({resolved['title']})을 오늘 실천했다.",
            "event_type": "MILESTONE", "status": "RESOLVED", "resolves_event_id": resolved["event_id"],
        })
    if is_major:
        major = MAJOR_BEATS[seed % len(MAJOR_BEATS)].format(friend=friend_name, item=item)
        events.append({"title": major, "description": major, "event_type": "PROMISE", "status": "OPEN"})
    events.append({
        "title": f"{place_name}에서 {item}",
        "description": f"{place_name}에서 {friend_name}과(와) {item}을(를) 경험했다.",
        "event_type": "DISCOVERY" if not revisit else "MILESTONE", "status": "NONE",
    })

    name = profile.get("name", "나")
    meet = f"처음 만난 {friend_name}" if new_friend else f"{relation} {friend_name}"
    lines = [
        f"오늘은 {area}에서 특별한 하루를 보냈어요!",
        (f"전에 왔던 {place_name}에 다시 왔는데, 이번에는 {item}을(를) 골라 봤어요." if revisit
         else f"{place_name}이라는 {sc['place_type']}을(를) 처음 알게 됐는데, {item}이(가) 정말 인상적이었어요."),
        f"{meet}과(와) 함께해서 더 즐거웠어요.",
    ]
    if resolved:
        lines.append(f"그리고 드디어 '{resolved['title']}' 약속을 지켰어요. 마음이 뿌듯해요.")
    if is_major:
        lines.append(f"그리고 중요한 결정을 했어요. {events[-2]['title']}!")
    lines.append(f"한국에서 먹던 것과 비교해 보니 비슷하면서도 다른 점이 많아서 재미있었어요.")
    lines.append("다음 이야기도 기대해 주세요!")

    title = (f"{place_name} 다시 방문 - {item}" if revisit else f"{area}의 {place_name}에서 만난 {item}")
    return _fix_all({
        "title": title,
        "summary": f"{name}이(가) {beat}. {friend_name}과(와) 함께 {item}을(를) 경험했다.",
        "story_date": story_date,
        "body": " ".join(lines),
        "characters": [{"name": friend_name, "role": "현지 친구", "relation": relation}],
        "places": [{"name": place_name, "type": sc["place_type"], "area": area}],
        "items": [{"name": item, "place": place_name}],
        "events": events,
        "memories": [
            f"{story_date}에 {place_name}({area})에서 {item}을(를) 경험했다.",
            f"{friend_name}은(는) {name}의 {relation}이다.",
        ],
        "tags": [sc["place_type"], item, area, *(sc["interest"][:1])],
        "next_hooks": [f"{friend_name}에게 한국 음식 {('김치찌개' if item != '김치찌개' else '떡볶이')}을(를) 소개하기"],
        "profile_assertions": {},
    })


def task_season_plan(payload: dict) -> dict:
    total = int(payload.get("total_days", 30))
    theme = payload.get("theme") or "새로운 도시에서의 생활"
    profile = payload.get("profile", {})
    loc = (profile.get("location") or {}).get("city", "")
    beats = ["새로운 장소 발견", "현지 음식 체험", "친구와의 하루", "문화 차이 발견", "작은 실수와 배움", "카페에서의 휴식"]
    days = []
    for d in range(1, total + 1):
        major = d % 7 == 0
        if d == 1:
            beat = f"{loc}에 처음 도착한 날"
        elif d == total:
            beat = f"{theme}의 마지막 날: 지금까지의 이야기를 정리하고 새로운 목표를 말한다"
        elif major:
            beat = "중요한 사건: 친구와 함께 새로운 프로젝트를 약속한다"
        else:
            beat = beats[d % len(beats)]
        days.append({"day_no": d, "theme": theme, "beat": beat, "is_major_event": major or d == total})
    return {"title": f"{loc} {theme} {total}일", "goal": profile.get("long_term_goal", ""),
            "arc_summary": f"{total}일 동안 {theme}을(를) 경험하고 마지막 날 하나의 이야기를 완결한다.", "days": days}


# ---------------------------------------------------------------------------
# 콘텐츠 텍스트
# ---------------------------------------------------------------------------

PHRASES = {
    "ko": {
        "intro": "오늘은 {place}에서 {friend}과(와) 함께했어요!",
        "item": "{item}, 꼭 한번 드셔 보세요.",
        "outro": "여러분의 추천 장소도 댓글로 알려주세요 💬",
        "blog_title": "{place} 방문기: {item} 이야기",
        "scene": ["{place} 입구", "{item} 클로즈업", "{friend}과(와) 건배하는 장면", "카메라를 보며 마무리 인사"],
        "narr": ["오늘은 {area}의 {place}에 왔어요!", "이게 바로 {item}이에요.", "{friend}과(와) 함께라서 더 맛있어요.", "다음 영상도 기대해 주세요!"],
    },
    "vi": {
        "intro": "Hôm nay mình đã ghé {place} cùng {friend}!",
        "item": "Món {item} thật sự rất đáng thử.",
        "outro": "Các bạn có địa điểm nào muốn giới thiệu không? Bình luận cho mình nhé 💬",
        "blog_title": "Ghé thăm {place}: câu chuyện về {item}",
        "scene": ["Lối vào {place}", "Cận cảnh {item}", "Cụng ly cùng {friend}", "Lời chào kết thúc"],
        "narr": ["Hôm nay mình đến {place} ở {area}!", "Đây chính là {item}.", "Đi cùng {friend} nên càng vui hơn.", "Hẹn gặp lại ở video sau nhé!"],
    },
    "en": {
        "intro": "Spent today at {place} with {friend}!",
        "item": "You have to try the {item}.",
        "outro": "Got a favorite spot? Tell me in the comments 💬",
        "blog_title": "Visiting {place}: all about {item}",
        "scene": ["Entrance of {place}", "Close-up of {item}", "Cheers with {friend}", "Closing greeting to camera"],
        "narr": ["Today I'm at {place} in {area}!", "This is {item}.", "Even better with {friend}.", "See you in the next one!"],
    },
}
DISCLOSURE = {
    "ko": "※ AI로 제작된 가상 인물의 콘텐츠입니다.",
    "vi": "※ Nội dung của nhân vật ảo được tạo bằng AI.",
    "en": "※ This content features a virtual, AI-generated character.",
}


def _story_slots(story: dict) -> dict:
    places = story.get("places") or [{}]
    items = story.get("items") or [{}]
    chars = story.get("characters") or [{}]
    return {
        "place": places[0].get("name", ""), "area": places[0].get("area", ""),
        "item": items[0].get("name", ""), "friend": chars[0].get("name", ""),
    }


def task_content_text(payload: dict) -> dict:
    out = _content_text(payload)
    return _fix_all(out) if payload.get("language", "ko") == "ko" else out


def _content_text(payload: dict) -> dict:
    fmt = payload.get("format")
    lang = payload.get("language", "ko")
    story = payload.get("story", {})
    variant = int(payload.get("variant", 0))
    p = PHRASES.get(lang)
    if p is None:
        return {"_needs_human_translation": True, "language": lang}
    s = _story_slots(story)
    disclosure = payload.get("disclosure") or DISCLOSURE.get(lang, "")
    tags = ["#" + t.replace(" ", "") for t in (story.get("tags") or [])][:6]
    if fmt == "instagram_post":
        body = [p["intro"].format(**s), p["item"].format(**s)]
        if lang == "ko" and variant == 0:
            body.insert(1, story.get("summary", ""))
        elif variant == 1:
            body.reverse()
        body.append(p["outro"])
        return {"caption": "\n".join(x for x in body if x) + "\n\n" + disclosure,
                "hashtags": tags + ["#AIinfluencer"],
                "alt_texts": [sc.format(**s) for sc in p["scene"][:3]]}
    if fmt == "shorts_script":
        dur = int(payload.get("duration_sec", 30))
        step = max(dur // 4, 1)
        scenes = []
        for i, (vis, nar) in enumerate(zip(p["scene"], p["narr"])):
            scenes.append({"t": f"{i * step}-{min((i + 1) * step, dur)}s", "visual": vis.format(**s),
                           "narration": nar.format(**s), "subtitle": nar.format(**s)})
        return {"title": p["blog_title"].format(**s), "duration_sec": dur, "scenes": scenes, "disclosure": disclosure}
    if fmt == "blog":
        if lang == "ko":
            body = f"# {p['blog_title'].format(**s)}\n\n{story.get('body', '')}\n\n## 오늘의 포인트\n- 장소: {s['place']} ({s['area']})\n- 메뉴/경험: {s['item']}\n- 함께한 사람: {s['friend']}\n\n{disclosure}\n"
        else:
            body = (f"# {p['blog_title'].format(**s)}\n\n{p['intro'].format(**s)} {p['item'].format(**s)}\n\n"
                    f"- {s['place']} ({s['area']})\n- {s['item']}\n- {s['friend']}\n\n{p['outro']}\n\n{disclosure}\n")
        return {"title": p["blog_title"].format(**s), "body_markdown": body, "tags": story.get("tags") or []}
    if fmt == "image_prompts":
        n = int(payload.get("count", 3))
        scenes = PHRASES["en"]["scene"]
        return {"prompts": [{"scene": scenes[i % len(scenes)].format(**s),
                             "prompt": scenes[i % len(scenes)].format(**s)} for i in range(n)]}
    raise ValueError(f"지원하지 않는 형식: {fmt}")


def task_translate(payload: dict) -> dict:
    # 템플릿 생성기는 자유 번역을 하지 못하므로, 서비스가 스토리에서 대상 언어로 다시 생성하도록 알린다.
    return {"_needs_human_translation": True}


def task_strategy(payload: dict) -> dict:
    stats = payload.get("topic_stats") or []
    ranked = sorted(stats, key=lambda x: x.get("score", 0), reverse=True)
    keep = [s["topic"] for s in ranked[:3]]
    reduce = [s["topic"] for s in ranked[-2:] if s.get("score", 0) < (ranked[0].get("score", 0) * 0.3 if ranked else 0)]
    interests = (payload.get("profile") or {}).get("interests") or []
    tried = {s["topic"] for s in stats}
    try_new = [i for i in interests if i not in tried][:2]
    plan = []
    for w in range(1, 5):
        theme = keep[(w - 1) % len(keep)] if keep else (interests[0] if interests else "일상")
        plan.append({"week": w, "theme": theme, "idea": f"{theme} 중심의 연속 에피소드 + 친구 재등장"})
    return {"summary": "반응이 좋은 주제를 유지하되 캐릭터의 기본 성격과 세계관은 그대로 둡니다.",
            "keep": keep, "try": try_new, "reduce": reduce, "plan": plan}
