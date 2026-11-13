"""End-to-end acquire demonstration on a purely synthetic local Git source.

The loop shown here is exactly what the acceptance docs describe:
observe -> adopt a complete commit -> move a product without identity change ->
rewind the branch -> prove the old full directory is still the current view ->
explicitly confirm the new generation -> switch -> trace commit/tree/blob.
Every artifact lives in a fresh temporary state directory and no real
supplier repository is contacted.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .materialize import main as materialize
from . import scenarios
from scripts.scenarios import RepoBuilder
from supplytree.acquire import Acquirer


def emit(title, payload):
    print('\n=== ' + title + ' ===')
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main():
    materialize()
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        spec = scenarios.build_lifecycle_repo(tmp / 'supplier')
        state = tmp / 'state'
        builder = RepoBuilder(spec['repository'])

        with Acquirer(state) as acquirer:
            acquirer.add_source('supplier', spec['repository'], 'catalog',
                                'products', 'outlets')

            builder.reset_hard(spec['initial'])
            first = acquirer.observe('supplier')
            emit('1. initial observation pinned to a real commit',
                 {'observation_id': first['id'], 'commit': first['commit_sha'],
                  'relationship': first['relationship']})
            acquirer.adopt('supplier', first['id'])

            builder.reset_hard(spec['moved'])
            moved_observation = acquirer.observe('supplier')
            acquirer.adopt('supplier', moved_observation['id'])
            moved_view = acquirer.current_view('supplier')
            product = next(r for r in moved_view['records']
                           if r['kind'] == 'product' and r['business_id'] == 'P1')
            emit('2. product moved: identity (kind,id) and blob sha are unchanged',
                 {'business_id': product['business_id'], 'path': product['path'],
                  'git_blob': product['git_blob'], 'tree': product['tree_sha']})

            # Supplier rewinds the branch to an ancestor commit.
            builder.reset_hard(spec['initial'])
            rewind = acquirer.observe('supplier')
            emit('3. branch rewind is classified as an independent generation',
                 {'observation_id': rewind['id'],
                  'relationship': rewind['relationship'],
                  'independent': bool(rewind['independent']),
                  'generation': rewind['generation']})
            before_confirm = acquirer.current_view('supplier')
            emit('4. before confirmation the current view is still the full moved tree',
                 {'watermark_commit': before_confirm['adoption']['commit_sha'],
                  'record_count': len(before_confirm['records']),
                  'business_keys': sorted((r['kind'], r['business_id'])
                                          for r in before_confirm['records'])})

            confirmed = acquirer.confirm_generation('supplier', rewind['id'])
            acquirer.adopt('supplier', confirmed['id'])
            after = acquirer.current_view('supplier')
            emit('5. after explicit generation confirmation the view switches atomically',
                 {'generation': confirmed['generation'],
                  'watermark_commit': after['adoption']['commit_sha'],
                  'record_count': len(after['records']),
                  'business_keys': sorted((r['kind'], r['business_id'])
                                          for r in after['records'])})

            trace = acquirer.history('supplier', kind='product', business_id='P1')
            emit('6. SQLite history traces every adoption to commit, tree, blob, observation',
                 [{'commit': row['commit_sha'], 'tree': row['tree_sha'],
                   'blob': row['git_blob'], 'path': row['path'],
                   'observation_id': row['observation_id'],
                   'relationship': row['relationship']} for row in trace])

        # Prove the legacy read-only inspector still works alongside acquire.
        legacy = subprocess.run(
            [sys.executable, '-m', 'supplytree', 'trees', spec['repository'],
             '--revision', spec['moved']],
            check=True, capture_output=True, text=True)
        summary = json.loads(legacy.stdout)
        emit('7. legacy trees inspector remains available',
             {'commit': summary['commit'], 'entry_count': len(summary['entries'])})


if __name__ == '__main__':
    main()
