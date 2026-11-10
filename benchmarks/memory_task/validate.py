"""Validate the frozen task bundle without network, model calls or user data access."""
from __future__ import annotations

import ast
import hashlib
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def check(condition, message):
    if not condition:
        raise ValueError(message)


def local(relative):
    path = (ROOT / relative).resolve()
    check(path.is_relative_to(ROOT) and path.is_file(), f"Invalid file: {relative}")
    return path


def jsonc(text):
    """Remove comments/trailing commas outside JSON strings, preserving URLs."""
    result, i = [], 0
    while i < len(text):
        if text[i] == '"':
            start = i
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            result.append(text[start:i])
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            result.append(" ")
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            check(end >= 0, "Unterminated JSONC comment")
            i = end + 2
            result.append(" ")
        elif text[i] == ",":
            # Defer comma removal until comments have also been stripped.
            result.append(text[i])
            i += 1
        else:
            result.append(text[i])
            i += 1
    text = "".join(result)
    result, i = [], 0
    decoder = json.JSONDecoder()
    while i < len(text):
        if text[i] == '"':
            _, end = decoder.raw_decode(text, i)
            result.append(text[i:end])
            i = end
        elif text[i] == ",":
            following = text[i + 1:].lstrip()
            if not following.startswith(("}", "]")):
                result.append(",")
            i += 1
        else:
            result.append(text[i])
            i += 1
    return json.loads("".join(result))


def difference(old, new, prefix=""):
    rows = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            path = f"{prefix}.{key}" if prefix else key
            if key not in old:
                rows.append({"path": path, "action": "added", "new_value": new[key]})
            elif key not in new:
                rows.append({"path": path, "action": "removed", "old_value": old[key]})
            else:
                rows.extend(difference(old[key], new[key], path))
    elif type(old) is not type(new) or old != new:
        rows.append({"path": prefix, "action": "changed", "old_value": old, "new_value": new})
    return rows


def main():
    manifest = read("manifest.json")
    checksums = read("checksums.json")
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts
              and p.name != "checksums.json"}
    check(actual == set(checksums), "Frozen file inventory changed")
    for relative, expected in checksums.items():
        check(hashlib.sha256(local(relative).read_bytes()).hexdigest() == expected,
              f"Hash mismatch: {relative}")
    sessions = read(manifest["seed"])["sessions"]
    messages = {m["message_id"]: m for s in sessions for m in s["messages"]}
    check(len(messages) == sum(len(s["messages"]) for s in sessions), "Duplicate message IDs")
    index = read("seed/evidence_index.json")
    check(set(messages) == set(index), "Missing message evidence")
    for session in sessions:
        check(date.fromisoformat(session["date"]) <= date.fromisoformat(manifest["evaluation_date"]),
              "Seed is later than evaluation date")
        for message in session["messages"]:
            mid = message["message_id"]
            check(message["role"] == "user", f"Non-user seed: {mid}")
            check(hashlib.sha256(message["content"].encode()).hexdigest() == index[mid]["content_sha256"],
                  f"Seed content differs from evidence: {mid}")
            check(session["kind"] == index[mid]["kind"], f"Seed message kind: {mid}")
            check(session["session_id"] == index[mid]["session_id"] and
                  session["date"] == index[mid]["date"], f"Seed session metadata: {mid}")
    tasks = manifest["tasks"]
    check(len(tasks) == manifest["task_count"] == 8, "Expected 8 tasks")
    check(len({t["task_id"] for t in tasks}) == 8, "Duplicate task IDs")
    asset_count = 0
    dimensions = {}
    for task in tasks:
        tid = task["task_id"]
        prompt = local(task["prompt"]).read_text(encoding="utf-8")
        check(task["prompt"].startswith(f"tasks/{tid}/"), "Public prompt path")
        check("{{output_dir}}" in prompt and "/workspace/output" not in prompt, f"Unadapted output: {tid}")
        check(all(word not in prompt for word in ("source_message_ids", "grading_rules", "max_score:", "target_score")),
              f"Judge metadata in prompt: {tid}")
        rubric = read(task["rubric"])
        items = rubric["criteria"]
        check(task["rubric"].startswith(f"judge/{tid}/"), "Private rubric path")
        check(rubric["task_id"] == tid and sum(q["max_score"] for q in items) == rubric["max_score"] == 100,
              f"Rubric total: {tid}")
        check(len({q["item_id"] for q in items}) == len(items), f"Duplicate criterion: {tid}")
        dimensions[tid] = {key: sum(q["max_score"] for q in items if q["dimension"] == key)
                           for key in ("memory", "mixed", "general")}
        for criterion in items:
            check(criterion["dimension"] in {"memory", "mixed", "general"}, f"Unknown dimension: {tid}")
            check(criterion["max_score"] > 0, f"Invalid score: {tid}")
            refs = criterion["source_message_ids"]
            check(set(refs) <= messages.keys(), f"Unresolved evidence: {tid}")
            check(criterion["dimension"] == "general" or refs, f"Missing memory evidence: {tid}")
            check(criterion["public_requirement"] == task["prompt"], f"Missing public contract: {tid}")
        follow = read(task["follow_ups"])
        check(follow["task_id"] == tid and len(follow["messages"]) + 1 == task["max_user_rounds"] == 3,
              f"Follow-up round count: {tid}")
        check(task["seed"] == manifest["seed"] and task["timeout_seconds_per_user_round"] > 0,
              f"Runtime configuration: {tid}")
        for asset in task["assets"]:
            path = local(asset["path"])
            check(asset["path"].startswith(f"tasks/{tid}/assets/"), f"Asset outside public inputs: {tid}")
            destination = Path(asset["destination"])
            check(not destination.is_absolute() and ".." not in destination.parts, "Unsafe asset destination")
            check(hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"], f"Asset hash: {tid}")
            if path.suffix == ".py":
                ast.parse(path.read_text(encoding="utf-8"))
            asset_count += 1
    inputs = ROOT / "tasks/07_json_config_diff/assets"
    old = jsonc((inputs / "current.json").read_text(encoding="utf-8"))
    new = jsonc((inputs / "proposed.json").read_text(encoding="utf-8"))
    computed = difference(old, new)
    expected = read("judge/07_json_config_diff/expected_diff.json")
    sort_key = lambda row: row["path"]
    check(sorted(computed, key=sort_key) == sorted(expected, key=sort_key), "Reference diff differs from actual inputs")
    check(len(expected) == 16 and asset_count == 3, "Unexpected asset/reference count")
    print(json.dumps({"status": "passed", "tasks": len(tasks), "seed_user_messages": len(messages),
                      "seed_sessions": len(sessions), "assets": asset_count, "reference_diff_changes": len(expected),
                      "frozen_files": len(checksums), "score_dimensions": dimensions}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
