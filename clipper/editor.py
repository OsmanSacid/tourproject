#!/usr/bin/env python3
"""Editor-Workflow: erst Kandidaten vorbereiten und ansehen, dann gezielt schneiden.

1) prepare  – Kandidaten (Chat-Hype und/oder Twitch-Top-Clips) mit viel Vorlauf laden,
              transkribieren, auf 18+-Inhalte prüfen, Kontaktbogen (Bild alle 3s) bauen.
2) (Mensch/Claude schaut sich review/NN/sheet.jpg + transcript.txt an und schreibt edits.json)
3) render   – edits.json -> fertige Reels mit Titel, Facecam-Split (oder Fallback), Untertiteln.

edits.json:
[
  {"id": "03", "start": 12.0, "end": 38.5, "title": "Kai just hit 22 MILLION followers"},
  ...
]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import captions
from clipper import download_segment, load_chat, locate_facecam, render_vertical
from hype import Moment, detect, fmt

# Nur eindeutig explizite Klassen – Bikini, Arme, Beine usw. sind ok
EXPLICIT = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
            "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
_NUDE = None


def nsfw_scan(video: Path, every: float = 1.0, min_score: float = 0.45) -> list[tuple[float, str, float]]:
    """Gibt (zeit, klasse, score) für explizite Treffer zurück."""
    global _NUDE
    import tempfile
    if _NUDE is None:
        from nudenet import NudeDetector
        _NUDE = NudeDetector()
    hits = []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf", f"fps=1/{every},scale=960:-2",
                        f"{td}/f_%04d.jpg"], check=True)
        for f in sorted(Path(td).glob("f_*.jpg")):
            t = (int(f.stem[2:]) - 1) * every
            for d in _NUDE.detect(str(f)):
                if d["class"] in EXPLICIT and d["score"] >= min_score:
                    hits.append((t, d["class"], round(d["score"], 2)))
    return hits


def contact_sheet(video: Path, dst: Path, every: float = 3.0) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf",
                    f"fps=1/{every},scale=384:216,"
                    "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
                    f"text='%{{eif\\:n*{every}\\:d}}s':x=6:y=6:fontsize=26:fontcolor=yellow:borderw=3,"
                    "tile=5x6", "-frames:v", "1", "-q:v", "3", str(dst)], check=True)


def transcript_text(words) -> str:
    lines, cur, start = [], [], None
    for s, e, w in words:
        if cur and (s - prev_end > 0.7 or len(cur) >= 14):
            lines.append(f"[{start:5.1f}] {' '.join(cur)}")
            cur = []
        if not cur:
            start = s
        cur.append(w)
        prev_end = e
    if cur:
        lines.append(f"[{start:5.1f}] {' '.join(cur)}")
    return "\n".join(lines)


def prepare(a) -> None:
    out = Path(a.out)
    rev = out / "review"
    rev.mkdir(parents=True, exist_ok=True)
    cands = []
    if a.chat:
        msgs = load_chat(a.chat, out / "chat_cache.json", a.start, a.end)
        for m in detect(msgs, top_n=a.top, pre_roll=a.pre, post_roll=15):
            cands.append({"kind": "chat", "peak": m.peak, "start": m.start, "end": m.end,
                          "info": f"Chat-Hype {m.score} | {' '.join(m.top_words)} | {fmt(m.peak)}",
                          "chat": m.sample})
    if a.clips_of:
        import yt_dlp
        url = f"https://www.twitch.tv/{a.clips_of}/clips?filter=clips&range={a.range}"
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "playlistend": a.top_clips}) as ydl:
            for e in ydl.extract_info(url, download=False).get("entries") or []:
                cands.append({"kind": "clip", "url": e["url"],
                              "info": f"Twitch-Clip '{e.get('title')}' {e.get('view_count')} Views"})
    index = []
    for i, c in enumerate(cands, 1):
        cid = f"{i:02d}"
        d = rev / cid
        d.mkdir(exist_ok=True)
        raw = d / "raw.mp4"
        try:
            if not raw.exists():
                if c["kind"] == "chat":
                    download_segment(a.video, Moment(c["peak"], c["start"], c["end"], 0, 0), raw)
                else:
                    import yt_dlp
                    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "outtmpl": str(raw),
                                           "format": "best[height<=1080]"}) as ydl:
                        ydl.download([c["url"]])
            words = captions.transcribe(raw, lang=a.lang)
            (d / "words.json").write_text(json.dumps(words))
            (d / "transcript.txt").write_text(transcript_text(words))
            contact_sheet(raw, d / "sheet.jpg")
            hits = nsfw_scan(raw)
            (d / "nsfw.json").write_text(json.dumps(hits))
        except Exception as ex:
            print(f"[{cid}] Fehler: {ex}")
            continue
        flag = f" | 18+ bei {sorted({int(h[0]) for h in hits})}s" if hits else ""
        index.append(f"{cid}: {c['info']}{flag}")
        print(index[-1])
    (rev / "index.txt").write_text("\n".join(index))


def snap_cut(words, t: float, before: bool, reach: float = 1.5) -> float:
    """Schnitt nicht mitten im Wort: auf die nächste Lücke zwischen Wörtern legen."""
    for s, e, _ in words:
        if s < t < e:  # Schnitt liegt in einem Wort
            return max(0.0, s - 0.15) if before else e + 0.15
    return t


def render(a) -> None:
    out = Path(a.out)
    rev = out / "review"
    edits = json.loads(Path(a.edits).read_text())
    for n, ed in enumerate(edits, 1):
        d = rev / ed["id"]
        raw = d / "raw.mp4"
        ss, end = float(ed["start"]), float(ed["end"])
        allw = json.loads((d / "words.json").read_text())
        ss, end = snap_cut(allw, ss, before=True), snap_cut(allw, end, before=False)
        dur = end - ss
        hits = [h for h in json.loads((d / "nsfw.json").read_text()) if ss - 1 <= h[0] <= end + 1]
        if hits and not ed.get("ignore_nsfw"):
            print(f"[{ed['id']}] übersprungen: 18+-Inhalt bei {[h[0] for h in hits]}")
            continue
        slug = re.sub(r"\W+", "_", ed["title"])[:40].strip("_")
        dst = out / f"{n:02d}_{slug}.mp4"
        cam = locate_facecam(raw, a.facecam, ss, dur) if a.facecam else None
        layout = "split" if cam else "blur"
        tmp = dst.with_suffix(".tmp.mp4")
        render_vertical(raw, tmp, None, layout, cam, ss, dur)
        words = [(w0 - ss, w1 - ss, t) for w0, w1, t in json.loads((d / "words.json").read_text())
                 if w0 >= ss - 0.1 and w1 <= end + 0.2]
        cap_y = 768 if layout == "split" else 1450
        captions.burn(tmp, captions.build_ass(words, y=cap_y, title=ed["title"], duration=dur), dst)
        tmp.unlink()
        print(f"fertig: {dst}  ({dur:.0f}s, {layout}, {len(words)} Wörter)")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--chat", help="Chat-Datei (TwitchDownloader .json/.json.gz)")
    p.add_argument("--video", help="VOD-URL zum Chat")
    p.add_argument("--clips-of", help="Twitch-Kanal: Top-Clips als Kandidaten")
    p.add_argument("--range", default="7d")
    p.add_argument("--top", type=int, default=15, help="Anzahl Chat-Kandidaten")
    p.add_argument("--top-clips", type=int, default=10)
    p.add_argument("--pre", type=float, default=55, help="Vorlauf vor dem Chat-Fenster (Kontext)")
    p.add_argument("--start", type=float, default=0)
    p.add_argument("--end", type=float, default=None)
    p.add_argument("--lang", default="en")
    p.add_argument("--out", required=True)
    r = sub.add_parser("render")
    r.add_argument("--edits", required=True)
    r.add_argument("--facecam", default="0,auto,392,220")
    r.add_argument("--out", required=True)
    a = ap.parse_args()
    prepare(a) if a.cmd == "prepare" else render(a)


if __name__ == "__main__":
    main()
