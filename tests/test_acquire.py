import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from supplytree.acquire import (Acquirer, AcquireError, REL_INITIAL,
                                REL_FAST_FORWARD, REL_MULTI_JUMP, REL_REWOUND,
                                REL_DIVERGED, REL_UP_TO_DATE,
                                REL_DISAPPEARED, REL_MISSING)
from supplytree import gitread
from scripts import scenarios
from scripts.scenarios import RepoBuilder, PRODUCT_P1, PRODUCT_P2, OUTLET_S1


class AcquireTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / 'state'

    def tearDown(self):
        self._tmp.cleanup()

    def acquire(self, source_name='supplier'):
        return Acquirer(self.state)

    def register(self, acquirer, repo, name='supplier', branch='catalog'):
        return acquirer.add_source(name, repo, branch, 'products', 'outlets')

    # ------------------------------------------------------- identity / movement
    def test_moved_product_keeps_business_identity_and_blob(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        builder = RepoBuilder(spec['repository'])
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            builder.reset_hard(spec['initial'])
            first = acquirer.observe('supplier')
            self.assertEqual(first['relationship'], REL_INITIAL)
            self.assertEqual(first['commit_sha'], spec['initial'])
            acquirer.adopt('supplier', first['id'])
            view_initial = acquirer.current_view('supplier')
            first_record = next(r for r in view_initial['records']
                                if r['kind'] == 'product' and r['business_id'] == 'P1')

            builder.reset_hard(spec['moved'])
            second = acquirer.observe('supplier')
            self.assertEqual(second['relationship'], REL_FAST_FORWARD)
            self.assertEqual(second['commit_sha'], spec['moved'])
            acquirer.adopt('supplier', second['id'])
            view_moved = acquirer.current_view('supplier')
            moved_record = next(r for r in view_moved['records']
                                if r['kind'] == 'product' and r['business_id'] == 'P1')

            self.assertEqual(moved_record['business_id'], 'P1')
            self.assertNotEqual(first_record['path'], moved_record['path'])
            self.assertIn('\n', first_record['path'])
            self.assertEqual(first_record['git_blob'], moved_record['git_blob'])
            blob_uses = acquirer.provenance(git_sha=moved_record['git_blob'])
            self.assertGreaterEqual(len(blob_uses['references']), 2)

    def test_exotic_paths_are_not_split_or_truncated(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        builder = RepoBuilder(spec['repository'])
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            builder.reset_hard(spec['initial'])
            observation = acquirer.observe('supplier')
            acquirer.adopt('supplier', observation['id'])
            view = acquirer.current_view('supplier')
            paths = {r['path'] for r in view['records']}
            self.assertIn('outlets/store with space.json', paths)
            self.assertTrue(any('\n' in path or '商品' in path for path in paths))

    # ------------------------------------------------- observation classification
    def test_fast_forward_multi_jump_and_up_to_date(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        builder = RepoBuilder(spec['repository'])
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            builder.reset_hard(spec['initial'])
            o1 = acquirer.observe('supplier')  # branch at initial
            self.assertEqual(o1['relationship'], REL_INITIAL)
            self.assertEqual(o1['commit_sha'], spec['initial'])
            o2 = acquirer.observe('supplier')
            self.assertEqual(o2['relationship'], REL_UP_TO_DATE)
            acquirer.adopt('supplier', o1['id'])

            builder.reset_hard(spec['moved'])
            o3 = acquirer.observe('supplier')
            self.assertEqual(o3['relationship'], REL_FAST_FORWARD)

            builder.reset_hard(spec['initial'])
            o4 = acquirer.observe('supplier')
            self.assertEqual(o4['relationship'], REL_REWOUND)

            # jumped is two commits past initial: a multi-commit jump.
            builder.reset_hard(spec['jumped'])
            o5 = acquirer.observe('supplier')
            self.assertEqual(o5['relationship'], REL_MULTI_JUMP)
            self.assertIn('2 commits were skipped', o5['note'])

            # A rewind after a jump is an independent generation, not an update.
            builder.reset_hard(spec['initial'])
            o6 = acquirer.observe('supplier')
            self.assertEqual(o6['relationship'], REL_REWOUND)
            self.assertTrue(o6['independent'])
            with self.assertRaises(AcquireError):
                acquirer.adopt('supplier', o6['id'])

    def test_multi_commit_jump_from_watermark(self):
        repo = self.tmp / 'repo'
        builder = RepoBuilder(repo)
        builder.write_json('outlets/s.json', OUTLET_S1)
        builder.write_json('products/p.json', PRODUCT_P1)
        c1 = builder.commit('one')
        builder.write_json('products/p2.json', PRODUCT_P2)
        c2 = builder.commit('two')
        builder.write_json('products/p3.json',
                           {'kind': 'product', 'id': 'P3', 'name': '三',
                            'outletIds': ['S1']})
        c3 = builder.commit('three')
        with self.acquire() as acquirer:
            self.register(acquirer, repo)
            # Adopt c1 directly by observing while the branch points at c1.
            builder.reset_hard(c1)
            o1 = acquirer.observe('supplier')
            acquirer.adopt('supplier', o1['id'])
            builder.reset_hard(c3)
            o2 = acquirer.observe('supplier')
            self.assertEqual(o2['relationship'], REL_MULTI_JUMP)
            self.assertEqual(o2['commit_sha'], c3)
            result = acquirer.adopt('supplier', o2['id'])
            self.assertEqual(result['products'], 3)

    def test_rewind_requires_confirmation_and_keeps_old_view(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        builder = RepoBuilder(spec['repository'])
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            o1 = acquirer.observe('supplier')  # jumped
            acquirer.adopt('supplier', o1['id'])
            full_view = acquirer.current_view('supplier')
            self.assertEqual(len(full_view['records']), 3)  # P1, P2, S1

            builder.reset_hard(spec['initial'])
            o_rewind = acquirer.observe('supplier')
            self.assertEqual(o_rewind['relationship'], REL_REWOUND)
            self.assertTrue(o_rewind['independent'])
            self.assertIsNone(o_rewind['generation'])
            with self.assertRaises(AcquireError):
                acquirer.adopt('supplier', o_rewind['id'])

            # The old full directory remains readable; nothing was deleted.
            unchanged = acquirer.current_view('supplier')
            self.assertEqual(unchanged['adoption']['commit_sha'], spec['jumped'])
            self.assertEqual(len(unchanged['records']), 3)

            confirmed = acquirer.confirm_generation('supplier', o_rewind['id'])
            self.assertTrue(confirmed['confirmed'])
            self.assertEqual(confirmed['generation'], 2)
            result = acquirer.adopt('supplier', confirmed['id'])
            self.assertFalse(result['already_adopted'])
            switched = acquirer.current_view('supplier')
            self.assertEqual(switched['adoption']['commit_sha'], spec['initial'])
            self.assertEqual(len(switched['records']), 2)  # P1, S1 only

    def test_diverged_branch_with_older_timestamp_is_independent_generation(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        builder = RepoBuilder(spec['repository'])
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            o1 = acquirer.observe('supplier')
            self.assertEqual(o1['commit_sha'], spec['jumped'])
            acquirer.adopt('supplier', o1['id'])

            builder.reset_hard(spec['diverged'])  # commit timestamp is older
            o2 = acquirer.observe('supplier')
            self.assertEqual(o2['relationship'], REL_DIVERGED)
            self.assertTrue(o2['independent'])
            with self.assertRaises(AcquireError):
                acquirer.adopt('supplier', o2['id'])
            acquirer.confirm_generation('supplier', o2['id'])
            acquirer.adopt('supplier', o2['id'])
            view = acquirer.current_view('supplier')
            ids = {r['business_id'] for r in view['records'] if r['kind'] == 'product'}
            self.assertEqual(ids, {'P1', 'PFORK'})
            self.assertNotIn('P2', ids)

    def test_branch_disappearance_then_reappearance_is_not_plain_update(self):
        repo = self.tmp / 'repo'
        builder = RepoBuilder(repo)
        builder.write_json('outlets/s.json', OUTLET_S1)
        builder.write_json('products/p.json', PRODUCT_P1)
        c1 = builder.commit('one')
        with self.acquire() as acquirer:
            self.register(acquirer, repo)
            o1 = acquirer.observe('supplier')
            acquirer.adopt('supplier', o1['id'])

            builder.branch_delete('catalog')
            missing = acquirer.observe('supplier')
            self.assertEqual(missing['relationship'], REL_DISAPPEARED)
            self.assertIsNone(missing['commit_sha'])
            self.assertIsNone(missing['generation'])
            with self.assertRaises(AcquireError):
                acquirer.adopt('supplier', missing['id'])
            # Watermark untouched.
            self.assertEqual(acquirer.current_view('supplier')
                             ['adoption']['commit_sha'], c1)

            # Reappear on a non-descendant commit (recreated from nothing-like).
            builder.git('update-ref', 'refs/heads/catalog', c1)
            back = acquirer.observe('supplier')
            self.assertEqual(back['relationship'], REL_UP_TO_DATE)
            self.assertEqual(back['prev_commit_sha'], c1)

            # Reappear at an ancestor of the last seen commit. Build c2 on the
            # catalog ref directly; the working tree stays on the old checkout.
            tree = builder.git('rev-parse', c1 + '^{tree}').stdout.decode().strip()
            c2 = builder.git('commit-tree', tree, '-p', c1,
                             input=b'second generation parent').stdout.decode().strip()
            builder.git('update-ref', 'refs/heads/catalog', c2)
            advanced = acquirer.observe('supplier')
            self.assertEqual(advanced['commit_sha'], c2)
            self.assertEqual(advanced['relationship'], REL_FAST_FORWARD)
            root_commit = builder.git('rev-list', '--max-parents=0', c2
                                      ).stdout.decode().strip()
            builder.branch_delete('catalog')
            acquirer.observe('supplier')  # disappearance logged again
            builder.git('update-ref', 'refs/heads/catalog', root_commit)
            rewind = acquirer.observe('supplier')
            self.assertEqual(rewind['relationship'], REL_REWOUND)
            self.assertTrue(rewind['independent'])
            self.assertNotEqual(c2, root_commit)

    def test_missing_branch_before_any_commit_is_reported(self):
        empty = RepoBuilder(self.tmp / 'empty')
        empty.git('init', '-b', 'catalog')
        with self.acquire() as acquirer:
            self.register(acquirer, empty.root)
            observation = acquirer.observe('supplier')
            self.assertEqual(observation['relationship'], REL_MISSING)
            self.assertIsNone(observation['commit_sha'])

    # --------------------------------------------------------------- pinning / tx
    def test_concurrent_ref_update_after_observe_does_not_mix_generations(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        builder = RepoBuilder(spec['repository'])
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            observation = acquirer.observe('supplier')  # pinned to jumped
            pinned = observation['commit_sha']

            # Supplier moves the branch again while we are processing.
            builder.reset_hard(spec['moved'])
            moved_head = builder.head()
            self.assertNotEqual(pinned, moved_head)

            acquirer.adopt('supplier', observation['id'])
            view = acquirer.current_view('supplier')
            self.assertEqual(view['adoption']['commit_sha'], pinned)
            self.assertEqual(view['adoption']['observation_id'], observation['id'])
            # P2 exists only in the pinned jumped commit, proving no mix.
            self.assertIn('P2', {r['business_id'] for r in view['records']
                                 if r['kind'] == 'product'})

    def test_adoption_is_idempotent_and_does_not_duplicate_objects(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            o1 = acquirer.observe('supplier')
            first = acquirer.adopt('supplier', o1['id'])
            second = acquirer.adopt('supplier', o1['id'])
            self.assertTrue(second['already_adopted'])
            blob_count = acquirer.database.execute(
                'SELECT COUNT(*) FROM git_blobs').fetchone()[0]
            record_count = acquirer.database.execute(
                'SELECT COUNT(*) FROM catalog_records').fetchone()[0]
            self.assertEqual(blob_count, 3)
            self.assertEqual(record_count, 3)
            self.assertEqual(first['adoption_id'], second['adoption_id'])

    def test_conflict_blocks_switch_and_preserves_watermark(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        conflict_commit, _ = scenarios.build_conflict_repo(self.tmp / 'conflict')
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            good = acquirer.observe('supplier')
            acquirer.adopt('supplier', good['id'])

            acquirer.add_source('broken', self.tmp / 'conflict',
                                'catalog', 'products', 'outlets')
            bad = acquirer.observe('broken')
            with self.assertRaises(AcquireError) as ctx:
                acquirer.adopt('broken', bad['id'])
            self.assertIn('duplicate business key', str(ctx.exception).lower())
            self.assertIsNone(acquirer.get_source('broken')['current_adoption_id'])
            # Original source watermark is untouched.
            self.assertEqual(acquirer.current_view('supplier')
                             ['adoption']['commit_sha'], spec['jumped'])
            # No orphan rows were committed.
            self.assertEqual(acquirer.database.execute(
                'SELECT COUNT(*) FROM adoptions').fetchone()[0], 1)

    def test_same_blob_multiple_paths_keeps_each_source_reference(self):
        commit, _builder, payload = scenarios.build_shared_blob_repo(self.tmp / 'repo')
        with self.acquire() as acquirer:
            self.register(acquirer, self.tmp / 'repo')
            observation = acquirer.observe('supplier')
            result = acquirer.adopt('supplier', observation['id'])
            view = acquirer.current_view('supplier')
            product_paths = [r['path'] for r in view['records'] if r['kind'] == 'product']
            self.assertEqual(product_paths, ['products/p.json'])
            excluded = [e for e in view['exclusions']
                        if e['reason'] == 'outside-managed-roots']
            self.assertEqual([e['path'] for e in excluded], ['archive/p-copy.json'])
            # Identical bytes at both paths mean one Git blob; trace it.
            blob_sha = gitread.list_tree(self.tmp / 'repo', commit)
            product_oid = next(oid for mode, _t, oid, path in blob_sha
                               if path.decode() == 'products/p.json')
            archive_oid = next(oid for mode, _t, oid, path in blob_sha
                               if path.decode() == 'archive/p-copy.json')
            self.assertEqual(product_oid, archive_oid)
            provenance = acquirer.provenance(git_sha=product_oid)
            self.assertEqual(len(provenance['references']), 1)
            self.assertEqual(provenance['references'][0]['path'], 'products/p.json')
            raw_path = Path(provenance['references'][0]['stored_path'])
            self.assertEqual(raw_path.read_bytes(), payload)

    def test_symlink_and_gitlink_are_excluded_with_reasons(self):
        commit, _ = scenarios.build_exclusion_repo(self.tmp / 'repo')
        with self.acquire() as acquirer:
            self.register(acquirer, self.tmp / 'repo')
            observation = acquirer.observe('supplier')
            acquirer.adopt('supplier', observation['id'])
            view = acquirer.current_view('supplier')
            reasons = {e['path']: e['reason'] for e in view['exclusions']}
            self.assertEqual(reasons['products/evil-link.json'], 'symlink-not-followed')
            self.assertEqual(reasons['products/vendor-submodule'],
                             'gitlink-submodule-not-expanded')
            business_paths = {r['path'] for r in view['records']}
            self.assertNotIn('products/evil-link.json', business_paths)

    def test_bad_json_and_dangling_ref_keep_previous_complete_commit(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        bad_commit, _ = scenarios.build_bad_json_repo(self.tmp / 'bad')
        dangling_commit, _ = scenarios.build_dangling_ref_repo(self.tmp / 'dangling')
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            good = acquirer.observe('supplier')
            acquirer.adopt('supplier', good['id'])

            for name, repo, fragment in (
                    ('bad', self.tmp / 'bad', 'not valid utf-8 json'),
                    ('dangling', self.tmp / 'dangling', 'unknown outlet')):
                acquirer.add_source(name, repo, 'catalog', 'products', 'outlets')
                observation = acquirer.observe(name)
                with self.assertRaises(AcquireError) as ctx:
                    acquirer.adopt(name, observation['id'])
                self.assertIn(fragment, str(ctx.exception).lower())
                self.assertIsNone(acquirer.get_source(name)['current_adoption_id'])
            self.assertEqual(acquirer.current_view('supplier')
                             ['adoption']['commit_sha'], spec['jumped'])

    # ----------------------------------------------------------------- crash test
    def test_sigkill_leaves_every_relation_on_previous_commit(self):
        repo = self.tmp / 'repo'
        builder = RepoBuilder(repo)
        builder.write_json('outlets/s.json', OUTLET_S1)
        builder.write_json('products/p.json', PRODUCT_P1)
        c1 = builder.commit('one')
        # Keep the branch pinned at c1 for the clean bootstrap.
        builder.branch_create('next', c1)
        bootstrap = subprocess.run(
            [sys.executable, '-m', 'scripts.crash_worker', str(self.state),
             str(repo), 'supplier', 'bootstrap'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)

        # A descendant commit (second product) becomes available on the branch.
        builder.git('checkout', 'next')
        builder.write_json('products/p2.json', PRODUCT_P2)
        c2 = builder.commit('two')
        builder.branch_delete('catalog')
        builder.git('branch', 'catalog', c2)
        self.assertTrue(gitread.is_ancestor(repo, c1, c2))

        marker = self.tmp / 'marker'
        env = dict(os.environ,
                   SUPPLYTREE_CRASH_BEFORE_COMMIT='supplier',
                   SUPPLYTREE_CRASH_MARKER=str(marker))
        killed = subprocess.Popen(
            [sys.executable, '-m', 'scripts.crash_worker', str(self.state),
             str(repo), 'supplier', 'adopt'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists() or killed.poll() is not None,
                        'worker never reached the pre-commit crash point')
        killed.send_signal(signal.SIGKILL)
        stdout, stderr = killed.communicate(timeout=30)
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        self.assertIn('"stage": "observed"', stdout)
        self.assertNotIn('"stage": "adopted"', stdout)

        with self.acquire() as acquirer:
            # Watermark and every relation remain on the previous commit c1.
            view = acquirer.current_view('supplier')
            self.assertEqual(view['adoption']['commit_sha'], c1)
            ids = {r['business_id'] for r in view['records']}
            self.assertEqual(ids, {'P1', 'S1'})
            # The observation pinned c2, but no adoption row was committed.
            pending = [o for o in acquirer.list_observations('supplier')
                       if o['commit_sha'] == c2]
            self.assertEqual(len(pending), 1)
            self.assertEqual(acquirer.database.execute(
                'SELECT COUNT(*) FROM adoptions').fetchone()[0], 1)
            # Raw objects are written inside the rolled-back transaction, so
            # the kill leaves only the two blobs belonging to committed c1.
            on_disk_after_kill = list((self.state / 'blobs').iterdir())
            self.assertEqual(len(on_disk_after_kill), 2)

            # Recovery: rerunning pins the same branch tip and completes c2 once.
            recovery = subprocess.run(
                [sys.executable, '-m', 'scripts.crash_worker', str(self.state),
                 str(repo), 'supplier', 'adopt'],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(recovery.returncode, 0, recovery.stderr)
            with self.acquire() as acquirer:
                view = acquirer.current_view('supplier')
                self.assertEqual(view['adoption']['commit_sha'], c2)
                self.assertEqual({r['business_id'] for r in view['records']},
                                 {'P1', 'P2', 'S1'})
                # Three distinct Git blobs across c1/c2; the killed run rolled
                # back so each object is imported exactly once.
                self.assertEqual(acquirer.database.execute(
                    'SELECT COUNT(*) FROM git_blobs').fetchone()[0], 3)
                distinct_files = acquirer.database.execute(
                    'SELECT COUNT(DISTINCT stored_path) FROM git_blobs').fetchone()[0]
                self.assertEqual(distinct_files, 3)
                # Records are per-adoption references: 2 from c1 + 3 from c2.
                self.assertEqual(acquirer.database.execute(
                    'SELECT COUNT(*) FROM catalog_records').fetchone()[0], 5)
                self.assertEqual(acquirer.database.execute(
                    'SELECT COUNT(*) FROM product_outlet_refs').fetchone()[0], 3)
                self.assertEqual(acquirer.database.execute(
                    'SELECT COUNT(*) FROM adoptions').fetchone()[0], 2)
                on_disk = list((self.state / 'blobs').iterdir())
                self.assertEqual(len(on_disk), 3)

    # ------------------------------------------------------------- traceability
    def test_history_traces_commit_tree_blob_and_observation(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        builder = RepoBuilder(spec['repository'])
        with self.acquire() as acquirer:
            self.register(acquirer, spec['repository'])
            builder.reset_hard(spec['initial'])
            o1 = acquirer.observe('supplier')  # initial
            self.assertEqual(o1['commit_sha'], spec['initial'])
            acquirer.adopt('supplier', o1['id'])
            o2 = acquirer.observe('supplier')  # up to date, no new adoption
            self.assertEqual(o2['relationship'], REL_UP_TO_DATE)
            builder = RepoBuilder(spec['repository'])
            builder.reset_hard(spec['moved'])
            o3 = acquirer.observe('supplier')
            self.assertEqual(o3['relationship'], REL_FAST_FORWARD)
            acquirer.adopt('supplier', o3['id'])

            history = acquirer.history('supplier', kind='product', business_id='P1')
            commits = [row['commit_sha'] for row in history]
            self.assertEqual(commits, [spec['initial'], spec['moved']])
            for row in history:
                self.assertEqual(row['business_id'], 'P1')
                self.assertTrue(gitread.is_sha1(row['git_blob']))
                self.assertTrue(gitread.is_sha1(row['tree_sha']))
                self.assertIsNotNone(row['observation_id'])
            self.assertNotEqual(history[0]['path'], history[1]['path'])
            self.assertEqual(history[0]['git_blob'], history[1]['git_blob'])

            record_id = history[0]['adoption_id']
            provenance = acquirer.provenance(record_id=next(
                r['id'] for r in acquirer.current_view('supplier')['records']
                if r['business_id'] == 'P1'))
            self.assertEqual(provenance['commit_sha'], spec['moved'])
            self.assertTrue(gitread.is_sha1(provenance['commit_tree']))
            self.assertEqual(provenance['outlet_ids'], ['S1'])

            product_outlet = acquirer.database.execute(
                'SELECT por.outlet_business_id FROM product_outlet_refs por '
                'JOIN catalog_records cr ON cr.id = por.record_id '
                'WHERE cr.business_id = ?', ('P1',)).fetchall()
            self.assertEqual({row[0] for row in product_outlet}, {'S1'})

    def test_unauthorized_repo_and_ref_inputs_are_rejected(self):
        from supplytree.acquire import validate_branch, validate_root
        for bad in ('-x', 'a;rm', 'a$b', 'a b', 'a..b', 'a@{', 'a\\b',
                    'a.lock', 'a/', ''):
            with self.assertRaises(AcquireError):
                validate_branch(bad)
        for bad in ('../x', '/abs', 'a/../b', 'a//b', './a', ''):
            with self.assertRaises(AcquireError):
                validate_root(bad, 'root')
        with self.acquire() as acquirer:
            with self.assertRaises(OSError):
                acquirer.add_source('x', self.tmp / 'missing', 'catalog',
                                    'products', 'outlets')
            with self.assertRaises(AcquireError):
                acquirer.add_source('y', self.tmp, '../etc', 'products', 'outlets')
            with self.assertRaises(AcquireError):
                acquirer.add_source('z', self.tmp, 'catalog', '/etc/passwd',
                                    'outlets')
            with self.assertRaises(AcquireError):
                acquirer.add_source('w', self.tmp, 'cat;rm', 'products', 'outlets')

    def test_cli_end_to_end(self):
        spec = scenarios.build_lifecycle_repo(self.tmp / 'repo')
        prefix = [sys.executable, '-m', 'supplytree', 'acquire', '--state',
                  str(self.state)]
        run = lambda *args: subprocess.run(
            [*prefix, *args], capture_output=True, text=True, timeout=60)
        result = run('add-source', 'supplier', spec['repository'], '--branch',
                     'catalog', '--product-root', 'products',
                     '--outlet-root', 'outlets')
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = run('observe', 'supplier')
        self.assertEqual(observed.returncode, 0, observed.stderr)
        observation = json.loads(observed.stdout)
        adopted = run('adopt', 'supplier', '--observation', str(observation['id']))
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        current = run('current', 'supplier')
        self.assertEqual(current.returncode, 0, current.stderr)
        payload = json.loads(current.stdout)
        self.assertEqual(len(payload['records']), 3)
        listed = run('observations', 'supplier')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('relationship', listed.stdout)
        history = run('history', 'supplier', '--kind', 'product', '--id', 'P1')
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertIn('git_blob', history.stdout)
        # A rewind without confirmation fails with a non-zero exit.
        RepoBuilder(spec['repository']).reset_hard(spec['initial'])
        rewind = run('observe', 'supplier')
        self.assertEqual(rewind.returncode, 0, rewind.stderr)
        refused = run('adopt', 'supplier')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('independent generation', refused.stderr)


if __name__ == '__main__':
    unittest.main()
