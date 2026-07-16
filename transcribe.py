"""Transcriber: YouTube/Podcast URL -> speaker-labeled HTML transcript.

Pipeline:
  1. yt-dlp downloads audio + metadata for the URL
  2. Groq whisper-large-v3 transcribes (segment-level timestamps)
  3. AssemblyAI diarizes (speaker labels per time range)
  4. Speakers are aligned to transcript segments by time overlap
  5. Jinja2 renders a self-contained HTML file with audio player
  6. Library index.html is regenerated to list every past transcript
  7. The webview navigates to the new transcript
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from html import escape as html_escape, unescape as html_unescape
from pathlib import Path

import assemblyai as aai
from send2trash import send2trash
import webview
import yt_dlp
from groq import Groq
from jinja2 import Environment, FileSystemLoader, select_autoescape


GROQ_MODEL = "whisper-large-v3"
GROQ_MAX_BYTES = 25 * 1024 * 1024  # 25MB free-tier limit
AUDIO_BITRATE_KBPS = 32  # mono mp3 ~ 14MB/hour at this bitrate
CHUNK_MINUTES = 20  # used only if encoded audio still exceeds GROQ_MAX_BYTES

# Paragraph breaking within a single speaker turn (group_segments_by_speaker).
PARA_PAUSE_SECONDS = 1.75  # a pause this long between segments starts a new paragraph
PARA_SOFT_CHARS = 550  # past this, break at the next sentence-ending segment
PARA_HARD_CHARS = 900  # past this, break at the next segment boundary regardless

# Stage weights for the transcription progress bar, normalized per run over
# the stages that actually apply (local files skip "download").
PROGRESS_STAGES = {
    "download": 0.15,
    "encode": 0.10,
    "transcribe": 0.30,
    "diarize": 0.40,
    "render": 0.05,
}
GROQ_REALTIME_RATIO = 0.03  # Groq whisper-large-v3 ≈ 3% of realtime (tunable)
AAI_REALTIME_RATIO = 0.25  # AssemblyAI diarization ≈ 25% of realtime (tunable)


class _Progress:
    """Weighted multi-stage progress feeding the shell's bar.

    Stages are declared up front so weights normalize to 100% even when a
    stage doesn't apply. Fractions passed with estimated=True come from
    elapsed-time guesses (Groq intra-chunk, AssemblyAI fill) and render
    with a pulsing style and "~" label; everything else is driven by real
    signals (yt-dlp bytes, ffmpeg out_time, chunk boundaries, AssemblyAI
    status transitions).
    """

    def __init__(self, emit, stages: list[str]) -> None:
        total = sum(PROGRESS_STAGES.get(s, 0.0) for s in stages) or 1.0
        self._weights = {s: PROGRESS_STAGES.get(s, 0.0) / total for s in stages}
        self._emit = emit
        self._done = 0.0  # weight of completed stages
        self._weight = 0.0  # weight of the active stage
        self._frac = 0.0  # progress within the active stage

    def stage(self, name: str) -> None:
        self._done += self._weight
        self._weight = self._weights.get(name, 0.0)
        self._frac = 0.0
        self._send(False)

    def update(self, fraction: float, estimated: bool = False) -> None:
        self._frac = max(0.0, min(1.0, fraction))
        self._send(estimated)

    def finish(self) -> None:
        self._done, self._weight, self._frac = 1.0, 0.0, 0.0
        self._send(False)

    def _send(self, estimated: bool) -> None:
        overall = round((self._done + self._weight * self._frac) * 100)
        self._emit({"overall": overall, "estimated": estimated})


class _NullProgress:
    def stage(self, name: str) -> None: ...
    def update(self, fraction: float, estimated: bool = False) -> None: ...
    def finish(self) -> None: ...


_NULL_PROGRESS = _NullProgress()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    """Directory holding config.json — next to the .exe in frozen builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def resource_dir() -> Path:
    """Directory holding templates/ and static/ — bundled by PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).parent


def transcripts_dir() -> Path:
    out = Path.home() / "Desktop" / "Transcripts"
    out.mkdir(parents=True, exist_ok=True)
    return out


def ffmpeg_exe() -> str:
    """Return ffmpeg path — prefer the bundled copy, then PATH."""
    name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    for d in (resource_dir(), app_dir()):
        candidate = d / name
        if candidate.exists():
            return str(candidate)
    return "ffmpeg"


# Hide the console flash when subprocess launches ffmpeg under a --windowed build.
_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


CONFIG_PATH = app_dir() / "config.json"
META_SUFFIX = ".meta.json"  # sidecar file for library rebuilds

_config_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with _config_lock:
        if CONFIG_PATH.exists():
            try:
                return json.loads(CONFIG_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}


def save_config(cfg: dict) -> None:
    with _config_lock:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    name = re.sub(r"[<>:\"/\\|?*]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "transcript"


def validate_api_keys(groq_key: str, assemblyai_key: str) -> str:
    """Return an error string if either key is rejected by its API, else ''.

    A network failure is treated as 'not a key problem' — we don't block saving.
    """
    if not groq_key or not assemblyai_key:
        return "Both keys are required."
    # Groq: GET /openai/v1/models — instant, free, returns 401 on bad key.
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {groq_key}"},
            ),
            timeout=8,
        )
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "Groq key invalid — check console.groq.com/keys."
    except Exception:
        pass  # network/DNS error — let it through
    # AssemblyAI: GET /v2/transcript?limit=1 — returns 401 on bad key.
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                "https://api.assemblyai.com/v2/transcript?limit=1",
                headers={"authorization": assemblyai_key},
            ),
            timeout=8,
        )
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "AssemblyAI key invalid — check app.assemblyai.com/account."
    except Exception:
        pass
    return ""


def extract_body_text(html_path: Path) -> str:
    """Extract plain paragraph text from a saved transcript HTML for search indexing."""
    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    chunks = re.findall(r'<p class="text"[^>]*>(.*?)</p>', html, re.DOTALL)
    text = " ".join(re.sub(r"<[^>]+>", "", chunk) for chunk in chunks)
    return " ".join(text.split())


def format_timestamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def platform_from_extractor(extractor: str | None) -> str:
    if not extractor:
        return "generic"
    e = extractor.lower()
    if "youtube" in e:
        return "youtube"
    if "apple" in e or "podcast" in e or "rss" in e:
        return "podcast"
    return "generic"


def parse_upload_date(yt_date: str | None) -> str:
    """yt-dlp returns YYYYMMDD; format as 'Mon D, YYYY'."""
    if not yt_date:
        return ""
    try:
        return datetime.strptime(yt_date, "%Y%m%d").strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        try:
            return datetime.strptime(yt_date, "%Y%m%d").strftime("%b %d, %Y")
        except Exception:
            return yt_date


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str = "Speaker 1"
    words: list = field(default_factory=list)  # [{word, start}] with chunk offset applied


def _ffmpeg_location() -> str | None:
    """Path to bundled ffmpeg, or None to fall back to system PATH."""
    d = resource_dir()
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    return str(d) if (d / exe).exists() else None


def _run_ffmpeg(args: list[str], duration: float | None = None,
                on_progress=None) -> None:
    """Run ffmpeg. With duration + on_progress, parse `-progress` output and
    report the fraction of the input processed. Captures stderr either way
    so a failure surfaces ffmpeg's actual error instead of a silent code."""
    cmd = [ffmpeg_exe(), "-y"]
    track = bool(on_progress and duration)
    if track:
        cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]
    cmd += args
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE if track else subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, creationflags=_NO_WINDOW,
    )
    if track:
        for line in proc.stdout:
            # ffmpeg quirk: out_time_ms is microseconds, not milliseconds.
            if line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                    on_progress(min(1.0, us / 1_000_000 / duration))
                except ValueError:
                    pass
    stderr = proc.communicate()[1] or ""
    if proc.returncode != 0:
        tail = stderr.strip().splitlines()
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, stderr=tail[-1] if tail else ""
        )


def _media_duration(path: Path) -> float | None:
    """Duration in seconds read from ffmpeg's header dump (no decode)."""
    try:
        proc = subprocess.run(
            [ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True,
            creationflags=_NO_WINDOW,
        )
        m = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", proc.stderr)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except OSError:
        pass
    return None


def download_audio(url: str, out_dir: Path, status,
                   progress=_NULL_PROGRESS) -> tuple[Path, dict]:
    """Download mono MP3 + return (audio_path, metadata dict)."""
    status("Downloading audio...")
    progress.stage("download")

    def _hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress.update(d.get("downloaded_bytes", 0) / total)
        elif d.get("status") == "finished":
            progress.update(1.0)

    tmpl = str(out_dir / "_tmp_%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": tmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": _ffmpeg_location(),
        "progress_hooks": [_hook],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(AUDIO_BITRATE_KBPS),
            },
        ],
        "postprocessor_args": ["-ac", "1"],  # mono
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    video_id = info.get("id", "audio")
    tmp_path = out_dir / f"_tmp_{video_id}.mp3"
    if not tmp_path.exists():
        candidates = list(out_dir.glob(f"_tmp_{video_id}.*"))
        if not candidates:
            raise RuntimeError("Audio download failed.")
        tmp_path = candidates[0]

    title = info.get("title") or video_id
    safe = sanitize_filename(title)
    project_dir = out_dir / safe
    project_dir.mkdir(exist_ok=True)
    final_audio = project_dir / "audio.mp3"
    tmp_path.replace(final_audio)

    status("Encoding audio...")
    progress.stage("encode")
    reenc = final_audio.with_suffix(".enc.mp3")
    _run_ffmpeg(
        ["-i", str(final_audio),
         "-ar", "16000", "-ac", "1", "-b:a", "32k", str(reenc)],
        duration=info.get("duration") or None,
        on_progress=progress.update,
    )
    reenc.replace(final_audio)

    meta = {
        "title": title,
        "safe_name": safe,
        "channel": info.get("uploader") or info.get("channel") or "",
        "upload_date_raw": info.get("upload_date") or "",
        "upload_date": parse_upload_date(info.get("upload_date")),
        "url": info.get("webpage_url") or url,
        "platform": platform_from_extractor(info.get("extractor")),
        "duration": info.get("duration") or 0,
        "audio_filename": "audio.mp3",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return final_audio, meta


def _unique_project_dir(base: Path, safe_name: str) -> tuple[Path, str]:
    """Pick base/safe_name, or base/safe_name_2, _3, ... if the prior is non-empty.
    Returns (project_dir, final_safe_name)."""
    candidate = base / safe_name
    if not candidate.exists() or not any(candidate.iterdir()):
        return candidate, safe_name
    n = 2
    while True:
        name = f"{safe_name}_{n}"
        candidate = base / name
        if not candidate.exists() or not any(candidate.iterdir()):
            return candidate, name
        n += 1


def load_local_audio(src: Path, out_dir: Path, status,
                     title: str | None = None,
                     progress=_NULL_PROGRESS) -> tuple[Path, dict]:
    """Re-encode a local audio/video file to mono MP3 + return (audio_path, metadata).
    title overrides the displayed title (default: src.stem) — used to avoid leaking
    upload-temp prefixes into the user-visible folder name."""
    if not src.exists():
        raise RuntimeError(f"File not found: {src}")
    status("Reading file...")
    display_title = title if title else src.stem
    safe_base = sanitize_filename(display_title)
    project_dir, safe = _unique_project_dir(out_dir, safe_base)
    project_dir.mkdir(parents=True, exist_ok=True)
    final_audio = project_dir / "audio.mp3"
    duration = _media_duration(src)

    status("Encoding audio...")
    progress.stage("encode")
    try:
        _run_ffmpeg(
            ["-i", str(src),
             "-vn", "-ar", "16000", "-ac", "1", "-b:a", f"{AUDIO_BITRATE_KBPS}k",
             str(final_audio)],
            duration=duration,
            on_progress=progress.update,
        )
    except subprocess.CalledProcessError:
        # Clean up the empty project dir so we don't leave debris behind.
        try:
            if project_dir.exists() and not any(project_dir.iterdir()):
                project_dir.rmdir()
        except OSError:
            pass
        raise RuntimeError(
            "Could not read this file as audio. "
            "Make sure it's a supported audio or video format."
        )

    meta = {
        "title": display_title,
        "safe_name": safe,
        "channel": "",
        "upload_date_raw": "",
        "upload_date": "",
        "url": "",
        "platform": "local",
        "duration": round(duration) if duration else 0,
        "audio_filename": "audio.mp3",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return final_audio, meta


def split_audio_if_needed(audio: Path, status) -> list[tuple[float, Path]]:
    """Return [(offset_seconds, chunk_path)]. Splits with ffmpeg if > 25MB."""
    if audio.stat().st_size <= GROQ_MAX_BYTES:
        return [(0.0, audio)]
    status("Splitting audio into chunks (file is large)...")
    chunk_dir = audio.parent / "_chunks"
    chunk_dir.mkdir(exist_ok=True)
    seg_seconds = CHUNK_MINUTES * 60
    pattern = chunk_dir / "chunk_%03d.mp3"
    _run_ffmpeg(
        [
            "-i", str(audio),
            "-f", "segment", "-segment_time", str(seg_seconds),
            "-ar", "16000", "-ac", "1", "-b:a", "32k", str(pattern),
        ]
    )
    chunks = sorted(chunk_dir.glob("chunk_*.mp3"))
    return [(i * seg_seconds, p) for i, p in enumerate(chunks)]


def transcribe_with_groq(api_key: str, chunks: list[tuple[float, Path]], status, hints: str = "",
                         progress=_NULL_PROGRESS, total_seconds: float = 0.0) -> list[Segment]:
    status("Transcribing with Groq...")
    progress.stage("transcribe")
    client = Groq(api_key=api_key)
    segments: list[Segment] = []
    for i, (offset, chunk) in enumerate(chunks):
        # Chunk boundaries are real progress; the fill inside one chunk is
        # an elapsed-time estimate (the API call is a single blocking POST).
        if total_seconds > 0:
            next_offset = chunks[i + 1][0] if i + 1 < len(chunks) else total_seconds
            base = offset / total_seconds
            span = max(0.0, (next_offset - offset) / total_seconds)
            chunk_seconds = next_offset - offset
        else:
            base, span = i / len(chunks), 1 / len(chunks)
            chunk_seconds = 0.0
        expected = max(3.0, chunk_seconds * GROQ_REALTIME_RATIO) if chunk_seconds else 10.0
        ticker_stop = threading.Event()

        def _tick(base=base, span=span, expected=expected, stop=ticker_stop):
            t0 = time.monotonic()
            while not stop.wait(0.5):
                frac = min(0.95, (time.monotonic() - t0) / expected)
                progress.update(base + span * frac, estimated=True)

        threading.Thread(target=_tick, daemon=True).start()
        try:
            with chunk.open("rb") as f:
                kwargs = dict(
                    file=(chunk.name, f.read()),
                    model=GROQ_MODEL,
                    response_format="verbose_json",
                    timestamp_granularities=["segment", "word"],
                )
                if hints:
                    kwargs["prompt"] = hints
                resp = client.audio.transcriptions.create(**kwargs)
        finally:
            ticker_stop.set()
        progress.update(base + span)
        def _get(obj, key, default=None):
            return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

        def _parse_words(raw, off):
            out = []
            for w in (raw or []):
                wtext = (_get(w, "word") or "")
                wstart = float(_get(w, "start") or 0.0) + off
                if wtext.strip():
                    out.append({"word": wtext, "start": wstart})
            return out

        # Groq may return word timestamps at the top level rather than per-segment
        top_words = _parse_words(getattr(resp, "words", None), offset)

        raw_segments = getattr(resp, "segments", None) or []
        for s in raw_segments:
            text = (_get(s, "text") or "").strip()
            if not text:
                continue
            seg_start = float(_get(s, "start") or 0.0) + offset
            seg_end   = float(_get(s, "end")   or 0.0) + offset
            # Prefer per-segment words; fall back to slicing top-level words by time
            words = _parse_words(_get(s, "words"), offset)
            if not words and top_words:
                words = [w for w in top_words if seg_start <= w["start"] < seg_end]
            segments.append(Segment(start=seg_start, end=seg_end, text=text, words=words))
    return segments


def diarize_with_assemblyai(api_key: str, audio: Path, status,
                            progress=_NULL_PROGRESS,
                            duration: float = 0.0,
                            speakers_expected: int = 0) -> list[tuple[float, float, str]]:
    """Returns list of (start_sec, end_sec, speaker_label)."""
    status("Identifying speakers with AssemblyAI...")
    progress.stage("diarize")
    aai.settings.api_key = api_key
    # Telling the diarizer how many voices to expect is the single biggest
    # lever on label quality; 0/blank means auto-detect.
    kwargs = {"speaker_labels": True}
    if speakers_expected and int(speakers_expected) > 0:
        kwargs["speakers_expected"] = int(speakers_expected)
    config = aai.TranscriptionConfig(**kwargs)
    # submit + poll instead of the SDK's blocking transcribe() so the bar
    # can move: queued/processing/completed transitions are real; the fill
    # while processing is an elapsed-time estimate against the audio length.
    transcript = aai.Transcriber().submit(str(audio), config=config)
    expected = max(20.0, duration * AAI_REALTIME_RATIO) if duration else 60.0
    t0 = time.monotonic()
    while transcript.status not in (
        aai.TranscriptStatus.completed, aai.TranscriptStatus.error
    ):
        frac = min(0.95, (time.monotonic() - t0) / expected)
        if transcript.status == aai.TranscriptStatus.queued:
            frac = min(frac, 0.05)
        progress.update(frac, estimated=True)
        time.sleep(3)
        transcript = aai.Transcript.get_by_id(transcript.id)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {transcript.error}")
    progress.update(1.0)
    out: list[tuple[float, float, str]] = []
    for utt in (transcript.utterances or []):
        out.append((utt.start / 1000.0, utt.end / 1000.0, f"Speaker {utt.speaker}"))
    return out


def align_speakers(segments: list[Segment], diar: list[tuple[float, float, str]]) -> None:
    """Mutates segments in place — set each segment's speaker by max overlap."""
    if not diar:
        return
    for seg in segments:
        best_label = seg.speaker
        best_overlap = 0.0
        for d_start, d_end, label in diar:
            overlap = max(0.0, min(seg.end, d_end) - max(seg.start, d_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = label
        seg.speaker = best_label


_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]?\s*$")


def group_segments_by_speaker(segments: list[Segment]) -> list[dict]:
    """Merge consecutive same-speaker segments into readable paragraphs.

    A paragraph always ends on a speaker change. Within one speaker's turn
    it also ends at a long pause between segments, or — once it has grown
    past PARA_SOFT_CHARS — at the next sentence-ending segment (past
    PARA_HARD_CHARS, at the next segment boundary regardless), so long
    monologues don't render as one giant block. Whisper segments are
    sentence-ish, so breaking between segments needs no word surgery.
    Continuation paragraphs are marked "cont" so the repeated speaker
    label can be de-emphasized.
    """
    paragraphs: list[dict] = []
    for seg in segments:
        prev = paragraphs[-1] if paragraphs else None
        same_speaker = prev is not None and prev["speaker"] == seg.speaker
        if same_speaker:
            pause = seg.start - prev["end"]
            length = len(prev["text"])
            break_here = (
                pause >= PARA_PAUSE_SECONDS
                or length >= PARA_HARD_CHARS
                or (length >= PARA_SOFT_CHARS and _SENTENCE_END.search(prev["text"]))
            )
        else:
            break_here = True
        if same_speaker and not break_here:
            prev["end"] = seg.end
            prev["text"] += " " + seg.text
            prev["words"].extend(seg.words)
        else:
            paragraphs.append(
                {
                    "speaker": seg.speaker,
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                    "words": list(seg.words),
                    "cont": same_speaker,
                }
            )
    for p in paragraphs:
        p["start_label"] = format_timestamp(p["start"])
    return paragraphs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def jinja_env() -> Environment:
    templates = resource_dir() / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates)),
        autoescape=select_autoescape(["html"]),
    )
    return env


def read_css() -> str:
    return (resource_dir() / "static" / "style.css").read_text()


def render_transcript(
    meta: dict,
    paragraphs: list[dict],
    segments: list[Segment] | None = None,
    diarization: list[tuple[float, float, str]] | None = None,
) -> Path:
    env = jinja_env()
    template = env.get_template("transcript.html")
    html = template.render(meta=meta, paragraphs=paragraphs, css=read_css())
    project_dir = transcripts_dir() / meta["safe_name"]
    project_dir.mkdir(exist_ok=True)
    out = project_dir / "transcript.html"
    out.write_text(html, encoding="utf-8")
    if segments is not None:
        # Raw pipeline output. The rendered HTML is lossy (edits overwrite
        # it), so this is the only place exact timings survive — it enables
        # future re-rendering, re-diarization, and SRT/VTT export.
        raw = {
            "segments": [
                {
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "speaker": s.speaker,
                    "words": s.words,
                }
                for s in segments
            ],
            "diarization": [
                {"start": d[0], "end": d[1], "speaker": d[2]}
                for d in (diarization or [])
            ],
        }
        (project_dir / "segments.json").write_text(
            json.dumps(raw), encoding="utf-8"
        )
    meta["transcribed_at"] = datetime.now().astimezone().isoformat()
    sidecar = project_dir / f"transcript{META_SUFFIX}"
    sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return out


def rebuild_library() -> Path:
    entries = []
    td = transcripts_dir()
    # New layout: <Transcripts>/<folder>/transcript.meta.json
    for sidecar in td.glob(f"*/transcript{META_SUFFIX}"):
        try:
            m = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            continue
        html_path = sidecar.parent / "transcript.html"
        if not html_path.exists():
            continue
        entries.append({
            "title": m.get("title", m.get("safe_name", "")),
            "channel": m.get("channel", ""),
            "upload_date": m.get("upload_date", ""),
            "upload_date_raw": m.get("upload_date_raw", ""),
            "transcribed_at": m.get("transcribed_at", ""),
            "platform": m.get("platform", "generic"),
            "href": f"{sidecar.parent.name}/transcript.html",
            "body_text": extract_body_text(html_path),
        })
    # Old flat layout: <Transcripts>/<safe>.meta.json (backwards compat)
    for sidecar in td.glob(f"*{META_SUFFIX}"):
        try:
            m = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            continue
        html_path = td / f"{m['safe_name']}.html"
        if not html_path.exists():
            continue
        entries.append({
            "title": m.get("title", m["safe_name"]),
            "channel": m.get("channel", ""),
            "upload_date": m.get("upload_date", ""),
            "upload_date_raw": m.get("upload_date_raw", ""),
            "transcribed_at": m.get("transcribed_at", ""),
            "platform": m.get("platform", "generic"),
            "href": f"{m['safe_name']}.html",
            "body_text": extract_body_text(html_path),
        })
    entries.sort(
        key=lambda e: e.get("transcribed_at") or e.get("upload_date_raw", ""),
        reverse=True,
    )
    env = jinja_env()
    template = env.get_template("library.html")
    html = template.render(entries=entries, css=read_css())
    out = td / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def migrate_to_folders() -> None:
    """Move flat-layout transcripts into per-project subfolders (one-time, silent)."""
    td = transcripts_dir()
    for sidecar in list(td.glob(f"*{META_SUFFIX}")):
        try:
            m = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            continue
        safe = m.get("safe_name", "")
        if not safe:
            continue
        old_html = td / f"{safe}.html"
        if not old_html.exists():
            sidecar.unlink(missing_ok=True)
            continue
        project_dir = td / safe
        project_dir.mkdir(exist_ok=True)
        # Patch and move the HTML: fix audio src and library link depth
        html_content = old_html.read_text(encoding="utf-8")
        old_mp3_name = m.get("audio_filename", f"{safe}.mp3")
        html_content = html_content.replace(f'src="{old_mp3_name}"', 'src="audio.mp3"')
        html_content = html_content.replace('href="index.html"', 'href="../index.html"')
        (project_dir / "transcript.html").write_text(html_content, encoding="utf-8")
        old_html.unlink()
        # Move audio if it exists
        old_mp3 = td / old_mp3_name
        if old_mp3.exists():
            old_mp3.replace(project_dir / "audio.mp3")
            m["audio_filename"] = "audio.mp3"
        # Write updated sidecar inside project folder; remove old one
        (project_dir / f"transcript{META_SUFFIX}").write_text(
            json.dumps(m, indent=2), encoding="utf-8"
        )
        sidecar.unlink(missing_ok=True)
    # Clean up any orphaned _chunks folders left by previous runs
    for folder in td.iterdir():
        if folder.is_dir() and folder.name.endswith("_chunks"):
            shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------------
# pywebview API (called from JavaScript via window.pywebview.api.*)
# ---------------------------------------------------------------------------

UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024  # JS sends ~4MB raw per chunk (~5.5MB base64)


class Api:
    def __init__(self) -> None:
        self._window: webview.Window | None = None
        # upload_id -> (temp file path, original filename supplied by JS)
        self._uploads: dict[str, tuple[Path, str]] = {}
        self._uploads_lock = threading.Lock()

    def set_window(self, w: webview.Window) -> None:
        self._window = w

    def _js(self, script: str) -> None:
        """Evaluate JS in the webview — silently no-ops if window has navigated."""
        try:
            if self._window:
                self._window.evaluate_js(script)
        except Exception:
            pass

    # Called by shell.html on load to decide whether to show setup or main form.
    def get_config(self) -> dict:
        cfg = load_config()
        return {
            "has_groq": bool(cfg.get("groq_api_key")),
            "has_assemblyai": bool(cfg.get("assemblyai_api_key")),
        }

    # Called by the setup form in shell.html.
    def save_api_keys(self, groq_key: str, assemblyai_key: str) -> dict:
        groq_key = groq_key.strip()
        assemblyai_key = assemblyai_key.strip()
        err = validate_api_keys(groq_key, assemblyai_key)
        if err:
            return {"ok": False, "error": err}
        cfg = load_config()
        cfg["groq_api_key"] = groq_key
        cfg["assemblyai_api_key"] = assemblyai_key
        save_config(cfg)
        return {"ok": True}

    # Called by the Transcribe button in shell.html.
    def transcribe(self, url: str, hints: str = "", speakers: int = 0) -> None:
        urls = [u.strip() for u in url.splitlines() if u.strip()]
        if not urls:
            self._js("updateStatus('Paste a URL first.')")
            self._js("onError()")
            return
        cfg = load_config()
        threading.Thread(
            target=self._pipeline_queue, args=(urls, hints, cfg),
            kwargs={"speakers": speakers}, daemon=True
        ).start()

    # Chunked upload flow used by the Upload file button in shell.html.
    # WebView2 doesn't expose disk paths of files picked via <input type="file">,
    # and base64-encoding a multi-GB file into one bridge call would balloon
    # memory. JS slices the file, sends ~4MB chunks, and we append each chunk
    # straight to disk so peak RAM stays bounded.
    def upload_begin(self, filename: str) -> str:
        upload_id = uuid.uuid4().hex
        tmp_dir = Path(tempfile.gettempdir()) / "simple-transcriber-uploads"
        tmp_dir.mkdir(exist_ok=True)
        safe_name = Path(filename or "upload.bin").name  # strip any path components
        tmp_path = tmp_dir / f"{upload_id}_{safe_name}"
        tmp_path.write_bytes(b"")
        with self._uploads_lock:
            self._uploads[upload_id] = (tmp_path, safe_name)
        return upload_id

    def upload_chunk(self, upload_id: str, b64data: str) -> bool:
        with self._uploads_lock:
            entry = self._uploads.get(upload_id)
        if entry is None:
            return False
        path, _ = entry
        try:
            data = base64.b64decode(b64data)
        except Exception:
            return False
        with open(path, "ab") as f:
            f.write(data)
        return True

    def upload_finish(self, upload_id: str, hints: str = "",
                      speakers: int = 0) -> None:
        with self._uploads_lock:
            entry = self._uploads.pop(upload_id, None)
        if entry is None:
            self._js("updateStatus('Unknown upload.')")
            self._js("onError()")
            return
        path, original_filename = entry
        cfg = load_config()
        # Use the original filename's stem as the display title so the user
        # never sees the upload_id prefix from the temp file.
        title = Path(original_filename).stem or "Uploaded audio"
        threading.Thread(
            target=self._pipeline_queue, args=([str(path)], hints, cfg),
            kwargs={"is_file": True, "title": title, "speakers": speakers},
            daemon=True,
        ).start()

    def upload_cancel(self, upload_id: str) -> None:
        with self._uploads_lock:
            entry = self._uploads.pop(upload_id, None)
        if entry is not None:
            path, _ = entry
            try:
                path.unlink()
            except OSError:
                pass

    def _pipeline_queue(self, sources: list[str], hints: str, cfg: dict,
                        is_file: bool = False, title: str | None = None,
                        speakers: int = 0) -> None:
        total = len(sources)
        last_html: Path | None = None
        for i, src in enumerate(sources, 1):
            prefix = f"[{i}/{total}] " if total > 1 else ""
            try:
                last_html = self._pipeline(src, hints, cfg, status_prefix=prefix,
                                           is_file=is_file, title=title,
                                           speakers=speakers)
            except Exception as e:
                label = "file" if is_file else "URL"
                safe = str(e).replace("\\", "\\\\").replace("'", "\\'")
                self._js(f"updateStatus('Error on {label} {i}: {safe}')")
                self._js("onError()")
                return
        if self._window:
            if total > 1:
                lib_uri = (transcripts_dir() / "index.html").as_uri()
                self._window.load_url(lib_uri)
            elif last_html is not None:
                self._window.load_url(last_html.as_uri())

    def _pipeline(self, src: str, hints: str, cfg: dict, status_prefix: str = "",
                  is_file: bool = False, title: str | None = None,
                  speakers: int = 0) -> Path:
        def status(msg: str) -> None:
            safe = (status_prefix + msg).replace("\\", "\\\\").replace("'", "\\'")
            self._js(f"updateStatus('{safe}')")

        def emit(payload: dict) -> None:
            self._js(f"updateProgress({json.dumps(payload)})")

        stages = ["encode"] if is_file else ["download", "encode"]
        progress = _Progress(emit, stages + ["transcribe", "diarize", "render"])

        if is_file:
            audio, meta = load_local_audio(Path(src), transcripts_dir(), status,
                                           title=title, progress=progress)
        else:
            audio, meta = download_audio(src, transcripts_dir(), status,
                                         progress=progress)
        chunks = split_audio_if_needed(audio, status)
        duration = float(meta.get("duration") or 0)
        segments = transcribe_with_groq(cfg["groq_api_key"], chunks, status, hints,
                                        progress=progress, total_seconds=duration)
        chunk_dir = audio.parent / "_chunks"
        if chunk_dir.exists():
            shutil.rmtree(chunk_dir, ignore_errors=True)
        diar = diarize_with_assemblyai(cfg["assemblyai_api_key"], audio, status,
                                       progress=progress, duration=duration,
                                       speakers_expected=speakers)
        align_speakers(segments, diar)
        progress.stage("render")
        paragraphs = group_segments_by_speaker(segments)
        html_path = render_transcript(meta, paragraphs, segments=segments,
                                      diarization=diar)
        rebuild_library()
        progress.finish()
        status("Done!")
        return html_path

    # Called by the Save edits button in transcript.html.
    # relative_path is e.g. "Goldman Sachs CEO.../transcript.html"
    def save_transcript(self, html: str, relative_path: str) -> bool:
        base = transcripts_dir().resolve()
        path = (base / relative_path).resolve()
        if not str(path).startswith(str(base)):
            return False  # reject path traversal attempts
        if path.exists():
            # Keep the previous version as a one-step safety net, since
            # autosave overwrites in place. Invisible to the library, which
            # only globs */transcript.meta.json.
            try:
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass
        path.write_text(html, encoding="utf-8")
        self._sync_title_from_html(path.parent, html)
        return True

    # An edited title lives in the saved HTML's <title>, but the library
    # reads titles from the sidecar meta.json — keep the two in sync.
    # Rebuilding the library re-reads every transcript, so only do it when
    # the title actually changed.
    def _sync_title_from_html(self, folder: Path, html: str) -> None:
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        if not m:
            return
        title = html_unescape(m.group(1)).strip()
        sidecar = folder / f"transcript{META_SUFFIX}"
        if not title or not sidecar.exists():
            return
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            if meta.get("title") == title:
                return
            meta["title"] = title
            sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            rebuild_library()
        except Exception:
            pass

    # Called by the rename (pencil) button in library.html. Updates the
    # display title in the sidecar (which the library reads) and inside
    # transcript.html (<title> and <h1>). The folder name stays unchanged:
    # it is an opaque storage key, and renaming it can fail on Windows
    # while the webview holds the folder's audio file open.
    def rename_transcript(self, folder: str, new_title: str) -> str:
        new_title = (new_title or "").strip()
        base = transcripts_dir().resolve()
        path = (base / folder).resolve()
        if new_title and str(path).startswith(str(base)) and path.is_dir():
            sidecar = path / f"transcript{META_SUFFIX}"
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                meta["title"] = new_title
                sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            except Exception:
                pass
            html_path = path / "transcript.html"
            if html_path.exists():
                try:
                    esc = html_escape(new_title)
                    doc = html_path.read_text(encoding="utf-8")
                    doc = re.sub(
                        r"<title>.*?</title>",
                        lambda _: f"<title>{esc}</title>",
                        doc, count=1, flags=re.S,
                    )
                    doc = re.sub(
                        r'(<h1 id="transcript-title"[^>]*>).*?(</h1>)',
                        lambda mm: mm.group(1) + esc + mm.group(2),
                        doc, count=1, flags=re.S,
                    )
                    html_path.write_text(doc, encoding="utf-8")
                except OSError:
                    pass
        return rebuild_library().as_uri()

    # Called by the Delete selected button in library.html.
    # folders is a list of subfolder names (e.g. ["goldman-sachs-ceo-..."]).
    # Each folder is sent to the OS Recycle Bin via send2trash (recoverable).
    # Returns the rebuilt library URL so JS can navigate to the refreshed page.
    def delete_transcripts(self, folders: list) -> str:
        base = transcripts_dir().resolve()
        for name in folders:
            path = (base / name).resolve()
            if not str(path).startswith(str(base)):
                continue  # reject path traversal
            if path.is_dir():
                send2trash(str(path))
        return rebuild_library().as_uri()

    # Called by "View Library" in shell.html and "← Library" in transcripts.
    # Returns the URL to navigate to — JS handles the actual navigation so
    # the pywebview bridge callback resolves cleanly before the page unloads.
    def open_library(self) -> str:
        index = rebuild_library()
        return index.as_uri()

    # Called by "New Transcription" in library.html. Returns shell URL.
    def show_home(self) -> str:
        return (resource_dir() / "templates" / "shell.html").as_uri()

    # Called by "Open Folder" in shell.html.
    def open_folder(self) -> None:
        folder = transcripts_dir()
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)])
        else:
            subprocess.run(["xdg-open", str(folder)])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _wipe_orphan_uploads() -> None:
    """Remove any leftover upload staging files from prior runs."""
    tmp_dir = Path(tempfile.gettempdir()) / "simple-transcriber-uploads"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    migrate_to_folders()
    _wipe_orphan_uploads()
    api = Api()
    shell_url = (resource_dir() / "templates" / "shell.html").as_uri()
    cfg = load_config()
    window = webview.create_window(
        "Simple Transcriber for Podcasts & Videos",
        url=shell_url,
        js_api=api,
        width=cfg.get("window_width", 600),
        height=cfg.get("window_height", 460),
        x=cfg.get("window_x"),
        y=cfg.get("window_y"),
        min_size=(480, 360),
    )
    api.set_window(window)

    def _save_geometry(*_):
        c = load_config()
        c.update(
            window_x=window.x,
            window_y=window.y,
            window_width=window.width,
            window_height=window.height,
        )
        save_config(c)

    window.events.moved += _save_geometry
    window.events.resized += _save_geometry

    def _flush_unsaved(*_):
        # Transcript pages expose __getSaveState with any unsaved edits;
        # other pages (shell, library, transcripts rendered before this
        # feature) return null and close immediately.
        try:
            state = window.evaluate_js(
                "window.__getSaveState ? JSON.stringify(window.__getSaveState()) : null"
            )
            if state:
                data = json.loads(state)
                if data.get("dirty") and data.get("html") and data.get("relPath"):
                    api.save_transcript(data["html"], data["relPath"])
        except Exception:
            pass
        return True  # never block the window from closing

    window.events.closing += _flush_unsaved
    webview.start()


if __name__ == "__main__":
    main()
