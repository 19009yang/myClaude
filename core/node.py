from __future__ import annotations
from typing import Any, Dict, Optional, Tuple, Iterable
import time
shared = {}


class RetryableError(Exception):
    """可重试的异常 —— 被 Node._exec 捕获后自动重试。
    用于区分"网络超时、下载失败"等瞬态错误与"用户取消"等永久性错误。
    """
    pass


class Node:
    """
    同步节点：exec(payload) 返回 (action, next_payload)，支持重试和回退。
    重试策略：指数退避，基础等待时间 = self.wait，每轮翻倍。
    仅对 RetryableError 触发重试；其他异常直接抛出不重试。
    重试耗尽后：
      - 若设置了 fallback_action，返回 (fallback_action, payload) 沿 DAG 回退到前序节点；
      - 若未设置 fallback_action，返回 ("error", {"error": str(e)}) 走 ErrorNode。
    """
    def __init__(self, max_retries=1, wait: float = 0, fallback_action: str | None = None):
        self.successors: Dict[str, Node] = {}
        self._action: str = "default"
        self.max_retries, self.wait = max_retries, wait
        self.fallback_action = fallback_action

    def exec(self, payload) -> Tuple[str, Any]:  # 需要子类实现
        return NotImplementedError  # 若未实现抛出异常

    # 重试机制（指数退避）+ 回退机制
    def _exec(self, payload: Any) -> Tuple[str, Any]:
        for cur_try in range(self.max_retries):
            try:
                return self.exec(payload)
            except RetryableError as e:
                if cur_try == self.max_retries - 1:
                    # 已达最大重试次数 → 走回退或错误路径
                    print(f"  ❌ 已达最大重试次数 ({self.max_retries})，重试耗尽: {e}")
                    if self.fallback_action:
                        print(f"  ↩️ 回退: {self.fallback_action}")
                        return self.fallback_action, payload
                    else:
                        return "error", {"error": str(e)}
                # 指数退避：wait * 2^cur_try
                delay = self.wait * (2 ** cur_try)
                print(f"  ⚠️ 第 {cur_try + 1} 次重试（共 {self.max_retries} 次），{delay:.1f}s 后重试... 原因: {e}")
                time.sleep(delay)
            except Exception as e:
                # 非 RetryableError —— 不重试，直接抛出
                raise e

    def __rshift__(self, other:Node)->Node:
        self.successors[self._action]=other
        self._action="default"
        return other

    def __sub__(self, action: str) -> Node:
        if not isinstance(action, str):
            raise TypeError("Action must be a string")
        self._action = action or "default"
        return self

class Flow:
    """
    同步编排器：按 action 依次执行节点。
    """
    def __init__(self, start: Optional[Node] = None) -> None:
        self.start = start

    def run(self, payload: Any = None) -> Tuple[Optional[str], Any]:
        curr, last_action = self.start, None
        if not self.start:
            raise TypeError("first node can not be None")

        while curr:
            last_action, payload = curr._exec(payload)
            curr = curr.successors.get(last_action)
        return last_action, payload
