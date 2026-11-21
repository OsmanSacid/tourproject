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
def load_chat(source: str, cache: Path, start: float = 0, end: float | None = None) -> list[ChatMessage]:
    """Chat aus Twitch/YouTube-VOD holen (oder aus lokaler JSON-Datei)."""
    if cache.exists():
        print(f"[chat] nutze Cache {cache}")
        return [ChatMessage(**m) for m in json.loads(cache.read_text())]

    twitch = re.search(r"twitch\.tv/videos/(\d+)", source)
    if Path(source).is_file():
        if source.endswith(".gz"):
            import gzip
            raw = json.loads(gzip.open(source, "rt", encoding="utf-8").read())
        else:
            raw = json.loads(Path(source).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "comments" in raw:   # TwitchDownloader-Export
            from twitch_chat import load_twitchdownloader_json
            msgs = [ChatMessage(*c) for c in load_twitchdownloader_json(source)]
        else:
            msgs = [ChatMessage(float(m["t"]), str(m.get("author", "?")), str(m["text"])) for m in raw]
        msgs = [m for m in msgs if m.t >= start and (end is None or m.t <= end)]
    elif twitch:
        from twitch_chat import fetch_vod_chat
        print(f"[chat] lade Twitch-Chat ab {fmt(start)}" + (f" bis {fmt(end)}" if end else "") + " ...")
        msgs = []
        for t, user, text in fetch_vod_chat(twitch.group(1), start, end):
            msgs.append(ChatMessage(t, user, text))
            if len(msgs) % 20000 == 0:
                print(f"  {len(msgs)} Nachrichten, bei {fmt(t)}")
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
    try:
        return _download_hls_section(url, m.start, m.end, out)
    except Exception as e:  # kein HLS (z.B. YouTube) -> yt-dlp/ffmpeg
        print(f"  (HLS-Direktdownload nicht möglich: {e}; nutze yt-dlp)")
    subprocess.run([
        sys.executable, "-m", "yt_dlp", url,
        "--download-sections", f"*{m.start:.0f}-{m.end:.0f}",
        "--force-keyframes-at-cuts",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "-o", str(out), "--quiet", "--no-warnings", "--progress",
    ], check=True)
    return out


_HLS_CACHE: dict[str, tuple[str, list[tuple[float, float, str]]]] = {}


def _download_hls_section(url: str, start: float, end: float, out: Path) -> Path:
    """Lädt bei Twitch-VODs nur die .ts-Stücke, die den Abschnitt abdecken (per requests),
    und schneidet dann lokal mit ffmpeg exakt zu."""
    import requests
    import yt_dlp
    from urllib.parse import urljoin

    if url not in _HLS_CACHE:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "format": "best[height<=1080][protocol^=m3u8]/best[protocol^=m3u8]"}) as ydl:
            info = ydl.extract_info(url, download=False)
        m3u8 = info.get("url")
        if not m3u8 or "m3u8" not in (info.get("protocol") or ""):
            raise RuntimeError("kein m3u8")
        text = requests.get(m3u8, timeout=60).text
        segs, t, dur = [], 0.0, 0.0
        for line in text.splitlines():
            if line.startswith("#EXTINF:"):
                dur = float(line[8:].split(",")[0])
            elif line and not line.startswith("#"):
                segs.append((t, dur, urljoin(m3u8, line)))
                t += dur
        _HLS_CACHE[url] = (m3u8, segs)
    _, segs = _HLS_CACHE[url]
    need = [s for s in segs if s[0] + s[1] > start and s[0] < end]
    if not need:
        raise RuntimeError("Zeitraum außerhalb des VODs")
    ts = out.with_suffix(".ts")
    with requests.Session() as sess, ts.open("wb") as f:
        for _, _, seg_url in need:
            r = sess.get(seg_url, timeout=60)
            if r.status_code == 403 and "-unmuted" in seg_url:   # stummgeschaltete Teile
                r = sess.get(seg_url.replace("-unmuted", "-muted"), timeout=60)
            r.raise_for_status()
            f.write(r.content)
    offset = start - need[0][0]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{offset:.2f}", "-i", str(ts),
                    "-t", f"{end - start:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "aac", str(out)], check=True)
    ts.unlink(missing_ok=True)
    return out


def locate_facecam(src: Path, facecam: str) -> str:
    """'x,auto,w,h': sucht die y-Position des Facecam-Overlays selbst (Streamer verschieben es
    manchmal). Nimmt mehrere Frames und sucht die Zeile mit den stärksten Kanten oben+unten."""
    x, y, w, h = facecam.split(",")
    if y != "auto":
        return facecam
    import numpy as np
    x, w, h = int(x), int(w), int(h)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    dur = float(probe.stdout.strip() or 10)
    acc = None
    for frac in (0.2, 0.4, 0.6, 0.8):
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{dur * frac:.2f}", "-i", str(src),
                              "-frames:v", "1", "-vf", "scale=1920:1080,format=gray", "-f", "rawvideo", "-"],
                             capture_output=True).stdout
        if len(raw) != 1920 * 1080:
            continue
        img = np.frombuffer(raw, np.uint8).reshape(1080, 1920).astype(np.int16)
        # rechte Overlay-Kante: Sprung zwischen Spalte x+w-2 und x+w+1, pro Zeile
        e = np.abs(img[:, x + w - 2] - img[:, x + w + 1]).astype(float)
        acc = e if acc is None else acc + e
    if acc is None:
        raise RuntimeError("Facecam nicht gefunden")
    csum = np.concatenate([[0], np.cumsum(acc)])
    cand = csum[h:] - csum[:-h]                     # Summe über Fenster der Höhe h
    best = int(cand.argmax())
    return f"{x},{best},{w},{h}"


def _esc(text: str) -> str:
    return re.sub(r"([\\:'%,\[\]])", r"\\\1", text)


def render_vertical(src: Path, dst: Path, title: str | None, layout: str, facecam: str | None,
                    ss: float | None = None, t: float | None = None, cx: float | None = None) -> Path:
    """Macht aus 16:9 ein 1080x1920 Video.

    layout=blur    -> Gameplay in der Mitte, geblurrter Hintergrund (funktioniert immer)
    layout=split   -> Facecam oben, Gameplay unten (braucht --facecam x,y,w,h in Pixeln)
    layout=crop    -> Mitte des Bildes auf 9:16 zuschneiden (gut für Just-Chatting / IRL)
    """
    if layout == "split" and facecam:
        facecam = locate_facecam(src, facecam)
        x, y, w, h = facecam.split(",")
        vf = (f"[0:v]split[a][b];"
              f"[a]crop={w}:{h}:{x}:{y},scale=1080:768:force_original_aspect_ratio=increase,crop=1080:768[cam];"
              f"[b]crop=ih*1080/1152:ih,scale=1080:1152[game];"
              f"[cam][game]vstack[v0]")
    elif layout == "crop":
        # cx = Bildposition (0..1) des Gesichts -> Ausschnitt darauf zentrieren
        x = "(iw-ow)/2" if cx is None else f"max(0\\,min(iw-ow\\,iw*{cx:.3f}-ow/2))"
        vf = f"[0:v]crop=ih*9/16:ih:{x}:0,scale=1080:1920[v0]"
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
    trim = (["-ss", f"{ss:.2f}"] if ss else []) + (["-t", f"{t:.2f}"] if t else [])
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", *trim, "-i", str(src),
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst),
    ], check=True)
    return dst


def pick_window(words: list[tuple[float, float, str]], peak: float, total: float,
                min_len: float = 15, max_len: float = 30) -> tuple[float, float]:
    """Wählt den kurzen Ausschnitt (relativ zum Rohclip). Der krasse Moment passiert meist
    5-20s VOR der Chat-Spitze. Start/Ende werden auf Sprechpausen gelegt, damit kein Satz
    abgeschnitten wird."""
    start, end = max(0.0, peak - 24), min(total, peak - 1)
    # Schnittpunkte = Pausen zwischen Wörtern (Satzenden bevorzugt)
    cuts = [(0.0, True), (total, True)]
    for (s1, e1, w1), (s2, _, _) in zip(words, words[1:]):
        if s2 - e1 > 0.25:
            cuts.append(((e1 + s2) / 2, w1[-1:] in ".?!"))
    def snap(t, lo, hi):
        c = [(abs(x - t) - (1.5 if sent else 0), x) for x, sent in cuts if lo <= x <= hi]
        return min(c)[1] if c else t
    start = snap(start, start - 5, start + 4)
    end = snap(end, max(start + min_len, end - 3), min(start + max_len, end + 6, total))
    end = min(max(end, start + min_len), start + max_len, total)
    return start, end


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
    ap.add_argument("--layout", choices=["blur", "split", "crop", "auto"], default="blur",
                    help="auto = pro Clip: Gameplay mit Facecam -> split, Vollbild-Kamera -> crop")
    ap.add_argument("--facecam", help="Facecam-Bereich x,y,w,h für --layout split, z.B. 1500,780,420,300; y=auto sucht die Höhe selbst")
    ap.add_argument("--title", default="auto", help="Text oben im Video; 'auto', 'none' oder eigener Text")
    ap.add_argument("--max-len", type=float, default=30, help="max. Clip-Länge in s (0 = ungekürzt)")
    ap.add_argument("--min-len", type=float, default=15, help="min. Clip-Länge in s")
    ap.add_argument("--captions", action="store_true", help="Untertitel (was der Streamer sagt) einbrennen")
    ap.add_argument("--lang", default="en", help="Sprache für Untertitel (en, de, ...)")
    ap.add_argument("--send", choices=["none", "telegram", "discord"], default="none")
    ap.add_argument("--out", default="clips", help="Ausgabeordner")
    ap.add_argument("--start", type=float, default=0, help="nur Chat ab Sekunde X auswerten")
    ap.add_argument("--end", type=float, default=None, help="nur Chat bis Sekunde X auswerten")
    ap.add_argument("--only-detect", action="store_true", help="nur Momente finden, keine Videos")
    ap.add_argument("--brand", help="Sponsor-Modus: Momente mit Produkt finden (z.B. puffco)")
    ap.add_argument("--no-chat", action="store_true", help="ohne Chat arbeiten ('vod' ist dann das Video)")
    ap.add_argument("--scan-video", help="Datei/URL für die Sprach-Suche (z.B. 160p-Download des VODs)")
    ap.add_argument("--scan-model", default="base.en", help="Whisper-Modell für die Sprach-Suche")
    ap.add_argument("--titles", help="JSON {Clip-Name oder Zeit: Hook-Titel}; nur diese Clips werden gerendert")
    ap.add_argument("--review", action="store_true", help="nur laden+transkribieren+prüfen, review.json schreiben")
    ap.add_argument("--no-context", action="store_true", help="alte Schnittlogik statt Satzanfang-Kontext")
    ap.add_argument("--react-lag", type=float, default=4.0, help="Höhepunkt liegt so viele s vor der Chat-Spitze")
    ap.add_argument("--min-words", type=int, default=8, help="min. erkannte Wörter, sonst Clip verwerfen")
    ap.add_argument("--no-nsfw-check", action="store_true", help="18+-Bildprüfung (NudeNet) abschalten")
    a = ap.parse_args()

    if not shutil.which("ffmpeg") and not a.only_detect:
        sys.exit("ffmpeg fehlt – bitte installieren (https://ffmpeg.org)")

    out = Path(a.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"\W+", "_", a.vod)[-40:] + f"_{int(a.start)}_{int(a.end or 0)}"
    msgs = [] if a.no_chat else load_chat(a.vod, out / f"chat_{slug}.json", a.start, a.end)
    if a.brand:
        import brand
        scan = a.scan_video or a.video or a.vod
        tag = re.sub(r"\W+", "_", str(scan))[-40:]
        words_all = brand.transcribe_full(scan, out / f"speech_{tag}.json", a.scan_model, a.lang)
        words_all = [w for w in words_all if w[0] >= a.start and (a.end is None or w[0] <= a.end)]
        loud = None
        if brand.PRESETS[a.brand].get("loud_weight"):
            lcache = out / f"loud_{tag}.json"
            if not lcache.exists():
                print("[audio] messe Lautstärke ...")
                lcache.write_text(json.dumps(brand.loudness(scan)))
            loud = json.loads(lcache.read_text())
        moments = brand.find_moments(words_all, a.brand, msgs, top_n=a.top, loud=loud)
    else:
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
    if a.layout == "auto" and not a.facecam:
        sys.exit("--layout auto braucht --facecam x,y,w,h")
    titles = json.loads(Path(a.titles).read_text()) if a.titles else {}
    review = []
    for rank, m in enumerate(moments, 1):
        name = f"{rank:02d}_{m.label()}"
        hook = next((v for k, v in titles.items() if k in name), None)
        if titles and hook is None and not a.review:
            continue                      # mit --titles nur die ausgewählten Clips rendern
        print(f"\n[{rank}/{len(moments)}] {name}")
        try:
            raw = download_segment(video, m, out / "raw" / f"{name}.mp4")
            title = None if a.title == "none" or hook else (
                a.title if a.title != "auto" else (m.top_words[0].upper() + " MOMENT" if m.top_words else None))
            ss = dur = None
            words = []
            if a.captions or a.max_len or a.review:
                import captions
                wcache = raw.with_suffix(".words.json")
                if wcache.exists():
                    words = [tuple(w) for w in json.loads(wcache.read_text())]
                else:
                    words = captions.transcribe(raw, lang=a.lang)
                    wcache.write_text(json.dumps(words))
            if a.max_len:
                if a.no_context:
                    ss, e = pick_window(words, m.peak - m.start, m.end - m.start, a.min_len, a.max_len)
                else:  # Kontext: Satzanfang als Start, Reaktion am Ende drin
                    import quality
                    ss, e = quality.context_window(words, m.peak - m.start - a.react_lag, m.end - m.start,
                                                   a.min_len, a.max_len)
                dur = e - ss
                words = [(w0 - ss, w1 - ss, t) for w0, w1, t in words if w0 >= ss - 0.1 and w1 <= e + 0.1]
                print(f"  Ausschnitt: {dur:.0f}s")
            text = " ".join(t for _, _, t in words)
            import quality
            bad_words = quality.nsfw_words(words)
            nude = []
            if not a.no_nsfw_check:
                cut = raw.with_suffix(".cut.mp4")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ss or 0:.2f}", "-i", str(raw),
                                *(["-t", f"{dur:.2f}"] if dur else []), "-an", "-c:v", "libx264",
                                "-preset", "ultrafast", str(cut)], check=True)
                nude = quality.nsfw_frames(cut)
                cut.unlink(missing_ok=True)
            problems = []
            if nude:
                problems.append(f"18+ Bild ({nude[0][1]} bei {nude[0][0]:.0f}s)")
            if bad_words:
                problems.append(f"18+ Wörter: {', '.join(bad_words)}")
            if a.captions and len(words) < a.min_words:
                problems.append(f"zu wenig Sprache für Untertitel ({len(words)} Wörter)")
            review.append({"name": name, "start": m.start + (ss or 0), "len": round(dur or 0, 1),
                           "score": m.score, "problems": problems, "text": text})
            if a.review:
                print(f"  {'; '.join(problems) or 'ok'} | {text[:160]}")
                continue
            if problems:
                print(f"  ! übersprungen: {'; '.join(problems)}")
                continue
            layout = a.layout
            if layout == "auto":
                import brand
                layout = brand.detect_layout(raw, a.facecam)
                print(f"  Layout: {layout}")
            cx = quality.face_center(raw, ss or 0, dur) if layout == "crop" else None
            final = render_vertical(raw, out / f"{name}.mp4", title, layout, a.facecam, ss, dur, cx)
            if a.captions or hook:
                import captions
                y = 768 if layout == "split" else 1400
                tmp = final.with_suffix(".nocap.mp4")
                final.replace(tmp)
                captions.burn(tmp, captions.build_ass(words if a.captions else [], y=y, title=hook), final)
                tmp.unlink()
            quality.contact_sheet(final, final.with_suffix(".jpg"))
        except subprocess.CalledProcessError as e:
            print(f"  ! Fehler: {e}")
            continue
        print(f"  fertig: {final}")
        send(final, f"#{rank} {m.label()} | Hype {m.score} | Chat: {' '.join(m.top_words)}\n{video}", a.send)
    if review:
        (out / "review.json").write_text(json.dumps(review, indent=2, ensure_ascii=False))
        if a.review:
            print(f"\nReview gespeichert: {out / 'review.json'} – Titel in eine JSON schreiben und mit --titles rendern")

if __name__ == "__main__":
    main()
