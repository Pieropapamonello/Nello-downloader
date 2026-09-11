# Cookie administration

The Telegram bot's private `/cookies` menu manages Instagram, Facebook, YouTube
and TikTok cookies on this service. `/setcookies instagram` selects an upload
directly. The bot authenticates the effective administrator and accepts files only
in that administrator's private chat.

Configure `COOKIE_RENDER_API_KEY` on this downloader with an API key for its Render
account. Render supplies `RENDER_SERVICE_ID`. The bot needs only the existing
`DOWNLOADER_URL` and `DOWNLOADER_TOKEN` settings. The key must remain valid for
future uploads; revoking it leaves downloads and monitoring functional but prevents
cookie updates from being saved.

Authenticated endpoints:

- `GET /admin/cookies`: state and a cookie revision hash, never cookie values.
- `PUT /admin/cookies/{platform}` with `{"content": "Netscape file contents"}`:
  validates platform domains, session cookies and size (512 KB), persists the
  corresponding Render secret, then atomically installs a live override.

The next extraction uses the new cookies without a deploy. After a restart the
Render secret takes over. In-flight jobs keep their current cookie copy; a result
using an older cookie revision cannot mark the new revision as rejected.

The bot polls every five minutes without downloading media. It distinguishes
cookie timestamps from access failures detected during failed downloads, including
WhatsApp requests. Expiry dates cannot detect sessions revoked early by a site;
those require a subsequent extraction to expose an authentication error. Generic
format, JSON, memory and timeout errors alone do not trigger cookie alerts. Each
unchanged alert is limited to once per day, with bot-side persistent deduplication.

No cookies or Render API keys should be committed to Git. Existing cookie files
in Render remain in use until the administrator explicitly replaces them.
