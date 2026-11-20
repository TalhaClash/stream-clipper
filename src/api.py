import os
import shutil
import sys
import time
import uuid
import subprocess
import threading
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

# Ensure src directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel

from social_publisher import SocialPublisher
from stream_downloader import StreamDownloader, validate_stream_url, COOKIES_FILE, COOKIES_UPLOADED_AT_FILE
from video_formatter import VideoFormatter
from face_tracker import FaceTracker
from audio_analyzer import AudioAnalyzer
from subtitle_engine import SubtitleEngine
from auth import AuthManager
from youtube_oauth import YouTubeOAuthManager, LOGIN_SCOPES
from upload_limits import DAILY_UPLOAD_LIMIT, get_uploads_today, record_upload
from clip_cleanup import start_cleanup_thread

app = FastAPI(
    title="StreamClipper API",
    description="Zero-cost stream viral moment clipper and multi-social publisher",
    version="1.0.0"
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CLIPS_DIR = os.path.join(STATIC_DIR, "clips")
os.makedirs(CLIPS_DIR, exist_ok=True)

# In-memory database
JOBS_DB: Dict[str, Dict[str, Any]] = {}
CLIPS_DB: Dict[str, List[Dict[str, Any]]] = {}

publisher = SocialPublisher()
downloader = StreamDownloader()
formatter = VideoFormatter()
audio_analyzer = AudioAnalyzer()
subtitle_engine = SubtitleEngine()
from clip_metadata import generate_clip_metadata
auth_mgr = AuthManager()
yt_oauth = YouTubeOAuthManager()


def _job_failure_message(err: str) -> str:
    # The failure detail (yt-dlp stderr, file paths) stays in the server log
    # only. The dashboard shows one of these plain-word messages instead.
    # The known cause: YouTube's bot wall means the saved YouTube login
    # cookies expired — the fix is refreshing them in Admin, not retrying
    # the same URL.
    if "not a bot" in err or "cookies are no longer valid" in err:
        return ("YouTube is asking to sign in — the saved YouTube login has expired. "
                "Upload fresh cookies in 'YouTube Login Cookies' below on this page "
                "(steps are listed there), or in Admin → YouTube Cookies.")
    if "timed out after" in err:
        # Say WHICH step timed out: a yt-dlp (metadata/download) timeout
        # is not a render timeout, and the fix is different (retry, not
        # shorter clips). The pipeline error carries the command path.
        if "yt-dlp" in err:
            return ("YouTube took too long to respond — try Generate again. "
                    "If it keeps failing, refresh the YouTube cookies below.")
        return ("The clip took too long to render on this server — try a shorter "
                "clip length or 30 FPS instead of 60 FPS.")
    if "live stream" in err:
        return err  # already plain words, no paths or internals
    return "Check the video URL and try again."


def _reject_live_stream(info: Dict[str, Any]) -> None:
    # A stream that is still live (or hasn't started) has no end, so the
    # audio download hangs until its 120s timeout and the job fails with a
    # generic message. Fail in seconds instead, with the real reason. A
    # finished stream's VOD (live_status "was_live"/"not_live") passes fine.
    status = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
    if status == "is_live":
        raise RuntimeError(
            "This is a live stream that is still running — clips can only be made "
            "once it finishes and becomes a normal video. Try again after the stream ends.")
    if status == "is_upcoming":
        raise RuntimeError(
            "This stream hasn't started yet — clips can only be made once it has "
            "finished and becomes a normal video.")


@app.on_event("startup")
def _start_clip_cleanup():
    # Generated clips auto-delete 24h after creation (boss order 18 Nov 2026):
    # prune once at boot, then sweep every hour. The thread also drops the
    # in-memory job/clip records whose files are gone.
    start_cleanup_thread(CLIPS_DIR, CLIPS_DB, JOBS_DB)

# --- Login gate: nobody uses the tool without signing in ---
# Every page and API call passes through this middleware. Only the login
# flow itself (/login, /api/auth/*, /api/oauth/*) and the health check stay
# public. Admin-only areas (/admin, /api/admin/*)
# additionally require role == "admin". Unauthenticated page requests are
# sent to /login; unauthenticated API calls get a 401 JSON response.
# The session token is the sc_token cookie set at login (HttpOnly, Secure,
# SameSite=Lax; a Bearer token in the Authorization header is accepted too).
# Access model: ANY Gmail can sign in with Google — a first sign-in creates
# a PENDING account. Pending users may load the dashboard (to see the
# "waiting for approval" notice) but every tool API, the admin page and
# other users' clips stay closed until the admin approves them.
# Favicon files must load WITHOUT sign-in (the login page's tab icon),
# so they are public even though the rest of /static/ needs a session.
PUBLIC_PATHS = {"/login", "/api/health", "/favicon.ico",
                "/static/favicon.svg", "/static/favicon-32x32.png",
                "/static/favicon-192.png", "/static/apple-touch-icon.png"}
PUBLIC_PREFIXES = ("/api/auth/", "/api/oauth/")
ADMIN_PATHS = {"/admin", "/static/admin.html"}
ADMIN_PREFIXES = ("/api/admin/",)


class LoginRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        token = request.cookies.get("sc_token")
        if not token:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
        user = auth_mgr.verify_token(token) if token else None

        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse(url="/login", status_code=303)

        # Admin-only areas are checked FIRST: a pending user must not be
        # able to load /static/admin.html (or any admin page) just because
        # it lives under /static/.
        if path in ADMIN_PATHS or path.startswith(ADMIN_PREFIXES):
            if user.get("role") != "admin":
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Admin access required"}, status_code=403)
                return RedirectResponse(url="/", status_code=303)

        # Pending accounts (signed in, not yet approved by the admin) may
        # only load the dashboard and its assets — every tool API and other
        # users' clips stay closed until approval.
        if user.get("role") == "pending":
            if path == "/" or (path.startswith("/static/") and not path.startswith("/static/clips/")):
                return await call_next(request)
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Your account is waiting for admin approval."}, status_code=403)
            return RedirectResponse(url="/", status_code=303)

        request.state.user = user
        return await call_next(request)


app.add_middleware(LoginRequiredMiddleware)

def _set_session_cookie(response: Response, token: str):
    """The session cookie is HttpOnly (no JavaScript can read it — an XSS
    flaw cannot steal the session), Secure (HTTPS only) and SameSite=Lax
    (cross-site form posts cannot use it). The frontend never writes this
    cookie itself; it only relies on the server's Set-Cookie."""
    response.set_cookie(
        key="sc_token", value=token, max_age=604800, path="/",
        httponly=True, secure=True, samesite="lax"
    )

class CreateJobRequest(BaseModel):
    url: str
    layout: Optional[str] = "fill"  # "fill" (Full Screen 9:16), "split", "stream" (Streamer 4:6), or "blur"
    duration: Optional[int] = 30
    subtitles: Optional[bool] = True
    fps: Optional[int] = 30  # 30 or 60 fps
    num_clips: Optional[int] = 3  # Number of viral clips to detect and generate

class TrimClipRequest(BaseModel):
    start_time: float
    end_time: float

class PublishRequest(BaseModel):
    clip_id: str
    platforms: List[str]
    title: str
    description: Optional[str] = ""
    tags: Optional[List[str]] = []
    privacy: Optional[str] = "public"

class ConnectAccountBody(BaseModel):
    username: Optional[str] = None
    channel_name: Optional[str] = None
    token: Optional[str] = None

class LoginRequest(BaseModel):
    username: str
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class AddUserRequest(BaseModel):
    name: str
    email: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = "editor"

class UpdateUserStatusRequest(BaseModel):
    status: str

class GoogleCredsRequest(BaseModel):
    client_id: str
    client_secret: str

def background_process_stream(
    job_id: str,
    url: str,
    layout: str,
    duration: int,
    subtitles: bool = True,
    fps: int = 30,
    num_clips: int = 3
):
    """
    True AI Viral Clipper Engine:
    1. Extracts metadata
    2. Downloads stream audio
    3. Analyzes volume & reaction surges
    4. Identifies top viral peaks (up to num_clips)
    5. Cuts highlight segments
    6. Reframes into 9:16 vertical video at chosen FPS (30 or 60)
    7. Generates & burns animated subtitles
    """
    try:
        # Step 1: Metadata
        JOBS_DB[job_id]["progress"] = 15
        JOBS_DB[job_id]["message"] = "Fetching stream details & video metadata..."
        info = downloader.get_info(url)
        # Live / not-yet-started streams have no end — reject them here,
        # before the audio download hangs for 120s and fails anyway.
        _reject_live_stream(info)
        title = info.get("title", "Stream Highlight")
        channel = info.get("uploader", "Creator")
        total_duration = float(info.get("duration", 600))

        # Step 2: Download low-bitrate audio stream for decibel & reaction detection
        JOBS_DB[job_id]["progress"] = 35
        JOBS_DB[job_id]["message"] = "Scanning stream audio for decibel spikes and excitement surges..."
        audio_target = f"/tmp/{job_id}_audio"
        audio_file = downloader.download_audio_sample(url, audio_target)

        # Step 3: Analyze audio excitement & reaction peaks
        JOBS_DB[job_id]["progress"] = 55
        JOBS_DB[job_id]["message"] = "Isolating peak decibel clusters and viral reaction moments..."
        timeline = audio_analyzer.analyze_audio_energy(audio_file, sample_rate=8000, window_seconds=2.0)

        # Clean up temporary audio file to free disk space
        if os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass

        # Zone-based peak selection: split the usable timeline into
        # num_clips equal zones and take the loudest moment in each zone.
        # The old code picked the globally loudest peaks, which cluster
        # at the start when the intro is the loudest part — every clip
        # came from the first few minutes and the middle/end (where the
        # gameplay actually happens) was never reached. Zones guarantee
        # one clip from the start, the middle, and the end of the stream.
        usable_start = 20.0 if total_duration > 120 else 0.0
        usable_end = (total_duration - 30.0) if total_duration > 120 else total_duration
        usable_end = max(usable_end, usable_start + duration + 5)
        zone_len = (usable_end - usable_start) / max(1, num_clips)
        chosen_moments = []
        for zi in range(num_clips):
            z0 = usable_start + zi * zone_len
            z1 = z0 + zone_len
            in_zone = [p for p in timeline if z0 <= p["time"] < z1]
            if in_zone:
                chosen_moments.append(max(in_zone, key=lambda x: x["z_score"]))
            else:
                # Quiet stretch with no peak — use the zone midpoint so
                # the clip still comes from this part of the stream.
                chosen_moments.append({"time": z0 + zone_len / 2, "z_score": 2.0, "rms": 7500})
        # Dedupe (short streams can make two zones land on the same
        # moment) and fill any gap from the globally loudest peaks.
        deduped = []
        for m in chosen_moments:
            if not any(abs(m["time"] - d["time"]) < duration for d in deduped):
                deduped.append(m)
        chosen_moments = deduped
        if len(chosen_moments) < num_clips:
            for p in sorted(timeline, key=lambda x: x["z_score"], reverse=True):
                if len(chosen_moments) >= num_clips:
                    break
                if not any(abs(p["time"] - m["time"]) < duration for m in chosen_moments):
                    chosen_moments.append(p)
        # Chronological order so clip 1 is the earliest moment.
        chosen_moments = sorted(chosen_moments, key=lambda x: x["time"])

        if not chosen_moments:
            fallback_time = total_duration * 0.45 if total_duration > 60 else 10.0
            chosen_moments = [{"time": fallback_time, "z_score": 2.5, "rms": 8000}]

        chosen_moments = chosen_moments[:num_clips]

        # Per-clip work (cut + transcribe + format) runs 2 at a time on
        # this 2-CPU box. The cut is network-bound (the CPUs idle while
        # yt-dlp downloads), so overlapping it with another clip's
        # CPU-bound format/transcribe actually uses both cores instead of
        # leaving one idle. Each clip writes its own /tmp + output files,
        # so workers never clash. The shared Whisper model and the
        # used-titles set are guarded by locks (the model is one
        # ~150 MB object shared by the process; CTranslate2 inference is
        # thread-safe, but serialising the transcribe keeps memory flat
        # and avoids any contention surprises).
        import threading as _threading
        from concurrent.futures import ThreadPoolExecutor as _TPE
        _transcribe_lock = _threading.Lock()
        _titles_lock = _threading.Lock()
        used_titles = set()
        total_clips = len(chosen_moments)

        # Facecam auto-detect: the streamer's PiP never moves, so detect
        # it ONCE per job (from the first raw cut) and reuse it for every
        # clip. Only the stream/split layouts use a facecam box; fill and
        # blur ignore it. If detection finds nothing, the formatter falls
        # back to its default corner box.
        _facecam_holder = {"box": None, "done": False}
        _facecam_lock = _threading.Lock()

        def _get_facecam_box(raw_path):
            if layout not in ("stream", "split"):
                return None
            with _facecam_lock:
                if not _facecam_holder["done"]:
                    try:
                        ft = FaceTracker()
                        # Prefer the whole CAMERA window (the PiP
                        # rectangle) — the user wants the full cam
                        # screen, not just a face crop. Fall back to
                        # the expanded face box when no rectangle is
                        # found.
                        box = ft.detect_pip_box(raw_path)
                        if box is None:
                            box = ft.detect_facecam_box(raw_path)
                        _facecam_holder["box"] = box
                        print(f"[facecam] detected box={_facecam_holder['box']}", flush=True)
                    except Exception as e:
                        print(f"[facecam] detection failed: {e}", flush=True)
                        _facecam_holder["box"] = None
                    _facecam_holder["done"] = True
                return _facecam_holder["box"]

        def _process_one_clip(args):
            i, moment = args
            t_peak = moment["time"]
            cut_start = max(0.0, t_peak - 5.0)
            if cut_start + duration > total_duration:
                cut_start = max(0.0, total_duration - duration)

            raw_cut_path = f"/tmp/{job_id}_raw_{i}.mp4"
            clip_filename = f"{job_id}_clip_{i}.mp4"
            final_clip_path = os.path.join(CLIPS_DIR, clip_filename)

            # Stream-cut exact segment
            downloader.cut_clip(url, start_time=cut_start, duration=float(duration), output_path=raw_cut_path)

            ass_path = None
            words = []
            if subtitles:
                with _transcribe_lock:
                    words = subtitle_engine.transcribe_words(raw_cut_path, duration=float(duration))
                if words:
                    ass_path = f"/tmp/{job_id}_sub_{i}.ass"
                    subtitle_engine.create_ass_subtitles(words, ass_path)

            # Reframe to 9:16 vertical and burn subtitles in a single fast pass with chosen fps.
            # The facecam box is auto-detected once per job (top = streamer,
            # bottom = gameplay); None means the formatter's default corner.
            facecam_box = _get_facecam_box(raw_cut_path)
            formatter.convert_to_vertical(
                raw_cut_path,
                final_clip_path,
                layout=layout,
                ass_subtitle_path=ass_path if subtitles else None,
                fps=fps,
                facecam_box=facecam_box
            )

            # Clean up intermediate cut & sub files
            if os.path.exists(raw_cut_path):
                os.remove(raw_cut_path)
            if ass_path and os.path.exists(ass_path):
                os.remove(ass_path)

            virality_score = min(99, int(88 + (moment["z_score"] * 5)))

            # Title, description and hashtags come from THIS clip's own
            # words — not a fixed template — so every clip reads different.
            with _titles_lock:
                meta = generate_clip_metadata(
                    words, title, channel, moment, i, used_titles)

            return {
                "index": i,
                "clip": {
                    "id": f"{job_id}-{i}",
                    "title": meta["title"],
                    "description": meta["description"],
                    "virality_score": virality_score,
                    "chat_spike": f"Peak Excitement (z-score: {moment['z_score']})",
                    "start_time": round(cut_start, 1),
                    "end_time": round(cut_start + duration, 1),
                    "duration": duration,
                    "fps": fps,
                    "tags": meta["tags"],
                    "video_url": f"/static/clips/{clip_filename}",
                    "download_url": f"/static/clips/{clip_filename}"
                }
            }

        generated_clips = []
        done = 0
        with _TPE(max_workers=2) as _ex:
            _futures = { _ex.submit(_process_one_clip, (i, m)): i
                         for i, m in enumerate(chosen_moments, start=1) }
            import concurrent.futures as _cf
            for _fut in _cf.as_completed(_futures):
                _res = _fut.result()  # raises if that clip failed: the job fails with the real error
                generated_clips.append(_res)
                done += 1
                JOBS_DB[job_id]["progress"] = int(55 + (done / total_clips) * 40)
                JOBS_DB[job_id]["message"] = f"Rendered {done}/{total_clips} clips..."
        generated_clips = [r["clip"] for r in sorted(generated_clips, key=lambda r: r["index"])]

        CLIPS_DB[job_id] = generated_clips
        CLIPS_DB["latest"] = generated_clips
        JOBS_DB[job_id]["progress"] = 100
        JOBS_DB[job_id]["status"] = "completed"
        JOBS_DB[job_id]["message"] = f"Successfully generated {len(generated_clips)} full-screen 9:16 clips at {fps} FPS!"

    except Exception as e:
        err = str(e)
        print(f"[Pipeline Error] {err}")
        JOBS_DB[job_id]["message"] = _job_failure_message(err)
        JOBS_DB[job_id]["status"] = "failed"
        JOBS_DB[job_id]["progress"] = 0

@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "StreamClipper API", "budget": "$0", "phase": 4}

@app.get("/favicon.ico")
def favicon_ico():
    # Browsers request /favicon.ico by default; serve the real icon
    # (public path, no sign-in needed).
    return FileResponse(os.path.join(STATIC_DIR, "favicon.ico"),
                        media_type="image/x-icon")

# At most 2 clip jobs run at once (2-CPU box; each job runs yt-dlp +
# ffmpeg). Anything more is queued with a clear 429 instead of silently
# grinding the server down. Old finished jobs are pruned so the in-memory
# job/clip lists cannot grow without bound.
MAX_CONCURRENT_JOBS = 2
MAX_STORED_JOBS = 50

@app.post("/api/jobs")
def create_clipping_job(req: CreateJobRequest, background_tasks: BackgroundTasks, request: Request):
    # Validate the request before spending any CPU on it. The URL host
    # check (YouTube/Twitch only) runs here so a bad URL is rejected
    # immediately instead of failing inside a background job.
    try:
        validate_stream_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.layout not in ("fill", "split", "blur", "stream"):
        raise HTTPException(status_code=400, detail="Invalid layout.")
    fps = req.fps or 30
    if fps not in (30, 60):
        raise HTTPException(status_code=400, detail="FPS must be 30 or 60.")
    num_clips = req.num_clips or 3
    if not 1 <= num_clips <= 5:
        raise HTTPException(status_code=400, detail="Number of clips must be between 1 and 5.")
    duration = req.duration or 30
    if not 5 <= duration <= 120:
        raise HTTPException(status_code=400, detail="Clip duration must be between 5 and 120 seconds.")

    running = sum(1 for j in JOBS_DB.values() if j.get("status") == "processing")
    if running >= MAX_CONCURRENT_JOBS:
        raise HTTPException(status_code=429, detail="Too many clip jobs running right now. Wait for one to finish and try again.")

    # Prune the oldest finished jobs (keep the newest 50).
    finished = [jid for jid, j in JOBS_DB.items() if j.get("status") in ("completed", "failed")]
    if len(JOBS_DB) > MAX_STORED_JOBS and finished:
        finished.sort(key=lambda jid: JOBS_DB[jid].get("created_at", 0))
        for jid in finished[: len(JOBS_DB) - MAX_STORED_JOBS]:
            JOBS_DB.pop(jid, None)
            CLIPS_DB.pop(jid, None)

    job_id = f"job-{uuid.uuid4().hex[:8]}"
    layout = req.layout or "fill"
    subtitles = req.subtitles if req.subtitles is not None else True
    user = getattr(request.state, "user", {}) or {}

    JOBS_DB[job_id] = {
        "id": job_id,
        "url": req.url,
        "user": user.get("username"),
        "status": "processing",
        "progress": 10,
        "message": "Initiating stream ingestion & excitement analysis...",
        "layout": layout,
        "duration": duration,
        "fps": fps,
        "num_clips": num_clips,
        "created_at": time.time()
    }
    background_tasks.add_task(
        background_process_stream,
        job_id,
        req.url,
        layout,
        duration,
        subtitles,
        fps,
        num_clips
    )
    return {"job_id": job_id, "status": "processing"}

@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str):
    if job_id not in JOBS_DB:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS_DB[job_id]

@app.get("/api/jobs/{job_id}/clips")
def get_job_clips(job_id: str):
    if job_id not in CLIPS_DB:
        status = JOBS_DB.get(job_id, {}).get("status", "processing")
        return {"job_id": job_id, "status": status, "clips": []}
    return {"job_id": job_id, "status": "completed", "clips": CLIPS_DB[job_id]}

@app.post("/api/clips/{clip_id}/trim")
def trim_clip(clip_id: str, req: TrimClipRequest):
    if not (0 <= req.start_time < req.end_time) or (req.end_time - req.start_time) > 600:
        raise HTTPException(status_code=400, detail="Invalid trim range.")
    for job_id, clips in CLIPS_DB.items():
        for clip in clips:
            if clip["id"] == clip_id:
                # The trim inputs are SOURCE timestamps (the clip covers
                # source seconds start_time..end_time). Re-cut the RENDERED
                # file to the new range — the old code only rewrote the
                # numbers and left the video untouched, so a "trimmed" clip
                # downloaded/published as the original. The new range must
                # stay inside the clip's original range (the rendered file
                # only holds that footage); outside it, generate a new clip.
                orig_start = float(clip.get("start_time", 0) or 0)
                orig_end = float(clip.get("end_time", 0) or 0)
                if orig_end <= orig_start:
                    raise HTTPException(status_code=400, detail="This clip has no stored range to trim.")
                if req.start_time < orig_start - 0.01 or req.end_time > orig_end + 0.01:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Trim must stay inside this clip's range ({orig_start:.0f}–{orig_end:.0f}s). To use footage outside it, generate a new clip.")
                video_url = clip.get("video_url", "")
                local_p = os.path.join(CLIPS_DIR, os.path.basename(video_url))
                if not os.path.exists(local_p):
                    raise HTTPException(status_code=400, detail="Clip file not found on the server.")
                rel_start = max(0.0, req.start_time - orig_start)
                rel_dur = req.end_time - req.start_time
                tmp_p = local_p + ".trim.mp4"
                ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
                cmd = [ffmpeg_bin, "-y", "-ss", str(rel_start), "-t", str(rel_dur),
                       "-i", local_p, "-c:v", "libx264", "-preset", "medium",
                       "-crf", "14", "-minrate", "8M", "-maxrate", "16M", "-bufsize", "16M",
                       "-profile:v", "high", "-pix_fmt", "yuv420p",
                       "-movflags", "+faststart", "-c:a", "aac", "-b:a", "192k", tmp_p]
                try:
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, timeout=800)
                except subprocess.TimeoutExpired:
                    raise HTTPException(status_code=500, detail="Trim took too long — try a shorter range.")
                if res.returncode != 0 or not os.path.exists(tmp_p):
                    if os.path.exists(tmp_p):
                        os.remove(tmp_p)
                    raise HTTPException(status_code=500, detail="Trim failed. Try again.")
                # Swap in the trimmed file (same directory, so a rename
                # works; clip files are regular files inside the mounted
                # clips dir, not single-file bind mounts).
                os.replace(tmp_p, local_p)
                clip["start_time"] = req.start_time
                clip["end_time"] = req.end_time
                clip["duration"] = round(req.end_time - req.start_time, 2)
                return {"status": "success", "clip": clip}
    raise HTTPException(status_code=404, detail="Clip not found")

@app.get("/api/social/accounts")
def get_social_accounts(request: Request):
    # Enrich each account with whether it can actually post right now.
    # YouTube is PER USER now: the requesting user's own linked channel
    # (real tokens in youtube_tokens.json). TikTok/Instagram have no API
    # credentials wired up yet, so they can never post from here — the UI
    # shows the saved handle honestly instead of "Active".
    user = _require_active_user(request)
    username = user["username"] if user else None
    accounts = json.loads(json.dumps(publisher.accounts))
    channel = yt_oauth.get_channel(username) if username else None
    can_post = yt_oauth.get_credentials(username) is not None if username else False
    accounts["youtube"]["connected"] = bool(can_post)
    accounts["youtube"]["can_post"] = bool(can_post)
    accounts["youtube"]["channel_name"] = channel.get("title") if channel else None
    accounts["youtube"]["username"] = channel.get("title") if channel else None
    # Daily Shorts limit, so the dashboard can SHOW it (editors: 10/day,
    # admin: no limit). Only successful uploads count (see upload_limits).
    if user and user.get("role") == "admin":
        accounts["youtube"]["daily_limit"] = None
        accounts["youtube"]["uploads_today"] = 0
        accounts["youtube"]["uploads_remaining"] = None
    else:
        accounts["youtube"]["daily_limit"] = DAILY_UPLOAD_LIMIT
        accounts["youtube"]["uploads_today"] = get_uploads_today(username) if username else 0
        accounts["youtube"]["uploads_remaining"] = max(
            0, DAILY_UPLOAD_LIMIT - accounts["youtube"]["uploads_today"])
    # Never expose any stored access token to the frontend — the UI
    # does not use them, and a leftover fabricated token used to leak
    # through here. Real YouTube auth lives in youtube_tokens.json and
    # is only ever read server-side.
    for acc in accounts.values():
        if isinstance(acc, dict):
            acc["access_token"] = None
    accounts["tiktok"]["can_post"] = False
    accounts["instagram"]["can_post"] = False
    return {"accounts": accounts}

@app.post("/api/social/connect/{platform}")
def connect_social_account(
    platform: str,
    username: Optional[str] = None,
    body: Optional[ConnectAccountBody] = None
):
    platform = platform.lower()
    if platform not in publisher.accounts:
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")
    
    chosen_user = (body.username or body.channel_name) if (body and (body.username or body.channel_name)) else (username or f"{platform.capitalize()}Creator")
    # Note: a caller-supplied access token is deliberately NOT stored —
    # typed-in "tokens" were how fake connected accounts were planted.
    account = publisher.connect_account(platform, chosen_user, None)
    return {"status": "connected", "platform": platform, "account": account}

@app.post("/api/social/disconnect/{platform}")
def disconnect_social_account(platform: str, request: Request):
    platform = platform.lower()
    if platform not in publisher.accounts:
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")
    if platform == "youtube":
        # YouTube disconnects are per user — each user unlinks only
        # their own channel.
        user = _require_active_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Sign in to disconnect a channel.")
        return yt_oauth.disconnect(user["username"])
    return publisher.disconnect_account(platform)

@app.post("/api/publish")
def publish_clip_endpoint(req: PublishRequest, request: Request):
    if not req.platforms:
        raise HTTPException(status_code=400, detail="Must select at least one platform")
    if any(p.lower() not in ("youtube", "tiktok", "instagram") for p in req.platforms):
        raise HTTPException(status_code=400, detail="Unsupported platform.")
    privacy = req.privacy or "public"
    if privacy not in ("public", "unlisted", "private"):
        raise HTTPException(status_code=400, detail="Privacy must be public, unlisted or private.")
    # YouTube publishes go to the REQUESTING user's own linked channel.
    # Never fall back to the admin's channel for someone else.
    user = _require_active_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to publish.")

    # Locate the actual clip file on disk. No fallback guess: if the file
    # is not here, say so instead of "publishing" a file that does not exist.
    video_file = None
    for j_id, clips in CLIPS_DB.items():
        for c in clips:
            if c.get("id") == req.clip_id:
                v_url = c.get("video_url", "")
                basename = os.path.basename(v_url)
                local_p = os.path.join(CLIPS_DIR, basename)
                if os.path.exists(local_p):
                    video_file = local_p
                break
        if video_file:
            break
    if not video_file:
        raise HTTPException(status_code=400, detail="Clip file not found on the server. Generate the clip first.")

    # Daily Shorts limit: editors get DAILY_UPLOAD_LIMIT YouTube uploads
    # per day (IST day). Admins have NO limit. Only successful uploads
    # count, so a failed attempt never eats into the quota.
    is_youtube = any(p.lower() == "youtube" for p in req.platforms)
    is_admin = user.get("role") == "admin"
    if is_youtube and not is_admin and get_uploads_today(user["username"]) >= DAILY_UPLOAD_LIMIT:
        raise HTTPException(status_code=429, detail="Daily limit reached: you've posted 10 Shorts today. Try again tomorrow.")

    result = publisher.publish_clip(
        video_path=video_file,
        platforms=req.platforms,
        title=req.title,
        description=req.description or req.title,
        tags=req.tags or ["Shorts", "Viral", "Gaming"],
        privacy=privacy,
        youtube_oauth=yt_oauth,
        username=user["username"]
    )

    if is_youtube and not is_admin:
        dispatched = (result or {}).get("dispatched", {})
        if dispatched.get("youtube", {}).get("status") == "success":
            record_upload(user["username"])

    return result

# --- Authentication & Access Control Endpoints ---

@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else None
    auth_res = auth_mgr.authenticate(req.username, req.password, client_ip=client_ip)
    if not auth_res:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if "error" in auth_res:
        raise HTTPException(status_code=403, detail=auth_res["error"])

    _set_session_cookie(response, auth_res["token"])
    return auth_res

# NOTE: there is deliberately NO unverified email sign-in endpoint any
# more. The old /api/auth/verify-email accepted any email address with no
# proof of ownership, so anyone could sign in as any approved user. The
# only way in with a Gmail is Google sign-in below (Google verifies the
# address); the admin keeps the username+password login above.

@app.get("/api/auth/google/login")
def google_auth_login(request: Request):
    redirect_uri = f"{str(request.base_url).rstrip('/')}/api/auth/google/callback"
    url = yt_oauth.get_auth_url(redirect_uri, scopes=LOGIN_SCOPES)
    if not url:
        return RedirectResponse(url="/login?error=google_not_configured")
    return RedirectResponse(url=url)

@app.get("/api/auth/google/callback")
def google_auth_callback(code: str, request: Request, state: Optional[str] = None):
    redirect_uri = f"{str(request.base_url).rstrip('/')}/api/auth/google/callback"
    try:
        userinfo = yt_oauth.get_google_userinfo(code, redirect_uri, state)
        email = userinfo.get("email")
        if not email:
            return RedirectResponse(url="/login?error=no_email_from_google")

        res = auth_mgr.authenticate_email(email, name=userinfo.get("name"))
        if "error" in res:
            return RedirectResponse(url="/login?error=access_denied")

        response = RedirectResponse(url="/")
        _set_session_cookie(response, res["token"])
        return response
    except Exception as e:
        # Never put the raw exception (paths, library messages) in the URL
        # the browser shows; the detail goes to the server log only.
        print(f"[Google OAuth Error] {e}")
        return RedirectResponse(url="/login?error=oauth_error")

@app.get("/api/auth/me")
def get_current_user(request: Request):
    token = request.cookies.get("sc_token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    user = auth_mgr.verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"user": user}

@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    # Log out server-side too: deleting the browser cookie alone left the
    # token valid for its full 7 days, so a copied token kept working.
    token = request.cookies.get("sc_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    auth_mgr.invalidate_token(token)
    response.delete_cookie(key="sc_token", path="/")
    return {"status": "logged_out"}

@app.post("/api/auth/change-password")
def change_password(req: ChangePasswordRequest, request: Request, response: Response):
    token = request.cookies.get("sc_token")
    user = auth_mgr.verify_token(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        result = auth_mgr.change_password(user["username"], req.current_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # The password change invalidates all sessions, including this one:
    # clear the cookie so the browser asks for the new password next.
    response.delete_cookie(key="sc_token", path="/")
    return result

# --- Admin User Management Endpoints ---

@app.get("/api/admin/users")
def get_admin_users():
    return {"users": auth_mgr.list_users()}

@app.post("/api/admin/users")
def add_admin_user(req: AddUserRequest):
    try:
        user_id = req.username or (req.email.split("@")[0].lower() if req.email else req.name.lower().replace(" ", "_"))
        user = auth_mgr.add_user(
            username=user_id,
            password=req.password,
            name=req.name,
            role=req.role or "editor",
            email=req.email or user_id
        )
        return {"status": "success", "user": user}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/admin/users/{username}/approve")
def approve_admin_user(username: str):
    """Admin approves a pending sign-in, granting tool access."""
    try:
        return auth_mgr.approve_user(username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/admin/users/{username}/status")
def update_user_status(username: str, req: UpdateUserStatusRequest):
    try:
        res = auth_mgr.update_user_status(username, req.status)
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/admin/users/{username}")
def delete_admin_user(username: str):
    try:
        auth_mgr.delete_user(username)
        return {"status": "deleted", "username": username}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- Google OAuth & YouTube Connection Endpoints ---

@app.get("/api/admin/oauth/config")
def get_oauth_config():
    return {
        "google": {
            "configured": yt_oauth.config.get("configured", False),
            "client_id": yt_oauth.config.get("client_id"),
            "connected_channel": yt_oauth.get_channel("admin")
        },
        "accounts": publisher.accounts,
        "connected_users": yt_oauth.list_connected_users()
    }

class YoutubeCookiesRequest(BaseModel):
    content: str

def _save_youtube_cookies(content: str) -> int:
    """Validates + saves a browser-exported cookies.txt for yt-dlp.
    Returns the number of youtube.com cookie lines saved. Shared by the
    admin-only endpoint and the dashboard endpoint (any active user may
    refresh the cookies when the tool stops working)."""
    content = content or ""
    if len(content) > 1_000_000:
        raise HTTPException(status_code=400, detail="File too large (max 1 MB).")
    lines = [l for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
    yt_lines = [l for l in lines if "youtube.com" in l]
    if not yt_lines:
        raise HTTPException(status_code=400, detail="No youtube.com cookies found in this file. Export the cookies while signed in to youtube.com and try again.")
    # Write in place, NOT via os.replace: the cookies file is a bind-mount
    # into the container and renaming over a mount point fails (EBUSY).
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(COOKIES_FILE, 0o600)
    # Record the TRUE upload moment in the sidecar (see
    # COOKIES_UPLOADED_AT_FILE). The cookies file's mtime is not a
    # reliable upload time — it has advanced without any upload API call
    # — so the timer reads this sidecar, never the mtime, when present.
    # The sidecar holds the upload timestamp plus a hash of the uploaded
    # content, so the status endpoint can tell an external replacement
    # (different content) apart from a mere touch (same content).
    try:
        import hashlib
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with open(COOKIES_UPLOADED_AT_FILE, "w", encoding="utf-8") as f:
            f.write(datetime.now(tz=timezone.utc).isoformat() + "\n" + content_hash)
        os.chmod(COOKIES_UPLOADED_AT_FILE, 0o600)
    except OSError:
        pass
    return len(yt_lines)

@app.post("/api/admin/youtube-cookies")
def upload_youtube_cookies(req: YoutubeCookiesRequest):
    """Admin refreshes the YouTube login cookies yt-dlp uses to get past
    YouTube's bot wall. The cookies come from a browser export (the "Get
    cookies.txt LOCALLY" extension, exported while signed in to
    youtube.com). YouTube rotates these cookies every few weeks, so they
    expire — when they do, every Generate fails with the sign-in error
    and this upload is the fix. The file is stored with private
    permissions and is never served or shown anywhere."""
    return {"status": "saved", "youtube_cookies": _save_youtube_cookies(req.content)}

@app.post("/api/youtube-cookies")
def upload_youtube_cookies_dashboard(req: YoutubeCookiesRequest, request: Request):
    """Dashboard upload: any APPROVED user may refresh the YouTube login
    cookies when Generate stops working (same file + validation as the
    admin endpoint). Pending users cannot — they wait for approval."""
    user = _require_active_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Your account is waiting for admin approval. Ask the admin to approve your account first.")
    return {"status": "saved", "youtube_cookies": _save_youtube_cookies(req.content)}

# Cookies live ~2 hours from the Oracle datacenter IP (observed across
# 9 batches: most die at ~2h, a few at ~2h40m, one at ~20min). The timer
# uses 2h as the nominal life and labels it an estimate — the real
# rotation is YouTube's call, not ours.
COOKIE_LIFETIME_SECONDS = 2 * 3600

def _recorded_upload_time() -> tuple:
    """Returns (upload_epoch_seconds, source) for the cookies timer.
    Prefers the sidecar written by _save_youtube_cookies at upload time
    ('upload') — the true upload moment. The sidecar also stores a hash
    of the uploaded content: if the live file's hash differs, the file
    was replaced outside the upload endpoints, so the timer falls back
    to the file's mtime ('mtime') — the replacement moment. A mere touch
    (same content, newer mtime) does NOT move the timer, which was the
    reported bug. Falls back to mtime when no sidecar exists yet (e.g.
    the file was placed on the server by hand before this fix shipped).
    The mtime fallback is labelled as such so a stale-looking time can
    be traced."""
    try:
        with open(COOKIES_UPLOADED_AT_FILE, "r", encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        if lines:
            dt = datetime.fromisoformat(lines[0].strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            # Sidecar v2 stores the content hash on line 2. If present
            # and the live file differs, the file was replaced outside
            # the API — the sidecar time is stale, use the mtime.
            if len(lines) >= 2 and lines[1].strip():
                try:
                    import hashlib
                    with open(COOKIES_FILE, "rb") as cf:
                        live_hash = hashlib.sha256(cf.read()).hexdigest()
                    if live_hash != lines[1].strip():
                        return os.path.getmtime(COOKIES_FILE), "mtime"
                except OSError:
                    pass
            return dt.timestamp(), "upload"
    except (OSError, ValueError):
        pass
    return os.path.getmtime(COOKIES_FILE), "mtime"

@app.get("/api/youtube-cookies/status")
def youtube_cookies_status(request: Request):
    """Cookie health timer for the dashboard + admin panel: when the
    cookies were last uploaded, when they are expected to expire, and
    whether they are already past it. The 'updated' time is the
    recorded upload moment (sidecar written at upload time), NOT the
    cookies file's mtime — the mtime has been observed advancing with
    no upload API call, which made the timer show "now" when the user
    had uploaded long before. Any active user may read it — the
    dashboard shows the timer to everyone who can generate clips.
    The timer ALSO checks the file actually contains the login cookies
    yt-dlp needs (LOGIN_INFO + SID). A fresh upload that is missing
    LOGIN_INFO (exported while not signed in to YouTube, or from the
    wrong profile) looks 'active' by time alone but still fails every
    download — so the status reports it as invalid instead of active."""
    user = _require_active_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Your account is waiting for admin approval.")
    if not os.path.exists(COOKIES_FILE):
        return {"exists": False, "updated_at": None, "expires_at": None,
                "seconds_remaining": 0, "expired": True, "valid": False,
                "reason": "no_cookies", "time_source": "none"}
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        content = ""
    has_login_info = "\tLOGIN_INFO\t" in content
    has_sid = "\tSID\t" in content
    uploaded_at, time_source = _recorded_upload_time()
    if not (has_login_info and has_sid):
        return {"exists": True,
                "updated_at": datetime.fromtimestamp(uploaded_at, tz=timezone.utc).isoformat(),
                "expires_at": datetime.fromtimestamp(uploaded_at + COOKIE_LIFETIME_SECONDS, tz=timezone.utc).isoformat(),
                "seconds_remaining": max(0, int(uploaded_at + COOKIE_LIFETIME_SECONDS - time.time())),
                "expired": False, "valid": False,
                "reason": "missing_login_info" if not has_login_info else "missing_sid",
                "time_source": time_source}
    expires = uploaded_at + COOKIE_LIFETIME_SECONDS
    remaining = max(0.0, expires - time.time())
    return {
        "exists": True,
        "updated_at": datetime.fromtimestamp(uploaded_at, tz=timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
        "seconds_remaining": int(remaining),
        "expired": remaining <= 0,
        "valid": True,
        "reason": None,
        "time_source": time_source,
    }

@app.post("/api/admin/oauth/google")
def save_google_credentials(req: GoogleCredsRequest):
    return yt_oauth.set_client_credentials(req.client_id, req.client_secret)

def _require_admin(request: Request):
    """Returns the admin user for this request, or None. The /api/oauth/*
    routes are public in the middleware (the login flow needs them), so
    the admin check for channel linking lives here, reading the session
    cookie (or a Bearer token) directly."""
    token = request.cookies.get("sc_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    user = auth_mgr.verify_token(token) if token else None
    if not user or user.get("role") != "admin":
        return None
    return user

def _require_active_user(request: Request):
    """Returns the signed-in user when the account is approved
    (status == 'active'), else None. Pending users can look at the
    dashboard but may not use any tool feature — including linking a
    channel or publishing. (verify_token already re-reads status from
    the DB, so an approval takes effect without re-login.)"""
    token = request.cookies.get("sc_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    user = auth_mgr.verify_token(token) if token else None
    if not user or user.get("status") != "active":
        return None
    return user

@app.get("/api/oauth/google/auth-url")
def get_google_auth_url(request: Request):
    # Any APPROVED user may link their OWN channel now — the flow is
    # bound to that user, so nobody can link a channel to someone
    # else's account. Pending users cannot: they wait for admin
    # approval first. (/api/oauth/* is public in the middleware because
    # the login flow needs it — the check lives here.)
    user = _require_active_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Your account is waiting for admin approval. Ask the admin to approve your account first.")
    redirect_uri = f"{str(request.base_url).rstrip('/')}/api/oauth/google/callback"
    url = yt_oauth.get_auth_url(redirect_uri, username=user["username"])
    if not url:
        raise HTTPException(status_code=400, detail="Google credentials not configured.")
    return {"auth_url": url}

@app.get("/api/oauth/google/callback")
def google_oauth_callback(code: str, request: Request, state: Optional[str] = None):
    user = _require_active_user(request)
    if not user:
        return RedirectResponse(url="/login?error=admin_required")
    redirect_uri = f"{str(request.base_url).rstrip('/')}/api/oauth/google/callback"
    try:
        res = yt_oauth.exchange_code(code, redirect_uri, state, username=user["username"])
        # Each user posts to their OWN channel now — nothing shared to sync.
        if user.get("role") == "admin":
            return RedirectResponse(url="/admin?google_connected=true")
        return RedirectResponse(url="/?google_connected=true")
    except Exception as e:
        # Generic error in the URL only — the detail goes to the server
        # log, never into a redirect where it could leak internals.
        print(f"[OAuth callback error] {e}")
        if user.get("role") == "admin":
            return RedirectResponse(url="/admin?error=oauth_error")
        return RedirectResponse(url="/?error=oauth_error")

# --- HTML Page Routes ---

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/login")
def serve_login_page():
    p = os.path.join(STATIC_DIR, "login.html")
    if os.path.exists(p):
        return FileResponse(p)
    return {"message": "Login page not found"}

@app.get("/admin")
def serve_admin_page():
    p = os.path.join(STATIC_DIR, "admin.html")
    if os.path.exists(p):
        return FileResponse(p)
    return {"message": "Admin page not found"}

@app.get("/")
def serve_dashboard():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "StreamClipper API running. Dashboard file not found."}
