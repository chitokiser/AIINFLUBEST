"""데이터 계층.

서비스 코드는 `Store` 의 일반 메서드(insert/update/get/query/execute)만 사용합니다.
SQL 은 PostgreSQL 과 호환되는 범위에서 작성했으므로, 데이터와 동시 사용자가 늘어나면
같은 인터페이스를 가진 PostgresStore(+pgvector)로 교체할 수 있습니다.
검색 전용 부분(FTS5, 임베딩 테이블)은 `search_*` 메서드로 분리되어 있습니다.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .util import dumps, new_id, now_iso

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS influencers (
    influencer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    handle TEXT,
    status TEXT NOT NULL,              -- DRAFT | ACTIVE | ARCHIVED
    profile_json TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_versions (
    version_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    version_no INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    change_reason TEXT,
    changed_fields TEXT,
    status TEXT NOT NULL,              -- ACTIVE | PROPOSED | SUPERSEDED | REJECTED
    created_by TEXT,
    created_at TEXT NOT NULL,
    effective_at TEXT
);

CREATE TABLE IF NOT EXISTS world_settings (
    world_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    version_no INTEGER NOT NULL,
    settings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seasons (
    season_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    title TEXT NOT NULL,
    goal TEXT,
    total_days INTEGER,
    plan_json TEXT,
    status TEXT NOT NULL,              -- PLANNED | ACTIVE | COMPLETED
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stories (
    story_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    season_id TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    story_date TEXT,
    day_no INTEGER,
    story_type TEXT NOT NULL,          -- SERIAL | STANDALONE
    status TEXT NOT NULL,              -- DRAFT | REVIEW_REQUIRED | APPROVED | PUBLISHED | ARCHIVED | SUPERSEDED | REJECTED
    data_json TEXT NOT NULL,
    validation_json TEXT,
    revision_of TEXT,
    provider TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL REFERENCES stories(story_id),
    sequence_no INTEGER NOT NULL,
    title TEXT,
    body TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS story_events (
    event_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    story_id TEXT REFERENCES stories(story_id),
    occurred_at TEXT,
    title TEXT NOT NULL,
    description TEXT,
    event_type TEXT,                   -- PROMISE | MILESTONE | CONFLICT | DISCOVERY | MEETING | CHANGE
    status TEXT NOT NULL,              -- OPEN | RESOLVED | NONE | SUPERSEDED
    resolves_event_id TEXT,
    shared_event_id TEXT,              -- 여러 인플루언서가 함께 겪은 사건 묶음
    fact_type TEXT NOT NULL DEFAULT 'FICTION',  -- FICTION | REAL
    event_data TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    entity_type TEXT NOT NULL,         -- PERSON | PLACE | ORG | ITEM | INFLUENCER
    name TEXT NOT NULL,
    attributes_json TEXT,
    first_story_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (influencer_id, entity_type, name)
);

CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    story_id TEXT NOT NULL REFERENCES stories(story_id),
    detail_json TEXT,
    occurred_at TEXT,
    PRIMARY KEY (entity_id, story_id)
);

CREATE TABLE IF NOT EXISTS relationships (
    relationship_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    sentiment TEXT,
    since TEXT,
    story_id TEXT,
    status TEXT NOT NULL,              -- ACTIVE | ENDED | SUPERSEDED
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    memory_type TEXT NOT NULL,         -- FACT | EVENT | PLACE | PERSON | ITEM | PREFERENCE | RELATION
    content TEXT NOT NULL,
    source_story_id TEXT,
    source TEXT,                       -- story_approval | manual | analytics | shared_event
    confidence REAL NOT NULL DEFAULT 1.0,
    fact_type TEXT NOT NULL DEFAULT 'FICTION',
    status TEXT NOT NULL,              -- CANDIDATE | ACTIVE | SUPERSEDED | REJECTED
    supersedes TEXT,
    valid_from TEXT,
    tags TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS story_links (
    source_story_id TEXT NOT NULL,
    target_story_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,       -- CONTINUES | REVISES | RESOLVES | CROSSOVER
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_story_id, target_story_id, relation_type)
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    influencer_id TEXT REFERENCES influencers(influencer_id),
    story_id TEXT,
    content_id TEXT,
    job_id TEXT,
    slot TEXT,                         -- 같은 작업 안에서의 순번 (재시도 시 중복 생성 방지)
    asset_type TEXT NOT NULL,          -- IMAGE | VIDEO | AUDIO | SUBTITLE | REFERENCE | OTHER
    file_path TEXT NOT NULL,
    prompt TEXT,
    model TEXT,
    params_json TEXT,
    is_placeholder INTEGER NOT NULL DEFAULT 0,
    review_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED',
    sha256 TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contents (
    content_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL REFERENCES stories(story_id),
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    platform TEXT NOT NULL,
    content_type TEXT NOT NULL,
    language TEXT NOT NULL,
    status TEXT NOT NULL,              -- DRAFT | REVIEW_REQUIRED | APPROVED | CHANGES_REQUESTED | REJECTED | PUBLISHED
    current_version INTEGER NOT NULL DEFAULT 1,
    title TEXT,
    translated_from TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_versions (
    version_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES contents(content_id),
    version_no INTEGER NOT NULL,
    content_data TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    influencer_id TEXT,
    target_id TEXT,
    status TEXT NOT NULL,              -- QUEUED | RUNNING | REVIEW_REQUIRED | COMPLETED | FAILED | CANCELLED
    idempotency_key TEXT UNIQUE,
    params_json TEXT,
    result_json TEXT,
    error TEXT,
    progress REAL NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    publication_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES contents(content_id),
    platform TEXT NOT NULL,
    status TEXT NOT NULL,              -- PREPARED | SCHEDULED | MANUAL_REQUIRED | PUBLISHED | FAILED | CANCELLED
    scheduled_at TEXT,
    published_at TEXT,
    external_id TEXT,
    idempotency_key TEXT UNIQUE,
    package_path TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics (
    analytics_id TEXT PRIMARY KEY,
    content_id TEXT REFERENCES contents(content_id),
    influencer_id TEXT,
    platform TEXT,
    measured_at TEXT NOT NULL,
    metric_data TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,         -- STORY | CONTENT | CHARACTER_VERSION | INFLUENCER | MEMORY
    target_id TEXT NOT NULL,
    influencer_id TEXT,
    reviewer_id TEXT,
    decision TEXT NOT NULL,            -- PENDING | APPROVED | REJECTED | CHANGES_REQUESTED
    comment TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    influencer_id TEXT NOT NULL REFERENCES influencers(influencer_id),
    run_time TEXT NOT NULL,            -- HH:MM (로컬 시간)
    task_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_date TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    influencer_id TEXT NOT NULL,
    model TEXT NOT NULL,
    vector TEXT NOT NULL,
    PRIMARY KEY (target_type, target_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    target_type UNINDEXED, target_id UNINDEXED, influencer_id UNINDEXED, occurred_at UNINDEXED, text,
    tokenize = 'trigram'
);

CREATE INDEX IF NOT EXISTS ix_stories_inf ON stories(influencer_id, status, story_date);
CREATE INDEX IF NOT EXISTS ix_events_inf ON story_events(influencer_id, status, occurred_at);
CREATE INDEX IF NOT EXISTS ix_entities_inf ON entities(influencer_id, entity_type);
CREATE INDEX IF NOT EXISTS ix_memories_inf ON memories(influencer_id, status);
CREATE INDEX IF NOT EXISTS ix_contents_story ON contents(story_id);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_approvals_pending ON approvals(decision, target_type);
CREATE INDEX IF NOT EXISTS ix_assets_job ON assets(job_id, slot);
"""

JSON_SUFFIXES = ("_json", "content_data", "metric_data", "event_data", "tags", "changed_fields")


def _decode_row(row: sqlite3.Row) -> dict:
    from .util import loads

    out = {}
    for key in row.keys():
        value = row[key]
        if isinstance(value, str) and key.endswith(JSON_SUFFIXES):
            out[key] = loads(value, value)
        else:
            out[key] = value
    return out


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._tx_depth = 0
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 트랜잭션 -------------------------------------------------------
    class _Tx:
        def __init__(self, store: "Store"):
            self.store = store

        def __enter__(self):
            self.store._lock.acquire()
            if self.store._tx_depth == 0:
                self.store._conn.execute("BEGIN IMMEDIATE")
            self.store._tx_depth += 1
            return self.store

        def __exit__(self, exc_type, exc, tb):
            try:
                self.store._tx_depth -= 1
                if self.store._tx_depth == 0:
                    self.store._conn.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.store._lock.release()
            return False

    def tx(self) -> "Store._Tx":
        return Store._Tx(self)

    # ---- 일반 메서드 ----------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.rowcount

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_decode_row(r) for r in rows]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, values: dict, or_ignore: bool = False) -> dict:
        cols = list(values.keys())
        data = [dumps(v) if isinstance(v, (dict, list)) else v for v in values.values()]
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        self.execute(sql, data)
        return values

    def update(self, table: str, key_col: str, key: Any, values: dict) -> int:
        cols = list(values.keys())
        data = [dumps(v) if isinstance(v, (dict, list)) else v for v in values.values()]
        sql = f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} WHERE {key_col} = ?"
        return self.execute(sql, [*data, key])

    def get(self, table: str, key_col: str, key: Any) -> dict | None:
        return self.one(f"SELECT * FROM {table} WHERE {key_col} = ?", (key,))

    # ---- 감사 로그 ------------------------------------------------------
    def audit(self, actor: str, action: str, target_type: str | None = None, target_id: str | None = None,
              detail: dict | None = None) -> None:
        self.insert("audit_logs", {
            "log_id": new_id("log"), "actor_id": actor, "action": action, "target_type": target_type,
            "target_id": target_id, "detail_json": detail or {}, "created_at": now_iso(),
        })

    # ---- 검색 인덱스 ----------------------------------------------------
    def search_index_put(self, target_type: str, target_id: str, influencer_id: str, text: str,
                         occurred_at: str | None) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM search_fts WHERE target_type = ? AND target_id = ?",
                               (target_type, target_id))
            self._conn.execute(
                "INSERT INTO search_fts (target_type, target_id, influencer_id, occurred_at, text) VALUES (?,?,?,?,?)",
                (target_type, target_id, influencer_id, occurred_at, text),
            )

    def search_index_delete(self, target_type: str, target_id: str) -> None:
        self.execute("DELETE FROM search_fts WHERE target_type = ? AND target_id = ?", (target_type, target_id))
        self.execute("DELETE FROM embeddings WHERE target_type = ? AND target_id = ?", (target_type, target_id))

    def search_text(self, influencer_id: str, terms: list[str], target_types: list[str] | None,
                    limit: int = 200) -> list[dict]:
        """키워드 검색. 3자 이상은 FTS5(trigram), 그보다 짧은 단어(예: 2자 이름)는 LIKE 로 찾습니다."""
        if not terms:
            return []
        type_sql = ""
        params: list[Any] = [influencer_id]
        if target_types:
            type_sql = f" AND target_type IN ({', '.join('?' for _ in target_types)})"
            params += target_types
        clauses = []
        cparams: list[Any] = []
        for t in terms:
            if len(t) >= 3:
                clauses.append("text MATCH ?")
                cparams.append('"' + t.replace('"', '""') + '"')
            else:
                clauses.append("text LIKE ?")
                cparams.append(f"%{t}%")
        sql = (
            "SELECT target_type, target_id, occurred_at, text FROM search_fts "
            f"WHERE influencer_id = ?{type_sql} AND ({' OR '.join(clauses)}) LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (*params, *cparams, limit)).fetchall()
        return [dict(r) for r in rows]

    def embedding_put(self, target_type: str, target_id: str, influencer_id: str, model: str,
                      vector: list[float]) -> None:
        self.execute(
            "INSERT OR REPLACE INTO embeddings (target_type, target_id, influencer_id, model, vector) VALUES (?,?,?,?,?)",
            (target_type, target_id, influencer_id, model, dumps([round(v, 5) for v in vector])),
        )

    def embeddings_for(self, influencer_id: str, target_types: list[str] | None) -> list[dict]:
        sql = "SELECT target_type, target_id, model, vector FROM embeddings WHERE influencer_id = ?"
        params: list[Any] = [influencer_id]
        if target_types:
            sql += f" AND target_type IN ({', '.join('?' for _ in target_types)})"
            params += target_types
        return self.query(sql, params)

    def backup_to(self, dest: Path) -> None:
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()
