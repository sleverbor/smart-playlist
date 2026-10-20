"""Actual child-process death at synthetic remote write boundaries; no Spotify calls."""
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import unittest
from playlist_engine import Track
from spotify_app.client import read_json,save_json
from spotify_app.service import Application
from spotify_app.storage import SQLiteStore
from spotify_app.sync import bootstrap
from test_history_gap import event
from test_progressive import track

class PersistedRemote:
    def __init__(self,root,kind=None,accepted=False):self.path=Path(root)/'remote.json';self.kind=kind;self.accepted=accepted
    def playlist(self,target):
        remote=read_json(self.path);return {'owner':{'id':'me'},'snapshot_id':str(remote['snapshot'])},[{'item':{'id':i}} for i in remote['ids']]
    def recent(self):return read_json(self.path)['events']
    def write_batch(self,target,uris,append=False):
        remote=read_json(self.path);kind='append' if append else 'replace';remote['attempts'][kind]+=1
        interrupt=kind==self.kind
        if not interrupt or self.accepted:
            ids=[u.split(':')[-1] for u in uris];remote['ids']=(remote['ids']+ids) if append else ids;remote['snapshot']+=1
        save_json(self.path,remote)
        if interrupt:os.kill(os.getpid(),signal.SIGKILL)

def pool():return [Track(str(i),str(i),('a'+str(i),),'b'+str(i),('g',)) for i in range(250)]
def child(root,kind,accepted):
    store=SQLiteStore(root);client=PersistedRemote(root,kind,accepted)
    app=Application(client,root,store,library_loader=lambda *a,**k:(pool(),{'source':'v'},0));app.account='me'
    app.apply_preview(app.preview())

@unittest.skipUnless(hasattr(signal,'SIGKILL'),'Requires process-kill support')
class ProcessRecoveryTests(unittest.TestCase):
    def test_replace_and_append_before_and_after_acceptance_survive_actual_process_death(self):
        context=multiprocessing.get_context('fork')
        for kind in ['replace','append']:
            for accepted in [False,True]:
                with self.subTest(kind=kind,accepted=accepted),tempfile.TemporaryDirectory() as folder:
                    root=Path(folder);state,_=bootstrap(pool(),['source'],{'source':'v'},[event(0)],{},[],150 if kind=='append' else 30,0,'me');state.update(status='ready',target='target')
                    play=dict(event(1),track=track(state['queue'][0]['id']),context={'uri':'spotify:playlist:target'})
                    save_json(root/'remote.json',{'ids':[t['id'] for t in state['queue']],'snapshot':0,'events':[event(0),play],'attempts':{'replace':0,'append':0}})
                    store=SQLiteStore(root);store.write('state.json',state);store.close()
                    process=context.Process(target=child,args=(root,kind,accepted));process.start();process.join(20)
                    if process.is_alive():process.kill();process.join();self.fail('Test child did not reach interruption boundary')
                    self.assertEqual(process.exitcode,-signal.SIGKILL)
                    store=SQLiteStore(root)
                    try:
                        pending=store.read('state.json');expected=pending['pending_update'];self.assertEqual(pending['last_event'],state['last_event']);self.assertTrue(pending['write_journal']['uncertain'])
                        app=Application(PersistedRemote(root),root,store);app.account='me';app.recover_write()
                        confirmed=store.read('state.json');remote=read_json(root/'remote.json')
                        self.assertEqual(remote['ids'],[t['id'] for t in expected['queue']]);self.assertEqual(confirmed['history'],expected['history']);self.assertEqual(confirmed['last_event'],expected['last_event']);self.assertEqual(remote['attempts'][kind],1 if accepted else 2)
                        self.assertNotIn('write_journal',confirmed);self.assertNotIn('pending_update',confirmed)
                    finally:store.close()
