"""研究助手工作流

论文搜索 → 挑选下载 → 解析论文 → 提出写作主题 → 用户评审 → LaTeX写作 → PDF渲染 → 结束

使用 Node/Flow DAG 编排。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Tuple
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core import Node, Flow, shared, call_llm
import importlib.util
from tools import get_tools, ToolExecutor, SafetyGuard, search_papers, download_papers, render_latex

from Agent.ResearchAssistant.prompts import (
    SELECT_SYSTEM_PROMPT, SELECT_USER_PROMPT,
    TOPIC_SYSTEM_PROMPT, TOPIC_USER_PROMPT,
    WRITING_SYSTEM_PROMPT, WRITING_USER_PROMPT,
    format_papers_for_select,
    format_papers_for_topic,
    format_references_for_writing,
)


# ──────────────────────────────────────────────
#  Node 定义
# ──────────────────────────────────────────────


class SearchNode(Node):
    """搜索论文：调用 search_papers 工具"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        query = payload.get("query", "")
        if not query:
            return "error", {"error": "请输入研究主题关键词"}

        print(f"\n🔍 正在搜索论文: {query}")
        try:
            papers = search_papers(query, max_results=10)
        except Exception as exc:
            print(f"  ❌ 搜索失败: {exc}")
            return "error", {"error": f"搜索失败: {exc}"}

        print(f"  ✅ 找到 {len(papers)} 篇论文")
        for i, p in enumerate(papers[:5], 1):
            print(f"    [{i}] {p['arxiv_id']} - {p['title'][:50]}...")

        return "select", {"query": query, "papers": papers}


class SelectNode(Node):
    """LLM 分析搜索结果，推荐最感兴趣的论文"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        query = payload.get("query", "")
        papers = payload.get("papers", [])

        if not papers:
            print("  ⚠️ 无搜索结果可供选择")
            return "error", {"error": "无搜索结果"}

        print("\n📋 分析搜索结果，推荐论文...")

        papers_text = format_papers_for_select(papers)
        messages = [
            {"role": "system", "content": SELECT_SYSTEM_PROMPT},
            {"role": "user", "content": SELECT_USER_PROMPT.format(
                query=query, papers_text=papers_text
            )},
        ]
        response = call_llm(messages=messages)
        content = response.get("content", "")

        # 解析 LLM 返回的 JSON
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    result = {}
            else:
                result = {}

        selected_ids = result.get("selected_ids", [])
        reasons = result.get("reasons", [])

        # 如果 LLM 未返回有效 ID，取前 5 篇作为默认
        if not selected_ids:
            selected_ids = [p["arxiv_id"] for p in papers[:5]]
            print("  ⚠️ LLM 未返回推荐，默认选择前 5 篇")
        else:
            print(f"  ✅ 推荐下载 {len(selected_ids)} 篇论文:")
            for r in reasons:
                print(f"    - {r.get('id', '?')}: {r.get('reason', '')[:60]}...")

        # 展示给用户确认
        print("\n  📝 推荐下载的论文 ID:")
        for i, aid in enumerate(selected_ids, 1):
            matching = [p for p in papers if p["arxiv_id"] == aid]
            title = matching[0]["title"][:60] if matching else "未知"
            print(f"    [{i}] {aid} - {title}")

        user_confirm = input("\n  是否确认下载以上论文？(y/修改/n): ").strip().lower()

        if user_confirm == "n":
            return "error", {"error": "用户取消下载"}
        elif user_confirm.startswith("y"):
            pass
        else:
            try:
                custom_ids = [x.strip() for x in user_confirm.split(",") if x.strip()]
                if custom_ids:
                    selected_ids = custom_ids
                    print(f"  ✅ 用户自定义下载列表: {selected_ids}")
            except Exception:
                pass

        return "download", {"query": query, "selected_ids": selected_ids, "papers": papers}


class DownloadNode(Node):
    """下载选定论文的 PDF"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        selected_ids = payload.get("selected_ids", [])
        query = payload.get("query", "")

        if not selected_ids:
            return "error", {"error": "无论文 ID 需下载"}

        print(f"\n📥 下载 {len(selected_ids)} 篇论文...")
        save_dir = str(Path.cwd() / "papers")
        result = download_papers(selected_ids, save_dir=save_dir)

        downloaded = {}
        failed_ids = []
        for aid, info in result.items():
            if info["status"] == "success":
                downloaded[aid] = info["path"]
                print(f"  ✅ {aid} → {info['path']}")
            else:
                failed_ids.append(aid)
                print(f"  ❌ {aid}: {info['path']}")

        if not downloaded:
            return "error", {"error": "所有论文下载失败"}

        if failed_ids:
            print(f"  ⚠️ {len(failed_ids)} 篇下载失败，继续处理成功部分")

        return "parse", {
            "query": query,
            "downloaded": downloaded,
            "papers": payload.get("papers", []),
        }


class ParseNode(Node):
    """解析下载的 PDF 为文本"""

    # 动态加载 pdf_text_extractor（目录名含连字符，无法直接 import）
    _pdf_extractor = None

    @classmethod
    def _load_pdf_extractor(cls):
        if cls._pdf_extractor is None:
            script_path = Path(__file__).parent.parent.parent / "tools" / "skills" / \
                          "pdf-image-text-extractor" / "scripts" / "pdf_text_extractor.py"
            spec = importlib.util.spec_from_file_location("pdf_text_extractor", str(script_path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls._pdf_extractor = module.extract_text_from_pdf
        return cls._pdf_extractor

    def exec(self, payload: Any) -> Tuple[str, Any]:
        downloaded = payload.get("downloaded", {})
        papers_info = payload.get("papers", [])
        query = payload.get("query", "")

        if not downloaded:
            return "error", {"error": "无论文可解析"}

        print(f"\n📖 解析 {len(downloaded)} 篇论文...")

        parsed_papers = []
        extract_fn = self._load_pdf_extractor()
        for aid, pdf_path in downloaded.items():
            print(f"  📄 解析: {aid}")
            try:
                result = extract_fn(pdf_path)
                if result.get("success"):
                    matching = [p for p in papers_info if p["arxiv_id"] == aid]
                    title = matching[0]["title"] if matching else aid
                    authors = matching[0].get("authors", []) if matching else []

                    parsed_papers.append({
                        "arxiv_id": aid,
                        "title": title,
                        "authors": authors,
                        "text": result["text"],
                        "page_count": result.get("page_count", 0),
                    })
                    print(f"    ✅ 解析成功 ({result.get('page_count', 0)} 页)")
                else:
                    print(f"    ❌ 解析失败: {result.get('error', '')[:80]}")
            except Exception as exc:
                print(f"    ❌ 解析异常: {exc}")

        if not parsed_papers:
            return "error", {"error": "所有论文解析失败"}

        print(f"  ✅ 成功解析 {len(parsed_papers)} 篇论文")

        return "topic", {"query": query, "parsed_papers": parsed_papers}


class TopicNode(Node):
    """LLM 阅读论文，提出写作主题建议"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        query = payload.get("query", "")
        parsed_papers = payload.get("parsed_papers", [])

        if not parsed_papers:
            return "error", {"error": "无论文内容可分析"}

        print("\n💡 分析论文内容，提出写作主题...")

        papers_summary = format_papers_for_topic(parsed_papers)
        messages = [
            {"role": "system", "content": TOPIC_SYSTEM_PROMPT},
            {"role": "user", "content": TOPIC_USER_PROMPT.format(
                query=query,
                paper_count=len(parsed_papers),
                papers_summary=papers_summary,
            )},
        ]
        response = call_llm(messages=messages)
        content = response.get("content", "")

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    result = {"topics": []}
            else:
                result = {"topics": []}

        topics = result.get("topics", [])

        if not topics:
            topics = [
                {
                    "title": f"基于{query}的研究综述",
                    "angle": f"综合分析{query}领域的最新进展",
                    "rationale": "作为默认主题，覆盖搜索到的论文内容",
                    "references": [p["arxiv_id"] for p in parsed_papers[:3]],
                }
            ]
            print("  ⚠️ LLM 未返回主题建议，使用默认主题")

        for topic in topics:
            if not topic.get("references"):
                topic["references"] = [p["arxiv_id"] for p in parsed_papers[:3]]

        return "review", {
            "topics": topics,
            "parsed_papers": parsed_papers,
            "query": query,
        }


class ReviewGateNode(Node):
    """人工审批点：展示主题建议，等待用户选择"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        topics = payload.get("topics", [])
        parsed_papers = payload.get("parsed_papers", [])

        print("\n" + "=" * 60)
        print("📝 写作主题建议")
        print("=" * 60)

        for i, t in enumerate(topics, 1):
            refs_str = ", ".join(t.get("references", []))
            print(f"\n  [{i}] {t['title']}")
            print(f"      切入角度: {t.get('angle', '')}")
            print(f"      写作理由: {t.get('rationale', '')}")
            print(f"      参考论文: {refs_str}")

        print("\n  选择方式:")
        print("    - 输入编号 (1, 2, 3...) 选择主题")
        print("    - 输入自定义主题标题")
        print("    - 输入 'back' 要求重新提议主题")

        user_choice = input("\n  👤 你的选择: ").strip()

        if user_choice.lower() == "back":
            print("  ↩️ 重新提出主题...")
            return "back_to_topic", {
                "parsed_papers": parsed_papers,
                "query": payload.get("query", ""),
            }

        chosen_topic = None
        try:
            idx = int(user_choice) - 1
            if 0 <= idx < len(topics):
                chosen_topic = topics[idx]
        except ValueError:
            chosen_topic = {
                "title": user_choice,
                "angle": f"用户自定义主题: {user_choice}",
                "rationale": "用户自主提出的写作方向",
                "references": [p["arxiv_id"] for p in parsed_papers[:3]],
            }

        if not chosen_topic:
            chosen_topic = topics[0]
            print("  ⚠️ 无效输入，默认选择主题 1")

        print(f"\n  ✅ 选定主题: {chosen_topic['title']}")

        return "write", {
            "chosen_topic": chosen_topic,
            "parsed_papers": parsed_papers,
        }


class WritingNode(Node):
    """LLM 基于选定主题撰写 LaTeX 论文"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        chosen_topic = payload.get("chosen_topic", {})
        parsed_papers = payload.get("parsed_papers", [])

        title = chosen_topic.get("title", "Research Paper")
        angle = chosen_topic.get("angle", "")
        rationale = chosen_topic.get("rationale", "")
        ref_ids = chosen_topic.get("references", [])

        print(f"\n✍️ 撰写论文: {title}")
        print("  生成 LaTeX 格式论文...")

        references_text = format_references_for_writing(parsed_papers, ref_ids)
        messages = [
            {"role": "system", "content": WRITING_SYSTEM_PROMPT},
            {"role": "user", "content": WRITING_USER_PROMPT.format(
                topic_title=title,
                topic_angle=angle,
                topic_rationale=rationale,
                references_text=references_text,
            )},
        ]
        response = call_llm(messages=messages)
        latex_content = response.get("content", "")

        # 清理 LaTeX 内容：去除代码块标记
        latex_content = latex_content.strip()
        if latex_content.startswith("```latex"):
            latex_content = latex_content[len("```latex"):]
        if latex_content.startswith("```"):
            latex_content = latex_content[len("```"):]
        if latex_content.endswith("```"):
            latex_content = latex_content[:-len("```")]
        latex_content = latex_content.strip()

        # 确保以 \documentclass 开头
        if not latex_content.startswith("\\documentclass"):
            idx = latex_content.find("\\documentclass")
            if idx > 0:
                latex_content = latex_content[idx:]
            else:
                print("  ⚠️ LLM 输出不含有效 LaTeX 结构，添加默认文档框架")
                latex_content = (
                    "\\documentclass{article}\n"
                    "\\usepackage{amsmath}\n"
                    "\\usepackage{hyperref}\n"
                    "\\title{" + title + "}\n"
                    "\\author{Research Assistant}\n"
                    "\\date{\\today}\n"
                    "\\begin{document}\n"
                    "\\maketitle\n\n"
                    + latex_content + "\n\n"
                    "\\end{document}\n"
                )

        print(f"  ✅ LaTeX 论文生成完成 ({len(latex_content)} 字符)")

        return "render", {
            "latex_content": latex_content,
            "chosen_topic": chosen_topic,
        }


class RenderNode(Node):
    """LaTeX 渲染为 PDF"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        latex_content = payload.get("latex_content", "")
        chosen_topic = payload.get("chosen_topic", {})

        title = chosen_topic.get("title", "paper")
        filename = title.replace(" ", "_").replace("/", "_").replace("\\", "_")
        filename = re.sub(r'[^\w]', '', filename)[:50] or "paper"

        output_dir = str(Path.cwd() / "output")

        print(f"\n🖨️ 渲染 LaTeX 为 PDF (文件名: {filename})...")

        result = render_latex(
            tex_content=latex_content,
            output_dir=output_dir,
            filename=filename,
        )

        if result.get("success"):
            print(f"  ✅ PDF 渲染成功: {result['pdf_path']}")
            return "end", {
                "pdf_path": result["pdf_path"],
                "tex_path": result["tex_path"],
                "chosen_topic": chosen_topic,
                "render_success": True,
            }
        else:
            print(f"  ❌ PDF 渲染失败: {result.get('error', '')[:100]}")
            print(f"  📄 .tex 文件已保存: {result.get('tex_path', '')}")
            return "end", {
                "pdf_path": "",
                "tex_path": result.get("tex_path", ""),
                "chosen_topic": chosen_topic,
                "render_success": False,
                "render_error": result.get("error", ""),
            }


class EndNode(Node):
    """输出最终报告"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        print("\n" + "=" * 60)
        print("📚 研究助手工作流 — 最终报告")
        print("=" * 60)

        chosen_topic = payload.get("chosen_topic", {})
        render_success = payload.get("render_success", False)

        print(f"\n  📝 写作主题: {chosen_topic.get('title', 'N/A')}")
        print(f"  切入角度: {chosen_topic.get('angle', '')}")

        if render_success:
            print(f"\n  ✅ PDF 已生成: {payload.get('pdf_path', '')}")
            print(f"  📄 LaTeX 源码: {payload.get('tex_path', '')}")
        else:
            print(f"\n  ⚠️ PDF 渲染失败: {payload.get('render_error', '')[:80]}")
            print(f"  📄 LaTeX 源码已保存: {payload.get('tex_path', '')}")
            print("  提示: 安装 MiKTeX (Windows) 或 TeX Live (Linux/macOS) 后可手动编译")

        print("\n" + "=" * 60)
        print("工作流结束 🎉")
        print("=" * 60)

        return "done", None


class ErrorNode(Node):
    """错误终止节点"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        error_msg = payload.get("error", "未知错误")
        print(f"\n❌ 工作流出错: {error_msg}")
        print("=" * 60)
        return "done", None


# ──────────────────────────────────────────────
#  入口函数
# ──────────────────────────────────────────────

def run_research_assistant() -> None:
    """运行研究助手工作流"""
    print("=" * 60)
    print("📚 研究助手工作流")
    print("=" * 60)
    print("流程: 搜索论文 → 选择下载 → 解析论文 → 提出主题 → 评审 → 写作 → 渲染PDF")
    print("输入 'quit' 或 'exit' 退出\n")

    shared.clear()
    shared["guard"] = SafetyGuard()
    shared["tool_executor"] = ToolExecutor(guard=shared["guard"])

    # 构建 DAG
    search = SearchNode()
    select = SelectNode()
    download = DownloadNode()
    parse = ParseNode()
    topic = TopicNode()
    review_gate = ReviewGateNode()
    writing = WritingNode()
    render = RenderNode()
    end = EndNode()
    error = ErrorNode()

    # 连接
    search - "select" >> select
    search - "error" >> error
    select - "download" >> download
    download - "parse" >> parse
    download - "error" >> error
    parse - "topic" >> topic
    parse - "error" >> error
    topic - "review" >> review_gate
    topic - "error" >> error
    review_gate - "write" >> writing
    review_gate - "back_to_topic" >> topic
    writing - "render" >> render
    render - "end" >> end
    render - "render_error" >> end
    error - "done" >> end

    user_query = input("👤 输入研究主题关键词: ").strip()

    if user_query.lower() in ("quit", "exit", "q"):
        print("\n再见！")
        return

    if not user_query:
        print("请输入有效的研究主题")
        return

    flow = Flow(search)
    flow.run({"query": user_query})


if __name__ == "__main__":
    run_research_assistant()
