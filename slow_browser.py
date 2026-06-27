"""In-process QtWebEngine browser for Anna's Archive slow downloads.

calibre's own ``WebStoreDialog`` runs the store browser in a *separate* process,
so a store plugin can't reach its cookie store. This module provides a small
in-process Chromium dialog instead, which lets us:

  * capture the DDoS-Guard / Cloudflare clearance cookie the moment the user
    clears the challenge, and hand it back to the store plugin so the silent
    (green-arrow) download path can reuse it until it expires; and
  * accept the actual "Download now" download and add the file straight into the
    calibre library.

Everything here is best-effort and defensive: QtWebEngine may be unavailable in
some calibre builds, and the download-item API differs between Qt5 and Qt6, so
imports and signal wiring are all guarded.
"""

import os
import tempfile

from calibre import prints

try:  # Qt6 / calibre 6+
    from qt.core import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QUrl
    from qt.webengine import QWebEngineView, QWebEnginePage, QWebEngineProfile
    _HAS_WEBENGINE = True
    _QT5 = False
except Exception:
    try:  # Qt5 / calibre 5
        from PyQt5.QtCore import QUrl
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineProfile
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


def available() -> bool:
    return _HAS_WEBENGINE


def _qba_to_str(value) -> str:
    try:
        return bytes(value).decode('utf-8', 'replace')
    except Exception:
        return str(value)


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

        self.setWindowTitle(_("Anna's Archive — Slow Partner Server #5"))
        self.resize(1000, 820)

        layout = QVBoxLayout(self)
        self.status = QLabel(_(
            'Wait for the check to clear / solve the captcha, then click "Download now". '
            'The download is added to your library automatically, and the verification is '
            'reused so the normal download button works for a while afterwards.'))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # Off-the-record profile so nothing is persisted to disk; it still has a
        # working cookie store and download signal.
        self.profile = QWebEngineProfile(self)
        try:
            if user_agent:
                self.profile.setHttpUserAgent(user_agent)
        except Exception:
            pass

        self.view = QWebEngineView(self)
        self._page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self._page)
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

    # ----------------------------------------------------------- cookies

    def _cookie_added(self, cookie):
        try:
            name = _qba_to_str(cookie.name())
            if not any(name.startswith(p) for p in _CLEARANCE_PREFIXES):
                return
            value = _qba_to_str(cookie.value())
            domain = cookie.domain() or ''
            self.store.remember_cookies([(name, value, domain)])
            self.status.setText(_(
                '✓ Verification captured — the normal download button will now work '
                'for a while. You can finish the download here or close this window.'))
        except Exception as err:
            prints("Anna's Archive: cookie parse failed:", err)

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
            # Only proceed on a genuinely completed download.
            is_finished = getattr(item, 'isFinished', None)
            if callable(is_finished) and not item.isFinished():
                return
            path = self._path
            if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
                self.status.setText(_('Download did not complete.'))
                return
            self._added = True

            if self._add_to_library(path):
                self.status.setText(_('✓ Downloaded and added to your calibre library.'))
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
        if add_action is None:
            return False

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
                db.add_format(book_id, fmt.upper(), path, run_hooks=False)
                self.gui.library_view.model().books_added(1)
                self.gui.refresh_ondevice()
                return True
        except Exception as err:
            prints("Anna's Archive: tagged import failed, falling back:", err)

        # No tags (or tagged import failed): use the normal Add Books flow, which
        # handles format-merging and duplicate detection for us.
        try:
            add_action.add_filesystem_book(path, allow_device=False)
            return True
        except Exception as err:
            prints("Anna's Archive: add_filesystem_book failed:", err)
            return False
