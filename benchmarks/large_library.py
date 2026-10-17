"""Offline profiling; each case runs in a killable process with a time limit."""
import argparse
import hashlib
import cProfile
import json
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playlist_engine import Track, Rule, Strength, Config, generate, library_from_playlists


def sources(size, count):
    # Half-overlap between adjacent sources; metadata consistent for shared IDs.
    return {str(s): [Track(str(i), str(i), ('a'+str(i % 200),), 'b'+str(i // 10),
                          ('g'+str(i % 10),))
                     for i in range(s*size//2, s*size//2+size)] for s in range(count)}


def worker(case, checkpoint, profile):
    report = dict(case=case, stage='building_data', status='running')
    def save():
        report['peak_rss_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        checkpoint.write_text(json.dumps(report))
    save()
    start = time.perf_counter()
    data = sources(case['size'], case['sources'])
    report.update(source_entries=sum(map(len, data.values())), data_seconds=time.perf_counter()-start)
    start = time.perf_counter()
    pool = library_from_playlists(data)
    report.update(unique_tracks=len(pool), dedup_seconds=time.perf_counter()-start, stage='generation')
    metrics = {}
    report['engine'] = metrics
    save()
    rules = [Rule('track','track',100), Rule('artist','artist',20), Rule('genre','genre',1)]
    history = []
    if case.get('rules') == 'recovery':
        pool = [Track(t.id,t.name,t.artists,t.album,('__unknown__',),t.sources) for t in pool]
        rules = [Rule('track','track',100), Rule('artist','artist',20), Rule('album','album',10,Strength.OPTIONAL)]
        history = pool[:50]
    report['rules'] = [dict(kind=r.kind,window=r.window,strength=r.strength.value) for r in rules]
    report['history_count'] = len(history)
    profiler = cProfile.Profile() if profile else None
    if profiler:
        profiler.enable()
    result = generate(pool, history, [], rules, case['positions'],
                      Config(seed=7, lookahead_depth=case['depth'],decision_detail=case.get('decision_detail','full')),
                      progress=lambda done,total: save(), metrics=metrics)
    if profiler:
        profiler.disable()
        profiler.dump_stats(profile)
    start = time.perf_counter()
    payload = json.dumps(result.to_dict())
    report.update(log_bytes=len(payload.encode()), log_serialization_seconds=time.perf_counter()-start,
                  generated=len(result.tracks), stop_reason=result.stop_reason, stage='done', status='complete')
    save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='large-library-results.json')
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--profile-directory')
    parser.add_argument('--worker')
    parser.add_argument('--checkpoint')
    parser.add_argument('--profile')
    args = parser.parse_args()
    if args.worker:
        worker(json.loads(args.worker), Path(args.checkpoint), args.profile)
        return
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    cases = [dict(name='baseline',size=1000,sources=1,positions=20,depth=0),
             dict(name='large_starter',size=10000,sources=1,positions=20,depth=0),
             dict(name='overlapping_sources',size=10000,sources=3,positions=20,depth=0),
             dict(name='normal_buffer',size=10000,sources=1,positions=100,depth=0),
             dict(name='current_poc_lookahead',size=10000,sources=1,positions=20,depth=1),
             dict(name='recovery_3504',size=3504,sources=1,positions=100,depth=1,rules='recovery')]
    results = dict(engine_sha256=hashlib.sha256((Path(__file__).resolve().parents[1]/'playlist_engine/engine.py').read_bytes()).hexdigest(),python=sys.version, platform=platform.platform(), seed=7,
                   timeout_seconds=args.timeout, cases=[])
    for case in cases:
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder)/'case.json'
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', json.dumps(case),
                       '--checkpoint', str(checkpoint)]
            if args.profile_directory:
                directory = Path(args.profile_directory)
                directory.mkdir(parents=True, exist_ok=True)
                command += ['--profile', str(directory/(case['name']+'.prof'))]
            start = time.perf_counter()
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
                status = 'complete' if completed.returncode == 0 else 'error'
            except subprocess.TimeoutExpired:
                completed = None
                status = 'timeout'
            try:
                report = json.loads(checkpoint.read_text())
            except (OSError, json.JSONDecodeError):
                report = dict(case=case)
            report.update(status=status, wall_seconds=time.perf_counter()-start)
            if completed and completed.returncode:
                report['error'] = completed.stderr[-2000:]
            results['cases'].append(report)
            Path(args.output).write_text(json.dumps(results, indent=2)+'\n')
            print(case['name'], status, round(report['wall_seconds'],2), 'seconds', flush=True)


if __name__ == '__main__':
    main()
