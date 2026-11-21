"""Qualitäts-Regeln für ALLE Clips (Feedback vom Nutzer):

1. Kontext: Clip beginnt am Anfang eines Satzes/einer Situation, nicht mitten im Geschehen.
2. Kein 18+ / keine Nacktheit: NudeNet prüft jedes Bild (1 fps); Sex-Wörter im Transkript.
3. Titel als Hook am Anfang (z.B. "The NPCs in GTA 6 are crazy 💀"), Box oben im Video.
4. Untertitel müssen da sein: Clips mit zu wenig erkannter Sprache werden verworfen.
5. Facecam muss stimmen: Kontaktabzug (contact sheet) zum Prüfen vor dem Versand.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# NudeNet-Klassen, die einen Clip sofort ausschließen
_NSFW = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
         "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
# Wörter, bei denen der Clip zur manuellen Prüfung markiert/verworfen wird
_NSFW_WORDS = re.compile(r"\b(porn\w*|onlyfans|nude\w*|naked|titt\w*|boob\w*|pussy|dick|cock|"
                         r"sex|sexy|strip\w*|thong|cum\w*|horny|twerk\w*|stripper\w*|booty|ass|jerk\w* off|nipple\w*)\b", re.I)


def nsfw_frames(video: Path, fps: float = 1.0, thresh: float = 0.45) -> list[tuple[float, str, float]]:
    """Gibt [(sekunde, klasse, score)] für jedes Bild mit Nacktheit zurück."""
    import tempfile
    from nudenet import NudeDetector
    det = NudeDetector()
    hits = []
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf", f"fps={fps},scale=640:-2",
                        f"{d}/f_%04d.jpg"], check=True)
        for p in sorted(Path(d).glob("f_*.jpg")):
            t = (int(p.stem[2:]) - 1) / fps
            for r in det.detect(str(p)):
                if r["class"] in _NSFW and r["score"] >= thresh:
                    hits.append((t, r["class"], round(r["score"], 2)))
    return hits


def nsfw_words(words: list[tuple[float, float, str]]) -> list[str]:
    return sorted({w for _, _, t in words for w in _NSFW_WORDS.findall(t)})


def context_window(words: list[tuple[float, float, str]], peak: float, total: float,
                   min_len: float = 20, max_len: float = 40, react: float = 3.0) -> tuple[float, float]:
    """Wählt Start/Ende so, dass der Zuschauer versteht, was passiert:
    - Ende: kurz NACH dem Höhepunkt (Reaktion bleibt drin), auf einer Sprechpause.
    - Start: am Beginn eines Satzes, so früh wie möglich (Kontext/Setup), aber <= max_len.
    `peak` = Zeitpunkt des Höhepunkts relativ zum Rohclip."""
    end_target = min(total, peak + react)
    gaps = []                                   # (zeit, pausenlänge, satzende davor?)
    for (s1, e1, w1), (s2, _, _) in zip(words, words[1:]):
        if s2 - e1 > 0.3:
            gaps.append(((e1 + s2) / 2, s2 - e1, w1[-1:] in ".?!"))
    # Ende: erste Pause nach end_target (max. +6 s), sonst end_target
    ends = [g[0] for g in gaps if end_target <= g[0] <= end_target + 6]
    end = ends[0] if ends else end_target
    end = min(end, total)
    # Start: Kandidaten = Pausen (Satzanfänge) im Bereich [end-max_len, end-min_len]
    lo, hi = max(0.0, end - max_len), max(0.0, end - min_len)
    cands = [(g[0], g[1] + (1.0 if g[2] else 0)) for g in gaps if lo <= g[0] <= hi]
    if lo <= 0.5:
        cands.append((0.0, 0.5))
    if cands:
        # lange Pause / Satzende = guter Einstieg; frühere Starts leicht bevorzugt (mehr Kontext)
        start = max(cands, key=lambda c: c[1] + 0.03 * (hi - c[0]))[0]
    else:
        start = lo
    return start, end


def title_ass(text: str, y: int = 150, size: int = 66, font: str = "Inter ExtraBold") -> tuple[str, str]:
    """ASS-Zeilen für den Hook-Titel: schwarze Schrift auf weißer Box, ganzer Clip.
    Wird zusammen mit den Untertiteln in einem Durchgang eingebrannt."""
    words, lines, cur = text.split(), [], ""
    for w in words:                              # Umbruch bei ~22 Zeichen
        if cur and len(cur) + 1 + len(w) > 22:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    body = r"\N".join(lines)
    style = (f"Style: Title,{font},{size},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,1,0,0,0,"
             f"100,100,0,0,3,18,0,8,60,60,{y},1")
    event = f"Dialogue: 1,0:00:00.00,9:00:00.00,Title,,0,0,0,,{body}"
    return style, event


def contact_sheet(video: Path, out: Path, n: int = 6) -> Path:
    """Kleine Übersicht (n Bilder nebeneinander) zum Prüfen von Facecam, Titel, Untertiteln."""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout or 10)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf",
                    f"fps={n / dur:.4f},scale=240:-2,tile={n}x1", "-frames:v", "1", str(out)], check=True)
    return out


def face_center(video: Path, ss: float = 0, dur: float | None = None, n: int = 6) -> float | None:
    """Horizontale Position (0..1) des größten Gesichts (Median über n Bilder) – damit
    beim Vollbild-Crop der Streamer in der Mitte ist und nicht am Rand abgeschnitten wird."""
    import statistics
    import tempfile
    from nudenet import NudeDetector
    det = NudeDetector()
    if dur is None:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout or 10) - ss
    xs = []
    with tempfile.TemporaryDirectory() as d:
        for i in range(n):
            t = ss + dur * (i + 0.5) / n
            f = f"{d}/{i}.jpg"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=960:540", f], check=False)
            faces = [r for r in det.detect(f) if r["class"].startswith("FACE") and r["score"] > 0.4]
            if faces:
                x, _, w, h = max(faces, key=lambda r: r["box"][2] * r["box"][3])["box"]
                xs.append((x + w / 2) / 960)
    return statistics.median(xs) if xs else None
