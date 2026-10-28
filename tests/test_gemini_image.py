"""Gemini 이미지 어댑터 (네트워크 없이 가짜 응답으로 검사)."""

import base64
import io
import json
import urllib.error

import pytest

from conftest import approved_story, new_active
from influencer_studio.adapters import image as image_mod
from influencer_studio.adapters.image import GeminiImageAdapter, ImageError, _png
from influencer_studio.config import load_settings

PNG = _png(4, 4, (10, 20, 30), (40, 50, 60))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_api(monkeypatch, payload=None, error=None):
    calls = []

    def urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "headers": dict(req.header_items()), "body": json.loads(req.data)})
        if error:
            raise error
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(image_mod.urllib.request, "urlopen", urlopen)
    return calls


def image_payload(mime="image/png"):
    return {"candidates": [{"content": {"parts": [
        {"text": "thinking", "thought": True},
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(PNG).decode()}}]}}]}


def test_request_shape_and_reference(monkeypatch, tmp_path):
    calls = fake_api(monkeypatch, image_payload("image/jpeg"))
    ref = tmp_path / "face.png"
    ref.write_bytes(PNG)
    ad = GeminiImageAdapter("test-key", "gemini-nano-banana-2.1")
    meta = ad.generate("27 year old woman, wavy hair", "deformed", 1, 1080, 1350, tmp_path / "out.png", str(ref))
    req = calls[0]
    assert req["url"].endswith("/v1/models/gemini-nano-banana-2.1:generateContent")
    assert req["headers"]["X-goog-api-key"] == "test-key"
    cfg = req["body"]["generationConfig"]
    assert cfg["responseModalities"] == ["TEXT", "IMAGE"]
    assert cfg["imageConfig"] == {"aspectRatio": "4:5", "imageSize": "1K"}
    parts = req["body"]["contents"][0]["parts"]
    assert "reference photo" in parts[0]["text"] and parts[1]["inline_data"]["mime_type"] == "image/png"
    assert meta["path"].endswith(".jpg") and (tmp_path / "out.jpg").read_bytes() == PNG


def test_blocked_and_http_errors(monkeypatch, tmp_path):
    ad = GeminiImageAdapter("k", "m")
    fake_api(monkeypatch, {"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(ImageError, match="SAFETY"):
        ad.generate("p", "n", 1, 1080, 1080, tmp_path / "a.png")
    err = urllib.error.HTTPError("u", 429, "quota", {}, io.BytesIO(b'{"error":"RESOURCE_EXHAUSTED"}'))
    fake_api(monkeypatch, error=err)
    with pytest.raises(ImageError, match="429"):
        ad.generate("p", "n", 1, 1080, 1080, tmp_path / "a.png")
    with pytest.raises(ImageError, match="AIS_GEMINI_API_KEY"):
        GeminiImageAdapter("", "m").generate("p", "n", 1, 10, 10, tmp_path / "b.png")


def test_provider_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("AIS_IMAGE_PROVIDER", "auto")
    monkeypatch.setenv("AIS_GEMINI_API_KEY", "k")
    monkeypatch.setenv("AIS_GEMINI_IMAGE_MODEL", "gemini-3-pro-image")
    ad = image_mod.build_image_adapter(load_settings(tmp_path))
    assert isinstance(ad, GeminiImageAdapter) and ad.model == "gemini-3-pro-image"
    monkeypatch.setenv("AIS_GEMINI_API_KEY", "")
    monkeypatch.setenv("AIS_COMFYUI_URL", "http://127.0.0.1:9")
    assert image_mod.build_image_adapter(load_settings(tmp_path)).is_placeholder


def test_gemini_images_flow_into_review(make_studio, monkeypatch):
    fake_api(monkeypatch, image_payload())
    st = make_studio(image=GeminiImageAdapter("k", "gemini-nano-banana-2.1"))
    iid = new_active(st)
    sid = approved_story(st, iid)
    res = st.content.generate_image(sid, 2)
    st.jobs.run_pending()
    job = st.jobs.get(res["job"]["job_id"])["job"]
    assert job["status"] == "REVIEW_REQUIRED", job
    assets = st.db.query("SELECT model, is_placeholder, file_path FROM assets WHERE content_id = ?", (res["content_id"],))
    assert len(assets) == 2 and all(a["is_placeholder"] == 0 and a["model"].startswith("gemini") for a in assets)
    # 실제 이미지이므로 사람이 승인할 수 있다
    assert st.content.decide(res["content_id"], "APPROVED", "admin")["status"] == "APPROVED"
