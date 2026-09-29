"""Durable SQLite storage for the reading library, settings, and request history."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 6

# Extra browser origins allowed to reach /ask and the JSON APIs, beyond the
# built-in localhost set. The Obsidian plugin's renderer origin is the default.
MAX_ALLOWED_ORIGINS = 16
_ORIGIN_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[a-z0-9.-]+(:\d{1,5})?$", re.IGNORECASE)

DEFAULT_SETTINGS: dict[str, Any] = {
    "provider": "claude",
    "model": "sonnet",
    "claude_model": "sonnet",
    "codex_model": "gpt-5.6-sol",
    "claude_effort": "medium",
    "codex_effort": "low",
    "response_style": "concise",
    # Answers may search and read the web to check an outside fact; off is faster.
    "web_lookups": True,
    "cache_ttl_hours": 168,
    "cache_max_entries": 100,
    "history_enabled": True,
    "allow_private_remote": False,
    "first_activity_timeout": 35,
    "request_timeout": 120,
    "glass_transparency": 38,
    "appearance_theme": "system",
    "markdown_follow_obsidian": True,
    "sidebar_follow_obsidian": True,
    "html_follow_page": True,
    # Browsed read-only in Vault mode; "" disables the vault view.
    "vault_root": str(Path.home() / "Documents" / "CX"),
    # Artifacts: a folder of symlinks to HTML pages anywhere on disk; "" hides it.
    # The key (like kind="html") predates the name: it was "HTML Vault" until 2026-09-11.
    "html_vault_root": str(Path.home() / "Documents" / "Artifacts"),
    "allowed_origins": ["app://obsidian.md"],
    # The Setup section opens by itself on launch until every step passes or this is set.
    "setup_dismissed": False,
}


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Onyx"


def legacy_database() -> Path:
    """Where the database lived while the app was called Ask Widget."""
    return Path.home() / "Library" / "Application Support" / "Ask Widget" / "ask-widget.db"


def adopt_legacy_database(legacy: Path, target: Path) -> bool:
    """Copy the pre-rename database into place once; the original is left as-is.

    SQLite's backup API takes a consistent snapshot even while an older server
    still has the legacy file open, which a plain file copy of a WAL database
    does not. The copy lands under a temporary name and is renamed into place,
    so an interrupted copy is retried on the next start instead of being opened
    as a half-written database.
    """
    if target.exists() or not legacy.is_file():
        return False
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(legacy)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    partial.replace(target)
    return True


def selection_hash(selection: str) -> str:
    return hashlib.sha256(selection.strip().encode("utf-8")).hexdigest()[:24]


class Storage:
    """Small thread-safe SQLite repository.

    The FastAPI app has a single process but can finish streams and diagnostics
    on worker threads. A lock keeps the one connection predictable; WAL keeps
    reads cheap and makes the database inspectable while the app is running.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or default_data_dir()).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "onyx.db"
        if data_dir is None:
            adopt_legacy_database(legacy_database(), self.path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=3000")
            self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS allowed_roots (
                path TEXT PRIMARY KEY,
                builtin INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS markdown_themes (
                vault_root TEXT PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sidebar_themes (
                vault_root TEXT PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                folder TEXT,
                page_count INTEGER,
                scroll_y REAL NOT NULL DEFAULT 0,
                last_opened_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                request_id TEXT PRIMARY KEY,
                document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                document_source TEXT,
                document_title TEXT,
                document_page INTEGER,
                selection TEXT NOT NULL,
                selection_hash TEXT NOT NULL,
                context TEXT,
                action TEXT NOT NULL,
                question TEXT,
                answer TEXT,
                folder TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'claude',
                model TEXT NOT NULL,
                effort TEXT NOT NULL DEFAULT 'medium',
                request_mode TEXT NOT NULL DEFAULT 'generated',
                parent_request_id TEXT,
                status TEXT NOT NULL,
                error TEXT,
                citations_json TEXT NOT NULL DEFAULT '[]',
                trace_json TEXT NOT NULL DEFAULT '[]',
                started_at REAL NOT NULL,
                completed_at REAL,
                latency_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_documents_recent
                ON documents(last_opened_at DESC);
            CREATE INDEX IF NOT EXISTS idx_conversations_recent
                ON conversations(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_conversations_document
                ON conversations(document_source, selection_hash, started_at DESC);
            CREATE TABLE IF NOT EXISTS highlights (
                id TEXT PRIMARY KEY,
                document_source TEXT NOT NULL,
                selection TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '',
                prefix TEXT NOT NULL DEFAULT '',
                suffix TEXT NOT NULL DEFAULT '',
                document_page INTEGER,
                note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_highlights_document
                ON highlights(document_source, created_at DESC);
            """
        )
        columns = {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(conversations)").fetchall()
        }
        if "provider" not in columns:
            self._db.execute(
                "ALTER TABLE conversations ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude'"
            )
        if "effort" not in columns:
            self._db.execute(
                "ALTER TABLE conversations ADD COLUMN effort TEXT NOT NULL DEFAULT 'medium'"
            )
        if "request_mode" not in columns:
            self._db.execute(
                "ALTER TABLE conversations ADD COLUMN request_mode TEXT NOT NULL DEFAULT 'generated'"
            )
        if "parent_request_id" not in columns:
            self._db.execute("ALTER TABLE conversations ADD COLUMN parent_request_id TEXT")
        self._db.execute(
            "INSERT INTO app_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._db.commit()

    def stored_setting_keys(self) -> set[str]:
        """The settings someone has set, as opposed to ones still on their shipped default."""
        with self._lock:
            return {row["key"] for row in self._db.execute("SELECT key FROM settings").fetchall()}

    def settings(self, *, model_default: str = "sonnet") -> dict[str, Any]:
        values = dict(DEFAULT_SETTINGS)
        values["model"] = model_default
        values["claude_model"] = model_default
        stored: dict[str, Any] = {}
        with self._lock:
            rows = self._db.execute("SELECT key, value_json FROM settings").fetchall()
        for row in rows:
            try:
                stored[row["key"]] = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError):
                continue
        values.update(stored)
        if "claude_model" not in stored and isinstance(stored.get("model"), str):
            values["claude_model"] = stored["model"]
        origins = values.get("allowed_origins")
        if not isinstance(origins, list) or not all(isinstance(item, str) for item in origins):
            values["allowed_origins"] = list(DEFAULT_SETTINGS["allowed_origins"])
        provider = values.get("provider") if values.get("provider") in {"claude", "codex"} else "claude"
        values["provider"] = provider
        values["model"] = values[f"{provider}_model"]
        values["reasoning_effort"] = values[f"{provider}_effort"]
        return values

    def update_settings(self, patch: dict[str, Any], *, model_default: str) -> dict[str, Any]:
        current = self.settings(model_default=model_default)
        clean: dict[str, Any] = {}
        selected_provider = str(patch.get("provider", current["provider"]))
        if selected_provider not in {"claude", "codex"}:
            raise ValueError("Provider must be claude or codex.")
        if "provider" in patch:
            clean["provider"] = selected_provider
        for key in ("claude_model", "codex_model"):
            if key in patch:
                value = str(patch[key]).strip()
                if not value or len(value) > 100 or not all(
                    character.isalnum() or character in "._:-" for character in value
                ):
                    raise ValueError("Model names may contain letters, numbers, dots, colons, underscores, and hyphens.")
                clean[key] = value
        if "model" in patch:  # v0.3 compatibility
            value = str(patch["model"]).strip()
            if not value or len(value) > 100:
                raise ValueError("Model must be between 1 and 100 characters.")
            clean[f"{selected_provider}_model"] = value
        for key in ("claude_effort", "codex_effort"):
            if key in patch:
                value = str(patch[key])
                if value not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
                    raise ValueError("Unknown reasoning effort.")
                clean[key] = value
        if "response_style" in patch:
            value = str(patch["response_style"])
            if value not in {"concise", "balanced", "detailed"}:
                raise ValueError("Unknown response style.")
            clean["response_style"] = value
        if "appearance_theme" in patch:
            value = str(patch["appearance_theme"])
            if value not in {"system", "light", "dark"}:
                raise ValueError("Appearance theme must be system, light, or dark.")
            clean["appearance_theme"] = value
        for key, lower, upper in (
            ("cache_ttl_hours", 0, 24 * 365),
            ("cache_max_entries", 0, 1000),
            ("first_activity_timeout", 10, 120),
            ("request_timeout", 30, 600),
            ("glass_transparency", 0, 100),
        ):
            if key in patch:
                try:
                    value = int(patch[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be a number.") from exc
                if value < lower or value > upper:
                    raise ValueError(f"{key} must be between {lower} and {upper}.")
                clean[key] = value
        for key in (
            "history_enabled",
            "allow_private_remote",
            "markdown_follow_obsidian",
            "sidebar_follow_obsidian",
            "html_follow_page",
            "web_lookups",
            "setup_dismissed",
        ):
            if key in patch:
                clean[key] = bool(patch[key])
        if "allowed_origins" in patch:
            raw = patch["allowed_origins"]
            if not isinstance(raw, list):
                raise ValueError("Allowed origins must be a list.")
            if len(raw) > MAX_ALLOWED_ORIGINS:
                raise ValueError(f"At most {MAX_ALLOWED_ORIGINS} allowed origins.")
            origins: list[str] = []
            for item in raw:
                origin = str(item).strip().rstrip("/")
                if not origin:
                    continue
                if "*" in origin or not _ORIGIN_RE.match(origin):
                    raise ValueError(f"{origin!r} is not a scheme://host[:port] origin.")
                if origin not in origins:
                    origins.append(origin)
            clean["allowed_origins"] = origins
        for key, label in (("vault_root", "Vault"), ("html_vault_root", "Artifacts")):
            if key not in patch:
                continue
            value = str(patch[key] or "").strip()
            if value:
                candidate = Path(value).expanduser()
                if not candidate.is_absolute() or not candidate.is_dir():
                    raise ValueError(f"{label} folder does not exist.")
                # Lexical normalization only: the vault may contain symlinked
                # folders, and every containment check compares against the
                # path the user sees, never a resolved realpath.
                value = os.path.normpath(str(candidate))
            clean[key] = value

        now = time.time()
        with self._lock:
            for key, value in clean.items():
                self._db.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                    "updated_at=excluded.updated_at",
                    (key, json.dumps(value), now),
                )
            self._db.commit()
        return self.settings(model_default=model_default)

    # Obsidian appearance snapshots, one per vault and kind: the reading view
    # (markdown_themes) and the file explorer (sidebar_themes).
    def _save_theme(self, table: str, root: Path, snapshot: dict[str, Any]) -> None:
        encoded = json.dumps(snapshot, sort_keys=True)
        with self._lock:
            self._db.execute(
                f"INSERT INTO {table}(vault_root, snapshot_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(vault_root) DO UPDATE SET snapshot_json=excluded.snapshot_json, "
                "updated_at=excluded.updated_at WHERE snapshot_json != excluded.snapshot_json",
                (str(root.resolve()), encoded, time.time()),
            )
            self._db.commit()

    def _theme(self, table: str, root: Path) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT snapshot_json FROM {table} WHERE vault_root=?", (str(root.resolve()),)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_markdown_theme(self, root: Path, snapshot: dict[str, Any]) -> None:
        self._save_theme("markdown_themes", root, snapshot)

    def markdown_theme(self, root: Path) -> dict[str, Any] | None:
        return self._theme("markdown_themes", root)

    def save_sidebar_theme(self, root: Path, snapshot: dict[str, Any]) -> None:
        self._save_theme("sidebar_themes", root, snapshot)

    def sidebar_theme(self, root: Path) -> dict[str, Any] | None:
        return self._theme("sidebar_themes", root)

    def sync_builtin_roots(self, roots: tuple[Path, ...]) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("UPDATE allowed_roots SET builtin=0 WHERE builtin=1")
            for root in roots:
                self._db.execute(
                    "INSERT INTO allowed_roots(path, builtin, created_at) VALUES(?, 1, ?) "
                    "ON CONFLICT(path) DO UPDATE SET builtin=1",
                    (str(root.resolve()), now),
                )
            self._db.commit()

    def roots(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT path, builtin, created_at FROM allowed_roots ORDER BY builtin DESC, path"
            ).fetchall()
        return [
            {"path": row["path"], "builtin": bool(row["builtin"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def add_root(self, path: Path) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO allowed_roots(path, builtin, created_at) VALUES(?, 0, ?) "
                "ON CONFLICT(path) DO NOTHING",
                (str(path.resolve()), time.time()),
            )
            self._db.commit()

    def remove_root(self, path: Path) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM allowed_roots WHERE path=? AND builtin=0", (str(path.resolve()),)
            )
            self._db.commit()
        return cursor.rowcount > 0

    def upsert_document(
        self,
        *,
        source: str,
        title: str,
        kind: str,
        folder: str | None,
        page_count: int | None = None,
    ) -> int:
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO documents(source, title, kind, folder, page_count, last_opened_at, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    title=excluded.title,
                    kind=excluded.kind,
                    folder=COALESCE(excluded.folder, documents.folder),
                    page_count=COALESCE(excluded.page_count, documents.page_count),
                    last_opened_at=excluded.last_opened_at
                """,
                (source, title or Path(source).name or source, kind, folder, page_count, now, now),
            )
            row = self._db.execute("SELECT id FROM documents WHERE source=?", (source,)).fetchone()
            self._db.commit()
        assert row is not None
        return int(row["id"])

    def update_position(self, source: str, scroll_y: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE documents SET scroll_y=?, last_opened_at=? WHERE source=?",
                (max(0.0, float(scroll_y)), time.time(), source),
            )
            self._db.commit()

    def recent_documents(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT d.*,
                    (SELECT COUNT(*) FROM conversations c
                     WHERE c.document_source=d.source AND c.status='complete') AS conversation_count
                FROM documents d
                WHERE d.kind != 'selection'
                ORDER BY d.last_opened_at DESC LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def document(self, source: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM documents WHERE source=?", (source,)).fetchone()
        return dict(row) if row is not None else None

    def start_conversation(
        self,
        *,
        request_id: str,
        document_id: int | None,
        document_source: str | None,
        document_title: str | None,
        document_page: int | None,
        selection: str,
        context: str,
        action: str,
        question: str,
        folder: str,
        provider: str,
        model: str,
        effort: str = "medium",
        request_mode: str = "generated",
        parent_request_id: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO conversations(
                    request_id, document_id, document_source, document_title, document_page,
                    selection, selection_hash, context, action, question, folder, provider, model,
                    effort, request_mode, parent_request_id, status, started_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    request_id,
                    document_id,
                    document_source,
                    document_title,
                    document_page,
                    selection,
                    selection_hash(selection),
                    context,
                    action,
                    question,
                    folder,
                    provider,
                    model,
                    effort,
                    request_mode,
                    parent_request_id,
                    time.time(),
                ),
            )
            self._db.commit()

    def finish_conversation(
        self,
        request_id: str,
        *,
        status: str,
        answer: str = "",
        error: str = "",
        citations: list[dict[str, Any]] | None = None,
        trace: list[dict[str, Any]] | None = None,
        latency_ms: int | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                UPDATE conversations SET status=?, answer=?, error=?, citations_json=?,
                    trace_json=?, completed_at=?, latency_ms=? WHERE request_id=?
                """,
                (
                    status,
                    answer,
                    error,
                    json.dumps(citations or []),
                    json.dumps(trace or []),
                    time.time(),
                    latency_ms,
                    request_id,
                ),
            )
            self._db.commit()

    @staticmethod
    def _conversation(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("citations_json", "trace_json"):
            target = key.removesuffix("_json")
            try:
                item[target] = json.loads(item.pop(key) or "[]")
            except json.JSONDecodeError:
                item[target] = []
                item.pop(key, None)
        return item

    def conversation(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._conversation(row) if row is not None else None

    def highlights(self, source: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM highlights WHERE document_source=? ORDER BY created_at DESC", (source,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_highlight(
        self, *, id: str, source: str, selection: str, context: str,
        prefix: str, suffix: str, page: int | None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO highlights(id, document_source, selection, context, prefix, suffix, "
                "document_page, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (id, source, selection, context, prefix, suffix, page, now, now),
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM highlights WHERE id=?", (id,)).fetchone()
        return dict(row)

    def update_highlight_note(self, id: str, note: str) -> dict[str, Any] | None:
        with self._lock:
            cursor = self._db.execute(
                "UPDATE highlights SET note=?, updated_at=? WHERE id=?", (note, time.time(), id)
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM highlights WHERE id=?", (id,)).fetchone() if cursor.rowcount else None
        return dict(row) if row is not None else None

    def delete_highlight(self, id: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM highlights WHERE id=?", (id,))
            self._db.commit()
        return bool(cursor.rowcount)

    def recent_conversations(
        self,
        *,
        limit: int = 50,
        source: str | None = None,
        selection: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        action: str | None = None,
        since: float | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["status IN ('complete', 'error', 'cancelled')"]
        params: list[Any] = []
        if source:
            clauses.append("document_source=?")
            params.append(source)
        if selection is not None:
            clauses.append("selection_hash=?")
            params.append(selection_hash(selection))
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if model:
            clauses.append("model=?")
            params.append(model)
        if action:
            clauses.append("action=?")
            params.append(action)
        if since is not None:
            clauses.append("started_at>=?")
            params.append(float(since))
        if query and query.strip():
            needle = f"%{query.strip()}%"
            clauses.append("(selection LIKE ? OR question LIKE ? OR answer LIKE ? OR document_title LIKE ?)")
            params.extend((needle, needle, needle, needle))
        params.append(max(1, min(limit, 200)))
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM conversations WHERE " + " AND ".join(clauses)
                + " ORDER BY started_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def history_facets(self) -> dict[str, list[Any]]:
        with self._lock:
            providers = [
                row[0]
                for row in self._db.execute(
                    "SELECT DISTINCT provider FROM conversations WHERE provider!='' ORDER BY provider"
                ).fetchall()
            ]
            models = [
                row[0]
                for row in self._db.execute(
                    "SELECT DISTINCT model FROM conversations WHERE model!='' ORDER BY model"
                ).fetchall()
            ]
            documents = [
                {"source": row[0], "title": row[1] or row[0]}
                for row in self._db.execute(
                    "SELECT document_source, MAX(document_title) FROM conversations "
                    "WHERE document_source IS NOT NULL AND document_source!='' "
                    "GROUP BY document_source ORDER BY MAX(started_at) DESC LIMIT 200"
                ).fetchall()
            ]
        return {"providers": providers, "models": models, "documents": documents}

    def search(
        self,
        query: str,
        limit: int = 50,
        *,
        provider: str | None = None,
        model: str | None = None,
        source: str | None = None,
        action: str | None = None,
        since: float | None = None,
    ) -> dict[str, Any]:
        needle = f"%{query.strip()}%"
        conversations = self.recent_conversations(
            limit=limit,
            source=source,
            provider=provider,
            model=model,
            action=action,
            since=since,
            query=query,
        )
        if not query.strip():
            return {
                "documents": self.recent_documents(limit=min(limit, 20)),
                "conversations": conversations,
                "facets": self.history_facets(),
            }
        with self._lock:
            documents = self._db.execute(
                "SELECT * FROM documents WHERE kind != 'selection' AND (title LIKE ? OR source LIKE ?) "
                "ORDER BY last_opened_at DESC LIMIT ?",
                (needle, needle, limit),
            ).fetchall()
        return {
            "documents": [dict(row) for row in documents],
            "conversations": conversations,
            "facets": self.history_facets(),
        }

    def export_markdown(self, source: str) -> str:
        with self._lock:
            document = self._db.execute("SELECT * FROM documents WHERE source=?", (source,)).fetchone()
        if document is None:
            raise KeyError(source)
        conversations = list(reversed(self.recent_conversations(limit=500, source=source)))
        lines = [f"# {document['title']}", "", f"Source: `{source}`", ""]
        for item in conversations:
            lines += [
                f"## {item['action'].upper()} - {item.get('question') or 'Selected passage'}",
                "",
                "> " + item["selection"].replace("\n", "\n> "),
                "",
                item.get("answer") or f"_Request {item['status']}: {item.get('error') or 'no answer'}_",
                "",
            ]
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            documents = self._db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind != 'selection'"
            ).fetchone()[0]
            conversations = self._db.execute(
                "SELECT COUNT(*) FROM conversations WHERE status='complete'"
            ).fetchone()[0]
        return {
            "schema_version": SCHEMA_VERSION,
            "database": str(self.path),
            "documents": documents,
            "conversations": conversations,
        }
