# Contributing

<p><b>English</b> · <a href="CONTRIBUTING.uk.md">Українська</a></p>

Any help is welcome, from a fixed typo to a new engine.

## Getting started

```bash
git clone https://github.com/TameyAnderson/sdp-downloader.git
cd sdp-downloader
cp .env.example .env          # paste your BOT_TOKEN
docker compose up -d --build
```

Tests need nothing but `pyyaml`:

```bash
cd tests && python -m unittest discover -s . -p "test_*.py" -v
```

Run them **before** opening a PR. CI does the same, but finding out yourself
is faster.

## Rules that save everyone time

- **One PR — one change.** Large mixed diffs are hard to review.
- **New feature — new test.** At least one that fails without your change.
- **Bilingual is mandatory.** Every new string goes into both `uk` and `en`
  (`T` in `bot.py` and the `I` dictionaries in `index.html`). A test checks this.
- **No secrets.** Cookies, tokens, chat ids — not even in examples. A test
  scans the whole repository for them.
- **A new environment variable** must appear in `.env.example` and in the right
  compose file, otherwise the deployment test fails.
- **Comments in config files are bilingual**: an English line, a Ukrainian line
  under it. Code comments in `bot.py` are English only.

## Worth discussing before you build it

Open an issue first if you plan to touch:

- new engines or external services;
- the access model;
- anything that starts writing new data to disk;
- restructuring files.

Not because it's forbidden, but so you don't spend an evening on something
that doesn't fit the design.

## What we will not agree on

- DRM circumvention (Spotify, Deezer, any protected service);
- emulating account activity or evading anti-bot protection;
- anything aimed at breaking the law rather than saving a video for yourself.

Such PRs are closed without discussion.

## License

The project is [MIT](LICENSE). By opening a PR you agree that your contribution
is licensed on the same terms. Nothing else to sign.
