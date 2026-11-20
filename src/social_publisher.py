import os
import json
from typing import Dict, Any, List, Optional

class SocialPublisher:
    """
    Manages 1-click publishing to YouTube Shorts and TikTok using official free API tiers.
    - YouTube Data API v3 (10,000 free quota units / day)
    - TikTok Content Posting API (Free developer tier)
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.path.join(os.path.dirname(__file__), "social_config.json")
        self.accounts = self._load_accounts()

    def _load_accounts(self) -> Dict[str, Any]:
        """Loads connected OAuth tokens and credentials stored locally."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "youtube": {"connected": False, "channel_name": None, "access_token": None},
            "tiktok": {"connected": False, "username": None, "access_token": None},
            "instagram": {"connected": False, "username": None, "access_token": None}
        }

    def save_accounts(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.accounts, f, indent=2)
        try:
            os.chmod(self.config_path, 0o600)
        except Exception:
            pass

    def connect_account(self, platform: str, username: str, access_token: Optional[str] = None) -> Dict[str, Any]:
        """Saves connected account details."""
        platform = platform.lower()
        if platform not in self.accounts:
            self.accounts[platform] = {}
        
        token = access_token or f"live_token_{platform}_{abs(hash(username)) % 1000000}"
        self.accounts[platform] = {
            "connected": True,
            "username": username,
            "channel_name": username,
            "access_token": token
        }
        self.save_accounts()
        return self.accounts[platform]

    def disconnect_account(self, platform: str) -> Dict[str, Any]:
        """Disconnects an account."""
        platform = platform.lower()
        self.accounts[platform] = {
            "connected": False,
            "username": None,
            "access_token": None
        }
        self.save_accounts()
        return {"status": "disconnected", "platform": platform}

    def format_youtube_payload(
        self,
        title: str,
        description: str,
        tags: List[str],
        privacy_status: str = "public"
    ) -> Dict[str, Any]:
        """
        Formats YouTube Data API v3 snippet and status.
        Ensures '#Shorts' is in title/description for automatic vertical indexing.
        """
        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title} #Shorts"

        full_tags = list(set(tags + ["Shorts", "YouTubeShorts", "Viral"]))

        return {
            "snippet": {
                "title": title[:100],  # YouTube title limit
                "description": description,
                "tags": full_tags,
                "categoryId": "20"  # Gaming Category ID
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False
            }
        }

    def format_tiktok_payload(
        self,
        title: str,
        tags: List[str],
        privacy_level: str = "PUBLIC_TO_EVERYONE"
    ) -> Dict[str, Any]:
        """Formats TikTok Content Posting API request payload."""
        tag_str = " ".join([t if t.startswith("#") else f"#{t}" for t in tags])
        full_text = f"{title} {tag_str}"[:150]

        return {
            "post_info": {
                "title": full_text,
                "privacy_level": privacy_level,
                "disable_duet": False,
                "disable_stitch": False,
                "disable_comment": False
            },
            "source_info": {
                "source": "FILE_UPLOAD"
            }
        }

    def publish_clip(
        self,
        video_path: str,
        platforms: List[str],
        title: str,
        description: str,
        tags: List[str],
        privacy: str = "public",
        youtube_oauth: Any = None,
        username: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Dispatches publishing to the selected platforms — for real.
        YouTube uploads through the REQUESTING user's own linked channel
        (YouTube Data API v3). TikTok and Instagram have no API
        credentials wired up yet, so they return an honest error instead
        of a fake success: nothing is ever reported as published unless
        it actually was.
        """
        results = {}

        for platform in platforms:
            platform = platform.lower()
            acc = self.accounts.get(platform, {})
            clean_name = os.path.basename(video_path).split('.')[0]

            if platform == "youtube":
                creds = youtube_oauth.get_credentials(username) if youtube_oauth else None
                if not creds:
                    results["youtube"] = {
                        "status": "error",
                        "platform": "YouTube Shorts",
                        "message": "YouTube channel not connected. Connect your own channel from the dashboard (Connected Accounts → YouTube → Connect with Google)."
                    }
                    continue
                try:
                    real = youtube_oauth.upload_short(
                        video_path=video_path,
                        title=title,
                        description=description,
                        tags=tags,
                        privacy=privacy,
                        username=username
                    )
                    channel = (youtube_oauth.get_channel(username) or {}).get("title", "My YouTube Channel")
                    results["youtube"] = {
                        "status": "success",
                        "platform": "YouTube Shorts (Live)",
                        "channel": channel,
                        "title": title,
                        "video_id": real["video_id"],
                        "view_url": real["view_url"],
                        "privacy": privacy,
                        "message": f"Successfully uploaded to your real YouTube channel ({channel})!"
                    }
                except Exception as e:
                    results["youtube"] = {
                        "status": "error",
                        "platform": "YouTube Shorts",
                        "message": f"YouTube upload failed: {e}"
                    }

            elif platform == "tiktok":
                results["tiktok"] = {
                    "status": "error",
                    "platform": "TikTok",
                    "message": "TikTok posting isn't set up yet — it needs a TikTok developer app with the Content Posting API. Your handle is saved; for now post the downloaded clip from the TikTok app."
                }

            elif platform == "instagram":
                results["instagram"] = {
                    "status": "error",
                    "platform": "Instagram Reels",
                    "message": "Instagram posting isn't set up yet — it needs a Meta app with the Instagram Graph API (Business/Creator account linked to a Facebook Page). Your handle is saved; for now post the downloaded clip from the Instagram app."
                }

        return {"status": "completed", "dispatched": results}
