from __future__ import annotations

import argparse
import json
from pathlib import Path

from quantlab.engine import Platform
from quantlab.models import jsonable


def main():
    parser = argparse.ArgumentParser(description='Local-only QuantLab research. No broker connection or real orders.')
    parser.add_argument('--runtime', type=Path, default=Path('runtime'))
    parser.add_argument('--output', type=Path, help='Write a JSON report to a new file (never overwrite)')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('demo', 'research'):
        command = commands.add_parser(name)
        command.add_argument('--strategy', choices=('trend', 'momentum'), default='trend')
        command.add_argument('--lookback', type=int, default=63)
        command.add_argument('--seed', type=int, default=7)
        if name == 'research':
            command.add_argument('path', type=Path)
            command.add_argument('--synthetic', action='store_true', help='Explicitly label a synthetic CSV')
    commands.add_parser('state')
    stop = commands.add_parser('halt')
    stop.add_argument('--reason', required=True)
    arguments = parser.parse_args()
    if arguments.output and (arguments.output.exists() or not arguments.output.parent.is_dir()):
        parser.error('Report requires a new filename inside an existing directory')
    platform = Platform(arguments.runtime)
    try:
        if arguments.command == 'demo':
            result = platform.run_demo(arguments.strategy, arguments.lookback, arguments.seed)
        elif arguments.command == 'research':
            result = platform.research_file(arguments.path, arguments.strategy, arguments.lookback,
                                            arguments.seed, synthetic=arguments.synthetic)
        elif arguments.command == 'halt':
            result = platform.emergency_halt(arguments.reason)
        else:
            result = platform.snapshot()
        text = json.dumps(jsonable(result), ensure_ascii=False, allow_nan=False, indent=2)
        if arguments.output:
            with arguments.output.open('x', encoding='utf-8') as handle:
                handle.write(text + '\n')
            print(json.dumps({'report': str(arguments.output.resolve()), 'run_id': result.get('run_id'),
                              'verdict': result.get('validation', {}).get('verdict')}, ensure_ascii=False))
        else:
            print(text)
    finally:
        platform.close()


if __name__ == '__main__':
    main()
