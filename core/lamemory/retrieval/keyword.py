"""可重建的关键词投影：英文词与中文双字片段，无伪语义向量。"""
import re
import unicodedata


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def terms(text):
    text = normalized(text)
    result = set(re.findall(r"[a-z0-9_]+", text))
    for span in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(span[i:i + 2] for i in range(len(span) - 1))
        if len(span) == 1:
            result.add(span)
    return result


def rank(query, items):
    query_terms = terms(query)
    if not query_terms:
        return []
    ranked = []
    for item in items:
        matched = query_terms & terms(item.content)
        # A single Chinese character query is useful even inside a longer word.
        matched.update(t for t in query_terms if len(t) == 1 and t in normalized(item.content))
        if matched:
            score = len(matched) / len(query_terms)
            if normalized(query) in normalized(item.content):
                score += 1
            ranked.append((item, score, "关键词匹配：" + ", ".join(sorted(matched))))
    return sorted(ranked, key=lambda row: (-row[1], -row[0].revision, row[0].memory_id))
