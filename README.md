# [Anna's Archive Calibre Store](https://github.com/ScottBot10/calibre_annas_archive)

A [Calibre](https://calibre-ebook.com/) store plugin for [Anna's Archive](https://en.wikipedia.org/wiki/Anna's_Archive).
> 📚 The largest truly open library in human history.
> ⭐️ We mirror Sci-Hub and LibGen. We scrape and open-source Z-Lib, DuXiu, and more.

> **This is a fork** of [ScottBot10/calibre_annas_archive](https://github.com/ScottBot10/calibre_annas_archive)
> that makes the in‑calibre download button work for Anna's Archive's own *Slow Partner Servers*,
> auto‑updates the (frequently‑changing) domains from Wikipedia, and adds an in‑app way to clear the
> DDoS‑Guard / captcha check. See **What's new in this fork** below.

## What's new in this fork

- **The green download button works for Slow Partner Servers.** `get_details` resolves the
  `/slow_download/<md5>/0/<n>` page to the real partner‑CDN file URL (the "📚 Download now" link, which
  points at a host like `momot.rs` that is *not* itself challenge‑gated), and puts it in
  `result.downloads` — so calibre's green‑arrow button downloads it directly. Servers are tried
  **#5 first** (no waitlist), then **#6–#8**, then the waitlisted **#1–#4**.
- **Auto‑passing the DDoS‑Guard check (the hard part).** Those pages sit behind a DDoS‑Guard
  JavaScript challenge that calibre's JS‑free downloader (mechanize) can't clear, *and* `get_details`
  runs on a worker thread where QtWebEngine can't. So `clearance.py` runs a **minimised** in‑process
  Chromium window on the GUI thread that clears the challenge — usually with **no interaction** — and
  captures the clearance cookies. Those cookies are re‑injected into mechanize, after which the slow
  pages resolve directly (**verified**: `urllib` + the captured cookies → HTTP 200 + the real CDN
  link). The clearance is reused for all results until it expires; if a captcha actually appears, the
  minimised window is brought to the front so you can solve it once.
- **Manual fallback dialog.** `slow_browser.py` opens Slow Server #5 in an in‑process browser
  (double‑click a result) that auto‑detects the "Download now" link, downloads it, and adds it to your
  library — used if you turn auto‑clearance off or QtWebEngine resolution fails.
- **Self‑updating domains.** Anna's Archive rotates domains constantly due to takedowns (`*.org`,
  `*.li`, `*.se` are all dead as of 2026). The plugin now pulls the current official domains from the
  [Anna's Archive Wikipedia infobox](https://en.wikipedia.org/wiki/Anna's_Archive) at runtime (the
  source Anna's Archive itself points users to), falling back to a hardcoded `.gl/.pk/.gd` list.
- **Hardened search/details:** per‑mirror error handling and a browser User‑Agent so the current
  Cloudflare/DDoS‑Guard‑fronted mirrors return real pages, plus the corrected url‑extension filter.

### Limitations (please read)

- Verified against the live mirrors (`.gl/.pk/.gd`): `/`, `/search`, `/md5/<md5>` return HTTP 200 and
  parse fine. `/slow_download/...` returns **HTTP 403 + DDoS‑Guard** to JS‑free clients (true even on
  a residential IP — confirmed via the connection in use). After a real browser clears the challenge,
  the captured cookies make `urllib`/mechanize return **HTTP 200 + the real CDN link** — this is the
  mechanism the green button relies on.
- The captured download URL is **time‑limited** (the partner‑CDN link carries an expiry token), so
  calibre should download it promptly after it's resolved — which it does on the green‑arrow click.
- Cookie reuse caveats: DDoS‑Guard keys clearance on IP + User‑Agent (the clearance browser sets the
  same UA as mechanize to match) and the cookie expires (minutes–hours); when it does, the next
  attempt transparently re‑clears via the minimised browser.
- Sometimes DDoS‑Guard shows an interactive captcha instead of auto‑passing; then the minimised
  window is surfaced for a one‑time solve.
- **Verify in your own calibre.** The threading bridge (worker → GUI), the minimised QtWebEngine
  clearance window, cookie reuse, and the green‑arrow download of the resolved URL are written against
  calibre's `qt.webengine` API with Qt5/Qt6 guards but should be confirmed live; the standalone
  network logic (search, detail parse, cookie‑authenticated slow resolution) **was** verified.

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
