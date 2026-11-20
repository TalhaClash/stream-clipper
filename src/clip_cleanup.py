"""Auto-delete generated clips older than 24 hours (boss order 18 Nov 2026).

Clips live in src/static/clips/ (bind-mounted, so they survive restarts) and
nothing else ever deletes them — without this they pile up forever. A daemon
thread runs inside the app process: it prunes once at startup, then sweeps
every hour. Only *.mp4 files older than CLIP_MAX_AGE_SECONDS are removed,
and the in-memory job/clip records that pointed at them are dropped too, so
the dashboard never shows a clip whose file is gone. Temp files (/tmp raw
cuts, audio, subtitles) are already deleted by the pipeline itself.
"""
import logging
import os
import threading
import time

logger = logging.getLogger("streamclipper.clip_cleanup")

# Clips are deleted 24h after they were created (file mtime).
CLIP_MAX_AGE_SECONDS = 24 * 3600
CLEANUP_INTERVAL_SECONDS = 3600


def _clip_filename(clip):
    url = clip.get("video_url") or clip.get("download_url") or ""
    return os.path.basename(url)


def prune_old_clips(clips_dir, clips_db=None, jobs_db=None,
                    max_age_seconds=CLIP_MAX_AGE_SECONDS, now=None):
    """Delete *.mp4 files in clips_dir older than max_age_seconds.

    Also drops in-memory clip entries whose file is gone (deleted here, or
    already missing for any other reason) and the completed job records left
    with no clips. Processing jobs are never dropped. Returns
    (deleted_files, freed_bytes). Non-.mp4 files are never touched.
    """
    now = now if now is not None else time.time()
    cutoff = now - max_age_seconds
    deleted, freed = set(), 0
    if os.path.isdir(clips_dir):
        for name in os.listdir(clips_dir):
            if not name.lower().endswith(".mp4"):
                continue
            path = os.path.join(clips_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) > cutoff:
                    continue
                size = os.path.getsize(path)
                os.remove(path)
            except OSError as e:
                logger.warning("clip cleanup: could not delete %s: %s", path, e)
                continue
            deleted.add(name)
            freed += size
    if clips_db is not None:
        for job_id in list(clips_db):
            clips = clips_db[job_id]
            # In-place: the "latest" alias shares the job's list object,
            # so it sees the same filtered list automatically.
            clips[:] = [c for c in clips
                        if _clip_filename(c) not in deleted
                        and os.path.isfile(os.path.join(clips_dir, _clip_filename(c)))]
            if not clips:
                clips_db.pop(job_id, None)
                if jobs_db is not None and job_id != "latest":
                    job = jobs_db.get(job_id)
                    if job and job.get("status") == "completed":
                        jobs_db.pop(job_id, None)
    if deleted:
        logger.info("clip cleanup: deleted %d clip(s), freed %.1f MB",
                    len(deleted), freed / (1024 * 1024))
    return len(deleted), freed


def _cleanup_loop(clips_dir, clips_db, jobs_db, interval_seconds, max_age_seconds):
    while True:
        try:
            prune_old_clips(clips_dir, clips_db, jobs_db, max_age_seconds)
        except Exception:
            logger.exception("clip cleanup sweep failed")
        time.sleep(interval_seconds)


def start_cleanup_thread(clips_dir, clips_db=None, jobs_db=None,
                         interval_seconds=CLEANUP_INTERVAL_SECONDS,
                         max_age_seconds=CLIP_MAX_AGE_SECONDS):
    """Prune once now, then every interval, on a daemon thread."""
    prune_old_clips(clips_dir, clips_db, jobs_db, max_age_seconds)
    thread = threading.Thread(
        target=_cleanup_loop,
        args=(clips_dir, clips_db, jobs_db, interval_seconds, max_age_seconds),
        daemon=True, name="clip-cleanup")
    thread.start()
    logger.info("clip cleanup: started (max age %dh, sweep every %dh)",
                max_age_seconds // 3600, interval_seconds // 3600)
    return thread
