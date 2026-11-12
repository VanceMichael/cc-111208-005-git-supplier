import json
from pathlib import Path
import subprocess


def generate(destination):
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    def git(*arguments):
        return subprocess.check_output(['git', '-C', str(root), *arguments], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-b', 'catalog')
    git('config', 'user.name', 'Synthetic Supplier')
    git('config', 'user.email', 'supplier@example.invalid')
    (root / 'products').mkdir()
    (root / 'outlets').mkdir()
    original = root / 'products' / '商品 name\nline.json'
    original.write_bytes(Path('fixtures/product.json').read_bytes())
    (root / 'outlets' / 'store.json').write_bytes(Path('fixtures/outlet.json').read_bytes())
    git('add', '.')
    git('commit', '-m', 'Synthetic catalog')
    initial = git('rev-parse', 'HEAD')
    original.rename(root / 'products' / 'moved.json')
    git('add', '-A')
    git('commit', '-m', 'Move without changing identity')
    moved = git('rev-parse', 'HEAD')
    git('branch', 'rewound', initial)
    return {'repository': str(root), 'initial': initial, 'moved': moved, 'rewound': initial}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('destination')
    print(json.dumps(generate(parser.parse_args().destination)))
