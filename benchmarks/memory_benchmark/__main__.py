"""python -m benchmarks.memory_benchmark [estimate|smoke|run] ..."""
import argparse
from datetime import datetime
import json
from pathlib import Path

from .accounting import Prices
from .estimate import estimate
from .settings import ROOT, read_settings, save_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="两套记忆系统及无记忆基线的隔离任务评测（默认只预估）")
    parser.add_argument("mode", nargs="?", default="estimate", choices=("estimate", "smoke", "run"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--prices", type=Path, default=Path(__file__).parent / "prices.deepseek-flash.json")
    parser.add_argument("--groups", nargs="+", choices=("none", "legacy", "lamemory"))
    parser.add_argument("--tasks", nargs="+")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--max-cost", type=float, help="费用预算，币种与 prices 一致；按请求预留估算限制")
    parser.add_argument("--output", type=Path, help="estimate 是 JSON 文件；smoke/run 是新建的运行目录")
    args = parser.parse_args(argv)
    try:
        settings = read_settings(args.config, groups=args.groups, tasks=args.tasks, repeats=args.repeats,
                                 max_cost=args.max_cost)
        prices = Prices(args.prices)
        if args.mode == "estimate":
            from .run import validate_bundle
            validate_bundle()
            report = estimate(settings, prices)
            if args.output:
                if args.output.exists():
                    raise ValueError("预估文件已存在；请使用新路径")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                save_json(args.output, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        from .run import execute
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output = args.output or ROOT / ".myclaude/benchmarks" / f"{args.mode}-{stamp}"
        report = execute(settings, prices, output, offline=args.mode == "smoke")
        print(json.dumps({"status": report["status"], "report": str(output.resolve() / "report.json"),
                          "usage": report["usage"]}, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "complete" else 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
