from __future__ import annotations

import json
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def new_id(prefix: str) -> str:
    """시간 순으로 정렬되는 짧은 고유 ID (예: inf_01k2...)."""
    t = int(time.time() * 1000)
    head = ""
    for _ in range(9):
        head = _ALPHABET[t % 32] + head
        t //= 32
    tail = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{prefix}_{head}{tail}"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def safe_path(base: Path, *parts: str) -> Path:
    """base 밖으로 나가는 경로(.., 절대 경로)를 막습니다."""
    base = base.resolve()
    target = base.joinpath(*parts).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"허용되지 않은 경로입니다: {target}")
    return target


def slug(text: str, limit: int = 40) -> str:
    keep = [c if c.isalnum() else "_" for c in text.strip()]
    s = "".join(keep).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return (s or "item")[:limit]


class StudioError(Exception):
    """도구 호출자에게 그대로 보여줄 수 있는 오류."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, **self.detail}
