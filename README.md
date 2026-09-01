<p align="center">
  <img src="docs/banner.png" alt="s.d.p downloader" width="100%">
</p>

<p align="center">
  A self-hosted Telegram bot that turns links into videos right inside your chat.<br>
  TikTok · YouTube · Instagram · Facebook · 1500+ sites
</p>

<p align="center">
  <b>English</b> · <a href="README.uk.md">Українська</a>
</p>

<p align="center">
  <a href="../../actions/workflows/ci.yml"><img src="../../actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python 3.12">
  <img src="https://img.shields.io/badge/aiogram-3.30-blue" alt="aiogram">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
  <a href="../../pkgs/container/sdp-downloader"><img src="https://img.shields.io/badge/ghcr.io-image-blue?logo=docker&logoColor=white" alt="GHCR"></a>
</p>

---

Drop a link into a chat — the bot replies with the video. No ads, no quotas,
no third-party servers: everything runs on your own machine, and nobody sees
what you download.

<p align="center">
  <img src="docs/01-group.png" alt="The bot answering a link in a group" width="70%">
</p>

## Why not just another downloader bot

**It doesn't send broken files.** Three engines with distinct roles (`yt-dlp`,
Cobalt, tikwm) and a fallback chain: if one returns a truncated, silent file
or an image instead of a video, the next one is tried. Before sending, every
file is verified — was the download cut short, is there a video stream, is it
longer than half a second, does it decode, does it have audio. Whatever fails
never reaches the user.

**You run it from your phone.** The Mini App is not a showcase: live progress
bars with percentages and ETA, statistics, allow lists, cookies and 25 settings
that change **on the fly**, without restarting the container.

**It takes care of itself.** It watches for new versions of `yt-dlp`, aiogram,
Cobalt and its own repository, reminds you to refresh cookies days before the
session expires, and restarts itself if it ever hangs.

## Two editions

| | **Lite** | **Full** |
|---|---|---|
| Containers | 2 | 6 |
| Setup required | `BOT_TOKEN` — that's it | token, tunnel, api_id/api_hash |
| Where it works | groups only | groups + private + panel |
| What it downloads | short videos | + full YouTube, MP3, playlists, trimming, 1500+ sites |
| File limit | 50 MB | **2 GB** |
| Mini App | no | yes |
| Writes to disk | **nothing** | SQLite: stats, settings, access lists |
| Managed via | environment variables | buttons in the panel |

**Lite** is "drop it in a group and forget about it". **Full** is for when you
want the panel, big files and music. Same code, same image — the only difference
is which compose file you start. Moving from Lite to Full is free: Lite stores
nothing, so there is nothing to lose.

## Quick start

First create the bot in [@BotFather](https://t.me/BotFather): `/newbot`, copy the
token, then **make sure** to run `/setprivacy` → pick the bot → **Disable**,
otherwise it won't see messages in groups.

### Lite

Two files, no cloning, no building — the image is prebuilt:

```bash
curl -O https://raw.githubusercontent.com/TameyAnderson/sdp-downloader/main/docker-compose.lite.yml
curl -o .env https://raw.githubusercontent.com/TameyAnderson/sdp-downloader/main/.env.lite.example
# paste your BOT_TOKEN into .env
docker compose -f docker-compose.lite.yml up -d
```

Add the bot to a group — done, it already works.

### Full

```bash
curl -O https://raw.githubusercontent.com/TameyAnderson/sdp-downloader/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/TameyAnderson/sdp-downloader/main/.env.example
# paste your BOT_TOKEN into .env
docker compose up -d
```

The Mini App and the 2 GB limit are enabled through profiles — see
[below](#large-files-up-to-2-gb).

### Portainer

**Stacks → Add stack → Repository**, compose path `docker-compose.yml`
(or `docker-compose.lite.yml`), add `BOT_TOKEN` under **Environment variables**,
then **Deploy the stack**.

## What it can do

### Downloading

- **19 services** in the registry: YouTube, TikTok, Instagram, Facebook, Twitch,
  SoundCloud, Bandcamp, Twitter/X, Reddit, Pinterest, Tumblr, Snapchat, Bluesky,
  Vimeo, Dailymotion, Bilibili and more. Each one toggles separately.
- **All-in** — a single switch that lets the bot take any of the 1500+ sites
  supported by `yt-dlp`.
- **Playlists** (video or mp3) and **trimming by timestamps** (`0:10-0:30`
  next to the link).
- **Music**: YouTube Music, SoundCloud, Bandcamp — sent as tagged audio files.
  Spotify and Deezer read the public track title and look for a match on
  YouTube (no DRM circumvention).
- **Soundtrack as a separate MP3** for short videos — for when the music under
  the clip is the point. The track is cut from the file that was just
  downloaded, so it costs no second request.
- **Photo posts and photo carousels**: a post with pictures instead of a video
  is sent as an album rather than reported as a failure.
- Quality ladder 4K → 2K → 1080 → 720, plus manual quality and bitrate selection.

### The panel (Mini App)

<table>
<tr>
<td width="33%"><img src="docs/07-preview.png" alt="Auto preview before downloading"></td>
<td width="33%"><img src="docs/03-stats.png" alt="Statistics"></td>
<td width="33%"><img src="docs/04-settings.png" alt="Settings"></td>
</tr>
<tr>
<td align="center"><sub>Auto preview: title, duration and size before you download</sub></td>
<td align="center"><sub>Statistics by source and by chat</sub></td>
<td align="center"><sub>Live settings — no restart</sub></td>
</tr>
</table>

<table>
<tr>
<td width="33%"><img src="docs/05-history.png" alt="History with one-tap repeat"></td>
<td width="33%"><img src="docs/06-cookies.png" alt="Cookies status card"></td>
<td width="33%"><img src="docs/08-notify.png" alt="Update notification"></td>
</tr>
<tr>
<td align="center"><sub>History with size, thumbnail and one-tap repeat</sub></td>
<td align="center"><sub>Cookies: days left, at a glance</sub></td>
<td align="center"><sub>It tells you when a new version is out</sub></td>
</tr>
</table>

<p align="center">
  <img src="docs/02-progress.png" alt="Live progress with speed and ETA" width="60%"><br>
  <sub>Live progress: percentage, speed, time left</sub>
</p>

- Progress bars with percentage, speed and time left.
- History with file size, real thumbnail and **one-tap re-download**.
- Auto preview: paste a link and immediately see the title, duration and size.
- Every setting live: 19 numeric knobs and 6 switches.
- Database backup with a single button — the file arrives in your chat.
- Ukrainian and English, switchable on the fly.

### Reliability

- Telegram rate limit (429) handled with waiting and retries, plus per-chat throttle.
- Abuse limits: links per message, parallel downloads per user and in total.
- `/health` + Docker healthcheck + `autoheal` — a stuck bot restarts itself.
- `yt-dlp` channel `stable | nightly | master` — broken extractors get fixed
  before the official release.
- **Failures are named**: private post, age restriction, geo block, deleted
  post or a changed site — not one message for everything.
- **A deadline per job**, so a link that fails slowly stops holding a slot, and
  a **pause on an engine** that has failed several times in a row on the same
  platform.
- The **same link posted twice at once** is downloaded once.
- **Startup self-check**: ffmpeg, Cobalt, the PO-token provider, the real file
  limit — and whether group privacy is on, which is the usual reason a bot
  looks dead in a group.
- **Graceful stop**: on `docker stop` running downloads are given time to
  finish; leftovers from a previous run are cleared at startup.
- **Restore from a backup**: send the `.db` back to the bot in private. The
  file is checked before it replaces anything, and the old one is kept.

## Configuration

Almost everything is adjustable in the panel and stored in the database. The
variables in `.env` are **starting values** for a fresh deployment. The full,
commented list lives in [`.env.example`](.env.example) (Full) and
[`.env.lite.example`](.env.lite.example) (Lite).

Only one variable is mandatory: `BOT_TOKEN`. Everything else has sane defaults.

### Who gets access

One chat allow list and a switch:

- **off** (default) — the bot works in any chat;
- **on** — only in listed chats, and in private it answers the admin only.

There are no access tiers: whoever is allowed gets everything. The panel is
always admin-only.

> In Lite the bot works in groups only, and the list is set through
> `ALLOWED_CHATS`. If your bot is public, fill it in — otherwise anyone can add
> it to their own group and download videos using your server.

### Instagram cookies (optional)

Instagram refuses to serve some posts without a login. With cookies `yt-dlp`
can fetch them and automatically becomes the primary engine for Instagram
and Facebook.

In Full you just **send the `cookies.txt` file to the bot in private** — it
validates the file, stores it with `600` permissions and deletes your message.
The panel shows the state: days left, which keys, when it was updated. The bot
reminds you in advance. In Lite the file is only mounted from the host.

> **A cookies file is a key to the account**: it logs in without a password or
> 2FA. Use a **separate** account, never your main one, and never commit that
> file — `.gitignore` already covers every common name.

### Large files (up to 2 GB)

A regular Telegram bot cannot send files larger than 50 MB. A local Bot API
server raises that to 2 GB. **No domain and no open ports are required** —
the server runs next to the bot inside Docker and is never exposed outside.

1. Get `api_id` / `api_hash` at [my.telegram.org](https://my.telegram.org)
   (a free personal Telegram account, not the bot).
2. Enable the service: `COMPOSE_PROFILES=bigfiles`.
3. Do the one-time `logOut` for the bot, then set
   `TELEGRAM_API_URL=http://telegram-bot-api:8081` and `MAX_FILE_SIZE_MB=2000`.

The `logOut` call moves your bot from Telegram's cloud servers to your own.
It is a one-way switch: to go back you would have to `logOut` from the local
server first. Files are downloaded to a volume, so budget disk space for them.

### Mini App

The panel is a Telegram Web App, and Telegram only opens Web Apps over
**HTTPS with a valid certificate**. A bare IP address will not work, so you
need an address — which is where a domain comes in. Two ways:

**With your own domain (recommended).** Any cheap domain will do; it has to be
added to Cloudflare (the free plan is enough). Create a named tunnel in
Cloudflare Zero Trust, put its token into `CLOUDFLARE_TUNNEL_TOKEN`, and point
a Public Hostname such as `bot.yourdomain.com` at `http://video-bot:8080`.
The address never changes, so the Menu Button in BotFather is set once.

**Without a domain (for trying it out).** Cloudflare quick tunnels hand you a
random `*.trycloudflare.com` address for free — replace the `command:` line of
the `cloudflared` service as shown in the compose file. The catch: that address
changes on every restart, and you have to update the Menu Button each time.

Then set `WEBAPP_ENABLED=1`, enable the `miniapp` profile and add the Menu
Button in BotFather.

The tunnel needs no open ports on your router: `cloudflared` connects outward
to Cloudflare itself.

The tunnel is **inbound**: it exposes the panel to you and has no effect on
which address the bot uses for outgoing requests.

## Updating

The bot watches for new versions itself and messages the admin — with a summary
of the changes and a link to the diff. To update:

```bash
docker compose pull && docker compose up -d
```

In Portainer — **Pull and redeploy** with Re-pull image enabled.

The image is published for `linux/amd64` and `linux/arm64`, so a Raspberry Pi
or an ARM VPS works the same.

| Tag | What it is |
|---|---|
| `:latest` | the newest release — appears when a `v*` tag is pushed |
| `:edge` | whatever is on `main` right now, rebuilt on every push |
| `:v1.0` | a fixed version that never moves under you |

Set `SDP_IMAGE` to switch without editing the compose file — useful in
Portainer, where a stack deployed from Git cannot be edited inline:

```
SDP_IMAGE=ghcr.io/tameyanderson/sdp-downloader:edge
```

By default it watches upstream. Your own fork — `GITHUB_REPO=you/fork`;
no notifications at all — `GITHUB_REPO=off`. A token is only needed for
a private repository.

## How it works inside

The engine chains, verification steps and containers are described in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Development

```bash
cd tests && python -m unittest discover -s . -p "test_*.py" -v
```

Contribution guidelines are in [CONTRIBUTING.md](CONTRIBUTING.md).

51 tests on the standard library, no dependencies: link detection, live
settings, LITE mode, bilingual completeness, deployment configs and secret
scanning. CI runs the same on every push — details in
[tests/README.md](tests/README.md).

## Security

The bot runs a downloader against links handed to it by other people, so a few
things are locked down by default. None of them need configuring.

- **Not root.** The container starts as root only long enough to hand `/data`
  over to an unprivileged user, then drops to it. An existing volume is taken
  care of automatically, so an upgrade needs nothing from you.
- **Links into your own network are refused.** Otherwise a link dropped in a
  group could aim the downloader at your router's panel, a neighbouring
  container, or the cloud metadata service that hands out credentials to
  anything asking from inside. Your own Cobalt and Bot API stay reachable. Set
  `ALLOW_PRIVATE_HOSTS=1` if you deliberately download from your own network.
- **Secrets can live in files.** Anything in the environment is readable by
  everyone who can run `docker inspect`, and Portainer shows it in plain text.
  Point `BOT_TOKEN_FILE` or `GITHUB_TOKEN_FILE` at a file and the file wins.
- **The panel API is rate limited** per caller, so a signed `initData` cannot be
  replayed in a loop for the length of its lifetime.
- **Cookies are treated as credentials.** The file is stored with tight
  permissions and the message you sent it in is deleted from the chat. Use a
  throwaway account: a cookies file is a login without the password or 2FA.

## Limitations

- Telegram does not let bots send files > 50 MB (or > 2 GB with a local Bot API).
- Instagram sometimes refuses anonymous requests from any address — cookies fix that.
- TikTok photo carousels rely on the public tikwm API; if it disappears, that
  particular feature goes with it.
- A proxy does not solve Instagram problems: the refusals come from the missing
  login, not from the address.

## License

[MIT](LICENSE). Fork it, extend it, use it in your own projects — the only
condition is keeping the copyright notice and the license text.

## Credits

This project stands on the shoulders of [yt-dlp](https://github.com/yt-dlp/yt-dlp),
[cobalt](https://github.com/imputnet/cobalt), [aiogram](https://github.com/aiogram/aiogram)
and [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider).
