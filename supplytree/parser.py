import subprocess


def git(repository, *arguments):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repository), *arguments],
                            check=True, capture_output=True)
    return result.stdout


def inspect(repository, revision='HEAD'):
    commit = git(repository, 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}').decode().strip()
    tree = git(repository, 'ls-tree', '-rz', '--full-tree', commit)
    entries = []
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        metadata, path = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        entries.append({'mode': mode, 'kind': kind, 'oid': oid,
                        'path': path.decode('utf-8', errors='surrogateescape')})
    return {'commit': commit, 'entries': entries}
