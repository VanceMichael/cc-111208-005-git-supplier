"""Synthetic local Git repositories for acquire acceptance tests and demos.

Everything here is generated in temporary directories from in-memory bytes.
No real supplier repository is ever contacted. The builder intentionally
supports exotic paths (spaces, newlines, non-ASCII), symlinks, gitlinks and
manual ref manipulation so the intake engine can be exercised end to end.
"""

import json
import os
from pathlib import Path
import subprocess


class RepoBuilder:
    def __init__(self, root, branch='catalog', initial_branch=True):
        self.root = Path(root)
        self.branch = branch
        self._env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        if (self.root / '.git').is_dir():
            # Attach to an already initialized synthetic repository.
            return
        self.root.mkdir(parents=True, exist_ok=False)
        if initial_branch:
            self.git('init', '-b', branch)
            self.git('config', 'user.name', 'Synthetic Supplier')
            self.git('config', 'user.email', 'supplier@example.invalid')
            self.git('config', 'commit.gpgsign', 'false')

    def git(self, *arguments, check=True, input=None):
        result = subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.root), *arguments],
            capture_output=True, env=self._env, input=input)
        if check and result.returncode != 0:
            raise RuntimeError(
                'git %s failed: %s' % (' '.join(arguments),
                                       result.stderr.decode(errors='replace')))
        return result

    def write(self, relpath, payload):
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        path.write_bytes(payload)
        return path

    def write_json(self, relpath, document):
        return self.write(relpath, json.dumps(document, ensure_ascii=False).encode('utf-8'))

    def move(self, src, dst):
        source = self.root / src
        destination = self.root / dst
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)

    def remove(self, relpath):
        (self.root / relpath).unlink()

    def symlink(self, link_relpath, target):
        link = self.root / link_relpath
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)

    def gitlink(self, relpath, target_sha):
        """Stage a submodule gitlink without initializing or expanding anything."""
        self.git('update-index', '--add',
                 '--cacheinfo', '160000,%s,%s' % (target_sha, relpath))

    def commit(self, message, author_date=None, commit_date=None, add_all=True):
        if add_all:
            self.git('add', '-A')
        env = None
        if author_date or commit_date:
            env = dict(self._env)
            if author_date:
                env['GIT_AUTHOR_DATE'] = author_date
            if commit_date:
                env['GIT_COMMIT_DATE'] = commit_date
        result = subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.root),
             'commit', '-m', message],
            capture_output=True, env=env or self._env)
        if result.returncode != 0:
            raise RuntimeError('commit failed: ' + result.stderr.decode(errors='replace'))
        return self.head()

    def head(self):
        return self.git('rev-parse', 'HEAD').stdout.decode().strip()

    def rev_parse(self, ref):
        return self.git('rev-parse', ref).stdout.decode().strip()

    def tree_of(self, commit):
        return self.git('rev-parse', commit + '^{tree}').stdout.decode().strip()

    def branch_create(self, name, commit):
        self.git('branch', name, commit)

    def branch_delete(self, name):
        # Deleting the currently checked-out branch is rejected; deleting the
        # ref directly models a supplier-side branch disappearance.
        self.delete_ref('refs/heads/' + name)

    def reset_hard(self, commit):
        self.git('reset', '--hard', commit)

    def update_ref(self, ref, commit):
        self.git('update-ref', ref, commit)

    def delete_ref(self, ref):
        self.git('update-ref', '-d', ref)


PRODUCT_P1 = {'kind': 'product', 'id': 'P1', 'name': '合成保养包', 'outletIds': ['S1']}
PRODUCT_P2 = {'kind': 'product', 'id': 'P2', 'name': '另一款商品', 'outletIds': ['S1']}
OUTLET_S1 = {'kind': 'outlet', 'id': 'S1', 'city': '合成城'}


def build_lifecycle_repo(destination, head='catalog'):
    """initial -> moved -> jump, plus a divergent generation and rewind branch.

    Returns shas needed for observation classification tests. Commit
    timestamps are deliberately non-monotonic in one branch to prove ancestry
    (not time) drives classification. ``head`` selects which branch is checked
    out when the function returns; the catalog branch still points at jumped.
    """
    builder = RepoBuilder(destination)
    builder.write_json('products/商品 name\nline.json', PRODUCT_P1)
    builder.write_json('outlets/store with space.json', OUTLET_S1)
    initial = builder.commit('Initial catalog', author_date='2026-01-01T00:00:00Z')

    builder.move('products/商品 name\nline.json', 'products/moved.json')
    moved = builder.commit('Move without changing identity',
                           author_date='2026-01-02T00:00:00Z')

    builder.write_json('products/p2 中文.json', PRODUCT_P2)
    jumped = builder.commit('Second product', author_date='2026-01-03T00:00:00Z')

    # Divergent generation: starts from initial, not from jumped.
    divergent_builder_sha = initial
    builder.branch_create('divergent-line', divergent_builder_sha)
    builder.git('checkout', 'divergent-line')
    builder.write_json('products/fork.json',
                       {'kind': 'product', 'id': 'PFORK', 'name': '分叉商品',
                        'outletIds': ['S1']})
    diverged = builder.commit('Diverged catalog',
                              author_date='2025-12-31T00:00:00Z')  # older timestamp
    builder.git('checkout', builder.branch)

    # Replayed/rewound branch name, pointing back at initial.
    builder.branch_create('rewound', initial)
    if head != builder.branch:
        builder.git('checkout', head)
    return {
        'repository': str(builder.root),
        'initial': initial,
        'moved': moved,
        'jumped': jumped,
        'diverged': diverged,
        'rewound': initial,
    }


def build_conflict_repo(destination):
    """Same business key at two paths inside one tree."""
    builder = RepoBuilder(destination)
    builder.write_json('products/a.json', PRODUCT_P1)
    builder.write_json('products/nested/b.json', PRODUCT_P1)
    builder.write_json('outlets/s.json', OUTLET_S1)
    return builder.commit('Duplicate business key'), builder


def build_shared_blob_repo(destination):
    """One blob referenced from two paths; only one is under a managed root."""
    builder = RepoBuilder(destination)
    payload = json.dumps(PRODUCT_P1, ensure_ascii=False).encode('utf-8')
    builder.write('products/p.json', payload)
    builder.write('archive/p-copy.json', payload)  # identical blob, outside roots
    builder.write_json('outlets/s.json', OUTLET_S1)
    commit = builder.commit('Same blob at multiple paths')
    return commit, builder, payload


def build_exclusion_repo(destination):
    """Symlink and gitlink entries that must be recorded, never followed."""
    builder = RepoBuilder(destination)
    builder.write_json('products/p.json', PRODUCT_P1)
    builder.write_json('outlets/s.json', OUTLET_S1)
    builder.symlink('products/evil-link.json', 'store with space.json')
    # Stage ordinary files and the symlink first; the gitlink is added last so
    # that no later 'git add -A' can silently drop it from the index.
    builder.git('add', 'products/p.json', 'outlets/s.json', 'products/evil-link.json')
    builder.gitlink('products/vendor-submodule', '0123456789abcdef0123456789abcdef01234567')
    commit = builder.commit('Unsupported tree entries', add_all=False)
    return commit, builder


def build_bad_json_repo(destination):
    """A managed blob that is not valid UTF-8 JSON."""
    builder = RepoBuilder(destination)
    builder.write_json('outlets/s.json', OUTLET_S1)
    builder.write('products/broken.json', b'{not valid json')
    return builder.commit('Broken JSON'), builder


def build_dangling_ref_repo(destination):
    """A product that references an outlet absent from the same tree."""
    builder = RepoBuilder(destination)
    builder.write_json('products/p.json',
                       {'kind': 'product', 'id': 'P1', 'name': 'x',
                        'outletIds': ['GHOST']})
    builder.write_json('outlets/s.json', OUTLET_S1)
    return builder.commit('Dangling outlet reference'), builder
