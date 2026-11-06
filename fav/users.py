import re
from datetime import datetime

from fav.errors import XError
from fav.parse import iso_z

BATCH = 100
USER_FIELDS = "name,username"
# IDs that can go into the comma-separated ids= list without splitting or escaping.
_LISTABLE = re.compile(r"[A-Za-z0-9_-]+")


def _unique_ids(author_ids):
    seen = []
    for author_id in author_ids:
        if isinstance(author_id, str) and author_id and author_id not in seen:
            seen.append(author_id)
    return seen


def _returned_users(payload, requested):
    """Users X returned for this batch, keeping only well-formed entries for requested IDs."""
    data = payload.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    found = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        user_id = raw.get("id")
        if not isinstance(user_id, str) or user_id not in requested:
            continue
        username = raw.get("username")
        name = raw.get("name")
        if not isinstance(username, str) or not username:
            continue
        found.append({"id": user_id, "username": username,
                      "display_name": name if isinstance(name, str) else None})
    return found


def resolve_authors(store, client, author_ids: list[str], now: datetime, command: str) -> int:
    wanted = _unique_ids(author_ids)
    # Only IDs that fit the ids= list go into it; anything else stays pending.
    todo = [user_id for user_id in store.users_needing_lookup(wanted, now) if _LISTABLE.fullmatch(user_id)]
    fetched_at = iso_z(now)
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        try:
            payload = client.get(command, "/2/users", {"ids": ",".join(batch), "user.fields": USER_FIELDS},
                                 kind="user_read", max_resources=len(batch))
        except XError as e:
            if e.code == "X_BUDGET_EXCEEDED":
                break
            raise
        found = _returned_users(payload, set(batch))
        if found:
            store.upsert_users(found, fetched_at)
    known = store.get_users(wanted)
    return sum(1 for user_id in wanted if user_id not in known)
