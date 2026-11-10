"""受限文件工具和真正的子进程隔离；不提供任意宿主 shell。"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import uuid

from tools.builtins.tool_def import Tool

MAX_FILE_BYTES = 160_000
MAX_TOTAL_BYTES = 1_000_000
MAX_FILES = 40

BOOTSTRAP = """import json,resource,runpy,sys
args=json.loads(sys.argv[1])
cpu=int(sys.argv[2])
for kind,limit in ((resource.RLIMIT_CPU,cpu),(resource.RLIMIT_FSIZE,2000000),(resource.RLIMIT_NOFILE,128)):
    resource.setrlimit(kind,(limit,limit))
sys.path.insert(0,'.')
if args[0]=='-c':
    sys.argv=['-c',*args[2:]]
    exec(compile(args[1],'<string>','exec'),{'__name__':'__main__'})
elif args[0]=='-m':
    sys.argv=args[1:]
    runpy.run_module(args[1],run_name='__main__',alter_sys=True)
else:
    sys.argv=args
    runpy.run_path(args[0],run_name='__main__')
"""


def files_in(directory):
    """Never follow agent-created symlinks into private data."""
    files, total = {}, 0
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("产物中存在符号链接")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
            raise ValueError("产物超过文件数量/体积上限")
        files[path.relative_to(directory).as_posix()] = path.read_text(encoding="utf-8")
    return files


class Sandbox:
    def __init__(self, settings):
        self.settings = settings
        self.deadline = None

    def command(self, work, args, name):
        if self.settings.sandbox == "docker":
            if not shutil.which("docker"):
                raise RuntimeError("缺少 Docker；先安装并启动，或在 macOS 使用 seatbelt")
            return ["docker", "run", "--rm", "--pull=never", "--name", name, "--network=none", "--read-only",
                    "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m",
                    "--cpus=1", "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,size=16m",
                    "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "TZ=Asia/Shanghai",
                    "--mount", f"type=bind,source={work},target=/work", "-w", "/work",
                    self.settings.docker_image, "python", "-B", "-s", *args]
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
            raise RuntimeError("seatbelt 仅支持 macOS；Linux 请配置 sandbox=docker")
        python = Path(sys.executable).resolve()
        # Runtime roots only: no repo, home, environment files or benchmark private directory.
        roots = {"/System", "/usr/lib", "/usr/share", str(python.parent.parent),
                 sysconfig.get_path("stdlib"), str(work)}
        read_rules = " ".join(f"(subpath {json.dumps(p)})" for p in sorted(roots))
        profile = ("(version 1)(deny default)(allow process-exec process-fork signal sysctl-read mach-lookup)"
                   "(allow file-read-metadata)"
                   f"(allow file-map-executable {read_rules})"
                   f"(allow file-read* {read_rules} (literal \"/\") (literal \"/dev/urandom\") (literal \"/dev/null\"))"
                   f"(allow file-write* (subpath {json.dumps(str(work))}) (literal \"/dev/null\"))")
        return ["/usr/bin/sandbox-exec", "-p", profile, str(python), "-B", "-s", *args]

    def run(self, directory, args, *, timeout=30):
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("执行代码前已达到轮次超时")
        # Copy only public artifacts; generated programs never access the live host workspace.
        with tempfile.TemporaryDirectory(prefix="lamemory-code-") as tmp:
            work = Path(tmp).resolve()
            initial = files_in(directory)
            for name, content in initial.items():
                target = work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                # Preserve CRLF and original input bytes.
                target.write_bytes((directory / name).read_bytes())
            container = "lamemory-" + uuid.uuid4().hex
            cmd = self.command(work, ["-c", BOOTSTRAP, json.dumps(args), str(max(1, int(timeout) + 1))], container)
            env = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TZ": "Asia/Shanghai",
                   "HOME": str(work), "TMPDIR": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
            start = time.monotonic()
            # A bounded output file avoids unbounded PIPE buffering from generated code.
            with tempfile.TemporaryFile(dir=work) as output:
                process = subprocess.Popen(cmd, cwd=work, env=env, stdout=output, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                timed_out = False
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    if self.settings.sandbox == "docker" and timed_out:
                        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10)
                output.seek(0)
                text = output.read(24_000).decode("utf-8", errors="replace")
            files_in(work)  # Validate links and size before returning any generated files.
            for name in files_in(work):
                dest = directory / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((work / name).read_bytes())
            return {"args": args, "returncode": process.returncode, "timed_out": timed_out,
                    "output": text, "elapsed_seconds": time.monotonic() - start,
                    "isolation": self.settings.sandbox}

    def preflight(self):
        with tempfile.TemporaryDirectory(prefix="lamemory-preflight-") as tmp:
            work = Path(tmp)
            # The probe must establish forbidden file access and network access, not just launch Python.
            forbidden = work.parent / ("lamemory-private-" + uuid.uuid4().hex)
            forbidden.write_text("private", encoding="utf-8")
            try:
                code = ("import pathlib,socket\n"
                        f"p=pathlib.Path({str(forbidden)!r})\n"
                        "try:\n p.read_text(); raise RuntimeError('private file readable')\n"
                        "except (PermissionError,FileNotFoundError): pass\n"
                        "try:\n s=socket.socket(); s.settimeout(1); s.connect(('1.1.1.1',53)); "
                        "raise RuntimeError('network enabled')\n"
                        "except OSError as e:\n"
                        " if e.errno not in (1,13,100,101,113): raise\n"
                        "print('isolated')\n")
                result = self.run(work, ["-c", code], timeout=15)
                if result["returncode"] or "isolated" not in result["output"]:
                    raise RuntimeError(f"代码隔离自检失败（exit={result['returncode']}）：" + result["output"][:1000])
                return result
            finally:
                forbidden.unlink()


def common_tools(directory, sandbox):
    root = directory.resolve()

    def local(path):
        p = Path(path)
        p = (p if p.is_absolute() else root / p).resolve()
        if not p.is_relative_to(root):
            raise PermissionError("只能访问当前任务交付目录")
        return p

    def read(path):
        p = local(path)
        if p.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("文件过大")
        return p.read_text(encoding="utf-8")

    def write(path, content):
        p = local(path)
        if len(content.encode()) > MAX_FILE_BYTES:
            raise ValueError("文件过大")
        files_in(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"saved": p.relative_to(root).as_posix()}

    def python(args):
        if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
            raise ValueError("args 必须是非空字符串数组，不包含 python 命令本身")
        # Absolute host paths in Python arguments are intentionally unsupported.
        if any(a.startswith("/") for a in args):
            raise ValueError("python 参数中的文件路径使用相对路径")
        return sandbox.run(root, args)

    definitions = [
        ("read", "读取本题交付目录中的文件。", {"path": {"type": "string"}}, ["path"], read),
        ("write", "写入本题交付目录中的 UTF-8 文件，可覆盖已有文件。",
         {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"], write),
        ("ls", "列出本题交付目录中的文件。", {}, [], lambda: sorted(files_in(root))),
        ("python", "在离线隔离目录实际执行 Python，使用相对路径。例 args=['-m','unittest','discover',"
         "'-s','.','-p','test_buggy_module.py']；或 ['diff_configs.py','current.json','proposed.json']。",
         {"args": {"type": "array", "items": {"type": "string"}}}, ["args"], python),
    ]
    return [Tool(name, desc, {"type": "object", "properties": props, "required": required,
                              "additionalProperties": False}, fn)
            for name, desc, props, required, fn in definitions]
