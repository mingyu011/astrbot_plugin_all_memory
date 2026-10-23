"""
settings.py —— 运行时可调的设置

WebUI 里改的东西写进 settings.json，优先级高于插件配置，
这样面板上的滑块能立刻生效，不用去改配置文件再重载。
"""

from __future__ import annotations

import os
import json
import threading

_LOCK = threading.Lock()

# 比例型：0~1 之间的小数
RANGES = {
    "trigger_ratio": (0.50, 0.80),
    "total_ratio": (0.30, 0.50),
}

# 计数型：整数
COUNTS = {
    "recall_limit": (1, 200),
    "map_days": (1, 3650),
}

# 开关型
BOOLS = {"dry_run"}

DEFAULTS = {
    "trigger_ratio": 0.80,
    "total_ratio": 0.40,
    "recall_limit": 50,
    "map_days": 365,
    "dry_run": False,
}


def is_runtime(key: str) -> bool:
    """这个键是否允许面板覆盖。"""
    return key in RANGES or key in COUNTS or key in BOOLS


class Settings:
    def __init__(self, root: str):
        self.path = os.path.join(root, "settings.json")
        self.root = root

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------ 写入

    def save(self, values: dict) -> dict:
        merged = dict(self.load())
        cleaned = {}

        for key, (low, high) in RANGES.items():
            if key not in values:
                continue
            try:
                number = float(values[key])
            except (TypeError, ValueError):
                continue
            cleaned[key] = round(max(low, min(high, number)), 3)

        for key, (low, high) in COUNTS.items():
            if key not in values:
                continue
            try:
                number = int(round(float(values[key])))
            except (TypeError, ValueError):
                continue
            cleaned[key] = max(low, min(high, number))

        for key in BOOLS:
            if key not in values:
                continue
            raw = values[key]
            if isinstance(raw, str):
                cleaned[key] = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                cleaned[key] = bool(raw)

        merged.update(cleaned)

        with _LOCK:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        return merged

    # ------------------------------------------------------------ 读取

    def get(self, key: str):
        """取生效值；没设置过返回 None，交给调用方回退到插件配置。"""
        value = self.load().get(key)
        if value is None:
            return None
        if key in RANGES:
            low, high = RANGES[key]
            try:
                return round(max(low, min(high, float(value))), 3)
            except (TypeError, ValueError):
                return None
        if key in COUNTS:
            low, high = COUNTS[key]
            try:
                return max(low, min(high, int(round(float(value)))))
            except (TypeError, ValueError):
                return None
        if key in BOOLS:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        return value

    def effective(self, fallback: dict) -> dict:
        """
        面板展示用：把生效值合出来。

        fallback 来自 AstrBot 的插件配置（设置页），那就是唯一事实来源，
        所以它给了值就一律以它为准；只有它没给的项，才回落到这里存过的旧值。
        否则面板里改了值、设置页里又改回来，两边就会各说各话。
        """
        merged = dict(DEFAULTS)
        for key, value in (fallback or {}).items():
            if value is not None:
                merged[key] = value

        for key in list(RANGES) + list(COUNTS) + list(BOOLS):
            if fallback and fallback.get(key) is not None:
                continue
            value = self.get(key)
            if value is not None:
                merged[key] = value
        return merged


def limits() -> dict:
    """给前端用的合法范围。"""
    return {
        "trigger_ratio": list(RANGES["trigger_ratio"]),
        "total_ratio": list(RANGES["total_ratio"]),
        "recall_limit": list(COUNTS["recall_limit"]),
        "map_days": list(COUNTS["map_days"]),
        "bool_keys": list(BOOLS),
    }
