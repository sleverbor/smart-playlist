"""Standard-library Spotify HTTP client and Authorization Code with PKCE."""
import base64
import hashlib
import math
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, HTTPServer
import webbrowser

API = 'https://api.spotify.com/v1'
SCOPES = 'playlist-read-private playlist-read-collaborative playlist-modify-private user-read-recently-played'
REDIRECT = 'http://127.0.0.1:8765/callback'


class SpotifyError(RuntimeError):
    def __init__(self, message, status=None):
        self.status = status
        super().__init__(message)


class PlaylistChanged(SpotifyError):
    pass


def save_json(path, value):
    """Atomic local replacement; credentials and listening state are owner-readable."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(tmp, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
    tmp.replace(path)


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default



def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def login(client_id, token_path, on_authorize=None, cancellation=None):
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = parsed.path == '/callback' and query.get('state') == [state]
            if valid:
                result.update(code=query.get('code', [None])[0], error=query.get('error', [None])[0])
            self.send_response(200 if valid else 400)
            self.end_headers()
            self.wfile.write(b'Authorization received. You can return to the terminal.' if valid else b'Invalid callback.')

    url = 'https://accounts.spotify.com/authorize?' + urlencode({
        'client_id': client_id, 'response_type': 'code', 'redirect_uri': REDIRECT,
        'scope': SCOPES, 'state': state, 'code_challenge_method': 'S256', 'code_challenge': challenge})
    with HTTPServer(('127.0.0.1', 8765), Callback) as server:
        server.timeout = 1
        if not on_authorize:
            print('Opening Spotify consent in your browser. Waiting up to five minutes.')
        if on_authorize:
            on_authorize(url)
        elif not webbrowser.open(url):
            print('Open this authorization URL locally:', url)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            if cancellation is not None and cancellation.is_set():
                raise SpotifyError("Spotify connection cancelled")
            server.handle_request()
    if not result.get('code'):
        raise SpotifyError('Authorization denied or timed out. Run login again.')
    token = token_request(token_path, client_id, {
        'grant_type': 'authorization_code', 'client_id': client_id,
        'code': result['code'], 'redirect_uri': REDIRECT, 'code_verifier': verifier})
    token.update(client_id=client_id, expires_at=time.time() + token['expires_in'])
    save_json(token_path, token)


class Client:
    def __init__(self, token_path, interval=None, retries=3, clock=None, sleep=None, store=None, progress=None, rate_path=None):
        self.store, self.progress = store, progress
        self.token_path = Path(token_path)
        self.token = read_json(token_path)
        if not self.token:
            raise SpotifyError('Run login first with SPOTIFY_CLIENT_ID set.')
        conservative = interval is None and 'SPOTIFY_REQUEST_INTERVAL' not in os.environ
        interval = float(os.environ.get('SPOTIFY_REQUEST_INTERVAL', '6')) if interval is None else interval
        self.metrics = dict(requests=0, retries=0, rate_limits=0, cache_hits=0, source_cache_hits=0, wait_seconds=0.0)
        self.gate = RateGate(Path(os.environ.get('SPOTIFY_RATE_STATE', str(rate_path or self.token_path.parent / 'rate.json'))),
                             interval, clock or time.time, sleep or time.sleep, self.metrics, progress=progress, conservative=conservative)
        if type(retries) is not int or not 0 <= retries <= 10:
            raise ValueError("Retries must be an integer between 0 and 10")
        self.retries = retries
        self.no_store_playlists = set()
        identity = hashlib.sha256((self.token['client_id'] + self.token.get('refresh_token', self.token['access_token'])).encode()).hexdigest()
        self.cache_scope = identity
        self.cache = self.token_path.parent / 'cache' / identity
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)

    def bind_account_cache(self, account, legacy_token=None, source_snapshots=None):
        """Call only after /me verifies the account and workspace binding."""
        stable=hashlib.sha256(('account-cache-v1:'+json.dumps([self.token['client_id'],account])).encode()).hexdigest()
        old_scopes={self.cache_scope}
        if legacy_token and legacy_token.get('client_id')==self.token['client_id']:
            old_scopes.add(hashlib.sha256((legacy_token['client_id']+legacy_token.get('refresh_token',legacy_token['access_token'])).encode()).hexdigest())
        new_cache=self.token_path.parent/'cache'/stable
        new_cache.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.store:
            with self.store.db:
                for scope in old_scopes:
                    if scope==stable:continue
                    # Copy complete source snapshots only; HTTP freshness and auth responses do not migrate.
                    self.store.db.execute("INSERT OR IGNORE INTO cache SELECT ?,name,playlist,payload,updated FROM cache WHERE scope=? AND name LIKE 'source-%'",(stable,scope))
                for playlist,snapshot in (source_snapshots or {}).items():
                    name='source-'+playlist+'.json'
                    candidates=self.store.db.execute('SELECT payload,updated FROM cache WHERE name=? AND playlist=? ORDER BY updated DESC',(name,playlist)).fetchall()
                    for payload,updated in candidates:
                        value=json.loads(payload)
                        if value.get('snapshot')==snapshot:
                            self.store.db.execute('INSERT OR IGNORE INTO cache VALUES(?,?,?,?,?)',(stable,name,playlist,payload,updated))
                            break
        else:
            for scope in old_scopes:
                for path in (self.token_path.parent/'cache'/scope).glob('source-*.json'):
                    target=new_cache/path.name
                    if not target.exists():save_json(target,read_json(path))
        self.cache_scope=stable
        self.cache=new_cache

    def access_token(self):
        if time.time() >= self.token['expires_at'] - 60:
            refreshed = token_request(self.token_path, self.token['client_id'], {
                'grant_type': 'refresh_token', 'refresh_token': self.token['refresh_token'],
                'client_id': self.token['client_id']}, progress=self.progress)
            self.token.update(refreshed)
            self.token['expires_at'] = time.time() + refreshed['expires_in']
            save_json(self.token_path, self.token)
        return self.token['access_token']

    def request(self, method, path, data=None):
        if path.startswith('https://'):
            if not path.startswith(API + '/'):
                raise SpotifyError('Unexpected pagination URL')
            url = path
        else:
            url = API + path
        if self.progress:
            self.progress.check()
        parsed = urlparse(url)
        parts = parsed.path.split('/')
        playlist = parts[3] if len(parts) > 3 and parts[2] == 'playlists' else None
        cacheable = method == 'GET' and '/me' not in parsed.path and not (playlist and len(parts) == 4)
        if method != 'GET' and playlist:
            self.invalidate_playlist(playlist)
        cache_directory = self.cache / ('playlist-' + playlist if playlist else 'http')
        cache_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_path = cache_directory / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        saved = self.cache_read(cache_path) if cacheable else None
        headers = {}
        if saved and saved.get('etag'):
            headers['If-None-Match'] = saved['etag']
        if saved and saved.get('expires', 0) > self.gate.clock():
            self.metrics['cache_hits'] += 1
            return saved['body']
        token = self.access_token()
        waited = 0.0
        for attempt in range(self.retries + 1):
            self.gate.acquire(method, parsed.path)
            self.metrics['requests'] += 1
            if self.progress:
                self.progress.emit('request',message='Contacting Spotify',method=method,endpoint='/'.join('playlist' if i==3 and playlist else part for i,part in enumerate(parts)),attempt=attempt+1)
            started=time.perf_counter()
            try:
                body, response_headers, status = http_json(method, url, data, token,
                    extra_headers=headers, details=True)
            except RateLimited as exc:
                self.metrics['rate_limits'] += 1
                delay = retry_delay(exc.retry_after, attempt)
                self.gate.defer(delay)
                self.gate.record(method,parsed.path,429,time.perf_counter()-started,exc.retry_after)
                if self.progress:
                    self.progress.emit('rate_limited',message='Spotify requested a cooldown',retry_after=exc.retry_after,retry_seconds=delay,method=method,endpoint=parsed.path,attempt=attempt+1)
                if attempt == self.retries or waited + delay > 120:
                    raise SpotifyError('Spotify rate limit persists; cooldown saved. Resume later.') from None
                waited += delay
                self.metrics['retries'] += 1
                continue
            except BaseException as exc:
                self.gate.record(method,parsed.path,getattr(exc,'status',None),time.perf_counter()-started)
                raise
            self.gate.record(method,parsed.path,status,time.perf_counter()-started)
            if status == 304:
                if not saved:
                    raise SpotifyError('304 without cached response; retry after clearing cache')
                self.metrics['cache_hits'] += 1
                body = saved['body']
            control = response_headers.get('Cache-Control', '')
            if playlist and 'no-store' in control:
                self.no_store_playlists.add(playlist)
            if cacheable:
                etag = response_headers.get('ETag', saved.get('etag') if status == 304 and saved else None)
                if 'no-store' in control or not etag:
                    self.cache_delete(cache_path)
                else:
                    age = next((int(v.split('=')[1]) for v in control.split(',')
                                if v.strip().startswith('max-age=') and v.split('=')[1].isdigit()), 0)
                    if 'no-cache' in control:
                        age = 0
                    try:
                        age = max(0, age - max(0, int(response_headers.get('Age', '0') or '0')))
                    except ValueError:
                        age = 0
                    self.cache_write(cache_path, dict(url=url, playlist=playlist, etag=etag, body=body,
                              expires=self.gate.clock() + age))
            return body

    def cache_read(self,path):
        return self.store.cache_read(self.cache_scope,str(Path(path).relative_to(self.cache))) if self.store else read_json(path)

    def cache_write(self,path,value):
        if self.store:
            self.store.cache_write(self.cache_scope,str(Path(path).relative_to(self.cache)),value)
        else:
            save_json(path,value)

    def cache_delete(self,path):
        if self.store:
            self.store.cache_delete(self.cache_scope,name=str(Path(path).relative_to(self.cache)))
        else:
            Path(path).unlink(missing_ok=True)

    def invalidate_playlist(self, identity):
        if self.store:
            self.store.cache_delete(self.cache_scope,playlist=identity)
        directory = self.cache / ('playlist-' + identity)
        if directory.exists():
            shutil.rmtree(directory)
        self.cache_delete(self.cache / ('source-' + identity + '.json'))

    def source_playlist(self, identity):
        before = self.request('GET', '/playlists/' + identity)
        path = self.cache / ('source-' + identity + '.json')
        saved = self.cache_read(path)
        if identity in self.no_store_playlists:
            self.cache_delete(path)
            saved = None
        if saved and saved['snapshot'] == before['snapshot_id']:
            self.metrics['source_cache_hits'] += 1
            return before, saved['rows']
        metadata, rows = self.playlist(identity)
        if identity not in self.no_store_playlists:
            self.cache_write(path, dict(playlist=identity, snapshot=metadata['snapshot_id'], rows=rows))
        return metadata, rows

    def pages(self, path):
        while path:
            page = self.request('GET', path)
            yield from page['items']
            path = page.get('next')

    def playlist(self, identity):
        """Read items between snapshot checks; reject concurrent changes."""
        path = '/playlists/' + identity
        for attempt in range(3):
            before = self.request('GET', path)
            if hasattr(self, 'cache'):
                self.invalidate_playlist(identity)
            rows = list(self.pages(path + '/items?limit=50'))
            after = self.request('GET', path)
            if before['snapshot_id'] == after['snapshot_id']:
                return after, rows
        raise PlaylistChanged('Playlist is still changing; saved setup can be continued later.')

    def playlist_saved(self, identity):
        # The playlist listing uses the account's existing playlist-read scopes.
        # A complete fresh traversal distinguishes removal from an accessible URL.
        return any(row.get('id')==identity for row in self.pages('/me/playlists?limit=50'))

    def recent(self):
        return self.request('GET', '/me/player/recently-played?limit=50')['items']

    def write_batch(self, identity, uris, append=False):
        if len(uris)>100:raise ValueError('Spotify write batches must contain at most 100 tracks')
        return self.request('POST' if append else 'PUT','/playlists/'+identity+'/items',{'uris':uris})

    def replace(self, identity, uris):
        path = '/playlists/' + identity + '/items'
        self.request('PUT', path, {'uris': uris[:100]})
        for start in range(100, len(uris), 100):
            self.request('POST', path, {'uris': uris[start:start + 100]})



class RateLimited(SpotifyError):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__('Spotify HTTP 429')


def retry_delay(header, attempt):
    try:
        delay = float(header)
        if math.isfinite(delay) and delay >= 0:
            return max(1.0, delay)
    except (ValueError, TypeError):
        pass
    return min(60.0, 2.0 ** (attempt + 1))


class RateGate:
    """Shared-file pacing/cooldown; lock serializes local request reservations."""
    def __init__(self, path, interval, clock, sleep, metrics, progress=None, conservative=False):
        if not math.isfinite(interval) or interval < 0:
            raise ValueError('Request interval must be finite and nonnegative')
        self.progress=progress
        self.conservative=conservative
        self.path, self.interval, self.clock, self.sleep, self.metrics = path, interval, clock, sleep, metrics
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def acquire(self, method="GET", endpoint=""):
        family = "writes" if method != "GET" else "reads"
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = read_json(self.path, {})
            deadline = state.get('next', 0)
            multiplier = state.get('multiplier', 1) if self.conservative else 1
            spacing = self.interval * multiplier
            if self.conservative:
                floor = 5 if family == 'writes' else 3
                deadline = max(deadline, state.get('families', {}).get(family, 0) + floor * multiplier)
                recent = [t for t in state.get('recent', []) if t > self.clock() - 30]
                if len(recent) >= 10:
                    deadline = max(deadline, recent[-10] + 30.01)
            if self.progress and deadline-self.clock()>2:
                self.progress.emit('cooldown' if state.get('retry_until',0)>self.clock() else 'pacing',message='Waiting before the next Spotify request',remaining_seconds=deadline-self.clock())
            while deadline > self.clock():
                delay = min(30.0, deadline - self.clock())
                if self.progress:
                    self.progress.check()
                started=self.clock()
                try:
                    self.sleep(delay)
                finally:
                    self.metrics['wait_seconds'] += max(0,self.clock()-started)
            now = self.clock()
            state['next'] = now + spacing
            if self.conservative:
                state['recent'] = [t for t in state.get('recent', []) if t > now - 30] + [now]
                state.setdefault('families', {})[family] = now
            save_json(self.path, state)

    def record(self, method, endpoint, status, seconds, retry_after=None):
        parts=endpoint.removeprefix('/v1').split('/')
        if len(parts)>2 and parts[1]=='playlists':parts[2]=':id'
        endpoint='/'.join(parts)
        known={'/me','/me/playlists','/me/library/contains','/me/player/recently-played','/playlists/:id','/playlists/:id/items'}
        if endpoint not in known:endpoint='/other'
        row=dict(at=self.clock(),endpoint=endpoint,method=method,status=status,duration_ms=round(seconds*1000,2))
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);state=read_json(self.path,{})
            diagnostic=state.setdefault('diagnostics',{'total_requests':0,'by_endpoint':{},'recent':[]})
            diagnostic['total_requests']+=1
            key=method+' '+endpoint;diagnostic['by_endpoint'][key]=diagnostic['by_endpoint'].get(key,0)+1
            diagnostic['recent']=(diagnostic['recent']+[row])[-100:]
            if status==429:diagnostic['last_429']=dict(row,retry_after=str(retry_after)[:128],retry_until=state.get('retry_until'))
            save_json(self.path,state)


    def defer(self, delay):
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = read_json(self.path, {})
            state['next'] = max(state.get('next', 0), self.clock() + delay)
            state['retry_until'] = max(state.get('retry_until',0), self.clock()+delay)
            if self.conservative:
                state['multiplier'] = min(8, max(1, state.get('multiplier', 1)) * 2)
            save_json(self.path, state)


def token_request(token_path, client_id, data, progress=None):
    """OAuth host has its own shared pacing and persistent cooldown."""
    parent = Path(token_path).resolve().parent
    root = next((p / '.spotify-accounts' for p in [parent, *parent.parents]
                 if (p / '.spotify-accounts').is_dir()), parent)
    path = root / ('oauth-rate-' + hashlib.sha256(client_id.encode()).hexdigest() + '.json')
    gate = RateGate(path, 5, time.time, time.sleep, {'wait_seconds': 0}, progress=progress)
    gate.acquire('POST', '/api/token')
    try:
        return http_json('POST', 'https://accounts.spotify.com/api/token', data, form=True)
    except RateLimited as exc:
        gate.defer(retry_delay(exc.retry_after, 0))
        raise SpotifyError('Spotify login requests are temporarily limited; try again later.') from None


def http_json(method, url, data=None, token=None, form=False, extra_headers=None, details=False):
    headers = {'Accept': 'application/json', **(extra_headers or {})}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = None
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        body = (urlencode(data) if form else json.dumps(data)).encode()
    try:
        with urlopen(Request(url, data=body, headers=headers, method=method), timeout=30) as response:
            raw = response.read()
            payload = json.loads(raw) if raw else {}
            return (payload, response.headers, response.status) if details else payload
    except HTTPError as error:
        if error.code == 304 and details:
            return None, error.headers, 304
        if error.code == 429:
            raise RateLimited(error.headers.get('Retry-After')) from None
        raise SpotifyError(f'Spotify HTTP {error.code} for {method} {urlparse(url).path}; inspect state before retrying writes.' + (' Playlist sources must be owned or accessible through collaboration.' if error.code==403 and '/playlists/' in urlparse(url).path else ''), status=error.code) from None
    except (URLError, TimeoutError):
        raise SpotifyError('The connection was interrupted. Your mix is saved. Reconnect and resume the update.') from None
