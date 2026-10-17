"""Threaded UI adapter. Database connections and Spotify work stay in workers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timezone
import fcntl,hashlib,json,re,sqlite3,threading,time,uuid
from pathlib import Path
from spotify_poc.accounts import Accounts
from spotify_poc.client import Client,SpotifyError,login,read_json
from spotify_poc.progress import Progress,Cancelled
from spotify_poc.service import Application
from spotify_poc.storage import SQLiteStore
from spotify_poc.__main__ import usable_playlists
from spotify_poc.sync import rules_from
from .errors import describe

class Controller:
    def __init__(self,project,demo=False,client_factory=Client):
        self.project=Path(project).resolve();self.accounts=Accounts(self.project/'.spotify-accounts')
        self.database=self.accounts.root/'application.sqlite';self.demo=demo
        self.client_factory=client_factory;self.jobs={};self.previews={};self.names={};self.lock=threading.RLock()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='playlist-ui')
        self.fake=None
        if demo:
            from .demo import DemoSpotify
            self.fake=DemoSpotify();self.names['demo']='Demo listener'
        # Initialize schema without moving or altering an existing playlist workspace.
        store=SQLiteStore(self.project/'.ui',self.database)
        self.names.update(store.cache_read('ui','account-labels') or {})
        store.close()
        self.stop_scheduler=threading.Event()
        self.scheduler=threading.Thread(target=self.schedule_loop,daemon=True)
        self.scheduler.start()

    def cooldown_until(self,account,root=None):
        if self.demo:return 0
        row=read_json(self.accounts.index_path,{}).get(account,{})
        client_id=row.get('client_id')
        if not client_id and root:
            client_id=(read_json(Path(root)/'token.json',{}) or {}).get('client_id')
        if not client_id:return 0
        path=self.accounts.root/('rate-'+hashlib.sha256(client_id.encode()).hexdigest()+'.json')
        # Explicit rate-state overrides must agree with the HTTP client's gate.
        import os
        path=Path(os.environ.get('SPOTIFY_RATE_STATE',str(path)))
        return max(read_json(path,{}).get('retry_until',0), (read_json(Path(root)/'rate.json',{}) if root else {}).get('retry_until',0))

    @staticmethod
    def identity(root):return hashlib.sha256(str(root).encode()).hexdigest()[:24]
    def snapshot(self):
        mixes=[]
        with closing(sqlite3.connect(self.database)) as db:
            for (workspace,) in db.execute('SELECT id FROM workspaces ORDER BY id'):
                root=Path(workspace).resolve()
                if root.name=='.ui' or not root.is_relative_to(self.project):continue
                documents={name:json.loads(payload) for name,payload in db.execute('SELECT name,payload FROM documents WHERE workspace=? AND name IN (?,?,?)',(workspace,'state.json','setup.json','settings.json'))}
                state,job=documents.get('state.json',{}),documents.get('setup.json',{})
                if not state and not job:continue
                count=db.execute('SELECT count(*) FROM sequences WHERE workspace=? AND kind=?',(workspace,'queue')).fetchone()[0]
                imported=db.execute('SELECT count(*) FROM import_items WHERE workspace=?',(workspace,)).fetchone()[0]
                updated=db.execute('SELECT max(updated) FROM documents WHERE workspace=? AND name IN (?,?)',(workspace,'state.json','setup.json')).fetchone()[0]
                settings=documents.get('settings.json',{})
                cooldown=self.cooldown_until((state or job).get('account'),root)
                if cooldown<=time.time():cooldown=0
                preview=[json.loads(row[0])['name'] for row in db.execute('SELECT payload FROM sequences WHERE workspace=? AND kind=? ORDER BY position LIMIT 10',(workspace,'queue'))]
                mixes.append(dict(id=self.identity(root),name=job.get('name') or 'Saved mix',account=(state or job).get('account'),status=state.get('status','setup'),setup_status=job.get('status'),changes_pending=bool(job.get('changes_sync_pending') or job.get('name_sync_pending')),target=state.get('target'),auto_sync=settings.get('auto_sync',False),last_sync=settings.get('last_sync'),last_check=settings.get('last_check'),sync_status=settings.get('sync_status'),sync_message=settings.get('sync_message'),sync_error=settings.get('sync_error'),cooldown_until=cooldown or None,next_sync=max(settings.get('next_sync') or 0,cooldown) if settings.get('auto_sync') else None,queue_size=count,normal=job.get('normal',state.get('size')),imported=imported,source_count=len((state or job).get('sources',[])),updated=updated,upcoming=preview))
        index=read_json(self.accounts.index_path,{})
        accounts=[dict(id=i,name=self.names.get(i,'Saved Spotify account')) for i in index]
        if self.demo:accounts=[dict(id='demo',name='Demo listener')]
        with self.lock:
            jobs=[self.public(job) for job in self.jobs.values()]
        return dict(demo=self.demo,accounts=accounts,mixes=mixes,jobs=jobs,active=next((j['id'] for j in jobs if j['status'] in ('queued','running','cancelling')),None))

    def source_status(self,account):
        if account not in [a['id'] for a in self.snapshot()['accounts']]:
            raise SpotifyError('Choose a connected Spotify account.')
        store=SQLiteStore(self.project/'.ui',self.database)
        try:
            saved=store.cache_read('ui','sources-'+account) or {}
            generated=self.generated_targets(store).get(account,set())
            rows=[row for row in saved.get('rows',[]) if row['id'] not in generated]
            deadline=self.cooldown_until(account,self.project/'.ui')
            return dict(sources=rows,cached=bool(saved),cooldown_until=deadline if deadline>time.time() else None)
        finally:store.close()

    def workspace(self,mix):
        with closing(sqlite3.connect(self.database)) as db:
            for (workspace,) in db.execute('SELECT id FROM workspaces'):
                root=Path(workspace).resolve()
                if root.is_relative_to(self.project) and self.identity(root)==mix and root.name!='.ui':return root
        raise SpotifyError('This mix was not found. Refresh the page.')

    def public(self,job):return deepcopy({k:v for k,v in job.items() if k not in ('cancel','future')})
    def job(self,identity):
        with self.lock:
            if identity not in self.jobs:raise SpotifyError('Operation not found. Refresh the page.')
            return self.public(self.jobs[identity])
    def cancel(self,identity):
        with self.lock:
            job=self.jobs.get(identity)
            if not job:raise SpotifyError('Operation not found')
            if job['status'] in ('queued','running','cancelling'):
                job['cancel'].set();job['status']='cancelling'
            return self.public(job)
    def update(self,job,**fields):
        with self.lock:job.update(fields)
    def emit(self,job,event):
        # Request endpoints and credentials are not part of the product UI.
        if event['stage']=='request':return
        if event['stage']=='cooldown' and event.get('remaining_seconds',0)>60:
            hours=event['remaining_seconds']/3600
            raise SpotifyError(f'Spotify is temporarily limiting requests. Try again in about {hours:.1f} hours. Your playlist and saved progress are unchanged.')
        with self.lock:
            job['progress']=event
            if event['stage']=='ready' and event.get('target'):
                job['ready']=dict(target=event['target'],queue_size=event['queue_size'])
    def submit(self,action,data):
        if action not in ('sources','create','resume','preview','apply','recover','login','delete','sync','rules','settings','edit_sources','edit_mix','recreate'):raise SpotifyError('Unknown action')
        with self.lock:
            if any(j['status'] in ('queued','running','cancelling') for j in self.jobs.values()):raise SpotifyError('An operation is already running. Wait or cancel it first.')
            self.previews={k:v for k,v in self.previews.items() if v['expires']>time.monotonic()}
            while len(self.jobs)>=20:self.jobs.pop(next(iter(self.jobs)))
            job=dict(id=uuid.uuid4().hex,action=action,status='queued',created=time.time(),progress=None,data=None,error=None,cancel=threading.Event())
            if action=='create':
                options=self.create_options(data)
                root=self.project/'mixes'/uuid.uuid4().hex
            elif action in ('resume','preview','apply','recover','delete','sync','rules','settings','edit_sources','edit_mix','recreate'):root=self.workspace(data.get('mix'))
            else:root=self.project/'.ui'
            job['mix']=self.identity(root) if action in ('create','resume','preview','apply','recover','delete','sync','rules','settings','edit_sources','edit_mix','recreate') else None
            self.jobs[job['id']]=job
            job['future']=self.executor.submit(self.run,job,action,deepcopy(data),root,options if action=='create' else None)
            return self.public(job)

    def create_options(self,data):
        name=data.get('name','')
        if not isinstance(name,str) or not name.strip() or len(name)>100:raise SpotifyError('Choose a playlist name of 1–100 characters.')
        sources=data.get('sources')
        if not isinstance(sources,list) or not 1<=len(sources)<=100 or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9]{1,64}',x) for x in sources):raise SpotifyError('Select at least one valid source playlist.')
        minimum,normal,seed=data.get('minimum',20),data.get('normal',100),data.get('seed',0)
        if any(type(x) is not int for x in (minimum,normal,seed)) or not 1<=minimum<=normal<=10000 or not 0<=seed<=2147483647:raise SpotifyError('The initial buffer must be between 1 and the normal buffer (maximum 10,000 songs).')
        rows=data.get('rules',[]);rules_from(rows,[])
        if len(rows)>4 or len({r['kind'] for r in rows})!=len(rows):raise SpotifyError('Use one rule per track, artist, album or genre.')
        genres=data.get('genres') or {}
        if not isinstance(genres,dict) or any(not isinstance(k,str) or not isinstance(v,list) or not v or any(not isinstance(g,str) or not g for g in v) for k,v in genres.items()):raise SpotifyError('Genre labels must map track IDs to nonempty lists of names.')
        if any(r.get('enabled',True) and r['kind']=='genre' and r['window']>0 for r in rows) and not genres:raise SpotifyError('Add genre labels before enabling genre spacing.')
        return dict(name=name.strip(),sources=list(dict.fromkeys(sources)),minimum=minimum,normal=normal,seed=seed,rows=rows,genres=genres)

    def connect(self,root,store,progress,account):
        if self.demo:client=self.fake
        else:
            deadline=self.cooldown_until(account,root)
            if deadline>time.time():
                error=SpotifyError('Waiting for Spotify until '+datetime.fromtimestamp(deadline).astimezone().strftime('%b %d, %I:%M %p')+'. Automatic sync will resume after the cooldown.')
                error.ui_code='cooldown';error.ui_params=dict(deadline=deadline)
                raise error
            token_path=self.accounts.select(root,account)
            client=self.client_factory(token_path,store=store,progress=progress,rate_path=self.accounts.import_legacy_rate(root,read_json(token_path)))
        app=Application(client,root,store,progress);verified=app.connect()
        if account and account!=verified:raise SpotifyError('The selected Spotify account does not match this login.')
        if not self.demo:
            client.token_path=self.accounts.register(verified,client.token)
        if verified not in self.names:
            profile=client.request('GET','/me');self.names[verified]=profile.get('display_name') or 'Spotify account'
            store.cache_write('ui','account-labels',self.names)
        return app,client,verified

    def run(self,job,action,data,root,options):
        store=None;client=None
        try:
            self.update(job,status='running')
            store=SQLiteStore(root,self.database)
            progress=Progress(store,lambda event:self.emit(job,event),job['cancel'])
            progress.check()
            if action=='delete':
                with (root/'lock').open('a') as lock:
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:raise SpotifyError('This mix is busy. Try again after its current operation finishes.') from None
                    backup=self.project/'.sync-backups'/uuid.uuid4().hex
                    store.export(backup)
                    self.generated_targets(store)
                    with store.transaction():
                        for table in ('documents','sequences','imports','import_items','tracks','attributes','events'):
                            store.db.execute(f'DELETE FROM {table} WHERE workspace=?',(store.workspace,))
                    with self.lock:
                        self.previews={k:v for k,v in self.previews.items() if v['mix']!=job['mix']}
                result=dict(mix=job['mix'])
            elif action in ('rules','settings','edit_sources','edit_mix'):
                with (root/'lock').open('a') as lock:
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:raise SpotifyError('This mix is busy. Try again shortly.') from None
                    saved=store.read('state.json') or {}
                    if saved.get('status')!='ready':raise SpotifyError('Finish or recover setup before editing.')
                    if action=='edit_mix':
                        name=data.get('name');size=data.get('size')
                        if not isinstance(name,str) or not name.strip() or len(name)>100:raise SpotifyError('Choose a playlist name of 1–100 characters.')
                        if type(size) is not int or not 1<=size<=10000:raise SpotifyError('Choose a valid queue size from 1 to 10000.')
                        setup=store.read('setup.json') or {}
                        if setup.get('status') in ('importing','awaiting_pause'):raise SpotifyError('Finish setup before editing this mix.')
                        if setup.get('name')!=name.strip():setup['name_sync_pending']=True
                        setup.update(name=name.strip(),normal=size,changes_sync_pending=True)
                        saved['size']=size
                        with store.transaction():store.write('state.json',saved);store.write('setup.json',setup)
                    elif action=='edit_sources':
                        sources=data.get('sources')
                        if not isinstance(sources,list) or not 1<=len(sources)<=100 or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9]{1,64}',x) for x in sources):raise SpotifyError('Select at least one valid source playlist.')
                        sources=list(dict.fromkeys(sources))
                        generated=self.generated_targets(store).get(saved['account'],set())
                        if any(x in generated for x in sources):raise SpotifyError('Generated playlists cannot be selected as sources.')
                        setup=store.read('setup.json') or {}
                        if setup.get('status') in ('importing','awaiting_pause'):raise SpotifyError('Finish setup before changing source playlists.')
                        app=Application(None,root,store);app.account=saved['account']
                        app.validate_dependencies(sources,saved['target'])
                        saved['sources']=sources
                        saved['snapshots']={k:v for k,v in saved.get('snapshots',{}).items() if k in sources}
                        setup['sources']=sources
                        setup['changes_sync_pending']=True
                        with store.transaction():
                            store.write('state.json',saved)
                            if setup.get('name'):store.write('setup.json',setup)
                    elif action=='rules':
                        rows=data.get('rules');rules_from(rows,[])
                        if len(rows)>3 or len({r['kind'] for r in rows})!=len(rows) or any(r['kind'] not in ('track','artist','album') or type(r['window']) is not int or not 0<=r['window']<=10000 for r in rows):raise SpotifyError('Choose valid track, artist and album spacing.')
                        setup=store.read('setup.json') or {}
                        saved['rules']=rows;setup['rules']=rows;setup['changes_sync_pending']=True
                        with store.transaction():
                            store.write('state.json',saved);store.write('setup.json',setup)
                    else:
                        enabled=data.get('auto_sync')
                        if type(enabled) is not bool:raise SpotifyError('Choose whether automatic sync is enabled.')
                        settings=store.read('settings.json') or {}
                        settings.update(auto_sync=enabled,next_sync=time.time()+600)
                        store.write('settings.json',settings)
                    with self.lock:self.previews={k:v for k,v in self.previews.items() if v['mix']!=job['mix']}
                    result=dict(mix=job['mix'])
            elif action=='login':
                if self.demo:raise SpotifyError('Demo mode does not connect to Spotify.')
                index=read_json(self.accounts.index_path,{})
                client_id=data.get('client_id') or next((v['client_id'] for v in index.values()),None)
                if not isinstance(client_id,str) or not re.fullmatch('[A-Za-z0-9]{16,64}',client_id):raise SpotifyError('Enter the client ID from your Spotify developer application.')
                progress.emit('authorization',message='Open Spotify to connect your account')
                login(client_id,root/'token.json',on_authorize=lambda url:self.update(job,auth_url=url),cancellation=job['cancel'])
                app,client,account=self.connect(root,store,progress,None)
                result=dict(account=account,name=self.names[account])
                self.update(job,auth_url=None)
            else:
                bound=store.read('state.json') or store.read('setup.json') or {}
                app,client,account=self.connect(root,store,progress,bound.get('account') or data.get('account'))
                if action=='sources':
                    generated=self.generated_targets(store).get(account,set())
                    rows=[]
                    for row,access in usable_playlists(client,account):
                        progress.check()
                        if row['id'] in generated:
                            continue
                        total=(row.get('items') or row.get('tracks') or {}).get('total')
                        rows.append(dict(id=row['id'],name=row['name'],access=access,total=total))
                        self.update(job,data=dict(sources=deepcopy(rows),account=account))
                        progress.emit('sources',message='Checking source playlists',completed=len(rows),total=None)
                    store.cache_write('ui','sources-'+account,dict(rows=rows))
                    result=dict(sources=rows,account=account)
                elif action=='create':
                    app.create(**options);result=dict(mix=self.identity(root),setup_status=(store.read('setup.json') or {}).get('status'))
                elif action=='resume':app.resume();result=dict(mix=self.identity(root),setup_status=(store.read('setup.json') or {}).get('status'))
                elif action=='recreate':
                    app.recreate();result=dict(mix=self.identity(root))
                elif action=='recover':app.recover_write();result=dict(mix=self.identity(root))
                elif action=='preview':
                    preview=app.preview(recovery=bool(data.get('history_recovery')),reconcile=bool(data.get('reconcile')))
                    identity=uuid.uuid4().hex
                    with self.lock:self.previews[identity]=dict(preview=preview,mix=self.identity(root),expires=time.monotonic()+600)
                    summary={k:v for k,v in preview.report.items() if k not in ('engine',)}
                    engine=preview.report.get('engine') or {}
                    result=dict(preview_id=identity,mix=self.identity(root),summary=summary,relaxed_rules=engine.get('relaxed_rules',[]),upcoming=[t['name'] for t in preview.state['queue'][:10]],history_recovery=bool(data.get('history_recovery')))
                elif action=='sync':
                    pending=store.read('state.json') or {}
                    if pending.get('status')=='pending':
                        app.recover_write();preview=None
                    else:
                        setup=store.read('setup.json') or {}
                        preview=app.preview(automatic=bool(data.get('automatic')) and not (setup.get('changes_sync_pending') or setup.get('name_sync_pending')))
                        app.apply_preview(preview)
                    result=dict(mix=self.identity(root),summary={k:v for k,v in (preview.report if preview else {'recovered_write':True}).items() if k!='engine'})
                elif action=='apply':
                    with self.lock:cached=self.previews.pop(data.get('preview_id'),None)
                    if not cached or cached['mix']!=self.identity(root) or cached['expires']<=time.monotonic():raise SpotifyError('This preview expired. Preview the sync again.')
                    app.apply_preview(cached['preview']);result=dict(mix=self.identity(root),summary={k:v for k,v in cached['preview'].report.items() if k!='engine'})
            if action in ('sync','apply','recover','recreate'):
                app.rename_pending()
                setup=store.read('setup.json') or {}
                if setup.get('changes_sync_pending'):
                    setup['changes_sync_pending']=False
                    store.write('setup.json',setup)
            progress.check();terminal=dict(status='complete',data=result,finished=time.time())
        except BaseException as exc:
            status='cancelled' if job['cancel'].is_set() or isinstance(exc,(Cancelled,KeyboardInterrupt)) else 'failed'
            raw=str(exc);kind=None;message=raw
            if 'Playback history gap' in raw:
                kind='history_gap';message='Spotify history no longer covers the saved bookmark. Preview recovery to start from the history Spotify still reports.'
            elif 'Target changed externally' in raw:
                kind='target_changed';message='This playlist was edited in Spotify. Preview reconciliation before applying changes.'
            elif 'HTTP 401' in raw:
                kind='authentication';message='Reconnect Spotify to keep this mix updated.'
            elif getattr(exc,'ui_code',None)=='target_missing':
                kind='unavailable';message='This playlist was removed.'
            elif 'HTTP 404' in raw:
                kind='unavailable';message='A playlist is no longer available. Check the target and source playlists in Spotify.'
            elif 'Choose --account' in raw or 'Run login first' in raw:
                message='Connect Spotify, then select a saved account.'
            elif 'Genre rules require genres.json' in raw:
                message='Genre spacing needs labels for every candidate and recent track. Add a complete genre map or leave genre spacing disabled.'
            terminal=dict(status=status,error=message,error_detail=describe(exc),error_kind=kind,auth_url=None,finished=time.time())
            if store:
                store.event(job['id'],dict(stage=status,message=str(exc),timestamp=datetime.now(timezone.utc).isoformat()))
        finally:
            if store:
                if action in ('sync','apply','recover','recreate') or (terminal.get('error_detail') or {}).get('code')=='target_missing':
                    settings=store.read('settings.json') or {}
                    settings.update(sync_status=terminal['status'],sync_message=terminal.get('error'),sync_error=terminal.get('error_detail'),last_attempt=time.time())
                    if terminal['status']=='complete':
                        summary=(terminal.get('data') or {}).get('summary') or {}
                        settings['last_check']=time.time()
                        if not summary.get('idle'):settings['last_sync']=time.time()
                        settings['idle_checks']=min(3,settings.get('idle_checks',0)+1) if summary.get('idle') else 0
                        delay=min(1800,300*2**settings['idle_checks']) if summary.get('idle') else 300
                        saved_state=store.read('state.json') or {}
                        settings['next_sync']=min(time.time()+delay,max(time.time()+60,saved_state.get('last_full_check',time.time())+1800))
                    if terminal.get('error_kind') in ('authentication','unavailable'):
                        settings.update(auto_sync=False,next_sync=None)
                    store.write('settings.json',settings)
                if isinstance(getattr(client,'metrics',None),dict):store.write('api-metrics.json',client.metrics)
                if job.get('mix') and action!='delete':
                    activity=store.read('activity.json') or []
                    summary=(terminal.get('data') or {}).get('summary') or {}
                    activity.append(dict(action=action,status=terminal['status'],timestamp=terminal['finished'],message=terminal.get('error'),error_detail=terminal.get('error_detail'),consumed=summary.get('consumed'),queue_size=summary.get('queue_size')))
                    store.write('activity.json',activity[-20:])
                store.close()
            self.update(job,**terminal)
    def details(self,mix):
        root=self.workspace(mix)
        store=SQLiteStore(root,self.database)
        try:
            saved=store.read('state.json') or {}
            generated=self.generated_targets(store).get(saved.get('account'),set())
            report=store.read('decision-log.json') or {};report=report.get('sync',report)
            report={key:value for key,value in report.items() if key!='engine'}
            return dict(size=saved.get('size'),generated_targets=sorted(generated),sources=saved.get('sources',[]),available_sources=(store.cache_read('ui','sources-'+saved.get('account','')) or {}).get('rows',[]),activity=store.read('activity.json') or [],account=saved.get('account'),name=(store.read('setup.json') or {}).get('name','Saved mix'),rules=saved.get('rules',[]),status=saved.get('status'),queue=[dict(id=t['id'],name=t['name'],artist_names=t.get('artist_names',[])) for t in saved.get('queue',[])],last_report=report)
        finally:store.close()

    def generated_targets(self,store):
        # Keep provenance after setup removal; names are not reliable identifiers.
        saved=[json.loads(row[0]) for row in store.db.execute("SELECT payload FROM documents WHERE name IN ('state.json','setup.json')")]
        for backup in (self.project/'.sync-backups').glob('*'):
            for name in ('state.json','setup.json'):
                value=read_json(backup/name,{})
                if value:saved.append(value)
        for value in saved:
            if value.get('account') and value.get('target'):
                store.db.execute('INSERT OR IGNORE INTO generated_targets(account,target) VALUES(?,?)',(value['account'],value['target']))
        store.db.commit()
        result={}
        for account,target in store.db.execute('SELECT account,target FROM generated_targets'):
            result.setdefault(account,set()).add(target)
        return result

    def schedule_loop(self):
        while not self.stop_scheduler.wait(30):
            try:self.schedule_due(time.time())
            except Exception:pass # next tick retries local discovery, without API calls

    def schedule_due(self,now):
        if self.snapshot()['active']:return
        with closing(sqlite3.connect(self.database)) as db:
            rows=db.execute("SELECT workspace,payload FROM documents WHERE name='settings.json'").fetchall()
            for workspace,payload in rows:
                settings=json.loads(payload);root=Path(workspace).resolve()
                if not root.is_relative_to(self.project) or not settings.get('auto_sync') or (settings.get('next_sync') or 0)>now:continue
                state=db.execute("SELECT payload FROM documents WHERE workspace=? AND name='state.json'",(workspace,)).fetchone()
                if not state or json.loads(state[0]).get('status') not in ('ready','pending'):continue
                deadline=self.cooldown_until(json.loads(state[0]).get('account'),root)
                if deadline>now:
                    settings['next_sync']=deadline
                    db.execute("UPDATE documents SET payload=?,updated=? WHERE workspace=? AND name='settings.json'",(json.dumps(settings),now,workspace));db.commit()
                    continue
                settings['next_sync']=now+600
                db.execute("UPDATE documents SET payload=?,updated=? WHERE workspace=? AND name='settings.json'",(json.dumps(settings),now,workspace));db.commit()
                self.submit('sync',dict(mix=self.identity(root),automatic=True))
                return

    def close(self):
        self.stop_scheduler.set()
        self.scheduler.join(timeout=2)
        with self.lock:
            for job in self.jobs.values():job['cancel'].set()
        self.executor.shutdown(wait=True,cancel_futures=True)
