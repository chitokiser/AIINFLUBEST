"""웹 API 와 MCP 도구 등록 확인."""

import asyncio
import json

from fastapi.testclient import TestClient

from conftest import HARIN
from influencer_studio import mcp_server
from influencer_studio.web.app import create_app

SPEC_TOOLS = {
    "influencer_create", "influencer_get", "influencer_update", "influencer_list", "influencer_archive",
    "story_generate", "story_continue", "story_get", "story_search", "story_validate", "story_approve", "story_revise",
    "story_timeline", "memory_search", "memory_get", "memory_create", "memory_update", "archive_asset", "archive_export",
    "archive_backup", "content_plan", "content_generate_text", "content_generate_image", "content_generate_video",
    "content_generate_audio", "content_translate", "content_validate", "content_package", "publish_prepare",
    "publish_schedule", "publish_execute", "publish_status", "analytics_get", "analytics_report",
    "content_strategy_update",
}


def test_mcp_registers_every_spec_tool(studio, monkeypatch):
    monkeypatch.setattr(mcp_server, "_studio", studio)
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert SPEC_TOOLS <= names, SPEC_TOOLS - names


def test_mcp_flow_and_errors(studio, monkeypatch):
    monkeypatch.setattr(mcp_server, "_studio", studio)

    def call(name, args):
        res = asyncio.run(mcp_server.mcp.call_tool(name, args))
        return json.loads(res.content[0].text)

    created = call("influencer_create", {"description": HARIN})
    iid = created["influencer_id"]
    assert call("story_generate", {"influencer_id": iid})["error"] == "INFLUENCER_NOT_ACTIVE"
    call("influencer_update", {"influencer_id": iid, "status": "ACTIVE"})
    story = call("story_generate", {"influencer_id": iid, "topic": "한식당"})
    assert call("story_approve", {"story_id": story["story_id"]})["status"] == "APPROVED"
    texts = call("content_generate_text", {"story_id": story["story_id"], "formats": ["instagram_post"]})
    approval = call("review_queue", {"target_type": "CONTENT"})["approvals"][0]
    denied = call("review_decide", {"approval_id": approval["approval_id"], "decision": "APPROVED"})
    assert denied["error"] == "APPROVAL_REQUIRES_HUMAN"
    assert call("publish_prepare", {"content_id": texts["contents"][0]["content_id"]})["error"] == "CONTENT_NOT_APPROVED"
    assert call("system_status", {})["ok"]


def test_web_dashboard_flow(studio):
    client = TestClient(create_app(studio, start_background=False))
    assert "AI Influencer Studio" in client.get("/").text
    r = client.post("/api/influencers", json={"description": HARIN}).json()
    iid = r["influencer_id"]
    client.patch(f"/api/influencers/{iid}", json={"status": "ACTIVE"})
    run = client.post("/api/studio/run", json={"influencer_id": iid, "topic": "새로운 한국 음식점 발견", "images": 2,
                                               "languages": ["ko", "vi"]}).json()
    assert run["ok"], run
    studio.jobs.run_pending()
    pending = client.get("/api/approvals?target_type=CONTENT").json()["approvals"]
    text_items = [a for a in pending if a["payload_json"].get("type") != "image_set"]
    assert len(text_items) == 6
    detail = client.get(f"/api/contents/{text_items[0]['target_id']}").json()
    assert detail["validation"]["ok"]
    ok = client.post(f"/api/approvals/{text_items[0]['approval_id']}", json={"decision": "APPROVED"}).json()
    assert ok["status"] == "APPROVED"
    img = [a for a in pending if a["payload_json"].get("type") == "image_set"][0]
    img_detail = client.get(f"/api/contents/{img['target_id']}").json()
    assert img_detail["assets"] and client.get(img_detail["assets"][0]["url"]).status_code == 200
    bad = client.post(f"/api/approvals/{img['approval_id']}", json={"decision": "APPROVED"})
    assert bad.status_code == 400 and bad.json()["error"] == "VALIDATION_FAILED"
    pub = client.post(f"/api/contents/{text_items[0]['target_id']}/publish", json={"platform": "local_export"}).json()
    done = client.post(f"/api/publications/{pub['publication']['publication_id']}/execute").json()
    assert done["publication"]["status"] == "PUBLISHED"
    assert client.get("/media/../../etc/passwd").status_code in (403, 404)
    assert client.get("/api/system/integrity").json()["ok"]
    for path in ("/api/dashboard", "/api/models", "/api/jobs", "/api/schedules", f"/api/influencers/{iid}/timeline",
                 f"/api/influencers/{iid}/entities", f"/api/influencers/{iid}/report", "/api/publications"):
        assert client.get(path).status_code == 200, path


def test_admin_token_guard(studio, monkeypatch):
    studio.settings.admin_token = "secret"
    client = TestClient(create_app(studio, start_background=False))
    assert client.post("/api/influencers", json={"description": HARIN}).status_code == 401
    assert client.post("/api/influencers", json={"description": HARIN},
                       headers={"X-Admin-Token": "secret"}).status_code == 200
