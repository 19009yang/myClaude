"""私有评分口径：冻结 rubric 不改写，模型原分另存，明确项由宿主一致裁决。"""
import copy
import re

from .settings import digest

POLICY_VERSION = "birthday-gift-note-v1"
GIFT_POLICY = {
    "version": POLICY_VERSION,
    "task_id": "01_parents_birthday",
    "item_id": "gift-note",
    "rubric_sha256": "e0bb1c2beaf9db53c4426fbe0cc503066434fb49f5e46906c5eba972f2950b1e",
    "tiers": {"explicit_pending_in_reminder_notes": 15, "device_plan_without_clear_note_or_status": 10,
              "generic_or_missing_gift_note": 5, "contradicts_pending_with_purchase_claim": 0},
    "interpretation": "满分要求提醒备注明确保留给父亲购买颈椎按摩仪的待办/未购买状态；仅提及计划、放在待确认或正文建议给10分；泛化礼物备注或无礼物备注给5分（原项15分、无备注扣10分）；与来源相反地声称已经购买给0分。"
}


def gift_passages(text):
    """提取 Markdown 备注字段与设备相关段落，保留事件标题作为角色上下文。"""
    sections, passages, notes = {}, [], []
    note_column = None
    calendar_title = ""
    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+)", line)
        if heading:
            level = len(heading[1])
            sections = {k: v for k, v in sections.items() if k < level}
            sections[level] = heading[2]
        context = " / ".join(sections.values())
        passage = f"{context}\n{line}"
        if line.startswith("BEGIN:VEVENT"):
            calendar_title = ""
        elif line.startswith("SUMMARY:"):
            calendar_title = line[len("SUMMARY:"):]
        elif line.startswith("DESCRIPTION:"):
            notes.append(f"{context}\n{calendar_title}\n备注：{line[len('DESCRIPTION:'):]}".replace("\\n", "\n"))
        if "按摩仪" in line:
            passages.append(passage)
        if line.lstrip().startswith("|"):
            cells = [s.strip() for s in line.strip().strip("|").split("|")]
            headers = [re.sub(r"[*\s]", "", s) for s in cells]
            if len(cells) >= 3 and "备注" in headers:
                note_column = headers.index("备注")
            elif len(cells) == 2 and headers[0] in {"备注", "提醒备注"}:
                notes.append(passage)
            elif note_column is not None and len(cells) > note_column and not all(re.fullmatch(r"[-: ]*", s) for s in cells):
                notes.append(f"{context}\n{' '.join(cells[:note_column])}\n备注：{cells[note_column]}")
        else:
            note_column = None
            if re.search(r"(?:提醒)?备注\**\s*[:：]", line):
                notes.append(passage)
    return passages, notes


def gift_score(text, *, father_names=()):
    passages, notes = gift_passages(text)
    parental = lambda p: "颈椎按摩仪" in p and (
        bool(re.search(r"爸爸|父亲|爸妈|父母|给爸", p)) or any(name in p for name in father_names))
    candidates = list(dict.fromkeys(p for p in passages + notes if parental(p)))
    for passage in candidates:
        assertion = re.sub(r"(?:不要|不得|不能|勿|别)[^。；;\n]*", "", passage)
        clauses = re.split(r"[。；;\n]", assertion)
        if any("颈椎按摩仪" in clause and re.search(r"已(?:经)?(?:购买|买好|买了|下单|购入)", clause) for clause in clauses):
            return 0, "contradicts_pending_with_purchase_claim", passage
    for note in notes:
        if not parental(note):
            continue
        pending = re.search(r"待办|尚未(?:购买|买|下单)|还没(?:买|购买)|未(?:购买|买好|下单)", note)
        conditional = re.search(r"是否仍执行|待[你您]?确认是否|[如若]需补送|待办[（(][^）)]*待确认", note)
        if pending and not conditional:
            return 15, "explicit_pending_in_reminder_notes", note
    if candidates:
        return 10, "device_plan_without_clear_note_or_status", candidates[0]
    return 5, "generic_or_missing_gift_note", "未在提醒产物中找到可核对的父亲颈椎按摩仪待办。"


def resolve_scores(data, rubric, artifacts):
    from .judging import validate_scores
    validate_scores(data, rubric)
    resolved = copy.deepcopy(data)
    resolutions = []
    if rubric.get("task_id") == GIFT_POLICY["task_id"]:
        criterion = next(c for c in rubric["criteria"] if c["item_id"] == "gift-note")
        if digest(criterion) != GIFT_POLICY["rubric_sha256"]:
            raise ValueError("gift-note rubric 已变更，必须重新审阅宿主评分口径")
        father = next(c for c in rubric["criteria"] if c["item_id"] == "father-birthday")
        names = re.findall(r"父亲([\u4e00-\u9fff]{2,4})(?=的生日)", father["criterion"])
        score, rule, evidence = gift_score(artifacts.get("birthday_reminders.md", ""), father_names=names)
        row = next(c for c in resolved["criteria"] if c["item_id"] == "gift-note")
        resolutions.append({"item_id": "gift-note", "policy": POLICY_VERSION, "rule": rule,
                            "model_score": row["score"], "resolved_score": score, "evidence": evidence,
                            "evidence_sha256": digest(evidence)})
        row.update(score=score, reason=f"宿主统一口径 {POLICY_VERSION}/{rule}；依据：{evidence}")
    result = validate_scores(resolved, rubric)
    result["policy_resolutions"] = resolutions
    result["score_source"] = "model_with_host_policy" if resolutions else "model"
    return result
