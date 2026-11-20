import re
from typing import List, Dict, Any
from chat_downloader import ChatDownloader

# High-signal reaction words & emotes for streaming culture
VIRAL_KEYWORDS = {
    "lul": 1.5, "lol": 1.2, "kekw": 2.0, "omegalul": 2.0, "lmao": 1.2,
    "pog": 1.5, "poggers": 1.8, "w": 1.5, "huge w": 2.0,
    "omg": 1.5, "wtf": 1.8, "no way": 2.0, "clip": 2.5, "clip that": 3.0,
    "bro": 1.0, "dead": 1.2, "aimbot": 1.5, "clutch": 2.0,
    "gg": 1.0, "hype": 1.5, "rip": 1.2, "caught": 1.5
}

class ChatAnalyzer:
    """Extracts chat replay and analyzes velocity & sentiment spikes."""

    def __init__(self):
        self.downloader = ChatDownloader()

    def fetch_chat(self, url: str, max_messages: int = 15000) -> List[Dict[str, Any]]:
        """Extracts chat messages with time offsets (in seconds from stream start)."""
        messages = []
        try:
            chat = self.downloader.get_chat(url)
            count = 0
            for message in chat:
                # time_in_seconds gives timestamp relative to video start
                timestamp = message.get("time_in_seconds")
                text = message.get("message", "")
                if timestamp is not None and text:
                    messages.append({
                        "time": float(timestamp),
                        "text": text,
                        "author": message.get("author", {}).get("name", "")
                    })
                    count += 1
                    if count >= max_messages:
                        break
        except Exception as e:
            print(f"[ChatAnalyzer] Notice: Chat replay extraction completed/ended: {e}")
        return messages

    def analyze_spikes(
        self,
        messages: List[Dict[str, Any]],
        video_duration: float,
        window_size: int = 20,
        step_size: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Sliding-window analysis across the stream to identify viral chat bursts.
        Returns scored time windows sorted by virality score.
        """
        if not messages:
            return []

        sorted_msgs = sorted(messages, key=lambda m: m["time"])
        # Use video duration if available, otherwise cover all messages up to the end
        if video_duration and video_duration > 0:
            max_time = video_duration
        else:
            max_time = sorted_msgs[-1]["time"] + window_size
        
        windows = []
        current_start = 0.0

        while current_start + window_size <= max_time:
            current_end = current_start + window_size
            
            # Find messages in this window
            window_msgs = [
                m for m in sorted_msgs 
                if current_start <= m["time"] < current_end
            ]
            
            msg_count = len(window_msgs)
            keyword_score = 0.0
            caps_count = 0

            for m in window_msgs:
                words = re.findall(r'\b\w+\b', m["text"].lower())
                for word in words:
                    if word in VIRAL_KEYWORDS:
                        keyword_score += VIRAL_KEYWORDS[word]
                
                # Check for excitement signals (all caps / excessive punctuation)
                if m["text"].isupper() and len(m["text"]) > 3:
                    caps_count += 1
                if "!" in m["text"] or "?" in m["text"]:
                    keyword_score += 0.5

            # Virality score combines message velocity, hype keywords, and caps/excitement
            total_score = (msg_count * 1.0) + (keyword_score * 1.5) + (caps_count * 0.8)

            windows.append({
                "start_time": current_start,
                "end_time": current_end,
                "message_count": msg_count,
                "score": round(total_score, 2),
                "sample_messages": [m["text"] for m in window_msgs[:5]]
            })

            current_start += step_size

        return sorted(windows, key=lambda w: w["score"], reverse=True)
