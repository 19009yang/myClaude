# Agent 安全防护方案

## 目标
防止 Agent 通过 bash/write/edit 等工具执行严重危害操作（删库、覆盖系统文件、提权等）。

## 设计原则
1. **集中式拦截**：在 `ToolExecutor.execute()` 中增加校验层，而非分散到每个工具函数
2. **可配置**：用户可通过 YAML/JSON 策略文件自定义黑名单，而非硬编码
3. **分级处置**：低风险操作直接放行，高风险操作拦截并返回错误信息，极高风险操作需用户确认
4. **不依赖 System Prompt**：LLM 可能忽略指令，防护必须在代码层强制执行

## 实现方案（3 层防护）

### 第 1 层：SafetyGuard 校验器（核心）

在 `tools/executor.py` 的 `ToolExecutor.execute()` 方法中，**调用工具之前**插入校验逻辑：

```python
# tools/executor.py - 修改后的 execute 方法

def execute(self, tool_call: ToolCall) -> ToolResult:
    tool = self.tool_map.get(tool_call.name)
    if not tool:
        return ToolResult(tool_call_id=tool_call.id, content=f"Tool '{tool_call.name}' not found", is_error=True)

    # ✅ 新增：安全校验
    guard_result = self.guard.check(tool_call.name, tool_call.arguments)
    if guard_result.blocked:
        return ToolResult(
            tool_call_id=tool_call.id,
            content=f"⚠️ 操作被安全策略拦截: {guard_result.reason}",
            is_error=True,
        )

    # ✅ 新增：高风险操作需用户确认
    if guard_result.needs_confirm:
        print(f"\n⚠️ 高风险操作: {guard_result.reason}")
        confirm = input("是否允许执行？(y/N): ").strip().lower()
        if confirm != "y":
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"用户拒绝执行: {guard_result.reason}",
                is_error=True,
            )

    # 原有逻辑不变
    try:
        raw_result = tool.execute(**tool_call.arguments)
    except Exception as exc:
        ...
```

新建 `tools/guard.py`，实现 `SafetyGuard` 类：

```python
# tools/guard.py

@dataclass
class GuardResult:
    blocked: bool       # 是否直接拦截
    needs_confirm: bool # 是否需要用户确认
    reason: str         # 拦截/确认的原因说明

class SafetyGuard:
    """集中式安全校验器"""

    def __init__(self, policy_path: str | None = None):
        self.policy = self._load_policy(policy_path)

    def check(self, tool_name: str, arguments: dict) -> GuardResult:
        # 检查工具是否需要校验
        checker = self._checkers.get(tool_name)
        if not checker:
            return GuardResult(blocked=False, needs_confirm=False, reason="")
        return checker(arguments)

    def _check_bash(self, args: dict) -> GuardResult:
        command = args.get("command", "")
        # 黑名单匹配
        # 白名单匹配（可选）
        # 路径保护

    def _check_write(self, args: dict) -> GuardResult:
        path = args.get("path", "")
        # 系统路径保护
        # 文件保护

    def _check_edit(self, args: dict) -> GuardResult:
        # 同 write 的路径保护逻辑
```

### 第 2 层：策略文件（可配置）

新建 `tools/safety_policy.yaml`，定义黑名单和风险等级：

```yaml
# bash 命令安全策略
bash:
  # 直接拦截（不可执行，无需确认）
  blocked_patterns:
    - pattern: "rm\s+-rf\s+/"
      reason: "禁止递归强制删除根目录"
    - pattern: "rm\s+-rf\s+~"
      reason: "禁止递归强制删除用户主目录"
    - pattern: "del\s+/s\s+/q\s+C:\\"
      reason: "禁止Windows下强制批量删除系统盘"
    - pattern: "shutdown|reboot|halt"
      reason: "禁止关机/重启操作"
    - pattern: ":(\\)\\{.*\\|:&\\}"
      reason: "禁止fork bomb"
    - pattern: "chmod\s+777|chmod\s+000"
      reason: "禁止极端权限修改"
    - pattern: "sudo\s+rm"
      reason: "禁止sudo删除操作"
    - pattern: "mkfs|dd\s+of=/dev"
      reason: "禁止磁盘格式化/裸设备写入"

  # 需用户确认（可执行，但需用户明确同意）
  confirm_patterns:
    - pattern: "rm\s+-"
      reason: "删除操作需确认"
    - pattern: "sudo"
      reason: "提权操作需确认"
    - pattern: "curl.*-X\s+POST|curl.*-d"
      reason: "向外发送数据需确认"
    - pattern: "git\s+push|git\s+reset\s+--hard"
      reason: "Git推送/硬重置需确认"
    - pattern: "pip\s+install|npm\s+install"
      reason: "安装包需确认"

# 文件路径安全策略（write/edit 共用）
file_paths:
  blocked_paths:
    - "C:\\Windows\\"
    - "C:\\Program Files\\"
    - "C:\\ProgramData\\"
    - "/etc/"
    - "/usr/bin/"
    - "/usr/lib/"
    - "/boot/"
    - "/sys/"
    - "/proc/"
    reason: "禁止写入系统目录"

  confirm_paths:
    - ".env"
    - "credentials"
    - "secret"
    - "password"
    - "token"
    reason: "敏感文件修改需确认"
```

### 第 3 层：System Prompt 软约束（补充）

在 `SYSTEM_PROMPT` 中加入安全提示，作为**软约束**（不保证 LLM 遵守，但可降低误操作概率）：

```
你不得主动执行以下操作：删除大量文件、修改系统配置、提权操作、向外发送用户数据。
如果用户要求执行可能有害的操作，先警告风险并确认意图后再操作。
```

> 注意：这一层**仅是补充**，核心防护在第 1 层代码强制拦截。

## 修改文件清单

| 文件 | 修改内容 |
|------|---------|
| `tools/guard.py` | **新建** — SafetyGuard 类，GuardResult 数据类 |
| `tools/safety_policy.yaml` | **新建** — 可配置的安全策略文件 |
| `tools/executor.py` | **修改** — ToolExecutor.execute() 中插入 guard.check() |
| `tools/__init__.py` | **修改** — 导出 SafetyGuard |
| `Agent/chatBot_with_memory/main.py` | **修改** — SYSTEM_PROMPT 添加软约束；初始化时注入 guard |

## 关键设计决策

1. **拦截位置选在 ToolExecutor 而非 bash.py 内部**：
   - 集中管理，所有工具（bash/write/edit）都经过同一校验点
   - 未来新增工具自动受保护，无需逐个修改
   - 保持底层工具函数纯净，方便单独测试

2. **分级处置（blocked vs confirm）而非一刀切**：
   - `rm -rf /` → 直接拦截，不让用户确认（绝对不可执行）
   - `rm file.txt` → 需用户确认（有合理使用场景）
   - `ls` → 直接放行

3. **策略外置为 YAML 而非硬编码**：
   - 用户可根据项目需求调整（如 CI 环境禁止 git push，开发环境允许）
   - 新增规则无需改代码

4. **不使用沙箱/chroot**：
   - 项目是 Windows 桌面工具，沙箱方案复杂且跨平台兼容差
   - 命令拦截 + 路径保护已覆盖主要风险场景
