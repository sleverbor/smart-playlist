import argparse
import fcntl
import json
import os
import sys
import sqlite3
from pathlib import Path
from .client import Client, SpotifyError, login, read_json, save_json
from .sync import library, bootstrap, plan, playlist_id, recover_history


def load_rules(path):
    if path is None:
        return [dict(id='track', kind='track', window=100, strength='REQUIRED'),
                dict(id='artist', kind='artist', window=5, strength='PREFERRED'),
                dict(id='album', kind='album', window=3, strength='OPTIONAL')]
    try:
        rows = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SpotifyError(f'Cannot load rules file {path}: {exc}') from None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SpotifyError('Rules file must contain a JSON array of rule objects; null is invalid')
    return rows


from .writes import apply, ids
from .service import Application
from .storage import SQLiteStore, JsonStore
from .accounts import Accounts
from .progress import Progress

def usable_playlists(client, account):
    for row in client.pages('/me/playlists?limit=50'):
        owned = (row.get('owner') or {}).get('id') == account
        if not owned and not row.get('collaborative'):
            continue
        # The collaborative flag alone does not prove this account has access.
        try:
            client.request('GET', '/playlists/' + row['id'] + '/items?limit=1')
        except SpotifyError as exc:
            if exc.status in (403, 404):
                continue
            raise
        yield row, 'owned' if owned else 'collaborative'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--directory', default='.spotify-poc')
    p.add_argument('--accounts-directory', default='.spotify-accounts')
    p.add_argument('--account')
    p.add_argument('--storage',choices=('sqlite','json'),default='sqlite',help='SQLite is primary; json is legacy compatibility')
    s = p.add_subparsers(dest='command', required=True)
    s.add_parser('login')
    s.add_parser('playlists')
    s.add_parser('status')
    export=s.add_parser('export')
    export.add_argument('--output',required=True)
    c = s.add_parser('create')
    c.add_argument('--source', action='append', required=True)
    c.add_argument('--name', default='Smart playlist prototype')
    c.add_argument('--size', type=int, default=100)
    c.add_argument('--seed', type=int, default=0)
    c.add_argument('--rules')
    c.add_argument('--minimum-buffer', type=int, default=20)
    c.add_argument('--force', action='store_true', help='Back up and replace an unpublished setup; keep login credentials')
    s.add_parser('resume').add_argument('--genres')
    h = s.add_parser('recover-history')
    h.add_argument('--apply', action='store_true')
    h.add_argument('--genres')
    for name in ('sync', 'recover'):
        q = s.add_parser(name)
        q.add_argument('--apply', action='store_true')
        if name == 'sync':
            q.add_argument('--reconcile', action='store_true')
            q.add_argument('--genres')
    for q in (c, q):
        q.add_argument('--genres')
    a = p.parse_args()
    root = Path(a.directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        accounts=Accounts(a.accounts_directory)
        if a.command == 'login':
            login(os.environ['SPOTIFY_CLIENT_ID'], root / 'token.json')
            client=Client(root/'token.json',rate_path=accounts.rate_path(read_json(root/'token.json')))
            account=client.request('GET','/me')['id']
            accounts.register(account,client.token)
            print('Account saved; new playlist directories can reuse this login.')
            return
        if a.storage=='json':
            database=Path(a.accounts_directory)/'application.sqlite'
            if database.exists():
                with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
                    migrated=db.execute('SELECT migrated FROM workspaces WHERE id=?',(str(root.resolve()),)).fetchone()
                if migrated and migrated[0]:
                    raise SpotifyError('This workspace uses SQLite; legacy JSON is stale. Use default storage or export to a separate directory')
            store=JsonStore(root)
            token_path=root/'token.json'
        else:
            store=SQLiteStore(root,Path(a.accounts_directory)/'application.sqlite')
            # Offline status/export must not require a working Spotify session.
            if a.command=='status':
                state=store.read(root/'state.json')
                job=store.read(root/'setup.json')
                print(json.dumps(dict(status=(state or {}).get('status'),target=(state or {}).get('target'),
                    queue_size=len((state or {}).get('queue',[])),setup_status=(job or {}).get('status'),last_event=store.latest_event(),api_metrics=store.read('api-metrics.json')),indent=2))
                store.close();return
            if a.command=='export':
                store.export(a.output);store.close();return
            bound=store.read(root/'state.json') or store.read(root/'setup.json') or {}
            token_path=accounts.select(root,a.account or bound.get('account'))
        def display(event):
            stage=event['stage']
            if stage=='request':
                print(f"Spotify: {event['method']} {event['endpoint']}",file=sys.stderr,flush=True)
            elif event.get('message'):
                suffix=''
                if event.get('completed') is not None:
                    suffix=f" {event['completed']}/{event.get('total') or '?'}"
                if stage=='cooldown':suffix=f" ({event['remaining_seconds']:.0f} seconds)"
                print(event['message']+suffix,file=sys.stderr,flush=True)
        progress=Progress(store,display)
        token=read_json(token_path,{})
        rate_path=accounts.import_legacy_rate(root,token) if token.get('client_id') else None
        client=Client(token_path,store=store if isinstance(store,SQLiteStore) else None,
                      progress=progress,rate_path=rate_path)
        app=Application(client,root,store,progress,library_loader=library,publisher=apply,lock_held=True)
        try:
            account=app.connect()
            if a.account and account!=a.account:
                raise SpotifyError("Login does not match the requested account")
            if isinstance(getattr(client,'token',None),dict):
                client.token_path=accounts.register(account,client.token)
            if a.command=='playlists':
                found=False
                for row,access in usable_playlists(client,account):
                    print(row['id'],row['name'],f'[{access}]');found=True
                if not found:print('No accessible owned or collaborative source playlists found.')
                return
            genres=read_json(a.genres) if getattr(a,'genres',None) else None
            if a.command=='recover':
                if not a.apply:raise SpotifyError('Recover requires --apply')
                app.recover_write()
            elif a.command=='create':
                app.create(sources=list(dict.fromkeys(playlist_id(x) for x in a.source)),rows=load_rules(a.rules),
                    minimum=a.minimum_buffer,normal=a.size,seed=a.seed,name=a.name,genres=genres,force=a.force)
            elif a.command=='resume':
                app.resume(genres=genres)
            elif a.command in ('sync','recover-history'):
                preview=app.preview(recovery=a.command=='recover-history',reconcile=getattr(a,'reconcile',False),genres=genres)
                print(json.dumps({key:value for key,value in preview.report.items() if key!='engine'},indent=2))
                if a.apply:app.apply_preview(preview)
            else:
                raise SpotifyError('Status/export require SQLite storage')
        except BaseException as exc:
            if hasattr(store,'event'):
                store.event(progress.operation,dict(stage='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',
                    error_type=type(exc).__name__,message=str(exc)))
            raise
        finally:
            if isinstance(getattr(client,'metrics',None),dict):
                store.write(root/'api-metrics.json',client.metrics)
            if hasattr(store,'close'):store.close()



if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('Setup paused. Run resume to continue; recover first if a write is pending.')
    except (SpotifyError, ValueError, KeyError, BlockingIOError) as exc:
        raise SystemExit(str(exc))
