"""의미 검색용 임베딩.

기본값은 외부 모델 없이 동작하는 문자 n-gram 해시 임베딩입니다(한국어·베트남어 모두 동작).
`AIS_OLLAMA_EMBED_MODEL` (예: bge-m3, nomic-embed-text)을 설정하면 Ollama 임베딩을 사용합니다.
벡터 인덱스는 원본 기억을 대체하지 않으며, 언제든 다시 만들 수 있습니다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.request

DIM = 512


class HashEmbedder:
    name = "hash-ngram-512"

    def embed(self, text: str) -> list[float]:
        text = re.sub(r"\s+", " ", text.lower()).strip()
        vec = [0.0] * DIM
        tokens = re.findall(r"\w+", text)
        grams: list[str] = []
        for tok in tokens:
            grams.append("w:" + tok)
            padded = f" {tok} "
            for n in (2, 3):
                grams += [padded[i:i + n] for i in range(len(padded) - n + 1)]
        for g in grams:
            h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "little")
            vec[h % DIM] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"

    def embed(self, text: str) -> list[float]:
        req = urllib.request.Request(
            f"{self.base_url}/api/embed", data=json.dumps({"model": self.model, "input": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        vec = data["embeddings"][0]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def build_embedder(settings):
    if settings.ollama_embed_model:
        return OllamaEmbedder(settings.ollama_url, settings.ollama_embed_model)
    return HashEmbedder()
