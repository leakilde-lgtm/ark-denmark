"""The one way out: downloads for setup. This is the only module in Lastbook that opens a network connection.

  - HTTPS only, to the hosts in ALLOWED_HOSTS (or a subdomain of ALLOWED_SUFFIXES), port 443.
  - No proxy: http.client never reads the system's or the environment's proxy settings (urllib would), so a request goes
    straight to the host named, or nowhere.
  - A redirect is followed only to another allowed address, at most MAX_REDIRECTS times.
  - Resuming uses HTTP Range on the ETag saved beside the partial file. R2 ignores If-Range (it answers 206 whatever the ETag),
    so the check is made here: another ETag on the 206, a 200 instead, or another total size starts that file over.
    (Not If-Match: on a redirect chain the first host's ETag is not the file's, so it would refuse every resume.)
  - The SHA-256 is computed while the bytes arrive. After a resume the bytes already on disk are read once to rebuild it.
  - Nothing here runs unless the person started or resumed a download (or opened setup: one GET of the manifest).

Tests pass their own `allowed` and `context` to reach a local test server. The product never does.
"""
import errno
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path

ALLOWED_HOSTS = frozenset({
    'downloads.lastbook.org',
    'huggingface.co',
    'cas-bridge.xethub.hf.co',
})
ALLOWED_SUFFIXES = ('.hf.co',)      # Hugging Face's download hosts (cas-bridge.xethub.hf.co and its siblings)
MAX_REDIRECTS = 5
TIMEOUT = 30                        # seconds without a byte before the connection counts as broken
CHUNK = 1024 * 1024
SAVE_EVERY = 2.0                    # seconds between saves of the partial file's .json
NETWORK_TRIES = 6                   # broken connections in a row, with no progress between them, before giving up
BACKOFF = (2, 4, 8, 15, 30, 30)
USER_AGENT = 'Lastbook-setup/1'


class Stopped(Exception):
    """The person paused or cancelled. .why is 'paused' or 'cancelled'."""

    def __init__(self, why):
        super().__init__(why)
        self.why = why


class Failed(Exception):
    """A plain sentence for the person, and a code from docs/SETUP-API.md."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class BadChecksum(Failed):
    def __init__(self, message):
        super().__init__('checksum', message)


def disk_full(e):
    return isinstance(e, OSError) and (e.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1)) or getattr(e, 'winerror', None) in (112, 39))


def host_allowed(host, allowed=None):
    host = (host or '').lower().rstrip('.')
    if allowed is not None:
        return host in allowed
    return host in ALLOWED_HOSTS or any(host.endswith(s) and len(host) > len(s) for s in ALLOWED_SUFFIXES)


def url_allowed(url, allowed=None):
    """An https address on an allowed host, with no user name or password in it, on port 443 (a test's own hosts: any port)."""
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return False
    if u.scheme != 'https' or not u.hostname or u.username or u.password:
        return False
    if allowed is None and port not in (None, 443):
        return False
    return host_allowed(u.hostname, allowed)


def roots_path():
    """engine/setup/roots.pem, beside this file (in the packaged app, where the packaging puts it: engine/setup/ under _MEIPASS)."""
    here = Path(__file__).with_name('roots.pem')
    if not here.is_file() and getattr(sys, 'frozen', False):
        here = Path(getattr(sys, '_MEIPASS', '')) / 'engine' / 'setup' / 'roots.pem'
    return here


def tls_context():
    """TLS 1.2 or later, hostname checked, and trust in the roots of roots.pem only: never the system's store (see
    packaging/setup_roots.py for why and which). With the file missing, nothing is downloaded."""
    roots = roots_path()
    if not roots.is_file():
        raise Failed('network', 'Denne udgave af ARK mangler listen over godkendte downloadservere, så den kan ikke hente. '
                                'Hent ARK igen.')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # check_hostname on and CERT_REQUIRED; no default certificates loaded
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=str(roots))
    return ctx


class Control:
    """Pause and cancel, from any thread. The download notices within one chunk or one second of waiting."""

    def __init__(self):
        self.ev = threading.Event()
        self.why = None

    def stop(self, why):
        if self.why != 'cancelled':
            self.why = why
        self.ev.set()

    def check(self):
        if self.ev.is_set():
            raise Stopped(self.why or 'paused')

    def sleep(self, s):
        if self.ev.wait(s):
            raise Stopped(self.why or 'paused')


def _open(url, start, etag, allowed, context, timeout):
    """(connection, response, final url) for a GET from byte `start`, after following allowed redirects."""
    for _ in range(MAX_REDIRECTS + 1):
        if not url_allowed(url, allowed):
            raise Failed('network', 'ARK afviste at hente fra en adresse, den ikke kender.')
        u = urllib.parse.urlsplit(url)
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=context)
        headers = {'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'}
        if start:
            headers['Range'] = f'bytes={start}-'
            if etag:
                headers['If-Range'] = etag
        conn.request('GET', (u.path or '/') + ('?' + u.query if u.query else ''), headers=headers)
        r = conn.getresponse()
        if r.status in (301, 302, 303, 307, 308):
            loc = r.getheader('Location')
            r.close()
            conn.close()
            if not loc:
                raise Failed('network', 'Downloadserveren svarede med en flytning til ingen steder.')
            url = urllib.parse.urljoin(url, loc)
            continue
        return conn, r, url
    raise Failed('network', 'Downloadserveren sendte ARK rundt i ring.')


def fetch_small(url, limit=1 << 20, allowed=None, context=None, timeout=10):
    """One small GET (the manifest). Bytes, or None on any failure."""
    try:
        conn, r, _ = _open(url, 0, None, allowed, context or tls_context(), timeout)
        try:
            if r.status != 200:
                return None
            data = r.read(limit + 1)
            return data if len(data) <= limit else None
        finally:
            conn.close()
    except (OSError, http.client.HTTPException, Failed, ValueError):
        return None


def _load_meta(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_meta(path, d):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f)
    os.replace(tmp, path)


def _content_range(v):
    """(start, total) from 'bytes 100-199/1000', or None."""
    try:
        unit, _, rest = (v or '').partition(' ')
        span, _, total = rest.partition('/')
        return int(span.split('-')[0]), int(total)
    except ValueError:
        return None


class Download:
    """One file: <dest>.part grows, with <dest>.part.json beside it (url, size, sha256, bytes done, ETag).
    run() returns once the whole file is there and its SHA-256 matches, renamed to <dest>. Raises Stopped, Failed or BadChecksum.
    progress(done, total, phase) is called as bytes arrive; phase is 'downloading' or 'verifying'."""

    def __init__(self, url, dest, size, sha256, control, progress=lambda d, t, p: None, allowed=None, context=None, timeout=TIMEOUT):
        self.url, self.dest, self.size, self.sha256 = url, str(dest), int(size), sha256.lower()
        self.part, self.meta = self.dest + '.part', self.dest + '.part.json'
        self.control, self.progress = control, progress
        self.allowed, self.context, self.timeout = allowed, context, timeout

    def done_bytes(self):
        try:
            return min(os.path.getsize(self.part), self.size)
        except OSError:
            return 0

    def discard(self):
        for p in (self.part, self.meta, self.meta + '.tmp'):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _rehash(self, n):
        """The SHA-256 of the first n bytes already on disk."""
        h = hashlib.sha256()
        done = 0
        with open(self.part, 'rb') as f:
            while done < n:
                self.control.check()
                b = f.read(min(8 * CHUNK, n - done))
                if not b:
                    break
                h.update(b)
                done += len(b)
                self.progress(done, n, 'verifying')
        return h, done

    def run(self):
        if os.path.isfile(self.dest) and os.path.getsize(self.dest) == self.size and not os.path.exists(self.part):
            return self.dest     # finished and checked before a restart
        os.makedirs(os.path.dirname(self.dest) or '.', exist_ok=True)
        meta = _load_meta(self.meta)
        if meta and (meta.get('sha256') != self.sha256 or meta.get('size') != self.size):
            self.discard()       # left by another version of this file
            meta = {}
        ctx = self.context or tls_context()
        start = self.done_bytes()
        if start and not meta.get('etag'):
            start = 0            # nothing to tell the server which file those bytes came from
        with open(self.part, 'ab') as f:
            f.truncate(start)
        h, start = self._rehash(start) if start else (hashlib.sha256(), 0)
        etag = meta.get('etag')
        tries = 0
        ignored_once = False
        while start < self.size:
            self.control.check()
            try:
                got = self._fetch(start, etag, h, ctx)
                start, etag = got
                tries = 0
            except Stopped:
                raise
            except Failed:
                raise
            except _Restart as r:
                if r.whole and not ignored_once:      # one 200 to a Range can be a passing hiccup at the edge: ask once more first
                    ignored_once = True
                    print(f'setup: the server sent the whole of {os.path.basename(self.dest)} to a resume; asking again', flush=True)
                    self.control.sleep(1)
                    continue
                print(f'setup: the file on the server changed or ignores Range; starting {os.path.basename(self.dest)} over', flush=True)
                with open(self.part, 'r+b') as f:
                    f.truncate(0)
                h, start, etag = hashlib.sha256(), 0, None
                continue
            except ssl.SSLCertVerificationError:
                raise Failed('network', 'ARK kunne ikke bekræfte, at den taler med downloadserveren, så den stoppede. '
                                        'Et netværk, der afbryder sikre forbindelser (nogle kontorer og hoteller), kan være årsagen.')
            except (OSError, http.client.HTTPException, ValueError) as e:
                if disk_full(e):
                    raise Failed('no_space', 'Disken blev fyldt under hentningen. Frigør plads, og prøv igen.')
                now = self.done_bytes()     # h already holds every byte written: it is updated just after each write
                tries = 0 if now > start else tries + 1
                start = now
                etag = _load_meta(self.meta).get('etag') or etag
                if tries >= NETWORK_TRIES:
                    raise Failed('network', f'Forbindelsen blev ved med at ryge ({type(e).__name__}). Tjek internetforbindelsen, og prøv igen.')
                print(f'setup: download broke at {start} of {self.size}: {type(e).__name__}: {e}; trying again', flush=True)
                self.control.sleep(BACKOFF[min(tries, len(BACKOFF) - 1)])
        if h.hexdigest() != self.sha256:
            self.discard()
            raise BadChecksum('Den hentede fil matchede ikke sin udgivne kontrolsum.')
        os.replace(self.part, self.dest)
        try:
            os.unlink(self.meta)
        except OSError:
            pass
        return self.dest

    def _fetch(self, start, etag, h, ctx):
        """Reads until the end or a break. Returns (bytes done, etag). Raises _Restart when the server's file is not the one begun."""
        conn, r, final = _open(self.url, start, etag, self.allowed, ctx, self.timeout)
        try:
            if r.status == 416 or r.status >= 400:
                if 500 <= r.status < 600:
                    raise OSError(f'HTTP {r.status}')
                raise Failed('network', f'Downloadserveren afviste filen (HTTP {r.status}).')
            new_etag = r.getheader('ETag')
            if r.status == 206:
                cr = _content_range(r.getheader('Content-Range'))
                if not cr or cr[0] != start or cr[1] != self.size or (etag and new_etag and new_etag != etag):
                    raise _Restart(new_etag)
            elif r.status == 200:
                if start:
                    raise _Restart(new_etag, whole=True)    # the server sent the whole file again: its copy changed, or it ignores Range
                length = r.getheader('Content-Length')
                if length is not None and length.isdigit() and int(length) != self.size:
                    raise BadChecksum(f'The file on the server is {int(length):,} bytes, not the {self.size:,} Lastbook expects.')
            else:
                raise OSError(f'HTTP {r.status}')
            etag = new_etag or etag
            meta = {'url': self.url, 'size': self.size, 'sha256': self.sha256, 'etag': etag, 'bytes_done': start}
            _save_meta(self.meta, meta)
            saved = time.time()
            with open(self.part, 'r+b') as f:
                f.seek(start)
                while start < self.size:
                    self.control.check()
                    b = r.read1(min(CHUNK, self.size - start))    # what has arrived, up to CHUNK: progress and pause stay quick on a slow line
                    if not b:
                        raise ConnectionError('the connection closed early')
                    f.write(b)
                    h.update(b)
                    start += len(b)
                    self.progress(start, self.size, 'downloading')
                    if time.time() - saved > SAVE_EVERY:
                        f.flush()
                        meta['bytes_done'] = start
                        _save_meta(self.meta, meta)
                        saved = time.time()
                f.flush()
                os.fsync(f.fileno())
            meta['bytes_done'] = start
            _save_meta(self.meta, meta)
            return start, etag
        finally:
            try:
                conn.close()
            except OSError:
                pass


class _Restart(Exception):
    def __init__(self, etag, whole=False):
        super().__init__('restart')
        self.etag, self.whole = etag, whole
