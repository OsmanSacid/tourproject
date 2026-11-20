"""Erkennt "crazy" Momente anhand der Chat-Aktivität.

Idee: Chat explodiert kurz NACH einem krassen Moment. Wir zählen pro Zeitfenster
Nachrichten, gewichten Hype-Emotes/Wörter, Caps, "?????" usw. und vergleichen das
mit dem normalen Chat-Level (rollierender Median). Die stärksten Ausreißer = Clips.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

# Emotes / Wörter, die typischerweise bei krassen Momenten gespammt werden.
# Gewicht = wie stark sie zum Hype-Score beitragen.
HYPE_TOKENS: dict[str, float] = {
    # Lachen
    "kekw": 2.0, "lul": 1.5, "lulw": 1.5, "omegalul": 2.5, "lmao": 1.5, "lmfao": 2.0,
    "xd": 1.0, "😂": 1.5, "🤣": 1.5, "icant": 2.0, "dead": 1.0, "💀": 2.0,
    # Hype / Hammer-Play
    "pog": 2.0, "pogchamp": 2.0, "poggers": 2.0, "pogu": 2.0, "w": 1.5, "ww": 1.5,
    "www": 1.5, "goat": 1.5, "insane": 2.0, "crazy": 2.0, "clip": 3.0, "clipit": 3.0,
    "clipped": 3.0, "letsgo": 2.0, "ez": 1.0, "🔥": 1.5, "geil": 1.5, "krass": 2.0,
    "brutal": 2.0, "digga": 1.0, "ehrenmann": 1.5,
    # Schock / Fail
    "wtf": 2.0, "omg": 1.5, "monkas": 1.5, "monkaw": 2.0, "pausechamp": 1.5,
    "l": 1.0, "rip": 1.5, "f": 1.0, "sadge": 1.0, "😱": 1.5, "😳": 1.0, "nein": 1.0,
}
_WORD = re.compile(r"[\wäöüß]+|[^\w\s]", re.UNICODE)


@dataclass
class ChatMessage:
    t: float          # Sekunden seit Stream-Start
    author: str
    text: str


@dataclass
class Moment:
    peak: float                 # Zeitpunkt der Chat-Spitze (s)
    start: float                # Clip-Start (s)
    end: float                  # Clip-Ende (s)
    score: float
    msgs_per_min: float
    top_words: list[str] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{fmt(self.start)}-{fmt(self.end)}"


def fmt(sec: float) -> str:
    sec = int(max(sec, 0))
    return f"{sec // 3600:d}h{sec % 3600 // 60:02d}m{sec % 60:02d}s"


def message_hype(text: str) -> float:
    """Hype-Wert einer einzelnen Nachricht (Basis 1.0 + Boni)."""
    score = 1.0
    toks = [t.lower() for t in _WORD.findall(text)]
    score += sum(HYPE_TOKENS.get(t, 0.0) for t in set(toks))
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        score += 1.0                                   # CAPS LOCK
    if re.search(r"\?{3,}|!{3,}", text):
        score += 1.0                                   # ????? / !!!!!
    if re.search(r"(\w)\1{3,}", text, re.I):
        score += 0.5                                   # NEEEEIN, LOOOL
    return min(score, 8.0)


def detect(
    messages: list[ChatMessage],
    top_n: int = 10,
    bucket: float = 5.0,
    window: float = 20.0,
    baseline: float = 300.0,
    pre_roll: float = 25.0,
    post_roll: float = 10.0,
    min_gap: float = 60.0,
) -> list[Moment]:
    """Findet die top_n Hype-Momente.

    bucket    – Auflösung in Sekunden
    window    – über wie viele Sekunden der Hype geglättet wird
    baseline  – Länge des "normalen" Chat-Levels zum Vergleich
    pre_roll  – Chat reagiert verzögert, Clip startet so viele Sekunden VOR der Spitze
    post_roll – Sekunden nach der Spitze
    min_gap   – Mindestabstand zwischen zwei Clips
    """
    if not messages:
        return []
    messages = sorted(messages, key=lambda m: m.t)
    n = int(messages[-1].t // bucket) + 1
    hype = [0.0] * n
    count = [0] * n
    authors: list[set[str]] = [set() for _ in range(n)]
    for m in messages:
        i = int(m.t // bucket)
        hype[i] += message_hype(m.text)
        count[i] += 1
        authors[i].add(m.author)

    # Gleitende Summe über `window` -> Hype pro Fenster; Spam einzelner User dämpfen
    w = max(1, int(window // bucket))
    smooth = []
    for i in range(n):
        lo = max(0, i - w + 1)
        raw = sum(hype[lo:i + 1])
        msgs = sum(count[lo:i + 1])
        uniq = len(set().union(*authors[lo:i + 1]))
        # viele verschiedene Leute = echter Hype, ein Spammer = kaum Wert
        smooth.append(raw * (0.4 + 0.6 * (uniq / msgs if msgs else 0)))

    # Vergleich mit rollierendem Median der letzten `baseline` Sekunden
    b = max(1, int(baseline // bucket))
    score = []
    for i in range(n):
        past = smooth[max(0, i - b):i] or [smooth[i]]
        med = statistics.median(past)
        score.append((smooth[i] - med) / (med + 5.0))

    # Spitzen auswählen (Non-Maximum-Suppression mit min_gap)
    order = sorted(range(n), key=lambda i: score[i], reverse=True)
    picked: list[int] = []
    gap = int(min_gap // bucket)
    for i in order:
        if score[i] <= 0.5:
            break
        if all(abs(i - j) >= gap for j in picked):
            picked.append(i)
        if len(picked) >= top_n:
            break

    moments = []
    for i in picked:
        peak = (i + 1) * bucket
        lo, hi = peak - window - 5, peak + 5
        win_msgs = [m for m in messages if lo <= m.t <= hi]
        counts: dict[str, int] = {}
        for m in win_msgs:
            for tok in set(t.lower() for t in _WORD.findall(m.text)):
                if tok in HYPE_TOKENS:
                    counts[tok] = counts.get(tok, 0) + 1
        top = sorted(counts, key=counts.get, reverse=True)[:5]
        moments.append(Moment(
            peak=peak,
            start=max(0.0, peak - window - pre_roll),
            end=peak + post_roll,
            score=round(score[i], 2),
            msgs_per_min=round(len(win_msgs) / ((hi - lo) / 60), 1),
            top_words=top,
            sample=[m.text for m in win_msgs[:8]],
        ))
    return moments
