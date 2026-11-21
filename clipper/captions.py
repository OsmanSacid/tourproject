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


def _model(size: str):
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(size, device="cpu", compute_type="int8")
    return _MODEL


def transcribe(video: Path, model_size: str = "small", lang: str | None = "en") -> list[tuple[float, float, str]]:
    """Gibt Wörter mit Zeitstempeln zurück: (start, ende, wort)."""
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"], check=True, capture_output=True).stdout
    audio = np.frombuffer(pcm, np.float32)
    if True:
        segs, _ = _model(model_size).transcribe(
            audio, language=lang, word_timestamps=True, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300}, condition_on_previous_text=False)
        words = []
        for s in segs:
            for w in s.words or []:
                t = w.word.strip()
                if t and w.probability > 0.15:
                    words.append((w.start, w.end, t))
    return words


def _ts(t: float) -> str:
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _clean(word: str) -> str:
    return word.upper().replace("{", "").replace("}", "").replace("\\", "")


def build_ass(words: list[tuple[float, float, str]], y: int = 768, per_line: int = 3,
              font: str = "Inter ExtraBold", size: int = 86, title: str | None = None) -> str:
    """ASS-Datei: Gruppen von bis zu `per_line` Wörtern, aktives Wort gelb mit Pop."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,0,0,1,7,4,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
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
    if title:  # Hook-Titel oben (eigener Style, liegt über allem)
        import quality
        st, ev = quality.title_ass(title, font=font)
        head = head.replace("\n\n[Events]", f"\n{st}\n\n[Events]")
        lines.insert(0, ev)
    return head + "\n".join(lines) + "\n"


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
