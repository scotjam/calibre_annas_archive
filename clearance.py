"""Obtain a DDoS-Guard clearance cookie for Anna's Archive using a real browser.

The /slow_download/ pages are gated by a DDoS-Guard JavaScript challenge that
calibre's JS-free downloader (mechanize) cannot pass, so the download button
would never get a real file URL. But once a *real* browser clears the challenge,
the clearance cookies it sets let plain mechanize requests through (verified:
urllib + those cookies returns HTTP 200 and the real partner-CDN link).

This module runs a minimised in-process QtWebEngine window that clears the
challenge (usually automatically, no user interaction) and captures the cookies.
QtWebEngine must run on the GUI thread, but get_details runs on a worker thread,
so ClearanceManager marshals the request across threads via a queued signal and
blocks the worker on a threading.Event until the GUI side is done.
"""

import threading

from calibre import prints

try:  # Qt6 / calibre 6+
    from qt.core import (QObject, pyqtSignal, QTimer, QUrl, QEventLoop,
                         QDialog, QVBoxLayout, QLabel)
    from qt.webengine import QWebEngineView, QWebEnginePage, QWebEngineProfile
    _OK = True
except Exception:
    try:  # Qt5 / calibre 5
        from PyQt5.QtCore import QObject, pyqtSignal, QTimer, QUrl, QEventLoop
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QLabel
        from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineProfile
        _OK = True
    except Exception:
        _OK = False

# Presence of any of these cookie names means the challenge has been cleared.
_CLEAR_MARKERS = ('__ddg1_', '__ddg5_', '__ddgid_', 'cf_clearance')


def available() -> bool:
    return _OK


if _OK:
    class _ClearDialog(QDialog):
        """Minimised browser that clears the challenge and collects cookies."""

        def __init__(self, store, url, user_agent, parent=None):
            super().__init__(parent)
            self.store = store
            self._fired = False
            self._t = 0
            self._clear_ticks = 0
            self._got = {}

            self.setWindowTitle("Anna's Archive — verifying access…")
            self.resize(900, 700)
            lay = QVBoxLayout(self)
            self.status = QLabel("Verifying access to Anna's Archive… this closes automatically. "
                                 "If a captcha appears, please complete it.")
            self.status.setWordWrap(True)
            lay.addWidget(self.status)

            self.profile = QWebEngineProfile(self)
            try:
                if user_agent:
                    self.profile.setHttpUserAgent(user_agent)
            except Exception:
                pass
            self.view = QWebEngineView(self)
            self._page = QWebEnginePage(self.profile, self.view)
            self.view.setPage(self._page)
            lay.addWidget(self.view, 1)

            try:
                self.profile.cookieStore().cookieAdded.connect(self._cookie_added)
            except Exception as err:
                prints("Anna's Archive: clearance cookie hook failed:", err)

            self.view.load(QUrl(url))

            self.timer = QTimer(self)
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self._tick)
            self.timer.start()

        def _cookie_added(self, cookie):
            try:
                domain = cookie.domain() or ''
                if 'annas-archive' not in domain:
                    return
                name = bytes(cookie.name()).decode('utf-8', 'replace')
                value = bytes(cookie.value()).decode('utf-8', 'replace')
                self._got[(domain.lstrip('.'), name)] = value
            except Exception:
                pass

        def _cleared(self) -> bool:
            return any(n in _CLEAR_MARKERS for (_, n) in self._got)

        def _tick(self):
            self._t += 1
            if self._cleared():
                self._clear_ticks += 1
                if self._clear_ticks >= 2:        # let the rest of the cookies land
                    self._finish()
                    return
            if self._t == 10 and not self._cleared():
                # Auto-pass didn't happen; a captcha is probably shown -> surface it.
                self.status.setText("Please complete the verification / captcha in this window.")
                try:
                    self.showNormal(); self.raise_(); self.activateWindow()
                except Exception:
                    pass
            elif self._t >= 150:                  # hard timeout (~2.5 min)
                self._finish()

        def _finish(self):
            if self._fired:
                return
            self._fired = True
            try:
                self.timer.stop()
            except Exception:
                pass
            try:
                self.store.remember_cookies([(n, v, d) for (d, n), v in self._got.items()])
            except Exception as err:
                prints("Anna's Archive: storing clearance cookies failed:", err)
            try:
                self.accept()
            except Exception:
                pass


    class ClearanceManager(QObject):
        """Bridges a worker-thread clearance request to a GUI-thread browser."""

        _request = pyqtSignal(str)

        def __init__(self, store):
            # Parent to the GUI so this object has GUI-thread affinity and the
            # queued signal/slot runs on the GUI thread.
            super().__init__(store.gui)
            self.store = store
            self._lock = threading.Lock()
            self._event = threading.Event()
            self._request.connect(self._on_request)

        def ensure_clearance(self, url, timeout=160) -> bool:
            """Called from a worker thread. Blocks until clearance is obtained
            (or timeout). Returns True if the store now has clearance cookies."""
            with self._lock:
                if self.store.has_clearance():
                    return True
                self._event.clear()
                self._request.emit(url)
                self._event.wait(timeout)
                return self.store.has_clearance()

        def _on_request(self, url):
            # Runs on the GUI thread.
            try:
                dlg = _ClearDialog(self.store, url, self.store.user_agent(), self.store.gui)
                loop = QEventLoop()
                dlg.finished.connect(loop.quit)
                dlg.show()
                dlg.showMinimized()
                loop.exec()
            except Exception as err:
                prints("Anna's Archive: clearance dialog failed:", err)
            finally:
                self._event.set()
else:
    class ClearanceManager:  # pragma: no cover - degraded fallback
        def __init__(self, store):
            pass

        def ensure_clearance(self, url, timeout=160) -> bool:
            return False
