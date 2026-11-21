"""Marken-/Sponsor-Momente finden (z.B. Puffco-Kampagne).

Für Sponsor-Kampagnen reicht reiner Chat-Hype nicht: der Clip MUSS das Produkt zeigen.
Deshalb suchen wir Stellen, an denen der Streamer das Produkt erwähnt/benutzt:
  1. Transkript (Whisper über den ganzen Stream): "puffco", "dab", "peak", Husten ...
  2. optional Chat-Keywords ("puffco", "dab", "💨" ...)
  3. optional Chat-Hype als Bonus (damit die lustigsten Produkt-Momente oben landen)

Außerdem: layout "auto" – erkennt pro Clip, ob gerade Gameplay mit Facecam-Overlay
(-> split) oder Vollbild-Kamera (-> crop) läuft.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from hype import ChatMessage, Moment, message_hype

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# Gewichte: wie stark ein Wort auf "Produkt wird benutzt" hindeutet.
PRESETS: dict[str, dict[str, dict[str, float]]] = {
    "puffco": {
        "speech": {
            "puffco": 6, "puff": 2.5, "puffcos": 6, "peak": 2.5, "pivot": 2, "proxy": 2,
            "dab": 4, "dabs": 4, "dabbing": 4, "dabbed": 4, "rip": 1.5, "hit": 1.5,
            "rosin": 3, "wax": 2, "concentrate": 3, "terps": 3, "smoke": 1.5, "smoking": 1.5,
            "cough": 3, "coughing": 3, "lungs": 2, "cloud": 1.5, "clouds": 1.5,
            "death": 1.5, "died": 1, "sponsor": 2, "sponsored": 2,
        },
        "chat": {
            "puffco": 4, "dab": 3, "dabs": 3, "💨": 2, "🌬️": 2, "hit": 1, "rip": 1,
            "cough": 2, "coughing": 2, "smoke": 1, "lungs": 1.5, "peak": 1.5,
        },
    },
}
_WORD = re.compile(r"[\w']+|[^\w\s]", re.UNICODE)


def transcribe_full(video: str | Path, cache: Path, model: str = "base.en", lang: str = "en"
                    ) -> list[tuple[float, float, str]]:
    """Transkribiert den ganzen Stream (Datei oder URL) einmal und cached das Ergebnis.
    Für die Suche reicht ein kleines Modell; die Untertitel macht später 'small'."""
    if cache.exists():
        return [tuple(w) for w in json.loads(cache.read_text())]
    import numpy as np
    from faster_whisper import WhisperModel
    print(f"[speech] transkribiere ganzen Stream ({model}) – das dauert eine Weile ...")
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"], check=True, capture_output=True).stdout
    audio = np.frombuffer(pcm, np.float32)
    segs, _ = WhisperModel(model, device="cpu", compute_type="int8").transcribe(
        audio, language=lang, word_timestamps=True, vad_filter=True,
        condition_on_previous_text=False, beam_size=1)
    words = []
    for s in segs:
        words += [(round(w.start, 2), round(w.end, 2), w.word.strip()) for w in s.words or []]
    cache.write_text(json.dumps(words))
    print(f"[speech] {len(words)} Wörter")
    return words


def _norm(t: str) -> str:
    return re.sub(r"[^\w']", "", t.lower())


def find_moments(words: list[tuple[float, float, str]], preset: str,
                 msgs: list[ChatMessage] | None = None, top_n: int = 10,
                 bucket: float = 5.0, window: float = 30.0, min_gap: float = 60.0,
                 lead: float = 8.0, tail: float = 22.0) -> list[Moment]:
    """Momente, in denen das Produkt vorkommt. Score = Sprache + Chat-Keywords + Chat-Hype.

    Der Clip soll ab `lead` s vor dem ersten Schlüsselwort bis `tail` s danach gehen
    (erst wird angekündigt "lass uns einen hitten", dann kommt der Zug/Husten).
    `Moment.peak` wird so gesetzt, dass clipper.pick_window genau dieses Fenster nimmt.
    """
    p = PRESETS[preset]
    end_t = max([w[1] for w in words] + [m.t for m in msgs or []] + [0])
    n = int(end_t // bucket) + 1
    speech, chat_kw, hype = [0.0] * n, [0.0] * n, [0.0] * n
    hits: list[list[str]] = [[] for _ in range(n)]
    for s, _, t in words:
        k = _norm(t)
        if k in p["speech"]:
            i = int(s // bucket)
            speech[i] += p["speech"][k]
            hits[i].append(k)
    for m in msgs or []:
        i = int(m.t // bucket)
        toks = set(x.lower() for x in _WORD.findall(m.text))
        chat_kw[i] += sum(p["chat"].get(x, 0) for x in toks)
        hype[i] += message_hype(m.text)

    w = max(1, int(window // bucket))
    base_hype = sorted(hype)[len(hype) // 2] + 1 if msgs else 1

    def win(arr, i):
        return sum(arr[i:i + w])

    # Chat reagiert ~5-20s später -> Chat-Fenster etwas nach hinten verschoben
    lag = int(10 // bucket)
    score = []
    for i in range(n):
        s = win(speech, i)
        if s < 3:            # ohne Produkt-Erwähnung im Gesprochenen kein Kandidat
            score.append(0.0)
            continue
        c = win(chat_kw, i + lag)
        h = win(hype, i + lag) / (base_hype * w)
        score.append(s + 0.5 * c + 2.0 * min(h, 5))

    order = sorted(range(n), key=lambda i: score[i], reverse=True)
    picked: list[int] = []
    gap = int(min_gap // bucket)
    for i in order:
        if score[i] <= 0:
            break
        if all(abs(i - j) >= gap for j in picked):
            picked.append(i)
        if len(picked) >= top_n:
            break

    moments = []
    for i in picked:
        kw_t = next((ws for ws, _, t in words
                     if i * bucket <= ws < (i + w) * bucket and _norm(t) in p["speech"]), i * bucket)
        found = [k for j in range(i, min(n, i + w)) for k in hits[j]]
        top = sorted(set(found), key=found.count, reverse=True)[:5]
        start = max(0.0, kw_t - lead - 10)
        moments.append(Moment(
            peak=kw_t + tail + 1,            # pick_window: Fenster = peak-24 .. peak-1
            start=start,
            end=kw_t + tail + 12,
            score=round(score[i], 2),
            msgs_per_min=round(sum(1 for m in msgs or [] if i * bucket <= m.t < (i + w) * bucket)
                               / (window / 60), 1),
            top_words=top,
            sample=[" ".join(t for ws, _, t in words if kw_t - 5 <= ws <= kw_t + 15)],
        ))
    return moments


def detect_layout(src: Path, facecam: str) -> str:
    """'split', wenn an der Facecam-Kante ein harter Rand ist (Gameplay mit Overlay),
    sonst 'crop' (Vollbild-Kamera / Just Chatting)."""
    import numpy as np
    x, y, w, h = facecam.split(",")
    x, w, h = int(x), int(w), int(h)
    y = 0 if y == "auto" else int(y)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    dur = float(probe.stdout.strip() or 10)
    votes = []
    for frac in (0.15, 0.35, 0.5, 0.65, 0.85):
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{dur * frac:.2f}", "-i", str(src),
                              "-frames:v", "1", "-vf", "scale=1920:1080,format=gray", "-f", "rawvideo", "-"],
                             capture_output=True).stdout
        if len(raw) != 1920 * 1080:
            continue
        img = np.frombuffer(raw, np.uint8).reshape(1080, 1920).astype(np.int16)
        rows = slice(y + 5, y + h - 5)
        edge = np.abs(img[rows, x + w - 1] - img[rows, x + w + 1]).mean()
        ref = np.mean([np.abs(img[rows, c - 1] - img[rows, c + 1]).mean()
                       for c in (x + w - 40, x + w - 20, x + w + 20, x + w + 40)])
        votes.append(edge > 2.5 * ref + 4)
    return "split" if votes and sum(votes) * 2 > len(votes) else "crop"
