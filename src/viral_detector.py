import os
import sys

# Ensure src directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from typing import List, Dict, Any, Optional
from chat_analyzer import ChatAnalyzer
from audio_analyzer import AudioAnalyzer
from stream_downloader import StreamDownloader

class ViralDetector:
    """Fuses chat velocity bursts and audio energy spikes into ranked viral clips."""

    def __init__(self):
        self.chat_analyzer = ChatAnalyzer()
        self.audio_analyzer = AudioAnalyzer()
        self.stream_downloader = StreamDownloader()

    def fuse_signals(
        self,
        chat_spikes: List[Dict[str, Any]],
        audio_energy: List[Dict[str, Any]],
        clip_duration: float = 30.0,
        top_n: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Combines chat spikes and audio surge data into unified clip intervals.
        Normalizes both scores and ranks top moments.
        """
        if not chat_spikes and not audio_energy:
            return []

        scored_moments = []

        # Index audio energy by time for fast lookup
        audio_by_time = {round(a["time"]): a["z_score"] for a in audio_energy} if audio_energy else {}

        for spike in chat_spikes:
            start = spike["start_time"]
            end = spike["end_time"]
            chat_score = spike["score"]

            # Sample audio z-scores within this window
            audio_scores = [
                audio_by_time[t] for t in range(int(start), int(end) + 1)
                if t in audio_by_time
            ]
            avg_audio_surge = sum(audio_scores) / len(audio_scores) if audio_scores else 0.0

            # Fused score: 60% chat burst + 40% audio volume excitement
            fused_score = (chat_score * 0.6) + (avg_audio_surge * 10.0 * 0.4)

            scored_moments.append({
                "start_time": start,
                "end_time": start + clip_duration,
                "virality_score": round(fused_score, 2),
                "chat_score": chat_score,
                "audio_surge": round(avg_audio_surge, 2),
                "sample_chat": spike.get("sample_messages", [])
            })

        # Sort by fused virality score descending
        scored_moments.sort(key=lambda m: m["virality_score"], reverse=True)

        # De-duplicate overlapping windows
        final_clips = []
        for moment in scored_moments:
            overlaps = False
            for existing in final_clips:
                if abs(moment["start_time"] - existing["start_time"]) < (clip_duration * 0.75):
                    overlaps = True
                    break
            if not overlaps:
                final_clips.append(moment)
                if len(final_clips) >= top_n:
                    break

        return final_clips
