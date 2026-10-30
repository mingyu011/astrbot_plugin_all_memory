"""local_embed.py —— 可选的本地 ONNX embedding。

依赖 onnxruntime / tokenizers / numpy；没装就由调用方回退到 LocalEmbedder。
模型文件由 local_models 下载，目录里至少要有 tokenizer.json 和 onnx/model.onnx。
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os


def _normalize(values) -> list[float]:
    vector = [float(item) for item in values]
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0:
        return [0.0] * len(vector)
    return [item / norm for item in vector]


class ProviderEmbedder:
    """把 AstrBot 的 embedding provider 包装成检索层用的同步接口。

    检索和向量回填都在 asyncio.to_thread 里跑；这里用 run_coroutine_threadsafe
    把 provider 的 async 调用送回主事件循环，避免在 worker 线程里新建 loop。
    """

    def __init__(self, provider, provider_id: str, loop):
        self.provider = provider
        self.provider_id = provider_id
        self.loop = loop
        self.name = f"api-{provider_id}"
        self.DIM = 0
        self._timeout = 30.0

    def _call_async(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=self._timeout)

    async def _vector_async(self, text: str) -> list[float]:
        provider = self.provider
        get_one = getattr(provider, "get_embedding", None)
        get_many = getattr(provider, "get_embeddings", None)
        if callable(get_one):
            value = get_one(str(text or ""))
            value = await value if inspect.isawaitable(value) else value
        elif callable(get_many):
            value = get_many([str(text or "")])
            value = await value if inspect.isawaitable(value) else value
            if value and isinstance(value[0], (list, tuple)):
                value = value[0]
        else:
            raise RuntimeError("provider 不支持 embedding")
        vector = _normalize(value)
        if not vector:
            raise RuntimeError("provider 返回空向量")
        self.DIM = len(vector)
        return vector

    def vector(self, text: str) -> list[float]:
        if not self.loop or not self.loop.is_running():
            raise RuntimeError("主事件循环不可用")
        return self._call_async(self._vector_async(text))

    def vector_many(self, texts: list[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]


class LocalONNXEmbedder:
    """BGE 系列 ONNX 文本向量；CPU 推理，CLS pooling + L2 归一化。"""

    def __init__(self, model_dir: str, model_id: str, dim: int = 512,
                 pooling: str = "cls", max_length: int = 512):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np

        onnx_path = os.path.join(model_dir, "onnx", "model.onnx")
        tokenizer_path = os.path.join(model_dir, "tokenizer.json")
        if not os.path.isfile(onnx_path):
            raise FileNotFoundError(f"缺少 {onnx_path}")
        if not os.path.isfile(tokenizer_path):
            raise FileNotFoundError(f"缺少 {tokenizer_path}")

        self.model_id = model_id
        self.name = f"onnx-{model_id}"
        self.DIM = int(dim or 512)
        self.pooling = pooling or "cls"
        self.max_length = max(32, int(max_length or 512))
        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_truncation(max_length=self.max_length)
        self.session = ort.InferenceSession(
            onnx_path,
            providers=["CPUExecutionProvider"],
            sess_options=self._session_options(ort),
        )
        self.input_names = {item.name for item in self.session.get_inputs()}

    @staticmethod
    def _session_options(ort):
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return options

    def vector(self, text: str) -> list[float]:
        encoding = self.tokenizer.encode(str(text or ""))
        ids = list(encoding.ids or [0])
        mask = list(encoding.attention_mask or [1] * len(ids))
        if not ids:
            ids = [0]
            mask = [0]
        feed = {
            "input_ids": self.np.asarray([ids], dtype=self.np.int64),
            "attention_mask": self.np.asarray([mask], dtype=self.np.int64),
        }
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = self.np.zeros(
                (1, len(ids)), dtype=self.np.int64)
        outputs = self.session.run(None, feed)
        if not outputs:
            return [0.0] * self.DIM
        array = self.np.asarray(outputs[0])
        if array.ndim == 3:
            vector = array[0, 0, :]
        elif array.ndim == 2:
            vector = array[0, :]
        else:
            vector = array.reshape(-1)
        if vector.shape[0] != self.DIM:
            # 有些导出模型输出的是 hidden size，不是配置维度；以实际为准。
            self.DIM = int(vector.shape[0])
        vector = vector.astype("float32", copy=False)
        norm = float(self.np.linalg.norm(vector))
        if norm <= 0:
            return [0.0] * self.DIM
        return (vector / norm).tolist()

    def vector_many(self, texts: list[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]
