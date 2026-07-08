"""搜索工具"""

from __future__ import annotations

from typing import Any, Dict, List

from ddgs import DDGS


def search(query: str, max_results: int = 5, backend: str = "auto", proxy: str | None = None) -> List[Dict[str, Any]]:
    """使用 DuckDuckGo 搜索网页。

    Args:
        query: 搜索关键词。
        max_results: 最大结果数。
        backend: 搜索引擎后端，可选 "auto"/"duckduckgo"/"bing"/"brave"/"google" 等。
            默认 "auto"。在国内环境如需使用 google 等引擎，需设置 proxy。
        proxy: HTTP 代理地址，例如 "socks5://127.0.0.1:1080"。
            也可通过环境变量 DDGS_PROXY 设置，无需每次传入。
    """
    with DDGS(proxy=proxy) as ddgs:
        return list(ddgs.text(query, max_results=max_results, backend=backend))


if __name__ == "__main__":
    print(search("python programming"))