import random
import unittest
from benchmarks import reference_engine as old
from playlist_engine import Track, Rule, Config, generate


class IndexedEngineTests(unittest.TestCase):
    def test_exact_decision_parity_with_scan_engine(self):
        for seed in range(12):
            rng=random.Random(seed)
            pool=[Track(str(i),str(i),tuple({'a'+str(rng.randrange(6)) for _ in range(2)}),
                        'b'+str(rng.randrange(5)),tuple({'g'+str(rng.randrange(4)) for _ in range(2)}),
                        ('s'+str(i%3),)) for i in range(14)]
            history=[pool[2],Track('external','external',('a2',),'b1',('g1',)),pool[2]]
            scheduled=[pool[4],pool[5]]
            rules=[Rule('track','track',5),Rule('artist','artist',2,'PREFERRED'),
                   Rule('album','album',3,'OPTIONAL'),Rule('genre','genre',1,'PREFERRED')]
            if seed%2:
                rules[1]=Rule('artist','artist',2)
            for depth in range(4):
                with self.subTest(seed=seed,depth=depth):
                    config=Config(seed=seed,lookahead_depth=depth,lookahead_width=2)
                    new=generate(pool,history,scheduled,rules,6,config).to_dict()
                    baseline=old.generate(pool,history,scheduled,rules,6,config).to_dict()
                    self.assertEqual(new,baseline)

    def test_required_genre_multi_artist_and_duplicate_rule_kinds(self):
        pool=[Track(str(i),str(i),('a'+str(i%4),'a'+str((i+1)%4)),
                    'b'+str(i%3),('g'+str(i%3),)) for i in range(12)]
        rules=[Rule('t','track',100),Rule('a','artist',1),Rule('g','genre',1),Rule('g2','genre',2,'OPTIONAL')]
        for depth in (1,2,3):
            config=Config(seed=9,lookahead_depth=depth,lookahead_width=2)
            self.assertEqual(generate(pool,pool[:2],[],rules,10,config).to_dict(),
                             old.generate(pool,pool[:2],[],rules,10,config).to_dict())

    def test_large_depth_one_uses_no_full_pool_eligibility_checks(self):
        pool=[Track(str(i),str(i),('a'+str(i%200),),'b'+str(i//10),('g'+str(i%10),)) for i in range(10000)]
        metrics={}
        result=generate(pool,[],[],[Rule('t','track',100),Rule('a','artist',20),Rule('g','genre',1)],
                        2,Config(seed=1,lookahead_depth=1),metrics=metrics)
        self.assertEqual(len(result.tracks),2)
        self.assertEqual(metrics['eligibility_track_checks'],0)
        self.assertEqual(metrics['context_index_builds'],2)

    def test_compact_logs_preserve_choices_and_selected_explanations(self):
        import json
        pool=[Track(str(i),str(i),('a'+str(i%200),),'b'+str(i//10),('g'+str(i%10),)) for i in range(1000)]
        rules=[Rule('t','track',100),Rule('a','artist',20),Rule('g','genre',1)]
        full=generate(pool,[],[],rules,5,Config(seed=7,lookahead_depth=1))
        compact=generate(pool,[],[],rules,5,Config(seed=7,lookahead_depth=1,decision_detail='compact'))
        self.assertEqual(full.tracks,compact.tracks)
        for expected,actual in zip(full.decisions,compact.decisions):
            for key in ('attempts','relaxed_rules','selected','selected_violations','reason','seed'):
                self.assertEqual(expected[key],actual[key])
            self.assertEqual(actual['scores'],[s for s in expected['scores'] if s['track']==expected['selected']])
            self.assertEqual(actual['shortlist_count'],len(expected['shortlist']))
        self.assertLess(len(json.dumps(compact.to_dict())),15000)
