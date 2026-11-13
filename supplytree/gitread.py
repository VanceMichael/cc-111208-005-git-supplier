"""Safe read-only access to an authorized local Git repository.

The intake pipeline never checks out supplier content, never runs hooks,
never expands submodules and never spawns a shell. Ref names and object ids
are always passed as discrete argv elements; tree paths are never passed to
Git at all (the whole tree is listed and filtered in Python). Git output is
NUL separated so paths may contain spaces, newlines and non-ASCII bytes.
"""

import os
import re
import subprocess

SHA1_RE = re.compile(r'[0-9a-f]{40}\Z')
ORDINARY_MODES = ('100644', '100755')
SYMLINK_MODE = '120000'
GITLINK_MODE = '160000'

_GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT='0')


class GitError(OSError):
    """Raised when a local Git command fails unexpectedly."""


def _run(repository, *arguments):
    command = [
        'git',
        '-c', 'core.hooksPath=/dev/null',
        '-c', 'protocol.file.allow=never',
        '-C', str(repository),
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=_GIT_ENV,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise GitError('git command timed out: ' + ' '.join(arguments)) from error
    return result


def ensure_repository(repository):
    """Validate an authorized local path and return its resolved repository."""
    path = os.path.realpath(str(repository))
    if not os.path.isdir(path):
        raise GitError('authorized repository path is not a directory: ' + path)
    result = _run(path, 'rev-parse', '--show-toplevel')
    if result.returncode != 0:
        raise GitError('not a git repository: ' + path)
    resolved = result.stdout.decode().strip()
    return resolved


def is_sha1(value):
    return bool(SHA1_RE.fullmatch(value or ''))


def resolve_branch_commit(repository, branch):
    """Resolve a local branch to a commit sha, returning None when absent."""
    ref = 'refs/heads/' + branch
    result = _run(
        repository,
        'rev-parse', '--verify', '--quiet', '--end-of-options',
        ref + '^{commit}',
    )
    if result.returncode == 0:
        commit = result.stdout.decode().strip()
        if not is_sha1(commit):
            raise GitError('unexpected rev-parse output: ' + commit)
        return commit
    if result.returncode == 1:
        return None
    raise GitError(result.stderr.decode(errors='replace').strip() or 'rev-parse failed')


def commit_metadata(repository, commit):
    """Return (tree_sha, parent_shas) for a pinned commit object."""
    if not is_sha1(commit):
        raise GitError('refusing to read a non-sha commit spec: ' + repr(commit))
    result = _run(repository, 'cat-file', 'commit', commit)
    if result.returncode != 0:
        raise GitError(result.stderr.decode(errors='replace').strip() or 'cat-file commit failed')
    tree = None
    parents = []
    for line in result.stdout.split(b'\n'):
        if not line:
            break
        key, _, value = line.partition(b' ')
        if key == b'tree':
            tree = value.decode()
        elif key == b'parent':
            parents.append(value.decode())
    if not is_sha1(tree):
        raise GitError('commit ' + commit + ' has no readable tree')
    return tree, parents


def is_ancestor(repository, ancestor, descendant):
    """Ancestry test based on the object graph, never on commit timestamps."""
    for sha in (ancestor, descendant):
        if not is_sha1(sha):
            raise GitError('refusing non-sha ancestry argument: ' + repr(sha))
    result = _run(repository, 'merge-base', '--is-ancestor', ancestor, descendant)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitError(result.stderr.decode(errors='replace').strip() or 'merge-base failed')


def count_commits_between(repository, ancestor, descendant):
    """Count commits reachable from descendant but not from ancestor."""
    for sha in (ancestor, descendant):
        if not is_sha1(sha):
            raise GitError('refusing non-sha rev-list argument: ' + repr(sha))
    result = _run(repository, 'rev-list', '--count', ancestor + '..' + descendant)
    if result.returncode != 0:
        raise GitError(result.stderr.decode(errors='replace').strip() or 'rev-list failed')
    return int(result.stdout.decode().strip())


def list_tree(repository, commit):
    """List every tree entry of a commit as NUL-separated raw records.

    Returns a list of (mode, object_type, blob_sha, path_bytes) tuples.
    Recursive listing means directories do not appear; submodule gitlinks
    surface as commit entries and are left to the caller to exclude.
    """
    if not is_sha1(commit):
        raise GitError('refusing to list a non-sha commit: ' + repr(commit))
    result = _run(repository, 'ls-tree', '-r', '-z', '--full-tree', commit)
    if result.returncode != 0:
        raise GitError(result.stderr.decode(errors='replace').strip() or 'ls-tree failed')
    entries = []
    for record in result.stdout.split(b'\0'):
        if not record:
            continue
        metadata, _, path_bytes = record.partition(b'\t')
        mode, object_type, oid = metadata.decode().split(' ')
        if not is_sha1(oid):
            raise GitError('unexpected ls-tree oid: ' + oid)
        entries.append((mode, object_type, oid, path_bytes))
    return entries


def read_blob(repository, blob_sha):
    """Read a blob object strictly by its hex object id."""
    if not is_sha1(blob_sha):
        raise GitError('refusing to read a non-sha blob: ' + repr(blob_sha))
    result = _run(repository, 'cat-file', 'blob', blob_sha)
    if result.returncode != 0:
        raise GitError(result.stderr.decode(errors='replace').strip() or 'cat-file blob failed')
    return result.stdout
