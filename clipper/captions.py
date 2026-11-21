"""Untertitel im Shorts/Reels-Style: Sprache -> Text (Whisper) -> Wort-für-Wort-Karaoke (ASS).

Style: 2-3 Wörter auf einmal, GROSSBUCHSTABEN, dicke schwarze Outline,
aktuelles Wort gelb + kurz größer ("Pop"), mittig an der Kante Kamera/Spiel.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

_MODEL = None
HALLUCINATIONS = ("thank you for watching", "subscribe to my channel", "thanks for watching",
                  "like and subscribe", "see you in the next", "please subscribe", "amara.org")


def _model(size: str):
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(size, device="cpu", compute_type="int8")
    return _MODEL


def _run(audio, model_size, lang, vad: bool):
    kw = dict(language=lang, word_timestamps=True, condition_on_previous_text=False, no_speech_threshold=0.7)
    if vad:
        kw.update(vad_filter=True, vad_parameters={"threshold": 0.2, "min_silence_duration_ms": 400,
                                                   "speech_pad_ms": 300})
    segs, _ = _model(model_size).transcribe(audio, **kw)
    words = []
    for s in segs:
        text = s.text.lower()
        # typische Whisper-Halluzinationen bei Musik/Spielsound rauswerfen
        if any(h in text for h in HALLUCINATIONS) or s.avg_logprob < (-1.0 if vad else -0.75):
            continue
        if s.no_speech_prob > 0.6 and s.avg_logprob < -0.5:
            continue
        for w in s.words or []:
            t = w.word.strip()
            if t and w.probability > 0.15:
                words.append((float(w.start), float(w.end), t))
    return words


def transcribe(video: Path, model_size: str = "small", lang: str | None = "en") -> list[tuple[float, float, str]]:
    """Gibt Wörter mit Zeitstempeln zurück: (start, ende, wort).
    Erst mit Sprach-Erkennung (VAD); findet die kaum etwas (Stimme unter Spielsound),
    zweiter Durchgang ohne VAD."""
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"], check=True, capture_output=True).stdout
    audio = np.frombuffer(pcm, np.float32)
    dur = len(audio) / 16000
    words = _run(audio, model_size, lang, vad=True)
    if len(words) < dur / 4:
        alt = _run(audio, model_size, lang, vad=False)
        if len(alt) > len(words):
            words = alt
    return words


def _ts(t: float) -> str:
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _clean(word: str) -> str:
    return word.upper().replace("{", "").replace("}", "").replace("\\", "")


def build_ass(words: list[tuple[float, float, str]], y: int = 768, per_line: int = 3,
              font: str = "Inter ExtraBold", size: int = 86, title: str | None = None,
              title_y: int = 150, duration: float = 60) -> str:
    """ASS-Datei: Gruppen von bis zu `per_line` Wörtern, aktives Wort gelb mit Pop."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,0,0,1,7,4,5,60,60,0,1
Style: Title,{font},62,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,1,0,0,0,100,100,0,0,3,14,0,8,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if title:
        # Titel-Box oben (weiß, schwarze Schrift), ganze Länge; \\N = Zeilenumbruch
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(duration)},Title,,0,0,0,,{{\\an8\\pos(540,{title_y})}}{wrap_title(title)}")
    # Wörter in Gruppen teilen (neue Gruppe auch bei Pause > 0.6s oder Satzende)
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= per_line or w[0] - cur[-1][1] > 0.6 or cur[-1][2][-1:] in ".?!"):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    for gi, g in enumerate(groups):
        g_end = groups[gi + 1][0][0] if gi + 1 < len(groups) else g[-1][1] + 0.3
        g_end = min(g_end, g[-1][1] + 0.8)
        for wi, (ws, we, _) in enumerate(g):
            start = ws if wi else min(ws, g[0][0])
            end = g[wi + 1][0] if wi + 1 < len(g) else g_end
            if end <= start:
                continue
            parts = []
            for k, (_, _, t) in enumerate(g):
                t = _clean(t)
                if k == wi:  # aktives Wort: gelb + Pop-Animation
                    parts.append(r"{\c&H00E6FF&\fscx112\fscy112\t(0,90,\fscx100\fscy100)}" + t +
                                 r"{\c&HFFFFFF&\fscx100\fscy100}")
                else:
                    parts.append(t)
            pos = rf"{{\an5\pos(540,{y})}}"
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{pos}{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"


def wrap_title(title: str, max_chars: int = 24) -> str:
    words, lines, cur = title.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines)


def burn(video: Path, ass_text: str, out: Path) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".ass", delete=False, encoding="utf-8") as f:
        f.write(ass_text)
        ass_path = f.name
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
                        "-vf", f"ass={ass_path}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)], check=True)
    finally:
        os.unlink(ass_path)
    return out
