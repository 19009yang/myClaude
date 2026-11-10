"""维护入口：python -m core.lamemory --help。"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from . import LaMemoryConfig, LaMemoryService
from .migration import discover_legacy_paths, import_legacy, preview_legacy
from .providers import ChatMemoryProvider
from .runtime import create_memory


def main():
    parser = argparse.ArgumentParser(description="LaMemory 单机记忆维护")
    parser.add_argument("--database", default=None)
    parser.add_argument("--user", default="local-user")
    parser.add_argument("--session", default="legacy-session")
    parser.add_argument("--project")
    parser.add_argument("--offline", action="store_true", help="不调用模型；摘要使用原文摘录，模型任务保留错误")
    parser.add_argument("--retrieval-mode", choices=["keyword", "hybrid"], default=None)
    parser.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=None)
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
    for name in ("index-status", "index-enqueue"):
        commands.add_parser(name)
    command = commands.add_parser("index-process")
    command.add_argument("--max-jobs", type=int, default=16)
    command = commands.add_parser("index-flush")
    command.add_argument("--timeout", type=float, default=30)
    command = commands.add_parser("index-retry")
    command.add_argument("id")
    commands.add_parser("jobs")
    command = commands.add_parser("process")
    command.add_argument("--max-jobs", type=int, default=1)
    command = commands.add_parser("flush")
    command.add_argument("--timeout", type=float, default=30)
    commands.add_parser("refresh-summaries")
    command = commands.add_parser("summaries")
    command.add_argument("query", nargs="?", default="")
    for name in ("summary", "experience", "experience-revoke"):
        command = commands.add_parser(name)
        command.add_argument("id")
    command = commands.add_parser("topic")
    command.add_argument("key")
    command.add_argument("--sources", nargs="*", default=[])
    command.add_argument("--project-scope", action="store_true")
    command = commands.add_parser("consolidate")
    command.add_argument("--max-jobs", type=int, default=100)
    command = commands.add_parser("experience-capture")
    command.add_argument("--turn")
    command = commands.add_parser("experiences")
    command.add_argument("--status", choices=["candidate", "active", "revoked", "superseded"])
    command = commands.add_parser("experience-search")
    command.add_argument("query")
    command = commands.add_parser("experience-feedback")
    command.add_argument("id")
    command.add_argument("outcome", choices=["success", "failure", "approved"])
    command.add_argument("--sources", nargs="+", required=True)
    command.add_argument("--kind", choices=["user", "test", "model"], default="user")
    command.add_argument("--evidence", default="")
    command = commands.add_parser("experience-propose")
    command.add_argument("candidate_file", help="含 task_type/conditions/steps/result_basis/source_message_ids 的 JSON")
    command = commands.add_parser("experience-replace")
    command.add_argument("id")
    command.add_argument("candidate_file")
    args = parser.parse_args()
    if args.command == "discover":
        result = discover_legacy_paths(args.project_root)
    elif args.command == "preview":
        result = preview_legacy(session_path=args.session_file, memory_path=args.memory_file,
                                user_id=args.user, session_id=args.session)
        result = {k: v for k, v in result.items() if k not in {"messages", "entries"}}
    else:
        cfg = LaMemoryConfig(project_id=args.project, **({"database_path": args.database} if args.database else {}))
        with create_memory(cfg, user_id=args.user, session_id=args.session,
                           retrieval_mode=args.retrieval_mode, rerank=args.rerank, offline=args.offline,
                           select_space=args.command == "index-status" or (args.offline and args.command.startswith("index-")),
                           provider=None if args.offline else ChatMemoryProvider()) as service:
            if args.command == "import":
                result = import_legacy(service, session_path=args.session_file, memory_path=args.memory_file)
            elif args.command == "search":
                bundle = service.retrieve(args.query)
                result = {"mode": bundle.diagnostics["effective_mode"], "results": [h.to_dict() for h in bundle.items],
                          "summaries": bundle.summaries, "experiences": bundle.experiences, "retrieval": bundle.diagnostics}
            elif args.command == "inspect":
                result = service.inspect(args.id)
            elif args.command == "forget":
                result = service.forget(args.id)
            elif args.command == "index-status":
                result = service.embedding_status()
            elif args.command == "index-enqueue":
                result = service.enqueue_embeddings()
            elif args.command == "index-process":
                if args.offline:
                    raise ValueError("offline 不运行 embedding provider")
                result = service.process_embedding_jobs(args.max_jobs)
            elif args.command == "index-flush":
                if args.offline:
                    raise ValueError("offline 不运行 embedding provider")
                result = service.flush_embeddings(args.timeout)
            elif args.command == "index-retry":
                result = {"retried": service.retry_embedding_job(args.id)}
            elif args.command == "jobs":
                result = service.jobs()
            elif args.command == "retry":
                result = {"retried": service.retry_failed(args.id)}
            elif args.command == "process":
                result = service.process_pending(args.max_jobs)
            elif args.command == "refresh-summaries":
                result = {"queued": service.refresh_summaries()}
            elif args.command == "summaries":
                result = service.search_summaries(args.query)
            elif args.command == "summary":
                result = service.inspect_summary(args.id)
            elif args.command == "topic":
                result = {"summary_id": service.define_topic(args.key, args.sources, project=args.project_scope)}
            elif args.command == "consolidate":
                result = service.consolidate(max_jobs=args.max_jobs)
            elif args.command == "experience-capture":
                result = {"job_id": service.capture_experience(args.turn)}
            elif args.command == "experiences":
                result = service.list_experiences(args.status)
            elif args.command == "experience-search":
                result = service.search_experiences(args.query)
            elif args.command == "experience":
                result = service.inspect_experience(args.id)
            elif args.command == "experience-revoke":
                result = service.revoke_experience(args.id)
            elif args.command == "experience-feedback":
                result = service.experience_feedback(args.id, args.outcome, args.sources, evidence_kind=args.kind, evidence=args.evidence)
            elif args.command == "experience-propose":
                result = service.propose_experience(**json.loads(Path(args.candidate_file).read_text(encoding="utf-8")))
            elif args.command == "experience-replace":
                result = service.replace_experience(args.id, **json.loads(Path(args.candidate_file).read_text(encoding="utf-8")))
            else:
                result = asdict(service.flush(args.timeout))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "index-flush" and result["status"] != "complete":
        return 1
    if args.command == "index-process" and any(j["status"] == "failed" for j in result):
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
