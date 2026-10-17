"""UI-independent application operations. The engine remains pure."""
import json
from copy import deepcopy
from functools import wraps
import fcntl
from dataclasses import dataclass
from pathlib import Path
from .client import Client, read_json, SpotifyError
from .progress import Progress
from .sync import library, plan, recover_history, ordered, key, adapt
from .writes import apply, ids, guard, desired_queue, PreconditionFailed
from .progressive import run


def exclusive(method):
    @wraps(method)
    def operation(self,*args,**kwargs):
        def perform():
            try:
                return method(self,*args,**kwargs)
            except BaseException as exc:
                self.progress.failed(exc)
                raise
        if self.lock_held:
            return perform()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (self.root/'lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise SpotifyError('Another operation is running for this playlist; wait or cancel it') from None
            self.lock_held=True
            try:
                return perform()
            finally:
                self.lock_held=False
    return operation


@dataclass
class Preview:
    state: dict
    report: dict
    previous: dict
    snapshot: str
    events: list
    actual: list
    operation: str


class Application:
    def __init__(self,client,root,store,progress=None,library_loader=library,publisher=apply,lock_held=False):
        self.client,self.root,self.store=client,Path(root),store
        self.progress=progress or Progress(store)
        self.library_loader,self.publisher=library_loader,publisher
        self.account=None
        self.lock_held=lock_held

    def validate_dependencies(self, sources, target=None):
        """Reject reachable cycles among mixes registered in this account's store."""
        graph = {}
        if hasattr(self.store, 'db'):
            records = self.store.db.execute(
                "SELECT payload FROM documents WHERE name IN ('state.json','setup.json')").fetchall()
            for (payload,) in records:
                record = json.loads(payload)
                if record.get('account') == self.account and record.get('target'):
                    graph.setdefault(record['target'], set()).update(record.get('sources', []))
        if target:
            graph[target] = set(sources)
        visiting, finished = set(), set()
        # Iterative DFS avoids recursion limits for long dependency chains.
        for root in ([target] if target else sources):
            stack = [(root, False)]
            while stack:
                node, leaving = stack.pop()
                if leaving:
                    visiting.discard(node); finished.add(node)
                elif node in visiting:
                    raise SpotifyError('Circular playlist sources detected; choose sources without a dependency loop')
                elif node not in finished:
                    visiting.add(node); stack.append((node, True))
                    stack.extend((child, False) for child in graph.get(node, ()))

    def connect(self):
        self.progress.emit('connecting',message='Connecting to Spotify')
        self.account=self.client.request('GET','/me')['id']
        state=self.store.read(self.root/'state.json') or self.store.read(self.root/'setup.json')
        if state and state['account']!=self.account:
            raise SpotifyError('Workspace belongs to a different account')
        if isinstance(self.client,Client):
            self.client.bind_account_cache(self.account,read_json(self.root/'token.json') if state else None,(state or {}).get('snapshots',{}))
        return self.account

    @exclusive
    def create(self,**options):
        sources=options.get('sources',[])
        if not sources:
            raise SpotifyError('At least one source is required')
        self.validate_dependencies(sources)
        # Probe readability before saving a new setup or creating a target.
        for source in sources:
            self.progress.emit('validating',message='Checking source access')
            metadata=self.client.request('GET','/playlists/'+source)
            if (metadata.get('owner') or {}).get('id')!=self.account and not metadata.get('collaborative'):
                raise SpotifyError('Sources must be owned or accessible collaborative playlists')
            self.client.request('GET','/playlists/'+source+'/items?limit=1')
        return self._setup(**options)

    @exclusive
    def resume(self,genres=None):
        return self._setup(genres=genres)

    def _setup(self,**options):
        saved = self.store.read(self.root/'state.json') or self.store.read(self.root/'setup.json') or {}
        self.validate_dependencies(options.get('sources', saved.get('sources', [])), saved.get('target'))
        def publish(client,path,state):
            return self.publisher(client,path,state,writer=self.store.write,progress=self.progress)
        return run(self.client,self.root,self.account,publish,ids,store=self.store,
                   on_progress=self.progress.emit,**options)

    @exclusive
    def preview(self,recovery=False,reconcile=False,genres=None,automatic=False):
        state=self.store.read(self.root/'state.json')
        if not state or state['account']!=self.account or state['target'] in state['sources']:
            raise SpotifyError('Missing state, account mismatch, or target used as source')
        self.validate_dependencies(state['sources'], state['target'])
        if not automatic and isinstance(self.client,Client) and not self.client.playlist_saved(state['target']):
            error=SpotifyError('This playlist was removed from your library')
            error.ui_code='target_missing'
            raise error
        pending_history_recovery=recovery and state['status']=='pending' and bool(state.get('write_guard'))
        if state['status']!='ready' and not pending_history_recovery:
            raise SpotifyError('Incomplete write; inspect state and recover first')
        if genres is None:
            genres=(self.store.read(self.root/'setup.json') or {}).get('genres',{})
        import time
        events=ordered(self.client.recent())
        previous=tuple(state['last_event']) if state.get('last_event') else None
        fresh=any(previous is None or key(event)>previous for event in events)
        audit_due=time.time()-state.get('last_full_check',0)>=1800
        if automatic and not fresh and not audit_due and len(state['queue'])==state['size']:
            updated=dict(state,last_check=time.time())
            return Preview(updated,dict(idle=True,new_plays=0,consumed=0,queue_size=len(state['queue']),write_needed=False),deepcopy(state),None,[],[t['id'] for t in state['queue']],'idle')
        if automatic and isinstance(self.client,Client) and not self.client.playlist_saved(state['target']):
            error=SpotifyError('This playlist was removed from your library');error.ui_code='target_missing';raise error
        self.progress.emit('importing',message='Loading source library')
        pool,snapshots,_=self.library_loader(self.client,state['sources'],genres,cache_sources=True)
        self.progress.emit('reading_target',message='Checking target playlist')
        try:metadata,rows=self.client.playlist(state['target'])
        except SpotifyError as exc:
            if exc.status==404:
                exc.ui_code='target_missing'
            raise
        if metadata['owner']['id']!=self.account:
            raise SpotifyError('Target ownership mismatch')
        actual=ids(rows)
        if pending_history_recovery:
            pending=state['write_guard'];desired=[t['id'] for t in desired_queue(state)]
            if actual!=pending['actual'] and actual!=desired[:len(actual)]:
                raise PreconditionFailed('Target diverged from the journal; inspect user edits before history recovery')
        callback=lambda done,total:self.progress.emit('generating',message='Building future queue',completed=done,total=total)
        self.progress.emit('planning',message='Planning playlist',candidate_count=len(pool))
        if recovery:
            updated,report=recover_history(dict(state,status='ready'),pool,snapshots,actual,events,genres,progress=callback)
            if pending_history_recovery:report['pending_write_recovered']=True
        else:
            updated,report=plan(state,pool,snapshots,actual,events,genres,True,progress=callback,allow_history_gap=True,observed_tracks=[t for row in rows if (t:=adapt(row.get('item',row.get('track')),genres)) is not None])
        updated['last_full_check']=time.time();updated['last_check']=time.time()
        self.progress.emit('preview_ready',message='Preview ready',queue_size=len(updated['queue']))
        return Preview(updated,report,deepcopy(state),metadata['snapshot_id'],
                       [list(key(event)) for event in events],actual,'recover_history' if recovery else 'sync')

    @exclusive
    def apply_preview(self,preview):
        current=self.store.read(self.root/'state.json')
        if current!=preview.previous:
            raise PreconditionFailed('Local state changed since preview; preview again')
        self.validate_dependencies(current['sources'], current['target'])
        self.progress.check()
        if preview.operation=='idle':
            self.store.write(self.root/'state.json',preview.state)
            return preview.state
        guard(self.client,preview.state['target'],preview.snapshot,preview.events,preview.actual)
        state=deepcopy(current)
        state.update(status='pending',pending_update=deepcopy(preview.state),pending_report=deepcopy(preview.report),write_guard=dict(snapshot=preview.snapshot,events=preview.events,actual=preview.actual))
        if preview.operation=='recover_history':self.store.write(self.root/f"history-backup-{current['generation']}.json",current)
        self.store.write(self.root/'state.json',state)
        try:
            self.publisher(self.client,self.root/'state.json',state,writer=self.store.write,progress=self.progress)
        except PreconditionFailed:
            # Once a journal exists, a write may already have happened. Never roll it back.
            if not state.get('write_journal'):self.store.write(self.root/'state.json',current)
            raise
        self.progress.emit('ready',message='Playlist verified',target=state['target'],queue_size=len(state['queue']))
        return state

    @exclusive
    def recreate(self):
        state=self.store.read(self.root/'state.json') or {}
        if state.get('status')!='ready' or state.get('account')!=self.account:
            raise SpotifyError('Incomplete write; inspect state and recover first')
        try:self.client.request('GET','/playlists/'+state['target'])
        except SpotifyError as exc:
            if exc.status!=404:raise
        else:
            if not isinstance(self.client,Client) or self.client.playlist_saved(state['target']):
                raise SpotifyError('The playlist is still available; sync it instead.')
        setup=self.store.read(self.root/'setup.json') or {}
        self.store.write(self.root/'replaced-target.json',state)
        trial=deepcopy(state);trial.update(status='creating',target=None)
        self.store.write(self.root/'state.json',trial)
        target=self.client.request('POST','/me/playlists',dict(name=setup.get('name','Saved mix'),public=False))['id']
        trial.update(status='pending',target=target)
        self.store.write(self.root/'state.json',trial)
        metadata,rows=self.client.playlist(target)
        if metadata['owner']['id']!=self.account or ids(rows):
            raise PreconditionFailed('New target changed before first fill; inspect pending state')
        events=ordered(self.client.recent())
        trial['write_guard']=dict(snapshot=metadata['snapshot_id'],events=[list(key(e)) for e in events],actual=[])
        self.store.write(self.root/'state.json',trial)
        self.publisher(self.client,self.root/'state.json',trial,writer=self.store.write,progress=self.progress)
        setup.update(name_sync_pending=False,changes_sync_pending=False)
        self.store.write(self.root/'setup.json',setup)
        return trial

    @exclusive
    def rename_pending(self):
        setup=self.store.read(self.root/'setup.json') or {}
        if not setup.get('name_sync_pending'):return
        state=self.store.read(self.root/'state.json') or {}
        if state.get('status')!='ready' or state.get('account')!=self.account:raise SpotifyError('Incomplete write; verify the playlist before renaming')
        path='/playlists/'+state['target']
        metadata=self.client.request('GET',path)
        if metadata['owner']['id']!=self.account:raise SpotifyError('Target ownership mismatch')
        if metadata.get('name')!=setup['name']:
            self.client.request('PUT',path,dict(name=setup['name']))
            if self.client.request('GET',path).get('name')!=setup['name']:raise SpotifyError('Playlist rename could not be verified')
        setup['name_sync_pending']=False
        self.store.write(self.root/'setup.json',setup)

    @exclusive
    def recover_write(self):
        state=self.store.read(self.root/'state.json')
        if not state or state['status']!='pending' or state['account']!=self.account:
            raise SpotifyError('Recover requires pending state for this account; uncertain creation needs inspection')
        self.validate_dependencies(state['sources'], state['target'])
        self.publisher(self.client,self.root/'state.json',state,writer=self.store.write,progress=self.progress,recovery=True)
        return state

    def status(self):
        state=self.store.read(self.root/'state.json')
        job=self.store.read(self.root/'setup.json')
        return dict(state=state,setup=job)
