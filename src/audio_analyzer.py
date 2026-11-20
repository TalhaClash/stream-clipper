import shutil
import subprocess
import numpy as np
from typing import List, Dict, Any, Optional

class AudioAnalyzer:
    """Uses FFmpeg and NumPy to calculate audio loudness peaks and excitement levels."""

    def __init__(self, ffmpeg_path: Optional[str] = None):
        # ffmpeg from PATH (the container installs it via apt; the old
        # hardcoded macOS path does not exist on the server).
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"

    def analyze_audio_energy(
        self,
        audio_file_path: str,
        sample_rate: int = 16000,
        window_seconds: float = 2.0
    ) -> List[Dict[str, Any]]:
        """
        Decodes audio to raw PCM stream via FFmpeg,
        computes second-by-second RMS energy (loudness),
        and detects sudden screams, laughing, or audio surges.
        """
        cmd = [
            self.ffmpeg_path,
            "-i", audio_file_path,
            "-f", "s16le",
            "-ac", "1",
            "-ar", str(sample_rate),
            "pipe:1"
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

        raw_data, _ = process.communicate()
        if not raw_data:
            return []

        # Convert to numpy array
        samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)
        chunk_len = int(sample_rate * window_seconds)

        if len(samples) == 0 or chunk_len == 0:
            return []

        num_chunks = len(samples) // chunk_len
        if num_chunks == 0:
            return []

        # Vectorised RMS over all windows at once (no Python loop): reshape
        # to (windows, samples-per-window) and reduce across axis 1. On a
        # 10-hour stream this is ~18k windows — the loop version was the
        # slow part of the analysis.
        arr = samples[: num_chunks * chunk_len].reshape(num_chunks, chunk_len)
        all_rms = np.sqrt(np.mean(arr ** 2, axis=1)).tolist()

        avg_rms = float(np.mean(all_rms))
        std_rms = float(np.std(all_rms)) if np.std(all_rms) > 0 else 1.0

        energy_timeline = []
        for i, rms in enumerate(all_rms):
            timestamp = i * window_seconds
            # Z-score represents how much louder this moment is compared to average stream volume
            z_score = max(0.0, (rms - avg_rms) / std_rms)

            energy_timeline.append({
                "time": timestamp,
                "rms": round(rms, 2),
                "z_score": round(z_score, 2)
            })

        return energy_timeline
