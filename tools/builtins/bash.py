"""Bash 命令执行工具 - 跨平台适配"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


DEFAULT_MAX_BYTES = 30 * 1024
DEFAULT_MAX_LINES = 2000


def _detect_shell() -> tuple[str, bool]:
    """
    检测可用的 shell 类型。

    Returns:
        (shell_path, is_real_bash)
        - Windows: 优先 Git Bash, 其次 WSL bash, 最后回退 cmd.exe
        - Linux/macOS: /bin/bash 或 /bin/sh
    """
    system = platform.system()

    if system != "Windows":
        # Linux / macOS — bash 通常可用
        for candidate in ("/bin/bash", "/bin/sh"):
            if Path(candidate).exists():
                return candidate, True
        return "/bin/sh", False

    # ── Windows ──
    # 1) Git Bash (最常见)
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if git_bash.exists():
        return str(git_bash), True

    # 2) WSL
    try:
        result = subprocess.run(
            ["wsl", "--list"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return "wsl bash", True
    except Exception:
        pass

    # 3) 回退 cmd.exe
    return "cmd.exe", False


_SHELL_PATH, IS_REAL_BASH = _detect_shell()

def bash(
    command: str,
    timeout: int | None = None,
    cwd: str | None = None,
) -> dict:
    """
    执行 shell 命令，跨平台适配。

    Args:
        command: 要执行的命令
        timeout: 超时时间（秒）
        cwd: 工作目录

    Returns:
        {stdout, stderr, exit_code}
    """
    work_dir = Path(cwd) if cwd else Path.cwd()
    if not work_dir.exists():
        raise FileNotFoundError(f"Working directory does not exist: {work_dir}")

    # ── 构建 subprocess 参数 ──
    if IS_REAL_BASH:
        # 真正的 bash: 用 -c 传命令
        shell_cmd = [_SHELL_PATH, "-c", command]
        use_shell = False
    else:
        # cmd.exe 回退: 必须用 shell=True
        shell_cmd = command
        use_shell = True

    # ── 统一编码 ──
    env = os.environ.copy()
    # 强制子进程输出 UTF-8（Windows 默认 GBK）
    env["PYTHONIOENCODING"] = "utf-8"
    if IS_REAL_BASH:
        env["LANG"] = "en_US.UTF-8"

    try:
        result = subprocess.run(
            shell_cmd,
            shell=use_shell,
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",          # 遇到无法解码的字节用  替代，而非崩溃
            timeout=timeout,
            env=env,
        )

        stdout = _truncate(result.stdout, DEFAULT_MAX_LINES, DEFAULT_MAX_BYTES)
        stderr = _truncate(result.stderr, DEFAULT_MAX_LINES // 4, DEFAULT_MAX_BYTES // 4)

        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
        }

    except subprocess.TimeoutExpired as e:
        # 保留已产生的部分输出
        partial_stdout = e.stdout or ""
        partial_stderr = e.stderr or ""
        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode("utf-8", errors="replace")
        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode("utf-8", errors="replace")

        return {
            "stdout": _truncate(partial_stdout, DEFAULT_MAX_LINES, DEFAULT_MAX_BYTES),
            "stderr": f"[Timeout after {timeout}s]\n{_truncate(partial_stderr, 100, 1024)}",
            "exit_code": -1,
        }

    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
        }

def _truncate(text: str, max_lines: int, max_bytes: int) -> str:
    """按行数和字节数截断输出，保留尾部内容"""
    if not text:
        return ""

    # 1) 按行截断
    lines = text.split("\n")
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        text = f"[Truncated to last {max_lines} lines]\n" + "\n".join(lines)

    # 2) 按字节截断（保证不破坏 UTF-8 字符边界）
    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        # 从 max_bytes-1 位置开始（避免越界）
        # 向前跳过 UTF-8 continuation bytes (0x80-0xBF)，找到字符起始字节
        cut = min(max_bytes, len(raw) - 1)
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        # cut 现在指向一个字符的起始字节，raw[cut:] 是完整字符序列
        text = f"[Truncated to last {max_bytes // 1024}KB]\n" + raw[cut:].decode("utf-8", errors="replace")

    return text

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = bash(" ".join(sys.argv[1:]))
        print(result["stdout"])
        if result["stderr"]:
            print(result["stderr"], file=sys.stderr)
        sys.exit(result["exit_code"])
    else:
        print(f"Shell: {_SHELL_PATH} (bash={IS_REAL_BASH})", file=sys.stderr)
        print("Usage: python bash.py <command>", file=sys.stderr)