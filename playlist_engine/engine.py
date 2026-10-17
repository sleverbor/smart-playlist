"""Pure, bounded playlist scheduler. No service or persistence dependencies."""
from dataclasses import dataclass, field, asdict, replace
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
    aliases: tuple[str, ...] = ()
    recording_id: str = ""
    duration_ms: int = 0
    artist_names: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.id or not self.artists or not self.album or not self.genres:
            raise ValueError('Tracks require identity, artists, album, and enriched genres')
        for key in ('artists', 'genres', 'sources', 'aliases','artist_names'):
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
    decision_detail: str = 'full'

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
        if self.decision_detail not in ('full', 'compact'):
            raise ValueError('decision_detail must be full or compact')


@dataclass
class GenerationResult:
    tracks: list[Track] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    relaxed_rules: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    def to_dict(self):
        result = asdict(self)
        for track in result['tracks']:
            if not track['aliases']: track.pop('aliases')
            if not track['recording_id']: track.pop('recording_id')
            if not track['duration_ms']: track.pop('duration_ms')
            if not track['artist_names']: track.pop('artist_names')
        return result


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
    return [replace(t, sources=tuple(memberships[t.id])) for t in sorted(unique.values(), key=lambda t: t.id)]



def values(track, kind):
    return {'track': (track.id, *track.aliases, *(("recording:"+track.recording_id,) if track.recording_id else ())), 'artist': track.artists,
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
    pool = tuple(replace(t, sources=tuple(s for s in t.sources if s != '__input__')) for t in pool)
    counters["candidate_count"] = len(pool)
    history, scheduled = tuple(history), tuple(scheduled)
    context = history + scheduled
    result = GenerationResult()
    rng = random.Random(config.seed)
    required = tuple(r for r in rules if r.strength == Strength.REQUIRED)
    relaxation_order = sorted((r for r in rules if r.strength != Strength.REQUIRED),
                              key=lambda r: (r.strength == Strength.PREFERRED, r.priority, r.id))
    max_window = max((r.window for r in rules), default=0)

    # An inverted index maps each attribute value to candidate positions.
    # Python integer bitsets make union/cardinality operations run in native code.
    kinds = ('track', 'artist', 'album', 'genre')
    index = {kind: {} for kind in kinds}
    for position, track in enumerate(pool):
        bit = 1 << position
        for kind in kinds:
            for value in values(track, kind):
                index[kind][value] = index[kind].get(value, 0) | bit
    all_candidates = (1 << len(pool)) - 1
    counters.update(bitset_unions=0, context_index_builds=0)

    def attribute_mask(track, kind):
        mask = 0
        for value in values(track, kind):
            mask |= index[kind].get(value, 0)
            counters['bitset_unions'] += 1
        return mask

    required_candidate_masks = {
        track.id: 0 for track in pool
    }
    for track in pool:
        for kind in {rule.kind for rule in required}:
            required_candidate_masks[track.id] |= attribute_mask(track, kind)

    def eligible_mask(ctx, active):
        counters['eligibility_scans'] += 1
        blocked = 0
        for rule in active:
            for previous in ctx[-rule.window:]:
                blocked |= attribute_mask(previous, rule.kind)
        return all_candidates & ~blocked

    def tracks_in(mask, limit=None):
        emitted = 0
        while mask and (limit is None or emitted < limit):
            bit = mask & -mask
            yield pool[bit.bit_length() - 1]
            mask ^= bit
            emitted += 1

    def trim(ctx):
        return ctx[-max_window:] if max_window else ()

    @lru_cache(maxsize=4096)
    def future_base(prefix):
        # After appending one candidate, only the preceding window-1 positions remain.
        blocked = 0
        for rule in required:
            if rule.window > 1:
                for previous in prefix[-(rule.window - 1):]:
                    blocked |= attribute_mask(previous, rule.kind)
        return blocked

    @lru_cache(maxsize=4096)
    def flexibility(ctx, depth):
        counters['flexibility_evaluations'] += 1
        if ctx:
            last = ctx[-1]
            blocked = future_base(ctx[:-1]) | required_candidate_masks[last.id]
            mask = all_candidates & ~blocked
            counters['eligibility_scans'] += 1
        else:
            mask = eligible_mask(ctx, required)
        fraction = mask.bit_count() / len(pool) if pool else 0.0
        if depth <= 1 or not mask:
            return fraction
        branches = tracks_in(mask, config.lookahead_width)
        return (fraction + max(flexibility(trim(ctx + (track,)), depth - 1)
                               for track in branches)) / 2

    def context_index(ctx):
        counters['context_index_builds'] += 1
        nearest = {kind: {} for kind in kinds}
        for distance, previous in enumerate(reversed(ctx), 1):
            for kind in kinds:
                for value in values(previous, kind):
                    nearest[kind].setdefault(value, distance)
        return nearest

    def indexed_violations(track, ctx, nearest):
        found = []
        for rule in rules:
            distance = min((nearest[rule.kind].get(value, len(ctx) + 1)
                            for value in values(track, rule.kind)), default=len(ctx) + 1)
            if distance <= min(rule.window, len(ctx)):
                previous = ctx[-distance]
                found.append(dict(rule_id=rule.id, kind=rule.kind, strength=rule.strength.value,
                                  window=rule.window, distance=distance,
                                  matched_values=sorted(set(values(track, rule.kind)).intersection(values(previous, rule.kind))),
                                  previous_track=previous.id))
        return found

    def recency_score(track, ctx, nearest):
        if not ctx:
            return 1.0
        scores = []
        for kind in kinds:
            distance = min((nearest[kind].get(value, len(ctx) + 1)
                            for value in values(track, kind)), default=len(ctx) + 1)
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
        nearest = context_index(context)
        checks = {}
        if config.decision_detail == 'full':
            counters['violation_track_checks'] += len(pool)
            checks = {t.id: indexed_violations(t, context, nearest) for t in pool}
        active = list(rules)
        relaxed = []
        attempts = []
        while True:
            eligible = list(tracks_in(eligible_mask(context, active)))
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
        if config.decision_detail == 'compact':
            record.pop('candidate_violations')
            record.update(decision_detail='compact', candidate_count=len(pool),
                          blocked_by_rule={rule.id: len(pool) - eligible_mask(context, (rule,)).bit_count() for rule in rules})
        if not eligible:
            result.stop_reason = 'empty_library' if not pool else 'required_constraints_impossible'
            record.update(selected=None, stop_reason=result.stop_reason)
            result.decisions.append(record)
            break
        scored = []
        counters["scored_candidates"] += len(eligible)
        recent_sources = {s for previous in context[-10:] for s in previous.sources}
        next_blocked = future_base(trim(context)) if config.lookahead_depth == 1 else 0
        for t in eligible:
            if config.lookahead_depth == 1:
                counters['flexibility_evaluations'] += 1
                counters['eligibility_scans'] += 1
                future = (all_candidates & ~(next_blocked | required_candidate_masks[t.id])).bit_count() / len(pool)
            else:
                future = flexibility(trim(context + (t,)), config.lookahead_depth) if config.lookahead_depth else 0.0
            recency = recency_score(t, context, nearest)
            source_diversity = len(set(t.sources) - recent_sources) / max(1, len(t.sources))
            score = recency + 0.25 * source_diversity + config.flexibility_weight * future
            scored.append({'track': t.id, 'recency': recency, 'source_diversity': source_diversity, 'future_flexibility': future, 'score': score})
        best = max(s['score'] for s in scored)
        band = config.quality_band * (1 + config.flexibility_weight)
        shortlist = [t for t, s in zip(eligible, scored) if s['score'] >= best - band]
        chosen = rng.choice(shortlist)
        if config.decision_detail == 'compact':
            counters['violation_track_checks'] += 1
            checks[chosen.id] = indexed_violations(chosen, context, nearest)
        record.update(selected=chosen.id, scores=scored, shortlist=[t.id for t in shortlist],
                      selected_violations=checks[chosen.id],
                      reason='seeded_uniform_choice_within_quality_band', seed=config.seed)
        if config.decision_detail == 'compact':
            record['scores'] = [score for score in scored if score['track'] == chosen.id]
            record['shortlist_count'] = len(shortlist)
            record.pop('shortlist')
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
