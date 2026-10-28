"""자산 등록·내보내기·백업·복구 (기획서 5장, 테스트 7)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from ..util import StudioError, dumps, new_id, now_iso, safe_path

ASSET_TYPES = {"IMAGE", "VIDEO", "AUDIO", "SUBTITLE", "REFERENCE", "OTHER"}


class ArchiveService:
    def __init__(self, studio):
        self.st = studio
        self.db = studio.db

    def _import_roots(self) -> list[Path]:
        roots = [self.st.settings.data_dir]
        extra = os.environ.get("AIS_IMPORT_DIRS", "")
        roots += [Path(p).resolve() for p in extra.split(";") if p.strip()]
        return roots

    def archive_asset(self, influencer_id: str, source_path: str, asset_type: str = "IMAGE",
                      story_id: str | None = None, content_id: str | None = None, prompt: str = "", model: str = "",
                      actor: str = "agent") -> dict:
        """외부 파일을 아카이브에 등록합니다. 허용된 폴더(data 폴더, AIS_IMPORT_DIRS) 안의 파일만 가져옵니다."""
        self.st.influencers._require(influencer_id)
        asset_type = asset_type.upper()
        if asset_type not in ASSET_TYPES:
            raise StudioError("INVALID_INPUT", f"asset_type 은 {sorted(ASSET_TYPES)} 중 하나입니다.")
        src = Path(source_path).resolve()
        if not any(src == r or r in src.parents for r in self._import_roots()):
            raise StudioError("PATH_NOT_ALLOWED", "허용된 폴더 밖의 파일입니다. AIS_IMPORT_DIRS 에 폴더를 추가하세요.",
                              allowed=[str(r) for r in self._import_roots()])
        if not src.is_file():
            raise StudioError("NOT_FOUND", f"파일이 없습니다: {src}")
        if story_id and not self.db.get("stories", "story_id", story_id):
            raise StudioError("NOT_FOUND", f"스토리를 찾을 수 없습니다: {story_id}")
        dest_dir = safe_path(self.st.settings.media_dir, influencer_id, asset_type.lower())
        dest_dir.mkdir(parents=True, exist_ok=True)
        aid = new_id("ast")
        dest = dest_dir / f"{aid}{src.suffix.lower()}"
        shutil.copy2(src, dest)
        self.db.insert("assets", {"asset_id": aid, "influencer_id": influencer_id, "story_id": story_id,
                                  "content_id": content_id, "job_id": None, "slot": None, "asset_type": asset_type,
                                  "file_path": str(dest), "prompt": prompt, "model": model, "params_json": {},
                                  "is_placeholder": 0,
                                  "review_status": "APPROVED" if asset_type == "REFERENCE" else "REVIEW_REQUIRED",
                                  "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), "created_at": now_iso()})
        self.db.audit(actor, "archive.asset", "ASSET", aid, {"type": asset_type, "source": str(src)})
        note = "캐릭터 참조 이미지로 등록되어 이후 이미지 생성에 사용됩니다." if asset_type == "REFERENCE" else None
        return {"ok": True, "asset_id": aid, "file_path": str(dest), "note": note}

    def export(self, influencer_id: str) -> dict:
        inf = self.st.influencers._require(influencer_id)
        q = self.db.query
        stories = q("SELECT * FROM stories WHERE influencer_id = ? ORDER BY story_date", (influencer_id,))
        sids = [s["story_id"] for s in stories] or [""]
        ph = ",".join("?" for _ in sids)
        contents = q("SELECT * FROM contents WHERE influencer_id = ?", (influencer_id,))
        cids = [c["content_id"] for c in contents] or [""]
        cph = ",".join("?" for _ in cids)
        data = {
            "exported_at": now_iso(), "influencer": inf,
            "character_versions": q("SELECT * FROM character_versions WHERE influencer_id = ? ORDER BY version_no", (influencer_id,)),
            "world_settings": q("SELECT * FROM world_settings WHERE influencer_id = ?", (influencer_id,)),
            "seasons": q("SELECT * FROM seasons WHERE influencer_id = ?", (influencer_id,)),
            "stories": stories,
            "episodes": q(f"SELECT * FROM episodes WHERE story_id IN ({ph})", sids),
            "story_links": q(f"SELECT * FROM story_links WHERE source_story_id IN ({ph})", sids),
            "events": q("SELECT * FROM story_events WHERE influencer_id = ? ORDER BY occurred_at", (influencer_id,)),
            "entities": q("SELECT * FROM entities WHERE influencer_id = ?", (influencer_id,)),
            "relationships": q("SELECT * FROM relationships WHERE influencer_id = ?", (influencer_id,)),
            "memories": q("SELECT * FROM memories WHERE influencer_id = ?", (influencer_id,)),
            "contents": contents,
            "content_versions": q(f"SELECT * FROM content_versions WHERE content_id IN ({cph})", cids),
            "assets": q("SELECT * FROM assets WHERE influencer_id = ?", (influencer_id,)),
            "publications": q(f"SELECT * FROM publications WHERE content_id IN ({cph})", cids),
            "analytics": q("SELECT * FROM analytics WHERE influencer_id = ?", (influencer_id,)),
        }
        path = safe_path(self.st.settings.export_dir, f"{influencer_id}_{datetime.now():%Y%m%d_%H%M%S}.json")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"ok": True, "path": str(path), "counts": {k: len(v) for k, v in data.items() if isinstance(v, list)}}

    def backup(self, actor: str = "agent") -> dict:
        """DB(일관된 스냅샷)와 미디어 파일을 zip 하나로 묶습니다. 파일 경로는 data 폴더 기준 상대 경로로 저장합니다."""
        s = self.st.settings
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_db = s.backup_dir / f"studio_{stamp}.db"
        self.db.backup_to(tmp_db)
        zpath = s.backup_dir / f"backup_{stamp}.zip"
        files = 0
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp_db, "studio.db")
            for f in s.media_dir.rglob("*"):
                if f.is_file():
                    z.write(f, f"media/{f.relative_to(s.media_dir).as_posix()}")
                    files += 1
            z.writestr("manifest.json", dumps({"created_at": now_iso(), "data_dir": str(s.data_dir),
                                               "media_files": files, "schema_version": 1}))
        tmp_db.unlink()
        self.db.audit(actor, "archive.backup", None, None, {"path": str(zpath)})
        return {"ok": True, "path": str(zpath), "media_files": files, "size_bytes": zpath.stat().st_size}

    @staticmethod
    def restore(zip_path: str, target_data_dir: str) -> dict:
        """백업을 새 data 폴더에 복구하고, 미디어 경로를 새 위치로 바꿉니다. (기존 폴더 덮어쓰기 금지)"""
        target = Path(target_data_dir).resolve()
        if target.exists() and any(target.iterdir()):
            raise StudioError("TARGET_NOT_EMPTY", f"복구 대상 폴더가 비어 있지 않습니다: {target}")
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            manifest = json.loads(z.read("manifest.json"))
            for member in z.namelist():
                dest = (target / member).resolve()
                if target not in dest.parents:
                    raise StudioError("PATH_NOT_ALLOWED", f"잘못된 백업 항목: {member}")
            z.extractall(target)
        old_media = str(Path(manifest["data_dir"]) / "media")
        new_media = str(target / "media")
        con = sqlite3.connect(target / "studio.db")
        try:
            con.execute("UPDATE assets SET file_path = replace(file_path, ?, ?)", (old_media, new_media))
            con.commit()
        finally:
            con.close()
        return {"ok": True, "data_dir": str(target), "manifest": manifest}

    def verify_integrity(self) -> dict:
        """스토리·콘텐츠·자산·게시 기록의 연결 관계와 파일 존재 여부를 점검합니다."""
        q = self.db.query
        problems = []
        for r in q("SELECT c.content_id FROM contents c LEFT JOIN stories s ON s.story_id = c.story_id WHERE s.story_id IS NULL"):
            problems.append({"type": "ORPHAN_CONTENT", "id": r["content_id"]})
        for r in q("SELECT c.content_id FROM contents c LEFT JOIN content_versions v ON v.content_id = c.content_id "
                   "AND v.version_no = c.current_version WHERE v.version_id IS NULL"):
            problems.append({"type": "MISSING_CONTENT_VERSION", "id": r["content_id"]})
        for r in q("SELECT asset_id, file_path FROM assets"):
            if not Path(r["file_path"]).exists():
                problems.append({"type": "MISSING_FILE", "id": r["asset_id"], "path": r["file_path"]})
        for r in q("SELECT e.event_id FROM story_events e LEFT JOIN stories s ON s.story_id = e.story_id "
                   "WHERE e.story_id IS NOT NULL AND s.story_id IS NULL"):
            problems.append({"type": "ORPHAN_EVENT", "id": r["event_id"]})
        counts = {t: self.db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in
                  ("influencers", "stories", "contents", "assets", "memories", "story_events", "publications")}
        return {"ok": not problems, "problems": problems, "counts": counts}
