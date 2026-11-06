import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Mapping

from fav import media, normalize
from fav.auth import Auth
from fav.budget import KINDS, Budget
from fav.client import XClient
from fav.config import Config, load_config
from fav.errors import XError, local_io_error
from fav.parse import iso_z, parse_date, parse_post_ref, parse_since, parse_user_ref
from fav.paths import Paths, ensure_dirs, resolve_paths
from fav.store import Store
from fav.users import resolve_authors

TWEET_FIELDS = "attachments,author_id,conversation_id,created_at,lang,public_metrics,referenced_tweets"
USER_FIELDS = "created_at,description,name,public_metrics,username"

SEARCH_MIN_RESULTS = 10
SEARCH_MAX_RESULTS = 100
THREAD_LIMIT_MAX = 500
HOP_LIMIT = 10
RECENT_WINDOW = timedelta(days=7)
OLD_REPLIES_NOTE = "replies older than 7 days are not searchable"

_REASONS = {"X_NOT_FOUND": "deleted", "X_PROTECTED": "protected"}
_LANG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")


@dataclass
class Context:
    paths: Paths
    config: Config
    store: Store
    budget: Budget
    client: XClient
    auth: Auth
    now_fn: Callable[[], datetime]


def build_context(env: Mapping[str, str], *, transport, now_fn: Callable[[], datetime],
                  sleep: Callable[[float], None] = time.sleep, extra_usd: float = 0.0,
                  invocation_id: str | None = None) -> Context:
    paths = resolve_paths(env)
    ensure_dirs(paths)
    config = load_config(paths.config_file)
    try:
        store = Store(paths.db_file)
    except (OSError, sqlite3.DatabaseError) as exc:
        raise local_io_error("open", paths.db_file, exc) from None
    try:
        budget = Budget(store, config, invocation_id=invocation_id or uuid.uuid4().hex, extra_usd=extra_usd,
                        now_fn=now_fn)
        auth = Auth(paths, config, transport, now_fn)
        client = XClient(transport, token=auth.access_token, budget=budget, sleep=sleep)
    except BaseException:
        store.close()
        raise
    return Context(paths=paths, config=config, store=store, budget=budget, client=client, auth=auth,
                   now_fn=now_fn)


def _bad(message):
    raise XError("X_BAD_INPUT", message)


def _whole(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < low or value > high:
        _bad(label + " must be a whole number from " + str(low) + " to " + str(high))
    return value


def _unreadable():
    return XError("X_UPSTREAM", "X returned a response fav cannot read")


def _list_data(payload):
    """A listing page's posts or entries: a list, or none only when X counts none."""
    data = payload.get("data")
    if data is None:
        data = []
    elif not isinstance(data, list):
        raise _unreadable()
    meta = payload.get("meta")
    # A count X sent, even null, must be a whole number matching the page, or the page is not known complete.
    if isinstance(meta, dict) and "result_count" in meta:
        count = meta["result_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count != len(data):
            raise _unreadable()
    return data


def _normalize(payload, users):
    """normalize_response for X output, with malformed shapes reported as X_UPSTREAM."""
    try:
        result = normalize.normalize_response(payload, users, "live")
    except XError:
        # A date X sent that does not parse is X's fault, not the operator's input.
        raise _unreadable() from None
    except (KeyError, TypeError, AttributeError, ValueError):
        raise _unreadable() from None
    if result["next"] is not None and not isinstance(result["next"], str):
        raise _unreadable()
    for item in result["items"] + result["included"]:
        if not isinstance(item["id"], str) or ("text" in item and not isinstance(item["text"], str)):
            raise _unreadable()
    return result


def _author_ids(payloads):
    ids = []
    for payload in payloads:
        try:
            found = normalize.author_ids(payload)
        except (TypeError, AttributeError):
            raise _unreadable() from None
        for author_id in found:
            if isinstance(author_id, str) and author_id not in ids:
                ids.append(author_id)
    return ids


def _finish(ctx, command, payloads):
    """Cache the posts, mark unavailable ones, resolve authors, and normalize again with them."""
    now = ctx.now_fn()
    ids = _author_ids(payloads)
    users = ctx.store.get_users(ids)
    first = [_normalize(payload, users) for payload in payloads]
    posts = []
    for result in first:
        posts.extend(result["items"])
        posts.extend(result["included"])
    ctx.store.upsert_posts(posts, iso_z(now))
    for result in first:
        for post_id, kind in result["unavailable"].items():
            if isinstance(post_id, str):
                ctx.store.mark_unavailable(post_id, kind)
    pending = resolve_authors(ctx.store, ctx.client, ids, now, command)
    users = ctx.store.get_users(ids)
    return [_normalize(payload, users) for payload in payloads], pending


def _missing(payload, post_id):
    """The error for a lookup that returned no post, from its errors[] entries."""
    unavailable = _normalize(payload, {})["unavailable"]
    code = "X_PROTECTED" if unavailable.get(post_id) == "protected" else "X_NOT_FOUND"
    parts = []
    errors = payload.get("errors")
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, dict):
                for key in ("title", "detail"):
                    value = err.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
                break
    return XError(code, ": ".join(parts) or "X returned no post " + post_id)


def _fetch_post(ctx, command, post_id, params, max_resources):
    """GET one post; X_NOT_FOUND or X_PROTECTED marks a cached copy unavailable before propagating."""
    try:
        payload = ctx.client.get(command, "/2/tweets/" + post_id, params, kind="post_read",
                                 max_resources=max_resources)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _missing(payload, post_id)
        if data.get("id") != post_id:
            raise _unreadable()
    except XError as e:
        if e.code in _REASONS:
            ctx.store.mark_unavailable(post_id, _REASONS[e.code])
        raise
    return payload


def search(ctx, query: str, *, since: str | None = None, until: str | None = None, from_user: str | None = None,
           lang: str | None = None, limit: int | None = None, next_token: str | None = None) -> dict:
    text = query.strip() if isinstance(query, str) else ""
    if not text:
        _bad("search query is empty")
    limit = _whole(ctx.config.search_limit_default if limit is None else limit, SEARCH_MIN_RESULTS,
                   SEARCH_MAX_RESULTS, "--limit")
    filters = []
    if from_user is not None:
        kind, name = parse_user_ref(from_user)
        if kind != "username":
            _bad("--from takes a username, not a user ID: " + from_user)
        filters.append("from:" + name)
    if lang is not None:
        if not _LANG.fullmatch(lang.strip()):
            _bad("--lang is not a language code: " + lang)
        filters.append("lang:" + lang.strip())
    if filters:
        # X applies AND before OR, so the operator's query is grouped before the filters.
        text = "(" + text + ") " + " ".join(filters)
    now = ctx.now_fn()
    params = {
        "query": text,
        "max_results": limit,
        "tweet.fields": TWEET_FIELDS,
        "start_time": iso_z(parse_since(since, now)) if since is not None else None,
        "end_time": iso_z(parse_date(until)) if until is not None else None,
        "next_token": next_token,
    }
    payload = ctx.client.get("search", "/2/tweets/search/recent", params, kind="post_read",
                             max_resources=limit)
    data = _list_data(payload)
    if len(data) > limit:
        # Trimming would hide posts behind a next token that points past them.
        raise XError("X_UPSTREAM", "X returned " + str(len(data)) + " posts for max_results " + str(limit))
    (result,), pending = _finish(ctx, "search", [payload])
    fields, media_notes = media.run_step(ctx, "search", result["items"])
    out = {
        "items": result["items"],
        "next": result["next"],
        "authors_pending": pending,
    }
    out.update(fields)
    if media_notes:
        out["notes"] = media_notes
    # Read after the media step, so the estimate includes its lookup.
    out["estimated_cost_usd"] = ctx.budget.spent()
    return out


def post(ctx, ref: str) -> dict:
    post_id = parse_post_ref(ref)
    params = {"tweet.fields": TWEET_FIELDS, "expansions": "referenced_tweets.id"}
    payload = _fetch_post(ctx, "post", post_id, params, 4)
    (result,), pending = _finish(ctx, "post", [payload])
    fields, media_notes = media.run_step(ctx, "post", result["items"] + result["included"])
    out = {
        "items": result["items"],
        "referenced_posts": result["included"],
        "authors_pending": pending,
    }
    out.update(fields)
    if media_notes:
        out["notes"] = media_notes
    out["estimated_cost_usd"] = ctx.budget.spent()
    return out


def _user_item(data):
    if not isinstance(data, dict):
        raise _unreadable()
    user_id = data.get("id")
    username = data.get("username")
    if not isinstance(user_id, str) or not user_id or not isinstance(username, str) or not username:
        raise _unreadable()
    item = {"id": user_id, "username": username}
    if isinstance(data.get("name"), str):
        item["display_name"] = data["name"]
    if isinstance(data.get("description"), str):
        item["description"] = data["description"]
    if data.get("created_at") is not None:
        try:
            item["created_at"] = iso_z(parse_date(data["created_at"]))
        except (XError, AttributeError, TypeError):
            raise _unreadable() from None
    metrics = data.get("public_metrics")
    if isinstance(metrics, dict):
        item["metrics"] = {
            "followers": metrics.get("followers_count") or 0,
            "following": metrics.get("following_count") or 0,
            "posts": metrics.get("tweet_count") or 0,
        }
    item["url"] = "https://x.com/" + username
    return item


def user(ctx, ref: str) -> dict:
    kind, value = parse_user_ref(ref)
    path = "/2/users/by/username/" + value if kind == "username" else "/2/users/" + value
    payload = ctx.client.get("user", path, {"user.fields": USER_FIELDS}, kind="user_read", max_resources=1)
    data = payload.get("data")
    if data is None:
        unavailable = _normalize(payload, {})["unavailable"]
        code = "X_PROTECTED" if "protected" in unavailable.values() else "X_NOT_FOUND"
        raise XError(code, "X returned no user for " + ref.strip())
    item = _user_item(data)
    # Usernames match without case; a different user than the one asked for is not shown or cached.
    returned = item["id"] if kind == "id" else item["username"].lower()
    if returned != (value if kind == "id" else value.lower()):
        raise _unreadable()
    ctx.store.upsert_users([{"id": item["id"], "username": item["username"],
                             "display_name": item.get("display_name")}], iso_z(ctx.now_fn()))
    return {"users": [item], "estimated_cost_usd": ctx.budget.spent()}


def _parent(raw):
    refs = raw.get("referenced_tweets")
    if refs is None:
        refs = raw.get("referenced_posts")
    if not isinstance(refs, list):
        return None
    for ref in refs:
        if isinstance(ref, dict) and ref.get("type") == "replied_to":
            parent = ref.get("id")
            if isinstance(parent, str) and parent.isdigit():
                return parent
    return None


def _sort_key(item):
    post_id = item["id"]
    return (item.get("created_at") or "", len(post_id), post_id)


def thread(ctx, ref: str, *, limit: int | None = None) -> dict:
    post_id = parse_post_ref(ref)
    limit = _whole(ctx.config.thread_limit_default if limit is None else limit, 1, THREAD_LIMIT_MAX, "--limit")
    params = {"tweet.fields": TWEET_FIELDS}
    truncated = False
    gaps = []
    notes = []

    main = _fetch_post(ctx, "thread", post_id, params, 1)
    count = 1
    main_raw = main["data"]
    conversation = main_raw.get("conversation_id")
    root = conversation if isinstance(conversation, str) and conversation.isdigit() else post_id
    seen = {post_id}

    root_payload = None
    if root != post_id:
        seen.add(root)
        if count < limit:
            try:
                root_payload = _fetch_post(ctx, "thread", root, params, 1)
                count += 1
            except XError as e:
                if e.code not in _REASONS:
                    raise
                gaps.append({"after": None, "missing": root, "reason": _REASONS[e.code]})
        else:
            truncated = True

    ancestors = []
    current = main_raw
    hops = 0
    while True:
        parent = _parent(current)
        if parent is None or parent == root or parent in seen:
            break
        if hops == HOP_LIMIT:
            gaps.append({"after": current["id"], "missing": parent, "reason": "hop limit"})
            break
        if count >= limit:
            truncated = True
            break
        try:
            payload = _fetch_post(ctx, "thread", parent, params, 1)
        except XError as e:
            if e.code not in _REASONS:
                raise
            gaps.append({"after": current["id"], "missing": parent, "reason": _REASONS[e.code]})
            break
        seen.add(parent)
        ancestors.append(payload)
        count += 1
        hops += 1
        current = payload["data"]

    replies_payload = None
    remaining = limit - count
    if remaining <= 0:
        truncated = True
    else:
        requested = min(max(remaining, SEARCH_MIN_RESULTS), SEARCH_MAX_RESULTS)
        page = ctx.client.get("thread", "/2/tweets/search/recent",
                              {"query": "conversation_id:" + root, "max_results": requested,
                               "tweet.fields": TWEET_FIELDS},
                              kind="post_read", max_resources=requested)
        listed = _normalize(page, {})
        candidates = [item for item in listed["items"] if item["id"] not in seen]
        candidates.sort(key=_sort_key)
        kept = {item["id"] for item in candidates[:remaining]}
        if len(candidates) > remaining or listed["next"] is not None:
            truncated = True
        data = _list_data(page)
        replies_payload = dict(page, data=[raw for raw in data if isinstance(raw, dict) and raw.get("id") in kept])

    ordered = ([root_payload] if root_payload is not None else []) + list(reversed(ancestors)) + [main]
    payloads = ordered + ([replies_payload] if replies_payload is not None else [])
    results, pending = _finish(ctx, "thread", payloads)
    items = []
    for result in results[:len(ordered)]:
        items.extend(result["items"])
    if replies_payload is not None:
        items.extend(sorted(results[-1]["items"], key=_sort_key))

    if root_payload is not None or root == post_id:
        root_raw = (root_payload or main)["data"]
        try:
            created = parse_date(root_raw["created_at"]) if root_raw.get("created_at") else None
        except (XError, AttributeError, TypeError):
            created = None
        if created is not None and ctx.now_fn() - created > RECENT_WINDOW:
            notes.append(OLD_REPLIES_NOTE)

    # The media step takes posts in the order they were fetched, not the display order (spec A1.1 "Batches"):
    # the requested post, the root, the ancestors nearest first, then the kept replies in X's order.
    by_id = dict((item["id"], item) for item in items)
    fetched = [post_id]
    if root_payload is not None:
        fetched.append(root)
    fetched.extend(payload["data"]["id"] for payload in ancestors)
    if replies_payload is not None:
        fetched.extend(item["id"] for item in results[-1]["items"])
    fields, media_notes = media.run_step(ctx, "thread", [by_id[i] for i in fetched if i in by_id])
    notes.extend(media_notes)
    out = {
        "items": items,
        "truncated": truncated,
        "gaps": gaps,
        "notes": notes,
        "authors_pending": pending,
    }
    out.update(fields)
    out["estimated_cost_usd"] = ctx.budget.spent()
    return out


USAGE_DAYS_MAX = 90


def _local_usage(ctx, today, days):
    first = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = ctx.store.conn.execute(
        """
        SELECT substr(at, 1, 10) AS day,
               COALESCE(SUM(CASE WHEN state = 'settled' THEN est_usd ELSE 0 END), 0) AS estimated,
               COALESCE(SUM(CASE WHEN state IN ('reserved', 'abandoned') THEN reserved_usd ELSE 0 END), 0)
                   AS reserved,
               COALESCE(SUM(CASE WHEN state = 'abandoned' THEN 1 ELSE 0 END), 0) AS abandoned
        FROM ledger
        WHERE substr(at, 1, 10) >= ?
        GROUP BY day
        """,
        (first,),
    ).fetchall()
    by_day = dict((row["day"], row) for row in rows)
    charged = {}
    for row in ctx.store.conn.execute(
        "SELECT utc_day, kind, COUNT(*) AS n FROM charged WHERE utc_day >= ? GROUP BY utc_day, kind",
        (first,),
    ):
        charged.setdefault(row["utc_day"], {})[row["kind"]] = int(row["n"])
    local = []
    for back in range(days):
        day = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        row = by_day.get(day)
        local.append({
            "day": day,
            "estimated_usd": float(row["estimated"]) if row else 0.0,
            "reserved_usd": float(row["reserved"]) if row else 0.0,
            "abandoned": int(row["abandoned"]) if row else 0,
            "charged": dict((kind, charged.get(day, {}).get(kind, 0)) for kind in KINDS),
        })
    return local


def usage(ctx: Context, days: int = 7) -> dict:
    days = _whole(days, 1, USAGE_DAYS_MAX, "--days")
    ctx.budget.abandon_stale()
    # The same UTC day the ledger stamps on its rows.
    today = parse_date(iso_z(ctx.now_fn())[:10])
    ceiling = ctx.config.daily_ceiling_usd
    result = {
        "local": _local_usage(ctx, today, days),
        "ceiling_usd": ceiling,
        "remaining_usd": max(0.0, ceiling - ctx.budget.committed_today()),
        "x": None,
        "x_note": None,
        "estimated_cost_usd": 0.0,
    }
    try:
        bearer = ctx.auth.load_app_bearer()
        if bearer is None:
            result["x_note"] = ("X usage needs the app bearer token in " + ctx.paths.app_bearer_file
                                + "; run fav auth app-token")
            return result
        credits = ctx.client.get_exempt("usage", "/2/usage/credits", None, bearer=bearer)
        tweets = ctx.client.get_exempt("usage", "/2/usage/tweets", {"days": days}, bearer=bearer)
    except XError as e:
        # The local ledger is still useful when X's usage endpoints fail.
        result["x_note"] = "X usage unavailable: " + str(e)
        return result
    result["x"] = {"credits": credits, "tweets": tweets}
    return result


BOOKMARK_LIMIT_DEFAULT = 25
BOOKMARK_LIMIT_MAX = 500
BOOKMARK_OFFSET_MAX = 10 ** 9


def _folder_synced_at(ctx, folder):
    row = ctx.store.conn.execute("SELECT members_synced_at FROM bookmark_folders WHERE id = ?",
                                 (folder,)).fetchone()
    return row["members_synced_at"] if row is not None else None


def _local_result(ctx, items, folder):
    result = {"items": items, "last_sync": ctx.store.get_state("last_sync_at")}
    if folder is not None:
        result["folder_synced_at"] = _folder_synced_at(ctx, folder)
    return result


def bookmarks_search(ctx, query: str, *, from_user: str | None = None, since: str | None = None,
                     until: str | None = None, folder: str | None = None, limit: int | None = None,
                     include_removed: bool = False) -> dict:
    # Imported here: fav.bookmarks imports this module.
    from fav import bookmarks

    text = query.strip() if isinstance(query, str) else ""
    if not text:
        _bad("bookmark search query is empty")
    limit = _whole(BOOKMARK_LIMIT_DEFAULT if limit is None else limit, 1, BOOKMARK_LIMIT_MAX, "--limit")
    name = None
    if from_user is not None:
        kind, name = parse_user_ref(from_user)
        if kind != "username":
            _bad("--from takes a username, not a user ID: " + from_user)
    start = parse_since(since, ctx.now_fn()) if since is not None else None
    end = parse_date(until) if until is not None else None
    items = bookmarks.search_bookmarks(ctx.store, text, from_user=name, since=start, until=end, folder=folder,
                                       limit=limit, include_removed=include_removed)
    return _local_result(ctx, items, folder)


def bookmarks_list(ctx, *, folder: str | None = None, limit: int | None = None, offset: int = 0,
                   include_removed: bool = False) -> dict:
    from fav import bookmarks

    limit = _whole(BOOKMARK_LIMIT_DEFAULT if limit is None else limit, 1, BOOKMARK_LIMIT_MAX, "--limit")
    offset = _whole(offset, 0, BOOKMARK_OFFSET_MAX, "--offset")
    items = bookmarks.list_bookmarks(ctx.store, folder=folder, limit=limit, offset=offset,
                                     include_removed=include_removed)
    return _local_result(ctx, items, folder)
