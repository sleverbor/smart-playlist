"""Passive observer for a browser-driven user acceptance session. No Spotify calls."""
import argparse,json,sqlite3,time
from pathlib import Path
from urllib.request import urlopen

def sample(project,url,pid):
    started=time.perf_counter()
    with urlopen(url+'/api/state',timeout=5) as response:state=json.load(response)
    elapsed=time.perf_counter()-started
    # Never capture credentials, OAuth URLs, account IDs, track titles or raw jobs.
    jobs=[]
    for j in state.get('jobs',[]):
        p=j.get('progress') or {};ready=j.get('ready') or {}
        jobs.append(dict(id=j['id'],action=j['action'],status=j['status'],created=j.get('created'),finished=j.get('finished'),stage=p.get('stage'),elapsed=p.get('elapsed_seconds'),completed=p.get('completed'),total=p.get('total'),unique_candidates=p.get('unique_candidates'),ready_songs=ready.get('queue_size'),setup_status=(j.get('data') or {}).get('setup_status')))
    memory={}
    if pid:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith(('VmRSS:','VmHWM:')):
                key,value,*_=line.split();memory[key.rstrip(':')+'_kib']=int(value)
    metrics=[]
    database=project/'.spotify-accounts/application.sqlite'
    if database.exists():
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
            for workspace,payload in db.execute("SELECT workspace,payload FROM documents WHERE name='api-metrics.json'"):
                import hashlib
                metrics.append(dict(mix=hashlib.sha256(workspace.encode()).hexdigest()[:24],counters=json.loads(payload)))
    return dict(timestamp=time.time(),local_state_response_seconds=elapsed,memory=memory,jobs=jobs,mixes=[dict(id=m['id'],queue_size=m['queue_size'],status=m['status'],setup_status=m.get('setup_status'),imported=m['imported']) for m in state['mixes']],api_metrics=metrics)

def main():
    p=argparse.ArgumentParser();p.add_argument('--project',type=Path,default=Path.cwd());p.add_argument('--url',default='http://127.0.0.1:8766');p.add_argument('--pid',type=int);p.add_argument('--seconds',type=float,default=1800);p.add_argument('--output',type=Path,default=Path('benchmarks/user-session.jsonl'));a=p.parse_args()
    if a.seconds<=0: p.error('--seconds must be positive')
    if a.url.rstrip('/')!='http://127.0.0.1:8766':p.error('Observer is restricted to the local app address')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    print('Observing local UI state; use the browser and Spotify for test actions. Ctrl+C stops recording.',flush=True)
    end=time.monotonic()+a.seconds
    with a.output.open('a') as output:
        try:
            while time.monotonic()<end:
                try:row=sample(a.project.resolve(),a.url.rstrip('/'),a.pid)
                except Exception as exc:row=dict(timestamp=time.time(),observer_error=type(exc).__name__)
                output.write(json.dumps(row)+'\n');output.flush();time.sleep(min(1,max(0,end-time.monotonic())))
        except KeyboardInterrupt:pass
if __name__=='__main__':main()
