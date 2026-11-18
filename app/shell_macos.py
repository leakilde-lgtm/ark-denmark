"""The macOS window: NSWindow + WKWebView, with lastbook://app served by bridge.py through a WKURLSchemeHandler.

No port is opened. There is also no way for a page to call this program: no script message handler, no JavaScript bridge.
Python only ever speaks to the page through the scheme handler's answers and, for one route change, evaluateJavaScript.

The rules this file carries out (bridge.py decides, this file obeys):
  - One window. Every new-window request is cancelled.
  - The top level shows the app's own page and nothing else.
  - A frame shows library paths only.
  - A frame runs no scripts (a second layer under the engine's Content-Security-Policy), and a saved page opens nothing.
  - Nothing reaches the network: NO_NETWORK blocks every load but the app's own, before the first page. Nothing is ever handed to a browser or another program.
"""
import ctypes
import json
import os
import signal
import sys
import threading
import time
import traceback

import AppKit
import Foundation
import objc
import WebKit
from PyObjCTools import AppHelper

import bridge as B

SCHEME = 'lastbook'
HOME = B.ORIGIN_MAC + '/'
POLICY_CANCEL, POLICY_ALLOW, POLICY_DOWNLOAD = 0, 1, 2   # WKNavigationActionPolicy
LINK_ACTIVATED = 0                                       # WKNavigationTypeLinkActivated
DOCUMENT_START = 0                                       # WKUserScriptInjectionTimeAtDocumentStart
MODAL_FIRST_BUTTON = 1000                                # NSAlertFirstButtonReturn
STYLE = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskResizable
         | AppKit.NSWindowStyleMaskFullSizeContentView)   # the page runs under the title bar: its sidebar and toolbar share the band with the traffic lights
SIZE = (1280, 860)
MIN_SIZE = (640, 480)   # below 900 the page folds its sidebar away; it never needs the phone layout here
BAND = 52               # the toolbar band from the top, in points: the height an empty unified toolbar gives the title bar
# Where the band is free of controls the window drags by it. Only the page knows that, so it is asked as the pointer moves, before any click:
# the answer is true or false from this script, about the element under that point. A click with no answer yet goes to the page.
BAND_FREE = "(e => !e || !e.closest('a,button,input,select,textarea,label,summary,iframe,canvas,video,[role=button],[tabindex],[contenteditable]'))(document.elementFromPoint(%f, %f))"

# All a right-click offers. Anything else, now or after an update, goes: much of that menu starts a load outside the policy below, or takes the user or the words on the page out of the app.
MENU_KEEP = {'WKMenuItemIdentifier' + n for n in ('Copy', 'CopyImage', 'Cut', 'Paste')}
MENU_KEEP_ACTIONS = {'copy:', 'cut:', 'paste:', 'selectAll:', 'undo:', 'redo:'}

# The same rule as Windows' host-resolver setting: refuse by default, whatever the scheme, frame or kind of load. Compiled before the first page; with no list there is no page.
NO_NETWORK = json.dumps([{'trigger': {'url-filter': '.*'}, 'action': {'type': 'block'}}]
                        + [{'trigger': {'url-filter': '^' + scheme + ':'}, 'action': {'type': 'ignore-previous-rules'}} for scheme in (SCHEME, 'data', 'blob', 'about')])
NO_NETWORK_REFUSED = 'ARK could not switch off its network access, so it will not open anything.'

# Off where this WebKit lets us. These are WebKit's own private switches, looked up by name.
FEATURES_OFF = ('PeerConnectionEnabled',   # UDP sockets, which nothing else here can see or refuse
                'NavigationAPIEnabled',    # a second way to walk history, and the UI does not use it
                'LinkPreconnect', 'LinkPreconnectEarlyHintsEnabled', 'LinkDNSPrefetchEnabled', 'LinkPrefetchEnabled', 'SpeculationRulesPrefetchEnabled',   # names looked up and sockets opened with no load to refuse
                'WebSocketEnabled', 'WebTransportEnabled', 'BeaconAPIEnabled', 'PushAPIEnabled', 'PrivateClickMeasurementEnabled', 'SafeBrowsingEnabled')   # nothing here uses them

# Count this app, its page and the GPU, measured, and the Networking process as a fixed NETWORK_SHARE. Its PID is not asked for: the
# WKWebView testing-only _networkProcessIdentifier accessor RELEASE_ASSERTs when the page's data store has no network process attached
# in WebKit before 313919@main (May 2026, bug 315599), causing the launch crash in issue #20. The page and GPU accessors are
# WKWebViewPrivate SPI and return 0 when their process is absent.
# Only ending the page's process makes the others give it back. Over the limit it is ended and the app starts again at home.
# This samples and then acts, so a page allocating very fast goes over by what it takes in that time.
MEMORY_LIMIT = min(4 * 1024 ** 3, Foundation.NSProcessInfo.processInfo().physicalMemory() // 2)   # the whole app in normal use: under 1.6 GB here
MEMORY_EVERY = 0.1
MEMORY_FLOOR = 1536 * 1024 ** 2
MEMORY_QUIET = 10                # seconds after ending a page before ending another: the system needs a moment to give the memory back
NETWORK_SHARE = 32 * 1024 ** 2   # counted for the Networking process (blob URLs, web storage): measured 5.8 to 8.5 MB in eleven runs here, memory floods included
# All frames share one process and one thread here, so a saved page in a loop stops the whole window. A page that does not answer is ended.
PULSE_EVERY = 1                  # seconds between asking the page's process for an answer
STUCK_AFTER = 12                 # that many asks unanswered in a row: stuck
SLOW_ANSWER = 3                  # an answer this many seconds late...
SLOW_IN_A_ROW = 3                # ...this many times in a row: a page freezing the window on purpose, in slices
STOPPED = 'A page stopped working, so it was closed.'
TOO_BIG = 'A page was using too much memory, so it was closed.'
STUCK = 'A page stopped responding, so it was closed.'
_libproc = ctypes.CDLL('/usr/lib/libproc.dylib')


def show_error(message):
    """Before there is a window, or instead of one."""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    app.activateIgnoringOtherApps_(True)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_('ARK')
    alert.setInformativeText_(message)
    alert.setAlertStyle_(AppKit.NSAlertStyleCritical)
    alert.runModal()


def choose_folder(title, message):
    """Before there is a window: the chosen folder's path, '' if the picker was cancelled, or None if they quit."""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    app.activateIgnoringOtherApps_(True)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_('Choose Folder…')
    alert.addButtonWithTitle_('Quit').setKeyEquivalent_('\x1b')
    if alert.runModal() != MODAL_FIRST_BUTTON:
        return None
    panel = AppKit.NSOpenPanel.openPanel()
    panel.setCanChooseDirectories_(True)
    panel.setCanChooseFiles_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setMessage_('Choose the folder called LastbookData.')
    panel.setPrompt_('Choose')
    panel.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(os.path.expanduser('~/Downloads')))
    if panel.runModal() == AppKit.NSModalResponseOK:
        return str(panel.URL().path())
    return ''


def _footprint(pid):
    """Bytes of memory the system charges to this process, as Activity Monitor shows it. None if it cannot say."""
    buf = (ctypes.c_uint64 * 64)()
    if _libproc.proc_pid_rusage(pid, 4, ctypes.byref(buf)) != 0:   # RUSAGE_INFO_V4
        return None
    return buf[2 + 7]   # after the 16-byte uuid: ri_phys_footprint


def _read_body(req):
    """A request's body, read no further than the most the bridge would take: past that it refuses by length alone."""
    data = req.HTTPBody()
    if data is not None:
        return bytes(data) if data.length() <= B.MAX_POST else bytes(B.MAX_POST + 1)   # bytes(data) would copy all of it first
    stream = req.HTTPBodyStream()
    if stream is None:
        return None
    out = bytearray()
    stream.open()
    try:
        while stream.hasBytesAvailable() and len(out) <= B.MAX_POST:
            n, piece = stream.read_maxLength_(None, 65536)
            if n <= 0:
                break
            out += bytes(piece)[:n]
    finally:
        stream.close()
    return bytes(out)


class _Pulse:
    """Asks the page's process for a constant, which it can only answer after it has taken everything sent before the question.
    didReceiveData says nothing of the kind: for a page too busy to read, it just queues the bytes in this process. So a piece's
    budget comes back only when an answer does, and a page that stops reading stops its answers and with them its bytes.
    The answer is a constant from a script this program wrote; no page can put anything in it. One question at a time, on the main thread."""

    def __init__(self, view, on_stuck):
        self.view, self.on_stuck = view, on_stuck
        self.waiting = []
        self.asked_at = None
        self.misses = self.slow = 0
        self.paused = 0
        self.acks_out = []

    def add(self, ack):
        self.waiting.append(ack)
        if self.asked_at is None:
            self.ask()

    def ask(self):
        acks, self.waiting = self.waiting, []
        self.asked_at = time.monotonic()
        generation = self.asked_at

        def answered(result, error):
            if self.asked_at != generation:     # reset() already released these
                return
            late = time.monotonic() - self.asked_at
            self.asked_at = None
            self.acks_out = []
            self.misses = 0
            self.slow = self.slow + 1 if late >= SLOW_ANSWER and not self.paused else 0
            self.release(acks)
            if self.slow >= SLOW_IN_A_ROW:
                self.on_stuck()
            elif self.waiting:
                self.ask()

        self.acks_out = acks
        self.view.evaluateJavaScript_completionHandler_('0', answered)

    def release(self, acks):
        for ack in acks:
            ack()

    def tick(self):
        """Every PULSE_EVERY seconds. Ticks, not the clock: a Mac asleep is not a page that is stuck."""
        if self.asked_at is None:
            self.ask()
        elif self.paused:
            self.misses = 0
        else:
            self.misses += 1
            if self.misses >= STUCK_AFTER:
                self.on_stuck()

    def reset(self):
        """The page's process has gone, and any question to it with it."""
        self.asked_at = None
        self.misses = self.slow = 0
        out, self.acks_out = self.acks_out, []
        waiting, self.waiting = self.waiting, []
        self.release(out + waiting)


class _Task(B.Sink):
    """One WKURLSchemeTask. The bridge calls head/data/finish on a worker; the task is touched only on the main thread, in that order,
    and never after WebKit has stopped it (WebKit raises if it is)."""

    def __init__(self, task, forget, pulse):
        self.task, self.forget, self.pulse = task, forget, pulse
        self.ex = None
        self.stopped = False

    def head(self, status, reason, headers):
        AppHelper.callAfter(self._head, status, headers)

    def data(self, chunk, ack):
        AppHelper.callAfter(self._data, chunk, ack)

    def finish(self, ok):
        AppHelper.callAfter(self._finish, ok)

    def stop(self):
        self.stopped = True
        if self.ex:
            self.ex.cancel()

    def _broke(self):
        traceback.print_exc()
        self.stop()
        self.forget(self)

    def _head(self, status, headers):
        if self.stopped:
            return
        try:
            res = Foundation.NSHTTPURLResponse.alloc().initWithURL_statusCode_HTTPVersion_headerFields_(self.task.request().URL(), status, 'HTTP/1.1', B.headers_dict(headers))
            self.task.didReceiveResponse_(res)
        except Exception:
            self._broke()

    def _data(self, chunk, ack):
        try:
            if self.stopped:
                return ack()
            self.task.didReceiveData_(Foundation.NSData.dataWithBytes_length_(chunk, len(chunk)))
            self.pulse.add(ack)
        except Exception:
            ack()
            self._broke()

    def _finish(self, ok):
        if self.stopped:
            return
        try:
            if ok:
                self.task.didFinish()
            else:
                self.task.didFailWithError_(Foundation.NSError.errorWithDomain_code_userInfo_(Foundation.NSURLErrorDomain, Foundation.NSURLErrorNetworkConnectionLost, None))
        except Exception:
            traceback.print_exc()
        self.forget(self)


class SchemeHandler(Foundation.NSObject, protocols=[objc.protocolNamed('WKURLSchemeHandler')]):
    def setBridge_(self, bridge):
        self.bridge = bridge
        self.pulse = None
        self.live = {}

    @objc.python_method
    def _forget(self, t):
        self.live.pop(id(t.task), None)

    def webView_startURLSchemeTask_(self, webview, task):
        try:
            req = task.request()
            t = _Task(task, self._forget, self.pulse)
            self.live[id(task)] = t
            method = str(req.HTTPMethod() or 'GET')
            headers = dict(req.allHTTPHeaderFields() or {})
            body = _read_body(req) if method == 'POST' and self.bridge.has_key(headers) else None   # with no key it is refused unread
            t.ex = self.bridge.start(method, str(req.URL().absoluteString()), headers, body, t)
        except Exception:
            traceback.print_exc()

    def webView_stopURLSchemeTask_(self, webview, task):
        try:
            t = self.live.pop(id(task), None)
            if t:
                t.stop()
        except Exception:
            traceback.print_exc()


class LastbookWebView(WebKit.WKWebView):
    _free = None    # (x, y, true when nothing to click is there) for the last point asked about in the band

    @objc.python_method
    def _in_band(self, event):
        """The point in CSS pixels from the view's top left, when it is in the toolbar band and the window is not full screen; else None."""
        w = self.window()
        if w is None or w.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
            return None
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        y = p.y if self.isFlipped() else self.bounds().size.height - p.y
        return (p.x, y) if 0 <= y < BAND else None

    def mouseMoved_(self, event):
        objc.super(LastbookWebView, self).mouseMoved_(event)
        try:
            at = self._in_band(event)
            if at and (self._free is None or abs(self._free[0] - at[0]) + abs(self._free[1] - at[1]) > 3):
                self._free = (*at, False)
                def answered(result, error, at=at):
                    if self._free and self._free[:2] == at:
                        self._free = (*at, result is True or result == 1)
                self.evaluateJavaScript_completionHandler_(BAND_FREE % at, answered)
        except Exception:
            traceback.print_exc()

    def mouseDown_(self, event):
        try:
            at = self._in_band(event)
            f = self._free
            if at and f and f[2] and abs(f[0] - at[0]) + abs(f[1] - at[1]) <= 3:
                if event.clickCount() == 2:
                    act = Foundation.NSUserDefaults.standardUserDefaults().stringForKey_('AppleActionOnDoubleClick')
                    if act == 'Minimize':
                        self.window().miniaturize_(None)
                    elif act != 'None':
                        self.window().zoom_(None)
                else:
                    self.window().performWindowDragWithEvent_(event)
                return
        except Exception:
            traceback.print_exc()
        objc.super(LastbookWebView, self).mouseDown_(event)

    def willOpenMenu_withEvent_(self, menu, event):
        try:
            for item in list(menu.itemArray()):
                action = item.action()      # a selector comes back as bytes
                action = action.decode() if isinstance(action, bytes) else str(action or '')
                if not item.isSeparatorItem() and str(item.identifier() or '') not in MENU_KEEP and action not in MENU_KEEP_ACTIONS:
                    menu.removeItem_(item)
            items = list(menu.itemArray())
            for i, item in reversed(list(enumerate(items))):    # separators with nothing left between them
                if item.isSeparatorItem() and (i == 0 or i == len(items) - 1 or items[i - 1].isSeparatorItem()):
                    menu.removeItem_(item)
                    items.pop(i)
        except Exception:
            traceback.print_exc()
            menu.removeAllItems()


def _from_top(action):
    """Did the app's own page ask for this? A request with no frame behind it gets no benefit of the doubt."""
    src = action.sourceFrame()
    return src is not None and bool(src.isMainFrame())


def _gesture(action):
    """True or False for a real user gesture behind this request. None if this WebKit cannot say: the request is then judged by its frame alone."""
    try:
        if action.respondsToSelector_('_isUserInitiated'):
            return bool(action._isUserInitiated())
    except Exception:
        traceback.print_exc()
    return None


class _Guard(Foundation.NSObject):
    """Every load and every request for a window passes here. Anything unexpected ends in a refusal.
    Declaring protocols= here would cost PyObjC its signatures for WebKit's completion handlers, and calling one would then abort the process."""

    def setShell_(self, shell):
        self.shell = shell

    def webView_decidePolicyForNavigationAction_preferences_decisionHandler_(self, webview, action, preferences, handler):
        try:
            decision = self.shell.decide(action)
            target = action.targetFrame()
            if (target is None or not target.isMainFrame()) and not self.shell.bridge.is_pdf_viewer(str(action.request().URL().absoluteString())):
                preferences.setAllowsContentJavaScript_(False)
        except Exception:
            traceback.print_exc()
            decision = POLICY_CANCEL
        handler(decision, preferences)

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, webview, config, action, features):
        try:
            self.shell.leave(str(action.request().URL().absoluteString()), _from_top(action), _gesture(action))
        except Exception:
            traceback.print_exc()
        return None

    def webViewWebContentProcessDidTerminate_(self, webview):
        try:
            self.shell.page_process_ended()
        except Exception:
            traceback.print_exc()

    def webView_navigationAction_didBecomeDownload_(self, webview, action, download):
        download.setDelegate_(self)
        self.shell.downloads.append(download)   # WebKit does not keep it alive

    def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(self, download, response, name, handler):
        try:
            self.shell.choose_save_path(str(name), lambda path: handler(Foundation.NSURL.fileURLWithPath_(path) if path else None))
        except Exception:
            traceback.print_exc()
            handler(None)

    def downloadDidFinish_(self, download):
        self.shell.download_ended(download)

    def download_didFailWithError_resumeData_(self, download, error, resume):
        self.shell.download_ended(download)

    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(self, webview, message, frame, handler):
        self._panel('alert', message, lambda ok: handler())

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(self, webview, message, frame, handler):
        self._panel('confirm', message, handler)

    @objc.python_method
    def _panel(self, kind, message, done):
        try:
            self.shell.ask(kind, str(message), done)
        except Exception:
            traceback.print_exc()
            done(False)


class AppDelegate(Foundation.NSObject):
    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def applicationSupportsSecureRestorableState_(self, app):
        return True

    def applicationWillTerminate_(self, note):
        threading.Timer(2, os._exit, [0]).start()   # engine threads must not hold up the exit


class Shell:
    def __init__(self, bridge):
        self.bridge = bridge
        self.downloads = []
        self.window = self.view = None
        self.ended_why = None      # why this program ended the page's process, until the view says it has gone
        self.ended_at = 0.0
        self.pids = ()
        self.network_off = False

    # ---- the window
    def make_window(self):
        w = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(Foundation.NSMakeRect(0, 0, *SIZE), STYLE, AppKit.NSBackingStoreBuffered, False)
        w.setTitle_('ARK')    # hidden in the bar, still named in the Window menu and Mission Control
        w.setTitlebarAppearsTransparent_(True)
        w.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        bar = AppKit.NSToolbar.alloc().initWithIdentifier_('lastbook')   # empty: it only makes the band 52 points tall, with the traffic lights centred in it
        bar.setShowsBaselineSeparator_(False)
        w.setToolbar_(bar)
        w.setToolbarStyle_(AppKit.NSWindowToolbarStyleUnified)
        w.setBackgroundColor_(AppKit.NSColor.textBackgroundColor())     # the page's own surface, light or dark, so nothing else shows before the first paint
        w.setReleasedWhenClosed_(False)
        w.setMinSize_(Foundation.NSMakeSize(*MIN_SIZE))
        w.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorFullScreenPrimary)
        w.center()
        return w

    def build(self):
        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        self.scheme = SchemeHandler.alloc().init()
        self.scheme.setBridge_(self.bridge)
        cfg.setURLSchemeHandler_forURLScheme_(self.scheme, SCHEME)
        cfg.userContentController().addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(self.bridge.top_script(), DOCUMENT_START, True))
        cfg.setAllowsAirPlayForMediaPlayback_(False)
        if cfg.respondsToSelector_('setWritingToolsBehavior:'):
            cfg.setWritingToolsBehavior_(-1)    # NSWritingToolsBehaviorNone: its tools can send what is selected to a service
        prefs = cfg.preferences()
        prefs.setJavaScriptCanOpenWindowsAutomatically_(False)
        prefs.setFraudulentWebsiteWarningEnabled_(False)   # the one thing here that would go looking on the network
        prefs.setElementFullscreenEnabled_(True)           # the video player's full screen button
        # Tab follows System Settings > Keyboard > Keyboard navigation, as in AppKit: off, it moves between text fields and the chosen sidebar row;
        # on, it reaches every control and, here, every link (the page's cards and rows are links), in the page's order.
        if prefs.respondsToSelector_('setTabFocusesLinks:'):
            prefs.setTabFocusesLinks_(bool(AppKit.NSApplication.sharedApplication().isFullKeyboardAccessEnabled()))
        self.switch_off(prefs)
        self.window = self.make_window()
        frame = self.window.contentView().bounds()
        # Under the page, the system's sidebar material: it takes the desktop's tint and greys when the window is not in front. The page is clear
        # only where its sidebar is (see app.css, "in the Mac app the sidebar is the window's own sidebar material") and paints the rest itself.
        material = AppKit.NSVisualEffectView.alloc().initWithFrame_(frame)
        material.setMaterial_(AppKit.NSVisualEffectMaterialSidebar)
        material.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        material.setState_(AppKit.NSVisualEffectStateFollowsWindowActiveState)
        material.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self.window.contentView().addSubview_(material)
        self.view = LastbookWebView.alloc().initWithFrame_configuration_(frame, cfg)
        self.view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        try:
            self.view.setValue_forKey_(False, 'drawsBackground')    # the web view itself paints nothing, so the material shows where the page is clear
        except Exception:
            traceback.print_exc()
        self.view.setAllowsLinkPreview_(False)
        if self.view.respondsToSelector_('setUnderPageBackgroundColor:'):
            self.view.setUnderPageBackgroundColor_(AppKit.NSColor.textBackgroundColor())
        self.window.setAcceptsMouseMovedEvents_(True)    # for the band: see LastbookWebView.mouseMoved_
        self.guard = _Guard.alloc().init()
        self.guard.setShell_(self)
        self.view.setNavigationDelegate_(self.guard)
        self.view.setUIDelegate_(self.guard)
        self.window.contentView().addSubview_(self.view)
        self.pulse = self.scheme.pulse = _Pulse(self.view, lambda: self.end_page(STUCK, 'not answering'))
        self.switch_off_network()
        self.pulse_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(PULSE_EVERY, True, lambda timer: self._each_second())
        self._each_second()
        threading.Thread(target=self._watch_memory, daemon=True, name='memory').start()

    def switch_off(self, prefs):
        try:
            for feature in WebKit.WKPreferences._features():
                if str(feature.key()) in FEATURES_OFF:
                    prefs._setEnabled_forFeature_(False, feature)
        except Exception:
            traceback.print_exc()

    def switch_off_network(self):
        """Compiling takes a moment and ends on the main thread. The first page waits for it."""
        def compiled(rules, error):
            if rules is None:
                print('no network rule list:', error, flush=True)
                return self.refuse_to_open()
            self.view.configuration().userContentController().addContentRuleList_(rules)
            self.network_off = True
            self.go_home()

        WebKit.WKContentRuleListStore.defaultStore().compileContentRuleListForIdentifier_encodedContentRuleList_completionHandler_('lastbook-no-network', NO_NETWORK, compiled)

    def refuse_to_open(self):
        show_error(NO_NETWORK_REFUSED)
        AppKit.NSApplication.sharedApplication().terminate_(None)

    def go_home(self):
        if not self.network_off:
            return
        self.view.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(HOME)))

    # ---- what may load where
    def decide(self, action):
        url = str(action.request().URL().absoluteString())
        from_top, gesture = _from_top(action), _gesture(action)
        target = action.targetFrame()
        if action.shouldPerformDownload():
            own_blob = url.startswith('blob:' + B.ORIGIN_MAC + '/')   # the chat's "save this conversation", and nothing else in the UI
            return POLICY_DOWNLOAD if own_blob and from_top and gesture is not False else POLICY_CANCEL
        if target is None:                 # a link with target=_blank: WebKit asks here first
            self.leave(url, from_top, gesture)
            return POLICY_CANCEL
        if target.isMainFrame():
            if self.bridge.top_url_ok(url) and (from_top or action.sourceFrame() is None):   # no frame: this program's own go_home
                return POLICY_ALLOW
            if from_top and action.navigationType() == LINK_ACTIVATED:   # a plain link in the UI to a file or to the web
                self.leave(url, from_top, gesture)
            return POLICY_CANCEL
        if not self.bridge.frame_url_ok(url):
            return POLICY_CANCEL
        if from_top:
            self.bridge.give_ticket(url)   # only the app's own page earns a whole large answer
        return POLICY_ALLOW

    def leave(self, url, from_top, gesture):
        """The one window stays. See bridge.new_window for what happens instead."""
        what, where = self.bridge.new_window(url, from_top, gesture)
        if what == 'inside':
            route = where[len(HOME):] or '#/'
            self.view.evaluateJavaScript_completionHandler_('location.hash=' + json.dumps(route), None)

    # ---- the page's process
    def page_pid(self):
        return self._view_pid('_webProcessIdentifier')

    def _view_pid(self, name):
        return int(getattr(self.view, name)()) if self.view.respondsToSelector_(name) else 0

    def _each_second(self):
        try:
            self.pids = (self.page_pid(), self._view_pid('_gpuProcessIdentifier'))   # never _networkProcessIdentifier: see MEMORY_LIMIT
            self.pulse.tick()
        except Exception:
            traceback.print_exc()

    def _watch_memory(self):
        """On a thread of its own, so a stuck main thread cannot stop it. self.pids is replaced whole, on the main thread."""
        me = os.getpid()
        while True:
            time.sleep(MEMORY_EVERY)
            try:
                pids = self.pids
                pages = sum(_footprint(p) or 0 for p in pids if p)
                total = pages + (_footprint(me) or 0) + NETWORK_SHARE
                # MEMORY_FLOOR: with the page's share this small, ending it would give nothing back.
                if total > MEMORY_LIMIT and pages > MEMORY_FLOOR and pids and pids[0] and time.monotonic() - self.ended_at > MEMORY_QUIET:
                    self.end_page(TOO_BIG, f'over the memory limit: {total} bytes, {pages} of it the page\'s')
            except Exception:
                traceback.print_exc()

    def end_page(self, why, detail):
        pid = self.page_pid() if threading.current_thread() is threading.main_thread() else self.pids[0]
        if not pid or self.ended_why:
            return
        print(f'ending the page process {pid}: {detail}', flush=True)
        self.ended_why, self.ended_at = why, time.monotonic()
        os.kill(pid, signal.SIGKILL)   # the only end a stuck page cannot put off. WebKit's own _killWebContentProcess left it running and the view wedged.

    def page_process_ended(self):
        message = self.ended_why or STOPPED
        self.ended_why = None
        self.ended_at = time.monotonic()
        self.pulse.reset()
        self.go_home()
        self.ask('alert', message, lambda ok: None)

    # ---- what the user is asked
    def ask(self, kind, message, done):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.addButtonWithTitle_('OK')
        if kind == 'confirm':
            alert.addButtonWithTitle_('Cancel')
        self.pulse.paused += 1      # an alert from the app's own page holds its process until it is answered: that is not a page stuck

        def answered(r):
            self.pulse.paused -= 1
            done(r == MODAL_FIRST_BUTTON)

        alert.beginSheetModalForWindow_completionHandler_(self.window, answered)

    def choose_save_path(self, name, done):
        panel = AppKit.NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(name)
        panel.beginSheetModalForWindow_completionHandler_(self.window, lambda r: done(str(panel.URL().path()) if r == AppKit.NSModalResponseOK else None))

    def download_ended(self, download):
        if download in self.downloads:
            self.downloads.remove(download)


# Is the sidebar showing? Asked of the page as the View menu opens, so its item can say Show Sidebar or Hide Sidebar. The answer is true or false only.
SIDE_SHOWN = "(c => innerWidth >= 900 ? !c.contains('side-off') : c.contains('side-open'))(document.documentElement.classList)"


class _Go(Foundation.NSObject):
    """The Go, View and Help menu items that move the page. They speak to it only as leave() does: native to page, a route or a click.
    The one answer that comes back is SIDE_SHOWN's true or false."""
    def setView_(self, view):
        self.view = view
        self.side_item = None

    def go_(self, item):
        self.view.evaluateJavaScript_completionHandler_('location.hash=' + json.dumps(dict(GO)[str(item.title())]), None)

    def help_(self, item):
        self.view.evaluateJavaScript_completionHandler_("location.hash='#/about'", None)

    def checkUpdates_(self, item):
        self.view.evaluateJavaScript_completionHandler_("window.LBUpdate && LBUpdate.fromMenu()", None)    # web/update.js: About, then one check

    @objc.python_method
    def _side_title(self, shown):
        if self.side_item is not None:
            self.side_item.setTitle_('Hide Sidebar' if shown else 'Show Sidebar')

    def menuNeedsUpdate_(self, menu):
        one_each(menu)
        if self.side_item is None or self.side_item.menu() != menu:
            return
        def answered(result, error):
            if error is None and result in (True, False, 0, 1):
                self._side_title(bool(result))
        try:
            self.view.evaluateJavaScript_completionHandler_(SIDE_SHOWN, answered)
        except Exception:
            traceback.print_exc()

    def toggleSidebar_(self, item):
        self.view.evaluateJavaScript_completionHandler_("document.getElementById('sideBtn').click()", None)
        if self.side_item is not None:
            self._side_title(str(self.side_item.title()) != 'Hide Sidebar')

    def find_(self, item):
        self.view.evaluateJavaScript_completionHandler_("document.dispatchEvent(new KeyboardEvent('keydown',{key:'f',code:'KeyF',metaKey:true}))", None)


GO = [('Videos', '#/videos'), ('Map', '#/map'), ('Chat', '#/ask')]
CTRL_CMD = AppKit.NSEventModifierFlagControl | AppKit.NSEventModifierFlagCommand
OPT_CMD = AppKit.NSEventModifierFlagOption | AppKit.NSEventModifierFlagCommand


def _menu(title, items, target=None):
    menu = AppKit.NSMenu.alloc().initWithTitle_(title)
    for name, action, key, *mods in items:
        if name is None:
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            continue
        item = menu.addItemWithTitle_action_keyEquivalent_(name, action, key)
        if mods:
            item.setKeyEquivalentModifierMask_(mods[0])
        if target is not None and action in ('go:', 'toggleSidebar:', 'find:', 'help:', 'checkUpdates:'):
            item.setTarget_(target)
    holder = AppKit.NSMenuItem.alloc().init()
    holder.setSubmenu_(menu)
    return holder


APPKIT_ADDS = ('toggleFullScreen:', 'startDictation:', 'orderFrontCharacterPalette:')


def one_each(menu):
    """AppKit adds its own Enter Full Screen, Start Dictation and Emoji & Symbols beside this menu's, sometimes more than once. Only the first
    of each stays, and the separators AppKit put before its copies go with them."""
    seen, after_line = set(), True
    for item in list(menu.itemArray()):
        if item.isSeparatorItem():
            if after_line:
                menu.removeItem_(item)
            after_line = True
            continue
        action = item.action()
        action = action.decode() if isinstance(action, bytes) else str(action or '')
        if action in APPKIT_ADDS and action in seen:
            menu.removeItem_(item)
            continue
        seen.add(action)
        after_line = False
    last = menu.itemArray().lastObject()
    if last is not None and last.isSeparatorItem():
        menu.removeItem_(last)


def install_menu(app, go=None):
    """Copy, paste and quit reach a web view only through the menu bar. go: a _Go for the window's view.
    Full screen, Start Dictation and Emoji & Symbols are this menu's own items; the copies AppKit adds are taken out (one_each), so there is one of each."""
    Foundation.NSUserDefaults.standardUserDefaults().registerDefaults_({'NSFullScreenMenuItemEverywhere': False})
    bar = AppKit.NSMenu.alloc().init()
    edit = _menu('Edit', [('Undo', 'undo:', 'z'), ('Redo', 'redo:', 'Z'), (None, None, None), ('Cut', 'cut:', 'x'), ('Copy', 'copy:', 'c'), ('Paste', 'paste:', 'v'), ('Select All', 'selectAll:', 'a'), (None, None, None), ('Find\u2026', 'find:', 'f'),
                                  (None, None, None), ('Start Dictation\u2026', 'startDictation:', ''), ('Emoji & Symbols', 'orderFrontCharacterPalette:', ' ', CTRL_CMD)], go)
    view = _menu('View', [('Hide Sidebar', 'toggleSidebar:', 's', CTRL_CMD), (None, None, None), ('Enter Full Screen', 'toggleFullScreen:', 'f', CTRL_CMD)], go)
    window = _menu('Window', [('Minimize', 'performMiniaturize:', 'm'), ('Zoom', 'performZoom:', ''), (None, None, None), ('Close', 'performClose:', 'w'),
                              (None, None, None), ('Bring All to Front', 'arrangeInFront:', '')])
    helps = _menu('Help', [('ARK Help', 'help:', '?')], go)
    for holder in (_menu('ARK', [('About ARK', 'orderFrontStandardAboutPanel:', ''), ('Check for Updates\u2026', 'checkUpdates:', ''), (None, None, None),
                                      ('Hide ARK', 'hide:', 'h'), ('Hide Others', 'hideOtherApplications:', 'h', OPT_CMD), ('Show All', 'unhideAllApplications:', ''),
                                      (None, None, None), ('Quit ARK', 'terminate:', 'q')], go),
                   edit,
                   view,
                   _menu('Go', [('Back', 'goBack:', '['), ('Forward', 'goForward:', ']'), (None, None, None)] + [(name, 'go:', str(i + 1)) for i, (name, _) in enumerate(GO)], go),
                   window, helps):
        bar.addItem_(holder)
    if go is not None:
        go.side_item = view.submenu().itemAtIndex_(0)
        view.submenu().setDelegate_(go)    # menuNeedsUpdate_: Show Sidebar or Hide Sidebar, as it is now
        edit.submenu().setDelegate_(go)    # menuNeedsUpdate_ tidies it as it opens, too
    app.setMainMenu_(bar)
    for later in (1.0, 3.0):     # after launch, when AppKit has added its own
        AppHelper.callLater(later, lambda: (one_each(view.submenu()), one_each(edit.submenu())))
    app.setWindowsMenu_(window.submenu())
    app.setHelpMenu_(helps.submenu())


def run(bridge):
    """Show the window and stay here until it closes."""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    shell = Shell(bridge)
    shell.build()
    go = shell.go = _Go.alloc().init()
    go.setView_(shell.view)
    install_menu(app, go)
    shell.window.makeKeyAndOrderFront_(None)
    if not getattr(sys, 'frozen', False):   # a built app is activated by the system when opened, and left alone when opened in the background
        app.activateIgnoringOtherApps_(True)
    app.run()
