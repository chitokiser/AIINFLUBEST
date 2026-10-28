"""관리자 웹 대시보드 (기획서 10장).

로컬 PC 전용으로 127.0.0.1 에서만 열립니다. AIS_ADMIN_TOKEN 을 설정하면 쓰기 요청에 X-Admin-Token 헤더가 필요합니다.
웹에서 하는 모든 작업의 행위자는 'admin'(운영자) 으로 기록됩니다.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ..studio import Studio
from ..util import StudioError, new_id, safe_path

STATIC = Path(__file__).parent / "static"
ADMIN = "admin"


def create_app(studio: Studio | None = None, start_background: bool = True) -> FastAPI:
    st = studio or Studio()

    @asynccontextmanager
    async def lifespan(app):
        if start_background:
            st.start_background()
        yield
        st.jobs.stop_worker()

    app = FastAPI(title="AI Influencer Studio", lifespan=lifespan)
    app.state.studio = st

    def media_url(file_path: str) -> str | None:
        try:
            return "/media/" + Path(file_path).resolve().relative_to(st.settings.data_dir).as_posix()
        except ValueError:
            return None

    @app.exception_handler(StudioError)
    async def studio_error(_req: Request, exc: StudioError):
        status = 404 if exc.code == "NOT_FOUND" else 403 if exc.code in ("FORBIDDEN", "APPROVAL_REQUIRES_HUMAN") else 400
        return JSONResponse(exc.to_dict(), status_code=status)

    @app.middleware("http")
    async def token_guard(request: Request, call_next):
        token = st.settings.admin_token
        if token and request.method in ("POST", "PATCH", "PUT", "DELETE") and request.url.path.startswith("/api/"):
            if request.headers.get("X-Admin-Token") != token:
                return JSONResponse({"ok": False, "error": "UNAUTHORIZED", "message": "관리자 토큰이 필요합니다."},
                                    status_code=401)
        return await call_next(request)

    # ------------------------------------------------------------ 화면
    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

    @app.get("/media/{path:path}")
    def media(path: str):
        try:
            f = safe_path(st.settings.data_dir, path)
        except ValueError:
            raise StudioError("FORBIDDEN", "허용되지 않은 경로입니다.")
        if not f.is_file():
            raise StudioError("NOT_FOUND", "파일이 없습니다.")
        return FileResponse(f)

    # ------------------------------------------------------------ 요약·설정
    @app.get("/api/dashboard")
    def dashboard():
        return {"ok": True, **st.dashboard()}

    @app.get("/api/models")
    def models():
        return {"ok": True, **st.models_status()}

    @app.get("/api/audit")
    def audit(limit: int = 200):
        return {"ok": True, "logs": st.db.query("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,))}

    # ------------------------------------------------------------ 인플루언서
    @app.get("/api/influencers")
    def influencers(status: str | None = None):
        return st.influencers.list(status)

    @app.post("/api/influencers")
    def influencer_create(body: dict = Body(...)):
        return st.influencers.create(body.get("description", ""), body.get("overrides"), actor=ADMIN)

    @app.get("/api/influencers/{iid}")
    def influencer_get(iid: str):
        res = st.influencers.get(iid)
        res["versions"] = st.db.query("SELECT version_id, version_no, status, change_reason, changed_fields, created_by, "
                                      "created_at, effective_at FROM character_versions WHERE influencer_id = ? "
                                      "ORDER BY version_no DESC", (iid,))
        res["references"] = st.db.query("SELECT asset_id, file_path FROM assets WHERE influencer_id = ? "
                                        "AND asset_type = 'REFERENCE'", (iid,))
        for a in res["references"]:
            a["url"] = media_url(a["file_path"])
        return res

    @app.patch("/api/influencers/{iid}")
    def influencer_update(iid: str, body: dict = Body(...)):
        return st.influencers.update(iid, body.get("changes"), body.get("reason", ""), body.get("status"), actor=ADMIN)

    @app.post("/api/influencers/{iid}/relate")
    def influencer_relate(iid: str, body: dict = Body(...)):
        return st.influencers.relate(iid, body["other_influencer_id"], body["relation_type"], actor=ADMIN)

    @app.get("/api/influencers/{iid}/timeline")
    def timeline(iid: str, date_from: str | None = None, date_to: str | None = None, drafts: bool = True):
        return st.stories.timeline(iid, date_from, date_to, drafts)

    @app.get("/api/influencers/{iid}/stories")
    def stories(iid: str, q: str = "", status: str | None = None):
        return st.stories.search(iid, q, status, 100)

    @app.post("/api/influencers/{iid}/stories")
    def story_generate(iid: str, body: dict = Body(default={})):
        return st.stories.generate(iid, body.get("topic", ""), body.get("story_date") or None,
                                   body.get("story_type", "SERIAL"), is_major=bool(body.get("is_major")),
                                   co_influencers=body.get("co_influencers"), is_flashback=bool(body.get("is_flashback")),
                                   actor=ADMIN)

    @app.post("/api/influencers/{iid}/season")
    def season(iid: str, body: dict = Body(default={})):
        return st.stories.plan_season(iid, int(body.get("total_days", 30)), body.get("theme", ""),
                                      body.get("start_date") or None, int(body.get("chunk_days", 7)), actor=ADMIN)

    @app.get("/api/influencers/{iid}/seasons")
    def seasons(iid: str):
        return {"ok": True, "seasons": st.db.query("SELECT * FROM seasons WHERE influencer_id = ? ORDER BY created_at DESC",
                                                   (iid,))}

    @app.post("/api/seasons/{sid}/next")
    def season_next(sid: str):
        return st.stories.generate_season_chunk(sid, actor=ADMIN)

    @app.post("/api/seasons/{sid}/approve")
    def season_approve(sid: str):
        return st.stories.approve_season_chunk(sid, actor=ADMIN)

    @app.get("/api/influencers/{iid}/memory")
    def memory_search(iid: str, q: str = "", types: str = "", date_from: str | None = None, date_to: str | None = None):
        if not q:
            return {"ok": True, "results": st.db.query(
                "SELECT memory_id AS id, 'MEMORY' AS type, content AS text, valid_from AS occurred_at, status, fact_type "
                "FROM memories WHERE influencer_id = ? ORDER BY created_at DESC LIMIT 100", (iid,))}
        return st.memory.search(iid, q, [t for t in types.split(",") if t] or None, 20, date_from, date_to)

    @app.post("/api/influencers/{iid}/memory")
    def memory_create(iid: str, body: dict = Body(...)):
        return st.memory.create(iid, body.get("content", ""), body.get("memory_type", "FACT"), None, "manual",
                                "ACTIVE", body.get("fact_type", "FICTION"), 1.0, None, body.get("valid_from"),
                                actor=ADMIN)

    @app.get("/api/memories/{mid}")
    def memory_get(mid: str):
        return st.memory.get(mid)

    @app.patch("/api/memories/{mid}")
    def memory_update(mid: str, body: dict = Body(...)):
        return st.memory.update(mid, body.get("content", ""), body.get("reason", ""), actor=ADMIN)

    @app.get("/api/influencers/{iid}/entities")
    def entities(iid: str):
        return {"ok": True, "people": st.memory.known_people(iid), "places": st.memory.known_places(iid)}

    @app.post("/api/influencers/{iid}/export")
    def export(iid: str):
        return st.archive.export(iid)

    @app.post("/api/influencers/{iid}/reference")
    def reference_upload(iid: str, body: dict = Body(...)):
        """캐릭터 참조 이미지 업로드 (base64). 업로드 파일은 data/uploads 에 저장한 뒤 아카이브에 등록합니다."""
        name = Path(body.get("filename", "ref.png")).name
        if Path(name).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            raise StudioError("INVALID_INPUT", "png/jpg/webp 이미지만 업로드할 수 있습니다.")
        raw = base64.b64decode(body.get("data_base64", "").split(",")[-1])
        if len(raw) > 20 * 1024 * 1024:
            raise StudioError("INVALID_INPUT", "20MB 이하 파일만 업로드할 수 있습니다.")
        up = safe_path(st.settings.data_dir, "uploads")
        up.mkdir(exist_ok=True)
        f = up / f"{new_id('up')}_{name}"
        f.write_bytes(raw)
        return st.archive.archive_asset(iid, str(f), body.get("asset_type", "REFERENCE"), actor=ADMIN)

    # ------------------------------------------------------------ 스토리
    @app.get("/api/stories/{sid}")
    def story_get(sid: str):
        return st.stories.get(sid)

    @app.post("/api/stories/{sid}/validate")
    def story_validate(sid: str):
        return st.stories.validate(sid)

    @app.post("/api/stories/{sid}/approve")
    def story_approve(sid: str, body: dict = Body(default={})):
        return st.stories.approve(sid, actor=ADMIN, force_reason=body.get("force_reason", ""))

    @app.post("/api/stories/{sid}/reject")
    def story_reject(sid: str, body: dict = Body(default={})):
        return st.stories.reject(sid, ADMIN, body.get("comment", ""))

    @app.post("/api/stories/{sid}/revise")
    def story_revise(sid: str, body: dict = Body(...)):
        return st.stories.revise(sid, body.get("changes"), body.get("instruction", ""), actor=ADMIN)

    @app.post("/api/stories/{sid}/continue")
    def story_continue(sid: str, body: dict = Body(default={})):
        return st.stories.continue_story(sid, body.get("topic", ""), actor=ADMIN)

    @app.post("/api/stories/{sid}/text")
    def content_text(sid: str, body: dict = Body(default={})):
        return st.content.generate_text(sid, body.get("formats"), body.get("languages"), int(body.get("variants", 1)),
                                        int(body.get("shorts_duration", 30)), actor=ADMIN)

    @app.post("/api/stories/{sid}/images")
    def content_images(sid: str, body: dict = Body(default={})):
        return st.content.generate_image(sid, int(body.get("count", 3)), body.get("aspect", "4:5"),
                                         body.get("idempotency_key"), actor=ADMIN)

    @app.post("/api/stories/{sid}/video")
    def content_video(sid: str):
        return st.content.generate_video(sid, actor=ADMIN)

    @app.post("/api/stories/{sid}/audio")
    def content_audio(sid: str, body: dict = Body(default={})):
        return st.content.generate_audio(sid, body.get("language", "ko"), actor=ADMIN)

    @app.post("/api/stories/{sid}/package")
    def content_package(sid: str, body: dict = Body(default={})):
        return st.content.package(sid, body.get("platform"))

    # ------------------------------------------------------------ 콘텐츠 스튜디오 (한 번에 실행)
    @app.post("/api/studio/run")
    def studio_run(body: dict = Body(...)):
        """스토리 생성 → 모순 검사 → (문제없으면) 운영자 승인 → 텍스트·이미지 생성. 콘텐츠는 검수 대기로 남습니다."""
        iid = body["influencer_id"]
        steps: list[dict[str, Any]] = []
        story = st.stories.generate(iid, body.get("topic", ""), actor=ADMIN)
        steps.append({"step": "스토리 초안", "story_id": story["story_id"], "title": story["story"]["title"],
                      "validation": story["validation"]})
        if not story["validation"]["ok"]:
            return {"ok": False, "stopped_at": "모순 검사", "steps": steps, "story_id": story["story_id"],
                    "message": "모순 검사 오류가 있어 멈췄습니다. 스토리 화면에서 수정하세요."}
        if body.get("auto_approve_story", True):
            st.stories.approve(story["story_id"], actor=ADMIN)
            steps.append({"step": "스토리 확정"})
        else:
            return {"ok": True, "steps": steps, "story_id": story["story_id"],
                    "message": "스토리 검수 후 콘텐츠를 생성하세요."}
        texts = st.content.generate_text(story["story_id"], body.get("formats"), body.get("languages"),
                                         int(body.get("variants", 1)), int(body.get("shorts_duration", 30)), actor=ADMIN)
        steps.append({"step": "텍스트 생성", "contents": texts["contents"]})
        if int(body.get("images", 0)) > 0:
            img = st.content.generate_image(story["story_id"], int(body["images"]), body.get("aspect", "4:5"), actor=ADMIN)
            steps.append({"step": "이미지 작업 등록", "job_id": img["job"]["job_id"]})
        if body.get("video"):
            vid = st.content.generate_video(story["story_id"], actor=ADMIN)
            steps.append({"step": "영상 작업 등록", "job_id": vid["job"]["job_id"]})
        return {"ok": True, "story_id": story["story_id"], "steps": steps}

    # ------------------------------------------------------------ 콘텐츠·검수
    @app.get("/api/contents")
    def contents(influencer_id: str | None = None, status: str | None = None, story_id: str | None = None):
        return st.content.list(influencer_id, status, story_id)

    @app.get("/api/contents/{cid}")
    def content_get(cid: str):
        res = st.content.get(cid)
        res["validation"] = st.content.validate(cid)
        for a in res["assets"]:
            a["url"] = media_url(a["file_path"])
        return res

    @app.post("/api/contents/{cid}/revise")
    def content_revise(cid: str, body: dict = Body(...)):
        return st.content.revise(cid, body.get("changes", {}), actor=ADMIN, comment=body.get("comment", ""))

    @app.post("/api/contents/{cid}/translate")
    def content_translate(cid: str, body: dict = Body(...)):
        return st.content.translate(cid, body["target_language"], actor=ADMIN)

    @app.get("/api/approvals")
    def approvals(decision: str = "PENDING", target_type: str | None = None, influencer_id: str | None = None):
        return st.approvals.list(decision, target_type, influencer_id)

    @app.post("/api/approvals/{aid}")
    def approval_decide(aid: str, body: dict = Body(...)):
        return st.approvals.decide(aid, body.get("decision", ""), ADMIN, body.get("comment", ""),
                                   body.get("force_reason", ""))

    # ------------------------------------------------------------ 작업
    @app.get("/api/jobs")
    def jobs(status: str | None = None):
        return st.jobs.list(status, 100)

    @app.post("/api/jobs/{jid}/retry")
    def job_retry(jid: str):
        return st.jobs.retry(jid, actor=ADMIN)

    @app.post("/api/jobs/{jid}/cancel")
    def job_cancel(jid: str):
        return st.jobs.cancel(jid, actor=ADMIN)

    # ------------------------------------------------------------ 게시
    @app.get("/api/publications")
    def publications(influencer_id: str | None = None):
        return st.publish.status(influencer_id=influencer_id)

    @app.post("/api/contents/{cid}/publish")
    def publish_prepare(cid: str, body: dict = Body(default={})):
        return st.publish.prepare(cid, body.get("platform"), actor=ADMIN)

    @app.post("/api/publications/{pid}/schedule")
    def publish_schedule(pid: str, body: dict = Body(...)):
        return st.publish.schedule(pid, body["scheduled_at"], actor=ADMIN)

    @app.post("/api/publications/{pid}/execute")
    def publish_execute(pid: str):
        return st.publish.execute(pid, actor=ADMIN)

    @app.post("/api/publications/{pid}/confirm")
    def publish_confirm(pid: str, body: dict = Body(default={})):
        return st.publish.confirm_manual(pid, body.get("external_url", ""), actor=ADMIN)

    @app.post("/api/publications/{pid}/cancel")
    def publish_cancel(pid: str):
        return st.publish.cancel(pid, actor=ADMIN)

    # ------------------------------------------------------------ 성과
    @app.get("/api/influencers/{iid}/report")
    def report(iid: str, days: int = 30):
        return st.analytics.report(iid, days)

    @app.post("/api/influencers/{iid}/strategy")
    def strategy(iid: str, body: dict = Body(default={})):
        return st.analytics.strategy_update(iid, int(body.get("days", 30)), actor=ADMIN)

    @app.post("/api/analytics")
    def analytics_record(body: dict = Body(...)):
        return st.analytics.record(body["content_id"], body.get("metrics", {}), body.get("measured_at"), "manual",
                                   actor=ADMIN)

    @app.post("/api/analytics/csv")
    def analytics_csv(body: dict = Body(...)):
        return st.analytics.import_csv(body.get("csv", ""), actor=ADMIN)

    # ------------------------------------------------------------ 스케줄·시스템
    @app.get("/api/schedules")
    def schedules():
        return st.scheduler.list()

    @app.post("/api/schedules")
    def schedule_set(body: dict = Body(...)):
        return st.scheduler.set(body["influencer_id"], body["run_time"], body.get("task"),
                                bool(body.get("enabled", True)), actor=ADMIN)

    @app.post("/api/system/backup")
    def backup():
        return st.archive.backup(actor=ADMIN)

    @app.get("/api/system/integrity")
    def integrity():
        return st.archive.verify_integrity()

    @app.post("/api/system/reindex")
    def reindex():
        return st.memory.reindex_all()

    return app


def main() -> None:
    import uvicorn

    st = Studio()
    print(f"AI Influencer Studio 대시보드: http://{st.settings.web_host}:{st.settings.web_port}")
    uvicorn.run(create_app(st), host=st.settings.web_host, port=st.settings.web_port, log_level="warning")


if __name__ == "__main__":
    main()
