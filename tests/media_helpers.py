"""Media fixtures and helpers for the Amendment 1 tests. No network and no real HOME."""
import copy

from tests.helpers import fixture, response

MEDIA_FIELDS = "media_key,type,url,preview_image_url,width,height,alt_text,duration_ms,variants"
TODAY = "2026-10-02"

# The fixture's includes.media entries, by key, as X sends them.
RAW = dict((item["media_key"], item) for item in fixture("media_lookup.json")["includes"]["media"])

# The same entries as stored media items.
PHOTO = {"key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/media/p1.jpg", "width": 1200, "height": 800,
         "alt_text": "Diagram of the agent loop"}
VIDEO = {"key": "13_2", "type": "video", "preview_image_url": "https://pbs.twimg.com/ext_tw_video_thumb/2/pu/img/v.jpg",
         "width": 1280, "height": 720, "duration_ms": 47000,
         "variants": [
             {"content_type": "video/mp4", "url": "https://video.twimg.com/ext_tw_video/2/pu/vid/1280x720/v.mp4",
              "bit_rate": 2176000},
             {"content_type": "video/mp4", "url": "https://video.twimg.com/ext_tw_video/2/pu/vid/480x270/v.mp4",
              "bit_rate": 632000},
             {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/ext_tw_video/2/pu/pl/v.m3u8"},
         ]}
GIF = {"key": "16_3", "type": "animated_gif", "preview_image_url": "https://pbs.twimg.com/tweet_video_thumb/g.jpg",
       "width": 498, "height": 280,
       "variants": [{"content_type": "video/mp4", "url": "https://video.twimg.com/tweet_video/g.mp4", "bit_rate": 0}]}
PHOTO_4A = {"key": "3_4a", "type": "photo", "url": "https://pbs.twimg.com/media/p4a.jpg", "width": 640, "height": 480}


def raw_photo(key):
    """A plain photo entry for a key the fixture does not hold."""
    return {"media_key": key, "type": "photo", "url": "https://pbs.twimg.com/media/" + key + ".jpg",
            "width": 10, "height": 10}


def photo(key):
    """raw_photo(key) as a stored media item."""
    return {"key": key, "type": "photo", "url": "https://pbs.twimg.com/media/" + key + ".jpg", "width": 10, "height": 10}


def pending(keys):
    return [{"key": key} for key in keys]


def lookup(*posts, omit=(), errors=None):
    """A 200 media lookup. posts are (post_id, [media keys]) pairs; includes.media holds an entry for every
    listed key except those in omit: the fixture's entry when it has one, otherwise raw_photo(key)."""
    data = []
    media = []
    for post_id, keys in posts:
        data.append({"id": post_id, "text": "lookup text " + post_id, "edit_history_tweet_ids": [post_id],
                     "attachments": {"media_keys": list(keys)}})
        for key in keys:
            if key not in omit:
                media.append(copy.deepcopy(RAW[key]) if key in RAW else raw_photo(key))
    payload = {"data": data, "includes": {"media": media}}
    if errors:
        payload["errors"] = errors
    return response(200, payload)


def not_found(post_id):
    """An errors[] entry X sends for a deleted post in an ids= lookup."""
    return {"value": post_id, "detail": "Could not find tweet with ids: [" + post_id + "].",
            "title": "Not Found Error", "resource_type": "tweet", "parameter": "ids", "resource_id": post_id,
            "type": "https://api.twitter.com/2/problems/resource-not-found"}


def stored(post_id, media, text=None, author_id="42"):
    """A normalized post as commands store it, with the given media list (None for no media)."""
    item = {"id": post_id, "url": "https://x.com/i/status/" + post_id,
            "text": text if text is not None else "stored text " + post_id, "author": {"id": author_id},
            "created_at": "2026-10-01T10:00:00Z", "conversation_id": post_id, "lang": "en",
            "metrics": {"likes": 1, "reposts": 0, "replies": 0, "quotes": 0},
            "origin": "live", "source": "x", "unavailable": None}
    if media is not None:
        item["media"] = copy.deepcopy(media)
    return item


def charge(store, kind, ids, day=TODAY):
    """Record resources as already charged on day, as an earlier request would have."""
    for resource_id in ids:
        store.conn.execute("INSERT OR IGNORE INTO charged (utc_day, kind, resource_id) VALUES (?, ?, ?)",
                           (day, kind, resource_id))
