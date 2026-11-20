# StreamClipper

Turn a Twitch or YouTube stream into viral 9:16 Shorts. Paste a link, the engine finds the loudest moments, cuts them into vertical clips with kinetic subtitles, detects the streamer's facecam and puts it on top — then posts the clip to YouTube Shorts in one click.

## What it does

- **Viral moment detection** — audio energy (RMS) surge detection fused with chat velocity and hype keywords, split into zones so moments come from across the whole stream, not just the start.
- **9:16 vertical engine** — 1080×1920, 30/60 fps, best-quality encode (CRF 14, 8 Mbps floor — above YouTube's 8 Mbps recommendation for 1080p).
- **Facecam detection (YuNet)** — OpenCV's tiny CNN face detector with landmark validation finds the streamer's cam wherever it sits (top, middle, bottom) and frames it on top; no cam found means full gameplay, never a fake box.
- **Kinetic subtitles** — real transcription with faster-whisper (no fake lines), word-by-word yellow highlight, TikTok-style.
- **Real titles, descriptions & hashtags** — built from the clip's own transcribed words, not the stream title.
- **1-click publishing to YouTube Shorts** — per-user OAuth channel connect, with a 10 Shorts/day limit for editors (admin has no limit).
- **Access control** — anyone can sign in with Google, but new accounts stay **pending** until the admin approves them. Admin panel manages users, passwords and YouTube cookies.
- **YouTube cookies, with a live timer** — downloads from a server IP need a signed-in browser's cookies; the dashboard shows when they were uploaded, when they expire (~2h), and flags an upload that is missing the login cookie.

## Quick start

```bash
git clone https://github.com/TalhaClash/stream-clipper.git
cd stream-clipper
docker compose up -d --build
```

Open `http://localhost:8000`. On first run the app creates `src/users_db.json`
with an **admin** account and a random password, printed once in the
container log:

```bash
docker logs stream-clipper | grep -i "admin password"
```

Sign in with that password, then change it from the Admin panel.

### Data files

These are bind-mounted from `src/` and created on first run — they hold
secrets and are git-ignored, never commit them:

| File | What it holds |
| --- | --- |
| `users_db.json` | Accounts, password hashes, roles |
| `sessions.json` | Login sessions |
| `google_credentials.json` | Google OAuth client ID/secret |
| `youtube_tokens.json` | Per-user YouTube OAuth tokens |
| `youtube_cookies.txt` | YouTube login cookies for downloads |
| `social_config.json` | Connected social accounts |
| `upload_counts.json` | Daily upload counts |

The files must exist on the host before the container recreates, or Docker
turns them into directories. Create empty ones if needed:

```bash
touch src/users_db.json src/social_config.json src/google_credentials.json \
  src/youtube_cookies.txt src/youtube_cookies_uploaded_at.txt \
  src/sessions.json src/youtube_tokens.json src/upload_counts.json
```

### Google sign-in & YouTube publishing

1. In [Google Cloud Console](https://console.cloud.google.com/), create an
   OAuth client (Web application) and enable the YouTube Data API v3.
2. Add the redirect URIs from the Admin panel's setup help
   (`https://YOUR-DOMAIN/api/oauth/google/callback` for login,
   `https://YOUR-DOMAIN/api/auth/google/callback` for the login button).
3. In the Admin panel, paste the client ID and secret. Users can then sign
   in with Google, and each user can connect their own YouTube channel.

### YouTube cookies (needed for downloads)

YouTube blocks downloads from datacenter IPs. The tool needs a signed-in
browser's cookies, uploaded on the dashboard:

1. Install the free **Get cookies.txt LOCALLY** extension, allow it in Incognito.
2. Open an Incognito window, sign in to YouTube, go to `youtube.com/robots.txt`.
3. Export the cookies, upload the file on the dashboard.

Google rotates these roughly every 2 hours, so expect to re-upload
regularly. The dashboard timer shows the live status.

### HTTPS

`Caddyfile` is a template — replace `YOUR-DOMAIN` with your domain and
point its DNS at the server. Caddy handles the certificate automatically.

## How clips are made

1. `yt-dlp` fetches the stream's info and audio (Deno solves YouTube's JS challenges).
2. Audio energy is scanned for peaks, split into zones — one peak per zone.
3. Each peak is cut with `yt-dlp --download-sections`, facecam-detected once
   per job with YuNet, and rendered in parallel (2 at a time) with FFmpeg:
   fitted facecam on top, vertical gameplay slice below, kinetic subtitles
   burned in the same encode.
4. Clips are saved to `src/static/clips/` and auto-deleted after 24 hours.

## Limits

- 2 concurrent clip jobs, 5 clips max per job, 30–60 s each.
- Editors: 10 YouTube Shorts per day. Admin: no limit.
- YouTube Data API: 100 video uploads per day per Google Cloud project.

## Stack

FastAPI + FFmpeg + yt-dlp + faster-whisper + OpenCV (YuNet) + Caddy, all in
Docker. No paid services — the whole thing runs on a single small server.

## Contributors

- [Talha Mujawar](https://github.com/TalhaClash) — creator
- [ninadparange](https://github.com/ninadparange) — contributor

## License

MIT — see [LICENSE](LICENSE).
