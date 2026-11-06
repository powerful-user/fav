from typing import Mapping

from wings_x.parse import iso_z, parse_date, post_url

_ORIGINS = ("live", "bookmark")


def _created_at(value):
    return iso_z(parse_date(value))


def _referenced(raw):
    if "referenced_tweets" in raw:
        return raw.get("referenced_tweets") or []
    return raw.get("referenced_posts") or []


def _included_raw(payload):
    includes = payload.get("includes") or {}
    if "tweets" in includes:
        return includes.get("tweets") or []
    return includes.get("posts") or []


def _reposts(metrics):
    if "retweet_count" in metrics:
        return metrics.get("retweet_count") or 0
    return metrics.get("repost_count") or 0


def _author(author_id, users):
    if not author_id:
        return None
    known = users.get(author_id)
    if not known:
        return {"id": author_id}
    author = {"id": author_id}
    if "username" in known:
        author["username"] = known["username"]
    if "display_name" in known:
        author["display_name"] = known["display_name"]
    return author


def normalize_post(raw: dict, users: Mapping[str, dict], origin: str) -> dict:
    if origin not in _ORIGINS:
        raise ValueError(origin)
    post_id = raw["id"]
    author = _author(raw.get("author_id"), users)
    username = None
    if author is not None:
        username = author.get("username")
    item = {
        "id": post_id,
        "url": post_url(post_id, username),
        "origin": origin,
        "source": "x",
        "unavailable": None,
    }
    if raw.get("text") is not None:
        item["text"] = raw["text"]
    if author is not None:
        item["author"] = author
    if raw.get("created_at"):
        item["created_at"] = _created_at(raw["created_at"])
    if raw.get("conversation_id") is not None:
        item["conversation_id"] = raw["conversation_id"]
    if raw.get("lang") is not None:
        item["lang"] = raw["lang"]
    metrics = raw.get("public_metrics")
    if metrics is not None:
        item["metrics"] = {
            "likes": metrics.get("like_count") or 0,
            "reposts": _reposts(metrics),
            "replies": metrics.get("reply_count") or 0,
            "quotes": metrics.get("quote_count") or 0,
        }
    media_keys = (raw.get("attachments") or {}).get("media_keys")
    if media_keys:
        item["media"] = [{"key": key} for key in media_keys]
    refs = _referenced(raw)
    if refs:
        item["referenced"] = [
            {"type": ref.get("type"), "id": ref.get("id"), "url": post_url(ref.get("id"), None)}
            for ref in refs
        ]
    return item


def unavailable_map(payload):
    found = {}
    for err in payload.get("errors") or []:
        key = err.get("resource_id")
        if key is None or key == "":
            key = err.get("value")
        if key is None or key == "":
            continue
        title = err.get("title") or ""
        detail = err.get("detail") or ""
        if title == "Not Found Error":
            kind = "deleted"
        elif title == "Authorization Error" or "not authorized to see" in detail:
            kind = "protected"
        else:
            continue
        found[key] = kind
    return found


def _with_reference_details(item, included_by_id, unavailable):
    if "referenced" not in item:
        return item
    updated = []
    for ref in item["referenced"]:
        ref = dict(ref)
        known = included_by_id.get(ref["id"])
        if known is not None:
            username = (known.get("author") or {}).get("username")
            if username:
                ref["url"] = post_url(ref["id"], username)
        if ref["id"] in unavailable:
            ref["unavailable"] = unavailable[ref["id"]]
        updated.append(ref)
    item["referenced"] = updated
    return item


def normalize_response(payload: dict, users: Mapping[str, dict], origin: str) -> dict:
    unavailable = unavailable_map(payload)
    included = [normalize_post(raw, users, origin) for raw in _included_raw(payload)]
    included_by_id = {}
    for item in included:
        included_by_id[item["id"]] = item
    data = payload.get("data")
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = [data]
    else:
        raw_items = []
    items = []
    for raw in raw_items:
        item = normalize_post(raw, users, origin)
        items.append(_with_reference_details(item, included_by_id, unavailable))
    meta = payload.get("meta") or {}
    return {
        "items": items,
        "included": included,
        "unavailable": unavailable,
        "next": meta.get("next_token"),
    }


def _add_author(raw, seen):
    if not isinstance(raw, dict):
        return
    author_id = raw.get("author_id")
    if author_id and author_id not in seen:
        seen.append(author_id)


def author_ids(payload: dict) -> list[str]:
    seen = []
    data = payload.get("data")
    if isinstance(data, list):
        for raw in data:
            _add_author(raw, seen)
    elif isinstance(data, dict):
        _add_author(data, seen)
    for raw in _included_raw(payload):
        _add_author(raw, seen)
    return seen
