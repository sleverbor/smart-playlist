"""Pure, bounded playlist scheduler. No service or persistence dependencies."""
from dataclasses import dataclass, field, asdict
from enum import Enum
from functools import lru_cache
import random
from time import perf_counter


class Strength(str, Enum):
    REQUIRED = 'REQUIRED'
    PREFERRED = 'PREFERRED'
    OPTIONAL = 'OPTIONAL'


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    artists: tuple[str, ...]
    album: str
    genres: tuple[str, ...]
    sources: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.id or not self.artists or not self.album or not self.genres:
            raise ValueError('Tracks require identity, artists, album, and enriched genres')
        for key in ('artists', 'genres', 'sources'):
            object.__setattr__(self, key, tuple(sorted(set(getattr(self, key)))))


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    window: int
    strength: Strength = Strength.REQUIRED
    priority: int = 0  # Higher means preserve longer within a strength.
    enabled: bool = True

    def __post_init__(self):
        if self.kind not in ('track', 'artist', 'album', 'genre'):
            raise ValueError('Unknown rule kind')
        if type(self.window) is not int or self.window < 0:
            raise ValueError('window must be a nonnegative integer')
        object.__setattr__(self, 'strength', Strength(self.strength))


@dataclass(frozen=True)
class Config:
    seed: int = 0
    lookahead_depth: int = 2
    lookahead_width: int = 8
    quality_band: float = 0.05
    flexibility_weight: float = 2.0

    def __post_init__(self):
        if type(self.lookahead_depth) is not int or not 0 <= self.lookahead_depth <= 3:
            raise ValueError('lookahead_depth must be 0..3')
        if type(self.lookahead_width) is not int or self.lookahead_width < 1:
            raise ValueError('lookahead_width must be positive')
        if not 0 <= self.quality_band <= 1 or not 0 <= self.flexibility_weight <= 10:
            raise ValueError('invalid scoring configuration')

    def __post_init__(self):
        if not 0 <= self.lookahead_depth <= 3 or not 1 <= self.lookahead_width <= 32:
            raise ValueError('lookahead depth must be 0..3 and width 1..32')
        if not 0 <= self.quality_band <= 1 or self.flexibility_weight < 0:
            raise ValueError('Invalid scoring configuration')


@dataclass
class GenerationResult:
    tracks: list[Track] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    relaxed_rules: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    def to_dict(self):
        return asdict(self)


def library_from_playlists(playlists: dict[str, list[Track]]) -> list[Track]:
    """Deduplicate by canonical ID, retain all memberships; reject metadata conflicts."""
    unique = {}
    memberships = {}
    for source, tracks in playlists.items():
        for track in tracks:
            metadata = (track.name, track.artists, track.album, track.genres)
            if track.id in unique:
                old = unique[track.id]
                if metadata != (old.name, old.artists, old.album, old.genres):
                    raise ValueError(f'Conflicting metadata for {track.id}')
            unique[track.id] = track
            memberships.setdefault(track.id, set()).update((*track.sources, source))
    return [Track(t.id, t.name, t.artists, t.album, t.genres,
                  tuple(memberships[t.id])) for t in sorted(unique.values(), key=lambda t: t.id)]



def values(track, kind):
    return {'track': (track.id,), 'artist': track.artists,
            'album': (track.album,), 'genre': track.genres}[kind]


def violations(track, context, rules):
    """Window N excludes matches in the previous N sequence positions."""
    found = []
    for rule in rules:
        if not rule.enabled or not rule.window:
            continue
        candidate_values = set(values(track, rule.kind))
        for distance, previous in enumerate(reversed(context[-rule.window:]), 1):
            shared = candidate_values.intersection(values(previous, rule.kind))
            if shared:
                found.append({'rule_id': rule.id, 'kind': rule.kind,
                              'strength': rule.strength.value, 'window': rule.window,
                              'distance': distance, 'matched_values': sorted(shared),
                              'previous_track': previous.id})
                break
    return found


def generate(candidates, history, scheduled, rules, count, config=None, progress=None, metrics=None):
    """Append up to count tracks; input history and scheduled are never mutated.

    Historical entries may be outside the candidate library. Scheduled entries are
    a committed prefix, not actual plays. Existing prefix violations are reported,
    never repaired here. Relaxations reset at every generated position.
    """
    started = perf_counter()
    counters = metrics if metrics is not None else {}
    counters.update(candidate_count=0, generated_count=0, eligibility_scans=0,
                    eligibility_track_checks=0, violation_track_checks=0,
                    scored_candidates=0, flexibility_evaluations=0, elapsed_seconds=0.0)
    config = config or Config()
    if type(count) is not int or count < 0:
        raise ValueError('count must be a nonnegative integer')
    rules = tuple(r for r in rules if r.enabled and r.window)
    if len({r.id for r in rules}) != len(rules):
        raise ValueError('Rule IDs must be unique')
    pool = tuple(library_from_playlists({'__input__': list(candidates)}))
    # Remove the internal merging marker from source metadata.
    pool = tuple(Track(t.id, t.name, t.artists, t.album, t.genres,
                       tuple(s for s in t.sources if s != '__input__')) for t in pool)
    counters["candidate_count"] = len(pool)
    history, scheduled = tuple(history), tuple(scheduled)
    context = history + scheduled
    result = GenerationResult()
    rng = random.Random(config.seed)
    required = tuple(r for r in rules if r.strength == Strength.REQUIRED)
    relaxation_order = sorted((r for r in rules if r.strength != Strength.REQUIRED),
                              key=lambda r: (r.strength == Strength.PREFERRED, r.priority, r.id))
    max_window = max((r.window for r in rules), default=0)

    def eligible_tracks(ctx, active):
        counters["eligibility_scans"] += 1
        counters["eligibility_track_checks"] += len(pool)
        blocked = tuple((r.kind, frozenset(v for p in ctx[-r.window:]
                                         for v in values(p, r.kind))) for r in active)
        return [t for t in pool if all(not banned.intersection(values(t, kind))
                                      for kind, banned in blocked)]

    def trim(ctx):
        return ctx[-max_window:] if max_window else ()

    @lru_cache(maxsize=4096)
    def flexibility(ctx, depth):
        counters["flexibility_evaluations"] += 1
        valid = eligible_tracks(ctx, required)
        fraction = len(valid) / len(pool) if pool else 0.0
        if depth <= 1 or not valid:
            return fraction
        # Stable bounded sampling of branches; heuristic, not an exhaustive solver.
        branches = valid[:config.lookahead_width]
        return (fraction + max(flexibility(trim(ctx + (t,)), depth - 1)
                               for t in branches)) / 2

    def recency_score(t, ctx):
        if not ctx:
            return 1.0
        scores = []
        for kind in ('track', 'artist', 'album', 'genre'):
            distance = next((i for i, p in enumerate(reversed(ctx), 1)
                             if set(values(t, kind)).intersection(values(p, kind))),
                            len(ctx) + 1)
            scores.append(distance / (len(ctx) + 1))
        return sum(scores) / len(scores)

    prefix_issues = []
    prefix = history
    for position, t in enumerate(scheduled):
        issues = violations(t, prefix, rules)
        if issues:
            prefix_issues.append({'position': position, 'track': t.id, 'violations': issues})
        prefix += (t,)

    for position in range(count):
        counters["violation_track_checks"] += len(pool)
        checks = {t.id: violations(t, context, rules) for t in pool}
        active = list(rules)
        relaxed = []
        attempts = []
        while True:
            eligible = eligible_tracks(context, active)
            attempts.append({'active_rules': [r.id for r in active], 'eligible_count': len(eligible)})
            if eligible or len(relaxed) == len(relaxation_order):
                break
            r = relaxation_order[len(relaxed)]
            active.remove(r)
            relaxed.append(r.id)
        record = {'position': position, 'history_count': len(history),
                  'scheduled_prefix_count': len(scheduled), 'generated_prefix_count': len(result.tracks),
                  'attempts': attempts, 'relaxed_rules': list(relaxed),
                  'candidate_violations': checks, 'prefix_issues': prefix_issues if position == 0 else []}
        if not eligible:
            result.stop_reason = 'empty_library' if not pool else 'required_constraints_impossible'
            record.update(selected=None, stop_reason=result.stop_reason)
            result.decisions.append(record)
            break
        scored = []
        counters["scored_candidates"] += len(eligible)
        for t in eligible:
            future = flexibility(trim(context + (t,)), config.lookahead_depth) if config.lookahead_depth else 0.0
            recency = recency_score(t, context)
            recent_sources = {s for p in context[-10:] for s in p.sources}
            source_diversity = len(set(t.sources) - recent_sources) / max(1, len(t.sources))
            score = recency + 0.25 * source_diversity + config.flexibility_weight * future
            scored.append({'track': t.id, 'recency': recency, 'source_diversity': source_diversity, 'future_flexibility': future, 'score': score})
        best = max(s['score'] for s in scored)
        band = config.quality_band * (1 + config.flexibility_weight)
        shortlist = [t for t, s in zip(eligible, scored) if s['score'] >= best - band]
        chosen = rng.choice(shortlist)
        record.update(selected=chosen.id, scores=scored, shortlist=[t.id for t in shortlist],
                      selected_violations=checks[chosen.id],
                      reason='seeded_uniform_choice_within_quality_band', seed=config.seed)
        result.decisions.append(record)
        result.tracks.append(chosen)
        for rule_id in relaxed:
            if rule_id not in result.relaxed_rules:
                result.relaxed_rules.append(rule_id)
        context += (chosen,)
        counters["generated_count"] = len(result.tracks)
        counters["elapsed_seconds"] = perf_counter() - started
        if progress is not None:
            progress(len(result.tracks), count)
    counters["elapsed_seconds"] = perf_counter() - started
    counters["flexibility_cache"] = flexibility.cache_info()._asdict()
    return result
