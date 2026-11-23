---
trigger: model_decision
description: "Recipes: add a platform, fix yt-dlp breakage, new config option"
---

## Common tasks

- **Add a dedicated platform:** add its domains to `PLATFORM_DOMAINS`, a
  display name to `PLATFORM_NAMES`, a `get_<name>()` in `ytdlp.py` (exported
  in `__init__.py`), and an entry in `DOWNLOADERS`. Keep exact host matching.
  Decide whether its probe failures should still offer a download (like
  TikTok).
- **yt-dlp breaks for a site:** first `pip install -U yt-dlp` and bump the
  minimum version in `requirements.txt`. Only write custom extraction if
  updating does not help.
- **Tune ad detection:** edit `AD_HOST_SUFFIXES`/`AD_PATH_PATTERN` in
  `common.py`, or the `SHORT_AD_*` constants in `streaminfo.py`. Re-run the
  IMA sample page test.
- **Tune browser timing:** change the constants at the top of `browser.py`.
  Keep `BROWSER_PROBE_TIMEOUT` in `bot.py` comfortably above
  `MAX_CAPTURE_SECONDS` + analysis budget + slot wait.
- **New config option:** read it in `load_settings()` with a safe default,
  add it to `Settings`, log it (never secrets), and document it in this file.
- **Editing `start_bot.bat` / `start_bot_hidden.vbs`:** keep CRLF line
  endings, keep `cd /d "%~dp0"`, and keep the restart loop. Wait with
  `ping -n 11 127.0.0.1 >nul`, not `timeout`, because `timeout` fails in a
  hidden window.

