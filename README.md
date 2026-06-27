# [Anna's Archive Calibre Store](https://github.com/ScottBot10/calibre_annas_archive)

A [Calibre](https://calibre-ebook.com/) store plugin for [Anna's Archive](https://en.wikipedia.org/wiki/Anna's_Archive).
> 📚 The largest truly open library in human history.
> ⭐️ We mirror Sci-Hub and LibGen. We scrape and open-source Z-Lib, DuXiu, and more.

> **This is a fork** of [ScottBot10/calibre_annas_archive](https://github.com/ScottBot10/calibre_annas_archive)
> that makes the in‑calibre download button work for Anna's Archive's own *Slow Partner Servers*,
> auto‑updates the (frequently‑changing) domains from Wikipedia, and adds an in‑app way to clear the
> DDoS‑Guard / captcha check. See **What's new in this fork** below.

## What's new in this fork

- **Direct downloads from Anna's Archive Slow Partner Servers.** `get_details` now resolves the
  `/slow_download/<md5>/0/<n>` pages to a real file URL, so calibre's green‑arrow download button
  works without clicking through to the website. Servers are tried **#5 first** (no waitlist), then
  **#6–#8**, then the waitlisted **#1–#4** as a fallback — configurable via *Download link options*.
- **Captcha handled inside calibre.** The slow‑download pages sit behind a DDoS‑Guard JavaScript
  challenge that calibre's JS‑free downloader can't pass. When that happens, opening a result loads
  **Slow Partner Server #5** in an *in‑process* Chromium dialog (`slow_browser.py`, QtWebEngine),
  where you solve the check once and the "Download now" file is added straight to your library — no
  full‑site navigation. That dialog also **captures the DDoS‑Guard clearance cookie**
  (`cookieStore().cookieAdded` → the plugin's `remember_cookies()`), which is re‑injected into every
  subsequent request, so the silent green‑arrow path keeps working until the cookie expires (~20 min).
  (calibre's own `WebStoreDialog` runs out‑of‑process, so a bundled in‑process dialog is required to
  reach the cookie store; it falls back to `WebStoreDialog` if QtWebEngine is unavailable.)
- **Self‑updating domains.** Anna's Archive rotates domains constantly due to takedowns (`*.org`,
  `*.li`, `*.se` are all dead as of 2026). The plugin now pulls the current official domains from the
  [Anna's Archive Wikipedia infobox](https://en.wikipedia.org/wiki/Anna's_Archive) at runtime (the
  source Anna's Archive itself points users to), falling back to a hardcoded `.gl/.pk/.gd` list.
- **Hardened search/details:** per‑mirror error handling and a browser User‑Agent so the current
  Cloudflare/DDoS‑Guard‑fronted mirrors return real pages, plus the corrected url‑extension filter.

### Limitations (please read)

- What was verified against the live mirrors (`.gl/.pk/.gd`): `/`, `/search`, and `/md5/<md5>` return
  HTTP 200 and parse correctly, so search and detail extraction work headlessly. `/slow_download/...`
  returned **HTTP 403 + a DDoS‑Guard JS challenge** from a datacenter/VPN IP.
- Resolving a slow download **headlessly only works when DDoS‑Guard isn't actively challenging your
  IP**. Residential IPs usually pass its passive check; datacenter/VPN IPs are often hard‑blocked
  (HTTP 403, as seen above). When blocked, use the embedded‑browser captcha flow above.
- There is **no JavaScript‑free way to auto‑solve** the challenge — that is by design on Anna's side.
- There is **no JavaScript‑free way to auto‑solve** the challenge — that is by design on Anna's side.
- Cookie reuse caveats: DDoS‑Guard also keys clearance on IP and User‑Agent (the in‑process dialog
  sets the same UA as the plugin to match), and possibly TLS fingerprint, so a captured cookie may
  still be rejected by mechanize in some cases. The in‑app dialog download always works regardless;
  the silent green‑arrow reuse is the best‑effort bonus.
- **Not yet exercised end‑to‑end inside a running calibre** (could not run calibre in the build
  environment): the in‑process QtWebEngine dialog, cookie capture/reuse, the in‑dialog
  add‑to‑library, and the green‑arrow download of a resolved slow URL. The code is written against
  calibre's own QtWebEngine API (`qt.webengine`) with Qt5/Qt6 guards, but please verify in your own
  calibre on your own connection.

## Installation
### From source
```shell
calibre-customize -b <path to cloned repo>
```
or build the zip and add it:
```shell
./zip_release.sh && calibre-customize -a $(ls calibre_annas_archive-v*.zip -1rt | tail -n1)
```
On Windows you can instead go to `Preferences > Plugins`, click `Load plugin from file`, and select
the built `calibre_annas_archive-vx.x.x.zip`.

## Configuration
Go to `Preferences > Plugins > Store`, then double‑click `Anna's Archive (x.x.x)` to open the settings.

### Search Options
Same search options as the site. For each checkbox group (filetype, language, …): if nothing is
checked it doesn't filter on that option; if anything is checked, only matching results are shown.

### Download link options
- **Direct download via Slow Partner Servers (#5 first):** resolve AA's own slow servers to a direct
  file URL for the download button. Disable to use only the external mirrors (Libgen/Sci‑Hub/Z‑Lib).
- **If blocked, open Slow Server #5 in calibre's browser:** when a slow download is captcha‑gated,
  open it in the embedded browser so you can solve the check and download in‑app.
- **Verify Content‑Type / Verify url extension:** validation applied to the *external mirror* links.

### Mirrors
- **Auto‑update domains from Wikipedia:** pull current official domains at runtime and try them first.
- The editable list is the fallback/ordering used when auto‑update is off or unavailable.
