"""Lädt den Chat-Replay eines Twitch-VODs (wie TwitchDownloader).

Nutzt die öffentliche GraphQL-Schnittstelle der Twitch-Webseite.
Bei langen Streams kann das dauern (~1-2 Mio. Nachrichten pro 10h bei großen Streamern),
deshalb kann man mit start/end nur einen Abschnitt laden.
"""
from __future__ import annotations

import json
import time
from typing import Iterator

import requests

GQL = "https://gql.twitch.tv/gql"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mumb2l6"
QUERY_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def _post(sess: requests.Session, variables: dict) -> dict:
    body = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": variables,
             "extensions": {"persistedQuery": {"version": 1, "sha256Hash": QUERY_HASH}}}]
    for attempt in range(6):
        r = sess.post(GQL, data=json.dumps(body), timeout=30)
        if r.ok:
            data = r.json()[0]
            if "errors" not in data:
                return data["data"]["video"]["comments"]
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Twitch-Chat konnte nicht geladen werden: {r.status_code} {r.text[:200]}")


def fetch_vod_chat(vod_id: str, start: float = 0, end: float | None = None) -> Iterator[tuple[float, str, str]]:
    """Gibt (sekunde, user, text) zurück."""
    sess = requests.Session()
    sess.headers.update({"Client-ID": WEB_CLIENT_ID, "Content-Type": "text/plain;charset=UTF-8"})
    comments = _post(sess, {"videoID": vod_id, "contentOffsetSeconds": int(start)})
    while True:
        edges = comments.get("edges") or []
        for e in edges:
            n = e["node"]
            t = float(n["contentOffsetSeconds"])
            if end is not None and t > end:
                return
            text = "".join(f.get("text", "") for f in (n.get("message") or {}).get("fragments") or [])
            user = ((n.get("commenter") or {}).get("login")) or "?"
            if text:
                yield t, user, text
        if not edges or not comments.get("pageInfo", {}).get("hasNextPage"):
            return
        comments = _post(sess, {"videoID": vod_id, "cursor": edges[-1]["cursor"]})


def load_twitchdownloader_json(path: str) -> list[tuple[float, str, str]]:
    """Import einer Chat-Datei aus TwitchDownloader (Chat Download -> JSON)."""
    import gzip
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for c in raw.get("comments", []):
        msg = c.get("message") or {}
        text = msg.get("body") or "".join(f.get("text", "") for f in msg.get("fragments") or [])
        who = (c.get("commenter") or {}).get("name") or (c.get("commenter") or {}).get("display_name") or "?"
        out.append((float(c["content_offset_seconds"]), who, text))
    return out
