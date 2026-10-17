import unittest
from playlist_engine import Config, Rule, generate, library_from_playlists
from benchmarks.large_library import sources
from spotify_poc.client import Client
from spotify_poc.sync import library


class LargeLibraryTests(unittest.TestCase):
    def test_three_maximum_sources_deduplicate_to_twenty_thousand(self):
        pool=library_from_playlists(sources(10000,3))
        self.assertEqual(len(pool),20000)
        shared=next(t for t in pool if t.id=='10000')
        self.assertEqual(set(shared.sources),{'1','2'})

    def test_ten_thousand_entry_source_imports_all_two_hundred_pages(self):
        client=object.__new__(Client)
        reads=[]
        def request(method,path):
            if '/items' not in path:
                return {'snapshot_id':'stable'}
            page=int(path.split('page=')[-1]) if 'page=' in path else 0
            reads.append(page)
            rows=[{'item':dict(type='track',id=str(i),name=str(i),
                       artists=[{'id':'a'+str(i%200)}],album={'id':'b'+str(i//10)})}
                  for i in range(page*50,(page+1)*50)]
            return dict(items=rows,total=10000,
                        next=f'/playlists/source/items?page={page+1}' if page<199 else None)
        client.request=request
        pool,snapshots,skipped=library(client,['source'],{})
        self.assertEqual(len(pool),10000)
        self.assertEqual(reads,list(range(200)))
        self.assertEqual(skipped,0)
        self.assertEqual(snapshots,{'source':'stable'})

    def test_instrumentation_preserves_decisions_and_counts_work(self):
        pool=library_from_playlists(sources(100,1))
        rules=[Rule('track','track',100),Rule('artist','artist',20),Rule('genre','genre',1)]
        metrics={}
        config=Config(seed=7,lookahead_depth=0)
        measured=generate(pool,[],[],rules,5,config,metrics=metrics)
        plain=generate(pool,[],[],rules,5,config)
        self.assertEqual(measured.to_dict(),plain.to_dict())
        self.assertEqual(metrics['generated_count'],5)
        self.assertEqual(metrics['violation_track_checks'],500)
        self.assertEqual(metrics['candidate_count'],100)
        self.assertGreater(metrics['elapsed_seconds'],0)
