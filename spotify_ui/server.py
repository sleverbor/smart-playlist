"""Loopback-only HTTP server. Mutations require same-origin requests and a token."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,secrets
from pathlib import Path
from urllib.parse import urlsplit
from spotify_poc.client import SpotifyError

from .errors import describe

STATIC=Path(__file__).parent/'static'

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,controller):
        if address[0]!='127.0.0.1':raise ValueError('The local app must bind to 127.0.0.1')
        self.controller=controller;self.csrf=secrets.token_urlsafe(32)
        super().__init__(address,Handler)
    @property
    def origin(self):return f'http://127.0.0.1:{self.server_port}'

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def headers_ok(self,mutation=False):
        if self.headers.get('Host')!=urlsplit(self.server.origin).netloc:return False
        origin=self.headers.get('Origin')
        if origin and origin!=self.server.origin:return False
        if self.headers.get('Sec-Fetch-Site')=='cross-site':return False
        if mutation and (origin!=self.server.origin or not secrets.compare_digest(self.headers.get('X-App-Token','').encode(),self.server.csrf.encode())):return False
        return True
    def send(self,status,payload,kind='application/json; charset=utf-8'):
        if isinstance(payload,dict) and payload.get('error') and not payload.get('error_detail'):payload=dict(payload,error_detail=describe(payload['error']))
        body=json.dumps(payload).encode() if kind.startswith('application/json') else payload
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header('Referrer-Policy','no-referrer');self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if not self.headers_ok():return self.send(403,dict(error='Open the app at its local address.'))
        path=urlsplit(self.path).path
        try:
            if path=='/api/bootstrap':return self.send(200,dict(token=self.server.csrf,**self.server.controller.snapshot()))
            if path=='/api/state':return self.send(200,self.server.controller.snapshot())
            if path.startswith('/api/sources/'):
                return self.send(200,self.server.controller.source_status(path.rsplit('/',1)[-1]))
            if path.startswith('/api/mixes/'):
                return self.send(200,self.server.controller.details(path.rsplit('/',1)[-1]))
            if path.startswith('/api/jobs/'):return self.send(200,self.server.controller.job(path.rsplit('/',1)[-1]))
            asset={'/':'index.html','/app.js':'app.js','/en.js':'en.js','/i18n.js':'i18n.js','/style.css':'style.css'}.get(path)
            if not asset:return self.send(404,dict(error='Not found'))
            mime={'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8','en.js':'text/javascript; charset=utf-8','i18n.js':'text/javascript; charset=utf-8','style.css':'text/css; charset=utf-8'}[asset]
            return self.send(200,(STATIC/asset).read_bytes(),mime)
        except (SpotifyError,ValueError,KeyError) as exc:return self.send(400,dict(error=str(exc)))
    def do_POST(self):
        if not self.headers_ok(True):return self.send(403,dict(error='Refresh the app and try again.'))
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.send(415,dict(error='Expected JSON'))
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=2_000_000:return self.send(413,dict(error='Request is too large or empty'))
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict):raise ValueError('Expected an object')
            path=urlsplit(self.path).path
            if path.startswith('/api/jobs/') and path.endswith('/cancel'):
                return self.send(200,self.server.controller.cancel(path.split('/')[-2]))
            if path.startswith('/api/actions/'):
                return self.send(202,self.server.controller.submit(path.rsplit('/',1)[-1],data))
            return self.send(404,dict(error='Not found'))
        except (SpotifyError,ValueError,KeyError,TypeError) as exc:return self.send(400,dict(error=str(exc)))
