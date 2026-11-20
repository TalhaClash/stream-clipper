import json
import os
import shutil
import subprocess
from typing import Dict, Any, Optional
from urllib.parse import urlparse

# The tool only ever clips YouTube and Twitch. Anything else is rejected
# before yt-dlp ever runs — this closes SSRF (the server fetching arbitrary
# internal URLs on a user's say-so) and stops a "-" URL being parsed as a
# yt-dlp command-line flag.
ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "twitch.tv", "www.twitch.tv", "clips.twitch.tv",
}

def validate_stream_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("A video URL is required.")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http(s) video URLs are supported.")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS and not host.endswith(".youtube.com"):
        raise ValueError("Only YouTube and Twitch URLs are supported.")
    return url.strip()

# Optional YouTube cookies (Netscape format, exported from the user's
# browser). If present, yt-dlp sends them with every request, which is
# the standard way past YouTube's "Sign in to confirm you're not a bot"
# wall on datacenter IPs. Absent = anonymous extraction.
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube_cookies.txt")

# Sidecar recording the TRUE last-upload time (UTC ISO timestamp).
# The cookies file's mtime is NOT a reliable upload time: the file has
# been observed changing without any upload API call (zero POSTs to the
# upload endpoints in the container's lifetime, yet the mtime advanced
# to "now" — 20 Nov 2026). Anything that touches the file (a direct
# copy, a deploy, an agent write) resets the mtime, so the timer showed
# "Updated ... 19:58" when the user last uploaded "long time ago".
# The sidecar is written ONLY by _save_youtube_cookies (the upload
# endpoints), so it records the real upload moment. It is bind-mounted
# (see docker-compose.yml) so it survives container rebuilds.
COOKIES_UPLOADED_AT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "youtube_cookies_uploaded_at.txt")

# Live Chrome profile on the server (headless browser signed into YouTube
# with the tool-only account). When its Cookies DB exists, yt-dlp reads
# fresh cookies straight from the profile, so manual cookie exports are
# unnecessary. Falls back to COOKIES_FILE. The profile is bind-mounted
# into the container at this path (see docker-compose.yml).
BROWSER_PROFILE = "/app/yt-profile"


def _cookies_file_valid() -> bool:
    """True when the exported cookies file holds a real YouTube session
    (SID + LOGIN_INFO). A partial export (missing LOGIN_INFO) is useless
    and must not win over the live profile."""
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read(65536)
        return "LOGIN_INFO" in text and "\tSID\t" in text
    except OSError:
        return False


class StreamDownloader:
    """Handles extracting metadata and media from YouTube and Twitch streams/VODs."""

    def __init__(self, ytdlp_path: Optional[str] = None):
        if ytdlp_path:
            self.ytdlp_path = ytdlp_path
        else:
            venv_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "yt-dlp")
            if os.path.exists(venv_bin):
                self.ytdlp_path = venv_bin
            else:
                self.ytdlp_path = shutil.which("yt-dlp") or "yt-dlp"

    def _base_args(self) -> list:
        """Flags every yt-dlp call needs: the Deno JS runtime, YouTube's
        EJS challenge-solver components (downloaded from GitHub at runtime
        — without them the 'n' signature challenge fails and no video/audio
        formats are returned), plus the cookies file when one has been
        provided.

        NOTE: no --proxy here. The Mac SOCKS tunnel was tried (deploy #36)
        but it SLOWED yt-dlp down — metadata calls timed out at 60s
        through it — and ffmpeg can't use a SOCKS proxy at all, so the
        clip cut bypassed it anyway. Direct + fresh Mac cookies is the
        proven fast path."""
        args = ["--js-runtimes", "deno", "--remote-components", "ejs:github"]
        profile_cookies = os.path.join(BROWSER_PROFILE, "Default", "Network", "Cookies")
        if _cookies_file_valid():
            # A fresh Mac export wins: its session (real Chrome, home IP)
            # is the one YouTube trusts for media on restricted videos.
            args += ["--cookies", COOKIES_FILE]
        elif os.path.exists(profile_cookies):
            # Live browser profile: its cookies refresh themselves.
            args += ["--cookies-from-browser", "chrome:" + BROWSER_PROFILE]
        elif os.path.exists(COOKIES_FILE):
            args += ["--cookies", COOKIES_FILE]
        return args

    def get_info(self, url: str) -> Dict[str, Any]:
        """Fetches video metadata (title, duration, uploader, thumbnail) without downloading."""
        url = validate_stream_url(url)
        cmd = [
            self.ytdlp_path,
            "--dump-json",
            "--no-playlist",
            *self._base_args(),
            url
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=800)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp metadata extraction failed: {result.stderr.strip()}")
        
        info = json.loads(result.stdout)
        return {
            "id": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"),
            # Live streams report duration None — `or 0` keeps this a float.
            "duration": float(info.get("duration") or 0), # in seconds
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url", url),
            # Live state: a stream that is still running (or hasn't started)
            # has no end, so the audio download hangs until it times out.
            # The pipeline rejects those up front (see _reject_live_stream).
            "is_live": bool(info.get("is_live")),
            "live_status": info.get("live_status"),
            "formats": info.get("formats", [])
        }

    def download_audio_sample(self, url: str, output_path: str) -> str:
        """
        Downloads low-bitrate audio stream for ultra-fast volume & excitement analysis
        without re-encoding or downloading high-res video.
        """
        url = validate_stream_url(url)
        base_dir = os.path.dirname(output_path) or "/tmp"
        base_name = os.path.splitext(os.path.basename(output_path))[0]
        temp_out = os.path.join(base_dir, f"{base_name}.%(ext)s")

        cmd = [
            self.ytdlp_path,
            "-f", "ba[abr<=50]/ba[abr<=64]/ba/b",
            "-o", temp_out,
            "--no-playlist",
            *self._base_args(),
            url
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=800)
        if result.returncode != 0:
            raise RuntimeError(f"Audio download failed: {result.stderr.strip()}")

        # Locate downloaded audio file
        for fname in os.listdir(base_dir):
            if fname.startswith(base_name) and not fname.endswith(".part"):
                actual_file = os.path.join(base_dir, fname)
                # If target extension specified and different, rename or return actual
                return actual_file

        raise RuntimeError(f"Audio download succeeded but file not found at {output_path}")

    def cut_clip(self, url: str, start_time: float, duration: float, output_path: str) -> str:
        """
        Uses yt-dlp to stream-cut only the required clip segment
        without downloading the entire stream video.
        Downloads up to 1080p: the clip is rendered at 1080x1920, and a
        720p source upscaled to 1080p can never look sharp — the detail
        has to come from the source. Automatically detects and aligns
        video keyframe offsets to prevent frozen starting frames.
        """
        url = validate_stream_url(url)
        start_str = f"{int(start_time // 3600):02d}:{int((start_time % 3600) // 60):02d}:{int(start_time % 60):02d}"
        end_time = start_time + duration
        end_str = f"{int(end_time // 3600):02d}:{int((end_time % 3600) // 60):02d}:{int(end_time % 60):02d}"

        base_dir = os.path.dirname(output_path) or "/tmp"
        base_name = os.path.splitext(os.path.basename(output_path))[0]
        temp_target = os.path.join(base_dir, f"{base_name}_temp.%(ext)s")

        # Prefer H.264 (avc1) video: yt-dlp's plain bestvideo[height<=1080]
        # picks AV1 (itag 399) on many videos, and AV1 + --remux-video mp4
        # breaks the merge — ffmpeg writes a .webm, yt-dlp then fails
        # with "No such file ..._temp.webm.part", and the cut is slow.
        # H.264 remuxes cleanly to mp4 (the formatter outputs H.264
        # anyway). Fall back to any 1080p, then any best, when no H.264
        # 1080p exists for this video.
        cmd = [
            self.ytdlp_path,
            "--download-sections", f"*{start_str}-{end_str}",
            "-f", "bestvideo[height<=1080][vcodec^=avc1]+bestaudio/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "--remux-video", "mp4",
            "-o", temp_target,
            *self._base_args(),
            url
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=800)
        if result.returncode != 0:
            raise RuntimeError(f"Clip cutting failed: {result.stderr.strip()}")

        # Locate raw cut file
        raw_cut = None
        for fname in os.listdir(base_dir):
            if fname.startswith(f"{base_name}_temp") and not fname.endswith(".part"):
                raw_cut = os.path.join(base_dir, fname)
                break

        if not raw_cut:
            raise RuntimeError(f"Clip segment cut completed but could not locate {output_path}")

        # Check video stream start_time offset to eliminate frozen initial frames
        v_start = 0.0
        # ffprobe/ffmpeg from PATH (the container installs them via apt;
        # the old hardcoded macOS paths do not exist on the server).
        ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
        try:
            probe_cmd = [
                ffprobe_bin, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=start_time",
                "-of", "json", raw_cut
            ]
            p_res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, text=True, timeout=800)
            data = json.loads(p_res.stdout)
            v_start = float(data["streams"][0].get("start_time", 0.0))
        except Exception:
            v_start = 0.0

        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        if v_start > 0.05:
            # Re-align audio and video stream so video motion starts immediately at 0.0s
            aligned_file = os.path.join(base_dir, f"{base_name}_aligned.mp4")
            align_cmd = [
                ffmpeg_bin, "-y",
                "-ss", str(v_start),
                "-i", raw_cut,
                "-t", str(duration),
                "-c", "copy",
                aligned_file
            ]
            subprocess.run(align_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=800)
            if os.path.exists(aligned_file):
                if os.path.exists(raw_cut):
                    os.remove(raw_cut)
                shutil.move(aligned_file, output_path)
                return output_path

        if raw_cut != output_path:
            shutil.move(raw_cut, output_path)

        return output_path
