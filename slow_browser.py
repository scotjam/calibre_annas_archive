"""In-process QtWebEngine browser for Anna's Archive slow downloads.

calibre's own ``WebStoreDialog`` runs the store browser in a *separate* process,
so a store plugin can't reach its cookie store or its downloads. This module
provides a small in-process Chromium dialog instead, pointed straight at
*Slow Partner Server #5*. It:

  * lets the user clear the DDoS-Guard / Cloudflare JS challenge (which has no
    JavaScript-free bypass) in a real browser;
  * captures the clearance cookie and hands it back to the store plugin so the
    silent (green-arrow) path can reuse it for a while;
  * once the challenge clears, finds the "Download now" link automatically (its
    href points at a partner CDN that is *not* behind the challenge), downloads
    it, and adds the file to the calibre library -- no manual clicking.

Everything is best-effort and defensive: QtWebEngine may be unavailable in some
builds, and several APIs differ between Qt5 and Qt6, so imports and signal wiring
are guarded.
"""

import os
import tempfile

from calibre import prints

try:  # Qt6 / calibre 6+
    from qt.core import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QUrl, QTimer
    from qt.webengine import QWebEngineView, QWebEnginePage, QWebEngineProfile, QWebEngineSettings
    _HAS_WEBENGINE = True
    _QT5 = False
except Exception:
    try:  # Qt5 / calibre 5
        from PyQt5.QtCore import QUrl, QTimer
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        from PyQt5.QtWebEngineWidgets import (QWebEngineView, QWebEnginePage, QWebEngineProfile,
                                              QWebEngineSettings)
        _HAS_WEBENGINE = True
        _QT5 = True
    except Exception:
        _HAS_WEBENGINE = False
        _QT5 = False

try:
    load_translations()
except Exception:
    def _(x):
        return x

# DDoS-Guard / Cloudflare clearance cookies worth re-using.
_CLEARANCE_PREFIXES = ('__ddg', 'cf_clearance', '__cf')

# JS that returns the real "Download now" href once the page has cleared the
# challenge (or '' if not present yet). The link's visible text contains
# "Download now"; its href is an absolute URL to a partner CDN.
_FIND_DOWNLOAD_JS = (
    "(function(){var a=[].slice.call(document.querySelectorAll('a'));"
    "for(var i=0;i<a.length;i++){var t=(a[i].textContent||'');"
    "if(t.indexOf('Download now')>=0 && a[i].href) return a[i].href;}"
    "return '';})()"
)


def available() -> bool:
    return _HAS_WEBENGINE


def _qba_to_str(value) -> str:
    try:
        return bytes(value).decode('utf-8', 'replace')
    except Exception:
        return str(value)


if _HAS_WEBENGINE:
    class _Page(QWebEnginePage):
        """Open target="_blank" links in the same view so downloads aren't lost."""

        def createWindow(self, _type):
            return self
else:  # pragma: no cover - placeholder so the class body below imports
    _Page = object


class SlowDownloadBrowser(QDialog):
    """A minimal Chromium window aimed at a /slow_download/ page."""

    def __init__(self, gui, store, url, user_agent, tags='', parent=None):
        super().__init__(parent or gui)
        self.gui = gui
        self.store = store
        self.tags = tags or ''
        self._tmpdir = tempfile.mkdtemp(prefix='aa_slow_')
        self._path = None
        self._added = False
        self._triggered = False  # have we kicked off the actual file download yet
        self._tries = 0

        self.setWindowTitle(_("Anna's Archive — Slow Partner Server #5"))
        self.resize(1000, 820)

        layout = QVBoxLayout(self)
        self.status = QLabel(_(
            'Waiting for the verification check to clear… the download then starts '
            'automatically and is added to your library. (If it asks you to wait or '
            'tick a box, do that — the rest is automatic.)'))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # Off-the-record profile: nothing persisted, but a working cookie store
        # and download signal.
        self.profile = QWebEngineProfile(self)
        try:
            if user_agent:
                self.profile.setHttpUserAgent(user_agent)
        except Exception:
            pass

        self.view = QWebEngineView(self)
        self._page = _Page(self.profile, self.view)
        self.view.setPage(self._page)
        # Force PDFs (and the like) to download instead of opening in the viewer.
        try:
            s = self._page.settings()
            for attr in ('PdfViewerEnabled', 'PluginsEnabled'):
                a = getattr(QWebEngineSettings.WebAttribute, attr, None) if not _QT5 \
                    else getattr(QWebEngineSettings, attr, None)
                if a is not None:
                    s.setAttribute(a, False)
        except Exception:
            pass
        layout.addWidget(self.view, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = QPushButton(_('Close'), self)
        self.close_btn.clicked.connect(self.accept)
        row.addWidget(self.close_btn)
        layout.addLayout(row)

        # Capture clearance cookies as soon as the challenge sets them.
        try:
            self.profile.cookieStore().cookieAdded.connect(self._cookie_added)
        except Exception as err:
            prints("Anna's Archive: cookie capture unavailable:", err)

        # Catch the actual file download.
        try:
            self.profile.downloadRequested.connect(self._download_requested)
        except Exception as err:
            prints("Anna's Archive: download hook unavailable:", err)

        self.view.load(QUrl(url))

        # Poll for the "Download now" link appearing once the challenge clears.
        self._poll = QTimer(self)
        self._poll.setInterval(1500)
        self._poll.timeout.connect(self._check_for_link)
        self._poll.start()

    # ----------------------------------------------------------- cookies

    def _cookie_added(self, cookie):
        try:
            name = _qba_to_str(cookie.name())
            if not any(name.startswith(p) for p in _CLEARANCE_PREFIXES):
                return
            value = _qba_to_str(cookie.value())
            domain = cookie.domain() or ''
            self.store.remember_cookies([(name, value, domain)])
        except Exception as err:
            prints("Anna's Archive: cookie parse failed:", err)

    # ------------------------------------------------- auto-find the link

    def _check_for_link(self):
        if self._triggered:
            return
        self._tries += 1
        if self._tries > 80:  # ~2 minutes; give up polling, leave window open
            self._poll.stop()
            self.status.setText(_(
                'Could not detect the download link automatically. If a '
                '"Download now" link is shown, click it.'))
            return
        try:
            self._page.runJavaScript(_FIND_DOWNLOAD_JS, self._on_link_found)
        except Exception as err:
            prints("Anna's Archive: runJavaScript failed:", err)

    def _on_link_found(self, href):
        if self._triggered or not href:
            return
        self._triggered = True
        try:
            self._poll.stop()
        except Exception:
            pass
        self.status.setText(_('Verification cleared — downloading…'))
        # Navigating the view to the file URL triggers downloadRequested.
        try:
            self.view.setUrl(QUrl(href))
        except Exception as err:
            prints("Anna's Archive: navigate-to-file failed:", err)

    # --------------------------------------------------------- downloads

    def _download_requested(self, item):
        try:
            if hasattr(item, 'setDownloadDirectory'):  # Qt6
                name = item.suggestedFileName() or 'annas-archive-download'
                item.setDownloadDirectory(self._tmpdir)
                item.setDownloadFileName(name)
                self._path = os.path.join(self._tmpdir, name)
                item.isFinishedChanged.connect(lambda: self._finished(item))
            else:  # Qt5
                name = item.suggestedFileName() or os.path.basename(item.path()) or 'annas-archive-download'
                self._path = os.path.join(self._tmpdir, name)
                item.setPath(self._path)
                item.finished.connect(lambda: self._finished(item))
            item.accept()
            self.status.setText(_('Downloading…'))
        except Exception as err:
            prints("Anna's Archive: download_requested failed:", err)

    def _finished(self, item):
        # isFinishedChanged / finished can fire more than once; only act once.
        if self._added:
            return
        try:
            is_finished = getattr(item, 'isFinished', None)
            if callable(is_finished) and not item.isFinished():
                return
            path = self._path
            if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
                self.status.setText(_('Download did not complete.'))
                return
            self._added = True

            if self._add_to_library(path):
                self.status.setText(_('✓ Downloaded and added to your calibre library. '
                                      'You can close this window.'))
            else:
                self.status.setText(_('Downloaded, but could not auto-add. Saved to: %s') % path)
        except Exception as err:
            prints("Anna's Archive: add to library failed:", err)

    def _add_to_library(self, path) -> bool:
        """Add the downloaded file to the current calibre library, applying any
        configured tags. Returns True on success."""
        try:
            add_action = self.gui.iactions.get('Add Books')
        except Exception:
            add_action = None

        tags = [t.strip() for t in self.tags.split(',') if t.strip()] if self.tags else []
        try:
            if tags:
                # add_filesystem_book runs asynchronously, so we can't reliably
                # tag afterwards. Import directly so we can set tags atomically.
                from calibre.ebooks.metadata.meta import get_metadata
                fmt = os.path.splitext(path)[1].lstrip('.').lower() or None
                with open(path, 'rb') as f:
                    mi = get_metadata(f, fmt)
                mi.tags = list(dict.fromkeys((list(mi.tags or []) + tags)))
                db = self.gui.current_db.new_api
                book_id = db.create_book_entry(mi)
                db.add_format(book_id, (fmt or 'unknown').upper(), path, run_hooks=False)
                self.gui.library_view.model().books_added(1)
                try:
                    self.gui.refresh_ondevice()
                except Exception:
                    pass
                return True
        except Exception as err:
            prints("Anna's Archive: tagged import failed, falling back:", err)

        if add_action is None:
            return False
        try:
            add_action.add_filesystem_book(path, allow_device=False)
            return True
        except Exception as err:
            prints("Anna's Archive: add_filesystem_book failed:", err)
            return False
