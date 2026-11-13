"""Subprocess worker for the real process-termination recovery test.

Launched as its own Python process so the test can deliver an actual SIGKILL.
When SUPPLYTREE_CRASH_BEFORE_COMMIT is set, the engine builds the full
snapshot and then blocks just before the SQLite commit; the worker touches
SUPPLYTREE_CRASH_MARKER once it has reached that point so the test knows when
to kill it.
"""

import argparse
import json
import os
from pathlib import Path

from supplytree.acquire import Acquirer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('state')
    parser.add_argument('repo')
    parser.add_argument('source')
    parser.add_argument('mode', choices=('bootstrap', 'adopt'))
    parser.add_argument('--branch', default='catalog')
    parser.add_argument('--product-root', default='products')
    parser.add_argument('--outlet-root', default='outlets')
    args = parser.parse_args()

    marker = os.environ.get('SUPPLYTREE_CRASH_MARKER')
    with Acquirer(args.state) as acquirer:
        try:
            acquirer.add_source(args.source, args.repo, args.branch,
                                args.product_root, args.outlet_root)
        except ValueError:
            pass  # source already registered during bootstrap
        observation = acquirer.observe(args.source)
        print(json.dumps({'stage': 'observed', 'observation_id': observation['id'],
                          'commit': observation['commit_sha'],
                          'relationship': observation['relationship']}), flush=True)
        result = acquirer.adopt(args.source, observation['id'])
        if marker:
            Path(marker).unlink(missing_ok=True)
        print(json.dumps({'stage': 'adopted', 'result': result}), flush=True)


if __name__ == '__main__':
    main()
