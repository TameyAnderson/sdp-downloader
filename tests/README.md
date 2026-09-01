# Tests

<p><b>English</b> · <a href="README.uk.md">Українська</a></p>

Plain standard library, no pytest, no extra dependencies (except `pyyaml`).

```bash
cd tests
python -m unittest discover -s . -p "test_*.py" -v
```

CI runs exactly this on every push.

## What is covered

| File | About |
|---|---|
| `test_links.py` | link and platform detection, stripping `?igsh=`, All-in not shadowing other services |
| `test_settings.py` | live knobs stay within bounds, quality ladders are never empty, access model and migration from the old database |
| `test_lite.py` | LITE writes nothing to disk, works in groups only, `ALLOWED_CHATS`, and most importantly does not break the full mode |
| `test_i18n.py` | same keys in `uk`/`en`, no Ukrainian strings bypassing the dictionary, every `data-i18n` translated |
| `test_sending.py` | external post titles never break a message: markdown parsing is off, no stray markers left in any string |
| `test_cookies.py` | short-lived TikTok cookies are stripped on upload (a stale one turns every request into 403), login keys and other sites survive, the file stays parseable |
| `test_entrypoint.py` | the yt-dlp self-upgrade actually works: documented install method per channel, no dead placeholder wheel links, failures are loud |
| `test_release.py` | the publish workflow builds for amd64+arm64, smoke-tests the image before releasing, needs no manual secrets; both stacks pull the published image |
| `test_deploy.py` | compose parses without duplicate keys, every variable is read by the code, `.dockerignore` keeps the needed files, no secrets in the repository |
| `test_security.py` | links pointing into the private network are refused, secrets can live in files, the panel API is rate limited |
| `test_cache.py` | quality is part of the cache key, and a cache is dropped when the Telegram API it came from changes |
| `test_concurrency.py` | limits, per-chat pacing, background tasks that must not be garbage collected, disk space |
| `test_engines.py` | failures are named, a job has a deadline, a failing engine is paused, one download per link, cookies for Cobalt |
| `test_db.py` | numbered migrations, WAL, a shared connection, and restoring from a backup without losing the live database |
| `test_ops.py` | Cobalt response shapes, the startup self-check, orphan cleanup, graceful stop, job ids in the log |

## Writing new ones

`helper.load_bot(**env)` imports `bot.py` into a temporary directory with a
clean environment — every call gives an independent module with its own database:

```python
from helper import load_bot

bot = load_bot(LITE=1, MAX_HEIGHT=1080)
bot.db_init()
bot.settings_load_sync()
```

Nothing is written into the project directory, and tests depend neither on each
other nor on the host environment.

## Verified by breaking things

Every check here was verified by deliberately breaking the thing it guards,
to make sure it actually fails: `strip` removed from Instagram, All-in moved out of last place
in the registry, bounds clamping removed from `tunable`, LITE writing to the
database, a missing English translation, a Ukrainian string literal in a reply,
a label without `data-i18n`, a duplicate key in compose, an unused variable,
a required file excluded by `.dockerignore`.
