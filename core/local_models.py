"""
local_models.py —— 本地向量模型市场

设计原则：

- 默认不下载、不启用；不选模型时完全走原来的本地哈希向量。
- 模型是运行时资源，不塞进插件包；用户选目录、手动下载、手动启用。
- 下载失败、依赖缺失、目录丢失、模型损坏，都只退回哈希向量，不影响聊天。
- 模型按 id 分目录，`models.json` 记录启用状态和路径；`vectors.db` 按模型名隔离。
"""

from __future__ import annotations

import json
import importlib
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from .ids import safe_component

# HuggingFace 国内镜像优先；失败再试官方。
_HF_MIRRORS = (
    "https://hf-mirror.com",
    "https://huggingface.co",
)

MODELS = [
    {
        "id": "bge-small-zh-v1.5",
        "name": "BGE Small 中文 v1.5",
        "repo": "Xenova/bge-small-zh-v1.5",
        "dim": 512,
        "size": "约 100 MB",
        "desc": "默认推荐。中文语义够用，CPU 也快，适合先跑起来。",
        "files": [
            "config.json", "tokenizer.json", "tokenizer_config.json",
            "special_tokens_map.json", "vocab.txt", "onnx/model.onnx",
        ],
        "pooling": "cls",
    },
    {
        "id": "bge-base-zh-v1.5",
        "name": "BGE Base 中文 v1.5",
        "repo": "Xenova/bge-base-zh-v1.5",
        "dim": 768,
        "size": "约 400 MB",
        "desc": "比 Small 更准，索引和查询都更慢一些。",
        "files": [
            "config.json", "tokenizer.json", "tokenizer_config.json",
            "special_tokens_map.json", "vocab.txt", "onnx/model.onnx",
        ],
        "pooling": "cls",
    },
    {
        "id": "bge-large-zh-v1.5",
        "name": "BGE Large 中文 v1.5",
        "repo": "Xenova/bge-large-zh-v1.5",
        "dim": 1024,
        "size": "约 1.3 GB",
        "desc": "中文效果更强，但内存和首次索引成本明显更高。",
        "files": [
            "config.json", "tokenizer.json", "tokenizer_config.json",
            "special_tokens_map.json", "vocab.txt", "onnx/model.onnx",
        ],
        "pooling": "cls",
    },
    {
        "id": "bge-m3",
        "name": "BGE M3 多语言",
        "repo": "Xenova/bge-m3",
        "dim": 1024,
        "size": "约 2.3 GB",
        "desc": "多语言、长文本最强，但模型最大、CPU 最慢，不建议默认开。",
        "files": [
            "config.json", "tokenizer.json", "tokenizer_config.json",
            "special_tokens_map.json", "sentencepiece.bpe.model",
            "onnx/model.onnx", "onnx/model.onnx_data",
        ],
        "pooling": "cls",
    },
]

MODEL_BY_ID = {item["id"]: item for item in MODELS}
DEFAULT_MODEL = MODELS[0]["id"]


def dependencies_ok() -> tuple[bool, str]:
    importlib.invalidate_caches()
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        return True, ""
    except Exception as exc:  # pragma: no cover - 只影响可选模型
        return False, f"缺少本地模型依赖：{exc}。请安装 onnxruntime、tokenizers、numpy。"


def model_files(model_id: str) -> list[str]:
    return list((MODEL_BY_ID.get(model_id) or {}).get("files") or [])


def default_model_parent(root: str) -> str:
    # 放在 all_memory/ 外面，避免“一键保存全量数据”把几 GB 模型也打进 zip。
    return os.path.join(os.path.dirname(os.path.abspath(root)), "all_memory_models")


def model_dir(root: str, model_id: str, parent: str = "") -> str:
    parent = os.path.abspath(parent.strip().strip('"')) if str(parent or "").strip() \
        else default_model_parent(root)
    return os.path.join(parent, safe_component(model_id, fallback="model"))


def is_installed(path: str, model_id: str) -> bool:
    files = model_files(model_id)
    if not files:
        return False
    for rel in files:
        if not os.path.isfile(os.path.join(path, rel.replace("/", os.sep))):
            return False
    return True


def _download_one(url: str, target: str, progress=None,
                  timeout: int = 60, force: bool = False) -> None:
    """下载单个文件到 .part，完成后再原子改名；已有完整文件则跳过。"""
    if not force and os.path.isfile(target) and os.path.getsize(target) > 0:
        if progress:
            progress(os.path.getsize(target))
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"
    request = urllib.request.Request(
        url, headers={"User-Agent": "astrbot-all-memory/1.1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response, \
            open(part, "wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            if progress:
                progress(len(chunk))
    if os.path.getsize(part) <= 0:
        os.remove(part)
        raise OSError(f"下载为空：{url}")
    os.replace(part, target)


def _download_file(model_id: str, rel: str, target_dir: str,
                   progress=None, force: bool = False) -> None:
    model = MODEL_BY_ID[model_id]
    target = os.path.join(target_dir, rel.replace("/", os.sep))
    errors = []
    for base in _HF_MIRRORS:
        url = f"{base}/{model['repo']}/resolve/main/{rel}"
        try:
            _download_one(url, target, progress=progress, force=force)
            return
        except Exception as exc:  # 换下一个源
            errors.append(f"{base}: {exc}")
    raise OSError("下载失败，已试过镜像和官方源：\n" + "\n".join(errors))


class LocalModelManager:
    """模型市场状态：安装、下载进度、启用模型。不负责向量查询。"""

    def __init__(self, root: str):
        self.root = root
        self.config_path = os.path.join(root, "models.json")
        self._lock = threading.Lock()
        self._progress: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._dep_progress: dict = {"status": "idle", "message": ""}
        self._dep_thread: threading.Thread | None = None
        self._config = self._read_config()

    # ------------------------------------------------------------ 配置

    def _read_config(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_config(self, data: dict) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.config_path)

    def _save(self) -> None:
        with self._lock:
            self._write_config(self._config)

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled"))

    @property
    def active_id(self) -> str:
        return str(self._config.get("model") or "")

    @property
    def source(self) -> str:
        value = str(self._config.get("source") or "").strip()
        if value in ("local", "api"):
            return value
        return "local" if self.active_id else "hash"

    @property
    def provider_id(self) -> str:
        return str(self._config.get("provider_id") or "")

    def path_of(self, model_id: str) -> str:
        return str((self._config.get("paths") or {}).get(model_id) or "")

    def set_path(self, model_id: str, path: str) -> None:
        with self._lock:
            paths = dict(self._config.get("paths") or {})
            paths[model_id] = path
            self._config["paths"] = paths
            self._write_config(self._config)

    def set_enabled(self, enabled: bool, model_id: str = "") -> None:
        with self._lock:
            self._config["enabled"] = bool(enabled)
            if model_id:
                self._config["source"] = "local"
                self._config["model"] = model_id
            self._write_config(self._config)

    def set_api(self, provider_id: str, enabled: bool = True) -> None:
        with self._lock:
            self._config["enabled"] = bool(enabled)
            self._config["source"] = "api"
            self._config["provider_id"] = provider_id
            self._write_config(self._config)

    # ------------------------------------------------------------ 状态

    def progress_of(self, model_id: str) -> dict:
        return dict(self._progress.get(model_id) or {})

    def dependency_progress(self) -> dict:
        return dict(self._dep_progress)

    def start_dependency_install(self) -> dict:
        """用当前 AstrBot 的 Python 安装 requirements；只在用户点按钮时执行。"""
        ok, error = dependencies_ok()
        if ok:
            return {"ok": True, "message": "依赖已安装"}
        with self._lock:
            if self._dep_thread and self._dep_thread.is_alive():
                return {"ok": False, "error": "依赖正在安装"}
            self._dep_progress = {
                "status": "installing", "message": "正在安装 onnxruntime / tokenizers",
                "error": "",
            }
            thread = threading.Thread(
                target=self._install_dependencies_worker,
                name="allmem-model-deps",
                daemon=True,
            )
            self._dep_thread = thread
            thread.start()
        return {"ok": True, "message": "已开始安装依赖"}

    def _install_dependencies_worker(self) -> None:
        requirements = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "requirements.txt")
        try:
            command = [
                sys.executable, "-m", "pip", "install",
                "--disable-pip-version-check", "-r", requirements,
            ]
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=900)
            output = (process.stdout or "") + "\n" + (process.stderr or "")
            if process.returncode != 0:
                tail = "\n".join(output.strip().splitlines()[-12:])
                raise RuntimeError(tail or f"pip 返回 {process.returncode}")
            ok, error = dependencies_ok()
            if not ok:
                raise RuntimeError(error)
            with self._lock:
                self._dep_progress = {
                    "status": "installed", "message": "依赖安装完成", "error": "",
                }
        except Exception as exc:
            with self._lock:
                self._dep_progress = {
                    "status": "error", "message": "依赖安装失败",
                    "error": str(exc),
                }

    def status(self, model_id: str = "") -> dict:
        deps_ok, deps_error = dependencies_ok()
        items = []
        for model in MODELS:
            mid = model["id"]
            path = self.path_of(mid) or model_dir(self.root, mid)
            item = dict(model)
            item["installed"] = is_installed(path, mid)
            item["path"] = path
            item["active"] = bool(self.enabled and self.active_id == mid
                                  and item["installed"])
            item["progress"] = self.progress_of(mid)
            items.append(item)
        active = self.active_id if self.enabled else ""
        result = {
            "ok": True, "enabled": self.enabled,
            "source": self.source if self.enabled else "hash",
            "provider_id": self.provider_id,
            "active": active,
            "dependencies_ok": deps_ok, "dependencies_error": deps_error,
            "dependency_progress": self.dependency_progress(),
            "default_parent": default_model_parent(self.root),
            "models": items,
        }
        if model_id:
            result["model"] = next(
                (item for item in items if item["id"] == model_id), None)
        return result

    # ------------------------------------------------------------ 下载

    def start_download(self, model_id: str, parent: str = "") -> dict:
        if model_id not in MODEL_BY_ID:
            return {"ok": False, "error": "未知模型"}
        if not dependencies_ok()[0]:
            return {"ok": False, "error": dependencies_ok()[1]}
        with self._lock:
            existing = self._threads.get(model_id)
            if existing and existing.is_alive():
                return {"ok": False, "error": "这个模型正在下载"}
            target = model_dir(self.root, model_id, parent)
            previous = self._progress.get(model_id) or {}
            force = is_installed(target, model_id) or previous.get("status") == "error"
            self._progress[model_id] = {
                "status": "downloading", "downloaded": 0,
                "total": 0, "percent": 0, "message": "准备下载",
                "path": target, "error": "",
            }
            thread = threading.Thread(
                target=self._download_worker,
                args=(model_id, target, force),
                name=f"allmem-model-{model_id}",
                daemon=True,
            )
            self._threads[model_id] = thread
            thread.start()
        return {"ok": True, "model": model_id, "path": target,
                "message": "已开始下载"}

    def _download_worker(self, model_id: str, target: str,
                         force: bool = False) -> None:
        model = MODEL_BY_ID[model_id]
        files = model_files(model_id)
        total_files = len(files)
        done_files = 0
        downloaded = 0
        total = 0
        try:
            os.makedirs(target, exist_ok=True)
            # 先拿每个文件的大小，进度按字节算；拿不到就按文件数。
            for rel in files:
                try:
                    req = urllib.request.Request(
                        f"{_HF_MIRRORS[0]}/{model['repo']}/resolve/main/{rel}",
                        method="HEAD",
                        headers={"User-Agent": "astrbot-all-memory/1.1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=20) as response:
                        total += int(response.headers.get("Content-Length") or 0)
                except Exception:
                    pass
            with self._lock:
                self._progress[model_id].update(
                    {"total": total, "message": "下载中"})

            def on_bytes(count: int) -> None:
                nonlocal downloaded
                downloaded += count
                with self._lock:
                    item = self._progress[model_id]
                    item["downloaded"] = downloaded
                    if total > 0:
                        item["percent"] = min(99, int(downloaded * 100 / total))
                    else:
                        item["percent"] = min(
                            99, int(done_files * 100 / max(1, total_files)))

            for rel in files:
                _download_file(model_id, rel, target,
                               progress=on_bytes, force=force)
                done_files += 1
                with self._lock:
                    item = self._progress[model_id]
                    if total <= 0:
                        item["percent"] = min(
                            99, int(done_files * 100 / max(1, total_files)))
                    item["message"] = f"已下载 {done_files}/{total_files}"

            if not is_installed(target, model_id):
                raise OSError("下载完成但文件不完整")
            self.set_path(model_id, target)
            with self._lock:
                self._progress[model_id].update({
                    "status": "installed", "percent": 100,
                    "message": "下载完成", "error": "",
                })
        except Exception as exc:
            with self._lock:
                self._progress[model_id].update({
                    "status": "error", "message": "下载失败",
                    "error": str(exc),
                })

    # ------------------------------------------------------------ 启用

    def enable(self, model_id: str):
        """加载并返回本地 embedder；失败抛异常，调用方保持哈希。"""
        if model_id not in MODEL_BY_ID:
            raise ValueError("未知模型")
        path = self.path_of(model_id)
        if not path or not is_installed(path, model_id):
            raise FileNotFoundError("模型还没下载完整")
        from .local_embed import LocalONNXEmbedder
        embedder = LocalONNXEmbedder(
            path, model_id,
            dim=int(MODEL_BY_ID[model_id]["dim"]),
            pooling=str(MODEL_BY_ID[model_id].get("pooling") or "cls"),
        )
        self.set_enabled(True, model_id)
        return embedder

    def disable(self) -> None:
        self.set_enabled(False)


def default_model_id() -> str:
    return DEFAULT_MODEL
