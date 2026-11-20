import os
import json
import time
from typing import Dict, Any, Optional

# oauthlib's Flow.fetch_token() hard-fails with "Scope has changed" when
# Google returns MORE scopes than the flow asked for. That happens on the
# plain sign-in flow: it asks only for identity scopes, but Google also
# returns the YouTube scopes the same account granted earlier (channel
# connect), so every sign-in after a channel connect failed. Relaxing the
# check is safe here — we only ever USE the scopes we asked for, and the
# token endpoint still validates the code, client and PKCE verifier.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

try:
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False

CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "google_credentials.json")
# Per-user YouTube channel tokens: every approved user links their OWN
# channel and posts to it. {username: {"channel": {...}, "tokens": {...}}}
# This file holds live OAuth tokens — it is chmod 600, excluded from the
# project zip, and never rsynced from the local tree to the server.
TOKENS_FILE = os.path.join(os.path.dirname(__file__), "youtube_tokens.json")
# Full scopes: only the CHANNEL-CONNECT flow asks for these (uploading to
# the tool's YouTube channel needs youtube.upload).
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly"
]
# Minimal scopes for plain sign-in: identity only. Asking every user who
# signs in for YouTube-upload permission was over-scoped and scary.
LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile"
]

class YouTubeOAuthManager:
    """Manages real Google OAuth 2.0 flow & direct YouTube Data API v3 video uploads."""

    def __init__(self, creds_path: str = CREDENTIALS_FILE):
        self.creds_path = creds_path
        self.config = self._load_config()
        self.user_tokens = self._load_user_tokens()
        self._migrate_legacy_tokens()
        # Flows awaiting their callback, keyed by OAuth state.
        # The Flow created for the authorization URL holds the PKCE
        # code_verifier; the token exchange MUST reuse that same Flow
        # (or at least its verifier), otherwise Google rejects the code
        # with "invalid_grant: Missing code verifier".
        self._pending_flows: Dict[str, tuple] = {}

    def _load_user_tokens(self) -> Dict[str, Any]:
        if os.path.exists(TOKENS_FILE):
            try:
                with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                pass
        return {}

    def _save_user_tokens(self):
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.user_tokens, f, indent=2)
        try:
            os.chmod(TOKENS_FILE, 0o600)
        except Exception:
            pass

    def _migrate_legacy_tokens(self):
        """One-time move of the old SHARED channel tokens (the admin's
        channel) into the per-user store under 'admin', so the admin keeps
        posting to the same channel without reconnecting."""
        if "admin" in self.user_tokens:
            return
        tokens = self.config.get("tokens")
        channel = self.config.get("connected_channel")
        if tokens and channel:
            self.user_tokens["admin"] = {"channel": channel, "tokens": tokens}
            self._save_user_tokens()
            self.config.pop("tokens", None)
            self.config.pop("connected_channel", None)
            self.save_config()

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.creds_path):
            try:
                with open(self.creds_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "client_id": None,
            "client_secret": None,
            "configured": False,
            "connected_channel": None,
            "tokens": None
        }

    def save_config(self):
        with open(self.creds_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)
        try:
            os.chmod(self.creds_path, 0o600)
        except Exception:
            pass

    def set_client_credentials(self, client_id: str, client_secret: str) -> Dict[str, Any]:
        self.config["client_id"] = client_id.strip()
        self.config["client_secret"] = client_secret.strip()
        self.config["configured"] = bool(client_id and client_secret)
        self.save_config()
        return {
            "configured": self.config["configured"],
            "client_id": self.config["client_id"][:15] + "..." if self.config["client_id"] else None
        }

    def _build_flow(self, redirect_uri: str, scopes=None):
        """A fresh Flow for the given redirect URI."""
        client_config = {
            "web": {
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri]
            }
        }
        flow = Flow.from_client_config(client_config, scopes=scopes or SCOPES)
        flow.redirect_uri = redirect_uri
        return flow

    def _take_flow(self, state: Optional[str], redirect_uri: str, scopes=None):
        """Reuse the Flow created for this state (it holds the PKCE
        code_verifier). Falls back to a fresh Flow if the state is
        unknown (e.g. server restarted mid-flow). Returns (flow, the
        username that started the flow, or None)."""
        if state:
            entry = self._pending_flows.pop(state, None)
            if entry is not None:
                return entry[0], (entry[2] if len(entry) > 2 else None)
        return self._build_flow(redirect_uri, scopes=scopes), None

    def get_auth_url(self, redirect_uri: str, scopes=None, username: Optional[str] = None) -> Optional[str]:
        if not self.config.get("client_id") or not self.config.get("client_secret"):
            return None

        if not GOOGLE_LIBS_AVAILABLE:
            # Fallback direct Google OAuth URL
            client_id = self.config["client_id"]
            scope_str = "%20".join(scopes or SCOPES)
            return (
                f"https://accounts.google.com/o/oauth2/v2/auth?"
                f"client_id={client_id}&redirect_uri={redirect_uri}&"
                f"response_type=code&scope={scope_str}&access_type=offline&prompt=consent"
            )

        # Drop stale pending flows (older than 10 minutes).
        now = time.time()
        for s in [s for s, (_, ts, *_) in self._pending_flows.items() if now - ts > 600]:
            del self._pending_flows[s]

        flow = self._build_flow(redirect_uri, scopes=scopes)
        # NOTE: do NOT pass include_granted_scopes="true" here. It makes
        # Google return every scope the account granted before (e.g. the
        # YouTube scopes from a channel connect) on top of the scopes this
        # flow asked for — and oauthlib then rejects the token with
        # "Scope has changed". Each flow asks for exactly the scopes it
        # needs; that is what Google should return.
        auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
        # Keep the Flow: its code_verifier is needed at the callback.
        # Also remember WHO started the flow, so the callback can only
        # attach the channel to that same user.
        self._pending_flows[state] = (flow, now, username)
        return auth_url

    def get_google_userinfo(self, code: str, redirect_uri: str, state: Optional[str] = None) -> Dict[str, Any]:
        """Exchanges OAuth code for Google user profile (verified email and name)."""
        import requests
        if not GOOGLE_LIBS_AVAILABLE:
            raise RuntimeError("Google API libraries not installed.")
        flow, _flow_user = self._take_flow(state, redirect_uri, scopes=LOGIN_SCOPES)
        flow.fetch_token(code=code)
        creds = flow.credentials

        resp = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=800
        )
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"Failed to fetch Google profile: {resp.text}")

    def exchange_code(self, code: str, redirect_uri: str, state: Optional[str] = None, username: Optional[str] = None) -> Dict[str, Any]:
        if not GOOGLE_LIBS_AVAILABLE:
            raise RuntimeError("Google API libraries not installed.")

        flow, flow_user = self._take_flow(state, redirect_uri)
        # The channel belongs to whoever started this flow. If the flow
        # is known, its owner must match the user completing it — a
        # callback URL must not let one user link a channel to another
        # user's account.
        owner = flow_user or username
        if not owner:
            raise RuntimeError("Could not tell which user this channel link belongs to. Start the connect step again.")
        if username and flow_user and flow_user != username:
            raise RuntimeError("This channel link was started by a different user. Start the connect step again.")
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Fetch channel details
        youtube = build("youtube", "v3", credentials=creds)
        ch_resp = youtube.channels().list(mine=True, part="snippet,statistics").execute()

        channel_title = "My YouTube Channel"
        channel_id = None
        if ch_resp.get("items"):
            item = ch_resp["items"][0]
            channel_title = item.get("snippet", {}).get("title", channel_title)
            channel_id = item.get("id")

        tokens_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes
        }

        # Tokens live per user now — each user posts to their OWN channel.
        self.user_tokens[owner] = {
            "channel": {
                "title": channel_title,
                "id": channel_id,
                "connected_at": time.strftime("%Y-%m-%d %H:%M:%S")
            },
            "tokens": tokens_data
        }
        self._save_user_tokens()

        return {
            "status": "connected",
            "channel_title": channel_title,
            "channel_id": channel_id
        }

    def get_credentials(self, username: Optional[str] = None) -> Optional[Any]:
        """That user's YouTube credentials, or None when they have not
        linked a channel. No argument means the admin (the old shared
        callers all ran as the admin)."""
        if not GOOGLE_LIBS_AVAILABLE:
            return None
        entry = self.user_tokens.get(username or "admin")
        if not entry or not entry.get("tokens"):
            return None
        t = entry["tokens"]
        return Credentials(
            token=t.get("token"),
            refresh_token=t.get("refresh_token"),
            token_uri=t.get("token_uri"),
            client_id=t.get("client_id"),
            client_secret=t.get("client_secret"),
            scopes=t.get("scopes")
        )

    def get_channel(self, username: Optional[str] = None) -> Optional[Dict[str, Any]]:
        entry = self.user_tokens.get(username or "admin")
        return entry.get("channel") if entry else None

    def disconnect(self, username: str) -> Dict[str, Any]:
        self.user_tokens.pop(username, None)
        self._save_user_tokens()
        return {"status": "disconnected", "username": username}

    def list_connected_users(self) -> list:
        out = []
        for username, entry in self.user_tokens.items():
            ch = entry.get("channel") or {}
            out.append({
                "username": username,
                "channel_title": ch.get("title"),
                "channel_id": ch.get("id"),
                "connected_at": ch.get("connected_at")
            })
        return out

    def upload_short(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list,
        privacy: str = "public",
        username: Optional[str] = None
    ) -> Dict[str, Any]:
        """Uploads video directly to YouTube Data API v3 — to THAT user's
        own linked channel."""
        creds = self.get_credentials(username)
        if not creds:
            raise RuntimeError("No YouTube channel connected for this user. Connect your own channel from the dashboard (Connected Accounts → YouTube → Connect with Google).")

        youtube = build("youtube", "v3", credentials=creds)

        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title} #Shorts"

        body = {
            "snippet": {
                "title": title[:100],
                "description": description,
                "tags": list(set(tags + ["Shorts", "YouTubeShorts", "Gaming"])),
                "categoryId": "20"
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False
            }
        }

        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )
        response = request.execute()
        video_id = response.get("id")

        return {
            "status": "success",
            "video_id": video_id,
            "view_url": f"https://youtube.com/shorts/{video_id}"
        }
