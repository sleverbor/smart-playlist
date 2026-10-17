"""Generate synthetic sources, inputs, and an inspectable scheduling result."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path
from .engine import Track, Rule, Strength, Config, generate, library_from_playlists


def synthetic_sources(size=1200):
    tracks = [Track(f't{i:04}', f'Synthetic song {i}',
                    (f'a{i % 240:03}',) + ((f'a{(i + 73) % 240:03}',) if i % 17 == 0 else ()),
                    f'album{i // 4:03}', (f'genre{i % 6}',)) for i in range(size)]
    return {'Favorites': tracks[:size * 3 // 4],
            'Driving': tracks[size // 4:],
            'Discoveries': tracks[::3]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('examples'))
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--seed', type=int, default=7)
    args = parser.parse_args()
    sources = synthetic_sources()
    library = library_from_playlists(sources)
    rules = [Rule('track100', 'track', 100), Rule('artist20', 'artist', 20),
             Rule('genre1', 'genre', 1, Strength.PREFERRED),
             Rule('album10', 'album', 10, Strength.OPTIONAL)]
    # History contains actual plays; scheduled remains a separate committed prefix.
    history = library[:5]
    scheduled = generate(library, history, [], rules, 5, Config(seed=2, lookahead_depth=1)).tracks
    result = generate(library, history, scheduled, rules, args.count,
                      Config(seed=args.seed, lookahead_depth=1))
    args.output.mkdir(parents=True, exist_ok=True)
    documents = {'sample_library.json': {s: [asdict(t) for t in ts] for s, ts in sources.items()},
                 'rules.json': [asdict(r) for r in rules],
                 'state.json': {'history': [asdict(t) for t in history], 'scheduled': [asdict(t) for t in scheduled]},
                 'generated_playlist.json': {'tracks': [asdict(t) for t in result.tracks],
                                             'relaxed_rules': result.relaxed_rules, 'stop_reason': result.stop_reason},
                 'decision_log.json': result.decisions}
    for name, document in documents.items():
        (args.output / name).write_text(json.dumps(document, indent=2) + '\n')
    print(f'{len(library)} unique tracks; {len(result.tracks)} generated; stop={result.stop_reason}')


if __name__ == '__main__':
    main()
