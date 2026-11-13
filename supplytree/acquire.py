"""Business intake ("acquire") from an authorized local Git repository.

Every intake is pinned to a real commit object. Branch observations classify
how the observed commit relates to the previously observed history (fast
forward, multi-commit jump, rewind, divergence, disappearance/reappearance).
Non-descendant commits open an independent observation generation and never
replace a source's current view until the operator confirms that generation.

A commit is adopted atomically: products, outlet references, raw blob
references and the observation watermark switch in one SQLite transaction, so
a parse failure or process termination leaves every relation on the previous
complete commit. All Git access goes through supplytree.gitread: no shell, no
hooks, no checkout, no submodule expansion.
"""

import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from datetime import datetime, timezone

from . import gitread
from .gitread import ORDINARY_MODES, GITLINK_MODE, SYMLINK_MODE

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    repo_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    product_root TEXT NOT NULL,
    outlet_root TEXT NOT NULL,
    current_adoption_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL REFERENCES sources(name),
    observed_at TEXT NOT NULL,
    commit_sha TEXT,
    tree_sha TEXT,
    prev_commit_sha TEXT,
    relationship TEXT NOT NULL,
    independent INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS adoptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL REFERENCES sources(name),
    observation_id INTEGER NOT NULL UNIQUE REFERENCES observations(id),
    commit_sha TEXT NOT NULL,
    tree_sha TEXT NOT NULL,
    adopted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS git_blobs (
    git_sha TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    raw_digest TEXT NOT NULL,
    stored_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adoption_id INTEGER NOT NULL REFERENCES adoptions(id),
    kind TEXT NOT NULL CHECK (kind IN ('product', 'outlet')),
    business_id TEXT NOT NULL,
    git_blob TEXT NOT NULL REFERENCES git_blobs(git_sha),
    tree_sha TEXT NOT NULL,
    path TEXT NOT NULL,
    UNIQUE (adoption_id, kind, business_id)
);
CREATE INDEX IF NOT EXISTS idx_records_key ON catalog_records(kind, business_id);
CREATE INDEX IF NOT EXISTS idx_records_adoption ON catalog_records(adoption_id);
CREATE INDEX IF NOT EXISTS idx_records_blob ON catalog_records(git_blob);
CREATE TABLE IF NOT EXISTS product_outlet_refs (
    record_id INTEGER NOT NULL REFERENCES catalog_records(id),
    outlet_business_id TEXT NOT NULL,
    PRIMARY KEY (record_id, outlet_business_id)
);
CREATE TABLE IF NOT EXISTS exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adoption_id INTEGER NOT NULL REFERENCES adoptions(id),
    mode TEXT,
    object_type TEXT,
    git_sha TEXT,
    path TEXT,
    path_hex TEXT,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exclusions_adoption ON exclusions(adoption_id);
"""

BRANCH_RE = re.compile(r'[A-Za-z0-9._/\-]+\Z')
ROOT_RE = re.compile(r'[^\x00]+')

# Observation relationship vocabulary.
REL_INITIAL = 'initial'
REL_FAST_FORWARD = 'fast_forward'
REL_MULTI_JUMP = 'multi_commit_jump'
REL_REWOUND = 'rewound'
REL_DIVERGED = 'diverged'
REL_UP_TO_DATE = 'up_to_date'
REL_DISAPPEARED = 'disappeared'
REL_MISSING = 'missing'
INDEPENDENT_RELATIONS = frozenset((REL_REWOUND, REL_DIVERGED))


class AcquireError(ValueError):
    """A validation or state error that must leave the watermark untouched."""


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def validate_branch(branch):
    if not branch or not BRANCH_RE.fullmatch(branch) or branch.startswith('-'):
        raise AcquireError('invalid branch name: ' + repr(branch))
    forbidden = ('..', '//', '@{', '\\', ' ')
    if any(token in branch for token in forbidden) or branch.endswith('.lock') \
            or branch.endswith('/') or branch.endswith('.'):
        raise AcquireError('invalid branch name: ' + repr(branch))
    return branch


def validate_root(root, label):
    if not root or not ROOT_RE.fullmatch(root) or root.startswith('/') or '\\' in root:
        raise AcquireError(label + ' must be a relative POSIX path')
    parts = root.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise AcquireError(label + ' must stay inside the repository root')
    return '/'.join(parts)


def _under_root(path, root):
    return path == root or path.startswith(root + '/')


def _safe_relative_path(path):
    if path.startswith('/') or '\\' in path or '\x00' in path:
        return False
    parts = path.split('/')
    if any(part in ('', '..') for part in parts):
        return False
    return True


class Acquirer:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.root / 'blobs'
        self.blob_dir.mkdir(exist_ok=True)
        self.database = sqlite3.connect(self.root / 'acquire.sqlite3')
        self.database.row_factory = sqlite3.Row
        self.database.execute('PRAGMA foreign_keys = ON')
        self.database.execute('PRAGMA journal_mode = WAL')
        self.database.execute('PRAGMA synchronous = FULL')
        self.database.executescript(SCHEMA)

    def close(self):
        self.database.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ sources
    def add_source(self, name, repository, branch, product_root, outlet_root):
        branch = validate_branch(branch)
        product_root = validate_root(product_root, 'product root')
        outlet_root = validate_root(outlet_root, 'outlet root')
        if product_root == outlet_root:
            raise AcquireError('product and outlet roots must be distinct')
        repo_path = gitread.ensure_repository(repository)
        created = _utcnow()
        try:
            with self.database:
                self.database.execute(
                    'INSERT INTO sources VALUES (?, ?, ?, ?, ?, NULL, ?)',
                    (name, repo_path, branch, product_root, outlet_root, created),
                )
        except sqlite3.IntegrityError as error:
            raise AcquireError('source already exists: ' + name) from error
        return self.get_source(name)

    def get_source(self, name):
        row = self.database.execute(
            'SELECT * FROM sources WHERE name = ?', (name,)).fetchone()
        if row is None:
            raise AcquireError('unknown source: ' + name)
        return dict(row)

    # --------------------------------------------------------------- observation
    def observe(self, source_name):
        """Resolve the branch once, pin its commit, and classify the observation.

        The pinned sha is captured immediately; later branch movement cannot
        alter this observation. Classification uses object ancestry, never
        commit timestamps.
        """
        source = self.get_source(source_name)
        commit = gitread.resolve_branch_commit(source['repo_path'], source['branch'])
        previous = self.database.execute(
            "SELECT * FROM observations WHERE source_name = ? AND commit_sha IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (source_name,)).fetchone()
        prev_commit = previous['commit_sha'] if previous else None

        if commit is None:
            relationship = REL_DISAPPEARED if prev_commit else REL_MISSING
            tree_sha = None
            independent = 0
            note = 'branch ref no longer resolves to a commit'
        else:
            tree_sha, _parents = gitread.commit_metadata(source['repo_path'], commit)
            relationship, independent, note = self._classify(
                source['repo_path'], commit, prev_commit)

        observed_at = _utcnow()
        cursor = self.database.execute(
            'INSERT INTO observations (source_name, observed_at, commit_sha, tree_sha, '
            'prev_commit_sha, relationship, independent, confirmed, note) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)',
            (source_name, observed_at, commit, tree_sha, prev_commit,
             relationship, independent, note),
        )
        self.database.commit()
        return self.get_observation(cursor.lastrowid)

    @staticmethod
    def _classify(repo_path, commit, prev_commit):
        if prev_commit is None:
            return REL_INITIAL, 0, 'first observable commit for this source'
        if commit == prev_commit:
            return REL_UP_TO_DATE, 0, 'branch still points at the last observed commit'
        if gitread.is_ancestor(repo_path, prev_commit, commit):
            count = gitread.count_commits_between(repo_path, prev_commit, commit)
            if count <= 1:
                return REL_FAST_FORWARD, 0, 'descendant commit, one new commit'
            return (REL_MULTI_JUMP, 0,
                    'descendant commit, %d commits were skipped' % count)
        if gitread.is_ancestor(repo_path, commit, prev_commit):
            return (REL_REWOUND, 1,
                    'commit is an ancestor of the last observed commit; new generation')
        return (REL_DIVERGED, 1,
                'commit shares no descendant relation with the observed history; new generation')

    def get_observation(self, observation_id):
        row = self.database.execute(
            'SELECT * FROM observations WHERE id = ?', (observation_id,)).fetchone()
        if row is None:
            raise AcquireError('unknown observation: ' + str(observation_id))
        result = dict(row)
        result['generation'] = self._generation(row)
        return result

    def _generation(self, observation_row):
        """Generation = 1 + number of confirmed independent earlier generations."""
        if observation_row['commit_sha'] is None:
            return None  # a disappearance is not part of any generation
        generation = 1
        rows = self.database.execute(
            'SELECT independent, confirmed FROM observations WHERE source_name = ? '
            'AND id <= ? ORDER BY id',
            (observation_row['source_name'], observation_row['id'])).fetchall()
        for row in rows:
            if row['independent']:
                if not row['confirmed']:
                    return None  # pending generation: not accepted yet
                generation += 1
        return generation

    def list_observations(self, source_name=None, limit=50):
        if source_name is not None:
            self.get_source(source_name)
            rows = self.database.execute(
                'SELECT * FROM observations WHERE source_name = ? ORDER BY id DESC LIMIT ?',
                (source_name, limit)).fetchall()
        else:
            rows = self.database.execute(
                'SELECT * FROM observations ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['generation'] = self._generation(row)
            result.append(item)
        return result

    def confirm_generation(self, source_name, observation_id=None):
        """Explicit operator confirmation for an independent observation."""
        observation = self._resolve_observation(source_name, observation_id)
        if observation['commit_sha'] is None:
            raise AcquireError('cannot confirm an observation whose branch has disappeared')
        if not observation['independent']:
            raise AcquireError(
                'observation %d is continuing history and needs no generation confirmation'
                % observation['id'])
        if observation['confirmed']:
            return observation
        with self.database:
            self.database.execute(
                'UPDATE observations SET confirmed = 1 WHERE id = ?', (observation['id'],))
        return self.get_observation(observation['id'])

    def _resolve_observation(self, source_name, observation_id):
        if observation_id is None:
            row = self.database.execute(
                'SELECT * FROM observations WHERE source_name = ? ORDER BY id DESC LIMIT 1',
                (source_name,)).fetchone()
            if row is None:
                raise AcquireError('source has no observations yet: ' + source_name)
        else:
            row = self.database.execute(
                'SELECT * FROM observations WHERE id = ? AND source_name = ?',
                (observation_id, source_name)).fetchone()
            if row is None:
                raise AcquireError(
                    'observation %s does not belong to source %s'
                    % (observation_id, source_name))
        return dict(row)

    # ------------------------------------------------------------------- adoption
    def adopt(self, source_name, observation_id=None):
        """Take one pinned commit into the current view, atomically."""
        source = self.get_source(source_name)
        observation = self._resolve_observation(source_name, observation_id)
        commit = observation['commit_sha']
        if commit is None:
            raise AcquireError(
                'observation %d has no commit (branch disappeared); nothing to adopt'
                % observation['id'])
        if observation['independent'] and not observation['confirmed']:
            raise AcquireError(
                'observation %d is an independent generation (%s); an operator must '
                'confirm it before it can replace the current view'
                % (observation['id'], observation['relationship']))

        watermark = self.database.execute(
            'SELECT commit_sha FROM adoptions WHERE id = ?',
            (source['current_adoption_id'],)).fetchone() if source['current_adoption_id'] else None
        current_commit = watermark['commit_sha'] if watermark else None
        if current_commit == commit:
            return {'already_adopted': True, 'source': source_name,
                    'commit': commit, 'adoption_id': source['current_adoption_id']}
        if current_commit is not None:
            descendant = gitread.is_ancestor(source['repo_path'], current_commit, commit)
            if not descendant and not (observation['independent'] and observation['confirmed']):
                raise AcquireError(
                    'commit %s is not a descendant of the current watermark %s; refusing to '
                    'rewind or diverge the view without generation confirmation'
                    % (commit, current_commit))

        snapshot = self._build_snapshot(source, commit, observation['tree_sha'])

        # Test-only crash hook: a real kill happens before the transaction is
        # committed, so every relation must remain on the previous commit.
        crash = os.environ.get('SUPPLYTREE_CRASH_BEFORE_COMMIT')
        if crash and crash == source_name:
            marker = os.environ.get('SUPPLYTREE_CRASH_MARKER')
            if marker:
                Path(marker).touch()
            import time
            time.sleep(30)

        with self.database:
            # Raw objects are materialized inside the transaction. A kill or a
            # rollback therefore never leaves an orphaned blob referenced by no
            # adoption, and the content-addressed write itself is idempotent.
            for git_sha, payload in snapshot['blob_payloads'].items():
                snapshot['blobs'][git_sha] = self._store_blob(git_sha, payload)
            cursor = self.database.execute(
                'INSERT INTO adoptions (source_name, observation_id, commit_sha, tree_sha, '
                'adopted_at) VALUES (?, ?, ?, ?, ?)',
                (source_name, observation['id'], commit, observation['tree_sha'], _utcnow()))
            adoption_id = cursor.lastrowid
            for blob in snapshot['blobs'].values():
                self.database.execute(
                    'INSERT OR IGNORE INTO git_blobs VALUES (?, ?, ?, ?)',
                    (blob['git_sha'], blob['size'], blob['raw_digest'], blob['stored_path']))
            for record in snapshot['records']:
                cursor = self.database.execute(
                    'INSERT INTO catalog_records (adoption_id, kind, business_id, git_blob, '
                    'tree_sha, path) VALUES (?, ?, ?, ?, ?, ?)',
                    (adoption_id, record['kind'], record['business_id'],
                     record['git_blob'], observation['tree_sha'], record['path']))
                record_id = cursor.lastrowid
                for outlet_id in record['outlet_ids']:
                    self.database.execute(
                        'INSERT INTO product_outlet_refs VALUES (?, ?)',
                        (record_id, outlet_id))
            for exclusion in snapshot['exclusions']:
                self.database.execute(
                    'INSERT INTO exclusions (adoption_id, mode, object_type, git_sha, path, '
                    'path_hex, reason) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (adoption_id, exclusion.get('mode'), exclusion.get('object_type'),
                     exclusion.get('git_sha'), exclusion.get('path'),
                     exclusion.get('path_hex'), exclusion['reason']))
            self.database.execute(
                'UPDATE sources SET current_adoption_id = ? WHERE name = ?',
                (adoption_id, source_name))
        return {
            'already_adopted': False,
            'source': source_name,
            'adoption_id': adoption_id,
            'observation_id': observation['id'],
            'commit': commit,
            'tree': observation['tree_sha'],
            'products': sum(1 for r in snapshot['records'] if r['kind'] == 'product'),
            'outlets': sum(1 for r in snapshot['records'] if r['kind'] == 'outlet'),
            'excluded': len(snapshot['exclusions']),
        }

    def _store_blob(self, git_sha, payload):
        import hashlib
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.blob_dir / digest
        if not destination.exists():
            descriptor, name = tempfile.mkstemp(dir=self.blob_dir)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, destination)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return {'git_sha': git_sha, 'size': len(payload),
                'raw_digest': digest, 'stored_path': str(destination)}

    def _build_snapshot(self, source, commit, tree_sha):
        """Read and validate a full commit snapshot before any DB or file writes.

        Blob bytes are held in memory and only materialized inside the adoption
        transaction, so an interrupted or failed adoption leaves no objects on
        disk that are not backed by a committed adoption.
        """
        entries = gitread.list_tree(source['repo_path'], commit)
        blobs = {}
        blob_payloads = {}
        records = []
        exclusions = []
        seen_keys = {}  # (kind, business_id) -> first path
        parsed = {}     # (kind, business_id) -> record dict

        for mode, object_type, oid, path_bytes in entries:
            try:
                path = path_bytes.decode('utf-8')
                decodable = True
            except UnicodeDecodeError:
                path = None
                decodable = False

            if not decodable:
                exclusions.append({'mode': mode, 'object_type': object_type, 'git_sha': oid,
                                   'path': None, 'path_hex': path_bytes.hex(),
                                   'reason': 'invalid-utf8-path'})
                continue
            if not _safe_relative_path(path):
                exclusions.append({'mode': mode, 'object_type': object_type, 'git_sha': oid,
                                   'path': path, 'path_hex': None,
                                   'reason': 'unsafe-path-outside-root'})
                continue
            if mode == GITLINK_MODE:
                exclusions.append({'mode': mode, 'object_type': object_type, 'git_sha': oid,
                                   'path': path, 'path_hex': None,
                                   'reason': 'gitlink-submodule-not-expanded'})
                continue
            if mode == SYMLINK_MODE:
                exclusions.append({'mode': mode, 'object_type': object_type, 'git_sha': oid,
                                   'path': path, 'path_hex': None,
                                   'reason': 'symlink-not-followed'})
                continue

            under_products = _under_root(path, source['product_root'])
            under_outlets = _under_root(path, source['outlet_root'])
            if not under_products and not under_outlets:
                exclusions.append({'mode': mode, 'object_type': object_type, 'git_sha': oid,
                                   'path': path, 'path_hex': None,
                                   'reason': 'outside-managed-roots'})
                continue
            if object_type != 'blob' or mode not in ORDINARY_MODES:
                raise AcquireError(
                    'entry %s has unsupported mode/type %s/%s; commit %s not adopted'
                    % (path, mode, object_type, commit))

            kind = 'product' if under_products else 'outlet'
            payload = gitread.read_blob(source['repo_path'], oid)
            try:
                document = json.loads(payload.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AcquireError(
                    'blob %s at %s is not valid UTF-8 JSON: %s; commit %s not adopted'
                    % (oid, path, error, commit)) from error
            business_id = self._business_key(document, kind, path, oid, commit)
            key = (kind, business_id)
            if key in seen_keys:
                raise AcquireError(
                    'duplicate business key %s/%s appears at %s and %s in tree %s; '
                    'refusing to silently overwrite; commit %s not adopted'
                    % (kind, business_id, seen_keys[key], path, tree_sha, commit))
            seen_keys[key] = path

            outlet_ids = []
            if kind == 'product':
                raw_refs = document.get('outletIds', [])
                if not isinstance(raw_refs, list) or not all(isinstance(v, str) for v in raw_refs):
                    raise AcquireError(
                        'product %s at %s has a non-string outletIds list; commit %s not adopted'
                        % (business_id, path, commit))
                outlet_ids = raw_refs

            if oid not in blob_payloads:
                blob_payloads[oid] = payload
            record = {'kind': kind, 'business_id': business_id, 'git_blob': oid,
                      'path': path, 'outlet_ids': outlet_ids}
            records.append(record)
            parsed[key] = record

        outlet_ids_present = {key[1] for key in parsed if key[0] == 'outlet'}
        for record in records:
            if record['kind'] != 'product':
                continue
            for outlet_id in record['outlet_ids']:
                if outlet_id not in outlet_ids_present:
                    raise AcquireError(
                        'product %s at %s references unknown outlet %s; commit %s not adopted'
                        % (record['business_id'], record['path'], outlet_id, commit))
        return {'blobs': blobs, 'blob_payloads': blob_payloads,
                'records': records, 'exclusions': exclusions}

    @staticmethod
    def _business_key(document, expected_kind, path, oid, commit):
        if not isinstance(document, dict):
            raise AcquireError(
                'blob %s at %s must be a JSON object; commit %s not adopted'
                % (oid, path, commit))
        kind = document.get('kind')
        business_id = document.get('id')
        if kind != expected_kind:
            raise AcquireError(
                'blob %s at %s declares kind=%r but %s files require kind=%r; '
                'commit %s not adopted'
                % (oid, path, kind, expected_kind, expected_kind, commit))
        if not isinstance(business_id, str) or not business_id:
            raise AcquireError(
                'blob %s at %s has no string id; commit %s not adopted'
                % (oid, path, commit))
        return business_id

    # -------------------------------------------------------------------- queries
    def current_view(self, source_name):
        source = self.get_source(source_name)
        if not source['current_adoption_id']:
            return {'source': source_name, 'adoption_id': None, 'records': [],
                    'exclusions': []}
        adoption_id = source['current_adoption_id']
        records = [dict(row) for row in self.database.execute(
            'SELECT cr.*, gb.raw_digest FROM catalog_records cr '
            'JOIN git_blobs gb ON gb.git_sha = cr.git_blob '
            'WHERE cr.adoption_id = ? ORDER BY cr.kind, cr.business_id',
            (adoption_id,)).fetchall()]
        for record in records:
            record['outlet_ids'] = [row[0] for row in self.database.execute(
                'SELECT outlet_business_id FROM product_outlet_refs WHERE record_id = ? '
                'ORDER BY outlet_business_id', (record['id'],)).fetchall()]
        exclusions = [dict(row) for row in self.database.execute(
            'SELECT * FROM exclusions WHERE adoption_id = ? ORDER BY id',
            (adoption_id,)).fetchall()]
        adoption = dict(self.database.execute(
            'SELECT * FROM adoptions WHERE id = ?', (adoption_id,)).fetchone())
        return {'source': source_name, 'adoption': adoption,
                'records': records, 'exclusions': exclusions}

    def history(self, source_name, kind=None, business_id=None):
        """Trace one business key (or the whole source) across every adoption.

        Each row names the commit, tree, blob and observation that adopted it;
        a NULL blob means the key was absent from that complete commit.
        """
        self.get_source(source_name)
        sql = ('SELECT a.id AS adoption_id, a.observation_id, a.commit_sha, a.tree_sha, '
               'a.adopted_at, o.relationship, cr.kind, cr.business_id, cr.git_blob, '
               'cr.path, gb.raw_digest '
               'FROM adoptions a '
               'JOIN observations o ON o.id = a.observation_id '
               'LEFT JOIN catalog_records cr ON cr.adoption_id = a.id')
        conditions = ['a.source_name = ?']
        parameters = [source_name]
        if kind is not None:
            conditions.append('(cr.kind = ? OR cr.kind IS NULL)')
            parameters.append(kind)
        if business_id is not None:
            conditions.append('(cr.business_id = ? OR cr.business_id IS NULL)')
            parameters.append(business_id)
        sql += ' LEFT JOIN git_blobs gb ON gb.git_sha = cr.git_blob WHERE '
        sql += ' AND '.join(conditions)
        sql += ' ORDER BY a.id, cr.kind, cr.business_id'
        return [dict(row) for row in self.database.execute(sql, parameters).fetchall()]
    def provenance(self, git_sha=None, record_id=None):
        """Trace a stored blob or catalog record to commit, tree and observation."""
        if not git_sha and not record_id:
            raise AcquireError('provenance requires --blob or --record')
        if record_id is not None:
            row = self.database.execute(
                'SELECT cr.*, a.commit_sha, a.tree_sha AS commit_tree, a.observation_id, '
                'a.adopted_at, o.relationship, gb.raw_digest, gb.stored_path, a.source_name '
                'FROM catalog_records cr '
                'JOIN adoptions a ON a.id = cr.adoption_id '
                'JOIN observations o ON o.id = a.observation_id '
                'JOIN git_blobs gb ON gb.git_sha = cr.git_blob '
                'WHERE cr.id = ?', (record_id,)).fetchone()
            if row is None:
                raise AcquireError('unknown catalog record: ' + str(record_id))
            result = dict(row)
            result['outlet_ids'] = [r[0] for r in self.database.execute(
                'SELECT outlet_business_id FROM product_outlet_refs WHERE record_id = ? '
                'ORDER BY outlet_business_id', (record_id,)).fetchall()]
            return result
        if not gitread.is_sha1(git_sha):
            raise AcquireError('not a Git object id: ' + repr(git_sha))
        rows = self.database.execute(
            'SELECT cr.id AS record_id, cr.kind, cr.business_id, cr.path, cr.tree_sha, '
            'a.source_name, a.id AS adoption_id, a.commit_sha, a.observation_id, '
            'a.adopted_at, o.relationship, gb.git_sha, gb.raw_digest, gb.stored_path '
            'FROM git_blobs gb '
            'JOIN catalog_records cr ON cr.git_blob = gb.git_sha '
            'JOIN adoptions a ON a.id = cr.adoption_id '
            'JOIN observations o ON o.id = a.observation_id '
            'WHERE gb.git_sha = ? ORDER BY a.id, cr.id', (git_sha,)).fetchall()
        if not rows:
            raise AcquireError('blob has never been adopted: ' + git_sha)
        return {'git_sha': git_sha, 'references': [dict(row) for row in rows]}
