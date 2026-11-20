import os
import shutil
import subprocess
from typing import Dict, Any, Optional, Tuple

class VideoFormatter:
    """
    Reframes 16:9 gaming streams into high-engagement 9:16 (1080x1920) vertical videos
    suitable for TikTok, YouTube Shorts, and Instagram Reels.
    """

    def __init__(self, ffmpeg_path: Optional[str] = None):
        # ffmpeg from PATH (the container installs it via apt; the old
        # hardcoded macOS path does not exist on the server).
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_path = shutil.which("ffprobe") or "ffprobe"

    def build_fill_screen_filter(
        self,
        output_width: int = 1080,
        output_height: int = 1920,
        fps: int = 30
    ) -> str:
        """
        Crops and scales video to full-screen 9:16 vertical (1080x1920).
        Fills 100% of the mobile screen with ZERO black/blurred bars top and bottom.
        Supports customizable framerate (30fps or 60fps for ultra-smooth gaming).
        """
        return f"[0:v]scale={output_width}:{output_height}:force_original_aspect_ratio=increase,crop={output_width}:{output_height}:(in_w-{output_width})/2:(in_h-{output_height})/2,setsar=1,fps={fps}[v]"

    def build_split_screen_filter(
        self,
        facecam_box: Optional[Tuple[int, int, int, int]] = None,
        source_width: int = 1280,
        source_height: int = 720,
        output_width: int = 1080,
        output_height: int = 1920,
        fps: int = 30
    ) -> str:
        """
        Creates an FFmpeg filtergraph for a 9:16 split layout:
        - Top 720px: Streamer facecam (cropped & scaled to 1080x720)
        - Bottom 1200px: Action-focused gameplay (cropped & scaled to 1080x1200)
        Supports customizable framerate (30fps or 60fps).

        NO-CAM FALLBACK (deploy #71): when facecam_box is None — no
        streamer cam was detected anywhere in the source — the layout
        degrades to FULL GAMEPLAY filling the whole 9:16 frame, exactly
        like the fill layout. It must NEVER invent a cam from a corner:
        the old bottom-left fallback box (20, h-260, 300, 240) is where
        GTA puts its minimap, so a no-cam video rendered the minimap
        blown up on top (job-80a632f1, Phoenix video). A missing cam
        means full gameplay, not a game HUD on top.
        """
        if facecam_box is None:
            return self.build_fill_screen_filter(
                output_width=output_width, output_height=output_height,
                fps=fps)
        fx, fy, fw, fh = facecam_box

        gw = int(source_height * (output_width / 1200))
        gx = (source_width - gw) // 2

        filter_str = (
            f"[0:v]split=2[cam_in][game_in];"
            f"[cam_in]crop={fw}:{fh}:{fx}:{fy},scale={output_width}:720:force_original_aspect_ratio=increase,crop={output_width}:720,setsar=1[cam_out];"
            f"[game_in]crop={gw}:{source_height}:{gx}:0,scale={output_width}:1200:force_original_aspect_ratio=increase,crop={output_width}:1200,setsar=1[game_out];"
            f"[cam_out][game_out]vstack=inputs=2,fps={fps}[v]"
        )
        return filter_str

    def build_streamer_46_filter(
        self,
        facecam_box: Optional[Tuple[int, int, int, int]] = None,
        source_width: int = 1280,
        source_height: int = 720,
        output_width: int = 1080,
        output_height: int = 1920,
        fps: int = 30
    ) -> str:
        """
        Creates an FFmpeg filtergraph for a 9:16 streamer 4:6 layout:
        - Top 768px (~40%): The streamer's WHOLE cam window, FITTED —
          the detected PiP rectangle is scaled to fit 1080 wide and
          centred over a blurred copy of itself, so the ENTIRE cam
          (head, shoulders, room) is visible with nothing cropped off.
          The old fill scaling cropped the PiP's sides and showed only
          a zoomed face. Same facecam source (detected PiP rectangle,
          else the expanded face box, else the top-right corner).
        - Bottom 1152px (~60%): Gameplay, VERTICAL FILL — a vertical
          slice of the source (aspect 1080/1152) is cropped and scaled
          to fill the whole bottom section, edge to edge, NO blurred
          bands. The old fit-over-blur left the gameplay as a thin
          horizontal 16:9 strip with blur above and below — the user
          wants it vertical, filling the section like his reference
          (road scene full-bleed). The slice is positioned to avoid
          the cam box, so the little streamer window is cropped OUT.
          The split is 4:6 on the user's word — he showed a reference
          frame with the streamer big and clear on top and the gameplay
          below.
        Supports customizable framerate (30fps or 60fps).

        NO-CAM FALLBACK (deploy #71): when facecam_box is None — no
        streamer cam was detected anywhere in the source — the layout
        degrades to FULL GAMEPLAY filling the whole 9:16 frame, exactly
        like the fill layout. It must NEVER invent a cam from a corner:
        the old bottom-left fallback box (20, h-260, 300, 240) is where
        GTA puts its minimap, so a no-cam video rendered the minimap
        blown up on top (job-80a632f1, Phoenix video). A missing cam
        means full gameplay, not a game HUD on top.
        """
        if facecam_box is None:
            return self.build_fill_screen_filter(
                output_width=output_width, output_height=output_height,
                fps=fps)
        fx, fy, fw, fh = facecam_box

        # Gameplay vertical fill: crop a vertical slice of the source
        # with the bottom section's aspect (1080/1152) and scale it to
        # fill the section edge to edge — no blur bands, no horizontal
        # strip. The slice uses the full source height; its width is
        # source_height * (1080/1152). It is centred, then shifted
        # away from the cam box so the PiP is cropped OUT.
        # The exclusion box is the detected box EXPANDED by 40%: the
        # face box is the face + margin, but the real PiP window is
        # wider (room around the face) — Speed's PiP spans x=0-390
        # while his face box is x=62-345, a 1.38x ratio.
        ex = int(max(0, fx - fw * 0.20))
        ey = int(max(0, fy - fh * 0.20))
        ew = int(min(source_width - ex, fw * 1.40))
        eh = int(min(source_height - ey, fh * 1.40))
        game_h = source_height
        game_w = int(source_height * output_width / 1152)
        game_w = min(game_w, source_width)
        game_h = int(game_w * 1152 / output_width)
        # Centre the slice, then shift it clear of the PiP.
        gx_crop = int((source_width - game_w) // 2)
        gy_crop = int((source_height - game_h) // 2)
        # Does the slice overlap the PiP horizontally AND vertically?
        overlaps_x = gx_crop < ex + ew and gx_crop + game_w > ex
        overlaps_y = gy_crop < ey + eh and gy_crop + game_h > ey
        if overlaps_x and overlaps_y:
            cx = source_width / 2.0
            if ex + ew <= cx:
                # PiP is on the left — push the slice right of it.
                gx_crop = min(ex + ew, source_width - game_w)
            elif ex >= cx:
                # PiP is on the right — push the slice left of it.
                gx_crop = max(0, ex - game_w)
            else:
                # PiP straddles the centre — fall back to a centred
                # 62.5% zoom (the PiP will be cropped by the fill).
                game_w = int(source_width * 0.625)
                game_h = int(game_w * 1152 / output_width)
                game_h = min(game_h, source_height)
                game_w = int(game_h * output_width / 1152)
                gx_crop = int((source_width - game_w) // 2)
                gy_crop = int((source_height - game_h) // 2)
        gx_crop = max(0, min(gx_crop, source_width - game_w))
        gy_crop = max(0, min(gy_crop, source_height - game_h))

        filter_str = (
            f"[0:v]split=2[cam_in][game_in];"
            f"[cam_in]crop={fw}:{fh}:{fx}:{fy},split=2[cam_bg_in][cam_fg_in];"
            f"[cam_bg_in]scale={output_width}:768:force_original_aspect_ratio=increase,crop={output_width}:768,boxblur=8:1,setsar=1[cam_bg];"
            f"[cam_fg_in]scale={output_width}:-1,setsar=1[cam_fg];"
            f"[cam_bg][cam_fg]overlay=(W-w)/2:(H-h)/2[cam_out];"
            f"[game_in]crop={game_w}:{game_h}:{gx_crop}:{gy_crop},scale={output_width}:1152,setsar=1[game_out];"
            f"[cam_out][game_out]vstack=inputs=2,fps={fps}[v]"
        )
        return filter_str

    def build_blur_backdrop_filter(
        self,
        output_width: int = 1080,
        output_height: int = 1920,
        fps: int = 30
    ) -> str:
        """
        Creates an FFmpeg filtergraph for blurred background + centered gameplay:
        - Background: Stretched & fast-blurred video filling 1080x1920.
        - Foreground: Crisp video centered.
        Supports customizable framerate (30fps or 60fps).
        """
        filter_str = (
            f"[0:v]split=2[bg_in][fg_in];"
            f"[bg_in]scale=270:480,boxblur=4:1,scale={output_width}:{output_height},setsar=1[bg];"
            f"[fg_in]scale={output_width}:-1,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps}[v]"
        )
        return filter_str

    def convert_to_vertical(
        self,
        input_video: str,
        output_video: str,
        layout: str = "fill",
        facecam_box: Optional[Tuple[int, int, int, int]] = None,
        ass_subtitle_path: Optional[str] = None,
        fps: int = 30
    ) -> str:
        """
        Renders the final vertical video using the selected layout.
        If ass_subtitle_path is provided, burns subtitles in the same single FFmpeg pass.
        Configurable fps: 30 (default) or 60 (high framerate smooth gaming).
        Probes the real source size so the facecam fallback box is computed
        for the actual frame, not the 1280x720 default — a wrong fallback
        box can point at a black corner and leave the top section empty.
        """
        src_w, src_h = 1280, 720
        try:
            probe = subprocess.run(
                [self.ffprobe_path, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0",
                 input_video],
                capture_output=True, text=True, timeout=30)
            parts = probe.stdout.strip().split(",")
            if len(parts) == 2:
                src_w, src_h = int(parts[0]), int(parts[1])
        except Exception:
            pass
        if layout == "split":
            filtergraph = self.build_split_screen_filter(
                facecam_box=facecam_box, source_width=src_w,
                source_height=src_h, fps=fps)
        elif layout == "stream":
            filtergraph = self.build_streamer_46_filter(
                facecam_box=facecam_box, source_width=src_w,
                source_height=src_h, fps=fps)
        elif layout == "blur":
            filtergraph = self.build_blur_backdrop_filter(fps=fps)
        else:
            filtergraph = self.build_fill_screen_filter(fps=fps)

        if ass_subtitle_path and os.path.exists(ass_subtitle_path):
            escaped_ass = ass_subtitle_path.replace(":", "\\:").replace("'", "\\'")
            filtergraph = f"{filtergraph};[v]ass='{escaped_ass}'[v_sub]"
            map_video = "[v_sub]"
        else:
            map_video = "[v]"

        # Best-quality encode for YouTube: the upload is only as good as
        # this file. CRF 14 + medium preset + an 8 Mbps floor (YouTube's
        # own recommendation for 1080p). The old CRF 18 / fast gave only
        # ~5.9 Mbps; CRF 16 / medium gave 7.2 Mbps on the Speed seaplane
        # clip — still just under YouTube's 8 Mbps. CRF 14 keeps visibly
        # more detail through YouTube's re-encode, and the minrate floor
        # guarantees at least 8 Mbps even on smooth content (water/sky)
        # that compresses too well at a fixed CRF. High profile +
        # faststart so YouTube can start processing immediately.
        # 30fps uses 'medium' (better detail per bit; ~106s for a 30s
        # clip). 60fps uses 'fast' — 'medium' at 60fps timed out on
        # this 2-CPU server.
        preset = "fast" if fps == 60 else "medium"
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", input_video,
            "-filter_complex", filtergraph,
            "-map", map_video,
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", "14",
            "-minrate", "8M",
            "-maxrate", "16M",
            "-bufsize", "16M",
            "-profile:v", "high",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac",
            "-b:a", "192k",
            output_video
        ]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=800)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg vertical formatting failed: {result.stderr.strip()}")

        return output_video
