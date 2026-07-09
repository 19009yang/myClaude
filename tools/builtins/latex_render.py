"""LaTeX 渲染工具 - 将 LaTeX 源码编译为 PDF"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any


def _detect_latex_compiler() -> tuple[str, bool]:
    """检测系统中可用的 LaTeX 编译器。

    Returns:
        (compiler_path, found) — 编译器路径和是否找到。
        Windows 优先检测 MiKTeX 的 pdflatex，其次 TeX Live。
        Linux/macOS 检测系统 pdflatex。
    """
    system = platform.system()

    # 通用检测：直接查找 pdflatex
    try:
        result = subprocess.run(
            ["pdflatex", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return "pdflatex", True
    except FileNotFoundError:
        pass

    # Windows: 检测 MiKTeX 常见安装路径
    if system == "Windows":
        miktex_paths = [
            r"C:\Program Files\MiKTeX\miktex\bin\x64\pdflatex.exe",
            r"C:\Program Files\MiKTeX\miktex\bin\pdflatex.exe",
            r"C:\Users\Root\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe",
        ]
        for p in miktex_paths:
            if Path(p).exists():
                return p, True

    # 未找到任何 LaTeX 编译器
    return "", False


_COMPILER_PATH, HAS_LATEX = _detect_latex_compiler()


def render_latex(
    tex_content: str,
    output_dir: str | None = None,
    filename: str = "paper",
) -> dict[str, Any]:
    """将 LaTeX 源码编译为 PDF。

    Args:
        tex_content: 完整的 LaTeX 源码字符串（须包含 \\documentclass 到 \\end{document}）。
        output_dir: 输出目录，默认为当前目录下的 output/。
        filename: 输出文件名（不含扩展名），默认 "paper"。

    Returns:
        dict 包含:
            success: bool — 编译是否成功
            pdf_path: str — PDF 文件路径（成功时）
            tex_path: str — .tex 文件路径（始终保存）
            log: str — 编译日志（成功时）
            error: str — 错误信息（失败时）
    """
    # 检查 LaTeX 编译器
    if not HAS_LATEX:
        # 即使没有编译器，仍保存 .tex 文件
        dest = Path(output_dir) if output_dir else Path.cwd() / "output"
        dest.mkdir(parents=True, exist_ok=True)
        tex_path = dest / f"{filename}.tex"
        tex_path.write_text(tex_content, encoding="utf-8")

        return {
            "success": False,
            "pdf_path": "",
            "tex_path": str(tex_path),
            "log": "",
            "error": "未检测到 LaTeX 编译器（pdflatex）。请安装 MiKTeX (Windows) 或 TeX Live (Linux/macOS)。"
                     f" .tex 文件已保存至 {tex_path}，可手动编译。",
        }

    # 确定输出目录
    dest = Path(output_dir) if output_dir else Path.cwd() / "output"
    dest.mkdir(parents=True, exist_ok=True)
    tex_path = dest / f"{filename}.tex"

    # 写入 .tex 文件
    tex_path.write_text(tex_content, encoding="utf-8")

    # 编译（运行两次以确保引用和交叉引用正确）
    env = os.environ.copy()
    env["TEXINPUTS"] = f"{dest}{os.pathsep}"

    log_parts = []
    for run_num in range(2):
        try:
            result = subprocess.run(
                [_COMPILER_PATH, "-interaction=nonstopmode",
                 "-output-directory", str(dest), str(tex_path)],
                cwd=dest,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                env=env,
            )
            log_parts.append(f"--- Pass {run_num + 1} ---\n{result.stdout[-500:] if len(result.stdout) > 500 else result.stdout}")
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "pdf_path": "",
                "tex_path": str(tex_path),
                "log": "\n".join(log_parts),
                "error": f"LaTeX 编译超时（第 {run_num + 1} 次编译）。.tex 文件已保存至 {tex_path}。",
            }
        except Exception as exc:
            return {
                "success": False,
                "pdf_path": "",
                "tex_path": str(tex_path),
                "log": "\n".join(log_parts),
                "error": f"LaTeX 编译异常: {exc}。.tex 文件已保存至 {tex_path}。",
            }

    # 检查 PDF 是否生成
    pdf_path = dest / f"{filename}.pdf"
    if pdf_path.exists():
        # 清理辅助文件
        for ext in (".aux", ".log", ".out", ".toc", ".bbl", ".blg", ".synctex.gz"):
            aux_file = dest / f"{filename}{ext}"
            if aux_file.exists():
                try:
                    aux_file.unlink()
                except OSError:
                    pass

        return {
            "success": True,
            "pdf_path": str(pdf_path),
            "tex_path": str(tex_path),
            "log": "\n".join(log_parts),
            "error": "",
        }

    # PDF 未生成 → 编译失败
    # 尝试读取编译日志中的错误
    log_file = dest / f"{filename}.log"
    error_detail = ""
    if log_file.exists():
        try:
            log_text = log_file.read_text(encoding="utf-8", errors="replace")
            # 提取错误行
            error_lines = [line for line in log_text.splitlines() if "^!" in line or line.startswith("!")]
            error_detail = "\n".join(error_lines[:10])
        except Exception:
            error_detail = "(无法读取日志文件)"

    return {
        "success": False,
        "pdf_path": "",
        "tex_path": str(tex_path),
        "log": "\n".join(log_parts),
        "error": f"LaTeX 编译失败，PDF 未生成。错误摘要:\n{error_detail}\n.tex 文件已保存至 {tex_path}。",
    }


if __name__ == "__main__":
    print("=" * 50)
    print("LaTeX Render Demo")
    print("=" * 50)
    print(f"LaTeX 编译器: {_COMPILER_PATH} (found={HAS_LATEX})")

    if HAS_LATEX:
        sample_tex = r"""
\documentclass{article}
\usepackage{amsmath}
\usepackage{hyperref}
\title{Test Paper}
\author{Test Author}
\date{\today}
\begin{document}
\maketitle
\begin{abstract}
This is a test paper generated by the research assistant workflow.
\end{abstract}
\section{Introduction}
Hello world. $E = mc^2$.
\section{Conclusion}
It works.
\end{document}
"""
        result = render_latex(sample_tex, output_dir="output", filename="test_paper")
        print(f"\nSuccess: {result['success']}")
        print(f"PDF: {result.get('pdf_path', 'N/A')}")
        print(f"TeX: {result['tex_path']}")
        if result['error']:
            print(f"Error: {result['error']}")
    else:
        print("\n未检测到 LaTeX 编译器，无法演示渲染。")
        print("请安装: MiKTeX (Windows) 或 TeX Live (Linux/macOS)")
