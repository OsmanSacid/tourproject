#!/usr/bin/env python3
"""Auto-Clipper: findet die krassesten Stellen eines Streams anhand des Chats,
schneidet sie als 9:16 Shorts/Reels/TikToks und schickt sie dir (Telegram/Discord).

Beispiel:
    python clipper.py https://www.twitch.tv/videos/123456789 --top 8
    python clipper.py https://www.youtube.com/watch?v=XXXX --top 5 --send telegram
    python clipper.py VOD_URL --only-detect          # nur Zeitstempel anzeigen
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from hype import ChatMessage, Moment, detect, fmt


# ---------------------------------------------------------------- Chat laden
def load_chat(source: str, cache: Path) -> list[ChatMessage]:
    """Chat aus Twitch/YouTube/Kick-VOD holen (oder aus lokaler JSON-Datei)."""
    if cache.exists():
        print(f"[chat] nutze Cache {cache}")
        return [ChatMessage(**m) for m in json.loads(cache.read_text())]

    if Path(source).is_file():
        raw = json.loads(Path(source).read_text())
        msgs = [ChatMessage(float(m["t"]), str(m.get("author", "?")), str(m["text"])) for m in raw]
    else:
        from chat_downloader import ChatDownloader
        print("[chat] lade Chat herunter (kann bei langen Streams ein paar Minuten dauern)...")
        msgs = []
        for i, m in enumerate(ChatDownloader().get_chat(source, message_types=["text_message"])):
            t = m.get("time_in_seconds")
            if t is None or not m.get("message"):
                continue
            msgs.append(ChatMessage(float(t), (m.get("author") or {}).get("name", "?"), m["message"]))
            if i % 5000 == 0 and i:
                print(f"  {i} Nachrichten, bei {fmt(t)}")
    cache.write_text(json.dumps([m.__dict__ for m in msgs]))
    print(f"[chat] {len(msgs)} Nachrichten")
    return msgs


# ---------------------------------------------------------------- Video
def download_segment(url: str, m: Moment, out: Path) -> Path:
    """Nur den benötigten Abschnitt des VODs laden (nicht den ganzen Stream)."""
    if out.exists():
        return out
    if Path(url).is_file():  # lokale Aufnahme (z.B. OBS-Datei)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{m.start:.2f}", "-i", url,
                        "-t", f"{m.end - m.start:.2f}", "-c:v", "libx264", "-preset", "veryfast",
                        "-c:a", "aac", str(out)], check=True)
        return out
    subprocess.run([
        sys.executable, "-m", "yt_dlp", url,
        "--download-sections", f"*{m.start:.0f}-{m.end:.0f}",
        "--force-keyframes-at-cuts",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "-o", str(out), "--quiet", "--no-warnings", "--progress",
    ], check=True)
    return out


def _esc(text: str) -> str:
    return re.sub(r"([\\:'%,\[\]])", r"\\\1", text)


def render_vertical(src: Path, dst: Path, title: str | None, layout: str, facecam: str | None) -> Path:
    """Macht aus 16:9 ein 1080x1920 Video.

    layout=blur    -> Gameplay in der Mitte, geblurrter Hintergrund (funktioniert immer)
    layout=split   -> Facecam oben, Gameplay unten (braucht --facecam x,y,w,h in Pixeln)
    layout=crop    -> Mitte des Bildes auf 9:16 zuschneiden (gut für Just-Chatting / IRL)
    """
    if layout == "split" and facecam:
        x, y, w, h = facecam.split(",")
        vf = (f"[0:v]split[a][b];"
              f"[a]crop={w}:{h}:{x}:{y},scale=1080:768:force_original_aspect_ratio=increase,crop=1080:768[cam];"
              f"[b]crop=ih*1080/1152:ih,scale=1080:1152[game];"
              f"[cam][game]vstack[v0]")
    elif layout == "crop":
        vf = "[0:v]crop=ih*9/16:ih,scale=1080:1920[v0]"
    else:
        vf = ("[0:v]split[a][b];"
              "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5,eq=brightness=-0.15[bg];"
              "[b]scale=1080:-2[fg];"
              "[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
    if title:
        font = next((f for f in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                                 "C:/Windows/Fonts/arialbd.ttf"] if Path(f).exists()), None)
        ff = f"fontfile='{_esc(font)}':" if font else ""
        vf += (f";[v0]drawtext={ff}text='{_esc(title)}':fontsize=64:fontcolor=white:"
               f"borderw=6:bordercolor=black:x=(w-text_w)/2:y=230[v]")
    else:
        vf += ";[v0]null[v]"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst),
    ], check=True)
    return dst


# ---------------------------------------------------------------- Senden
def send(path: Path, caption: str, target: str) -> None:
    import requests
    if target == "telegram":
        tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not (tok and chat):
            print("  ! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen – nicht gesendet")
            return
        with path.open("rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendVideo",
                              data={"chat_id": chat, "caption": caption[:1000], "supports_streaming": "true"},
                              files={"video": f}, timeout=300)
    elif target == "discord":
        hook = os.environ.get("DISCORD_WEBHOOK_URL")
        if not hook:
            print("  ! DISCORD_WEBHOOK_URL fehlt – nicht gesendet")
            return
        with path.open("rb") as f:
            r = requests.post(hook, data={"content": caption[:1900]}, files={"file": f}, timeout=300)
    else:
        return
    print(f"  -> {target}: {'ok' if r.ok else r.text[:200]}")


# ---------------------------------------------------------------- Main
def main() -> None:
    ap = argparse.ArgumentParser(description="Findet Hype-Momente im Chat und macht daraus Clips.")
    ap.add_argument("vod", help="Twitch/YouTube/Kick VOD-URL (oder Chat-JSON mit --video)")
    ap.add_argument("--video", help="Video-URL/Datei, falls 'vod' eine Chat-JSON ist")
    ap.add_argument("--top", type=int, default=8, help="Anzahl Clips (Standard 8)")
    ap.add_argument("--pre", type=float, default=25, help="Sekunden vor der Chat-Spitze")
    ap.add_argument("--post", type=float, default=10, help="Sekunden nach der Chat-Spitze")
    ap.add_argument("--layout", choices=["blur", "split", "crop"], default="blur")
    ap.add_argument("--facecam", help="Facecam-Bereich x,y,w,h für --layout split, z.B. 1500,780,420,300")
    ap.add_argument("--title", default="auto", help="Text oben im Video; 'auto', 'none' oder eigener Text")
    ap.add_argument("--send", choices=["none", "telegram", "discord"], default="none")
    ap.add_argument("--out", default="clips", help="Ausgabeordner")
    ap.add_argument("--only-detect", action="store_true", help="nur Momente finden, keine Videos")
    a = ap.parse_args()

    if not shutil.which("ffmpeg") and not a.only_detect:
        sys.exit("ffmpeg fehlt – bitte installieren (https://ffmpeg.org)")

    out = Path(a.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"\W+", "_", a.vod)[-40:]
    msgs = load_chat(a.vod, out / f"chat_{slug}.json")
    moments = detect(msgs, top_n=a.top, pre_roll=a.pre, post_roll=a.post)
    if not moments:
        sys.exit("Keine Hype-Momente gefunden (zu wenig Chat?)")

    print(f"\nTop {len(moments)} Momente:")
    for rank, m in enumerate(moments, 1):
        print(f" #{rank:<2} {m.label():<20} score {m.score:>5}  {m.msgs_per_min:>6} msg/min  {' '.join(m.top_words)}")
    (out / "moments.json").write_text(json.dumps([m.__dict__ for m in moments], indent=2, ensure_ascii=False))
    if a.only_detect:
        return

    video = a.video or a.vod
    for rank, m in enumerate(moments, 1):
        name = f"{rank:02d}_{m.label()}"
        print(f"\n[{rank}/{len(moments)}] {name}")
        try:
            raw = download_segment(video, m, out / "raw" / f"{name}.mp4")
            title = None if a.title == "none" else (
                a.title if a.title != "auto" else (m.top_words[0].upper() + " MOMENT" if m.top_words else None))
            final = render_vertical(raw, out / f"{name}.mp4", title, a.layout, a.facecam)
        except subprocess.CalledProcessError as e:
            print(f"  ! Fehler: {e}")
            continue
        print(f"  fertig: {final}")
        send(final, f"#{rank} {m.label()} | Hype {m.score} | Chat: {' '.join(m.top_words)}\n{video}", a.send)


if __name__ == "__main__":
    main()
