"""이미지 생성 어댑터.

- ComfyUIAdapter: ComfyUI API 형식 워크플로(JSON)의 자리표시자({{POSITIVE}}, {{NEGATIVE}}, {{SEED}},
  {{WIDTH}}, {{HEIGHT}}, {{REFERENCE_IMAGE}})를 채워 /prompt 로 제출하고 결과 이미지를 내려받습니다.
- GeminiImageAdapter: Google Gemini API(generateContent, 이미지 출력)로 생성합니다. 캐릭터 참조 이미지가 있으면
  함께 보내 같은 인물을 유지하도록 요청합니다. API 키는 환경 변수 AIS_GEMINI_API_KEY 로만 받습니다.
- PlaceholderImageAdapter: 생성 모델이 없을 때 쓰는 단색 PNG. 자산에 is_placeholder=1 로 기록되어
  실제 결과물과 구분되며, 검수함에서 표시됩니다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path


class ImageError(Exception):
    pass


class ImageAdapter:
    name = "base"
    is_placeholder = False

    def available(self) -> bool:
        return True

    def generate(self, prompt: str, negative: str, seed: int, width: int, height: int, out_path: Path,
                 reference_image: str | None = None) -> dict:
        raise NotImplementedError


def _png(width: int, height: int, rgb_top: tuple, rgb_bottom: tuple) -> bytes:
    rows = []
    for y in range(height):
        t = y / max(height - 1, 1)
        c = bytes(int(a + (b - a) * t) for a, b in zip(rgb_top, rgb_bottom))
        rows.append(b"\x00" + c * width)
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


class PlaceholderImageAdapter(ImageAdapter):
    name = "placeholder"
    is_placeholder = True

    def generate(self, prompt, negative, seed, width, height, out_path, reference_image=None):
        h = hashlib.sha256(f"{prompt}|{seed}".encode("utf-8")).digest()
        # 미리보기용으로 작은 크기로 만든다 (실제 해상도는 메타데이터에만 기록)
        w, hh = max(width // 8, 16), max(height // 8, 16)
        out_path.write_bytes(_png(w, hh, (h[0], h[1], h[2]), (h[3], h[4], h[5])))
        out_path.with_suffix(".prompt.txt").write_text(
            f"[PLACEHOLDER] 이미지 생성 모델이 연결되지 않아 자리표시 이미지를 만들었습니다.\n\n"
            f"prompt: {prompt}\nnegative: {negative}\nseed: {seed}\nsize: {width}x{height}\n"
            f"reference: {reference_image or '-'}\n", encoding="utf-8")
        return {"model": self.name, "width": w, "height": hh}


class ComfyUIAdapter(ImageAdapter):
    name = "comfyui"

    def __init__(self, base_url: str, workflow_path: Path, timeout: int = 600):
        self.base_url = base_url.rstrip("/")
        self.workflow_path = Path(workflow_path)
        self.timeout = timeout

    def available(self) -> bool:
        if not self.workflow_path.exists():
            return False
        try:
            with urllib.request.urlopen(f"{self.base_url}/system_stats", timeout=3):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def _fill(self, node, values: dict):
        if isinstance(node, dict):
            return {k: self._fill(v, values) for k, v in node.items()}
        if isinstance(node, list):
            return [self._fill(v, values) for v in node]
        if isinstance(node, str):
            for key, val in values.items():
                token = "{{" + key + "}}"
                if node == token:
                    return val
                node = node.replace(token, str(val))
        return node

    def generate(self, prompt, negative, seed, width, height, out_path, reference_image=None):
        try:
            workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ImageError(f"ComfyUI 워크플로 파일을 읽을 수 없습니다: {e}") from e
        workflow = {k: v for k, v in workflow.items() if not k.startswith("_")}  # 설명용 키 제거
        graph = self._fill(workflow, {"POSITIVE": prompt, "NEGATIVE": negative, "SEED": seed, "WIDTH": width,
                                      "HEIGHT": height, "REFERENCE_IMAGE": reference_image or ""})
        try:
            req = urllib.request.Request(f"{self.base_url}/prompt", data=json.dumps({"prompt": graph}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                prompt_id = json.loads(r.read())["prompt_id"]
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                with urllib.request.urlopen(f"{self.base_url}/history/{prompt_id}", timeout=30) as r:
                    hist = json.loads(r.read())
                if prompt_id in hist:
                    outputs = hist[prompt_id].get("outputs", {})
                    for node in outputs.values():
                        for img in node.get("images", []):
                            q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                                        "type": img.get("type", "output")})
                            with urllib.request.urlopen(f"{self.base_url}/view?{q}", timeout=60) as r:
                                out_path.write_bytes(r.read())
                            return {"model": "comfyui", "prompt_id": prompt_id, "width": width, "height": height}
                    raise ImageError("ComfyUI 작업이 끝났지만 이미지 출력이 없습니다.")
                time.sleep(2)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            raise ImageError(f"ComfyUI 호출 실패: {e}") from e
        raise ImageError("ComfyUI 이미지 생성 시간이 초과되었습니다.")


GEMINI_RATIOS = {"1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9"}
MIME_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class GeminiImageAdapter(ImageAdapter):
    name = "gemini"

    def __init__(self, api_key: str, model: str, api_version: str = "v1", image_size: str = "1K",
                 base_url: str = "https://generativelanguage.googleapis.com", timeout: int = 180):
        self.api_key = api_key
        self.model = model
        self.api_version = api_version
        self.image_size = image_size
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = f"gemini:{model}"

    def available(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _ratio(width: int, height: int) -> str:
        from math import gcd
        g = gcd(width, height) or 1
        r = f"{width // g}:{height // g}"
        if r in GEMINI_RATIOS:
            return r
        target = width / height
        return min(GEMINI_RATIOS, key=lambda x: abs(int(x.split(":")[0]) / int(x.split(":")[1]) - target))

    def generate(self, prompt, negative, seed, width, height, out_path, reference_image=None):
        if not self.api_key:
            raise ImageError("AIS_GEMINI_API_KEY 가 설정되지 않았습니다. .env 파일에 API 키를 넣으세요.")
        text = (f"Photorealistic lifestyle photo for social media. {prompt}. "
                f"Do not include: {negative}. No text, captions or watermarks in the image.")
        parts: list[dict] = [{"text": text}]
        if reference_image and Path(reference_image).is_file():
            ref = Path(reference_image)
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ref.suffix.lower(), "image/png")
            parts[0]["text"] = ("Use the attached reference photo as the identity of the main person: keep the same face, "
                                "hairstyle and age. " + text)
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(ref.read_bytes()).decode()}})
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                                     "imageConfig": {"aspectRatio": self._ratio(width, height),
                                                     "imageSize": self.image_size}}}
        url = f"{self.base_url}/{self.api_version}/models/{self.model}:generateContent"
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise ImageError(f"Gemini API 오류 {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ImageError(f"Gemini API 호출 실패: {e}") from e
        texts = []
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data") and not part.get("thought"):
                    mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    path = out_path.with_suffix(MIME_SUFFIX.get(mime, ".png"))
                    path.write_bytes(base64.b64decode(inline["data"]))
                    return {"model": self.name, "path": str(path), "aspect_ratio": self._ratio(width, height),
                            "image_size": self.image_size}
                if part.get("text"):
                    texts.append(part["text"])
        reason = ((data.get("promptFeedback") or {}).get("blockReason")
                  or (data.get("candidates") or [{}])[0].get("finishReason"))
        raise ImageError(f"Gemini 가 이미지를 돌려주지 않았습니다 (사유: {reason or '알 수 없음'}). {' '.join(texts)[:200]}")


def build_image_adapter(settings) -> ImageAdapter:
    if settings.image_provider == "placeholder":
        return PlaceholderImageAdapter()
    gemini = GeminiImageAdapter(settings.gemini_api_key, settings.gemini_image_model, settings.gemini_api_version,
                                settings.gemini_image_size)
    if settings.image_provider == "gemini":
        return gemini
    comfy = ComfyUIAdapter(settings.comfyui_url, settings.comfyui_workflow)
    if settings.image_provider == "comfyui":
        return comfy
    if gemini.available():
        return gemini
    return comfy if comfy.available() else PlaceholderImageAdapter()
