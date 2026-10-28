"""기획서 15장 최종 인수 테스트 1~7."""

from datetime import date, datetime
from pathlib import Path

import pytest

from conftest import HARIN, approved_story, new_active
from influencer_studio.adapters.image import ImageError, PlaceholderImageAdapter
from influencer_studio.config import load_settings
from influencer_studio.services.archive import ArchiveService
from influencer_studio.studio import Studio
from influencer_studio.util import StudioError


# ---------------------------------------------------------------- 테스트 1. 인플루언서 독립성
def test_influencers_are_independent(studio):
    harin = new_active(studio)
    minjun = new_active(studio, "서울 성수에서 활동하는 31세 한국인 남성. AI 기술과 디지털 트렌드를 차분하고 전문적인 설명체로 다룬다.",
                        name="이민준")
    sophia = new_active(studio, "방콕에서 활동하는 29세 한국인 여성. 여행, 호텔, 맛집을 밝고 친근하게 소개한다.", name="한소율")
    profiles = {i: studio.influencers.get(i)["influencer"]["profile_json"] for i in (harin, minjun, sophia)}
    assert len({p["name"] for p in profiles.values()}) == 3
    assert profiles[minjun]["location"]["city"] == "서울"
    assert profiles[sophia]["location"]["city"] == "방콕"
    assert profiles[harin]["speech_style"] != profiles[minjun]["speech_style"]

    for iid in (harin, minjun, sophia):
        for _ in range(3):
            approved_story(studio, iid)

    harin_places = {p["name"] for p in studio.memory.known_places(harin)}
    minjun_places = {p["name"] for p in studio.memory.known_places(minjun)}
    # 하린의 장소로 민준의 기억을 검색해도 하린의 기록이 나오면 안 된다
    for place in harin_places - minjun_places:
        res = studio.memory.search(minjun, place)["results"]
        assert all(place not in (r.get("text") or "") and place not in (r.get("title") or "") for r in res)
    owners = studio.db.query("SELECT DISTINCT m.influencer_id, s.influencer_id AS story_owner FROM memories m "
                             "JOIN stories s ON s.story_id = m.source_story_id")
    assert all(o["influencer_id"] == o["story_owner"] for o in owners)


def test_joint_story_does_not_copy_memory(studio):
    harin = new_active(studio)
    jiu = new_active(studio, "하노이에서 활동하는 26세 한국인 여성. 패션과 뷰티, 일상을 다룬다.", name="박지우")
    with pytest.raises(StudioError) as e:
        studio.stories.generate(harin, co_influencers=[jiu])
    assert e.value.code == "NO_RELATIONSHIP"
    studio.influencers.relate(harin, jiu, "친구")
    sid = approved_story(studio, harin, topic="지우와 함께 카페", co_influencers=[jiu])
    jiu_mems = studio.db.query("SELECT status, content FROM memories WHERE influencer_id = ?", (jiu,))
    assert jiu_mems and all(m["status"] == "CANDIDATE" for m in jiu_mems), "상대 인플루언서에게는 후보로만 남아야 함"
    title = studio.stories.get(sid)["story"]["title"]
    assert not [r for r in studio.memory.search(jiu, title)["results"] if r["type"] == "MEMORY"]
    # 상대방이 승인하면 그때 상대의 기억이 된다
    apr = studio.approvals.list("PENDING", "MEMORY", jiu)["approvals"][0]
    studio.approvals.decide(apr["approval_id"], "APPROVED", "admin")
    assert any(r["type"] == "MEMORY" for r in studio.memory.search(jiu, "공동 경험")["results"])


# ---------------------------------------------------------------- 테스트 2. 스토리 연속성
def test_thirty_episode_season_is_consistent(studio):
    iid = new_active(studio)
    res = studio.stories.plan_season(iid, total_days=30, theme="하노이 생활", start_date="2026-11-01")
    sid = res["season_id"]
    assert len(res["first_chunk"]["stories"]) == 7
    with pytest.raises(StudioError) as e:
        studio.stories.generate_season_chunk(sid)  # 첫 구간 검수 전에는 다음 구간을 만들 수 없다
    assert e.value.code == "PREVIOUS_CHUNK_NOT_APPROVED"
    while True:
        ok = studio.stories.approve_season_chunk(sid)
        assert ok["ok"], ok
        nxt = studio.stories.generate_season_chunk(sid)
        if not nxt["stories"]:
            break
    tl = studio.stories.timeline(iid)["timeline"]
    assert len(tl) == 30
    dates = [s["story_date"] for s in tl]
    assert dates == sorted(dates) and len(set(dates)) == 30
    assert [s["day_no"] for s in tl] == list(range(1, 31))
    majors = [s for s in tl if any(e["event_type"] == "PROMISE" for e in s["events"])]
    assert {7, 14, 21, 28}.issubset({s["day_no"] for s in majors})
    # 약속(OPEN) 사건 중 일부는 이후 에피소드에서 해결된다
    assert studio.db.one("SELECT COUNT(*) AS n FROM story_events WHERE influencer_id = ? AND status = 'RESOLVED'",
                         (iid,))["n"] >= 1
    # 재등장 인물: 같은 이름이 여러 엔티티로 쪼개지지 않는다
    people = studio.memory.known_people(iid)
    names = [p["name"] for p in people]
    assert len(names) == len(set(names))
    assert max(p["appearances"] for p in people) >= 3, "같은 친구가 여러 에피소드에 다시 등장해야 함"
    assert len(people) < 30


def test_contradiction_never_overwrites(studio):
    iid = new_active(studio)
    first = approved_story(studio, iid, story_date="2026-10-10")
    before = studio.stories.get(first)["story"]
    # 과거 날짜로 새 스토리를 만들면 시간 충돌
    bad = studio.stories.generate(iid, story_date="2026-10-01")
    codes = {i["code"] for i in bad["validation"]["issues"]}
    assert "TIMELINE_CONFLICT" in codes
    with pytest.raises(StudioError) as e:
        studio.stories.approve(bad["story_id"])
    assert e.value.code == "VALIDATION_FAILED"
    # 에이전트는 강제 승인 불가
    with pytest.raises(StudioError):
        studio.stories.approve(bad["story_id"], actor="agent", force_reason="테스트")
    # 나이 모순
    draft = studio.stories.generate(iid)
    v = studio.stories.revise(draft["story_id"], {"body": "저는 31살이에요. 오늘은 카페에 갔어요."})["validation"]
    assert any(i["code"] == "PROFILE_CONFLICT" for i in v["issues"])
    # 확정된 기록은 그대로
    after = studio.stories.get(first)["story"]
    assert after["data_json"] == before["data_json"] and after["status"] == "APPROVED"


def test_revision_supersedes_without_deleting(studio):
    iid = new_active(studio)
    sid = approved_story(studio, iid)
    old_mems = studio.db.query("SELECT memory_id FROM memories WHERE source_story_id = ?", (sid,))
    rev = studio.stories.revise(sid, {"body": "수정된 본문입니다. 오늘은 친구와 시장에 갔어요."}, "본문 다듬기")
    assert rev["story_id"] != sid
    assert studio.stories.get(sid)["story"]["status"] == "APPROVED"  # 승인 전에는 원본 유지
    studio.stories.approve(rev["story_id"])
    assert studio.stories.get(sid)["story"]["status"] == "SUPERSEDED"
    statuses = {m["status"] for m in studio.db.query("SELECT status FROM memories WHERE memory_id IN (%s)" %
                                                      ",".join("?" * len(old_mems)), [m["memory_id"] for m in old_mems])}
    assert statuses == {"SUPERSEDED"}  # 삭제되지 않고 보존


def test_relationship_change_is_recorded_as_event(studio):
    iid = new_active(studio)
    sid = approved_story(studio, iid)
    friend = studio.stories.get(sid)["story"]["data_json"]["characters"][0]["name"]
    d = studio.stories.generate(iid)
    v = studio.stories.revise(d["story_id"], {"characters": [{"name": friend, "relation": "사업 파트너"}]})["validation"]
    assert any(i["code"] == "RELATIONSHIP_MISMATCH" for i in v["issues"])
    studio.stories.approve(d["story_id"])
    changes = studio.db.query("SELECT title FROM story_events WHERE influencer_id = ? AND event_type = 'CHANGE'", (iid,))
    assert any("사업 파트너" in c["title"] for c in changes)


def test_core_profile_change_needs_review_and_keeps_history(studio):
    iid = new_active(studio)
    with pytest.raises(StudioError):
        studio.influencers.update(iid, {"occupation": "요리 강사"})  # 사유 없음
    res = studio.influencers.update(iid, {"occupation": "요리 강사"}, reason="새 시즌 설정")
    assert res["status"] == "PROPOSED"
    assert studio.influencers.get(iid)["influencer"]["profile_json"]["occupation"] != "요리 강사"
    with pytest.raises(StudioError):  # 에이전트는 핵심 설정 변경을 승인할 수 없다
        studio.approvals.decide(res["approval_id"], "APPROVED", "agent")
    studio.approvals.decide(res["approval_id"], "APPROVED", "admin")
    inf = studio.influencers.get(iid)
    assert inf["influencer"]["profile_json"]["occupation"] == "요리 강사"
    versions = studio.db.query("SELECT status FROM character_versions WHERE influencer_id = ? ORDER BY version_no", (iid,))
    assert [v["status"] for v in versions] == ["SUPERSEDED", "ACTIVE"]
    tl = studio.stories.timeline(iid)
    assert any("occupation" in e["title"] for e in tl["other_events"])
    # 외모가 아닌 제작 설정은 바로 반영
    assert studio.influencers.update(iid, {"production": {"image_style": "필름 카메라 느낌"}})["status"] == "APPLIED"


# ---------------------------------------------------------------- 테스트 3. 장기 기억 검색
def test_long_term_memory_recall(studio):
    iid = new_active(studio)
    target = studio.memory.create(iid, "하린은 서호 근처 노점에서 연꽃차를 처음 마시고 할머니 생각이 났다.", "EVENT",
                                  status="ACTIVE", valid_from="2026-09-01")["memory_id"]
    for _ in range(12):
        approved_story(studio, iid)
    res = studio.memory.search(iid, "연꽃차")["results"]
    assert res[0]["id"] == target
    res = studio.memory.search(iid, "처음 마셔 본 차와 할머니")["results"]
    assert target in [r["id"] for r in res[:3]]
    # 기간 검색
    res = studio.memory.search(iid, "연꽃차", date_from="2026-10-01")["results"]
    assert target not in [r["id"] for r in res]


def test_unintroduced_menu_at_known_restaurant(studio):
    iid = new_active(studio)
    first = approved_story(studio, iid, topic="한식당")
    d = studio.stories.get(first)["story"]["data_json"]
    place, item = d["places"][0]["name"], d["items"][0]["name"]
    found = studio.memory.search(iid, f"미딩에서 방문했던 식당 {place}")["results"]
    assert any(place in (r.get("places") or []) for r in found)
    nxt = studio.stories.generate(iid, topic=f"{place} 다시 방문, 이전에 소개하지 않은 메뉴")
    new_items = [i["name"] for i in nxt["story"]["items"]]
    assert item not in new_items
    assert not any(i["code"] == "REPEATED_ITEM" for i in nxt["validation"]["issues"])


# ---------------------------------------------------------------- 테스트 4. 콘텐츠 파생
def test_all_content_links_to_one_story(studio):
    iid = new_active(studio)
    draft = studio.stories.generate(iid)
    with pytest.raises(StudioError) as e:
        studio.content.generate_text(draft["story_id"])
    assert e.value.code == "STORY_NOT_APPROVED"
    studio.stories.approve(draft["story_id"])
    sid = draft["story_id"]
    texts = studio.content.generate_text(sid, ["instagram_post", "shorts_script", "blog"], ["ko", "vi"], variants=2)
    assert len(texts["contents"]) == 2 * 2 + 2 + 2  # 인스타 2종×2언어 + 쇼츠 2 + 블로그 2
    img = studio.content.generate_image(sid, 3)
    studio.jobs.run_pending()
    tr = studio.content.translate(texts["contents"][0]["content_id"], "en")
    rows = studio.db.query("SELECT story_id FROM contents WHERE influencer_id = ?", (iid,))
    assert {r["story_id"] for r in rows} == {sid}
    assets = studio.db.query("SELECT story_id, is_placeholder FROM assets WHERE content_id = ?", (img["content_id"],))
    assert len(assets) == 3 and {a["story_id"] for a in assets} == {sid}
    data = studio.content.current_data(texts["contents"][0]["content_id"])
    assert "AI로 제작된 가상 인물" in data["caption"]
    vi = [c for c in texts["contents"] if c["language"] == "vi" and c["content_type"] == "blog"][0]
    assert "Nội dung" in studio.content.current_data(vi["content_id"])["body_markdown"]
    assert studio.content.get(tr["content_id"])["content"]["translated_from"] == texts["contents"][0]["content_id"]
    # 자리표시 이미지는 승인(게시) 불가
    with pytest.raises(StudioError):
        studio.content.decide(img["content_id"], "APPROVED", "admin")
    pkg = studio.content.package(sid)
    assert Path(pkg["path"], "manifest.json").exists()


# ---------------------------------------------------------------- 테스트 5. 작업 복구
class FlakyImage(PlaceholderImageAdapter):
    name = "flaky"

    def __init__(self):
        self.calls = 0

    def generate(self, *a, **kw):
        self.calls += 1
        if self.calls == 2:
            raise ImageError("GPU 메모리 부족 (테스트용 오류)")
        return super().generate(*a, **kw)


def test_failed_job_retry_without_duplicates(make_studio):
    flaky = FlakyImage()
    st = make_studio(image=flaky)
    iid = new_active(st)
    sid = approved_story(st, iid)
    job = st.content.generate_image(sid, 3, idempotency_key="img-1")["job"]
    st.jobs.run_pending()
    failed = st.jobs.get(job["job_id"])["job"]
    assert failed["status"] == "FAILED" and "GPU" in failed["error"]
    assert st.db.one("SELECT COUNT(*) AS n FROM assets WHERE job_id = ?", (job["job_id"],))["n"] == 1
    again = st.content.generate_image(sid, 3, idempotency_key="img-1")["job"]
    assert again["job_id"] == job["job_id"] and again["deduplicated"]
    st.jobs.retry(job["job_id"])
    st.jobs.run_pending()
    done = st.jobs.get(job["job_id"])["job"]
    assert done["status"] == "REVIEW_REQUIRED" and done["retry_count"] == 1
    assert st.db.one("SELECT COUNT(*) AS n FROM assets WHERE job_id = ?", (job["job_id"],))["n"] == 3
    assert flaky.calls == 4  # 1, 2(실패), 재시도 2, 3 — 1번은 다시 만들지 않음


def test_unconfigured_tools_report_honestly(studio):
    iid = new_active(studio)
    sid = approved_story(studio, iid)
    v = studio.content.generate_video(sid)["job"]
    a = studio.content.generate_audio(sid)["job"]
    studio.jobs.run_pending()
    for j in (v, a):
        got = studio.jobs.get(j["job_id"])["job"]
        assert got["status"] == "FAILED" and got["error"].startswith("NOT_CONFIGURED")
        with pytest.raises(StudioError):
            studio.jobs.retry(j["job_id"])


def test_cancel_job(studio):
    iid = new_active(studio)
    sid = approved_story(studio, iid)
    job = studio.content.generate_image(sid, 2)["job"]
    studio.jobs.cancel(job["job_id"])
    studio.jobs.run_pending()
    assert studio.jobs.get(job["job_id"])["job"]["status"] == "CANCELLED"


# ---------------------------------------------------------------- 테스트 6. 자동화 및 승인
def test_schedule_creates_drafts_but_never_publishes(studio):
    iid = new_active(studio)
    studio.scheduler.set(iid, "00:00", {"formats": ["instagram_post"], "images": 2})
    out = studio.scheduler.run_due(datetime.now())
    assert out and out[0]["story_status"] == "REVIEW_REQUIRED"
    assert studio.scheduler.run_due(datetime.now()) == []  # 같은 날 두 번 실행하지 않음
    assert studio.db.one("SELECT COUNT(*) AS n FROM publications")["n"] == 0
    # 자동 승인 정책이면 텍스트·이미지 초안까지 만들지만 여전히 게시는 없음
    studio.influencers.update(iid, {"review_policy": {"story": "auto_if_clean"}})
    studio.db.execute("UPDATE schedules SET last_run_date = NULL")
    out = studio.scheduler.run_due(datetime.now())
    assert out[0]["story_status"] in ("APPROVED", "REVIEW_REQUIRED")
    assert studio.db.one("SELECT COUNT(*) AS n FROM publications")["n"] == 0
    statuses = {r["status"] for r in studio.db.query("SELECT status FROM contents")}
    assert "PUBLISHED" not in statuses and "APPROVED" not in statuses


def test_publish_only_after_human_approval(studio):
    iid = new_active(studio)
    sid = approved_story(studio, iid)
    cid = studio.content.generate_text(sid, ["instagram_post"], ["ko"])["contents"][0]["content_id"]
    with pytest.raises(StudioError) as e:
        studio.publish.prepare(cid)
    assert e.value.code == "CONTENT_NOT_APPROVED"
    apr = studio.approvals.list("PENDING", "CONTENT")["approvals"][0]
    with pytest.raises(StudioError) as e:
        studio.approvals.decide(apr["approval_id"], "APPROVED", "agent")
    assert e.value.code == "APPROVAL_REQUIRES_HUMAN"
    studio.approvals.decide(apr["approval_id"], "APPROVED", "admin")

    insta = studio.publish.prepare(cid, "instagram")["publication"]
    res = studio.publish.execute(insta["publication_id"])
    assert res["supported"] is False and res["publication"]["status"] == "MANUAL_REQUIRED"

    local = studio.publish.prepare(cid, "local_export")["publication"]
    res = studio.publish.execute(local["publication_id"])
    assert res["publication"]["status"] == "PUBLISHED"
    assert Path(res["publication"]["external_id"], "manifest.json").exists()
    assert studio.publish.execute(local["publication_id"])["deduplicated"]
    assert studio.publish.prepare(cid, "local_export")["deduplicated"]
    assert studio.db.one("SELECT COUNT(*) AS n FROM publications WHERE status = 'PUBLISHED'")["n"] == 1
    assert studio.stories.get(sid)["story"]["status"] == "PUBLISHED"


def test_scheduled_publication_runs_when_due(studio):
    iid = new_active(studio)
    sid = approved_story(studio, iid)
    cid = studio.content.generate_text(sid, ["blog"], ["ko"])["contents"][0]["content_id"]
    studio.content.decide(cid, "APPROVED", "admin")
    pub = studio.publish.prepare(cid, "local_export")["publication"]
    studio.publish.schedule(pub["publication_id"], "2000-01-01T09:00")
    studio.publish.run_due()
    assert studio.publish.status(pub["publication_id"])["publications"][0]["status"] == "PUBLISHED"


# ---------------------------------------------------------------- 테스트 7. 데이터 보존
def test_backup_and_restore_keeps_links(make_studio, tmp_path):
    st = make_studio("orig")
    iid = new_active(st)
    sid = approved_story(st, iid)
    st.content.generate_text(sid, ["instagram_post"], ["ko", "vi"])
    st.content.generate_image(sid, 2)
    st.jobs.run_pending()
    ref = st.settings.data_dir / "ref.png"
    ref.write_bytes(Path(st.db.one("SELECT file_path FROM assets LIMIT 1")["file_path"]).read_bytes())
    st.archive.archive_asset(iid, str(ref), "REFERENCE")
    before = st.archive.verify_integrity()
    assert before["ok"], before
    backup = st.archive.backup()

    with pytest.raises(StudioError):
        ArchiveService.restore(backup["path"], str(st.settings.data_dir))  # 기존 폴더 덮어쓰기 금지
    ArchiveService.restore(backup["path"], str(tmp_path / "restored"))
    st2 = make_studio("restored")
    after = st2.archive.verify_integrity()
    assert after["ok"], after
    assert after["counts"] == before["counts"]
    assert {r["story_id"] for r in st2.db.query("SELECT story_id FROM contents")} == {sid}
    assert st2.influencers.get(iid)["influencer"]["name"] == st.influencers.get(iid)["influencer"]["name"]
    assert st2.memory.search(iid, st.stories.get(sid)["story"]["title"])["results"]


def test_asset_import_path_is_restricted(studio, tmp_path):
    iid = new_active(studio)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    with pytest.raises(StudioError) as e:
        studio.archive.archive_asset(iid, str(outside), "IMAGE")
    assert e.value.code == "PATH_NOT_ALLOWED"


def test_memory_update_keeps_previous_version(studio):
    iid = new_active(studio)
    m = studio.memory.create(iid, "하린은 고수를 싫어한다.", "PREFERENCE", status="ACTIVE")["memory_id"]
    with pytest.raises(StudioError):
        studio.memory.update(m, "하린은 이제 고수를 좋아한다.", "")
    new = studio.memory.update(m, "하린은 이제 고수를 좋아한다.", "현지 생활로 입맛이 바뀜")["memory_id"]
    got = studio.memory.get(new)
    assert got["previous_versions"][0]["memory_id"] == m
    assert studio.memory.get(m)["memory"]["status"] == "SUPERSEDED"
    res = studio.memory.search(iid, "고수")["results"]
    assert [r["id"] for r in res if r["type"] == "MEMORY"] == [new]


def test_strategy_does_not_change_profile(studio):
    iid = new_active(studio)
    sid = approved_story(studio, iid)
    cid = studio.content.generate_text(sid, ["instagram_post"], ["ko"])["contents"][0]["content_id"]
    studio.analytics.record(cid, {"views": 1500, "likes": 120, "comments": 14})
    before = studio.influencers.get(iid)["influencer"]["profile_json"]
    out = studio.analytics.strategy_update(iid)
    assert out["proposal"]["plan"] and out["report"]["contents_measured"] == 1
    assert studio.influencers.get(iid)["influencer"]["profile_json"] == before
