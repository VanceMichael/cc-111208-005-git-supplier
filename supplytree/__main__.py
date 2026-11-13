import argparse
import json
from pathlib import Path
import sys
from .parser import inspect
from .journal import Journal
from .transport import capture
from .acquire import Acquirer


def build_parser():
    parser = argparse.ArgumentParser(description='供应商商品目录 Git 树与不可变对象检查工具。')
    sub = parser.add_subparsers(dest='command', required=True)
    view = sub.add_parser('trees', help='单份来源的 Git 树结构摘要（不含业务接收）')
    view.add_argument('source')
    view.add_argument('--revision', default='HEAD')
    raw = sub.add_parser('capture', help='loopback HTTP 原文留存')
    raw.add_argument('url')
    raw.add_argument('--state', default='var')
    raw.add_argument('--method', default='GET')
    raw.add_argument('--body')
    raw.add_argument('--header', action='append', default=[])

    acquire = sub.add_parser('acquire', help='固定 commit 的业务接收流程')
    acquire.add_argument('--state', default='var/acquire', help='SQLite 与原文对象状态目录')
    task = acquire.add_subparsers(dest='task', required=True)

    add = task.add_parser('add-source', help='登记一个授权本地仓库来源')
    add.add_argument('name')
    add.add_argument('repository')
    add.add_argument('--branch', required=True)
    add.add_argument('--product-root', required=True)
    add.add_argument('--outlet-root', required=True)

    observe = task.add_parser('observe', help='观察分支一次并固定到真实 commit')
    observe.add_argument('source')

    confirm = task.add_parser('confirm-generation', help='显式确认独立观察代际')
    confirm.add_argument('source')
    confirm.add_argument('--observation', type=int)

    adopt = task.add_parser('adopt', help='把一个已观察 commit 完整接收到当前视图')
    adopt.add_argument('source')
    adopt.add_argument('--observation', type=int)

    obs = task.add_parser('observations', help='列出分支观察与快进/回退/分叉分类')
    obs.add_argument('source', nargs='?')
    obs.add_argument('--limit', type=int, default=50)

    current = task.add_parser('current', help='查看来源当前视图（商品、网点、排除项）')
    current.add_argument('source')

    history = task.add_parser('history', help='按业务键追溯 commit/tree/blob/观察')
    history.add_argument('source')
    history.add_argument('--kind', choices=('product', 'outlet'))
    history.add_argument('--id', dest='business_id')

    provenance = task.add_parser('provenance', help='按 blob 或记录 ID 追溯来源')
    provenance.add_argument('source')
    provenance.add_argument('--blob')
    provenance.add_argument('--record', type=int)
    return parser


def run_acquire(args):
    with Acquirer(args.state) as acquirer:
        if args.task == 'add-source':
            return acquirer.add_source(args.name, args.repository, args.branch,
                                       args.product_root, args.outlet_root)
        if args.task == 'observe':
            return acquirer.observe(args.source)
        if args.task == 'confirm-generation':
            return acquirer.confirm_generation(args.source, args.observation)
        if args.task == 'adopt':
            return acquirer.adopt(args.source, args.observation)
        if args.task == 'observations':
            return {'observations': acquirer.list_observations(args.source, args.limit)}
        if args.task == 'current':
            return acquirer.current_view(args.source)
        if args.task == 'history':
            return {'history': acquirer.history(args.source, args.kind, args.business_id)}
        if args.task == 'provenance':
            return acquirer.provenance(args.blob, args.record)
    raise AssertionError('unhandled acquire task')


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            headers = dict(value.split(':', 1) for value in args.header)
            headers = {key.strip(): value.strip() for key, value in headers.items()}
            status, response_headers, payload = capture(args.url, args.method,
                Path(args.body).read_bytes() if args.body else None, headers)
            journal = Journal(args.state)
            try:
                result = journal.save(payload, args.url)
            finally:
                journal.close()
            result.update({'http_status': status, 'headers': response_headers})
        elif args.command == 'trees':
            result = inspect(args.source, args.revision)
        else:
            result = run_acquire(args)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
