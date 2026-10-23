"""all_memory 核心模块。

刻意不在包初始化时导入子模块：插件按需 from .core import vault 即可，
避免某个子模块出错时整个包无法加载。
"""

__all__ = ["tokens", "vault", "index", "sync", "budget", "inject", "extract", "importer"]
