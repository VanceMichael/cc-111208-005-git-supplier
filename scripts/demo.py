import json
from pathlib import Path
import subprocess
import sys
import tempfile
from .materialize import main as materialize
from .source_server import running


def main():
    materialize()
    from .git_source import generate
    with tempfile.TemporaryDirectory() as directory:
        source = generate(Path(directory) / 'supplier')
        subprocess.run([sys.executable, '-m', 'supplytree', 'trees', source['repository']], check=True)


if __name__ == '__main__':
    main()
