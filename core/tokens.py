"""
tokens.py —— token 估算与在线校准

估算口径刻意与 AstrBot 平台对齐（中文 ×0.6、其他 ×0.3），
保证两边对同一份上下文算出同一量级，不然预算判断会打架。

provider 返回真实 prompt_tokens 时，用 EMA 修正估算比例。
"""

from __future__ import annotations

from typing import Any

# AstrBot 平台口径
CJK_RATIO = 0.6
OTHER_RATIO = 0.3
IMAGE_TOKENS = 765
AUDIO_TOKENS = 500

# 校准比例钳制范围：防止个别异常样本把估算带飞
CALIBRATION_MIN = 0.5
CALIBRATION_MAX = 3.0
EMA_ALPHA = 0.3


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF      # 中日韩统一表意
        or 0x3040 <= code <= 0x30FF   # 日文假名
        or 0xAC00 <= code <= 0xD7AF   # 韩文
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
    )


def estimate_tokens(text: Any) -> int:
    """字符启发式估算。不带校准，用于原始口径。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
    other = len(text) - cjk
    return int(cjk * CJK_RATIO + other * OTHER_RATIO) + 1


def estimate_messages(messages: list[dict]) -> int:
    """估算一组消息的 token 总量（含每条的开销）。"""
    total = 0
    for msg in messages:
        total += estimate_tokens(msg.get("content", "")) + 4
    return total


class Calibrator:
    """
    EMA 校准器。state 里存一个比例，实际估算 = 原始估算 × ratio。

    规则：
    - 只在没有工具调用循环的干净回合上学习（工具结果会把用量口径带偏）
    - 比例钳制在 [0.5, 3.0]
    """

    def __init__(self, ratio: float = 1.0, enabled: bool = True):
        self.ratio = _clamp(ratio)
        self.enabled = enabled

    def apply(self, raw: int) -> int:
        if not self.enabled:
            return raw
        return max(1, int(raw * self.ratio))

    def learn(self, raw_estimate: int, actual_prompt_tokens: int) -> float:
        """用真实用量修正比例，返回新比例。"""
        if not self.enabled or raw_estimate <= 0 or actual_prompt_tokens <= 0:
            return self.ratio
        observed = actual_prompt_tokens / raw_estimate
        observed = _clamp(observed)
        self.ratio = _clamp(self.ratio * (1 - EMA_ALPHA) + observed * EMA_ALPHA)
        return self.ratio

    def to_dict(self) -> dict:
        return {"ratio": round(self.ratio, 4), "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Calibrator":
        if not isinstance(data, dict):
            return cls()
        return cls(ratio=float(data.get("ratio", 1.0)),
                   enabled=bool(data.get("enabled", True)))


def _clamp(value: float) -> float:
    return max(CALIBRATION_MIN, min(CALIBRATION_MAX, float(value)))
