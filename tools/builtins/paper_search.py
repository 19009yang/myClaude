"""PaperSearch 工具 - 在 arxiv 检索论文并下载 PDF"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx


ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_PDF_URL = "https://arxiv.org/pdf"

# Atom namespace
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

# sort_by 映射到 arxiv API 参数值
_SORT_BY_MAP = {
    "relevance": "relevance",
    "lastUpdatedDate": "lastUpdatedDate",
    "submittedDate": "submittedDate",
}

_SORT_ORDER_MAP = {
    "ascending": "ascending",
    "descending": "descending",
}


def search_papers(
    query: str,
    max_results: int = 10,
    sort_by: str = "relevance",
    sort_order: str = "descending",
    start: int = 0,
) -> list[dict[str, Any]]:
    """在 arxiv 检索论文，返回搜索结果列表。

    Args:
        query: 搜索关键词，支持 arxiv 查询语法：
            - `ti:` 标题搜索，如 ti:"deep learning"
            - `au:` 作者搜索，如 au:"Hinton"
            - `cat:` 分类搜索，如 cat:"cs.AI"
            - `abs:` 摘要搜索
            - 支持 AND / OR / ANDNOT 逻辑组合
        max_results: 最大返回条数，默认 10。
        sort_by: 排序依据，可选 "relevance" / "lastUpdatedDate" / "submittedDate"。
        sort_order: 排序方向，可选 "ascending" / "descending"。
        start: 结果偏移量，用于分页。

    Returns:
        论文列表，每个元素包含:
            title, authors, summary, arxiv_id, pdf_url, published, updated, categories
    """
    sb = _SORT_BY_MAP.get(sort_by, "relevance")
    so = _SORT_ORDER_MAP.get(sort_order, "descending")

    params = {
        "search_query": query,
        "start": start,
        "max_results": max_results,
        "sortBy": sb,
        "sortOrder": so,
    }

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(ARXIV_API_URL, params=params)

    if response.status_code != 200:
        raise RuntimeError(f"arxiv API 返回错误: HTTP {response.status_code}")

    return _parse_arxiv_xml(response.text)


def download_papers(
    arxiv_ids: str | list[str],
    save_dir: str | None = None,
) -> dict[str, dict[str, str]]:
    """下载指定论文的 PDF，支持单个或批量下载。

    Args:
        arxiv_ids: 单个 arxiv ID（如 "2301.00123" 或 "2301.00123v1"）
                   或多个 ID 列表（如 ["2301.00123", "2301.00456"]）。
        save_dir: PDF 保存目录，默认为当前目录下的 papers/。

    Returns:
        dict，key 为 arxiv_id，value 为 {"status": "success"/"error", "path": "..."/"error_msg"}。
    """
    # 统一转为列表
    if isinstance(arxiv_ids, str):
        ids = [arxiv_ids]
    else:
        ids = list(arxiv_ids)

    # 确定保存目录
    dest = Path(save_dir) if save_dir else Path.cwd() / "papers"
    dest.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict[str, str]] = {}

    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for aid in ids:
            # 去掉版本号后缀作为文件名（如 2301.00123v1 → 2301.00123）
            clean_id = re.sub(r"v\d+$", "", aid)
            pdf_url = f"{ARXIV_PDF_URL}/{aid}.pdf"
            file_path = dest / f"{clean_id}.pdf"

            try:
                with client.stream("GET", pdf_url) as stream:
                    if stream.status_code != 200:
                        results[aid] = {
                            "status": "error",
                            "path": f"HTTP {stream.status_code}",
                        }
                        continue

                    with open(file_path, "wb") as f:
                        for chunk in stream.iter_bytes(chunk_size=8192):
                            f.write(chunk)

                results[aid] = {
                    "status": "success",
                    "path": str(file_path),
                }

            except Exception as exc:
                results[aid] = {
                    "status": "error",
                    "path": str(exc),
                }

    return results


# ── XML 解析辅助 ──


def _parse_arxiv_xml(xml_text: str) -> list[dict[str, Any]]:
    """解析 arxiv API 返回的 Atom XML，提取论文列表。"""
    root = ET.fromstring(xml_text)
    entries = root.findall(f"{ATOM_NS}entry")

    papers = []
    for entry in entries:
        paper = _parse_entry(entry)
        if paper:
            papers.append(paper)

    return papers


def _parse_entry(entry: ET.Element) -> dict[str, Any] | None:
    """解析单个 <entry> 元素，提取论文信息。"""
    # arxiv_id：从 <id> 中提取
    raw_id = entry.findtext(f"{ATOM_NS}id", "")
    if not raw_id:
        return None
    # 去掉 "http://arxiv.org/abs/" 前缀，得到纯 ID（如 2301.00123v1）
    arxiv_id = raw_id.replace("http://arxiv.org/abs/", "").strip()

    title = entry.findtext(f"{ATOM_NS}title", "").strip()
    summary = entry.findtext(f"{ATOM_NS}summary", "").strip()
    published = entry.findtext(f"{ATOM_NS}published", "").strip()
    updated = entry.findtext(f"{ATOM_NS}updated", "").strip()

    # authors
    authors = []
    for author_elem in entry.findall(f"{ATOM_NS}author"):
        name = author_elem.findtext(f"{ATOM_NS}name", "").strip()
        if name:
            authors.append(name)

    # categories
    categories = []
    for cat_elem in entry.findall(f"{ATOM_NS}category"):
        term = cat_elem.get("term", "")
        if term:
            categories.append(term)

    # pdf_url
    pdf_url = f"{ARXIV_PDF_URL}/{arxiv_id}.pdf"

    # primary_category
    primary_cat_elem = entry.find(f"{ARXIV_NS}primary_category")
    primary_category = primary_cat_elem.get("term", "") if primary_cat_elem is not None else ""

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "summary": summary,
        "published": published,
        "updated": updated,
        "categories": categories,
        "primary_category": primary_category,
        "pdf_url": pdf_url,
    }


if __name__ == "__main__":
    print("=" * 50)
    print("PaperSearch Demo")
    print("=" * 50)

    # 1) 搜索
    print("\n1. 搜索论文: ti:'large language model'")
    results = search_papers("ti:'large language model'", max_results=3)
    for p in results:
        print(f"\n  [{p['arxiv_id']}] {p['title']}")
        print(f"    Authors: {', '.join(p['authors'][:3])}...")
        print(f"    Category: {p['primary_category']}")
        print(f"    Summary: {p['summary'][:80]}...")

    # 2) 下载（仅下载第一篇）
    if results:
        first_id = results[0]["arxiv_id"]
        print(f"\n2. 下载论文: {first_id}")
        dl_result = download_papers(first_id, save_dir="papers")
        for aid, info in dl_result.items():
            print(f"    {aid}: {info['status']} → {info['path']}")
