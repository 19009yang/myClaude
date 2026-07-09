"""研究助手工作流 - LLM Prompt 模板"""

# ── SelectNode: 分析搜索结果，推荐论文 ──

SELECT_SYSTEM_PROMPT = """你是一个学术研究助手，擅长从大量论文中筛选最有价值的研究。
你的任务是根据用户的研究主题，从搜索结果中推荐最值得深入阅读的论文。
请综合考虑：与研究主题的相关性、论文的影响力、发表时间的时效性、研究方向的多样性。"""

SELECT_USER_PROMPT = """用户研究主题: {query}

以下是 arxiv 搜索结果:

{papers_text}

请从中推荐 3-5 篇最值得深入阅读的论文。对每篇推荐论文，给出：
1. 选择理由（与研究主题的关联）
2. 预期价值（阅读后能获得什么）

请直接输出 JSON 格式（不要用代码块包裹）：
{{"selected_ids": ["arxiv_id_1", "arxiv_id_2", ...], "reasons": [{{"id": "...", "reason": "...", "value": "..."}}]}}"""


# ── TopicNode: 阅读论文，提出写作主题 ──

TOPIC_SYSTEM_PROMPT = """你是一个学术创意顾问，擅长从研究文献中提炼新颖的写作角度。
你基于对多篇论文的深度理解，提出有深度、有创见、有写作价值的主题建议。
每个主题应该是独特的切入角度，而非简单的综述性标题。"""

TOPIC_USER_PROMPT = """用户原始研究主题: {query}

以下是 {paper_count} 篇论文的摘要和核心内容:

{papers_summary}

请基于以上论文内容，提出 2-3 个写作主题建议。每个主题需包含：
1. title: 建议的论文标题
2. angle: 切入角度和创新点
3. rationale: 为什么这个主题值得写，能填补什么研究空白或提供什么新视角
4. references: 可以引用哪些已读论文作为支撑

请直接输出 JSON 格式（不要用代码块包裹）：
{{"topics": [{{"title": "...", "angle": "...", "rationale": "...", "references": ["id1", "id2"]}}]}}"""


# ── WritingNode: 基于 LaTeX 格式撰写论文 ──

WRITING_SYSTEM_PROMPT = """你是一位学术写作专家，擅长撰写高质量的学术论文。
你的输出必须是完整的 LaTeX 代码，包含完整的文档结构。
请确保：
- 使用合适的 \\documentclass（如 article 或 llncs）
- 包含必要的宏包（amsmath, graphicx, hyperref 等）
- 结构完整：title, abstract, introduction, methodology/results, discussion, conclusion, references
- 参考文献使用 \\bibitem 手工列举（不依赖 bibtex 文件）
- LaTeX 代码可直接编译，无需额外文件"""

WRITING_USER_PROMPT = """请基于以下信息撰写一篇学术论文的 LaTeX 源码:

## 选定写作主题
标题: {topic_title}
切入角度: {topic_angle}
写作理由: {topic_rationale}

## 参考论文
{references_text}

要求:
1. 论文长度适中（约 3000-5000 字的内容量）
2. 包含摘要、引言、主体部分、结论、参考文献
3. 正确引用参考论文
4. 输出纯 LaTeX 代码，不要有任何额外解释文字
5. LaTeX 代码必须以 \\documentclass 开头，以 \\end{{document}} 结尾"""


# ── 格式化辅助函数 ──

def format_papers_for_select(papers: list[dict]) -> str:
    """将搜索结果格式化为 SelectNode 可读的文本。"""
    lines = []
    for i, p in enumerate(papers, 1):
        authors_str = ", ".join(p.get("authors", [])[:5])
        if len(p.get("authors", [])) > 5:
            authors_str += " et al."
        lines.append(
            f"[{i}] ID: {p['arxiv_id']}\n"
            f"    Title: {p['title']}\n"
            f"    Authors: {authors_str}\n"
            f"    Category: {p.get('primary_category', '')}\n"
            f"    Published: {p.get('published', '')}\n"
            f"    Summary: {p.get('summary', '')[:200]}"
        )
    return "\n\n".join(lines)


def format_papers_for_topic(parsed_papers: list[dict]) -> str:
    """将解析后的论文格式化为 TopicNode 可读的摘要文本。"""
    lines = []
    for p in parsed_papers:
        # 截取前 1500 字，避免 token 过长
        text = p.get("text", "")
        if len(text) > 1500:
            text = text[:1500] + "...(truncated)"
        lines.append(
            f"### {p.get('title', 'Unknown')}\n"
            f"ID: {p.get('arxiv_id', '')}\n\n"
            f"{text}"
        )
    return "\n\n".join(lines)


def format_references_for_writing(parsed_papers: list[dict], ref_ids: list[str]) -> str:
    """将指定论文格式化为 WritingNode 可引用的参考信息。"""
    lines = []
    for p in parsed_papers:
        if p.get("arxiv_id") in ref_ids:
            # 截取前 2000 字
            text = p.get("text", "")
            if len(text) > 2000:
                text = text[:2000] + "...(truncated)"
            authors_str = ", ".join(p.get("authors", [])[:5])
            lines.append(
                f"#### {p.get('title', '')}\n"
                f"Authors: {authors_str}\n"
                f"ID: {p.get('arxiv_id', '')}\n\n"
                f"{text}"
            )
    return "\n\n".join(lines) if lines else "无参考论文详细内容"


if __name__ == "__main__":
    # 演示 prompt 模板
    sample_papers = [
        {"arxiv_id": "2301.00123", "title": "Test Paper", "authors": ["A", "B"],
         "primary_category": "cs.AI", "published": "2023-01-01", "summary": "A test summary."},
    ]
    print("SELECT_USER_PROMPT:")
    print(SELECT_USER_PROMPT.format(
        query="deep learning",
        papers_text=format_papers_for_select(sample_papers),
    )[:300])
    print("\n" + "=" * 50)
    print("TOPIC_USER_PROMPT:")
    print(TOPIC_USER_PROMPT.format(
        query="deep learning",
        paper_count=1,
        papers_summary="### Test Paper\nSome content...",
    )[:300])
