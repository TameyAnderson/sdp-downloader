# s.d.p downloader — how it works

<p><b>English</b> · <a href="ARCHITECTURE.uk.md">Українська</a></p>

The full path a link takes: from the message to the finished file.

---

## 1. Entry points

| From | What it accepts |
|---|---|
| **Private chat** | links, the `mp3` keyword, timestamps `0:10-0:30`, a cookies file, commands |
| **Group chat** | links only (it never joins the conversation) |
| **Mini App** | links + quality/bitrate choice, settings management |

In **LITE** only the middle row remains: the bot works in groups exclusively
and answers a short hint in private.

---

## 2. Access layer (before any work starts)

1. **Chat allow list** — a single switch:
   - **off** (default): the bot works in any chat, everything is available;
   - **on**: only chats from the list, and in private it answers the admin only.
2. **No access tiers** — whoever is allowed gets everything: YouTube, playlists,
   MP3, All-in. Only `admin` stands apart — the Mini App and service commands.
3. **Limits**: links per message, parallel downloads per user and in total.
4. **LITE**: access is decided by `ALLOWED_CHATS` — empty means any group.

---

## 3. Parsing the link

```
message text
   ↓ regex built ONLY from the enabled services
found URLs
   ↓ platform detection (registry of 19 services; All-in is last)
   ↓ playlist expansion (up to the limit)
   ↓ tracking parameters stripped (Instagram ?igsh=…)
   ↓ trim timestamps + quality/bitrate from the Mini App
job queue
```

---

## 4. Engines and fallback chains

Three engines with distinct roles — none duplicates another:

| Engine | Unique role |
|---|---|
| **tikwm** | TikTok photos and carousels |
| **yt-dlp** | video, YouTube, quality ladder, tagged MP3 |
| **Cobalt** | Instagram/Facebook **without cookies**, safety net when an extractor breaks |

**Chains per platform:**

```
TikTok /photo/  :  tikwm → Cobalt → yt-dlp
TikTok video    :  yt-dlp → Cobalt
Instagram / FB  :  [cookies?] yt-dlp → Cobalt
                   [none]     Cobalt → yt-dlp
YouTube         :  yt-dlp (+PO token, Deno) → Cobalt
Spotify/Deezer  :  public oEmbed (title only) → YouTube search → audio
All-in (rest)   :  yt-dlp
```

**Two passes:**

1. **Strict** — every engine must return a complete video **with audio**;
   silent files or "an image instead of a video" are rejected.
2. **Lenient** — runs only if something was rejected for quality; here a silent
   file is accepted as a last resort. If every engine failed outright, this pass
   is skipped instead of wasting time.

---

## 5. Verification before sending

A file never reaches the user until it passes all of this:

```
downloaded file
   ├─ truncated download?      (actual size vs Content-Length)
   ├─ any video stream?        (ffprobe)
   ├─ duration > 0.5 s?        (cuts off "zero-length" broken files)
   ├─ decodes cleanly?         (ffmpeg, for Cobalt files)
   ├─ enough frames?           (when duration is unknown)
   └─ has audio?               (silent → next engine)
        ↓
   failed → next engine in the chain
```

---

## 6. Sending

- **Metadata is mandatory**: width, height, duration, thumbnail,
  `supports_streaming` — without them Telegram shows the video as broken.
- **Flood control**: on 429 it waits `retry_after` and retries; there is also
  a pause between sends to the same chat.
- **Size**: up to **2 GB** through a local Bot API, or 50 MB through the cloud one.
- `file_id` cache — a repeated link is sent instantly (can be turned off).

---

## 7. State and background work

**Stored:**

- SQLite: events (statistics), settings, allow lists;
- cookies — on a volume, permissions 600;
- the `file_id` cache.

**Every N hours** (interval editable in the panel, 2 by default) it checks:

- new versions of yt-dlp / aiogram / Cobalt;
- a new commit in the watched repository (upstream or your fork) — compared
  against the **running** commit, so it never asks for a pointless redeploy;
- cookie expiry — warns N days in advance.

In **LITE** nothing from the "Stored" list exists: no database, no cache, no
writable cookies. Temporary files live in `/tmp` in RAM.

---

## 8. Containers

| Service | Role |
|---|---|
| **video-bot** | the bot itself + Mini App API |
| **cobalt-api** | the Cobalt engine (local, private) |
| **bgutil-provider** | PO tokens to get past YouTube protection |
| **telegram-bot-api** | local Bot API → 2 GB limit *(profile `bigfiles`)* |
| **cloudflared** | HTTPS tunnel for the Mini App *(profile `miniapp`)* |
| **autoheal** | restarts the bot if it stops answering `/health` |

In **LITE** there are two: `video-bot` and `cobalt-api`. The PO token provider
is enabled with the `youtube` profile if YouTube starts asking for confirmation.

---

## 9. The whole flow at a glance

```
User ──► Telegram ──► s.d.p bot
                         │
                 access + limits
                         │
                    link parsing
                         │
      ┌──────────────────┼──────────────────┐
   tikwm             yt-dlp             Cobalt
      └──────────────────┼──────────────────┘
                 integrity checks
                         │
             metadata + flood control
                         │
User ◄── video / photo / MP3 ◄── Telegram
```
