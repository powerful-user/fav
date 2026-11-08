import json
import math
from datetime import datetime
from urllib.parse import urlsplit

from fav.errors import XError
from fav.media import is_pending
from fav.parse import parse_date

_UNAVAILABLE_MARKERS = {"deleted": "[deleted on X]", "protected": "[protected on X]"}
_STATE_MARKERS = {"beyond_window": "[beyond X's 800 window]", "removed": "[removed]"}
_VERBS = {"quoted": "quotes", "replied_to": "replies to", "retweeted": "reposts"}
_MEDIA_LABELS = {"photo": "▣ photo", "video": "▶ video", "animated_gif": "▶ GIF"}
_PREVIEWED = ("video", "animated_gif")


# Unicode's bidirectional formatting characters (ALM, LRM, RLM, LRE-RLO, LRI-PDI): text from X could use them
# to make what a terminal shows differ from what was sent.
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def terminal_safe(text: str) -> str:
    """Text for a terminal: control characters other than newline and tab are shown as \\xNN, never sent,
    and bidirectional formatting characters as \\uXXXX.

    Post text, names, alt text, URLs and X's error messages come from X, so a terminal must not act on them.
    """
    out = []
    for c in text.replace("\r\n", "\n"):
        code = ord(c)
        if (code < 0x20 and c not in "\n\t") or 0x7f <= code <= 0x9f:
            out.append("\\x" + format(code, "02x"))
        elif c in _BIDI:
            out.append("\\u" + format(code, "04x"))
        else:
            out.append(c)
    return "".join(out)


def _moment(iso):
    try:
        return parse_date(iso)
    except (XError, AttributeError, TypeError):
        return None


def ago(iso: str | None, now: datetime) -> str:
    if iso is None:
        return "never"
    moment = _moment(iso)
    if moment is None:
        return str(iso)
    seconds = (now - moment).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return str(int(seconds // 60)) + "m ago"
    if seconds < 86400:
        return str(int(seconds // 3600)) + "h ago"
    return str(int(seconds // 86400)) + "d ago"


def _when(iso):
    moment = _moment(iso)
    if moment is None:
        return str(iso)
    return moment.strftime("%Y-%m-%d %H:%MZ")


def _author_label(author):
    if not isinstance(author, dict):
        return "?"
    if author.get("username"):
        return "@" + author["username"]
    if author.get("id"):
        return "id:" + author["id"]
    return "?"


def _ref_target(url):
    try:
        segments = [part for part in urlsplit(url).path.split("/") if part]
    except (ValueError, AttributeError, TypeError):
        return str(url)
    if len(segments) == 3 and segments[1] == "status" and segments[0] != "i":
        return "@" + segments[0] + " · " + url
    return url


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value):
    return isinstance(value, str) and value != ""


def _length(duration_ms):
    seconds = duration_ms // 1000
    return str(seconds // 60) + ":" + format(seconds % 60, "02d")


def _one_line(text):
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _attachments(count, what):
    return "  · " + str(count) + (" attachment " if count == 1 else " attachments ") + what


def _media_lines(media):
    """One line per resolved attachment, with its alt text under it, then the pending and unavailable counts."""
    if not isinstance(media, list):
        return []
    lines = []
    waiting = missing = 0
    for item in media:
        if not isinstance(item, dict):
            continue
        if is_pending(item):
            waiting += 1
            continue
        if item.get("unavailable") is True:
            missing += 1
            continue
        kind = item.get("type")
        parts = [_MEDIA_LABELS.get(kind, "▣ media")]
        if _whole(item.get("width")) and _whole(item.get("height")) and item["width"] >= 1 and item["height"] >= 1:
            parts.append(str(item["width"]) + "×" + str(item["height"]))
        if _whole(item.get("duration_ms")) and item["duration_ms"] >= 0:
            parts.append(_length(item["duration_ms"]))
        if kind in _PREVIEWED:
            if _text(item.get("preview_image_url")):
                parts.append("preview " + item["preview_image_url"])
        elif _text(item.get("url")):
            parts.append(item["url"])
        lines.append("  " + " ".join(parts))
        if _text(item.get("alt_text")):
            lines.append('    alt: "' + _one_line(item["alt_text"]) + '"')
    if waiting:
        lines.append(_attachments(waiting, "pending"))
    if missing:
        lines.append(_attachments(missing, "unavailable on X"))
    return lines


def _post_block(item):
    header = [_author_label(item.get("author"))]
    if item.get("created_at"):
        header.append(_when(item["created_at"]))
    header.append(item.get("origin") or "?")
    header.append(item.get("url") or "?")
    line = " · ".join(header)
    marker = _UNAVAILABLE_MARKERS.get(item.get("unavailable"))
    if marker:
        line += " " + marker
    bookmark = item.get("bookmark")
    if item.get("origin") == "bookmark" and isinstance(bookmark, dict):
        marker = _STATE_MARKERS.get(bookmark.get("state"))
        if marker:
            line += " " + marker
    lines = [line]
    if item.get("text") is not None:
        lines.append(item["text"])
    # Media lines come after the body and before the reference lines (spec A1.2 "Plain output").
    lines.extend(_media_lines(item.get("media")))
    for ref in item.get("referenced") or []:
        verb = _VERBS.get(ref.get("type"), "references")
        ref_line = "  ↳ " + verb + " " + _ref_target(ref.get("url") or "")
        marker = _UNAVAILABLE_MARKERS.get(ref.get("unavailable"))
        if marker:
            ref_line += " " + marker
        lines.append(ref_line)
    return lines


def _user_block(user):
    header = ["@" + str(user.get("username") or "?")]
    if user.get("display_name"):
        header.append(user["display_name"])
    if user.get("url"):
        header.append(user["url"])
    lines = [" · ".join(header)]
    if user.get("description"):
        lines.append(user["description"])
    metrics = user.get("metrics")
    if isinstance(metrics, dict):
        lines.append("followers " + str(metrics.get("followers", 0)) + " · following "
                     + str(metrics.get("following", 0)) + " · posts " + str(metrics.get("posts", 0)))
    return lines


def _charged_counts(charged):
    if not isinstance(charged, dict):
        return ""
    return ("  charged post " + str(charged.get("post_read", 0)) + " owned " + str(charged.get("owned_read", 0))
            + " user " + str(charged.get("user_read", 0)) + " media " + str(charged.get("media_read", 0)))


def _usage_block(result):
    lines = []
    for day in result["local"]:
        lines.append(day["day"] + "  est $" + format(day["estimated_usd"], ".3f")
                     + "  reserved $" + format(day["reserved_usd"], ".3f")
                     + "  abandoned " + str(day["abandoned"])
                     + _charged_counts(day.get("charged")))
    lines.append("ceiling $" + format(result["ceiling_usd"], ".2f") + " · remaining $"
                 + format(result["remaining_usd"], ".2f"))
    x = result.get("x")
    if x is None:
        lines.append(result.get("x_note") or "X usage unavailable")
        return lines
    credits = x.get("credits") if isinstance(x, dict) else None
    data = credits.get("data") if isinstance(credits, dict) else None
    balance = data.get("total_balance") if isinstance(data, dict) else None
    if isinstance(balance, (int, float)) and not isinstance(balance, bool) and math.isfinite(balance):
        lines.append("X balance $" + format(balance, ".2f"))
    else:
        lines.append("X balance unknown")
    return lines


def _footer(result, now):
    lines = []
    for note in result.get("notes") or []:
        lines.append("note: " + note)
    for gap in result.get("gaps") or []:
        after = gap.get("after")
        where = "above the start" if after is None else "above " + after
        lines.append("gap: " + gap["missing"] + " (" + gap["reason"] + ") " + where)
    if result.get("next") is not None:
        lines.append("next: " + result["next"])
    if result.get("truncated"):
        lines.append("truncated: thread cap reached")
    if result.get("authors_pending"):
        lines.append("authors pending: " + str(result["authors_pending"]))
    if result.get("media_pending"):
        lines.append("media pending: " + str(result["media_pending"]))
    # bookmarks sync names the failure in its summary already (decision 26).
    if result.get("media_error") and "summary" not in result:
        lines.append("media lookup failed: " + str(result["media_error"]))
    if "last_sync" in result:
        lines.append("last sync " + ago(result["last_sync"], now))
    if "folder_synced_at" in result:
        lines.append("folder synced " + ago(result["folder_synced_at"], now))
    if "estimated_cost_usd" in result:
        lines.append("estimated cost $" + format(result["estimated_cost_usd"], ".3f"))
    return lines


def render_plain(result: dict, now: datetime) -> str:
    blocks = []
    summary = result.get("summary") or []
    if summary:
        blocks.append(list(summary))
    if "items" in result:
        items = result["items"] or []
        if not items and not summary:
            blocks.append(["no results"])
        blocks.extend(_post_block(item) for item in items)
    blocks.extend(_user_block(user) for user in result.get("users") or [])
    folders = result.get("folders") or []
    if folders:
        blocks.append([folder["id"] + " · " + str(folder.get("name") or "") + " · members synced "
                       + ago(folder.get("members_synced_at"), now) for folder in folders])
    referenced = result.get("referenced_posts") or []
    if referenced:
        blocks.append(["referenced:"])
        blocks.extend(_post_block(item) for item in referenced)
    if "local" in result:
        blocks.append(_usage_block(result))
    footer = _footer(result, now)
    if footer:
        blocks.append(footer)
    if not blocks:
        return ""
    return terminal_safe("\n\n".join("\n".join(block) for block in blocks) + "\n")


def render_json(result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False)
    # json.dumps escapes U+0000-U+001F but not DEL, C1 controls or bidirectional formatting characters;
    # inside a string \uXXXX is the same value.
    return "".join("\\u" + format(ord(c), "04x") if 0x7f <= ord(c) <= 0x9f or c in _BIDI else c
                   for c in text) + "\n"
