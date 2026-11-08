import fcntl
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from typing import Mapping

from fav import commands, media
from fav.errors import XError, local_io_error
from fav.parse import iso_z, post_url
from fav.store import BOOKMARK_FTS_ARGS, Store
from fav.users import resolve_authors

STOP_RUN = 10
WINDOW = 800
PAGE_SIZE = 100
BOOKMARK_PAGES_MAX = 8
HYDRATE_BATCH = 100

SYNC_COMMAND = "bookmarks sync"
FOLDERS_COMMAND = "bookmarks folders"

# IDs that go into a URL path or a comma-separated ids= list.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")
_USER_ID = re.compile(r"[0-9]+")

_ROW_MAX = 2 ** 62


def _bad(message):
    raise XError("X_BAD_INPUT", message)


def _count(value, low, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < low or value > _ROW_MAX:
        _bad(label + " must be a whole number of at least " + str(low))
    return value


def _unique(ids):
    seen = set()
    out = []
    for post_id in ids:
        if post_id not in seen:
            seen.add(post_id)
            out.append(post_id)
    return out


def find_stop(ids: list[str], active_before: set[str], run: int = STOP_RUN) -> int | None:
    streak = 0
    for i, post_id in enumerate(ids):
        if post_id in active_before:
            streak += 1
            if streak == run:
                return i - run + 1
        else:
            streak = 0
    return None


def _mark_unavailable(store, unavailable):
    for post_id, kind in (unavailable or {}).items():
        if isinstance(post_id, str) and kind in ("deleted", "protected"):
            store.mark_unavailable(post_id, kind)


def _rows(store):
    return dict((row["post_id"], row) for row in store.conn.execute("SELECT post_id, seq, state FROM bookmarks"))


def _place(store, rows, post_id, seq, now):
    """Put one bookmark at seq as active; a new row gets first_seen_at, a state change gets state_at."""
    row = rows.get(post_id)
    if row is None:
        store.conn.execute(
            "INSERT INTO bookmarks (post_id, seq, first_seen_at, last_seen_at, state, state_at) "
            "VALUES (?, ?, ?, ?, 'active', ?)",
            (post_id, seq, now, now, now),
        )
    elif row["state"] != "active":
        store.conn.execute(
            "UPDATE bookmarks SET seq = ?, state = 'active', state_at = ? WHERE post_id = ?",
            (seq, now, post_id),
        )
    else:
        store.conn.execute("UPDATE bookmarks SET seq = ? WHERE post_id = ?", (seq, post_id))


def apply_incremental(store: Store, new_ids: list[str], seen_ids: list[str], posts: list[dict], now: str, *,
                      unavailable: Mapping[str, str] | None = None) -> None:
    new_ids = _unique(new_ids)
    with store.transaction(immediate=True):
        store.upsert_posts(posts, now)
        # After the upsert, which resets the column.
        _mark_unavailable(store, unavailable)
        # Read under the write lock, so a concurrent sync cannot hand out the same seq.
        rows = _rows(store)
        top = max([row["seq"] for row in rows.values()] or [0])
        for i, post_id in enumerate(new_ids):
            _place(store, rows, post_id, top + len(new_ids) - i, now)
        for post_id in _unique(seen_ids):
            store.conn.execute("UPDATE bookmarks SET last_seen_at = ? WHERE post_id = ?", (now, post_id))
        store.set_state("last_sync_at", now)
        refresh_fts(store, new_ids)


def apply_full(store: Store, ids_in_order: list[str], posts: list[dict], now: str, *, complete: bool = True,
               unavailable: Mapping[str, str] | None = None) -> dict:
    ids = _unique(ids_in_order)
    seen = set(ids)
    n = len(ids)
    with store.transaction(immediate=True):
        store.upsert_posts(posts, now)
        _mark_unavailable(store, unavailable)
        rows = _rows(store)
        base = max([row["seq"] for post_id, row in rows.items() if post_id not in seen] or [0])
        for i, post_id in enumerate(ids):
            _place(store, rows, post_id, base + n - i, now)
            store.conn.execute("UPDATE bookmarks SET last_seen_at = ? WHERE post_id = ?", (now, post_id))
        # A complete listing under the window covers every bookmark, so it proves removal of any unseen
        # active or beyond_window row; anything else proves nothing and leaves beyond_window rows (decision 15).
        proven = complete and n < WINDOW
        removed = 0
        beyond = 0
        for post_id, row in rows.items():
            if post_id in seen:
                continue
            state = row["state"]
            if proven and state in ("active", "beyond_window"):
                state = "removed"
                store.conn.execute("UPDATE bookmarks SET state = ?, state_at = ? WHERE post_id = ?",
                                   (state, now, post_id))
                removed += 1
            elif state == "active":
                state = "beyond_window"
                store.conn.execute("UPDATE bookmarks SET state = ?, state_at = ? WHERE post_id = ?",
                                   (state, now, post_id))
            # beyond_window counts every unseen bookmark left outside the window, not only new ones.
            if state == "beyond_window":
                beyond += 1
        store.set_state("last_full_sync_at", now)
        store.set_state("last_full_count", str(n))
        store.set_state("last_sync_at", now)
        refresh_fts(store, ids)
    return {"removed": removed, "beyond_window": beyond, "seen": n}


def refresh_fts(store: Store, post_ids: list[str]) -> None:
    store.refresh_fts(post_ids)


def _states(include_removed):
    return ("active", "beyond_window", "removed") if include_removed else ("active", "beyond_window")


def _item(store, row):
    post_id = row["post_id"]
    item = store.get_post(post_id, origin="bookmark")
    if item is None:
        item = {"id": post_id, "url": post_url(post_id, None), "origin": "bookmark", "source": "x",
                "unavailable": None}
    folders = [r["folder_id"] for r in store.conn.execute(
        "SELECT folder_id FROM bookmark_folder_items WHERE post_id = ? ORDER BY folder_id", (post_id,))]
    item["bookmark"] = {"folder_ids": folders, "first_seen_at": row["first_seen_at"], "state": row["state"],
                        "state_at": row["state_at"]}
    return item


def _check_match(query):
    """Parse the MATCH expression against an empty in-memory copy of bookmark_fts.

    Any error there comes from the query text, never from the database, so it is X_QUERY_INVALID;
    errors from the real query propagate unchanged."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(" + BOOKMARK_FTS_ARGS + ")")
        try:
            conn.execute("SELECT rowid FROM probe WHERE probe MATCH ?", (query,)).fetchall()
        except sqlite3.OperationalError as e:
            raise XError("X_QUERY_INVALID", "bookmark search query rejected: " + str(e)) from None


def search_bookmarks(store: Store, query: str, *, from_user: str | None = None, since: datetime | None = None,
                     until: datetime | None = None, folder: str | None = None, limit: int = 25,
                     include_removed: bool = False) -> list[dict]:
    limit = _count(limit, 1, "--limit")
    states = _states(include_removed)
    sql = ("SELECT bookmarks.post_id, bookmarks.first_seen_at, bookmarks.state, bookmarks.state_at "
           "FROM bookmark_fts "
           "JOIN bookmarks ON bookmarks.post_id = bookmark_fts.post_id "
           "LEFT JOIN posts ON posts.id = bookmarks.post_id "
           "WHERE bookmark_fts MATCH ? AND bookmarks.state IN (" + ",".join("?" for _ in states) + ")")
    params = [query] + list(states)
    if from_user is not None:
        sql += (" AND EXISTS (SELECT 1 FROM users WHERE users.id = posts.author_id "
                "AND lower(users.username) = lower(?))")
        params.append(from_user.strip().lstrip("@"))
    if since is not None:
        sql += " AND posts.created_at >= ?"
        params.append(iso_z(since))
    if until is not None:
        sql += " AND posts.created_at <= ?"
        params.append(iso_z(until))
    if folder is not None:
        sql += (" AND EXISTS (SELECT 1 FROM bookmark_folder_items "
                "WHERE bookmark_folder_items.folder_id = ? AND bookmark_folder_items.post_id = bookmarks.post_id)")
        params.append(folder)
    sql += " ORDER BY bm25(bookmark_fts), bookmarks.seq DESC LIMIT ?"
    params.append(limit)
    _check_match(query)
    return [_item(store, row) for row in store.conn.execute(sql, params).fetchall()]


def list_bookmarks(store: Store, *, folder: str | None = None, limit: int = 25, offset: int = 0,
                   include_removed: bool = False) -> list[dict]:
    limit = _count(limit, 1, "--limit")
    offset = _count(offset, 0, "--offset")
    states = _states(include_removed)
    sql = ("SELECT post_id, first_seen_at, state, state_at FROM bookmarks "
           "WHERE state IN (" + ",".join("?" for _ in states) + ")")
    params = list(states)
    if folder is not None:
        sql += (" AND EXISTS (SELECT 1 FROM bookmark_folder_items "
                "WHERE bookmark_folder_items.folder_id = ? AND bookmark_folder_items.post_id = bookmarks.post_id)")
        params.append(folder)
    sql += " ORDER BY seq DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [_item(store, row) for row in store.conn.execute(sql, params).fetchall()]


def pending_authors(store: Store) -> list[str]:
    rows = store.conn.execute(
        "SELECT posts.author_id AS author_id, MAX(bookmarks.seq) AS top FROM bookmarks "
        "JOIN posts ON posts.id = bookmarks.post_id "
        "LEFT JOIN users ON users.id = posts.author_id "
        "WHERE posts.author_id IS NOT NULL AND posts.author_id != '' AND users.id IS NULL "
        "GROUP BY posts.author_id ORDER BY top DESC, posts.author_id"
    ).fetchall()
    return [row["author_id"] for row in rows]


def _user_path(ctx):
    user_id = ctx.auth.user_id()
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise XError("X_AUTH_REQUIRED", "token.json holds no valid user ID; run fav auth login")
    return "/2/users/" + user_id + "/bookmarks"


def _too_many(count, size, what):
    # The reservation covered size resources; a larger page is not passed on as if it were in bounds.
    return XError("X_UPSTREAM", "X returned " + str(count) + " " + what + " for max_results " + str(size))


def _bookmark_pages(ctx, path, *, full, active_before):
    """Every bookmark page this sync needs, with nothing applied: (ids, payloads, pages, token left)."""
    ids = []
    payloads = []
    token = None
    pages = 0
    while pages < BOOKMARK_PAGES_MAX:
        params = {"max_results": PAGE_SIZE, "tweet.fields": commands.TWEET_FIELDS, "pagination_token": token}
        payload = ctx.client.get(SYNC_COMMAND, path, params, kind="owned_read", max_resources=PAGE_SIZE)
        pages += 1
        data = commands._list_data(payload)
        if len(data) > PAGE_SIZE:
            raise _too_many(len(data), PAGE_SIZE, "bookmarks")
        result = commands._normalize(payload, {})
        ids.extend(item["id"] for item in result["items"])
        payloads.append(payload)
        token = result["next"]
        if token is None:
            break
        if not full and find_stop(_unique(ids), active_before) is not None:
            break
    return _unique(ids), payloads, pages, token


def _id_pages(ctx, command, path, cap):
    """Raw entries of an ID-listing endpoint, page by page up to cap: (entries, token left after the cap)."""
    entries = []
    token = None
    for _ in range(cap):
        payload = ctx.client.get(command, path, {"max_results": PAGE_SIZE, "pagination_token": token},
                                 kind="owned_read", max_resources=PAGE_SIZE)
        data = commands._list_data(payload)
        if len(data) > PAGE_SIZE:
            raise _too_many(len(data), PAGE_SIZE, "entries")
        for entry in data:
            entry_id = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(entry_id, str) or not _SAFE_ID.fullmatch(entry_id):
                raise commands._unreadable()
            entries.append(entry)
        meta = payload.get("meta")
        token = meta.get("next_token") if isinstance(meta, dict) else None
        if token is not None and not isinstance(token, str):
            raise commands._unreadable()
        if token is None:
            break
    return entries, token


def _folders(ctx, command, path, now):
    """List and store every folder up to the page cap: (folders in X's order, notes)."""
    cap = ctx.config.folder_pages_max
    entries, token = _id_pages(ctx, command, path + "/folders", cap)
    folders = []
    seen = set()
    for entry in entries:
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        name = entry.get("name")
        folders.append({"id": entry["id"], "name": name if isinstance(name, str) else None})
    with ctx.store.transaction(immediate=True):
        for folder in folders:
            ctx.store.conn.execute(
                "INSERT INTO bookmark_folders (id, name, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, fetched_at = excluded.fetched_at",
                (folder["id"], folder["name"], now),
            )
    notes = []
    if token is not None:
        notes.append("folder list incomplete (page cap " + str(cap) + ")")
    return folders, notes


def _hydrate(ctx, missing):
    """Fetch posts by ID, 100 per request, at the post-read price: the raw payloads, nothing stored."""
    payloads = []
    for start in range(0, len(missing), HYDRATE_BATCH):
        batch = missing[start:start + HYDRATE_BATCH]
        payload = ctx.client.get(SYNC_COMMAND, "/2/tweets",
                                 {"ids": ",".join(batch), "tweet.fields": commands.TWEET_FIELDS},
                                 kind="post_read", max_resources=len(batch))
        data = commands._list_data(payload)
        if len(data) > len(batch):
            raise _too_many(len(data), len(batch), "posts")
        requested = set(batch)
        accounted = set()
        for raw in data:
            # A post nobody asked for would be cached while the requested one stays missing.
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or raw["id"] not in requested:
                raise commands._unreadable()
            accounted.add(raw["id"])
        commands._normalize(payload, {})
        errors = payload.get("errors")
        for err in errors if isinstance(errors, list) else []:
            resource_id = err.get("resource_id") if isinstance(err, dict) else None
            if isinstance(resource_id, str):
                accounted.add(resource_id)
        # A requested post X neither returned nor reported is unknown, so the batch is not complete.
        if not requested <= accounted:
            raise commands._unreadable()
        payloads.append(payload)
    return payloads


def _store_posts(ctx, payloads, now):
    """Normalize payloads with the cached authors and upsert them, marking unavailable posts."""
    users = ctx.store.get_users(commands._author_ids(payloads))
    for payload in payloads:
        result = commands._normalize(payload, users)
        ctx.store.upsert_posts(result["items"], now)
        _mark_unavailable(ctx.store, result["unavailable"])


def _payload_ids(payloads):
    """The IDs of the posts in hydration payloads, in the order stored."""
    ids = []
    for payload in payloads:
        data = payload.get("data")
        if isinstance(data, list):
            ids.extend(raw["id"] for raw in data if isinstance(raw, dict) and isinstance(raw.get("id"), str))
    return ids


def _sync_folders(ctx, path, now, summary, hydrated):
    """Full sync's folder step. Each folder's hydration payloads are appended to hydrated once that folder
    commits, so a later folder's failure still leaves the posts earlier folders stored with the caller."""
    cap = ctx.config.folder_pages_max
    folders, notes = _folders(ctx, SYNC_COMMAND, path, now)
    summary.extend(notes)
    for folder in folders:
        entries, token = _id_pages(ctx, SYNC_COMMAND, path + "/folders/" + folder["id"], cap)
        if token is not None:
            summary.append("folder " + folder["id"] + " membership incomplete (page cap " + str(cap) + ")")
            continue
        members = _unique(entry["id"] for entry in entries)
        missing = [post_id for post_id in members
                   if ctx.store.conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone() is None]
        payloads = _hydrate(ctx, missing)
        with ctx.store.transaction(immediate=True):
            _store_posts(ctx, payloads, now)
            ctx.store.conn.execute("DELETE FROM bookmark_folder_items WHERE folder_id = ?", (folder["id"],))
            for post_id in members:
                ctx.store.conn.execute("INSERT INTO bookmark_folder_items (folder_id, post_id) VALUES (?, ?)",
                                       (folder["id"], post_id))
            ctx.store.conn.execute("UPDATE bookmark_folders SET members_synced_at = ? WHERE id = ?",
                                   (now, folder["id"]))
        hydrated.extend(payloads)


@contextmanager
def _sync_lock(ctx):
    """Hold the sync lock (Global Constraints decision 19); released and closed on every path."""
    path = ctx.paths.sync_lock
    # Opened on every call so flock also excludes other processes and threads.
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise local_io_error("lock", path, e) from None
    try:
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            raise local_io_error("lock", path, e) from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def sync(ctx, *, full: bool = False) -> dict:
    path = _user_path(ctx)
    # Every read of stored state below happens under the lock, so a second sync sees this one's result.
    with _sync_lock(ctx):
        # Read the clock once the lock is held: a sync that waited stamps the time it ran (decision 19).
        moment = ctx.now_fn()
        now = iso_z(moment)
        return _sync(ctx, path, moment, now, full)


def _sync(ctx, path, moment, now, full):
    store = ctx.store

    # Authors left pending by earlier syncs first; only the budget case is best-effort here.
    resolve_authors(store, ctx.client, pending_authors(store), moment, SYNC_COMMAND)

    active_before = set(row["post_id"] for row in store.conn.execute(
        "SELECT post_id FROM bookmarks WHERE state = 'active'"))
    ids, payloads, pages, token = _bookmark_pages(ctx, path, full=full, active_before=active_before)

    users = store.get_users(commands._author_ids(payloads))
    posts = []
    unavailable = {}
    for payload in payloads:
        result = commands._normalize(payload, users)
        posts.extend(result["items"])
        unavailable.update(result["unavailable"])

    summary = []
    removed = beyond = 0
    if full:
        complete = token is None
        if not complete:
            summary.append("bookmark list incomplete (page cap " + str(BOOKMARK_PAGES_MAX) + ")")
        counts = apply_full(store, ids, posts, now, complete=complete, unavailable=unavailable)
        removed, beyond = counts["removed"], counts["beyond_window"]
        new_ids = ids
    else:
        stop = find_stop(ids, active_before)
        # Paging ended with a token left and no stop run: the cap cut it short (decision 15).
        if stop is None and token is not None:
            summary.append("bookmark list incomplete (page cap " + str(BOOKMARK_PAGES_MAX) + ")")
        # An explicit None test: 0 is a valid stop.
        cut = len(ids) if stop is None else stop
        new_ids = ids[:cut]
        apply_incremental(store, new_ids, ids, posts, now, unavailable=unavailable)
    moved = sum(1 for post_id in new_ids if post_id in active_before)
    new = len(new_ids) - moved

    # The bookmark index is committed; later failures become summary lines (Global Constraints decision 14).
    hydrated = []
    if full:
        try:
            _sync_folders(ctx, path, now, summary, hydrated)
        except XError as e:
            summary.append("folders not synced: " + str(e))
    fetched = list(payloads) + hydrated
    try:
        resolve_authors(store, ctx.client, commands._author_ids(fetched), moment, SYNC_COMMAND)
    except XError as e:
        summary.append("authors not resolved: " + str(e))

    # One media step, after applying, folders and authors, still under the sync lock (spec A1.1).
    if full:
        # A complete listing retries what X once left out (spec A1.2, decision 28).
        store.reset_unavailable_media(ids)
    stored_ids = [post["id"] for post in posts] + _payload_ids(hydrated)
    step = media.lookup_media(ctx, SYNC_COMMAND, media.bookmark_candidates(store, stored_ids))
    summary.extend(step["notes"])
    if step["error"] is not None:
        summary.append("media lookup failed: " + step["error"])

    first = "synced: " + str(new) + " new, " + str(moved) + " moved, " + str(pages) + (
        " page" if pages == 1 else " pages")
    if full:
        first += ", " + str(removed) + " removed, " + str(beyond) + " beyond window"
    result = {
        "items": [],
        "new": new,
        "moved": moved,
        "pages": pages,
        "removed": removed,
        "beyond_window": beyond,
        "authors_pending": len(pending_authors(store)),
        "media_pending": media.bookmark_media_pending(store),
    }
    if step["error"] is not None:
        result["media_error"] = step["error"]
    result.update({
        "last_sync": now,
        "estimated_cost_usd": ctx.budget.spent(),
        "summary": [first] + summary,
    })
    return result


def list_folders(ctx) -> dict:
    path = _user_path(ctx)
    folders, notes = _folders(ctx, FOLDERS_COMMAND, path, iso_z(ctx.now_fn()))
    out = []
    for folder in folders:
        row = ctx.store.conn.execute("SELECT members_synced_at FROM bookmark_folders WHERE id = ?",
                                     (folder["id"],)).fetchone()
        out.append({"id": folder["id"], "name": folder["name"],
                    "members_synced_at": row["members_synced_at"] if row is not None else None})
    return {"folders": out, "notes": notes, "estimated_cost_usd": ctx.budget.spent()}
