"""真实检索 smoke：固定合成 fixture、临时库；缺配置明确 blocked。"""
import argparse
import json
from pathlib import Path
from .hybrid_retrieval import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rerank', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出已存在；请指定新文件')
    result = evaluate('hybrid-rerank' if args.rerank else 'hybrid')
    result = next(iter(result.values()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write('\n')
    print(json.dumps({'status': result['status'], 'reason': result.get('reason'), 'synthetic_only': True}))
    return 0 if result['status'] == 'complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
