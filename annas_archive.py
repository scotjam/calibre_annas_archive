import re
from contextlib import closing
from http.client import RemoteDisconnected
from math import ceil
from typing import Generator
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin, urlsplit
from urllib.request import urlopen, Request

from calibre import browser, prints
from calibre.gui2 import open_url
from calibre.gui2.store import StorePlugin
from calibre.gui2.store.search_result import SearchResult
from calibre.gui2.store.web_store_dialog import WebStoreDialog
from calibre_plugins.store_annas_archive.constants import (
    DEFAULT_MIRRORS, WIKIPEDIA_URL, SLOW_SERVER_ORDER, RESULTS_PER_PAGE, SearchOption,
)
from lxml import html

try:
    from qt.core import QUrl
except (ImportError, ModuleNotFoundError):
    from PyQt5.Qt import QUrl

SearchResults = Generator[SearchResult, None, None]

# Markers that tell us a response is a DDoS-Guard / Cloudflare interstitial
# rather than the real page. mechanize (calibre's browser()) runs no JS, so it
# cannot clear these -- when we see one we hand off to the embedded Chromium.
_CHALLENGE_MARKERS = ('ddos-guard', 'ddg-l10n-title', 'just a moment', 'cf-browser-verification',
                      'challenge-platform', 'cf_chl_opt')

# DDoS-Guard / Cloudflare clearance cookies worth re-using once the user has
# solved the challenge in the embedded browser.
_CLEARANCE_COOKIES = ('__ddg', 'cf_clearance', '__cf')

_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')


class _SlowChallenge(Exception):
    """Raised when a /slow_download/ page is gated behind a JS challenge."""

    def __init__(self, url):
        super().__init__(url)
        self.url = url


class AnnasArchiveStore(StorePlugin):

    def __init__(self, gui, name, config=None, base_plugin=None):
        super().__init__(gui, name, config, base_plugin)
        self.working_mirror = None
        self._wiki_mirrors = None          # cached Wikipedia lookup for this session
        # Clearance cookies captured from the embedded browser, keyed by
        # (domain, name). Re-injected into every browser() so the silent
        # green-arrow path keeps working until they expire.
        self._cookies = {}

    # ------------------------------------------------------------------ utils

    def _browser(self):
        br = browser(user_agent=_USER_AGENT)
        for (domain, name), value in list(self._cookies.items()):
            try:
                br.set_simple_cookie(name, value, domain, path='/')
            except Exception:
                pass
        return br

    @staticmethod
    def _is_challenge(body: bytes) -> bool:
        head = body[:4000].lower()
        return any(m.encode() in head for m in _CHALLENGE_MARKERS)

    def remember_cookies(self, cookies):
        """Store clearance cookies captured from the embedded browser.

        `cookies` is an iterable of (name, value, domain) tuples.

        NOTE: this hook is not yet auto-connected to the embedded WebStoreDialog
        cookie store (calibre's dialog internals vary by version). Once wired up
        -- e.g. profile.cookieStore().cookieAdded -> remember_cookies -- the
        silent green-arrow path will reuse the DDoS-Guard clearance for ~20 min."""
        for name, value, domain in cookies:
            if any(name.startswith(p) for p in _CLEARANCE_COOKIES):
                self._cookies[(domain.lstrip('.'), name)] = value

    # ---------------------------------------------------------------- mirrors

    def _fetch_wikipedia_mirrors(self, br, timeout):
        """Scrape the live official domains from the Wikipedia infobox URL row.

        Anna's Archive tells users to consult Wikipedia for current domains, so
        this keeps the mirror list fresh without a plugin update."""
        try:
            with closing(br.open(WIKIPEDIA_URL, timeout=timeout)) as resp:
                text = resp.read().decode('utf-8', 'replace')
        except (HTTPError, URLError, TimeoutError, RemoteDisconnected, OSError) as err:
            prints(f"Anna's Archive: Wikipedia mirror refresh failed: {err}")
            return []
        m = re.search(r'class="[^"]*infobox-data url[^"]*".*?</td>', text, re.S)
        cell = m.group(0) if m else ''
        mirrors = []
        for dom in re.findall(r'href="(https?://annas-archive\.[a-z]{2,6})/?"', cell):
            dom = dom.rstrip('/')
            if dom not in mirrors:
                mirrors.append(dom)
        return mirrors

    def _mirrors(self, br=None, timeout=30):
        """Return the ordered mirror list to try (Wikipedia first when enabled)."""
        configured = list(self.config.get('mirrors', DEFAULT_MIRRORS)) or list(DEFAULT_MIRRORS)
        if not self.config.get('wikipedia_mirrors', True):
            return configured
        if self._wiki_mirrors is None:
            self._wiki_mirrors = self._fetch_wikipedia_mirrors(br or self._browser(), timeout)
        if not self._wiki_mirrors:
            return configured
        merged = list(self._wiki_mirrors)
        for m in configured + list(DEFAULT_MIRRORS):
            if m not in merged:
                merged.append(m)
        return merged

    # ----------------------------------------------------------------- search

    def _search(self, url: str, max_results: int, timeout: int) -> SearchResults:
        br = self._browser()
        doc = None
        counter = max_results

        for page in range(1, ceil(max_results / RESULTS_PER_PAGE) + 1):
            mirrors = self._mirrors(br, timeout)
            if self.working_mirror is not None and self.working_mirror in mirrors:
                mirrors.remove(self.working_mirror)
                mirrors.insert(0, self.working_mirror)
            for mirror in mirrors:
                try:
                    with closing(br.open(url.format(base=mirror, page=page), timeout=timeout)) as resp:
                        if resp.code < 500 or resp.code > 599:
                            self.working_mirror = mirror
                            doc = html.fromstring(resp.read())
                            break
                except (HTTPError, URLError, TimeoutError, RemoteDisconnected, OSError):
                    continue
            if doc is None:
                self.working_mirror = None
                raise Exception('No working mirrors of Anna\'s Archive found.')

            books = doc.xpath('//table/tr')
            for book in books:
                if counter <= 0:
                    break

                columns = book.findall("td")
                s = SearchResult()

                cover = columns[0].xpath('./a[@tabindex="-1"]')
                if cover:
                    cover = cover[0]
                else:
                    continue
                s.detail_item = cover.get('href', '').split('/')[-1]
                if not s.detail_item:
                    continue

                s.cover_url = ''.join(cover.xpath('(./span/img/@src)[1]'))
                s.title = ''.join(columns[1].xpath('./a/span/text()'))
                s.author = ''.join(columns[2].xpath('./a/span/text()'))
                s.formats = ''.join(columns[9].xpath('./a/span/text()')).upper()

                s.price = '$0.00'
                s.drm = SearchResult.DRM_UNLOCKED

                counter -= 1
                yield s

    def search(self, query, max_results=10, timeout=60) -> SearchResults:
        url = f'{{base}}/search?page={{page}}&q={quote_plus(query)}&display=table'
        search_opts = self.config.get('search', {})
        for option in SearchOption.options:
            value = search_opts.get(option.config_option, ())
            if isinstance(value, str):
                value = (value,)
            for item in value:
                url += f'&{option.url_param}={item}'
        yield from self._search(url, max_results, timeout)

    # ------------------------------------------------------------------- open

    def open(self, parent=None, detail_item=None, external=False):
        if detail_item:
            # If we have a md5 and the user wants the in-app captcha flow, take
            # them straight to Slow Partner Server #5 inside calibre's browser
            # so they solve the challenge once and the file downloads in-app.
            if not external and not self.config.get('open_external', False) \
                    and self.config.get('slow_captcha_browser', True):
                if self._open_slow_in_browser(parent, detail_item):
                    return
            url = self._get_url(detail_item)
        else:
            if self.working_mirror is not None:
                url = self.working_mirror
            else:
                url = self._mirrors()[0]
        if external or self.config.get('open_external', False):
            open_url(QUrl(url))
        else:
            d = WebStoreDialog(self.gui, self.working_mirror, parent, url)
            d.setWindowTitle(self.name)
            d.set_tags(self.config.get('tags', ''))
            d.exec()

    def _open_slow_in_browser(self, parent, md5) -> bool:
        """Open Slow Partner Server #5 in calibre's embedded Chromium so the user
        can clear the DDoS-Guard / captcha challenge and download in-app.

        Returns True if the dialog was shown."""
        base = self.working_mirror or self._mirrors()[0]
        # Slow Partner Server #5 == /slow_download/<md5>/0/4
        url = f"{base}/slow_download/{md5}/0/{SLOW_SERVER_ORDER[0]}"
        try:
            d = WebStoreDialog(self.gui, base, parent, url)
            d.setWindowTitle(self.name + " — Slow Partner Server #5 (solve the check, then download)")
            d.set_tags(self.config.get('tags', ''))
            d.exec()
            return True
        except Exception as err:
            prints(f"Anna's Archive: embedded slow-download browser failed: {err}")
            return False

    # ---------------------------------------------------------------- details

    def get_details(self, search_result: SearchResult, timeout=60):
        if not search_result.formats:
            return

        _format = '.' + search_result.formats.lower()
        fmt = search_result.formats

        link_opts = self.config.get('link', {})
        url_extension = link_opts.get('url_extension', True)
        content_type = link_opts.get('content_type', False)
        use_slow = self.config.get('slow_servers', True)

        br = self._browser()
        with closing(br.open(self._get_url(search_result.detail_item), timeout=timeout)) as f:
            doc = html.fromstring(f.read())

        # 1) Preferred path: resolve a Slow Partner Server (#5 first) to a real
        #    file URL. If we manage it, the green-arrow button downloads silently.
        if use_slow:
            slow_url = self._resolve_best_slow(doc, br, timeout)
            if slow_url:
                search_result.downloads[fmt] = slow_url
                return

        # 2) Fallback: the external mirrors (Libgen / Sci-Hub / Z-Library).
        for link in doc.xpath('//div[@id="md5-panel-downloads"]//ul[contains(@class, "list-inside")]'
                              '/li/a[contains(@class, "js-download-link")]'):
            url = link.get('href')
            link_text = ''.join(link.itertext())

            try:
                if link_text == 'Libgen.li':
                    url = self._get_libgen_link(url, br)
                elif link_text == 'Libgen.rs Fiction' or link_text == 'Libgen.rs Non-Fiction':
                    url = self._get_libgen_nonfiction_link(url, br)
                elif link_text.startswith('Sci-Hub'):
                    url = self._get_scihub_link(url, br)
                elif link_text == 'Z-Library':
                    url = self._get_zlib_link(url, br)
                else:
                    continue
            except (HTTPError, URLError, TimeoutError, RemoteDisconnected, OSError):
                continue

            if not url:
                continue

            # Takes longer, but more accurate
            if content_type:
                try:
                    with urlopen(Request(url, method='HEAD'), timeout=timeout) as resp:
                        if resp.info().get_content_maintype() != 'application':
                            continue
                except (HTTPError, URLError, TimeoutError, RemoteDisconnected):
                    pass
            elif url_extension:
                # Speeds it up by checking the extension of the url. Only keep
                # urls that end with the wanted extension (dynamic endpoints like
                # get.php are kept since they can still serve the right file).
                params = url.find("?")
                if params < 0:
                    params = None
                if not url.endswith(_format, 0, params) and not self._looks_dynamic(url):
                    continue
            search_result.downloads[f"{link_text}.{fmt}"] = url

    # ------------------------------------------------- slow partner servers

    def _resolve_best_slow(self, doc, br, timeout):
        """Try the Slow Partner Servers in #5-first order and return the first
        real file URL we can resolve headlessly. Returns None if every server we
        try is behind a JS challenge (then the user falls back to open())."""
        slow = {}
        for a in doc.xpath('//div[@id="md5-panel-downloads"]//a[contains(@class, "js-download-link")]'):
            href = a.get('href') or ''
            m = re.search(r'/slow_download/[^/]+/\d+/(\d+)', href)
            if m:
                slow.setdefault(int(m.group(1)), urljoin(self._base(), href))

        if not slow:
            return None

        order = [i for i in SLOW_SERVER_ORDER if i in slow]
        order += [i for i in sorted(slow) if i not in SLOW_SERVER_ORDER]

        challenged = False
        for idx in order:
            try:
                url = self._resolve_slow_download(slow[idx], br, timeout)
            except _SlowChallenge:
                challenged = True
                continue
            except (HTTPError, URLError, TimeoutError, RemoteDisconnected, OSError):
                continue
            if url:
                prints(f"Anna's Archive: resolved Slow Partner Server #{idx + 1}")
                return url
        if challenged:
            prints("Anna's Archive: slow servers behind a challenge; use the "
                   "download via the result's open action to solve it in-app.")
        return None

    def _resolve_slow_download(self, url, br, timeout):
        """Fetch a /slow_download/ page and extract the real file link.

        The countdown on that page is purely client-side -- the href is present
        in the initial HTML -- so mechanize can read it *iff* the page isn't
        gated by a JS challenge."""
        try:
            with closing(br.open(url, timeout=timeout)) as resp:
                final = resp.geturl()
                body = resp.read()
        except HTTPError as e:
            if e.code in (403, 429, 503):
                raise _SlowChallenge(url)
            raise

        if self._is_challenge(body):
            raise _SlowChallenge(url)

        doc = html.fromstring(body)
        href = ''.join(doc.xpath('//a[@id="download-button"]/@href')).strip()
        if not href:
            # Some variants put the URL as plain text in a styled span.
            href = ''.join(t.strip() for t in doc.xpath(
                '//span[contains(@class, "bg-gray-200") and contains(@class, "break-all")]/text()')).strip()
        if not href:
            href = ''.join(doc.xpath('//a[contains(@class, "js-download-link")]/@href')).strip()
        if not href:
            return None
        return urljoin(final, href)

    # ----------------------------------------------------- external mirrors

    @staticmethod
    def _get_libgen_link(url: str, br) -> str:
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _, host, _ = resp.geturl().split('/', 3)
        url = ''.join(doc.xpath('//a[h2[text()="GET"]]/@href'))
        return f"{scheme}//{host}/{url}"

    @staticmethod
    def _get_libgen_nonfiction_link(url: str, br) -> str:
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
        url = ''.join(doc.xpath('//h2/a[text()="GET"]/@href'))
        return url

    @staticmethod
    def _get_scihub_link(url, br):
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _ = resp.geturl().split('/', 1)
        url = ''.join(doc.xpath('//embed[@id="pdf"]/@src'))
        if url:
            return scheme + url

    @staticmethod
    def _get_zlib_link(url, br):
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _, host, _ = resp.geturl().split('/', 3)
        url = ''.join(doc.xpath('//a[contains(@class, "addDownloadedBook")]/@href'))
        if url:
            return f"{scheme}//{host}/{url}"

    # ------------------------------------------------------------------ misc

    @staticmethod
    def _looks_dynamic(url: str) -> bool:
        path = urlsplit(url).path.lower()
        return path.endswith(('.php', '.asp', '.aspx', '.cgi', '.jsp')) or '.' not in path.rsplit('/', 1)[-1]

    def _base(self):
        return self.working_mirror or self._mirrors()[0]

    def _get_url(self, md5):
        return f"{self._base()}/md5/{md5}"

    def config_widget(self):
        from calibre_plugins.store_annas_archive.config import ConfigWidget
        return ConfigWidget(self)

    def save_settings(self, config_widget):
        config_widget.save_settings()
