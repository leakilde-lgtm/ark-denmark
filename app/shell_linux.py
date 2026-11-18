"""The Linux window: GTK 3 + WebKitGTK (WebKit2 4.1) through PyGObject, with lastbook://app served by bridge.py through a registered URI scheme.

No port is opened. There is no way for a page to call this program: no script message handler, no JavaScript bridge.
Python only ever speaks to the page through the scheme's answers and, for menu items, evaluate_javascript.

The rules this file carries out, as the Mac shell does (bridge.py decides, this file obeys):
  - One window, one copy of the app (GtkApplication's D-Bus name, or a lock file where there is no session bus). Every new-window request is cancelled.
  - The top level shows the app's own page and nothing else. WebKitGTK's navigation request does not say which frame it is for,
    so the frame is judged where it does say: the response, before anything is shown (is_main_frame_main_resource).
  - A frame shows library paths only. Saved pages and encyclopedia entries carry the engine's sandbox policy, so they cannot open
    windows or move the top level: every new-window request and every top-level navigation comes from the app's own page.
  - Nothing reaches the network: NO_NETWORK (the same content rule list as on the Mac) blocks every load but the app's own, before the first page.
    Nothing is ever handed to a browser or another program.
  - Video plays through GStreamer: H.264/AAC in MP4 needs gstreamer1.0-libav, WebM and the rest gstreamer1.0-plugins-good (the package depends on both,
    so WebKit never asks the system to go and find a codec). WebKit gives GStreamer a lastbook:// video address as it is; lastbooksrc, our own
    source element in /opt/lastbook/gst, reads it from the library's video folders (packaging/linux/gst/lastbooksrc.c).
"""
import json
import os
import select
import sys
import threading
import time
import traceback

# Nothing outside may steer WebKit or GTK in the packaged app: WEBKIT_INSPECTOR_SERVER and its kin open a debugging port, GI_TYPELIB_PATH and
# GST_PLUGIN_PATH could swap in other code, GTK_MODULES loads it. Removed before any of them is imported (the Windows shell does the same for WebView2).
SCHEME_NAME = 'lastbook'
# Kept: WebKit's own switches for graphics drivers that draw badly with its default renderer. They change how pages are drawn and open nothing.
RENDERING_SWITCHES = {'WEBKIT_DISABLE_COMPOSITING_MODE', 'WEBKIT_DISABLE_DMABUF_RENDERER'}
if getattr(sys, 'frozen', False):
    for _k in [k for k in os.environ if k not in RENDERING_SWITCHES and k.startswith(('WEBKIT_', 'GI_TYPELIB', 'GST_PLUGIN', 'GST_REGISTRY', 'GTK_MODULES', 'GTK3_MODULES', 'GTK_DEBUG', 'GTK_PATH', 'GIO_EXTRA_MODULES', 'LD_PRELOAD', 'LD_LIBRARY_PATH'))]:
        os.environ.pop(_k, None)
    # Ours instead: lastbooksrc (packaging/linux/gst/lastbooksrc.c), the GStreamer source that plays lastbook://app/videos/ addresses. WebKit's player
    # takes only blob, data, file and http(s) unless WEBKIT_GST_ALLOWED_URI_PROTOCOLS names more, and GStreamer itself has no source for lastbook:.
    # The web process finds both through the environment it inherits from here.
    _gst = os.path.join(getattr(sys, '_MEIPASS', ''), 'gst')
    if os.path.isdir(_gst):
        os.environ['GST_PLUGIN_PATH_1_0'] = _gst
        os.environ['WEBKIT_GST_ALLOWED_URI_PROTOCOLS'] = SCHEME_NAME    # WebKit's player refuses any scheme but its own list and this one

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Soup', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gdk, Gio, GLib, Gtk, Soup, WebKit2   # noqa: E402

import bridge as B   # noqa: E402

APP_ID = 'org.lastbook.Lastbook'
SCHEME = 'lastbook'
HOME = B.ORIGIN_MAC + '/'
SIZE = (1280, 860)
MIN_SIZE = (640, 480)
STALL = 60                      # seconds without WebKit reading any of an answer before it is cancelled
MEMORY_LIMIT_MB = 4096          # the page's process; over it WebKit ends the page and the app starts again at home (as the Mac shell's MEMORY_LIMIT)
NO_NETWORK = json.dumps([{'trigger': {'url-filter': '.*'}, 'action': {'type': 'block'}}]
                        + [{'trigger': {'url-filter': '^' + scheme + ':'}, 'action': {'type': 'ignore-previous-rules'}} for scheme in (SCHEME, 'data', 'blob', 'about')])
NO_NETWORK_REFUSED = 'ARK kunne ikke slå netværksadgangen fra, så den åbner ikke noget.'
FEATURES_OFF = ('PeerConnectionEnabled', 'NavigationAPIEnabled', 'LinkPreconnect', 'LinkPreconnectEarlyHintsEnabled', 'LinkDNSPrefetchEnabled',
                'LinkPrefetchEnabled', 'SpeculationRulesPrefetchEnabled', 'WebSocketEnabled', 'WebTransportEnabled', 'BeaconAPIEnabled', 'PushAPIEnabled',
                'PrivateClickMeasurementEnabled', 'SafeBrowsingEnabled')
MENU_KEEP = {getattr(WebKit2.ContextMenuAction, n) for n in ('COPY', 'CUT', 'PASTE', 'SELECT_ALL', 'COPY_IMAGE_TO_CLIPBOARD', 'PASTE_AS_PLAIN_TEXT') if hasattr(WebKit2.ContextMenuAction, n)}
STOPPED = 'A page stopped working, so it was closed.'
TOO_BIG = 'A page was using too much memory, so it was closed.'
GO = [('Videoer', '#/videos'), ('Kort', '#/map'), ('Chat', '#/ask')]
SIDE_TOGGLE = "document.getElementById('sideBtn').click()"
FIND = "document.dispatchEvent(new KeyboardEvent('keydown',{key:'f',code:'KeyF',metaKey:true}))"   # the page's own find, as the Mac menu asks for it
_gtk_ok = None


def _gtk():
    """True once GTK has a display. Without one (a terminal with no desktop), dialogs fall back to the terminal."""
    global _gtk_ok
    if _gtk_ok is None:
        GLib.set_prgname('lastbook')
        GLib.set_application_name('ARK')
        try:
            r = Gtk.init_check(None)
            _gtk_ok = bool(r[0] if isinstance(r, tuple) else r)
        except Exception:
            _gtk_ok = False
    return _gtk_ok


def _icon(window):
    try:
        window.set_icon_name('lastbook')
    except Exception:
        pass


def show_error(message):
    """Before there is a window, or instead of one."""
    if not _gtk():
        return
    d = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK, text='ARK')
    d.format_secondary_text(message)
    _icon(d)
    d.run()
    d.destroy()
    _flush()


def _flush():
    while Gtk.events_pending():
        Gtk.main_iteration()


def _chooser(title, parent, files, start):
    action = Gtk.FileChooserAction.OPEN if files else Gtk.FileChooserAction.SELECT_FOLDER
    d = Gtk.FileChooserNative.new(title, parent, action, 'Vælg', 'Annuller')
    d.set_modal(True)
    d.set_select_multiple(False)
    d.set_local_only(True)
    if start and os.path.isdir(start):
        d.set_current_folder(str(start))
    try:
        if d.run() == Gtk.ResponseType.ACCEPT:
            return d.get_filename() or ''
        return ''
    finally:
        d.destroy()
        _flush()


def choose_folder(title, message):
    """Before there is a window: the chosen folder's path, '' if the picker was cancelled, or None if they quit."""
    if not _gtk():
        return None
    d = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE, text=title)
    d.format_secondary_text(message)
    d.add_buttons('Afslut', Gtk.ResponseType.CANCEL, 'Vælg mappe…', Gtk.ResponseType.ACCEPT)
    d.set_default_response(Gtk.ResponseType.ACCEPT)
    _icon(d)
    r = d.run()
    d.destroy()
    _flush()
    if r != Gtk.ResponseType.ACCEPT:
        return None
    return _chooser('Vælg mappen, der hedder LastbookData', None, False, os.path.expanduser('~/Downloads'))


def pick(files, start):
    """For setup's Choose… buttons. Called on the main thread (main.pick_folder posts it here)."""
    parent = SHELL.window if SHELL else None
    return _chooser('Vælg en biblioteks-zip eller modelfil' if files else 'Vælg en mappe', parent, files, start) or None


# --------------------------------------------------------------------------------------------------------- one copy only --
_lock_file = None


def claim(lock_dir):
    """Called first thing. Returns False when Lastbook is already open: that copy's window has then been brought to the front.
    With a session bus, GtkApplication's unique name does it; without one, a lock file in this person's app folder stands in."""
    global APP, _lock_file
    reap_inherited()
    if not _gtk():
        return True
    APP = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS if hasattr(Gio.ApplicationFlags, 'DEFAULT_FLAGS') else Gio.ApplicationFlags.FLAGS_NONE)
    try:
        APP.register(None)
        if APP.get_is_remote():
            APP.activate()      # the copy already open raises its window
            return False
    except GLib.Error as e:
        print('no session bus for one copy only:', e.message, flush=True)
    try:
        import fcntl
        os.makedirs(lock_dir, exist_ok=True)
        _lock_file = open(os.path.join(lock_dir, 'lastbook.lock'), 'w')
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        show_error('ARK er allerede åben.')
        return False
    return True


APP = None
SHELL = None


# --------------------------------------------------------------------------------------------------------- the scheme --
class _Task(B.Sink):
    """One lastbook:// request. Its answer goes to WebKit through a pipe: WebKit reads the read end as the body, the bridge's worker writes the
    other. A write waits while WebKit has not read what came before, so a page that stops reading stops the engine (the Mac shell's acks do this).
    When WebKit gives up on the request it lets go of the stream, which closes its end; the next write fails and the exchange is cancelled.
    For that, nothing here keeps the request once its answer has begun: holding it would hold the stream, and the writer would wait forever.
    A request WebKit has not read from for STALL seconds is cancelled too, and the log says so."""

    def __init__(self, req):
        self.req = req
        self.ex = None
        self.stopped = False
        self.r, self.w = os.pipe2(os.O_CLOEXEC)
        os.set_blocking(self.w, False)
        self.wlock = threading.Lock()     # self.w is written and closed under it: a closed number can be reused for another file at once

    def head(self, status, reason, headers):
        if threading.current_thread() is threading.main_thread():
            self._head(status, reason, headers)
        else:
            GLib.idle_add(self._head, status, reason, headers)

    def _head(self, status, reason, headers):
        try:
            stream = Gio.UnixInputStream.new(self.r, True)
            length = -1
            h = Soup.MessageHeaders.new(Soup.MessageHeadersType.RESPONSE)
            ctype = None
            for k, v in headers:
                if k.lower() == 'content-length':
                    try:
                        length = int(v)
                    except ValueError:
                        pass
                if k.lower() == 'content-type':
                    ctype = v
                h.append(k, v)
            res = WebKit2.URISchemeResponse.new(stream, length)
            res.set_status(int(status), reason or None)
            res.set_http_headers(h)
            if ctype:
                res.set_content_type(ctype)
            req, self.req = self.req, None
            req.finish_with_response(res)
        except Exception:
            traceback.print_exc()
            self.stop()
        return False

    def data(self, chunk, ack):
        try:
            view = memoryview(chunk)
            last = time.monotonic()
            while view and not self.stopped:
                with self.wlock:
                    if self.w is None:
                        break
                    try:
                        n = os.write(self.w, view)
                    except BlockingIOError:
                        n = 0
                    fd = self.w
                if n:
                    view, last = view[n:], time.monotonic()
                    continue
                if time.monotonic() - last > STALL:
                    print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} a page stopped reading an answer for {STALL} s: cancelled', flush=True)
                    self.stop()
                    break
                try:
                    select.select([], [fd], [], 0.5)    # until WebKit has read some; if the number was closed meanwhile, the loop sees it
                except (OSError, ValueError):
                    pass
        except OSError:      # WebKit closed its end: it no longer wants this answer
            self.stop()
        finally:
            ack()

    def finish(self, ok):
        with self.wlock:
            if self.w is not None:
                os.close(self.w)
                self.w = None

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.ex:
            self.ex.cancel()
        self.finish(False)


def _read_body(req):
    """A POST's body, read no further than the most the bridge would take: past that it refuses by length alone."""
    try:
        stream = req.get_http_body()
    except AttributeError:      # WebKitGTK before 2.40
        return None
    if stream is None:
        return None
    out = bytearray()
    try:
        while len(out) <= B.MAX_POST:
            piece = stream.read_bytes(65536, None)
            if not piece or piece.get_size() == 0:
                break
            out += piece.get_data()
    finally:
        try:
            stream.close(None)
        except GLib.Error:
            pass
    return bytes(out[:B.MAX_POST + 1])


def _headers(req):
    out = []
    try:
        h = req.get_http_headers()
        if h is not None:
            h.foreach(lambda k, v: out.append((k, v)))
    except Exception:
        traceback.print_exc()
    return out


# ---------------------------------------------------------------------------------------------------------- the window --
class Shell:
    def __init__(self, bridge, app_dirs):
        self.bridge = bridge
        self.data_dir, self.cache_dir = app_dirs
        self.window = self.view = None
        self.network_off = False
        self.ended_why = None
        self.actions = {}      # the last navigation request per address: (type, gesture), for the response that follows it

    def build(self, app):
        mem = None
        try:
            mem = WebKit2.MemoryPressureSettings.new()
            mem.set_memory_limit(MEMORY_LIMIT_MB)
        except Exception:
            traceback.print_exc()
        manager = WebKit2.WebsiteDataManager(base_data_directory=os.path.join(self.data_dir, 'webkit'), base_cache_directory=os.path.join(self.cache_dir, 'webkit'))
        try:
            manager.set_itp_enabled(False)
        except Exception:
            pass
        kw = {'website_data_manager': manager}
        if mem is not None:
            kw['memory_pressure_settings'] = mem
        self.ctx = WebKit2.WebContext(**kw)
        self.ctx.set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)
        self.ctx.set_spell_checking_enabled(False)
        sec = self.ctx.get_security_manager()
        sec.register_uri_scheme_as_secure(SCHEME)
        sec.register_uri_scheme_as_cors_enabled(SCHEME)
        self.ctx.register_uri_scheme(SCHEME, self.on_scheme)
        self.ctx.connect('download-started', self.on_download)

        self.ucm = WebKit2.UserContentManager()
        self.ucm.add_script(WebKit2.UserScript.new(self.bridge.top_script(), WebKit2.UserContentInjectedFrames.TOP_FRAME,
                                                   WebKit2.UserScriptInjectionTime.START, None, None))
        s = WebKit2.Settings()
        for name, value in (('enable_javascript', True), ('javascript_can_open_windows_automatically', False), ('enable_developer_extras', False),
                            ('enable_dns_prefetching', False), ('enable_hyperlink_auditing', False), ('enable_webrtc', False),
                            ('enable_media_stream', False), ('enable_encrypted_media', False), ('enable_fullscreen', True),
                            ('enable_mediasource', True), ('enable_back_forward_navigation_gestures', False),
                            ('allow_file_access_from_file_urls', False), ('allow_universal_access_from_file_urls', False),
                            ('enable_write_console_messages_to_stdout', False), ('enable_page_cache', False), ('enable_tabs_to_links', False)):
            try:
                s.set_property(name.replace('_', '-'), value)
            except Exception:
                pass
        try:
            # No accelerated compositing. With it, on software GL (virtual machines, computers without a working GPU driver), a frame that opens at
            # its #page=N is drawn only down to its first few hundred pixels (Whole PDF at a later page). The app's pages are still: nothing needs it.
            s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
        except Exception:
            traceback.print_exc()
        try:
            features = WebKit2.Settings.get_all_features() if hasattr(WebKit2.Settings, 'get_all_features') else None
            for f in (features.get(i) for i in range(features.get_length())) if features else ():
                if f.get_identifier() in FEATURES_OFF:
                    s.set_feature_enabled(f, False)
        except Exception:
            traceback.print_exc()

        self.view = WebKit2.WebView(web_context=self.ctx, user_content_manager=self.ucm, settings=s)
        self.view.connect('decide-policy', self.on_policy)
        self.view.connect('create', self.on_create)
        self.view.connect('context-menu', self.on_context_menu)
        self.view.connect('permission-request', lambda v, r: (r.deny(), True)[1])
        self.view.connect('web-process-terminated', self.on_terminated)
        self.view.connect('load-failed', lambda *a: True)   # no WebKit error page: it can name addresses the app refused
        try:
            self.view.set_background_color(Gdk.RGBA(1, 1, 1, 1))
        except Exception:
            pass

        w = self.window = Gtk.ApplicationWindow(application=app, title='ARK')
        w.set_default_size(*SIZE)
        w.set_size_request(*MIN_SIZE)
        _icon(w)
        w.set_titlebar(self.header())
        w.add(self.view)
        w.connect('delete-event', lambda *a: (quit_app(), True)[1])
        self.keys(w)
        self.switch_off_network()

    # ---- header and keys: the Mac menu's items
    def header(self):
        bar = Gtk.HeaderBar(title='ARK', show_close_button=True)
        menu = Gio.Menu()
        go = Gio.Menu()
        for i, (name, _) in enumerate(GO):
            go.append(name, f'win.go{i}')
        menu.append_section(None, go)
        view = Gio.Menu()
        view.append('Vis eller skjul sidepanel', 'win.sidebar')
        view.append('Find…', 'win.find')
        view.append('Fuld skærm', 'win.fullscreen')
        menu.append_section(None, view)
        helps = Gio.Menu()
        helps.append('Hjælp til ARK', 'win.help')
        helps.append('Søg efter opdateringer…', 'win.updates')
        helps.append('Afslut ARK', 'win.quit')
        menu.append_section(None, helps)
        btn = Gtk.MenuButton(menu_model=menu, image=Gtk.Image.new_from_icon_name('open-menu-symbolic', Gtk.IconSize.BUTTON))
        btn.set_tooltip_text('Menu')
        bar.pack_end(btn)
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        nav.get_style_context().add_class('linked')
        for icon, tip, act in (('go-previous-symbolic', 'Tilbage', 'win.back'), ('go-next-symbolic', 'Frem', 'win.forward')):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.set_action_name(act)
            nav.pack_start(b, False, False, 0)
        bar.pack_start(nav)
        return bar

    def keys(self, w):
        acts = [(f'go{i}', (lambda r: lambda *a: self.js('location.hash=' + json.dumps(r)))(route), [f'<Primary>{i + 1}']) for i, (_, route) in enumerate(GO)]
        acts += [('sidebar', lambda *a: self.js(SIDE_TOGGLE), ['<Primary><Shift>s']),
                 ('find', lambda *a: self.js(FIND), ['<Primary>f']),
                 ('fullscreen', lambda *a: self.toggle_fullscreen(), ['F11']),
                 ('help', lambda *a: self.js("location.hash='#/about'"), ['F1']),
                 ('updates', lambda *a: self.js('window.LBUpdate && LBUpdate.fromMenu()'), []),
                 ('back', lambda *a: self.view.go_back(), ['<Alt>Left']),
                 ('forward', lambda *a: self.view.go_forward(), ['<Alt>Right']),
                 ('quit', lambda *a: quit_app(), ['<Primary>q']),
                 ('close', lambda *a: quit_app(), ['<Primary>w'])]
        app = w.get_application()
        for name, fn, accels in acts:
            a = Gio.SimpleAction.new(name, None)
            a.connect('activate', fn)
            w.add_action(a)
            if accels and app is not None:
                app.set_accels_for_action('win.' + name, accels)
        self.fullscreen = False
        w.connect('window-state-event', lambda win, ev: setattr(self, 'fullscreen', bool(ev.new_window_state & Gdk.WindowState.FULLSCREEN)) or False)

    def toggle_fullscreen(self):
        (self.window.unfullscreen if self.fullscreen else self.window.fullscreen)()

    def js(self, source):
        try:
            if hasattr(self.view, 'evaluate_javascript'):
                self.view.evaluate_javascript(source, -1, None, None, None, None, None)
            else:
                self.view.run_javascript(source, None, None, None)
        except Exception:
            traceback.print_exc()

    # ---- no network
    def switch_off_network(self):
        """Compiling takes a moment; the first page waits for it."""
        store = WebKit2.UserContentFilterStore.new(os.path.join(self.cache_dir, 'content-filters'))

        def saved(st, result):
            try:
                rules = st.save_finish(result)
            except GLib.Error as e:
                print('no network rule list:', e.message, flush=True)
                rules = None
            if rules is None:
                show_error(NO_NETWORK_REFUSED)
                return quit_app()
            self.ucm.add_filter(rules)
            self.network_off = True
            self.go_home()

        store.save('lastbook-no-network', GLib.Bytes.new(NO_NETWORK.encode()), None, saved)

    def go_home(self):
        if self.network_off:
            self.view.load_uri(HOME)

    # ---- the scheme
    def on_scheme(self, req):
        try:
            if req.get_web_view() is not self.view:
                req.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), 'refused', Gio.IOErrorEnum.PERMISSION_DENIED))
                return
            t = _Task(req)
            method = req.get_http_method() if hasattr(req, 'get_http_method') else 'GET'
            method = str(method or 'GET')
            headers = _headers(req)
            body = _read_body(req) if method == 'POST' and self.bridge.has_key(headers) else None   # with no key it is refused unread
            t.ex = self.bridge.start(method, str(req.get_uri()), headers, body, t)
        except Exception:
            traceback.print_exc()

    # ---- what may load where
    def on_policy(self, view, decision, kind):
        try:
            if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
                return self.decide_navigation(decision)
            if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                a = decision.get_navigation_action()
                self.leave(a.get_request().get_uri(), a.is_user_gesture())
                decision.ignore()
                return True
            if kind == WebKit2.PolicyDecisionType.RESPONSE:
                return self.decide_response(decision)
        except Exception:
            traceback.print_exc()
        decision.ignore()
        return True

    def decide_navigation(self, decision):
        a = decision.get_navigation_action()
        url = str(a.get_request().get_uri())
        kind = a.get_navigation_type()
        gesture = bool(a.is_user_gesture())
        self.actions[url] = (kind, gesture)
        if len(self.actions) > 64:
            self.actions.pop(next(iter(self.actions)))
        if self.bridge.top_url_ok(url):
            decision.use()
        elif url.startswith('blob:' + B.ORIGIN_MAC + '/') and gesture:
            # The chat's "save this conversation" (<a download>): WebKit turns this into a download (on_download asks where). Shown as a page,
            # it would be the top level showing something other than the app, which decide_response refuses.
            decision.use()
        elif self.bridge.frame_url_ok(url):
            # A frame, or the top level following a link; the response says which (decide_response). A frame's first load is the app's own
            # (the UI sets every frame's address by script); a click inside a saved page in a frame earns nothing.
            if kind not in (WebKit2.NavigationType.LINK_CLICKED, WebKit2.NavigationType.FORM_SUBMITTED, WebKit2.NavigationType.FORM_RESUBMITTED):
                self.bridge.give_ticket(url)
            decision.use()
        else:
            if kind == WebKit2.NavigationType.LINK_CLICKED:   # a plain link in the UI to the web: nothing opens, as on the Mac
                self.leave(url, gesture)
            decision.ignore()
        return True

    def decide_response(self, decision):
        url = str(decision.get_request().get_uri())
        main = decision.is_main_frame_main_resource() if hasattr(decision, 'is_main_frame_main_resource') else None
        if main is None:      # WebKitGTK before 2.40 cannot say: only the app's own page passes
            ok = self.bridge.top_url_ok(url)
        elif main:
            ok = self.bridge.top_url_ok(url)
            if not ok:
                kind, gesture = self.actions.get(url, (None, False))
                if kind == WebKit2.NavigationType.LINK_CLICKED:
                    self.leave(url, gesture)
        else:
            ok = self.bridge.frame_url_ok(url)
        if ok and not decision.is_mime_type_supported() and not url.startswith('blob:'):
            ok = False      # nothing is saved without the person choosing to: only the chat's own blob: download is
        (decision.use if ok else decision.ignore)()
        return True

    def leave(self, url, gesture):
        """The one window stays. Saved pages cannot ask for windows or the top level (their sandbox), so a request here is the app's own page's."""
        what, where = self.bridge.new_window(str(url), True, gesture)
        if what == 'inside':
            self.js('location.hash=' + json.dumps(where[len(HOME):] or '#/'))

    def on_create(self, view, action):
        self.leave(action.get_request().get_uri(), action.is_user_gesture())
        return None

    def on_context_menu(self, view, menu, event, hit):
        for item in list(menu.get_items()):
            if item.is_separator():
                continue
            if item.get_stock_action() not in MENU_KEEP:
                menu.remove(item)
        items = menu.get_items()
        for i in range(len(items) - 1, -1, -1):     # separators with nothing left between them
            if items[i].is_separator() and (i == 0 or i == len(items) - 1 or items[i - 1].is_separator()):
                menu.remove(items[i])
                items = menu.get_items()
        return menu.get_n_items() == 0

    # ---- downloads: the chat's "save this conversation" only
    def on_download(self, ctx, download):
        url = str(download.get_request().get_uri())
        if download.get_web_view() is not self.view or not url.startswith('blob:' + B.ORIGIN_MAC + '/'):
            download.cancel()
            return
        download.connect('decide-destination', self.on_destination)

    def on_destination(self, download, name):
        d = Gtk.FileChooserNative.new('Save', self.window, Gtk.FileChooserAction.SAVE, 'Save', 'Cancel')
        d.set_do_overwrite_confirmation(True)
        d.set_current_name(name or 'conversation.txt')
        d.set_current_folder(os.path.expanduser('~'))
        try:
            if d.run() == Gtk.ResponseType.ACCEPT and d.get_filename():
                download.set_allow_overwrite(True)
                download.set_destination(GLib.filename_to_uri(d.get_filename(), None))
            else:
                download.cancel()
        finally:
            d.destroy()
        return True

    # ---- the page's process
    def on_terminated(self, view, reason):
        if _restarting:     # before_exec ended it: starting a new one now would only leave it behind
            return
        why = TOO_BIG if reason == getattr(WebKit2.WebProcessTerminationReason, 'EXCEEDED_MEMORY_LIMIT', -1) else STOPPED
        if reason == getattr(WebKit2.WebProcessTerminationReason, 'TERMINATED_BY_API', -1):
            return
        print(f'the page process ended: {reason}', flush=True)
        self.go_home()
        d = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK, text=why)
        d.connect('response', lambda d, r: d.destroy())
        d.show()


_quitting = False


_restarting = False


def _children():
    """This program's child processes, from /proc."""
    me, kids = os.getpid(), []
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:
                with open(f'/proc/{d}/stat', 'rb') as f:
                    st = f.read().decode('utf-8', 'replace')
                if int(st[st.rindex(')') + 2:].split()[1]) == me:
                    kids.append(int(d))
            except (OSError, ValueError):
                pass
    return kids


def before_exec():
    """Setup's restart replaces this program in place (main.relaunch). WebKit's processes are its children: they are ended first, and the
    page's crash handler is told not to start new ones meanwhile. Any that are still there when the new program starts, it collects (reap_inherited)."""
    global _restarting
    _restarting = True
    import signal
    for pid in _children():
        try:
            with open(f'/proc/{pid}/comm') as f:
                if not f.read().startswith('WebKit'):
                    continue
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        for _ in range(50):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
            except ChildProcessError:
                break
            time.sleep(0.02)


def reap_inherited():
    """Called first thing. Any child this program has now was left by the program it replaced (setup's restart): nothing here has started one yet.
    Each is waited for on a thread of its own, so it does not stay behind as a zombie when it ends."""
    def wait(pid):
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    for pid in _children():
        threading.Thread(target=wait, args=(pid,), daemon=True, name=f'reap-{pid}').start()


def quit_app():
    """Close the window and stop the local AI's runner before leaving (it would also end when its pipe closes)."""
    global _quitting
    if _quitting:
        return
    _quitting = True
    print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} window closed', flush=True)
    try:
        if SHELL and SHELL.window:
            SHELL.window.hide()
        _flush()
    except Exception:
        pass

    def stop_ai():
        assistant = sys.modules.get('assistant')
        local = getattr(assistant, 'LOCAL', None)
        if local is not None:
            try:
                local.stop('ARK is closing')
            except Exception:
                traceback.print_exc()
    t = threading.Thread(target=stop_ai, daemon=True, name='stop-ai')
    t.start()
    t.join(5)
    sys.stdout.flush()
    os._exit(0)     # the engine's threads end here, as on the other systems


def run(bridge, app_dirs):
    """Show the window and stay here until it closes."""
    global SHELL, APP
    if not _gtk():
        raise RuntimeError('ARK skal bruge et skrivebord for at åbne sit vindue (der blev ikke fundet en skærm).')
    if APP is None:
        APP = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    SHELL = Shell(bridge, app_dirs)

    def activate(app):
        if SHELL.window is None:
            SHELL.build(app)
            SHELL.window.show_all()
        SHELL.window.present()

    APP.connect('activate', activate)
    APP.run([sys.argv[0]])
    quit_app()
