import os
import shutil
import subprocess
from typing import List, Dict, Any, Optional

import numpy as np

# faster-whisper (CTranslate2, CPU-efficient) is the transcription engine.
# It returns real word-level timestamps, so subtitles land exactly when the
# words are spoken. No cloud API, no invented timing, no fake hook lines:
# when a clip has no speech, it simply gets no subtitles.
try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

# Whisper model baked into the Docker image at build time (see Dockerfile).
# 'base' is the multilingual sweet spot for this box: clearly better than
# 'tiny' on Hindi/Hinglish gaming speech, still fast on 2 CPU cores.
MODEL_NAME = "base"
MODEL_DIR = "/app/models"
# Kinetic look: bold condensed-feel sans, white words, current word yellow.
FONT_NAME = "Montserrat ExtraBold"
FONT_SIZE = 64
HIGHLIGHT_SIZE = 76
PRIMARY_COLOR = "&H00FFFFFF"   # white
HIGHLIGHT_COLOR = "&H0000FFFF"  # yellow (ASS BGR)
OUTLINE_COLOR = "&H00000000"   # black
MAX_WORDS_PER_LINE = 4
MAX_CHARS_PER_LINE = 22


class SubtitleEngine:
    """
    Real speech-to-text subtitles with word-level timing, rendered as
    kinetic (word-by-word highlight) .ass subtitles and burned into
    vertical clips by the formatter in the same FFmpeg pass.
    """

    def __init__(self, ffmpeg_path: Optional[str] = None):
        # ffmpeg from PATH (the container installs it via apt; the old
        # hardcoded macOS path does not exist on the server).
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
        self._model = None

    def _get_model(self):
        """Load the Whisper model once per process (it is ~150 MB)."""
        if WhisperModel is None:
            raise RuntimeError(
                "faster-whisper is not installed; subtitles need it.")
        if self._model is None:
            self._model = WhisperModel(
                MODEL_NAME, device="cpu", compute_type="int8",
                download_root=MODEL_DIR)
        return self._model

    def transcribe_words(
        self,
        media_path: str,
        duration: float = 30.0
    ) -> List[Dict[str, Any]]:
        """
        Transcribe the clip's real speech with word-level timestamps.
        Language is auto-detected per clip (Hindi, English, Hinglish).
        Returns [] when there is no speech — never invented text.
        """
        model = self._get_model()
        # Decode the audio ourselves with ffmpeg to 16 kHz mono PCM and hand
        # faster-whisper the samples directly: its built-in PyAV decoder
        # calls av.open(..., metadata_errors="ignore"), which the installed
        # PyAV 19 rejects, so that path is broken regardless of the file.
        pcm_path = f"/tmp/{os.path.basename(media_path)}_16k.pcm"
        cmd = [
            self.ffmpeg_path, "-y", "-v", "error",
            "-i", media_path, "-ac", "1", "-ar", "16000",
            "-f", "s16le", pcm_path,
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
            with open(pcm_path, "rb") as f:
                pcm = np.frombuffer(f.read(), dtype=np.int16).astype(
                    np.float32) / 32768.0
        finally:
            if os.path.exists(pcm_path):
                os.remove(pcm_path)
        if pcm.size == 0:
            return []
        # Detect the language, then transcribe with an EXPLICIT language.
        # Never leave language=None: on Indian-accented English the auto
        # mode drifts into the wrong script (it produced Japanese on a
        # real clip). These gaming streams are English with an Indian
        # accent or Hindi; the accent fools the detector into favouring
        # Hindi, but real Hindi gives English ~0 probability — so when
        # English has any real probability, the speech is English.
        try:
            lang, _lp, probs = model.detect_language(pcm)
        except Exception:
            lang, probs = "en", []
        prob_by_lang = dict(probs or [])
        language = "en" if prob_by_lang.get("en", 0.0) > 0.10 else (lang or "en")
        try:
            segments, _info = model.transcribe(
                pcm, word_timestamps=True, language=language, vad_filter=False)
            words = self._words_from_segments(segments, duration)
        except Exception as e:
            # Word-timestamp alignment can crash inside faster-whisper on
            # some audio (IndexError in find_alignment). Fall back to
            # segment timing with the words spread evenly across each
            # short segment — real words, timing off by a fraction of a
            # second at most, instead of no subtitles at all.
            print(f"[subtitles] word-timestamp pass failed ({e}); "
                  "retrying without word timestamps", flush=True)
            try:
                segments, _info = model.transcribe(
                    pcm, word_timestamps=False, language=language,
                    vad_filter=False)
                words = self._words_from_segments_no_ts(segments, duration)
            except Exception as e2:
                print(f"[subtitles] transcription failed, skipping "
                      f"subtitles: {e2}", flush=True)
                return []
        return words

    @staticmethod
    def _words_from_segments(segments, duration: float) -> List[Dict[str, Any]]:
        words: List[Dict[str, Any]] = []
        for seg in segments:
            for w in (seg.words or []):
                text = (w.word or "").strip()
                if not text:
                    continue
                start = float(w.start)
                end = float(w.end)
                if end <= start or start >= duration:
                    continue
                words.append({
                    "word": text.upper(),
                    "start": round(start, 2),
                    "end": round(min(end, duration), 2),
                })
        return words

    @staticmethod
    def _words_from_segments_no_ts(segments, duration: float) -> List[Dict[str, Any]]:
        # Segment-level fallback: spread a segment's real words evenly
        # across that segment (a few seconds), so timing stays close.
        words: List[Dict[str, Any]] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            parts = text.split()
            if not parts:
                continue
            start = float(seg.start)
            end = min(float(seg.end), duration)
            if end <= start or start >= duration:
                continue
            step = (end - start) / len(parts)
            for i, p in enumerate(parts):
                words.append({
                    "word": p.upper(),
                    "start": round(start + i * step, 2),
                    "end": round(start + (i + 1) * step, 2),
                })
        return words

    # Backwards-compatible alias (the old name promised "generation" that
    # was really fake hook lines; the new method only transcribes).
    def transcribe_or_generate_words(
        self,
        media_path: str,
        duration: float = 30.0
    ) -> List[Dict[str, Any]]:
        return self.transcribe_words(media_path, duration)

    def create_ass_subtitles(
        self,
        words_data: List[Dict[str, Any]],
        output_ass_path: str,
        font_name: str = FONT_NAME,
        font_size: int = FONT_SIZE,
        primary_color: str = PRIMARY_COLOR,
        outline_color: str = OUTLINE_COLOR,
        outline_width: int = 5
    ) -> str:
        """
        Builds a kinetic .ass file: words are grouped into short lines
        (<=4 words, <=22 chars so they fit 1080px), and for each word one
        event shows the whole line with THAT word highlighted in yellow
        and slightly larger — the highlight walks word by word as the
        speech plays. No emojis (they render as boxes without an emoji
        font), no fake lines.
        """
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTokStyle,{font_name},{font_size},{primary_color},{HIGHLIGHT_COLOR},{outline_color},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_width},2,2,40,40,320,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        dialogue_lines = []
        for line in self._group_lines(words_data):
            if not line:
                continue
            line_end = line[-1]["end"]
            for j, cur in enumerate(line):
                start = cur["start"]
                end = line[j + 1]["start"] if j + 1 < len(line) else line_end
                if end <= start:
                    end = start + 0.08
                parts = []
                for k, w in enumerate(line):
                    text = self._escape_ass(w["word"])
                    if k == j:
                        parts.append(
                            f"{{\\c{HIGHLIGHT_COLOR}&\\fs{HIGHLIGHT_SIZE}}}"
                            f"{text}"
                            f"{{\\c{primary_color}&\\fs{font_size}}}")
                    else:
                        parts.append(text)
                body = " ".join(parts)
                dialogue_lines.append(
                    f"Dialogue: 0,{self._format_ass_time(start)},"
                    f"{self._format_ass_time(end)},TikTokStyle,,0,0,0,,{body}")

        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write(header + "\n".join(dialogue_lines) + "\n")

        return output_ass_path

    @staticmethod
    def _group_lines(
        words_data: List[Dict[str, Any]]
    ) -> List[List[Dict[str, Any]]]:
        """Split words into short lines that fit a 1080px-wide frame."""
        lines: List[List[Dict[str, Any]]] = []
        cur: List[Dict[str, Any]] = []
        cur_len = 0
        for w in words_data:
            wl = len(w["word"])
            if cur and (len(cur) >= MAX_WORDS_PER_LINE
                        or cur_len + 1 + wl > MAX_CHARS_PER_LINE):
                lines.append(cur)
                cur = []
                cur_len = 0
            cur.append(w)
            cur_len += wl + (1 if len(cur) > 1 else 0)
        if cur:
            lines.append(cur)
        return lines

    @staticmethod
    def _escape_ass(text: str) -> str:
        # ASS treats { } and backslash as control characters.
        return (text.replace("\\", "\\\\")
                    .replace("{", "\\{")
                    .replace("}", "\\}"))

    def burn_subtitles(
        self,
        input_video: str,
        ass_subtitle_path: str,
        output_video: str
    ) -> str:
        """Burns styled subtitles directly into the video frames."""
        escaped_ass = ass_subtitle_path.replace(":", "\\:").replace("'", "\\'")

        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", input_video,
            "-vf", f"ass='{escaped_ass}'",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "14",
            "-minrate", "8M",
            "-maxrate", "16M",
            "-bufsize", "16M",
            "-c:a", "copy",
            output_video
        ]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=800)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg subtitle burning failed: {result.stderr.strip()}")

        return output_video

    def _format_ass_time(self, seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:05.2f}"
