import os
import json
import hashlib
import hmac
import secrets
import time
from typing import Dict, Any, List, Optional

USERS_FILE = os.path.join(os.path.dirname(__file__), "users_db.json")
SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")

# PBKDF2-HMAC-SHA256, 600k iterations (OWASP recommendation). Stored as
# "pbkdf2$<iterations>$<salt_hex>$<hash_hex>". Older accounts carry a plain
# SHA-256(salt+password) hex hash in `password_hash` with the salt in `salt`
# — those still verify, and are upgraded to PBKDF2 on their next sign-in.
_PBKDF2_ITERATIONS = 600_000

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"

def _verify_password(password: str, user: Dict[str, Any]) -> bool:
    stored = user.get("password_hash", "")
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"),
                bytes.fromhex(salt_hex), int(iters)
            )
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    # Legacy fast hash: SHA-256(salt + password). Still accepted so existing
    # accounts keep working; a successful legacy login upgrades the hash.
    salt = user.get("salt", "")
    legacy = hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored)

def _is_legacy_hash(user: Dict[str, Any]) -> bool:
    return not str(user.get("password_hash", "")).startswith("pbkdf2$")

def _chmod_private(path: str):
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

# Login lockout: 5 failed attempts for one account from one IP within
# 15 minutes locks that account+IP out for 15 minutes.
_LOCKOUT_MAX_ATTEMPTS = 5
_LOCKOUT_WINDOW_SECONDS = 900

class AuthManager:
    """Manages user authentication, admin roles, and granular tool access control."""

    def __init__(self, db_path: str = USERS_FILE):
        self.db_path = db_path
        self.users = self._load_users()
        self.tokens: Dict[str, Dict[str, Any]] = self._load_sessions()
        # Failed password sign-ins, keyed "username|ip" -> [timestamps].
        self._failed_logins: Dict[str, List[float]] = {}

    def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        """Sessions survive a container restart, so a deploy does not log
        everyone out. Expired sessions are pruned on load."""
        if os.path.exists(SESSIONS_FILE):
            try:
                with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                    sessions = json.load(f)
                now = time.time()
                live = {t: s for t, s in sessions.items() if s.get("expires_at", 0) > now}
                if len(live) != len(sessions):
                    self._save_sessions_dict(live)
                return live
            except Exception:
                pass
        return {}

    def _save_sessions_dict(self, sessions: Dict[str, Dict[str, Any]]):
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2)
        _chmod_private(SESSIONS_FILE)

    def _save_sessions(self):
        self._save_sessions_dict(self.tokens)

    def invalidate_token(self, token: Optional[str]):
        """Logs one session out for good (logout, or a stolen token)."""
        if token and token in self.tokens:
            del self.tokens[token]
            self._save_sessions()

    def invalidate_user_tokens(self, username: str):
        self.tokens = {k: v for k, v in self.tokens.items() if v["username"] != username}
        self._save_sessions()

    def _load_users(self) -> Dict[str, Any]:
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        
        # No user DB yet (first run, or the file was lost): create the admin
        # with a RANDOM password and print it once to the server log.
        # There is deliberately no known default password any more.
        initial_password = secrets.token_urlsafe(18)
        default_admin = {
            "admin": {
                "username": "admin",
                "email": "admin@streamclipper.local",
                "name": "Stream Master (Admin)",
                "salt": "",
                "password_hash": _hash_password(initial_password),
                "role": "admin",
                "status": "active",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
        }
        self._save_users_dict(default_admin)
        print(f"[Auth] First-run admin password: {initial_password} — change it in Admin → Change Password.")
        return default_admin

    def _save_users_dict(self, users: Dict[str, Any]):
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2)
        _chmod_private(self.db_path)

    def save_users(self):
        self._save_users_dict(self.users)

    def find_user(self, identifier: str) -> Optional[Dict[str, Any]]:
        ident = identifier.strip().lower()
        for u in self.users.values():
            if u.get("username", "").lower() == ident:
                return u
            if u.get("email", "").lower() == ident:
                return u
        return None

    def _lockout_key(self, username: str, client_ip: Optional[str]) -> str:
        return f"{(username or '').strip().lower()}|{client_ip or 'unknown'}"

    def _is_locked_out(self, key: str) -> bool:
        now = time.time()
        attempts = [t for t in self._failed_logins.get(key, []) if now - t < _LOCKOUT_WINDOW_SECONDS]
        self._failed_logins[key] = attempts
        return len(attempts) >= _LOCKOUT_MAX_ATTEMPTS

    def _record_failed_login(self, key: str):
        self._failed_logins.setdefault(key, []).append(time.time())

    def authenticate(self, username: str, password: str, client_ip: Optional[str] = None) -> Optional[Dict[str, Any]]:
        lock_key = self._lockout_key(username, client_ip)
        if self._is_locked_out(lock_key):
            return {"error": "Too many failed sign-in attempts. Try again in 15 minutes."}
        user = self.find_user(username)
        if not user:
            self._record_failed_login(lock_key)
            return None
        if user.get("status") == "pending":
            return {"error": "Your account is waiting for admin approval."}
        if user.get("status") != "active":
            return {"error": "Account is disabled. Contact your administrator."}

        if not _verify_password(password, user):
            self._record_failed_login(lock_key)
            return None
        self._failed_logins.pop(lock_key, None)

        # A legacy SHA-256 hash that just verified gets upgraded to PBKDF2
        # now, so the weak hash disappears from disk on first use.
        if _is_legacy_hash(user):
            user["password_hash"] = _hash_password(password)
            user["salt"] = ""
            self.save_users()

        # Generate secure session token
        token = f"sc_tok_{secrets.token_urlsafe(32)}"
        session_data = {
            "username": user["username"],
            "email": user.get("email", user["username"]),
            "name": user["name"],
            "role": user["role"],
            "created_at": time.time(),
            "expires_at": time.time() + (86400 * 7) # 7 days
        }
        self.tokens[token] = session_data
        self._save_sessions()
        return {
            "token": token,
            "user": {
                "username": user["username"],
                "email": user.get("email", user["username"]),
                "name": user["name"],
                "role": user["role"]
            }
        }

    def authenticate_email(self, email: str, name: Optional[str] = None) -> Dict[str, Any]:
        """
        Sign-in by a GOOGLE-VERIFIED Gmail (the Google OAuth callback only).
        The email is trusted here because Google proved ownership of it.
        There is deliberately no unverified email sign-in any more: anyone
        could type any address into a form, so that path was removed.
        Open sign-in, admin-gated use: ANY Gmail can sign in, but a first-time
        Gmail becomes a PENDING user — it gets a session (so it can see the
        "waiting for approval" page) but cannot use the tool until the admin
        approves it. Approved users sign straight in.
        """
        email_clean = email.strip().lower()
        user = self.find_user(email_clean)

        if not user:
            # First sign-in with this Gmail: create a pending account.
            local = email_clean.split("@")[0]
            username = "".join(c if c.isalnum() else "_" for c in local).lower() or "user"
            base = username
            n = 2
            while username in self.users:
                username = f"{base}_{n}"
                n += 1
            user = {
                "username": username,
                "email": email_clean,
                "name": (name or local).strip(),
                "salt": "",
                "password_hash": _hash_password(secrets.token_hex(12)),
                "role": "pending",
                "status": "pending",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            self.users[username] = user
            self.save_users()

        if user.get("status") == "disabled":
            return {
                "error": f"Access Denied: Your account '{email_clean}' has been revoked by the administrator."
            }

        # Issue a session token. Pending users get role "pending" — the
        # middleware lets them load the dashboard (to see the waiting
        # notice) but blocks every tool API until approved.
        token = f"sc_tok_{secrets.token_urlsafe(32)}"
        session_data = {
            "username": user["username"],
            "email": user.get("email", email_clean),
            "name": user["name"],
            "role": user["role"],
            "created_at": time.time(),
            "expires_at": time.time() + (86400 * 7)
        }
        self.tokens[token] = session_data
        self._save_sessions()
        return {
            "status": "pending" if user.get("status") == "pending" else "success",
            "token": token,
            "user": {
                "username": user["username"],
                "email": user.get("email", email_clean),
                "name": user["name"],
                "role": user["role"]
            }
        }

    def approve_user(self, username: str) -> Dict[str, Any]:
        """Admin approves a pending sign-in: grants tool access (editor role)."""
        user = self.users.get(username)
        if not user:
            raise ValueError("User not found.")
        if user.get("status") != "pending":
            raise ValueError("Only pending accounts can be approved.")
        user["status"] = "active"
        if user.get("role") == "pending":
            user["role"] = "editor"
        self.save_users()
        return {
            "username": user["username"],
            "email": user.get("email", user["username"]),
            "name": user["name"],
            "role": user["role"],
            "status": user["status"]
        }

    def verify_token(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        sess = self.tokens.get(token)
        if not sess:
            return None
        if time.time() > sess["expires_at"]:
            del self.tokens[token]
            self._save_sessions()
            return None

        # Check if user status is still valid in DB. Pending users keep
        # their session (they need it to see the waiting notice); revoked
        # users lose it. Role is re-read from the DB so an admin approval
        # takes effect immediately, without the user signing in again.
        u = self.users.get(sess["username"])
        if not u or u.get("status") == "disabled":
            del self.tokens[token]
            self._save_sessions()
            return None
        if u.get("status") not in ("active", "pending"):
            del self.tokens[token]
            self._save_sessions()
            return None
        sess["role"] = u.get("role", sess.get("role"))
        sess["status"] = u.get("status")

        return sess

    def list_users(self) -> List[Dict[str, Any]]:
        return [
            {
                "username": u["username"],
                "email": u.get("email", u["username"]),
                "name": u["name"],
                "role": u["role"],
                "status": u["status"],
                "created_at": u.get("created_at", "N/A")
            }
            for u in self.users.values()
        ]

    def add_user(
        self,
        username: str,
        password: Optional[str] = None,
        name: str = "",
        role: str = "editor",
        email: Optional[str] = None
    ) -> Dict[str, Any]:
        username = username.strip().lower()
        email_clean = email.strip().lower() if email else username

        # Check existing
        for u in self.users.values():
            if u.get("username", "").lower() == username:
                raise ValueError(f"User with username '{username}' already exists.")
            if email_clean and u.get("email", "").lower() == email_clean and u.get("username", "").lower() != username:
                raise ValueError(f"Gmail '{email_clean}' is already registered for another user.")

        pwd = password if password else secrets.token_hex(6)
        new_user = {
            "username": username,
            "email": email_clean,
            "name": name.strip() or username,
            "salt": "",
            "password_hash": _hash_password(pwd),
            "role": role if role in ["admin", "editor"] else "editor",
            "status": "active",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        self.users[username] = new_user
        self.save_users()
        return {
            "username": new_user["username"],
            "email": new_user["email"],
            "name": new_user["name"],
            "role": new_user["role"],
            "status": new_user["status"]
        }

    def update_user_status(self, username: str, status: str) -> Dict[str, Any]:
        if username not in self.users:
            raise ValueError("User not found.")
        if username == "admin" and status != "active":
            raise ValueError("Cannot deactivate master admin account.")
        
        self.users[username]["status"] = status
        self.save_users()
        # Invalidate active sessions if revoked
        if status != "active":
            self.invalidate_user_tokens(username)
        return {"username": username, "status": status}

    def delete_user(self, username: str):
        if username not in self.users:
            raise ValueError("User not found.")
        if username == "admin":
            raise ValueError("Cannot delete master admin account.")

        del self.users[username]
        self.save_users()
        self.invalidate_user_tokens(username)

    def change_password(self, username: str, current_password: str, new_password: str) -> Dict[str, Any]:
        user = self.users.get(username)
        if not user:
            raise ValueError("User not found.")
        if not _verify_password(current_password, user):
            raise ValueError("Current password is incorrect.")
        if not new_password or len(new_password) < 6:
            raise ValueError("New password must be at least 6 characters.")
        if _verify_password(new_password, user):
            raise ValueError("New password must be different from the current password.")
        user["salt"] = ""
        user["password_hash"] = _hash_password(new_password)
        self.save_users()
        # Invalidate all sessions for this user, so sessions created with
        # the old password stop working immediately.
        self.invalidate_user_tokens(username)
        return {"username": username, "status": "password_changed"}
