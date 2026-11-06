"""Media (spec Amendment 1): media items, and the budgeted lookup step that fills them in."""
import json
import re

from fav import normalize
from fav.errors import XError

MEDIA_FIELDS = "media_key,type,url,preview_image_url,width,height,alt_text,duration_ms,variants"
LOOKUP_BATCH = 100
LOOKUP_PATH = "/2/tweets"

_ID = re.compile(r"[A-Za-z0-9_-]+")
# The tolerance Budget.reserve allows over the allowance.
_EPSILON = 1e-9

_TEXT_FIELDS = ("type", "url", "preview_image_url", "alt_text")
# Each whole-number field and its least value: a size is at least 1, a length or bit rate at least 0.
_WHOLE_FIELDS = (("width", 1), ("height", 1), ("duration_ms", 0))


def _whole(value, least=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


def _variants(raw):
    if not isinstance(raw, list):
        return []
    mp4_rated = []
    mp4_unrated = []
    others = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        content_type = entry.get("content_type")
        url = entry.get("url")
        if not isinstance(content_type, str) or not isinstance(url, str):
            continue
        variant = {"content_type": content_type, "url": url}
        bit_rate = entry.get("bit_rate")
        if _whole(bit_rate):
            variant["bit_rate"] = bit_rate
        if content_type != "video/mp4":
            others.append(variant)
        elif "bit_rate" in variant:
            mp4_rated.append(variant)
        else:
            mp4_unrated.append(variant)
    # sorted is stable, so equal bit rates keep X's order.
    mp4_rated = sorted(mp4_rated, key=lambda v: -v["bit_rate"])
    return mp4_rated + mp4_unrated + others


def normalize_media(raw: dict) -> dict:
    """One includes.media entry, already known to hold a string media_key, as a stored media item."""
    item = {"key": raw["media_key"]}
    for field in _TEXT_FIELDS:
        value = raw.get(field)
        if isinstance(value, str):
            item[field] = value
    for field, least in _WHOLE_FIELDS:
        value = raw.get(field)
        if _whole(value, least):
            item[field] = value
    variants = _variants(raw.get("variants"))
    if variants:
        item["variants"] = variants
    return item


def media_index(payload: dict) -> dict:
    """Media key to media item for a whole response. A malformed includes.media is X_UPSTREAM."""
    if not isinstance(payload, dict) or "includes" not in payload:
        return {}
    includes = payload["includes"]
    if not isinstance(includes, dict):
        raise XError("X_UPSTREAM", "X returned malformed includes")
    if "media" not in includes:
        return {}
    entries = includes["media"]
    if not isinstance(entries, list):
        raise XError("X_UPSTREAM", "X returned malformed includes.media")
    index = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("media_key"), str):
            raise XError("X_UPSTREAM", "X returned a malformed media entry")
        index[entry["media_key"]] = normalize_media(entry)
    return index


def is_pending(item) -> bool:
    """A media item holding only its key has not been looked up yet."""
    return isinstance(item, dict) and list(item) == ["key"]


def pending_count(posts: list) -> int:
    """Pending attachments across posts, skipping posts marked unavailable (decision 22)."""
    count = 0
    for post in posts:
        if not isinstance(post, dict) or post.get("unavailable"):
            continue
        items = post.get("media")
        if not isinstance(items, list):
            continue
        count += sum(1 for item in items if is_pending(item))
    return count


def _malformed():
    return XError("X_UPSTREAM", "X returned a media lookup wings-x cannot read")


def _resources(post):
    """Every resource a lookup of this stored post can return: the post and each stored media key."""
    resources = [("post_read", post["id"])]
    for item in post.get("media") or []:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            resources.append(("media_read", item["key"]))
    return resources


def _candidates(store, post_ids):
    """Stored, available posts with a pending item, in the given order, as (id, resources)."""
    seen = set()
    found = []
    for post_id in post_ids:
        if not isinstance(post_id, str) or post_id in seen or not _ID.fullmatch(post_id):
            continue
        seen.add(post_id)
        post = store.get_post(post_id)
        if post is None or post.get("unavailable"):
            continue
        items = post.get("media")
        if not isinstance(items, list) or not any(is_pending(item) for item in items):
            continue
        found.append((post_id, _resources(post)))
    return found


def _next_batch(budget, candidates):
    """The longest prefix of candidates, at most LOOKUP_BATCH, whose reservations fit the allowance."""
    allowance = budget.allowance()
    total = 0.0
    batch = []
    for post_id, resources in candidates[:LOOKUP_BATCH]:
        cost = budget.resource_cost(resources)
        if total + cost > allowance + _EPSILON:
            break
        total += cost
        batch.append((post_id, resources))
    return batch


def _parse(payload, batch_ids):
    """The lookup's returned keys by post, its media detail, and the batch's posts X reports unavailable."""
    data = payload.get("data")
    if data is None:
        data = []
    elif not isinstance(data, list):
        raise _malformed()
    returned = {}
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise _malformed()
        keys = []
        if "attachments" in entry:
            attachments = entry["attachments"]
            if not isinstance(attachments, dict):
                raise _malformed()
            if "media_keys" in attachments:
                keys = attachments["media_keys"]
                if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                    raise _malformed()
        if entry["id"] in batch_ids and entry["id"] not in returned:
            returned[entry["id"]] = list(keys)
    errors = payload.get("errors")
    if errors is not None:
        if not isinstance(errors, list) or not all(isinstance(err, dict) for err in errors):
            raise _malformed()
    index = media_index(payload)
    try:
        reported = normalize.unavailable_map(payload)
    except (AttributeError, TypeError):
        raise _malformed() from None
    unavailable = dict((post_id, kind) for post_id, kind in reported.items()
                       if isinstance(post_id, str) and post_id in batch_ids)
    return returned, index, unavailable


def lookup_media(ctx, command: str, post_ids: list) -> dict:
    """One media lookup step: batches within the allowance, merged as they return. Never raises XError."""
    notes = []
    try:
        candidates = _candidates(ctx.store, post_ids)
        while candidates:
            batch = _next_batch(ctx.budget, candidates)
            if not batch:
                # The next post's own reservation exceeds the allowance: it and every later one stay pending.
                break
            candidates = candidates[len(batch):]
            ids = [post_id for post_id, _resources in batch]
            params = {"ids": ",".join(ids), "tweet.fields": "attachments",
                      "expansions": "attachments.media_keys", "media.fields": MEDIA_FIELDS}
            resources = [resource for _post_id, listed in batch for resource in listed]
            payload = ctx.client.get(command, LOOKUP_PATH, params, resources=resources)
            returned, index, unavailable = _parse(payload, set(ids))
            for post_id, kind in unavailable.items():
                ctx.store.mark_unavailable(post_id, kind)
            # The lookup's data carries no author, date or metrics, so it is merged by key, never upserted.
            for post_id in ctx.store.merge_media(returned, index):
                notes.append("media keys changed for " + post_id)
    except XError as e:
        if e.code == "X_BUDGET_EXCEEDED":
            # A raced reservation, or a retry past the --budget the first attempt used: a budget stop.
            return {"error": None, "notes": notes}
        return {"error": e.code, "notes": notes}
    return {"error": None, "notes": notes}


def refresh_items(store, items: list) -> None:
    """Replace each item's media and unavailable with the stored ones (decision 30)."""
    for item in items:
        post = store.get_post(item["id"])
        if post is None:
            continue
        item["unavailable"] = post.get("unavailable")
        if "media" in post:
            item["media"] = post["media"]
        else:
            item.pop("media", None)


def run_step(ctx, command: str, items: list) -> tuple:
    """The media step for a command's posts, given in the order fetched."""
    step = lookup_media(ctx, command, [item["id"] for item in items])
    refresh_items(ctx.store, items)
    fields = {"media_pending": pending_count(items)}
    if step["error"] is not None:
        fields["media_error"] = step["error"]
    return fields, step["notes"]


def bookmark_candidates(store, first: list) -> list:
    """first, then every other active or beyond_window bookmark, newest seq first. lookup_media filters the rest."""
    ids = []
    seen = set()
    for post_id in first:
        if post_id not in seen:
            seen.add(post_id)
            ids.append(post_id)
    rows = store.conn.execute(
        "SELECT post_id FROM bookmarks WHERE state IN ('active', 'beyond_window') ORDER BY seq DESC"
    ).fetchall()
    for row in rows:
        if row["post_id"] not in seen:
            seen.add(row["post_id"])
            ids.append(row["post_id"])
    return ids


def bookmark_media_pending(store) -> int:
    """Pending attachments across live bookmarks whose post is not unavailable (spec A1.1, decision 22)."""
    rows = store.conn.execute(
        "SELECT posts.media_json AS media_json FROM bookmarks JOIN posts ON posts.id = bookmarks.post_id "
        "WHERE bookmarks.state IN ('active', 'beyond_window') AND posts.unavailable IS NULL "
        "AND posts.media_json IS NOT NULL"
    ).fetchall()
    count = 0
    for row in rows:
        try:
            items = json.loads(row["media_json"])
        except (TypeError, ValueError, RecursionError):
            continue
        if isinstance(items, list):
            count += sum(1 for item in items if is_pending(item))
    return count
