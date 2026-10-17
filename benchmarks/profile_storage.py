"""Offline storage profile: two overlapping 10,000-entry sources, 50-item pages."""
import json,resource,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from spotify_poc.storage import SQLiteStore

with tempfile.TemporaryDirectory() as tmp:
    store=SQLiteStore(tmp)
    job=dict(imports={source:dict(snapshot='v1',tracks=[]) for source in ['a','b']})
    start=time.perf_counter()
    for page in range(200):
        for source,offset in [('a',0),('b',5000)]:
            job['imports'][source]['tracks'].extend(dict(id=str(i),artists=[dict(id='artist'+str(i%200))],album=dict(id='album'+str(i//10))) for i in range(offset+page*50,offset+(page+1)*50))
            store.write('setup.json',job)
    imported=time.perf_counter()-start
    start=time.perf_counter();loaded=store.read('setup.json');read_seconds=time.perf_counter()-start
    assert sum(len(e['tracks']) for e in loaded['imports'].values())==20000
    assert store.db.execute('SELECT count(*) FROM tracks').fetchone()[0]==15000
    changes=store.db.total_changes;store.write('setup.json',job)
    assert store.db.total_changes-changes==3
    result=dict(source_entries=20000,unique_tracks=15000,pages=400,import_seconds=imported,read_seconds=read_seconds,unchanged_checkpoint_sql_changes=3,peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)
    store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    result['database_mib']=store.path.stat().st_size/1024**2
    store.close();print(json.dumps(result,indent=2))
