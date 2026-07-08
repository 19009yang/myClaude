"""Grep 搜索工具 - 跨平台适配版本

优先使用 ripgrep (rg)，无 rg 时回退到纯 Python 实现。
支持 Windows / Linux / macOS，统一 UTF-8 编码，统一正斜杠路径输出。
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path, PurePosixPath


# ─────────────────── 常量 ───────────────────

DEFAULT_LIMIT = 100
DEFAULT_MAX_BYTES = 30 * 1024          # 30KB
GREP_MAX_LINE_LENGTH = 1000

# 跨平台跳过的目录名（ripgrep 默认也会跳过这些）
SKIP_DIR_NAMES = {
    ".git", ".svn", ".hg",            # VCS
    "node_modules", ".venv", "venv",  # 依赖
    "__pycache__", ".mypy_cache",     # Python 缓存
    ".idea", ".vscode", ".vs",        # IDE
    "dist", "build", ".tox",          # 构建产物
}

# 跳过的文件扩展名（常见二进制文件）
SKIP_BINARY_EXTENSIONS = {
    ".exe", ".dll", ".so", ".o", ".obj", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tif", ".tiff", ".webp",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mkv", ".wav", ".flac", ".ogg",
    ".pyc", ".pyd", ".pyo", ".class", ".jar", ".war",
    ".db", ".sqlite", ".sqlite3",
}

# Windows 上通过文件属性标记为隐藏的文件也应跳过
IS_WINDOWS = platform.system() == "Windows"


# ─────────────────── 辅助函数 ───────────────────

def _is_hidden(path: Path) -> bool:
    """判断文件/目录是否为隐藏（跨平台）

    - Linux/macOS: 以 '.' 开头
    - Windows: 文件名以 '.' 开头 或 具有隐藏属性
    """
    name = path.name
    if name.startswith("."):
        return True
    if IS_WINDOWS:
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs != -1 and attrs & 0x2:  # FILE_ATTRIBUTE_HIDDEN
                return True
        except Exception:
            pass
    return False


def _is_binary_file(path: Path) -> bool:
    """判断文件是否为二进制文件（基于扩展名 + 内容嗅探）"""
    if path.suffix.lower() in SKIP_BINARY_EXTENSIONS:
        return True
    # 内容嗅探：读取前 8KB，检测 NULL 字节
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return True
    except Exception:
        return True
    return False


def _read_file_text(path: Path) -> str | None:
    """读取文件文本内容，自动检测编码

    优先 UTF-8，失败后尝试 chardet（若已安装），最终回退 latin-1（绝不丢失字节）。
    """
    raw = path.read_bytes()
    if not raw:
        return ""

    # 1) 优先 UTF-8
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 2) 尝试 chardet（可选依赖）
    try:
        import chardet
        detected = chardet.detect(raw)
        encoding = detected.get("encoding") or "utf-8"
        confidence = detected.get("confidence") or 0
        if confidence > 0.7:
            return raw.decode(encoding, errors="replace")
    except ImportError:
        pass

    # 3) 回退 latin-1（每个字节都有映射，不会丢失数据）
    return raw.decode("latin-1")


def _posix_rel_path(file_path: Path, search_path: Path) -> str:
    """计算相对路径，统一输出正斜杠格式（与 rg 一致）

    - file_path 在 search_path 下 → 相对路径
    - file_path == search_path → 仅文件名
    - 其他情况 → 绝对路径（正斜杠）
    """
    try:
        if file_path == search_path:
            return file_path.name
        rel = file_path.relative_to(search_path)
        return str(PurePosixPath(rel))  # 统一转为正斜杠
    except ValueError:
        # 不在 search_path 下（如符号链接），回退为正斜杠绝对路径
        return str(PurePosixPath(file_path.resolve()))


def _truncate_output(output: str, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """按字节截断输出，保证不破坏 UTF-8 字符边界"""
    raw = output.encode("utf-8")
    if len(raw) <= max_bytes:
        return output

    # 从 max_bytes 位置向前找到完整 UTF-8 字符边界
    cut = max_bytes
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1

    result = raw[:cut].decode("utf-8", errors="replace")
    result += f"\n\n[{max_bytes // 1024}KB limit reached]"
    return result


def _find_rg() -> str | None:
    """查找系统中的 ripgrep 可执行文件

    Windows: 查找 rg.exe
    Linux/macOS: 查找 rg
    """
    system = platform.system()
    exe_name = "rg.exe" if system == "Windows" else "rg"

    # 1) PATH 中查找
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for d in path_dirs:
        candidate = Path(d) / exe_name
        if candidate.is_file():
            return str(candidate)

    # 2) Windows 常见安装位置
    if system == "Windows":
        home = Path(os.environ.get("USERPROFILE", ""))
        common_locations = [
            Path("C:/Program Files/ripgrep/rg.exe"),
            Path("C:/Program Files/Git/mingw64/bin/rg.exe"),
            home / "scoop" / "shims" / "rg.exe",
            home / ".cargo" / "bin" / "rg.exe",
        ]
        for loc in common_locations:
            if loc.is_file():
                return str(loc)

    return None


# ─────────────────── 主函数 ───────────────────

def grep(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    ignore_case: bool = False,
    literal: bool = False,
    context: int = 0,
    limit: int | None = None,
    cwd: str | None = None,
) -> str:
    """
    搜索文件内容，跨平台适配。

    优先使用 ripgrep (rg)，无 rg 时回退纯 Python 实现。
    输出路径统一使用正斜杠，编码统一 UTF-8。

    Args:
        pattern: 搜索模式（正则或字面量）
        path: 搜索路径（默认当前目录）
        glob: 文件过滤模式，如 '*.py'
        ignore_case: 忽略大小写
        literal: 将 pattern 视为字面量而非正则
        context: 匹配前后显示的行数
        limit: 最大匹配数
        cwd: 当前工作目录，用于相对路径搜索

    Returns:
        搜索结果文本
    """
    # ── 路径解析 ──
    if cwd:
        search_path = Path(cwd) / (path or ".")
    else:
        search_path = Path(path or ".")

    search_path = search_path.resolve()

    if not search_path.exists():
        raise FileNotFoundError(f"Path not found: {search_path}")

    effective_limit = limit or DEFAULT_LIMIT

    # ── 尝试使用 ripgrep ──
    rg_path = _find_rg()
    if rg_path:
        result = _grep_rg(
            rg_path=rg_path,
            pattern=pattern,
            search_path=search_path,
            glob=glob,
            ignore_case=ignore_case,
            literal=literal,
            context=context,
            limit=effective_limit,
        )
        if result is not None:
            return result

    # ── 回退到 Python 实现 ──
    return _grep_python(
        pattern=pattern,
        search_path=search_path,
        glob=glob,
        ignore_case=ignore_case,
        literal=literal,
        context=context,
        limit=effective_limit,
    )


# ─────────────────── ripgrep 实现 ───────────────────

def _grep_rg(
    rg_path: str,
    pattern: str,
    search_path: Path,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
) -> str | None:
    """使用 ripgrep 搜索。返回 None 表示 rg 调用失败，应回退。"""

    cmd = [rg_path, "--line-number", "--color=never", "--hidden"]

    if ignore_case:
        cmd.append("--ignore-case")
    if literal:
        cmd.append("--fixed-strings")
    if glob:
        cmd.extend(["--glob", glob])
    if context > 0:
        cmd.extend(["-C", str(context)])

    cmd.extend(["-m", str(limit)])
    cmd.append(pattern)
    # 统一使用正斜杠路径传给 rg（rg 在所有平台都接受 /）
    cmd.append(str(PurePosixPath(search_path)))

    # ── 统一编码 ──
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
        )
    except (FileNotFoundError, OSError):
        return None  # rg 不可用，回退
    except subprocess.TimeoutExpired:
        return f"[ripgrep timed out after 30s]"

    if result.returncode not in (0, 1):
        # rg 返回 2+ 表示错误，回退到 Python
        return None

    output = result.stdout.strip()

    if not output:
        return "No matches found"

    # ── 行长度截断 ──
    lines = output.split("\n")
    truncated_lines = []
    lines_truncated = False
    for line in lines:
        if len(line) > GREP_MAX_LINE_LENGTH:
            line = line[:GREP_MAX_LINE_LENGTH] + "..."
            lines_truncated = True
        truncated_lines.append(line)

    output = "\n".join(truncated_lines)

    # ── 输出大小截断 ──
    output = _truncate_output(output)

    # ── 通知信息 ──
    notices = []
    if len(truncated_lines) >= limit:
        notices.append(f"{limit} matches limit reached")
    if lines_truncated:
        notices.append(f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars")

    if notices:
        output += f"\n\n[{'. '.join(notices)}]"

    return output


# ─────────────────── Python 回退实现 ───────────────────

def _grep_python(
    pattern: str,
    search_path: Path,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
) -> str:
    """纯 Python 实现的 grep（无 rg 时的回退方案）"""

    flags = re.IGNORECASE if ignore_case else 0
    if literal:
        pattern = re.escape(pattern)

    try:
        regex = re.compile(pattern, flags)
    except re.PatternError as e:
        raise ValueError(f"Invalid regex pattern: {pattern}") from e

    # ── 收集要搜索的文件 ──
    files = _collect_files(search_path, glob)

    # ── 搜索 ──
    matches: list[str] = []
    match_count = 0
    # 记录已输出的行坐标 (file, line_no)，防止 context 重叠重复
    emitted_lines: dict[tuple[str, int], str] = {}  # key: (rel_path, line_no), value: formatted line

    for file_path in files:
        if match_count >= limit:
            break

        if _is_binary_file(file_path):
            continue

        content = _read_file_text(file_path)
        if content is None:
            continue

        lines = content.split("\n")
        rel_path = _posix_rel_path(file_path, search_path)

        for i, line_text in enumerate(lines):
            line_no = i + 1  # 1-indexed

            if not regex.search(line_text):
                continue

            if match_count >= limit:
                break

            if context > 0:
                # 输出匹配行及其上下文，合并重叠区域
                start = max(0, line_no - context - 1)
                end = min(len(lines), line_no + context)
                for j in range(start, end):
                    j_no = j + 1
                    key = (rel_path, j_no)
                    if key not in emitted_lines:
                        is_match_line = (j_no == line_no)
                        prefix = f"{rel_path}:{j_no}:" if is_match_line else f"{rel_path}-{j_no}-"
                        emitted_lines[key] = f"{prefix} {lines[j]}"
            else:
                key = (rel_path, line_no)
                if key not in emitted_lines:
                    emitted_lines[key] = f"{rel_path}:{line_no}: {line_text}"

            match_count += 1

    # ── 按文件和行号排序，模拟 rg 的输出顺序 ──
    sorted_lines = sorted(emitted_lines.items(), key=lambda kv: kv[0])
    output_lines = [kv[1] for kv in sorted_lines]

    if not output_lines:
        return "No matches found"

    # ── 行长度截断 ──
    truncated = []
    lines_truncated = False
    for line in output_lines:
        if len(line) > GREP_MAX_LINE_LENGTH:
            line = line[:GREP_MAX_LINE_LENGTH] + "..."
            lines_truncated = True
        truncated.append(line)

    output = "\n".join(truncated)

    # ── 输出大小截断 ──
    output = _truncate_output(output)

    # ── 通知信息 ──
    notices = []
    if match_count >= limit:
        notices.append(f"{limit} matches limit reached")
    if lines_truncated:
        notices.append(f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars")

    if notices:
        output += f"\n\n[{'. '.join(notices)}]"

    return output


def _collect_files(search_path: Path, glob: str | None) -> list[Path]:
    """收集要搜索的文件列表，跳过隐藏目录和二进制文件候选"""
    if search_path.is_file():
        return [search_path]

    files: list[Path] = []

    def _should_skip_dir(dir_path: Path) -> bool:
        """判断是否应跳过某个目录"""
        if dir_path.name in SKIP_DIR_NAMES:
            return True
        if _is_hidden(dir_path):
            return True
        return False

    # 使用 os.walk 而非 rglob，以便在遍历时跳过整个目录
    for root, dirs, filenames in os.walk(search_path):
        # 过滤掉应跳过的目录（原地修改 dirs 列表影响 os.walk 的后续遍历）
        dirs[:] = [
            d for d in dirs
            if not _should_skip_dir(Path(root) / d)
        ]

        root_path = Path(root)
        for fname in filenames:
            fpath = root_path / fname

            # 跳过隐藏文件
            if _is_hidden(fpath):
                continue

            # glob 过滤
            if glob:
                # 使用 fnmatch 进行 glob 匹配
                import fnmatch
                if not fnmatch.fnmatch(fname, glob):
                    continue

            files.append(fpath)

    return files


# ─────────────────── CLI 入口 ───────────────────

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Grep search tool (cross-platform)")
    parser.add_argument("pattern", help="Search pattern (regex or literal)")
    parser.add_argument("path", nargs="?", default=".", help="Search path (default: current directory)")
    parser.add_argument("-g", "--glob", help="File glob filter, e.g. '*.py'")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="Ignore case")
    parser.add_argument("-l", "--literal", action="store_true", help="Treat pattern as literal string")
    parser.add_argument("-C", "--context", type=int, default=0, help="Show N lines of context")
    parser.add_argument("-m", "--limit", type=int, default=None, help="Max matches")
    parser.add_argument("--cwd", default=None, help="Working directory")

    args = parser.parse_args()

    try:
        result = grep(
            pattern=args.pattern,
            path=args.path,
            glob=args.glob,
            ignore_case=args.ignore_case,
            literal=args.literal,
            context=args.context,
            limit=args.limit,
            cwd=args.cwd,
        )
        print(result)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
