import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from wings_x.parse import parse_date, post_url

SCHEMA_VERSION = 1

_UNAVAILABLE = ("deleted", "protected")

# Columns and tokenizer of bookmark_fts; bookmark search parses queries against an empty copy.
BOOKMARK_FTS_ARGS = "post_id UNINDEXED, text, author, tokenize = 'unicode61 remove_diacritics 2'"


def _json_or_none(value):
    if value is None:
        return None
    return json.dumps(value)


def _load_media(text):
    """A stored media_json as a list of items; anything unreadable counts as no media."""
    if text is None:
        return []
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return []
    if not isinstance(value, list):
        return []
    return value


def _media_keys(items):
    if not isinstance(items, list):
        return []
    return [item.get("key") if isinstance(item, dict) else None for item in items]


def _alt_texts(items):
    if not isinstance(items, list):
        return []
    return [
        item["alt_text"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("alt_text"), str) and item["alt_text"]
    ]


def _is_resolved_or_unavailable(item):
    return isinstance(item, dict) and list(item) != ["key"]


class Store:
    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(path):
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(path, 0o600)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=True)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            os.chmod(path, 0o600)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        conn = getattr(self, "conn", None)
        self.conn = None
        if conn is not None:
            conn.close()

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def transaction(self, immediate: bool = False):
        if self.conn.in_transaction:
            raise RuntimeError("transaction already open")
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def _write(self, work, immediate=False):
        if self.conn.in_transaction:
            work()
            return
        with self.transaction(immediate=immediate):
            work()

    def _migrate(self):
        while self.schema_version() < SCHEMA_VERSION:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                version = self.schema_version()
                if version < SCHEMA_VERSION:
                    nxt = version + 1
                    if nxt == 1:
                        self._migration_1()
                    else:
                        raise RuntimeError("no migration %s" % nxt)
                    self.conn.execute("PRAGMA user_version = %d" % nxt)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _migration_1(self):
        self.conn.execute(
            """
            CREATE TABLE posts (
                id TEXT PRIMARY KEY,
                url TEXT,
                text TEXT,
                author_id TEXT,
                created_at TEXT,
                conversation_id TEXT,
                lang TEXT,
                metrics_json TEXT,
                media_json TEXT,
                referenced_json TEXT,
                fetched_at TEXT,
                unavailable TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                fetched_at TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE bookmarks (
                post_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL,
                first_seen_at TEXT,
                last_seen_at TEXT,
                state TEXT NOT NULL CHECK (state IN ('active','removed','beyond_window')),
                state_at TEXT
            )
            """
        )
        self.conn.execute("CREATE INDEX bookmarks_seq ON bookmarks(seq)")
        self.conn.execute(
            """
            CREATE TABLE bookmark_folders (
                id TEXT PRIMARY KEY,
                name TEXT,
                fetched_at TEXT,
                members_synced_at TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE bookmark_folder_items (
                folder_id TEXT NOT NULL,
                post_id TEXT NOT NULL,
                PRIMARY KEY (folder_id, post_id)
            )
            """
        )
        self.conn.execute("CREATE VIRTUAL TABLE bookmark_fts USING fts5(" + BOOKMARK_FTS_ARGS + ")")
        self.conn.execute(
            """
            CREATE TABLE ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT,
                pid INTEGER,
                at TEXT,
                command TEXT,
                endpoint TEXT,
                state TEXT NOT NULL CHECK (state IN ('reserved','settled','abandoned')),
                reserved_usd REAL NOT NULL DEFAULT 0,
                resources INTEGER,
                est_usd REAL NOT NULL DEFAULT 0,
                duration_ms INTEGER,
                result_count INTEGER,
                x_request_id TEXT,
                http_status INTEGER,
                error_code TEXT
            )
            """
        )
        self.conn.execute("CREATE INDEX ledger_at ON ledger(at)")
        self.conn.execute(
            """
            CREATE TABLE charged (
                utc_day TEXT NOT NULL,
                kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                PRIMARY KEY (utc_day, kind, resource_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE sync_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

    def upsert_posts(self, posts: list, fetched_at: str) -> None:
        # Reads each stored media list before writing, so it takes the write lock first (spec A1.2 "Storage").
        def work():
            ids = []
            for post in posts:
                author = post.get("author") or {}
                incoming = post.get("media")
                media_json = _json_or_none(incoming)
                row = self.conn.execute("SELECT media_json FROM posts WHERE id = ?", (post["id"],)).fetchone()
                if row is not None and _media_keys(_load_media(row["media_json"])) == _media_keys(incoming or []):
                    # Same keys in the same order: keep whatever detail is already stored.
                    media_json = row["media_json"]
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO posts (
                        id, url, text, author_id, created_at, conversation_id, lang,
                        metrics_json, media_json, referenced_json, fetched_at, unavailable
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        post["id"],
                        post.get("url"),
                        post.get("text"),
                        author.get("id"),
                        post.get("created_at"),
                        post.get("conversation_id"),
                        post.get("lang"),
                        _json_or_none(post.get("metrics")),
                        media_json,
                        _json_or_none(post.get("referenced")),
                        fetched_at,
                    ),
                )
                ids.append(post["id"])
            self.refresh_fts(ids)

        self._write(work, immediate=True)

    def merge_media(self, returned, detail) -> list:
        """Write looked-up media into the stored lists by key. Returns the IDs whose keys differed."""
        changed = []
        refresh = []
        with self.transaction(immediate=True):
            for post_id, keys in returned.items():
                # Re-read under the write lock, never the list the caller held before the request.
                row = self.conn.execute("SELECT media_json FROM posts WHERE id = ?", (post_id,)).fetchone()
                if row is None:
                    continue
                current = _load_media(row["media_json"])
                keys = list(keys)
                if _media_keys(current) == keys:
                    merged = []
                    for item in current:
                        key = item.get("key") if isinstance(item, dict) else None
                        if key in detail:
                            merged.append(dict(detail[key]))
                        elif _is_resolved_or_unavailable(item):
                            # Resolved or already unavailable: an omission keeps the stored item (decision 23).
                            merged.append(item)
                        else:
                            merged.append({"key": key, "unavailable": True})
                else:
                    changed.append(post_id)
                    merged = [dict(detail[key]) if key in detail else {"key": key, "unavailable": True} for key in keys]
                self.conn.execute(
                    "UPDATE posts SET media_json = ? WHERE id = ?",
                    (_json_or_none(merged) if merged else None, post_id),
                )
                if _alt_texts(current) != _alt_texts(merged):
                    refresh.append(post_id)
            self.refresh_fts(refresh)
        return changed

    def reset_unavailable_media(self, post_ids: list) -> None:
        """Turn unavailable media items of the given posts back into pending ones."""
        def work():
            for post_id in post_ids:
                row = self.conn.execute("SELECT media_json FROM posts WHERE id = ?", (post_id,)).fetchone()
                if row is None:
                    continue
                current = _load_media(row["media_json"])
                if not any(isinstance(item, dict) and item.get("unavailable") is True for item in current):
                    continue
                reset = [
                    {"key": item.get("key")} if isinstance(item, dict) and item.get("unavailable") is True else item
                    for item in current
                ]
                self.conn.execute("UPDATE posts SET media_json = ? WHERE id = ?", (_json_or_none(reset), post_id))

        self._write(work, immediate=True)

    def get_post(self, post_id: str, origin: str = "live"):
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None:
            return None
        author = None
        username = None
        if row["author_id"]:
            cached = self.get_users([row["author_id"]]).get(row["author_id"])
            if cached is None:
                author = {"id": row["author_id"]}
            else:
                author = cached
                username = cached.get("username") or None
        item = {
            "id": row["id"],
            "url": post_url(row["id"], username),
            "origin": origin,
            "source": "x",
            "unavailable": row["unavailable"],
        }
        if row["text"] is not None:
            item["text"] = row["text"]
        if author is not None:
            item["author"] = author
        if row["created_at"] is not None:
            item["created_at"] = row["created_at"]
        if row["conversation_id"] is not None:
            item["conversation_id"] = row["conversation_id"]
        if row["lang"] is not None:
            item["lang"] = row["lang"]
        if row["metrics_json"] is not None:
            item["metrics"] = json.loads(row["metrics_json"])
        if row["media_json"] is not None:
            item["media"] = json.loads(row["media_json"])
        if row["referenced_json"] is not None:
            item["referenced"] = json.loads(row["referenced_json"])
        return item

    def mark_unavailable(self, post_id: str, kind: str) -> None:
        if kind not in _UNAVAILABLE:
            raise ValueError(kind)
        self.conn.execute(
            "UPDATE posts SET unavailable = ? WHERE id = ?",
            (kind, post_id),
        )

    def upsert_users(self, users: list, fetched_at: str) -> None:
        def work():
            ids = []
            for user in users:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO users (id, username, display_name, fetched_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user["id"], user.get("username"), user.get("display_name"), fetched_at),
                )
                ids.append(user["id"])
            if not ids:
                return
            placeholders = ",".join("?" for _ in ids)
            rows = self.conn.execute(
                "SELECT bookmarks.post_id FROM bookmarks "
                "JOIN posts ON posts.id = bookmarks.post_id "
                "WHERE posts.author_id IN (" + placeholders + ")",
                ids,
            ).fetchall()
            self.refresh_fts([row["post_id"] for row in rows])

        self._write(work)

    def get_users(self, ids: list) -> dict:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            "SELECT id, username, display_name FROM users WHERE id IN (" + placeholders + ")",
            ids,
        ).fetchall()
        found = {}
        for row in rows:
            found[row["id"]] = {
                "id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"],
            }
        return found

    def users_needing_lookup(self, ids: list, now: datetime, max_age_days: int = 30) -> list:
        cutoff = now - timedelta(days=max_age_days)
        seen = set()
        needed = []
        for user_id in ids:
            if user_id in seen:
                continue
            seen.add(user_id)
            row = self.conn.execute(
                "SELECT fetched_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None or row["fetched_at"] is None:
                needed.append(user_id)
                continue
            fetched = parse_date(row["fetched_at"])
            if fetched < cutoff:
                needed.append(user_id)
        return needed

    def get_state(self, key: str):
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return row["value"]

    def set_state(self, key: str, value: str) -> None:
        def work():
            self.conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                (key, value),
            )

        self._write(work)

    def refresh_fts(self, post_ids: list) -> None:
        def work():
            for post_id in post_ids:
                bookmark = self.conn.execute(
                    "SELECT 1 FROM bookmarks WHERE post_id = ?",
                    (post_id,),
                ).fetchone()
                if bookmark is None:
                    continue
                self.conn.execute(
                    "DELETE FROM bookmark_fts WHERE post_id = ?",
                    (post_id,),
                )
                post = self.conn.execute(
                    "SELECT text, author_id, media_json FROM posts WHERE id = ?",
                    (post_id,),
                ).fetchone()
                text = ""
                author = ""
                if post is not None:
                    # The post's text, then each attachment's alt text, one per line (spec §3, A1.2).
                    text = "\n".join([post["text"] or ""] + _alt_texts(_load_media(post["media_json"])))
                    if post["author_id"]:
                        user = self.conn.execute(
                            "SELECT username, display_name FROM users WHERE id = ?",
                            (post["author_id"],),
                        ).fetchone()
                        if user is not None:
                            username = user["username"] or ""
                            display_name = user["display_name"] or ""
                            author = (username + " " + display_name).strip()
                self.conn.execute(
                    "INSERT INTO bookmark_fts (post_id, text, author) VALUES (?, ?, ?)",
                    (post_id, text, author),
                )

        self._write(work)
