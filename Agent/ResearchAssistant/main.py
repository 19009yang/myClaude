"""研究助手工作流

论文搜索 → 挑选下载 → 解析论文 → 提出写作主题 → 用户评审 → LaTeX写作 → PDF渲染 → 结束

使用 Node/Flow DAG 编排，支持重试（指数退避）和回退（重试耗尽后跳回前序节点）。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Tuple
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core import Node, Flow, shared, call_llm
from core.node import RetryableError
import importlib.util
from tools import get_tools, ToolExecutor, SafetyGuard
from tools.skill_loader import get_default_registry
from tools.builtins.paper_search import search_papers
from tools.builtins.paper_search import download_papers
from tools.builtins.latex_render import render_latex

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


class InputNode(Node):
    """起始节点：提示用户输入搜索关键词。
    若是从 SearchNode 回退而来（payload 中有 fallback hint），提示换关键词。
    """

    def exec(self, payload: Any) -> Tuple[str, Any]:
        # 判断是否为回退场景
        fallback_hint = payload.get("fallback_hint", "")

        if fallback_hint:
            print(f"\n  ⚠️ {fallback_hint}")
            print("  请换一个关键词或检查网络后重试。")

        user_query = input("\n👤 输入研究主题关键词（输入 quit/exit 退出）: ").strip()

        if user_query.lower() in ("quit", "exit", "q"):
            print("\n再见！")
            return "done", None

        if not user_query:
            print("  ⚠️ 请输入有效的研究主题")
            # 回到自身重新输入
            return "re_input", {"fallback_hint": "关键词不能为空"}

        return "search", {"query": user_query}


class SearchNode(Node):
    """搜索论文：调用 search_papers 工具。搜索失败（网络超时等）抛出 RetryableError 可自动重试。
    重试耗尽后通过 fallback_action 回退到 InputNode（重新输入关键词）。
    """

    def exec(self, payload: Any) -> Tuple[str, Any]:
        query = payload.get("query", "")
        if not query:
            return "error", {"error": "请输入研究主题关键词"}

        print(f"\n🔍 正在搜索论文: {query}")
        try:
            papers = search_papers(query, max_results=10)
        except Exception as exc:
            # 网络超时、HTTP 错误等瞬态失败 → 可重试
            print(f"  ❌ 搜索失败: {exc}")
            raise RetryableError(f"搜索关键词 '{query}' 失败: {exc}")

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
    """下载选定论文的 PDF。对每篇失败论文逐篇重试（最多 per_paper_retries 次），全部失败时抛出 RetryableError。
    重试耗尽后通过 fallback_action 回退到 SelectNode（重新筛选论文）。
    """

    PER_PAPER_RETRIES = 5  # 逐篇下载重试次数

    def exec(self, payload: Any) -> Tuple[str, Any]:
        selected_ids = payload.get("selected_ids", [])
        query = payload.get("query", "")
        papers = payload.get("papers", [])

        if not selected_ids:
            return "error", {"error": "无论文 ID 需下载"}

        print(f"\n📥 下载 {len(selected_ids)} 篇论文...")
        save_dir = str(Path.cwd() / "papers")

        # 逐篇下载，失败则重试
        downloaded = {}
        failed_ids = []

        for aid in selected_ids:
            success = False
            for attempt in range(self.PER_PAPER_RETRIES):
                # 每次重试重新调用 download_papers（仅下载当前这篇）
                result = download_papers(aid, save_dir=save_dir)
                info = result[aid]
                if info["status"] == "success":
                    downloaded[aid] = info["path"]
                    print(f"  ✅ {aid} → {info['path']}")
                    success = True
                    break
                else:
                    if attempt < self.PER_PAPER_RETRIES - 1:
                        delay = 2 * (2 ** attempt)  # 指数退避: 2, 4, 8, 16s
                        print(f"  ❌ {aid} 下载失败（第 {attempt + 1}/{self.PER_PAPER_RETRIES} 次），{delay}s 后重试... 原因: {info['path'][:80]}")
                        time.sleep(delay)
                    else:
                        print(f"  ❌ {aid} 下载失败（已重试 {self.PER_PAPER_RETRIES} 次）: {info['path'][:80]}")

            if not success:
                failed_ids.append(aid)

        if not downloaded:
            # 所有论文下载失败 → 触发节点级重试，耗尽后回退到 SelectNode
            raise RetryableError(f"所有 {len(selected_ids)} 篇论文下载失败")

        if failed_ids:
            print(f"  ⚠️ {len(failed_ids)} 篇下载失败，继续处理成功部分")

        return "parse", {
            "query": query,
            "downloaded": downloaded,
            "papers": papers,
        }


class ParseNode(Node):
    """解析下载的 PDF 为文本。所有论文解析失败时抛出 RetryableError 可自动重试。
    无回退目标，重试耗尽后走 ErrorNode。
    """

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
            # 所有论文解析失败 → 触发节点级重试
            raise RetryableError("所有论文解析失败")

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
    """LaTeX 渲染为 PDF。渲染失败（编译超时等瞬态问题）时抛出 RetryableError 可自动重试。
    无回退目标，重试耗尽后走 ErrorNode。
    """

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
            error_msg = result.get("error", "")
            # 区分瞬态错误（编译超时）和永久性错误（缺少编译器）
            # 缺少编译器是不可重试的，编译超时/失败可重试
            if "未检测到 LaTeX 编译器" in error_msg:
                # 不可重试 —— 直接返回失败结果
                print(f"  ❌ PDF 渲染失败: {error_msg[:100]}")
                print(f"  📄 .tex 文件已保存: {result.get('tex_path', '')}")
                return "end", {
                    "pdf_path": "",
                    "tex_path": result.get("tex_path", ""),
                    "chosen_topic": chosen_topic,
                    "render_success": False,
                    "render_error": error_msg,
                }
            else:
                # 编译超时、编译失败等瞬态问题 → 可重试
                print(f"  ❌ PDF 渲染失败: {error_msg[:100]}")
                raise RetryableError(f"PDF 渲染失败: {error_msg}")


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
    print("流程: 输入关键词 → 搜索论文 → 选择下载 → 解析论文 → 提出主题 → 评审 → 写作 → 渲染PDF")
    print("支持: 重试(指数退避) + 回退(搜索失败→重新输入, 下载失败→重新筛选)")
    print("输入 'quit' 或 'exit' 退出\n")

    shared.clear()
    shared["guard"] = SafetyGuard()
    shared["tool_executor"] = ToolExecutor(guard=shared["guard"])

    # 构建 DAG
    # - 易失败节点: max_retries=5, wait=2（指数退避）
    # - SearchNode 回退到 InputNode（重新输入关键词）
    # - DownloadNode 回退到 SelectNode（重新筛选论文）
    # - ParseNode/RenderNode 无回退，重试耗尽走 ErrorNode
    input_node = InputNode()
    search = SearchNode(max_retries=5, wait=2, fallback_action="fallback_search")
    select = SelectNode()
    download = DownloadNode(max_retries=5, wait=2, fallback_action="fallback_download")
    parse = ParseNode(max_retries=5, wait=2)
    topic = TopicNode()
    review_gate = ReviewGateNode()
    writing = WritingNode()
    render = RenderNode(max_retries=5, wait=2)
    end = EndNode()
    error = ErrorNode()

    # ── DAG 连接 ──
    # 主流程
    input_node - "search" >> search
    search - "select" >> select
    select - "download" >> download
    download - "parse" >> parse
    parse - "topic" >> topic
    topic - "review" >> review_gate
    review_gate - "write" >> writing
    writing - "render" >> render
    render - "end" >> end

    # 回退路径
    input_node - "re_input" >> input_node          # 空关键词 → 重新输入
    search - "fallback_search" >> input_node       # 搜索重试耗尽 → 重新输入关键词
    download - "fallback_download" >> select        # 下载重试耗尽 → 重新筛选论文

    # 错误路径
    search - "error" >> error
    select - "error" >> error
    download - "error" >> error
    parse - "error" >> error
    topic - "error" >> error
    render - "render_error" >> end

    # 用户主动回退
    review_gate - "back_to_topic" >> topic

    # 终止路径
    input_node - "done" >> end
    error - "done" >> end

    flow = Flow(input_node)
    flow.run({})


if __name__ == "__main__":
    run_research_assistant()
