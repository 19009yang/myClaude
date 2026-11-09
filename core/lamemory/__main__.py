"""维护入口：python -m core.lamemory --help。"""
import argparse
from dataclasses import asdict
import json

from . import LaMemoryConfig, LaMemoryService
from .migration import discover_legacy_paths, import_legacy, preview_legacy
from .providers import ChatMemoryProvider


def main():
    parser = argparse.ArgumentParser(description="LaMemory 单机记忆维护")
    parser.add_argument("--database", default=None)
    parser.add_argument("--user", default="local-user")
    parser.add_argument("--session", default="legacy-session")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "import"):
        command = commands.add_parser(name)
        command.add_argument("--session-file")
        command.add_argument("--memory-file")
    command = commands.add_parser("discover")
    command.add_argument("project_root")
    command = commands.add_parser("search")
    command.add_argument("query")
    for name in ("inspect", "forget", "retry"):
        command = commands.add_parser(name)
        command.add_argument("id")
    commands.add_parser("jobs")
    command = commands.add_parser("process")
    command.add_argument("--max-jobs", type=int, default=1)
    command = commands.add_parser("flush")
    command.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.command == "discover":
        result = discover_legacy_paths(args.project_root)
    elif args.command == "preview":
        result = preview_legacy(session_path=args.session_file, memory_path=args.memory_file,
                                user_id=args.user, session_id=args.session)
        result = {k: v for k, v in result.items() if k not in {"messages", "entries"}}
    else:
        cfg = LaMemoryConfig(**({"database_path": args.database} if args.database else {}))
        with LaMemoryService(cfg, user_id=args.user, session_id=args.session, provider=ChatMemoryProvider()) as service:
            if args.command == "import":
                result = import_legacy(service, session_path=args.session_file, memory_path=args.memory_file)
            elif args.command == "search":
                result = {"mode": "keyword", "results": [h.to_dict() for h in service.search(args.query)]}
            elif args.command == "inspect":
                result = service.inspect(args.id)
            elif args.command == "forget":
                result = service.forget(args.id)
            elif args.command == "jobs":
                result = service.jobs()
            elif args.command == "retry":
                result = {"retried": service.retry_failed(args.id)}
            elif args.command == "process":
                result = service.process_pending(args.max_jobs)
            else:
                result = asdict(service.flush(args.timeout))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
