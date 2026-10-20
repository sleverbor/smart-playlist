import unittest
from playlist_engine import *


def track(id, artist=None, genre='rock', album=None, sources=()):
    return Track(id, id, (artist or id,), album or id, (genre,), sources)


class EngineTests(unittest.TestCase):
    def test_core_100_20_1(self):
        tracks = [Track(f't{i:04}', f'Song {i}',
                        (f'a{i % 240:03}',), f'album{i // 4:03}', (f'genre{i % 6}',))
                  for i in range(300)]
        library = library_from_playlists({
            'Favorites': tracks[:225],
            'Driving': tracks[75:],
            'Discoveries': tracks[::3],
        })
        rules = [Rule('t', 'track', 100), Rule('a', 'artist', 20), Rule('g', 'genre', 1)]
        history = library[:10]
        queued = generate(library, history, [], rules, 10, Config(lookahead_depth=1)).tracks
        result = generate(library, history, queued, rules, 120, Config(lookahead_depth=1))
        self.assertEqual(len(result.tracks), 120)
        context = history + queued
        for t in result.tracks:
            self.assertEqual(violations(t, context, rules), [])
            context.append(t)
        self.assertIsNone(result.stop_reason)
        self.assertEqual(result.relaxed_rules, [])

    def test_dedup_membership_no_duplicate_bias(self):
        a, b = track('a'), track('b')
        lib = library_from_playlists({'one': [a, a, b], 'two': [a]})
        self.assertEqual(len(lib), 2)
        self.assertEqual(lib[0].sources, ('one', 'two'))
        self.assertEqual(generate(lib, [], [], [], 5).tracks,
                         generate(lib + lib, [], [], [], 5).tracks)

    def test_conflicting_duplicates(self):
        with self.assertRaises(ValueError):
            library_from_playlists({'p': [track('a'), track('a', genre='jazz')]})

    def test_future_blocks_even_without_history(self):
        a, b = track('a'), track('b')
        rules = [Rule('t', 'track', 1)]
        result = generate([a, b], [], [a], rules, 1)
        self.assertEqual(result.tracks, [b])

    def test_window_boundaries(self):
        a, b, c = track('a'), track('b'), track('c')
        r = [Rule('t', 'track', 2)]
        self.assertTrue(violations(a, [a, b], r))
        self.assertFalse(violations(a, [a, b, c], r))

    def test_all_attribute_intersections(self):
        a = Track('a', 'a', ('x', 'y'), 'album', ('rock', 'jazz'))
        b = Track('b', 'b', ('z', 'y'), 'album', ('jazz', 'pop'))
        rules = [Rule(k, k, 1) for k in ('artist', 'album', 'genre')]
        self.assertEqual({v['kind'] for v in violations(b, [a], rules)}, {'artist', 'album', 'genre'})

    def test_relax_order_priority_required(self):
        a = track('a')
        rules = [Rule('preferred', 'genre', 1, Strength.PREFERRED),
                 Rule('optionalHigh', 'album', 1, Strength.OPTIONAL, 5),
                 Rule('optionalLow', 'artist', 1, Strength.OPTIONAL, 0)]
        result = generate([a], [a], [], rules, 1)
        self.assertEqual(result.relaxed_rules, ['optionalLow', 'optionalHigh', 'preferred'])
        self.assertEqual(len(result.tracks), 1)
        rules.append(Rule('required', 'track', 1))
        stopped = generate([a], [a], [], rules, 1)
        self.assertEqual(stopped.tracks, [])
        self.assertEqual(stopped.stop_reason, 'required_constraints_impossible')
        self.assertIsNone(stopped.decisions[0]['selected'])

    def test_tiny_pool_partial_stop(self):
        result = generate([track('a'), track('b')], [], [], [Rule('t', 'track', 100)], 5)
        self.assertEqual(len(result.tracks), 2)
        self.assertEqual(result.stop_reason, 'required_constraints_impossible')

    def test_empty_and_zero(self):
        self.assertEqual(generate([], [], [], [], 1).stop_reason, 'empty_library')
        self.assertEqual(generate([], [], [], [], 0).decisions, [])

    def test_relaxation_resets(self):
        a, b = track('a'), track('b', genre='jazz')
        r = [Rule('g', 'genre', 1, Strength.PREFERRED)]
        result = generate([a], [a], [], r, 1)
        self.assertEqual(result.relaxed_rules, ['g'])
        result = generate([a, b], [a], [], r, 1)
        self.assertEqual(result.relaxed_rules, [])

    def test_seed_and_input_order(self):
        pool = [track(str(i)) for i in range(10)]
        a = generate(pool, [], [], [], 10, Config(seed=1))
        b = generate(list(reversed(pool)), [], [], [], 10, Config(seed=1))
        c = generate(pool, [], [], [], 10, Config(seed=2))
        self.assertEqual(a, b)
        self.assertNotEqual(a.tracks, c.tracks)

    def test_lookahead_avoids_dead_end(self):
        # A shares x with B and y with C: picking A blocks everyone next.
        a = Track('a', 'a', ('x', 'y'), 'a', ('rock',))
        b, c = track('b', 'x'), track('c', 'y')
        rules = [Rule('artist', 'artist', 1)]
        result = generate([a, b, c], [], [], rules, 3,
                          Config(seed=0, lookahead_depth=1, quality_band=0))
        self.assertNotEqual(result.tracks[0], a)
        self.assertEqual(len(result.tracks), 3)
        self.assertGreater(result.decisions[0]['scores'][1]['future_flexibility'], 0)

    def test_bounded_depth_two_and_three(self):
        pool = [track('a'), track('b', genre='jazz'), track('c')]
        rules = [Rule('t', 'track', 1)]
        for depth in (2, 3):
            result = generate(pool, [], [], rules, 4,
                              Config(lookahead_depth=depth, lookahead_width=2))
            self.assertEqual(len(result.tracks), 4)
            self.assertTrue(all(0 <= s['future_flexibility'] <= 1
                                for d in result.decisions for s in d['scores']))

    def test_refill_matches_unconsumed_timeline(self):
        pool = [track(str(i)) for i in range(8)]
        rules = [Rule('t', 'track', 3)]
        original = generate(pool, [], [], rules, 5).tracks
        # Consuming prefix changes status, not its order in the effective timeline.
        a = generate(pool, [], original, rules, 3)
        b = generate(pool, original[:2], original[2:], rules, 3)
        self.assertEqual(a.tracks, b.tracks)


    def test_immutable_inputs_prefix_warning(self):
        a = track('a')
        history, queued = [a], [a]
        result = generate([a], history, queued, [Rule('t', 'track', 1)], 1)
        self.assertEqual(history, [a])
        self.assertEqual(queued, [a])
        self.assertTrue(result.decisions[0]['prefix_issues'])

    def test_disabled_zero_validation(self):
        a = track('a')
        result = generate([a], [a], [], [Rule('zero', 'track', 0), Rule('off', 'track', 10, enabled=False)], 2)
        self.assertEqual(len(result.tracks), 2)
        for call in (lambda: Rule('x', 'bad', 1), lambda: Rule('x', 'track', -1),
                     lambda: Config(lookahead_depth=4), lambda: generate([], [], [], [], -1)):
            with self.assertRaises(ValueError):
                call()


if __name__ == '__main__':
    unittest.main()
