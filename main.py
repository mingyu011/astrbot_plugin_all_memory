"""
astrbot_plugin_all_memory —— 三层记忆体

第一层 原始层 vault/      只追加，永不修改          → 存得全
第二层 提炼层 facts/      带 source_fp，可重算       → 记得住
第三层 检索层 index.db    派生物，可重建            → 查得到

设计取舍：不为省钱优化。token 往满里塞，聪明优先。
"""

from __future__ import annotations

import os
import io
import time
import json
import inspect
import sqlite3
import base64
import zipfile
import asyncio
from datetime import datetime, timezone, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .core import budget, extract, importer, index as index_mod, inject, refine, restore as restore_mod, sync, vault as vault_mod
from .core import manifest as manifest_mod
from .core import retrieve_pipeline as retrieve_mod
from .core.entities import EntityStore
from .core.ids import SessionIds
from .core.synonyms import SynonymStore
from .core import backup as backup_mod
from .core import media as media_mod, pending as pending_mod, settings as settings_mod
from .core import summary as summary_mod
from .core import query as query_mod
from .core import search as search_mod
from .core import vectors as vectors_mod
from .core import local_models
from .core.tokens import Calibrator, estimate_tokens, usage_input_tokens
from .web_api import WebApi

TZ = timezone(timedelta(hours=8))

# 全局接管状态单独存一份（不是某个会话的），开了就所有会话都生效
GLOBAL_TAKEOVER = "__global__"

DEFAULTS = {
    "data_dir": "",
    "enabled": True,
    "dry_run": False,
    "window_tokens": 0,
    "trigger_ratio": 0.80,
    "total_ratio": 0.40,
    "map_ratio": 0.03,
    "recall_ratio": 0.05,
    "summary_ratio": 0.05,
    "summary_max_chars": 0,
    "summary_keep_recent": 20,
    "facts_before_trigger": False,
    "map_days": 365,
    "recall_limit": 50,
    "recall_context_span": 1,
    "recall_context_hits": 3,
    "recall_snippet_chars": 800,
    "recall_expand": False,
    "auto_recall": True,
    "auto_extract_interval": 0,
    "extract_on_trim": True,
    "extract_chunk_chars": 6000,
    "extract_concurrency": 6,
    "extract_skip_tool": True,
    "extract_model": "",
    "calibrate_tokens": True,
    "astrbot_db": "",
    "beyond_dir": "",
    "backup_astrbot_data": True,
    "backup_extra_dirs": [],
    "takeover_after_restore": True,
    "restore_personas_to_astrbot": True,
    "auto_export_at": "",
    "export_keep": 0,
}


class AllMemory(Star):
    """插件由 metadata.yaml 注册，这里只继承 Star。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        self.settings = None
        self.root = self._resolve_root()
        os.makedirs(self.root, exist_ok=True)

        self.vault = vault_mod.Vault(self.root)
        self.index = index_mod.Index(self.root)
        self.ids = SessionIds(self.root)
        # 检索：字面（index）+ 向量（vectors）两路，由 search.combine 融合。
        # 默认永远是本地哈希；本地 ONNX 模型是可选的，失败自动退回哈希。
        self.local_models = local_models.LocalModelManager(self.root)
        self.embedder = vectors_mod.LocalEmbedder()
        self.vectors = vectors_mod.VectorStore(self.root, self.embedder.name)
        self._local_model_task = None
        # 向量不是"配置项"，是自动补的：启动后补历史，每轮结束补增量
        self._vector_started: set[str] = set()
        # 附件同理：库里一张图都没有时，自动从宿主库回填一次
        self._media_backfill_started: set[str] = set()
        self.states = budget.StateStore(self.root)
        self.settings = settings_mod.Settings(self.root)

        self._turn_counter: dict[str, int] = {}
        self._extract_cooldown: dict[str, float] = {}
        self._last_raw_estimate: dict[str, int] = {}
        self._refining: dict[str, bool] = {}
        # 宿主 DB 的 token_usage 读得很频繁，缓存几秒，避免每轮都开 SQLite
        self._token_usage_cache: dict[str, tuple[float, int]] = {}
        self._token_logged: set[str] = set()
        self._token_debugged: set[str] = set()
        # 正在滚动纪要的会话（和提炼分开：两件事可以并行）
        self._summarizing: dict[str, bool] = {}
        # 本轮的人格快照，等 on_agent_done 归档这一轮时一起带上
        self._pending_turn: dict[str, dict] = {}
        # 这一轮最后一条助手消息的指纹：回复里追加的图片/语音挂它身上
        self._last_assistant_fp: dict[str, str] = {}

        # 如果之前启用过本地模型，启动时尝试加载；失败自动停在哈希向量。
        self._load_active_local_model()

        # WebUI：老版本没有 register_web_api，自动跳过，不影响其它功能
        self.web = WebApi(self)
        try:
            self.web.register()
        except Exception as exc:
            logger.warning(f"[all_memory] WebUI 注册失败（不影响使用）: {exc}")

        # 每天定时导出（配置了 auto_export_at 才启动）
        self._export_task = None
        if str(self._cfg("auto_export_at") or "").strip():
            try:
                self._export_task = asyncio.create_task(self._auto_export_loop())
                logger.info(f"[all_memory] 已启用每日导出：{self._cfg('auto_export_at')}")
            except RuntimeError:
                pass

        logger.info(f"[all_memory] 总库 {self.root}｜"
                    f"事件 {self.index.count()} 条｜"
                    f"事实 {self._all_facts_count()} 条｜"
                    f"附件 {media_mod.stats_of(self.root)['files']} 个")

    # ------------------------------------------------------------ 配置

    def _resolve_root(self) -> str:
        configured = str(self._cfg("data_dir") or "").strip()
        if configured:
            return os.path.abspath(configured)
        return os.path.join(os.getcwd(), "data", "all_memory")

    def _cfg(self, key: str):
        """
        取一个配置项的值。

        优先级：AstrBot 插件配置（设置页）> 面板存的 settings.json > 内置默认。

        设置页和面板现在写的是同一份配置，所以两边永远一致；
        settings.json 只留作「宿主没给配置对象」时的兜底，不再压过设置页——
        不然在设置页改完，旧值还躺在 settings.json 里继续生效，就成单向同步了。
        """
        default = DEFAULTS.get(key)
        value = None

        config = getattr(self, "config", None)
        if config is not None:
            try:
                value = (config.get(key) if hasattr(config, "get")
                         else getattr(config, key, None))
            except Exception:
                value = None

        if value is None and settings_mod.is_runtime(key):
            runtime = getattr(self, "settings", None)
            if runtime is not None:
                value = runtime.get(key)

        return default if value is None else value

    def apply_settings(self, values: dict) -> dict:
        """
        把面板改的值写回 AstrBot 的插件配置（data/config/<插件>_config.json）。

        写进去之后「设置」页看到的也是这份，两边双向同步；
        写文件失败也不影响本轮生效（内存里的 config 已经改了）。
        """
        config = getattr(self, "config", None)
        if config is None or not isinstance(values, dict):
            return {}

        applied = {}
        for key, value in values.items():
            if key not in DEFAULTS or not settings_mod.is_runtime(key):
                continue
            try:
                config[key] = value
                applied[key] = value
            except Exception:
                continue
        if not applied:
            return {}

        save = getattr(config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as exc:
                logger.warning(f"[all_memory] 面板设置写回配置文件失败: {exc}")
        return applied

    def extra_backup_dirs(self) -> list[str]:
        """导出时要一并打包的额外目录（AstrBot 数据目录以外的东西）。"""
        raw = self._cfg("backup_extra_dirs")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        out = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(os.path.abspath(item.strip()))
        return out

    def astrbot_data_root(self) -> str:
        """
        AstrBot 的数据根目录 —— plugins / plugin_data / config / 数据库都在下面。

        每个插件都是小木的一件"技能"，它们的数据和自带资源（表情包之类）
        自然也属于她的记忆。所以导出时默认整目录扫描，不用手工列路径。
        """
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            value = get_astrbot_data_path()
            if value and os.path.isdir(str(value)):
                return os.path.abspath(str(value))
        except Exception:
            pass
        # 兜底：AstrBot 默认在当前工作目录下建 data/
        guess = os.path.abspath(os.path.join(os.getcwd(), "data"))
        return guess if os.path.isdir(guess) else ""

    def _window(self) -> int:
        try:
            return int(self._cfg("window_tokens") or 0)
        except (TypeError, ValueError):
            return 0

    def _plugin_version(self) -> str:
        """读 metadata.yaml 的 version，不引 YAML 依赖。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "metadata.yaml")
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                for line in handle:
                    key, _, value = line.partition(":")
                    if key.strip() == "version":
                        return value.strip().strip("'\"") or "unknown"
        except OSError:
            pass
        return "unknown"

    def _manifest_meta(self) -> dict:
        """manifest 里记的模型/提示词版本；只记知道的，不猜。"""
        return {
            "models": {
                "chat": "current",
                "extract": self._extract_model_name(),
                "embedding": self.embedder.name,
            },
            "prompt_versions": {
                "extract": refine.PROMPT_VERSION,
                "summary": summary_mod.PROMPT_VERSION,
            },
        }

    def _extract_model_name(self) -> str:
        """提炼实际用的模型名（用于 facts provenance），不假装用了 extract_model。"""
        try:
            provider = self.context.get_using_provider()
            model = provider.get_model() if provider is not None else ""
            return str(model or "current")
        except Exception:
            return "current"

    # ------------------------------------------------------------ 按 id 取存储

    def facts_for(self, uid: str):
        return extract.FactStore(self.root, uid)

    def entities_for(self, uid: str):
        return EntityStore(self.root, uid)

    def synonyms_for(self, uid: str):
        return SynonymStore(self.root, uid)

    def summaries_for(self, uid: str):
        return summary_mod.SummaryStore(self.root, uid)

    def media_for(self, uid: str):
        return media_mod.MediaStore(self.root, uid)

    def failed_for(self, uid: str):
        return pending_mod.FailedQueue(self.root, uid)

    def query_cache_for(self, uid: str):
        return query_mod.QueryCache(self.root, uid)

    def media_stats(self) -> dict:
        return media_mod.stats_of(self.root)

    def _uid(self, event) -> str:
        return self.ids.resolve(
            self._session_of(event), self._sender_of(event),
            self._sender_name_of(event))

    def _all_facts_count(self) -> int:
        total = 0
        for uid in self.vault.ids():
            try:
                total += len(self.facts_for(uid).all_facts())
            except Exception:
                continue
        return total

    def _all_summaries_stats(self) -> dict:
        sessions = 0
        chars = 0
        for uid in self.vault.ids():
            try:
                stats = self.summaries_for(uid).stats()
            except Exception:
                continue
            sessions += int(stats.get("sessions") or 0)
            chars += int(stats.get("chars") or 0)
        return {"sessions": sessions, "chars": chars}

    # ------------------------------------------------------------ 本地向量模型

    def _load_active_local_model(self) -> bool:
        """启动时恢复上次启用的向量来源；失败不抛，继续用哈希。"""
        if not self.local_models.enabled:
            return False
        if self.local_models.source == "api":
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._restore_api_embedder())
            except RuntimeError:
                return False
            return False
        if not self.local_models.active_id:
            return False
        model_id = self.local_models.active_id
        try:
            embedder = self.local_models.enable(model_id)
            self._apply_embedder(embedder)
            logger.info(f"[all_memory] 已加载本地向量模型：{model_id}")
            return True
        except Exception as exc:
            logger.warning(
                f"[all_memory] 本地向量模型加载失败，继续用哈希向量：{exc}")
            return False

    def _apply_embedder(self, embedder) -> None:
        self.embedder = embedder
        self.vectors = vectors_mod.VectorStore(self.root, embedder.name)
        self._vector_started.clear()

    async def _restore_api_embedder(self) -> None:
        provider_id = self.local_models.provider_id
        provider = await self._resolve_embedding_provider(provider_id)
        if provider is None:
            logger.warning(
                f"[all_memory] API 向量 Provider 不可用：{provider_id or '(auto)'}")
            return
        try:
            from .core.local_embed import ProviderEmbedder
            embedder = ProviderEmbedder(
                provider, provider_id, asyncio.get_running_loop())
            self._apply_embedder(embedder)
            logger.info(f"[all_memory] 已加载 API 向量 Provider：{provider_id}")
        except Exception as exc:
            logger.warning(f"[all_memory] API 向量加载失败：{exc}")

    @staticmethod
    def _has_embedding(provider) -> bool:
        return any(callable(getattr(provider, name, None))
                   for name in ("get_embedding", "get_embeddings",
                                "get_embeddings_batch"))

    @staticmethod
    async def _maybe_await(value):
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _provider_info(provider) -> dict:
        config = getattr(provider, "provider_config", {}) or {}
        provider_id = str(config.get("id") or "")
        if not provider_id:
            try:
                provider_id = str(provider.meta().id)
            except Exception:
                provider_id = ""
        model = ""
        try:
            model = str(provider.get_model() or "")
        except Exception:
            model = str(config.get("embedding_model") or "")
        return {
            "id": provider_id,
            "name": str(config.get("name") or config.get("display_name")
                        or provider_id or "Embedding Provider"),
            "model": model,
        }

    async def _resolve_embedding_provider(self, provider_id: str = ""):
        for getter_name in ("get_embedding_provider_by_id", "get_provider_by_id"):
            getter = getattr(self.context, getter_name, None)
            if not callable(getter):
                continue
            try:
                provider = await self._maybe_await(getter(provider_id))
            except Exception:
                provider = None
            if provider is not None and self._has_embedding(provider):
                return provider
        for getter_name in ("get_all_embedding_providers", "get_all_providers"):
            getter = getattr(self.context, getter_name, None)
            if not callable(getter):
                continue
            try:
                values = await self._maybe_await(getter())
            except Exception:
                continue
            if isinstance(values, dict):
                values = list(values.values())
            for provider in values or []:
                if not self._has_embedding(provider):
                    continue
                info = self._provider_info(provider)
                if not provider_id or info["id"] == provider_id:
                    return provider
        return None

    async def _embedding_providers(self) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for getter_name in ("get_all_embedding_providers", "get_all_providers"):
            getter = getattr(self.context, getter_name, None)
            if not callable(getter):
                continue
            try:
                values = await self._maybe_await(getter())
            except Exception:
                continue
            if isinstance(values, dict):
                values = list(values.values())
            for provider in values or []:
                if not self._has_embedding(provider):
                    continue
                info = self._provider_info(provider)
                if not info["id"] or info["id"] in seen:
                    continue
                seen.add(info["id"])
                out.append(info)
        return out

    async def model_market(self) -> dict:
        data = self.local_models.status()
        data["providers"] = await self._embedding_providers()
        data["active_embedder"] = self.embedder.name
        source = self.local_models.source if self.local_models.enabled else "hash"
        data["source"] = source
        data["provider_id"] = self.local_models.provider_id
        for item in data.get("models") or []:
            item["active"] = bool(
                item.get("active") and source == "local"
                and str(self.embedder.name) == f"onnx-{item.get('id')}")
        data["api_active"] = bool(
            source == "api"
            and str(self.embedder.name)
            == f"api-{self.local_models.provider_id}")
        data["rebuilding"] = bool(
            self._local_model_task and not self._local_model_task.done())
        return data

    def start_model_download(self, model_id: str, parent: str = "") -> dict:
        return self.local_models.start_download(model_id, parent)

    async def enable_local_model(self, model_id: str) -> dict:
        try:
            embedder = await asyncio.to_thread(
                self.local_models.enable, model_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._apply_embedder(embedder)
        if self._local_model_task and not self._local_model_task.done():
            return {"ok": True, "model": model_id, "message": "模型已启用，正在重建向量"}
        self._local_model_task = asyncio.create_task(self._rebuild_vectors_async())
        return {"ok": True, "model": model_id,
                "message": "模型已启用，正在后台重建全量向量"}

    async def enable_api_embedder(self, provider_id: str) -> dict:
        provider = await self._resolve_embedding_provider(provider_id)
        if provider is None:
            return {"ok": False, "error": "找不到可用的 embedding provider"}
        try:
            from .core.local_embed import ProviderEmbedder
            embedder = ProviderEmbedder(
                provider, provider_id, asyncio.get_running_loop())
            self.local_models.set_api(provider_id)
            self._apply_embedder(embedder)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if self._local_model_task and not self._local_model_task.done():
            return {"ok": True, "provider_id": provider_id,
                    "message": "API 向量已启用，正在重建向量"}
        self._local_model_task = asyncio.create_task(self._rebuild_vectors_async())
        return {"ok": True, "provider_id": provider_id,
                "message": "API 向量已启用，正在后台重建全量向量"}

    async def _rebuild_vectors_async(self) -> None:
        try:
            total = await asyncio.to_thread(
                self.vectors.rebuild, self.vault, self.embedder)
            logger.info(
                f"[all_memory] 向量重建完成：{total} 条（{self.embedder.name}）")
        except Exception as exc:
            logger.warning(f"[all_memory] 向量重建失败，继续用字面召回：{exc}")

    def disable_local_model(self) -> dict:
        self.local_models.disable()
        self._apply_embedder(vectors_mod.LocalEmbedder())
        return {"ok": True, "message": "已停用向量模型，回到内置哈希向量"}

    # ------------------------------------------------------------ 主钩子

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, *args, **kwargs):
        """
        注意签名用了 *args：有些插件（比如 group_chat_plus）会把所有
        on_llm_request 钩子再包一层做统计，转发时会多塞参数。
        写死 (event, req) 会被它们搞成 TypeError，直接导致整轮不归档。
        """
        req = _pick_request(args, kwargs)
        if req is None:
            logger.debug("[all_memory] on_llm_request 没找到 ProviderRequest，跳过")
            return

        if not self._cfg("enabled"):
            return

        session = self._session_of(event)
        if not self.ids.is_private(session):
            return
        uid = self._uid(event)

        # 第一次进来时把该 id 的历史向量补齐（后台跑，不阻塞本轮）
        self._ensure_vector_backfill(uid)
        self._ensure_media_backfill(uid)

        try:
            raw, parsed = sync.parse_contexts(req)
            system_prompt = sync.system_of(req)
            persona = self.vault.put_persona(system_prompt)

            # 0) 先把上次没写进去的补上（补写发生在新增之前，顺序不会乱）
            self._flush_failed(uid)

            # 记住人格和「历史到哪为止」，等这一轮跑完在 on_agent_done 里
            # 只把这一轮的消息补进去（历史已经在库里了，不能整段重写）
            self._pending_turn[uid] = {
                "persona": persona or "",
                "context_count": len(req.contexts) if isinstance(req.contexts, list) else 0,
                "last_fp": (vault_mod.fp_of(f"{parsed[-1]['role']}\x00{parsed[-1]['content']}")
                            if parsed else ""),
            }

            # 1) 归档：先把看到的一切存下来，再谈裁剪
            state = self.states.load(uid)
            stats = sync.sync(
                self.vault, self.index, parsed, persona,
                session=session, ts_hint=self._message_time(event),
                carry=_parse_stamp(state.get("last_carry")), uid=uid)
            if stats.get("new"):
                logger.info(f"[all_memory] 新增 {stats['new']} 条记忆 "
                            f"(id {uid}，去重 {stats.get('duplicate', 0)})")
            if stats.get("carry"):
                state["last_carry"] = stats["carry"]

            # 全局接管：关 = 所有会话都不裁不注入；开 = 所有会话都注入。
            # 默认「自动」= 还没开，任意会话第一次裁剪时自动开（对所有会话生效）。
            global_mode = budget.takeover_mode(self.states.load(GLOBAL_TAKEOVER))

            # 全局关闭接管：所有会话都不裁剪、不注入，交回 AstrBot 原生上下文。
            # 只把「已经发生过的对话」照常归档，保证原文一条不少。
            if global_mode == "off":
                self.states.save(uid, state)
                self._last_raw_estimate.pop(uid, None)
                self._maybe_extract(event, uid)
                return

            # 附件不在这一层抓：这里的 parsed 是历史，本轮消息还没进来。
            # 真正的归档在 on_agent_done（拿到本轮用户消息的指纹之后）。

            calibrator = Calibrator.from_dict(state.get("calibration"))
            calibrator.enabled = bool(self._cfg("calibrate_tokens"))

            # AstrBot 的工具定义在 req.func_tool 里，不在 system_prompt。
            # 不数它，真实 95k 的请求会被算成 48k。
            tool_tokens = _tool_schema_tokens(req)
            system_tokens = estimate_tokens(system_prompt) + tool_tokens
            history_raw = sum(estimate_tokens(m["content"]) for m in parsed)
            window = self._window()

            # 上一轮的 conversation.token_usage 是宿主给的真实总量；
            # 再用 on_llm_response 存下来的 last_prompt_tokens 更准（不含输出）。
            previous_prompt = int(state.get("last_prompt_tokens", 0) or 0)
            actual_prompt = (previous_prompt
                             or self._db_token_usage(session)
                             or _conversation_tokens(req))
            previous_raw = self._last_raw_estimate.get(uid, 0)
            if actual_prompt and previous_raw:
                calibrator.learn(previous_raw, actual_prompt)
                state["calibration"] = calibrator.to_dict()

            # 80% 压力优先用宿主真实注入量；没有才退回校准后的字符估算。
            trusted_tokens = actual_prompt if actual_prompt > 0 else 0
            if trusted_tokens and window > 0 and uid not in self._token_logged:
                self._token_logged.add(uid)
                logger.info(
                    f"[all_memory] 真实注入 {trusted_tokens} tok"
                    f"（{trusted_tokens / window * 100:.1f}%），80% 按它算")
            self._last_raw_estimate[uid] = history_raw + system_tokens

            if window <= 0:
                # 没有可信窗口就不接管上下文，只归档
                self._maybe_extract(event, uid)
                return

            # 2) 预算决策（滞回）
            #    facts 固定占窗口 10%（facts_ratio_for 直接返回上限，不随裁剪次数爬坡）；
            #    早期库小、抽不满这个额度，历史自然还能多留一些
            trim_count = int(state.get("trim_count", 0) or 0)
            # 全局接管刚打开时，会话可能还没裁过；先按「第 1 次」给 facts 预算，
            # 否则 facts_ratio 会是 0，人物卡一块都进不去。
            if global_mode == "on" and trim_count <= 0:
                trim_count = 1
                state["trim_count"] = 1
            projected = trim_count + 1
            plan = budget.decide(
                parsed, system_tokens, window, calibrator,
                trigger_ratio=float(self._cfg("trigger_ratio")),
                total_ratio=float(self._cfg("total_ratio")),
                facts_ratio=budget.facts_ratio_for(projected),
                map_ratio=float(self._cfg("map_ratio")),
                recall_ratio=float(self._cfg("recall_ratio")),
                summary_ratio=float(self._cfg("summary_ratio")),
                trusted_tokens=trusted_tokens,
            )
            if plan.applied:
                trim_count = projected
                state["trim_count"] = trim_count
            facts_ratio = budget.facts_ratio_for(trim_count) if trim_count else 0.0

            # 裁剪那一刻触发两件后台活：提炼人物卡、滚动前情纪要。
            # 都扔到后台，本轮回复不等它们。
            if plan.applied and not self._cfg("dry_run"):
                self._schedule_refine(uid, budget.block_budget(
                    window, facts_ratio,
                    getattr(calibrator, "ratio", 1.0) or 1.0))
                self._schedule_summary(uid)
                # 默认「自动」：任意会话第一次真裁剪，就把全局接管打开，
                # 之后所有会话（包括新开的）都开始注入记忆。
                if global_mode == "auto":
                    self.auto_enable_global_takeover()

            # 演练模式：只记账、只写日志，绝不改动本轮请求
            if self._cfg("dry_run"):
                if plan.applied:
                    logger.info(f"[all_memory] DRY-RUN {plan.summary()} → 未改动任何内容")
                self._maybe_extract(event, uid)
                return

            # 3) 触发裁剪，回写原始对象（保住宿主消息格式）
            #    trimmed_mode：只要裁剪过一次，此后每轮都处于「接管模式」，
            #    持续注入事实/地图/检索。否则裁完回到 40% 压力，
            #    后续几十轮 applied=False，被裁掉的内容会有一段很长的失忆期。
            trimmed_mode = bool(state.get("trimmed_mode")) or global_mode == "on"
            if plan.applied:
                keep_ids = {m["idx"] for m in plan.kept}
                req.contexts = [item for i, item in enumerate(raw) if i in keep_ids]
                trimmed_mode = True
                # 裁剪后真正发出去的历史短了，边界要跟着改
                pending = self._pending_turn.get(uid)
                if isinstance(pending, dict):
                    pending["context_count"] = len(req.contexts)
                logger.info(f"[all_memory] {plan.summary()}")

            # 4) 组装记忆块
            #    第一次裁剪之前与原版完全一致：不裁剪、不注入任何块。
            blocks: list[str] = []
            history_raw = sum(estimate_tokens(m["content"])
                              for m in (plan.kept if plan.applied else parsed))
            sizes = {
                "system": calibrator.apply(system_tokens),
                "history": calibrator.apply(history_raw),
                "summary": 0,
                "facts": 0,
                "map": 0,
                "recall": 0,
            }

            # 纪要排最前面：先给"前情"，再给"她是谁"，最后给细节。
            # 只在接管模式下注入——触发裁剪之前，一块都不加。
            if trimmed_mode:
                summary_block = inject.build_summary_block(
                    str(self.summaries_for(uid).load().get("text") or ""),
                    self._block_budget(
                        window, float(self._cfg("summary_ratio")), calibrator))
                if summary_block:
                    blocks.append(summary_block)
                    sizes["summary"] = calibrator.apply(
                        estimate_tokens(summary_block))

            # 本轮用户刚说的话：它不在 parsed 里（contexts 只有历史），
            # 得从 req.prompt 拿；拿不到才退回最后一条上下文。
            query_text = (getattr(req, "prompt", "") or "").strip() \
                or (parsed[-1]["content"] if parsed else "")

            if trimmed_mode or self._cfg("facts_before_trigger"):
                facts_budget = self._block_budget(
                    window, facts_ratio, calibrator)
                facts_block = inject.build_facts_block(
                    self._active_facts(uid, query_text,
                                       budget=facts_budget),
                    facts_budget)
                if facts_block:
                    blocks.append(facts_block)
                    sizes["facts"] = calibrator.apply(
                        estimate_tokens(facts_block))

            if trimmed_mode:
                recall_trace = {
                    "at": _now_iso(), "query": query_text[:200],
                    "candidates": 0, "known": 0, "after_known": 0,
                    "expanded": 0, "final": 0, "chars": 0,
                    "fps": [],
                }
                if self._cfg("auto_recall"):
                    limit = max(1, int(self._cfg("recall_limit")))
                    # 两路召回（字面 + 向量），多取一些：下面要剔掉已在本轮上下文里的
                    hits = await self.retrieve(
                        query_text, uid=uid, limit=limit * 2,
                        use_llm=bool(self._cfg("recall_expand")))

                    # 最近 N 轮已经原样发出去了，检索再把它们捞回来纯属浪费，
                    # 还会让同一句话重复出现。按内容指纹剔除。
                    in_context = plan.kept if plan.applied else parsed
                    skip = {vault_mod.fp_of(f"{m['role']}\x00{m['content']}")
                            for m in in_context}
                    # earliest / 按天 / 负向 / 前后文已经在 retrieve_pipeline 里做完，
                    # 这里只做“剔除本轮上下文”和计数。
                    recall_trace["candidates"] = len(hits)
                    before_known = len(hits)
                    hits = inject.drop_known(hits, skip)[:limit]
                    recall_trace["known"] = before_known - len(hits)
                    recall_trace["after_known"] = len(hits)
                    recall_trace["expanded"] = sum(
                        1 for hit in hits if hit.get("is_context"))
                    hits = inject.drop_known(hits, skip)
                    recall_trace["final"] = len(hits)
                    recall_trace["fps"] = [str(h.get("fp") or "")
                                           for h in hits[:10]]

                    recall_block = inject.build_recall_block(
                        hits,
                        self._block_budget(
                            window, float(self._cfg("recall_ratio")), calibrator),
                        max_chars=int(self._cfg("recall_snippet_chars") or 800))
                    recall_trace["chars"] = len(recall_block or "")
                    if recall_block:
                        blocks.append(recall_block)
                        sizes["recall"] = calibrator.apply(
                            estimate_tokens(recall_block))
                    logger.info(
                        f"[all_memory] 检索：候选 {recall_trace['candidates']} / "
                        f"已在本轮上下文 {recall_trace['known']} / "
                        f"扩上下文 +{recall_trace['expanded']} / "
                        f"最终 {recall_trace['final']}（{recall_trace['chars']} 字）")
                state["last_recall"] = recall_trace

                # 地图覆盖全部历史，不是只回看 N 天；只算当前 id
                day_rows = self.index.day_summary(days=None, uid=uid)
                map_block = inject.build_map_block(
                    day_rows,
                    self._block_budget(
                        window, float(self._cfg("map_ratio")), calibrator),
                    detail_days=int(self._cfg("map_days")))
                if map_block:
                    blocks.append(map_block)
                    sizes["map"] = calibrator.apply(estimate_tokens(map_block))

            payload = inject.compose(blocks)
            if payload and not inject.attach(req, payload):
                logger.debug("[all_memory] 无法附加记忆块（TextPart 不可用）")

            # 5) 状态回写（含各块实际用量，供面板查看）
            #    payload 本来就是 facts + 地图 + 检索拼起来的（外加使用提示和 _no_save 戳），
            #    所以合计只能 = 主体（system + history）+ payload。
            #    把那三块再加一遍会让「合计」凭空多算一次，拿它调参会被带偏。
            sizes["payload"] = (calibrator.apply(estimate_tokens(payload))
                                if payload else 0)
            if trusted_tokens > 0:
                # 合计显示宿主真实注入量，不再拿字符估算冒充。
                sizes["total"] = trusted_tokens
                # 真实总量里，除了历史/注入块，剩下的主要就是工具定义。
                # 面板没有单独的「工具」格，就并进「人格/工具」，别让它凭空消失。
                residual = trusted_tokens - sizes["history"] - sizes["payload"]
                if residual > sizes["system"]:
                    sizes["system"] = residual
            else:
                sizes["total"] = budget.total_request_tokens(
                    sizes["system"], sizes["history"], sizes["payload"])
            usage = {
                "at": _now_iso(),
                "window": window,
                "tokens": sizes,
                "pct": {k: round(v / window * 100, 2) if window else 0
                        for k, v in sizes.items()},
                "facts_ratio": round(facts_ratio * 100, 2),
                "trim_count": trim_count,
                "applied": plan.applied,
            }
            if window:
                logger.info("[all_memory] 用量 "
                            + " / ".join(f"{k} {v:.1f}%"
                                         for k, v in usage["pct"].items()
                                         if k != "payload"))

            state["calibration"] = calibrator.to_dict()
            state["trimmed_mode"] = trimmed_mode
            state["last_usage"] = usage
            state["last_plan"] = {
                "at": _now_iso(),
                "applied": plan.applied,
                "pressure": round(plan.pressure, 4),
                "after": plan.after,
                "dropped": plan.dropped,
            }
            self.states.save(uid, state)

            self._maybe_extract(event, uid)
        except Exception as exc:
            logger.warning(f"[all_memory] 请求处理失败（已跳过本轮）: {exc}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, *args, **kwargs):
        """
        只做用量校准。

        **不要在这里把助手那句话写进原始层**：本轮用户消息还不在
        req.contexts 里，宿主要到整轮结束才一起回存，这里先写回复的话，
        每条回复都会抢在它的问句前面——顺序就这么错开的（见 on_agent_done）。
        """
        resp = _pick_response(event, args, kwargs)
        if resp is None:
            logger.debug("[all_memory] on_llm_response 没找到 LLMResponse，跳过")
            return
        if not self._cfg("enabled"):
            return
        if not self.ids.is_private(self._session_of(event)):
            return
        uid = self._uid(event)
        try:
            # 用真实用量校准估算
            usage = getattr(resp, "usage", None)
            actual = _prompt_tokens(usage)
            raw_estimate = self._last_raw_estimate.get(uid, 0)
            if actual:
                state = self.states.load(uid)
                state["last_prompt_tokens"] = int(actual)
                if raw_estimate:
                    calibrator = Calibrator.from_dict(state.get("calibration"))
                    calibrator.enabled = bool(self._cfg("calibrate_tokens"))
                    calibrator.learn(raw_estimate, actual)
                    state["calibration"] = calibrator.to_dict()
                self.states.save(uid, state)
        except Exception as exc:
            logger.debug(f"[all_memory] 用量校准失败: {exc}")

    if hasattr(filter, "on_decorating_result"):

        @filter.on_decorating_result()
        async def on_decorating_result(self, event: AstrMessageEvent, *args, **kwargs):
            """
            发送前把这一轮**真正发出去**的图片/语音存下来。

            为什么必须在这里：有些插件（表情包、图片对话之类）在模型回复之后
            自己往消息链里追加图片，那些图既不在 run_context.messages 里，
            AstrBot 的会话页也不会记录——只有这个钩子能看到整条出站消息。
            """
            if not self._cfg("enabled"):
                return
            try:
                result = event.get_result()
                chain = getattr(result, "chain", None) if result is not None else None
                if not chain:
                    return

                sources = []
                for component in chain:
                    kind = media_mod.guess_kind(component)
                    if not kind:
                        continue
                    source = media_mod.source_of(component)
                    if not source:
                        continue
                    name = ""
                    for attr in ("name", "filename", "file_name"):
                        value = getattr(component, attr, None)
                        if isinstance(value, str) and value.strip():
                            name = os.path.basename(value.strip())
                            break
                    sources.append({"kind": kind, "source": source, "name": name})
                if not sources:
                    return

                session = self._session_of(event)
                if not self.ids.is_private(session):
                    return
                uid = self._uid(event)
                # 挂在最后一条助手消息上；找不到就退到本会话最后一条记录
                msg_fp = (self._last_assistant_fp.get(uid)
                          or self.index.last_fp(session, uid))
                if not msg_fp:
                    return
                task = asyncio.create_task(
                    self.media_for(uid).ingest(msg_fp, session, sources))
                task.add_done_callback(_log_task_error)
                logger.debug(f"[all_memory] 出站附件 {len(sources)} 个 → {msg_fp}")
            except Exception as exc:
                logger.debug(f"[all_memory] 出站附件归档失败: {exc}")

    # 老版本 AstrBot 没有 on_agent_done 这个钩子。有就注册；没有就跳过，
    # 归档退化成「只用 req.contexts」（顺序仍然正确，只是本轮要等下一轮才进库）。
    # 这样插件在旧宿主上也能加载，不会因为装饰器不存在直接崩掉。
    if hasattr(filter, "on_agent_done"):

        @filter.on_agent_done()
        async def on_agent_done(self, event: AstrMessageEvent, *args, **kwargs):
            """
            一轮跑完，把宿主即将回存的那几条消息按原顺序补进原始层。

            为什么必须在这里：AstrBot 的 req.contexts 只有**历史**，
            本轮用户消息在 req.prompt + extra_user_content_parts 里，
            宿主是整轮结束后才把「用户消息 + 工具消息 + 回复」一起写回会话历史。
            所以请求钩子永远看不到本轮内容——上一版就靠回复钩子提前存助手那句话，
            问句却要等下一轮从 contexts 补，结果每条回复都排到了问句前面。

            run_context.messages 正是宿主保存历史时用的同一份列表
            （system 在第 0 位，带 _no_save 的会被丢掉），
            照它抄：顺序天然一致，工具消息也不会漏，下一轮 contexts 里再见到时
            指纹一模一样，直接去重。
            """
            if not self._cfg("enabled"):
                return
            rc = _pick_run_context(args, kwargs)
            messages = getattr(rc, "messages", None) if rc is not None else None
            if not isinstance(messages, list) or not messages:
                return

            session = self._session_of(event)
            if not self.ids.is_private(session):
                return
            uid = self._uid(event)
            pending = self._pending_turn.pop(uid, {}) or {}
            persona = str(pending.get("persona") or "")

            # 只取这一轮（历史已经在库里了）；边界对不上就退回整段去重
            turn, exact = sync.split_turn(
                messages,
                pending.get("context_count") or 0,
                str(pending.get("last_fp") or ""),
            )

            try:
                parsed = sync.turn_messages(turn)
            except Exception as exc:
                logger.warning(f"[all_memory] 本轮消息解析失败: {exc}")
                return
            if not parsed:
                return

            try:
                # exact=True（正常情况）：这一轮照写。同一句话以前说过也要留一条，
                # 只把重复的编成 base-2/-3，不会因为「库里已有」就丢掉。
                # exact=False（边界不可信）：退回按内容去重，绝不重写历史。
                state = self.states.load(uid)
                stats = sync.sync(self.vault, self.index, parsed, persona,
                                  session=session, origin="astrbot_turn",
                                  force=exact,
                                  carry=_parse_stamp(state.get("last_carry")),
                                  sender=self._sender_of(event), uid=uid)
                if stats.get("carry") and stats["carry"] != state.get("last_carry"):
                    state["last_carry"] = stats["carry"]
                    self.states.save(uid, state)
                if stats.get("new"):
                    logger.info(f"[all_memory] 本轮补入 {stats['new']} 条 "
                                f"(id {uid}，编号 {stats.get('renamed', 0)} 条)")
                # 附件：本轮用户消息的图片/语音，键就是刚才存进去的那条指纹
                self._capture_turn_media(event, parsed, session, uid)
                # 记下最后一条助手消息：有些插件会在回复之后自己追加表情，
                # 那些图不在 run_context.messages 里，只能由 decorator 钩子接住
                for item in reversed(parsed):
                    if item.get("role") == "assistant":
                        self._last_assistant_fp[uid] = vault_mod.fp_of(
                            f"assistant\x00{item['content']}")
                        break
                # 向量：纯本地计算，几十毫秒，后台补，别拖慢回复
                try:
                    asyncio.create_task(
                        asyncio.to_thread(self.vectorize_recent, 400, uid))
                except RuntimeError:
                    pass
            except Exception as exc:
                # 写失败不能只留一行日志：先落盘，下一轮请求进来时补写
                path = self.failed_for(uid).push(session, parsed, persona, str(exc))
                logger.warning(
                    f"[all_memory] 本轮归档失败，"
                    + (f"已存入重试队列（{os.path.basename(path)}）" if path
                       else "重试队列也写不进去")
                    + f": {exc}")

    # ------------------------------------------------------------ 接管模式

    def global_takeover_state(self) -> dict:
        """给面板看的全局接管状态：auto / on / off。"""
        state = self.states.load(GLOBAL_TAKEOVER)
        mode = budget.takeover_mode(state)
        return {
            "mode": mode,
            "manual": str(state.get("takeover_manual") or "") == "on",
            "trimmed": bool(state.get("trimmed_mode")),
            "takeover_at": str(state.get("takeover_at") or ""),
            "window": self._window(),
        }

    def set_global_takeover(self, on: bool) -> dict:
        """全局开关：一次对所有会话生效（含以后新开的会话）。"""
        if on and self._window() <= 0:
            return {"ok": False,
                    "error": "先填上下文窗口（window_tokens），否则接管不会注入"}
        state = self.states.load(GLOBAL_TAKEOVER)
        if on:
            state["takeover_manual"] = "on"
            state["trimmed_mode"] = True
        else:
            state["takeover_manual"] = "off"
            state["trimmed_mode"] = False
        state["takeover_at"] = _now_iso()
        self.states.save(GLOBAL_TAKEOVER, state)
        logger.info(f"[all_memory] 全局接管{'开启' if on else '关闭'}")
        return {"ok": True, **self.global_takeover_state()}

    def auto_enable_global_takeover(self) -> None:
        """默认「自动」：任意会话第一次真裁剪时，把全局接管打开。"""
        state = self.states.load(GLOBAL_TAKEOVER)
        if str(state.get("takeover_manual") or "") == "off":
            return
        if budget.takeover_mode(state) == "on":
            return
        state["takeover_manual"] = ""
        state["trimmed_mode"] = True
        state["takeover_at"] = _now_iso()
        self.states.save(GLOBAL_TAKEOVER, state)
        logger.info("[all_memory] 首次裁剪：全局接管已自动开启（所有会话）")

    def enable_takeover(self, sessions=None) -> int:
        """恢复备份后用：直接开全局接管。返回值只用于提示影响面。"""
        result = self.set_global_takeover(True)
        if not result.get("ok"):
            return 0
        return len(list(sessions or [])) or 1

    def disable_takeover(self) -> None:
        self.set_global_takeover(False)

    @filter.command("allmem_takeover")
    async def cmd_takeover(self, event: AstrMessageEvent, mode: str = ""):
        """全局开关：开=所有会话都带记忆；关=全都不裁不注入，交回官方。"""
        try:
            mode = (mode or "").strip().lower()
            if mode in ("off", "0", "关", "关闭"):
                self.disable_takeover()
                yield event.plain_result(
                    "已关闭全局接管：所有会话都不再裁剪、不再注入记忆块，"
                    "交回 AstrBot 原生上下文。\n"
                    "已经被裁掉的历史不会自动回来；要恢复全量得从 vault 重建。\n"
                    "重新开启：/allmem_takeover")
                return

            result = self.set_global_takeover(True)
            if not result.get("ok"):
                yield event.plain_result(result.get("error") or "开启失败")
                return
            yield event.plain_result(
                "已开启全局接管：所有会话（含以后新开的）都会带记忆地图 + "
                "相关性检索（预算约 8%），有事实的话也会带人物卡。\n"
                "关闭：/allmem_takeover off")
        except Exception as exc:
            yield event.plain_result(f"操作失败: {exc}")

    # ------------------------------------------------------------ 导出

    def export_targets(self) -> list[tuple]:
        """
        导出时要打包的 (源目录, 包内前缀, 该源跳过的子树)。

        跳过列表必须按来源分开，否则插件数据会被跳过或重复打包。
        本地向量模型是运行时资源，不进记忆备份；默认目录和用户自定义目录都跳过。
        """
        exports = os.path.abspath(os.path.join(self.root, "exports"))
        model_skips = {local_models.default_model_parent(self.root)}
        for model in local_models.MODELS:
            path = self.local_models.path_of(model["id"])
            if path:
                model_skips.add(os.path.abspath(path))
        model_skips = tuple(sorted(model_skips))
        targets: list[tuple] = [
            (self.root, "all_memory", (exports, *model_skips))]

        if self._cfg("backup_astrbot_data"):
            data_root = self.astrbot_data_root()
            if data_root:
                targets.append(
                    (data_root, "astrbot_data", (self.root, *model_skips)))
            else:
                logger.warning("[all_memory] 找不到 AstrBot 数据目录，跳过整目录备份")
        return targets

    def run_export(self) -> dict:
        """同步执行一次打包 + 清理旧包。调用方自己丢线程。"""
        exports = os.path.abspath(os.path.join(self.root, "exports"))
        os.makedirs(exports, exist_ok=True)
        name = f"all_memory_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        target = os.path.join(exports, name)

        info = backup_mod.build_zip(target, self.export_targets())
        meta = self._manifest_meta()
        try:
            manifest_info = manifest_mod.attach_to_zip(
                target, plugin_version=self._plugin_version(),
                models=meta["models"],
                prompt_versions=meta["prompt_versions"])
            info["files"] = int(info.get("files", 0)) + 2
            info["manifest"] = {
                "schema": manifest_info.get("schema", ""),
                "events": (manifest_info.get("counts") or {}).get("events", 0),
                "facts": (manifest_info.get("counts") or {}).get("facts", 0),
                "files": (manifest_info.get("counts") or {}).get("files", 0),
            }
        except Exception as exc:
            logger.warning(f"[all_memory] manifest 写入导出包失败: {exc}")
            info["manifest_error"] = str(exc)

        # 让在线目录也自描述；失败不影响本次导出。
        try:
            manifest_mod.write(
                self.root, plugin_version=self._plugin_version(),
                models=meta["models"],
                prompt_versions=meta["prompt_versions"])
        except Exception as exc:
            logger.warning(f"[all_memory] manifest 写入数据目录失败: {exc}")

        pruned = self.prune_exports()
        return {
            "name": name,
            "files": info["files"],
            "skipped": info["skipped"],
            "bytes": info["bytes"],
            "size": info["size"],
            "path": target,
            "pruned": pruned,
            "downloads_path": os.path.join(_downloads_dir(), name),
            "url": f"/api/v1/plugins/extensions/astrbot_plugin_all_memory/download?name={name}",
        }

    # ------------------------------------------------------------ 会话导出

    @staticmethod
    def _session_slug(session: str) -> str:
        return ("".join(ch if ch.isalnum() or ch in "-_." else "_"
                        for ch in (session or "session"))[:60] or "session")

    def _embedded_media(self, items: list[dict]) -> list[dict]:
        """
        把附件内嵌成 data URI，导出文件才是"一个会话一个文件"。

        只写路径的话，文件拿到别处就只剩一堆 `blobs/ab/cd.jpg`，没有意义。
        不设大小上限：导出就该是全的，文件大是用户自己的选择。
        """
        out: list[dict] = []
        for item in items:
            entry = {
                "kind": item.get("kind", ""),
                "name": item.get("name", ""),
                "mime": item.get("mime", ""),
                "size": int(item.get("size") or 0),
            }
            rel = str(item.get("path") or "")
            path = os.path.join(self.root, rel.replace("/", os.sep)) if rel else ""
            raw = b""
            if path and os.path.isfile(path):
                try:
                    with open(path, "rb") as handle:
                        raw = handle.read()
                except OSError:
                    raw = b""
            if raw:
                mime = entry["mime"] or "application/octet-stream"
                entry["data"] = ("data:" + mime + ";base64,"
                                 + base64.b64encode(raw).decode("ascii"))
                out.append(entry)
                continue
            if rel:
                entry["path"] = rel
            out.append(entry)
        return out

    def _write_session_rows(self, session: str, handle, uid: str) -> int:
        """把一个会话写成 jsonl，一行一条；返回条数。"""
        data = self.index.history(session, offset=0, limit=0, newest_first=False)
        items = data.get("items") or []
        fingerprints = {str(item.get("fp") or "") for item in items}
        media_map = self.media_for(uid).records_for(fingerprints)

        for item in items:
            fingerprint = str(item.get("fp") or "")
            row = {
                "fp": fingerprint,
                "ts": item.get("ts", ""),
                "role": item.get("role", ""),
                "text": item.get("text", ""),
                "session": session,
                "origin": item.get("origin", ""),
                "backfill": bool(item.get("backfill")),
            }
            media = media_map.get(media_mod.base_fp(fingerprint)) or []
            if media:
                row["media"] = self._embedded_media(media)
            handle.write(json.dumps(row, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")
        return len(items)

    def run_session_export(self, sessions) -> dict:
        """
        导出选中的会话：一个会话一个文件（jsonl，**不按日期拆**）。

        选一个 → 直接 jsonl；选多个 → 打一个 zip，里面每个会话一个 jsonl。
        浏览器一次只能好好接一个文件，多选时不能指望它连下好几次。
        """
        if isinstance(sessions, str):
            sessions = [sessions]
        chosen = [str(s).strip() for s in (sessions or []) if str(s).strip()]
        chosen = list(dict.fromkeys(chosen))
        if not chosen:
            raise ValueError("先勾选一个会话")

        exports = os.path.join(self.root, "exports")
        os.makedirs(exports, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")

        if len(chosen) == 1:
            session = chosen[0]
            uid = self.ids.id_for(session)
            name = f"{self._session_slug(session)}_{stamp}.jsonl"
            path = os.path.join(exports, name)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                count = self._write_session_rows(session, handle, uid)
            return {
                "name": name,
                "path": path,
                "count": count,
                "bytes": os.path.getsize(path),
                "sessions": chosen,
                "downloads_path": os.path.join(_downloads_dir(), name),
                "url": ("/api/v1/plugins/extensions/astrbot_plugin_all_memory"
                        f"/download?name={name}"),
            }

        name = f"sessions_{stamp}.zip"
        path = os.path.join(exports, name)
        total = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for session in chosen:
                buffer = io.StringIO()
                total += self._write_session_rows(
                    session, buffer, self.ids.id_for(session))
                archive.writestr(f"{self._session_slug(session)}.jsonl",
                                 buffer.getvalue())
        return {
            "name": name,
            "path": path,
            "count": total,
            "bytes": os.path.getsize(path),
            "sessions": chosen,
            "downloads_path": os.path.join(_downloads_dir(), name),
            "url": ("/api/v1/plugins/extensions/astrbot_plugin_all_memory"
                    f"/download?name={name}"),
        }

    def prune_exports(self) -> int:
        """只保留最近 N 个导出包（export_keep，0 表示不限）。"""
        try:
            keep = int(self._cfg("export_keep") or 0)
        except (TypeError, ValueError):
            keep = 0
        if keep <= 0:
            return 0

        exports = os.path.join(self.root, "exports")
        if not os.path.isdir(exports):
            return 0

        files = [os.path.join(exports, n) for n in os.listdir(exports)
                 if n.startswith("all_memory_") and n.endswith(".zip")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

        removed = 0
        for path in files[keep:]:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                continue
        if removed:
            logger.info(f"[all_memory] 清理旧导出包 {removed} 个（保留 {keep} 个）")
        return removed

    async def _auto_export_loop(self) -> None:
        """每天到点自动导出一次。"""
        while True:
            delay = _seconds_until(str(self._cfg("auto_export_at") or ""))
            if delay is None:
                return
            await asyncio.sleep(max(60, delay))
            try:
                info = await asyncio.to_thread(self.run_export)
                logger.info(f"[all_memory] 自动导出完成：{info['name']}"
                            f"（{round(info['size'] / 1048576, 1)} MB，"
                            f"清理 {info['pruned']} 个旧包）")
            except Exception as exc:
                logger.warning(f"[all_memory] 自动导出失败: {exc}")

    # ------------------------------------------------------------ 人格写回

    async def sync_personas_to_astrbot(self) -> dict:
        """
        把人格快照写进 AstrBot 的人格列表。

        走它自己的 persona_manager，不直接改数据库——AstrBot 会自己处理
        缓存刷新和落库，比我们手写 sqlite 安全得多。

        已经存在同样 system_prompt 的会跳过，不会造重复人格。
        """
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return {"ok": False, "created": [],
                    "error": "当前 AstrBot 版本没有 persona_manager"}

        try:
            existing = await manager.get_all_personas()
        except Exception as exc:
            return {"ok": False, "created": [], "error": f"读取现有人格失败: {exc}"}

        known = {str(getattr(p, "system_prompt", "") or "") for p in existing}
        created: list[str] = []
        failed = 0

        for sha, _size in self.vault.personas():
            text = self.vault.read_persona(sha)
            if not text or text in known:
                continue
            persona_id = f"allmem-{sha[:8]}"
            try:
                await manager.create_persona(persona_id, text,
                                             begin_dialogs=None,
                                             tools=None, skills=None)
                created.append(persona_id)
                known.add(text)
            except Exception as exc:
                failed += 1
                logger.warning(f"[all_memory] 写回人格 {persona_id} 失败: {exc}")

        if created:
            logger.info(f"[all_memory] 已写回 {len(created)} 个人格到 AstrBot")
        return {"ok": True, "created": created, "failed": failed}

    def finalize_index(self, copied: bool) -> dict:
        """
        恢复完决定索引怎么办。

        搬过来的 index.db 能用、条数也和 vault 对得上 → 直接留着，不重建。
        打不开 / 条数不符 / 根本没有 → 重建。
        """
        info = restore_mod.reconcile_index(self.vault, self.index, copied)
        if info.get("index_kept"):
            logger.info(f"[all_memory] 沿用备份里的索引（{info['index_events']} 条）")
        else:
            logger.info(f"[all_memory] 索引已重建（{info.get('index_rebuilt', 0)} 条）")
        return info

    # ------------------------------------------------------------ 提炼

    def _maybe_extract(self, event, uid: str) -> None:
        """可选的周期性提炼（默认关闭，0）。主路径是裁剪时触发。"""
        interval = int(self._cfg("auto_extract_interval") or 0)
        if interval <= 0:
            return
        count = self._turn_counter.get(uid, 0) + 1
        self._turn_counter[uid] = count
        if count % interval != 0:
            return
        if self._extract_cooldown.get(uid, 0) > asyncio.get_event_loop().time():
            return
        try:
            asyncio.create_task(self._refine_job(uid, reason="periodic"))
        except RuntimeError:
            pass

    def start_refine(self, uid: str = "__web__", reason: str = "web",
                     replace: bool = True) -> dict:
        """
        启动一次提炼（指令与面板共用这一个入口）。

        手动触发 / 切片长度变化 → replace=True（全量重炼，旧版归档）。
        自动裁剪 → replace=False（只处理游标之后的新历史，追加）。
        """
        if any(bool(flag) for flag in self._refining.values()):
            return {"ok": False, "error": "已经有一轮提炼在跑，等它结束"}

        window = self._window()
        if window <= 0:
            return {"ok": False, "error": "还没填 window_tokens，算不出 facts 预算"}

        state = self.states.load(uid)
        ratio = budget.facts_ratio_for(int(state.get("trim_count", 0) or 0))
        facts_budget = budget.block_budget(window, ratio, 1.0)

        try:
            task = asyncio.create_task(self._refine_job(
                uid, facts_budget=facts_budget, reason=reason,
                replace=replace))
            task.add_done_callback(_log_task_error)
        except RuntimeError:
            return {"ok": False, "error": "当前没有可用的后台任务循环"}

        return {
            "ok": True,
            "window": window,
            "facts_budget": facts_budget,
            "facts_ratio": round(ratio * 100, 2),
            "replace": bool(replace),
            "message": (f"已开始全量提炼：预算约 {facts_budget} tok"
                        f"（{ratio * 100:.0f}%），旧人物卡会先归档"
                        if replace else
                        f"已开始增量提炼：预算约 {facts_budget} tok"),
        }

    def _schedule_refine(self, uid: str, facts_budget: int) -> None:
        """
        裁剪那一刻触发提炼：只处理该 id 游标之后的新历史，追加。
        """
        if not self._cfg("extract_on_trim"):
            return
        if self._refining.get(uid):
            logger.info("[all_memory] 上一轮提炼还没跑完，本次跳过")
            return
        try:
            task = asyncio.create_task(
                self._refine_job(uid, facts_budget=facts_budget,
                                 reason="trim", replace=False))
            task.add_done_callback(_log_task_error)
        except RuntimeError:
            pass

    async def _refine_job(self, uid: str, facts_budget: int = 0,
                          reason: str = "trim", replace: bool = False) -> None:
        if self._refining.get(uid):
            return
        self._refining[uid] = True
        try:
            window = self._window()
            if facts_budget <= 0 and window > 0:
                state = self.states.load(uid)
                facts_budget = budget.block_budget(
                    window, budget.facts_ratio_for(
                        int(state.get("trim_count", 0) or 0)), 1.0)

            # 读该 id 的原文是 IO，扔线程里，别卡事件循环。
            events = await asyncio.to_thread(self._extract_events, uid)
            if len(events) < 4:
                return

            chunk_chars = int(self._cfg("extract_chunk_chars") or 6000)
            state = self.states.load(uid)
            previous_chunk = int(state.get("facts_chunk_chars", 0) or 0)
            if previous_chunk and previous_chunk != chunk_chars:
                replace = True
                logger.info("[all_memory] 切片长度变了：该 id 事实全量重做")
            cursor = str(state.get("facts_covered_fp") or "")
            cursor_index = int(state.get("facts_covered_index", 0) or 0)

            per_batch = max(1, int(max(200, facts_budget) / max(1, chunk_chars)))
            concurrency = max(1, int(self._cfg("extract_concurrency") or 6))
            total_chars = sum(len(str(e.get("text", ""))) for e in events)
            model_name = self._extract_model_name()

            previous = state.get("last_refine") or {}
            estimate = ""
            if previous.get("batches") and previous.get("seconds"):
                per_batch_seconds = float(previous["seconds"]) / max(1, int(previous["batches"]))
                rough = int(per_batch_seconds * max(1, total_chars // max(1, chunk_chars))
                            / concurrency)
                estimate = f"，预计约 {rough} 秒"
            logger.info(
                f"[all_memory] 开始提炼（{reason}/{ '全量' if replace else '增量' }）："
                f"{len(events)} 条 / {total_chars} 字，按 {chunk_chars} 字切批，"
                f"每批最多 {per_batch} 条，并发 {concurrency}{estimate}")

            started = time.monotonic()
            result = await refine.run(
                self._call_llm, events, self.facts_for(uid),
                target_tokens=max(200, facts_budget),
                model=model_name,
                chunk_chars=chunk_chars,
                max_facts=per_batch,
                replace=replace,
                cursor=("" if replace else cursor),
                cursor_index=(0 if replace else cursor_index),
                cache=refine.ExtractionCache(self.root, uid),
                concurrency=concurrency,
            )
            elapsed = time.monotonic() - started
            logger.info(
                f"[all_memory] 提炼完成（{result.get('mode')}）："
                f"{result.get('batches')} 批"
                f"（复用 {result.get('cached_batches', 0)} 批）→ "
                f"{result.get('written')} 条事实，"
                f"{result.get('tokens')} tok / 预算 {result.get('target_tokens')}，"
                f"用时 {elapsed:.0f} 秒"
                + ("（超出一部分，注入时取最近）"
                   if result.get("over_budget") else ""))

            state = self.states.load(uid)
            state["last_refine"] = {
                "at": _now_iso(),
                "reason": reason,
                "mode": result.get("mode"),
                "events": len(events),
                "events_used": result.get("events_used", 0),
                "chars": total_chars,
                "chunk_chars": chunk_chars,
                "concurrency": concurrency,
                "facts_per_batch": per_batch,
                "batches": result.get("batches"),
                "cached_batches": result.get("cached_batches", 0),
                "written": result.get("written"),
                "tokens": result.get("tokens"),
                "target_tokens": result.get("target_tokens"),
                "over_budget": result.get("over_budget"),
                "failed_batches": result.get("failed_batches"),
                "seconds": round(elapsed, 1),
            }
            if not result.get("error"):
                state["facts_chunk_chars"] = chunk_chars
                if result.get("covered_fp"):
                    state["facts_covered_fp"] = result["covered_fp"]
                if result.get("covered_index"):
                    state["facts_covered_index"] = int(result["covered_index"])
            self.states.save(uid, state)
        except Exception as exc:
            logger.warning(f"[all_memory] 提炼失败（已跳过）: {exc}")
            self._extract_cooldown[uid] = (
                asyncio.get_event_loop().time() + 300)
        finally:
            self._refining[uid] = False

    # ------------------------------------------------------------ 前情纪要

    @staticmethod
    def _first_kept_fp(plan) -> str:
        """裁剪后还留在上下文里的第一条消息 = 纪要的右边界（它之前都该被纪要到）。"""
        kept = getattr(plan, "kept", None) or []
        if not kept:
            return ""
        first = kept[0]
        return vault_mod.fp_of(f"{first['role']}\x00{first['content']}")

    def start_summary(self, uid: str, reason: str = "manual") -> dict:
        """
        启动一次滚动纪要（指令与面板共用）。

        只追加：从上次纪要覆盖到的那条之后，处理到该 id 当前最后一条。
        """
        if self._summarizing.get(uid):
            return {"ok": False, "error": "这个 id 正在生成纪要，等它结束"}

        try:
            task = asyncio.create_task(
                self._summary_job(uid, reason=reason))
            task.add_done_callback(_log_task_error)
        except RuntimeError:
            return {"ok": False, "error": "当前没有可用的后台任务循环"}

        return {
            "ok": True,
            "id": uid,
            "reason": reason,
            "message": "已开始生成前情纪要：只追加还没纪要过的历史",
        }

    def _schedule_summary(self, uid: str) -> None:
        """裁剪那一刻触发。配了 0 预算就不生成，省得白调模型。"""
        try:
            if float(self._cfg("summary_ratio") or 0) <= 0:
                return
        except (TypeError, ValueError):
            return
        if self._summarizing.get(uid):
            logger.info("[all_memory] 上一轮纪要还没跑完，本次跳过")
            return
        try:
            task = asyncio.create_task(
                self._summary_job(uid, "trim"))
            task.add_done_callback(_log_task_error)
        except RuntimeError:
            pass

    async def _summary_job(self, uid: str, reason: str = "trim") -> None:
        if self._summarizing.get(uid):
            return
        self._summarizing[uid] = True
        try:
            events = await asyncio.to_thread(self._session_events, uid)
            if len(events) < 4:
                logger.info("[all_memory] 内容太少，跳过纪要")
                return

            chunk_chars = int(self._cfg("extract_chunk_chars") or 6000)
            logger.info(f"[all_memory] 开始生成纪要（{reason}）：{len(events)} 条，"
                        f"id {uid}")

            result = await summary_mod.build(
                self._call_llm, events, self.summaries_for(uid),
                chunk_chars=chunk_chars,
                cache=summary_mod.SummaryCache(self.root, uid))

            if result.get("skipped"):
                logger.info("[all_memory] 纪要不更新：没有新的可纪要内容")
            elif result.get("ok"):
                logger.info(
                    f"[all_memory] 纪要已更新：{result.get('batches')} 批"
                    f"（复用 {result.get('cached')} 批）→ {result.get('chars')} 字，"
                    f"覆盖到 {result.get('covered_until', '')}")
            else:
                logger.warning(f"[all_memory] 纪要失败：{result.get('error')}")

            state = self.states.load(uid)
            state["last_summary"] = {
                "at": _now_iso(),
                "reason": reason,
                "ok": bool(result.get("ok")),
                "skipped": bool(result.get("skipped")),
                "chars": int(result.get("chars") or 0),
                "batches": int(result.get("batches") or 0),
                "cached": int(result.get("cached") or 0),
                "covered_until": result.get("covered_until", ""),
                "error": result.get("error", ""),
            }
            self.states.save(uid, state)
        except Exception as exc:
            logger.warning(f"[all_memory] 纪要失败（已跳过）: {exc}")
        finally:
            self._summarizing[uid] = False

    def _extract_events(self, uid: str) -> list[dict]:
        """
        提炼用的原文。

        滤掉空行和工具输出：工具结果是噪声（"File written successfully"），
        既拖慢提炼，又稀释 prompt 里的有效信息。

        历史消息没有 sender（回填时宿主不给），这里按会话推一个出来，
        否则模型拿不到 ID，subject 永远是空的——只影响给模型的证据，
        不写回库。
        """
        skip_tool = bool(self._cfg("extract_skip_tool"))
        kept: list[dict] = []
        for event in self.vault.iter_events(uid):
            if not str(event.get("text", "")).strip():
                continue
            if skip_tool and str(event.get("role")) == "tool":
                continue
            kept.append(event)

        known: dict[str, set] = {}
        for event in kept:
            sender = str(event.get("sender") or "").strip()
            if sender:
                known.setdefault(str(event.get("session") or ""), set()).add(sender)

        out: list[dict] = []
        for event in kept:
            if event.get("sender"):
                out.append(event)
                continue
            session = str(event.get("session") or "")
            guess = sync.infer_sender(session, known.get(session))
            out.append({**event, "sender": guess} if guess else event)
        return out

    def _session_events(self, uid: str) -> list[dict]:
        """这个 id 的全部原文，按写入顺序。"""
        return list(self.vault.iter_events(uid))

    async def _call_llm(self, prompt: str) -> str:
        """尽量兼容不同 AstrBot 版本的调用方式，全都失败就返回空。"""
        provider = None
        for getter in ("get_using_provider", "get_provider"):
            fn = getattr(self.context, getter, None)
            if callable(fn):
                try:
                    provider = fn()
                except Exception:
                    provider = None
                if provider:
                    break
        if provider is None:
            return ""

        chat = getattr(provider, "text_chat", None)
        if not callable(chat):
            return ""

        attempts = (
            {"prompt": prompt},
            {"prompt": prompt, "contexts": []},
            {"prompt": prompt, "system_prompt": "你是记忆整理助手，只输出 JSON。"},
        )
        for kwargs in attempts:
            try:
                resp = await chat(**kwargs)
            except TypeError:
                continue
            except Exception:
                return ""
            text = (getattr(resp, "completion_text", None)
                    or getattr(resp, "text", None) or "")
            if isinstance(text, str) and text.strip():
                return text
        return ""

    # ------------------------------------------------------------ 辅助

    # ------------------------------------------------------------ 检索

    async def _expand_terms(self, text: str, use_llm: bool = False,
                            uid: str = "default") -> list[str]:
        """
        让模型补几个同义词（可选）。

        自动召回默认不走这条路：它每轮都跑，多一次模型调用就多一秒延迟。
        工具检索走这条路——模型主动查的时候，多等一会儿值得。
        开关：recall_expand（默认关）。
        """
        if not use_llm:
            return []
        cache = self.query_cache_for(uid)
        cached = cache.get(text)
        if cached is not None:
            return cached
        try:
            raw = await self._call_llm(
                query_mod.EXPAND_PROMPT.replace("{query}", text[:200]))
        except Exception:
            return []
        terms = query_mod.parse_expansions(raw)
        if terms:
            cache.put(text, terms)
        return terms

    async def retrieve(self, text: str, uid: str = "default", limit: int = 20,
                       use_llm: bool = False) -> list[dict]:
        """
        字面 + 向量两路召回，融合排序；只查这个 id。

        自动召回、search_memory 工具、/allmem_search 共用这一个入口。
        具体排序在 core/retrieve_pipeline.py：
        锚点保留 → 字面/向量 RRF → MMR → earliest/按天 → 负向过滤 → 前后文。
        """
        text = (text or "").strip()
        if len(text) < 2:
            return []

        expansions = list(await self._expand_terms(text, use_llm=use_llm,
                                                   uid=uid))
        for value in self.entities_for(uid).expand(text, limit=8):
            if value not in expansions:
                expansions.append(value)
        for value in self.synonyms_for(uid).expand(text, limit=8):
            if value not in expansions:
                expansions.append(value)

        # SQLite / FTS / 向量都在线程里跑；pipeline 内部会批量回表。
        hits = await asyncio.to_thread(
            retrieve_mod.retrieve,
            self.index, text,
            uid=uid, limit=max(1, int(limit or 20)),
            vectors=self.vectors, embedder=self.embedder,
            expansions=expansions,
            use_vector=True, use_mmr=True, boost=True,
            expand_context=True, candidate_multiplier=2.0,
            anchor_count=5)
        return hits[:max(1, int(limit or 20))]

    def vectorize_recent(self, limit: int = 400, uid: str = "default") -> int:
        """给最近的事件补向量。纯本地计算，几十毫秒，扔后台跑。"""
        try:
            events = self.vault.recent_events(limit, uid)
            if not events:
                return 0
            fingerprints = [str(e.get("fp") or "") for e in events]
            known = self.vectors.has_many(fingerprints, uid)
            todo = [e for e in events
                    if str(e.get("fp") or "") and str(e["fp"]) not in known]
            if not todo:
                return 0
            payload = [(str(e["fp"]), self.embedder.vector(e.get("text", "")))
                       for e in todo]
            return self.vectors.put(payload, uid=uid)
        except Exception as exc:
            logger.debug(f"[all_memory] 向量化失败: {exc}")
            return 0

    def vectorize_missing(self, batch: int = 400, uid: str = "default") -> int:
        """
        从最早的一天开始补，补满 batch 条就返回，下次接着来。
        """
        written = 0
        payload: list[tuple[str, list[float]]] = []
        try:
            for day in self.vault.days(uid):
                if written >= batch:
                    break
                events = self.vault.read_day(day, uid)
                if not events:
                    continue
                fingerprints = [str(e.get("fp") or "") for e in events]
                known = self.vectors.has_many(fingerprints, uid)
                for event in events:
                    fp = str(event.get("fp") or "")
                    if not fp or fp in known:
                        continue
                    payload.append((fp, self.embedder.vector(event.get("text", ""))))
                    written += 1
                    if written >= batch:
                        break
        except Exception as exc:
            logger.debug(f"[all_memory] 向量补齐失败: {exc}")
        if payload:
            try:
                self.vectors.put(payload, uid=uid)
            except Exception as exc:
                logger.debug(f"[all_memory] 向量写入失败: {exc}")
                return 0
        return written

    async def _vector_backfill(self, uid: str) -> None:
        """启动后把这个 id 的历史补齐。补完就退出；增量由每轮结束负责。"""
        while True:
            try:
                done = await asyncio.to_thread(self.vectorize_missing, 400, uid)
            except Exception:
                return
            if not done:
                return
            logger.info(f"[all_memory] 已补 {done} 条向量，继续…")
            await asyncio.sleep(0)

    def _ensure_vector_backfill(self, uid: str) -> None:
        """第一次用到检索时把补齐任务拉起来，每个 id 只拉一次。"""
        if uid in self._vector_started:
            return
        try:
            asyncio.create_task(self._vector_backfill(uid))
            self._vector_started.add(uid)
        except RuntimeError:
            pass

    def _ensure_media_backfill(self, uid: str) -> None:
        """附件库是空的时候，自动从宿主库补一次历史图片。"""
        if uid in self._media_backfill_started:
            return
        try:
            asyncio.create_task(self._media_backfill(uid))
            self._media_backfill_started.add(uid)
        except RuntimeError:
            pass

    async def _media_backfill(self, uid: str) -> None:
        """
        只在附件库为空时跑：旧版本的归档是坏的，库里一张图都没有，
        而宿主库里图是 base64 内嵌的，补得回来。补过就不再动。
        """
        try:
            store = self.media_for(uid)
            if os.path.isdir(store.dir) and any(
                    n.endswith(".jsonl") for n in os.listdir(store.dir)):
                return
            path = self._find_astrbot_db()
            if not path:
                return
            result = await asyncio.to_thread(
                importer.backfill_media_from_astrbot, path, store,
                ids=self.ids)
            if result.get("saved"):
                logger.info(
                    f"[all_memory] 历史附件回填 {result['saved']} 张"
                    f"（{round(result['bytes'] / 1024, 1)} KB，"
                    f"扫描 {result['images']} 张）")
            elif result.get("images"):
                logger.info(f"[all_memory] 历史附件已齐（扫描 {result['images']} 张）")
        except Exception as exc:
            logger.debug(f"[all_memory] 附件回填失败: {exc}")

    def _expand_hits(self, hits: list[dict], uid: str = "default",
                     span: int = 1, max_hits: int = 3) -> list[dict]:
        """
        给最相关的前几条命中补前后文。

        检索本身返回的是一条条孤立消息；问“那天几点回来”时，只有
        “我回来了”不够，前面“我出门了”才是上下文。这里按窗口合并、
        去重，命中项标 is_hit，邻居标 is_context。
        """
        if not hits:
            return []
        span = max(0, min(int(span or 0), 3))
        max_hits = max(0, min(int(max_hits or 0), 8))
        out: list[dict] = []
        seen: set[str] = set()
        for rank, hit in enumerate(hits):
            fp = str(hit.get("fp") or "")
            if not fp:
                continue
            if rank >= max_hits or span == 0:
                if fp not in seen:
                    seen.add(fp)
                    out.append(dict(hit))
                continue
            window = [dict(hit)]
            try:
                for item in self.index.neighbors(fp, span=span, uid=uid):
                    neighbor = dict(item)
                    neighbor["is_context"] = True
                    neighbor["is_hit"] = False
                    window.append(neighbor)
            except Exception:
                pass
            window.sort(key=lambda item: str(item.get("ts") or ""))
            for item in window:
                item_fp = str(item.get("fp") or "")
                if not item_fp or item_fp in seen:
                    continue
                seen.add(item_fp)
                item.setdefault("is_hit", item_fp == fp)
                out.append(item)
        return out

    def _earliest_hits(self, hits: list[dict], limit: int = 8,
                       uid: str = "default") -> list[dict]:
        """
        把最早那天的开头几条排到召回最前面。

        问"第一次见面说了什么"时，检索词可能完全命不中（原句里未必有"第一次"），
        所以这条不靠模型自觉：只要识别出意图，证据直接递到眼前。
        """
        try:
            day = self.index.earliest_day(uid)
            if not day:
                return hits
            seeds = self.index.day_messages(day, limit=limit, uid=uid)
            if not seeds:
                return hits
            seen = {str(hit.get("fp")) for hit in hits}
            front = []
            for seed in seeds:
                fingerprint = str(seed.get("fp") or "")
                if not fingerprint or fingerprint in seen:
                    continue
                front.append({"fp": fingerprint, "ts": seed.get("ts", ""),
                              "role": seed.get("role", ""),
                              "text": seed.get("text", ""), "score": 1.0})
                seen.add(fingerprint)
            return front + hits
        except Exception as exc:
            logger.debug(f"[all_memory] 最早记忆补位失败: {exc}")
            return hits

    def _day_hits(self, hits: list[dict], day: str, limit: int = 20,
                  uid: str = "default") -> list[dict]:
        """
        把某一天的原话排到召回最前面。

        问"那天几点出门、几点回来"时，原文可能写成"中午对付了两口"，
        检索词命中不了；既然日期是明确的，就把那天的对话直接递上去。

        按**均匀取样**而不是只取开头结尾：一天里的关键节点可能在中间，
        只取两头会漏掉"下午两点我回来了"这种。
        """
        try:
            items = self.index.day_messages(day, limit=200, uid=uid)
            if not items:
                return hits

            if len(items) > limit:
                step = max(1, len(items) // limit)
                picked = items[::step][:max(1, limit - 1)]
                # 结尾那条一定要有：一天的最后一句常常是"我回来了"
                picked.append(items[-1])
            else:
                picked = items

            seen = {str(hit.get("fp")) for hit in hits}
            front = []
            for item in picked:
                fingerprint = str(item.get("fp") or "")
                if not fingerprint or fingerprint in seen:
                    continue
                front.append({"fp": fingerprint, "ts": item.get("ts", ""),
                              "role": item.get("role", ""),
                              "text": item.get("text", ""), "score": 1.0})
                seen.add(fingerprint)
            return front + hits
        except Exception as exc:
            logger.debug(f"[all_memory] 按天补位失败: {exc}")
            return hits

    def _flush_failed(self, uid: str) -> int:
        """
        把之前归档失败的内容补写进去。

        放在每条请求的最前面：补的是旧轮次，先写进去，顺序才不会乱。
        写成功才删文件，写不进去就留着下次再试。
        """
        written = 0
        queue = self.failed_for(uid)
        try:
            for path, data in queue.pending(limit=3):
                items = data.get("items") or []
                parsed = [
                    {"idx": i, "role": str(item.get("role") or "user"),
                     "content": str(item.get("content") or ""), "raw": None}
                    for i, item in enumerate(items)
                    if str(item.get("content") or "").strip()
                ]
                if not parsed:
                    queue.discard(path)
                    continue
                stats = sync.sync(self.vault, self.index, parsed,
                                  str(data.get("persona") or ""),
                                  session=str(data.get("session") or ""),
                                  origin="retry", uid=uid)
                queue.discard(path)
                written += int(stats.get("new") or 0)
            if written:
                logger.info(f"[all_memory] 重试队列补写 {written} 条")
        except Exception as exc:
            logger.warning(f"[all_memory] 补写重试队列失败: {exc}")
        return written

    def _capture_turn_media(self, event, parsed: list[dict], session: str,
                            uid: str) -> None:
        """
        把这一轮用户消息里的图片/语音落盘，键 = 那条消息的指纹。

        **必须在一轮结束时做**：请求钩子拿到的解析结果末尾是上一条历史消息
        （contexts 只有历史），按它做键会把附件挂到别人身上——0.6.0 之前
        那段代码就是这么死的（守卫恒假，一张图都没存下来）。
        """
        try:
            sources = media_mod.collect(event)
            if not sources:
                return
            target = next((m for m in parsed if m.get("role") == "user"), None)
            if target is None:
                return
            msg_fp = vault_mod.fp_of(f"user\x00{target['content']}")
            task = asyncio.create_task(
                self.media_for(uid).ingest(msg_fp, session, sources))
            task.add_done_callback(_log_task_error)
        except Exception as exc:
            logger.debug(f"[all_memory] 附件归档失败: {exc}")

    @staticmethod
    def _message_time(event):
        """
        取这条消息的真实发生时间。

        平台给的是 unix 时间戳，有的是秒有的是毫秒，两种都认。
        拿不到就返回 None，由归档层退回「现在」。
        """
        raw = getattr(getattr(event, "message_obj", None), "timestamp", None)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        if value > 1e11:          # 毫秒
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, TZ)
        except (OSError, OverflowError, ValueError):
            return None

    def _session_of(self, event) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value:
                return value
        return "default"

    @staticmethod
    def _sender_of(event) -> str:
        """
        这条消息是谁发的（数字 ID）。

        历史补录拿不到，只有"这一轮新鲜收到的消息"有。所以事实的 subject
        要从今往后才有；老数据留空，按出处会话兜底。
        """
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = None
            if value:
                return str(value).strip()

        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        for attr in ("user_id", "sender_id", "id"):
            value = getattr(sender, attr, None)
            if value:
                return str(value).strip()
        return ""

    @staticmethod
    def _sender_name_of(event) -> str:
        """软件读不到 ID 时，用账户名称当文件夹名。"""
        for getter in ("get_sender_name", "get_sender_nickname"):
            fn = getattr(event, getter, None)
            if callable(fn):
                try:
                    value = fn()
                except Exception:
                    value = None
                if value:
                    return str(value).strip()

        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        for attr in ("nickname", "card", "name", "display_name", "user_name"):
            value = getattr(sender, attr, None)
            if value:
                return str(value).strip()
        return ""

    def _active_facts(self, uid: str = "default", query: str = "",
                      extra_subjects=None, budget: int = 0) -> list[dict]:
        """
        只取这个 id 的人物卡，按「身份 > 关系 > 偏好 > 其他 > 事件」排序，
        同权重内新的在前；给了 budget 时按核心/事件分层选择。
        """
        facts = list(self.facts_for(uid).latest().values())
        if query:
            aliases = self.entities_for(uid).expand(query, limit=8)
            aliases += self.synonyms_for(uid).expand(query, limit=8)
            terms = query_mod.build_terms(query, aliases)
            facts = search_mod.rank_facts(facts, terms)
        else:
            facts.sort(key=lambda f: (extract.kind_weight(f.get("kind")),
                                      str(f.get("extracted_at", ""))),
                       reverse=True)
        if budget > 0:
            return inject.select_facts(facts, budget)
        return facts

    def _visible_facts(self, facts: list[dict], session: str,
                       extra_subjects=None) -> list[dict]:
        """把"谁能看见"需要的两个索引一次性查出来，别在循环里查库。"""
        fingerprints: list[str] = []
        for fact in facts:
            fingerprints.extend(fact.get("source_fp") or [])
        try:
            source_session = self.index.sessions_of(fingerprints)
        except Exception:
            source_session = {}
        try:
            subjects = set(self.index.subjects(session) or ())
        except Exception:
            subjects = set()
        # 本轮说话的人可能还没写进索引（新会话第一条），先算进来；
        # 否则「关于我」的 people 事实会因为“本会话没这个人”被静默过滤。
        for value in extra_subjects or ():
            value = str(value or "").strip()
            if value:
                subjects.add(value)
        # 提炼时会给历史消息补一个推算的 sender，这里必须同步认它，
        # 否则事实带着这个 ID，却因为"本会话没这个人"被过滤掉——静默丢记忆。
        guess = sync.infer_sender(session, subjects)
        if guess:
            subjects.add(guess)
        return extract.visible_facts(facts, session, subjects, source_session)

    def _block_budget(self, window: int, fraction: float, calibrator) -> int:
        ratio = getattr(calibrator, "ratio", 1.0) or 1.0
        return budget.block_budget(window, float(fraction), ratio)

    # ------------------------------------------------------------ 模型工具

    @filter.llm_tool(name="search_memory")
    async def tool_memory_search(self, event: AstrMessageEvent, query: str,
                                 limit: int = 20) -> str:
        """在长期记忆里全文检索过去的聊天原文。

        当觉得「这件事以前聊过」「需要引用对方之前说过的话」，或者
        memory_timeline 提到某个日期但想不起细节时，用本工具查找。
        检索范围是整个记忆库，包含已经从上下文里被裁掉的早期对话。

        Args:
            query(string): 搜索词。用对方原话里的关键词效果最好
            limit(int): 最多返回几条，默认 20，最多 60
        """
        try:
            query = (query or "").strip()
            if len(query) < 2:
                return "检索词太短，请换一个更具体的词。"
            limit = max(1, min(int(limit or 20), 60))
            hits = await self.retrieve(
                query, uid=self._uid(event), limit=limit,
                use_llm=bool(self._cfg("recall_expand")))
            if not hits:
                return (f"没有找到与「{query}」相关的记忆。"
                        "可以换个说法，或用 memory_timeline 看时间线。")
            lines = [f"找到 {len(hits)} 条（内容截断，引用前用 read_memory 读全文）："]
            for hit in hits:
                text = " ".join(str(hit["text"]).split())[:80]
                lines.append(f"- [{hit['fp']}] {hit['ts']} {hit['role']}：{text}")
            return "\n".join(lines)
        except Exception as exc:
            return f"检索失败：{exc}"

    @filter.llm_tool(name="read_memory")
    async def tool_memory_read(self, event: AstrMessageEvent, id: str = "",
                               span: int = 2, day: str = "",
                               offset: int = 0) -> str:
        """读取一条记忆的完整原文，带前后几条；也可以按天顺序读一整天。

        search_memory 返回的内容是截断的。要准确引用对方说过的原话、
        或想知道一段对话的前因后果时，用本工具读取。

        知道是哪天但不知道编号时（比如"那天我几点跑步、几点回来"），
        传 day（YYYY-MM-DD，从 memory_timeline 拿）会按时间顺序读出那天的对话。
        一天超过 40 条时，把返回末尾的 offset 传回来继续读下一页。

        Args:
            id(string): search_memory 结果里方括号中的编号；只给 day 时可以留空
            span(int): 前后各带几条上下文，默认 2，最多 10
            day(string): 日期 YYYY-MM-DD；给了就按天读，忽略 id
            offset(int): 按天读的起始条数，翻页用，默认 0
        """
        try:
            uid = self._uid(event)
            day = (day or "").strip()
            if day:
                start = max(0, int(offset or 0))
                items = self.index.day_messages(day, limit=40, offset=start,
                                                uid=uid)
                if not items:
                    if start:
                        return f"{day} 第 {start} 条之后没有更多了。"
                    return (f"{day} 没有记录。先用 memory_timeline 看有哪几天，"
                            "或用 search_memory 搜关键词。")
                total = self.index.day_count(day, uid)
                lines = [f"[{item['ts']}] {item['role']}：{item['text']}"
                         for item in items]
                tail = ""
                if start + len(items) < total:
                    tail = (f"\n（共 {total} 条，已显示 {start + 1}–{start + len(items)}；"
                            f"要看后面用 offset={start + len(items)}）")
                return (f"{day} 共 {total} 条（按时间顺序）：\n"
                        + "\n".join(lines) + tail)

            memory_id = (id or "").strip().strip("[]")
            if not memory_id:
                return "要读取就给我 id（search_memory 结果的编号）或 day（YYYY-MM-DD）。"
            target = self.index.get(memory_id, uid)
            if not target:
                return f"没有找到编号为 {memory_id} 的记忆，编号可能已经过期。"

            lines = []
            for item in self.index.neighbors(memory_id, span=span, uid=uid):
                lines.append(f"[{item['ts']}] {item['role']}：{item['text']}")

            marker = f"【原文】{target['ts']} {target['role']}：\n{target['text']}"
            if lines:
                return ("前后文：\n" + "\n".join(lines) +
                        "\n\n" + marker +
                        f"\n\n（来源：{target.get('origin', '')}）")
            return marker
        except Exception as exc:
            return f"读取失败：{exc}"

    @filter.llm_tool(name="memory_timeline")
    async def tool_memory_timeline(self, event: AstrMessageEvent,
                                   detail_days: int = 365) -> str:
        """查看记忆的时间线：从第一天到现在有哪些天记录、各多少条。

        想不起来某件事发生的日期，或者想确认「某段时间我们聊过什么」时，
        先用本工具定位日期，再用 search_memory / read_memory 取细节。

        Args:
            detail_days(int): 最近多少天按天列出，更早的按月汇总，默认 30
        """
        try:
            rows = self.index.day_summary(days=None, uid=self._uid(event))
            if not rows:
                return "记忆库里还没有任何记录。"
            block = inject.build_map_block(
                rows, budget=4000,
                detail_days=max(1, min(int(detail_days or 30), 3650)))
            return block or "记忆地图生成失败。"
        except Exception as exc:
            return f"读取失败：{exc}"

    # ------------------------------------------------------------ 指令

    @filter.command("allmem")
    async def cmd_status(self, event: AstrMessageEvent):
        try:
            uid = self._uid(event)
            stats = self.vault.stats(uid)
            facts = len(self.facts_for(uid).all_facts())
            digest = self.summaries_for(uid).load()
            digest_chars = len(str(digest.get("text") or ""))
            state = self.states.load(uid)
            plan = state.get("last_plan") or {}
            queued = self.failed_for(uid).count()
            actual_tokens = (int(state.get("last_prompt_tokens", 0) or 0)
                             or self._db_token_usage(self._session_of(event)))
            window = self._window()
            lines = [
                f"原始层：{stats['events']} 条 / {stats['days']} 天",
                f"提炼层：{facts} 条事实",
                f"前情纪要：{digest_chars} 字"
                + (f"（覆盖到 {digest.get('covered_until', '')[:16]}）"
                   if digest_chars else "（还没有）"),
                f"索引层：{self.index.count(uid)} 条",
                f"向量层：{self.vectors.count(uid)} 条（本地哈希）",
                f"窗口：{window or '未配置'}",
                (f"真实注入：{actual_tokens} tok"
                 + (f"（{actual_tokens / window * 100:.1f}%）" if window else "")
                 if actual_tokens else
                 "真实注入：还没读到（本轮会按字符估算）"),
                "全局接管：" + {
                    "on": "开",
                    "off": "关",
                    "auto": "关（首次裁剪自动开）",
                }.get(self.global_takeover_state().get("mode"), "?"),
                f"上次：{plan.get('pressure', 0) * 100:.1f}% "
                f"{'已裁剪' if plan.get('applied') else '未触发'}",
            ]
            if queued:
                lines.append(f"待补写：{queued} 轮（下条消息会自动补）")
            lines.append(f"目录：{self.root}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"读取失败: {exc}")

    @filter.command("allmem_recall")
    async def cmd_recall(self, event: AstrMessageEvent):
        """看上一轮检索到底贡献了什么。"""
        try:
            state = self.states.load(self._uid(event))
            trace = state.get("last_recall") or {}
            if not trace:
                yield event.plain_result(
                    "还没有检索记录；先聊一轮，且要处于接管模式。")
                return
            lines = [
                f"时间：{trace.get('at', '')}",
                f"问题：{trace.get('query', '')}",
                f"候选：{trace.get('candidates', 0)}",
                f"已在本轮上下文（剔除）：{trace.get('known', 0)}",
                f"前后文新增：{trace.get('expanded', 0)}",
                f"最终注入：{trace.get('final', 0)} 条 / "
                f"{trace.get('chars', 0)} 字",
            ]
            fps = [x for x in (trace.get("fps") or []) if x]
            if fps:
                lines.append("最终指纹：" + "、".join(fps))
            if not trace.get("final"):
                lines.append("结论：这轮没有新增检索内容，回答主要来自上下文。")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"读取失败: {exc}")

    @filter.command("allmem_verify")
    async def cmd_verify(self, event: AstrMessageEvent):
        """
        超集验收：原始层 ⊇ AstrBot 存档 ∪ memory_beyond 目录。

        判据必须落在**来源里有什么**上：把来源扫一遍（dry_run，不写盘），
        算出「本该进库」的指纹，再看索引里有没有。
        拿索引自己的分项跟自己的合计比是恒真的 —— 那样永远打印达标，验不出东西。
        """
        try:
            vault_stats = self.vault.stats()
            total = self.index.count()
            diff = total - vault_stats["events"]

            lines = [
                f"原始层：{vault_stats['events']} 条 / {vault_stats['days']} 天",
                f"索引层：{total} 条"
                + ("" if diff == 0 else
                   f"（⚠️ 与原始层差 {abs(diff)} 条，建议 /allmem_rebuild）"),
            ]

            by_origin = self.index.count_by_origin()
            if by_origin:
                items = sorted(by_origin.items(), key=lambda kv: -kv[1])
                lines.append("来源：" + " / ".join(f"{k} {v}" for k, v in items))

            sources = (
                ("AstrBot 数据库", self._find_astrbot_db(),
                 lambda path: importer.import_astrbot(
                     path, self.vault, self.index, dry_run=True)),
                ("memory_beyond", self._find_beyond_dir(),
                 lambda path: importer.import_beyond(
                     path, self.vault, self.index, dry_run=True)),
            )

            checked = 0
            gaps = 0
            for label, path, scan in sources:
                # 没配、没找到、或者目录是空的（装过没用过的插件留下的），
                # 都不算"来源"，直接跳过——别每次验收都刷一行噪音
                if not path or not os.path.exists(path):
                    continue

                stats = await asyncio.to_thread(scan, path)
                fps = list(stats.get("fps") or [])
                if not fps:
                    continue

                missing = importer.missing_fps(self.index, fps)
                checked += 1
                gaps += missing

                line = f"{label}：来源里应有 {len(fps)} 条，缺 {missing} 条"
                if missing:
                    line += " → 跑 /allmem_import 补齐"
                lines.append(line)
                for err in (stats.get("errors") or [])[:2]:
                    lines.append(f"  （扫描提示：{err}）")

            if not checked:
                lines.append("结论：没有找到有内容的来源（AstrBot 数据库 / memory_beyond），"
                             "无法判定超集 —— 这不等于通过")
            elif gaps:
                lines.append(f"⚠️ 有缺口：来源里还有 {gaps} 条没进库")
            else:
                lines.append("✅ 超集成立：找到的来源全部在库里")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"验收失败: {exc}")

    @filter.command("allmem_manifest")
    async def cmd_manifest(self, event: AstrMessageEvent):
        """给当前数据目录生成/刷新 manifest.json + README_M.md。"""
        try:
            meta = self._manifest_meta()
            data = await asyncio.to_thread(
                manifest_mod.write, self.root,
                self._plugin_version(), meta["models"],
                meta["prompt_versions"])
            counts = data.get("counts") or {}
            yield event.plain_result(
                "manifest 已写入：\n"
                f"{os.path.join(self.root, manifest_mod.MANIFEST_NAME)}\n"
                f"事件 {counts.get('events', 0)} / 事实 {counts.get('facts', 0)} / "
                f"纪要 {counts.get('summaries', 0)} / 文件 {counts.get('files', 0)}")
        except Exception as exc:
            yield event.plain_result(f"生成失败: {exc}")

    @filter.command("allmem_verify_m")
    async def cmd_verify_m(self, event: AstrMessageEvent, target: str = ""):
        """校验当前目录或导出 zip 里的 all_memory/。"""
        try:
            target = (target or "").strip().strip('"')
            if target and os.path.isfile(target):
                result = await asyncio.to_thread(
                    manifest_mod.verify_zip, target)
                label = os.path.basename(target)
            elif target and os.path.isdir(target):
                result = await asyncio.to_thread(
                    manifest_mod.verify, target, False)
                label = target
            else:
                result = await asyncio.to_thread(
                    manifest_mod.verify, self.root, False)
                label = self.root

            counts = result.get("counts") or {}
            lines = [
                f"记忆体校验：{'✅ 通过' if result.get('ok') else '❌ 失败'}",
                f"位置：{label}",
                f"schema：{result.get('schema') or '?'}",
                f"事件 {counts.get('events', 0)} / 事实 {counts.get('facts', 0)} / "
                f"纪要 {counts.get('summaries', 0)} / 附件 {counts.get('blobs', 0)}",
                f"校验文件 {result.get('checked_files', 0)} 个",
            ]
            errors = result.get("errors") or []
            warnings = result.get("warnings") or []
            if errors:
                lines.append(f"错误 {result.get('error_count', len(errors))} 条：")
                lines.extend(f"- {item}" for item in errors[:8])
            if warnings:
                lines.append(f"警告 {result.get('warning_count', len(warnings))} 条：")
                lines.extend(f"- {item}" for item in warnings[:5])
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"校验失败: {exc}")

    # ------------------------------------------------------------ 来源查找

    def _find_astrbot_db(self) -> str:
        """
        找 AstrBot 的会话数据库。

        以前要在配置里手填路径；现在直接去数据目录里找 data_v*.db，
        取最新改动的那个（AstrBot 从 v3 升 v4 会换文件名）。
        """
        configured = str(self._cfg("astrbot_db") or "").strip()
        if configured and os.path.exists(configured):
            return configured

        root = self.astrbot_data_root()
        roots = []
        # 插件数据目录就是 <data>/all_memory；宿主 DB 在它的父目录。
        # 这个位置最确定，优先于可能指错的 get_astrbot_data_path()。
        parent = os.path.dirname(os.path.abspath(self.root))
        if parent:
            roots.append(parent)
        if root and root not in roots:
            roots.append(root)
        for base in roots:
            try:
                names = [n for n in os.listdir(base)
                         if n.startswith("data_v") and n.endswith(".db")]
            except OSError:
                continue
            if not names:
                continue
            paths = [os.path.join(base, n) for n in names]
            paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            return paths[0]
        return configured

    def _db_token_usage(self, session: str) -> int:
        """
        直接读 AstrBot conversations.token_usage。

        这是宿主上一次请求真正注入的 token 总量（含工具定义），比字符估算准。
        缓存 10 秒，避免每一轮都开一次 SQLite。
        """
        key = str(session or "")
        now = time.monotonic()
        cached = self._token_usage_cache.get(key)
        if cached and now - cached[0] < 10:
            return cached[1]

        path = self._find_astrbot_db()
        if not path or not os.path.isfile(path):
            return 0
        value = 0
        try:
            # mode=ro：绝不写宿主库，也不触发建库
            uri = "file:" + path.replace("\\", "/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            try:
                row = conn.execute(
                    "SELECT token_usage FROM conversations "
                    "WHERE user_id=? ORDER BY updated_at DESC LIMIT 1",
                    (key,)).fetchone()
                if not row:
                    # 某些平台会给 user_id 加前缀/别名，按尾号再兜一次
                    tail = key.split(":")[-1]
                    if tail and tail != key:
                        row = conn.execute(
                            "SELECT token_usage FROM conversations "
                            "WHERE user_id LIKE ? ORDER BY updated_at DESC LIMIT 1",
                            (f"%{tail}",)).fetchone()
                if row and row[0]:
                    value = int(row[0])
                if not value:
                    # 最后兜底：宿主如果只保留一条活动对话，直接取最新那条。
                    row = conn.execute(
                        "SELECT token_usage FROM conversations "
                        "ORDER BY updated_at DESC LIMIT 1").fetchone()
                    if row and row[0]:
                        value = int(row[0])
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError):
            value = 0
        if not value and key not in self._token_debugged:
            self._token_debugged.add(key)
            logger.warning(
                f"[all_memory] 读不到宿主 token：db={path or '空'} "
                f"session={key!r} exists={bool(path and os.path.isfile(path))}")
        self._token_usage_cache[key] = (now, value)
        return value

    def _find_beyond_dir(self) -> str:
        """找 memory_beyond 的数据目录（用来做超集校验）。"""
        configured = str(self._cfg("beyond_dir") or "").strip()
        if configured and os.path.isdir(configured):
            return configured
        root = self.astrbot_data_root()
        if not root:
            return configured
        guess = os.path.join(root, "plugin_data", "astrbot_plugin_memory_beyond")
        return guess if os.path.isdir(guess) else configured

    @filter.command("allmem_import")
    async def cmd_import(self, event: AstrMessageEvent, target: str = ""):
        try:
            target = (target or "").strip()
            if not target:
                yield event.plain_result(
                    "用法：/allmem_import <路径>\n"
                    "· AstrBot 数据库：.../data/data_v4.db（旧版是 data_v3.db）\n"
                    "· memory_beyond 目录：.../plugin_data/astrbot_plugin_memory_beyond")
                return

            path = os.path.abspath(target)
            if not os.path.exists(path):
                yield event.plain_result(f"路径不存在：{path}")
                return

            uid = self._uid(event)
            session = self._session_of(event)
            if os.path.isdir(path):
                stats = importer.import_beyond(path, self.vault, self.index,
                                               uid=uid)
                yield event.plain_result(
                    f"导入 memory_beyond（id {uid}）：\n"
                    f"扫描文件 {stats['files']} 个\n"
                    f"新增事件 {stats['events']} 条\n"
                    + (f"错误 {len(stats['errors'])} 个"
                       if stats["errors"] else "无错误"))
            else:
                stats = importer.import_astrbot(path, self.vault, self.index,
                                                session=session, uid=uid)
                yield event.plain_result(
                    f"导入 AstrBot 数据库（id {uid}）：\n"
                    f"扫描表 {stats['tables']} 个\n"
                    f"新增事件 {stats['events']} 条\n"
                    f"跳过表 {len(stats['skipped_tables'])} 个\n"
                    + (f"错误 {len(stats['errors'])} 个"
                       if stats["errors"] else "无错误"))
        except Exception as exc:
            yield event.plain_result(f"导入失败: {exc}")

    @filter.command("allmem_search")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        try:
            keyword = (keyword or "").strip()
            if not keyword:
                yield event.plain_result("用法：/allmem_search 关键词")
                return
            hits = await self.retrieve(
                keyword, uid=self._uid(event), limit=8,
                use_llm=bool(self._cfg("recall_expand")))
            if not hits:
                yield event.plain_result(f"没有找到「{keyword}」")
                return
            lines = [f"「{keyword}」命中 {len(hits)} 条："]
            for hit in hits:
                text = " ".join(str(hit["text"]).split())[:60]
                lines.append(f"- [{hit['ts']}] {hit['role']}：{text}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"检索失败: {exc}")

    @filter.command("allmem_facts")
    async def cmd_facts(self, event: AstrMessageEvent, keyword: str = ""):
        try:
            latest = list(self.facts_for(self._uid(event)).latest().values())
            keyword = (keyword or "").strip()
            if keyword:
                latest = [f for f in latest if keyword in str(f.get("claim", ""))]
            if not latest:
                yield event.plain_result("还没有提炼出任何事实")
                return
            latest.sort(key=lambda f: str(f.get("extracted_at", "")), reverse=True)
            lines = [f"共 {len(latest)} 条长期事实："]
            for fact in latest[:15]:
                kind = fact.get("kind", "fact")
                lines.append(f"- [{kind}] {fact.get('claim', '')}  ({fact.get('id', '')})")
            if len(latest) > 15:
                lines.append(f"…还有 {len(latest) - 15} 条")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"读取失败: {exc}")

    @filter.command("allmem_entities")
    async def cmd_entities(self, event: AstrMessageEvent,
                           action: str = "", value: str = ""):
        """实体/别名词典：不带参数列出；add/remove/suggest。"""
        try:
            uid = self._uid(event)
            store = self.entities_for(uid)
            action = (action or "").strip().lower()
            value = (value or "").strip()

            if action == "suggest":
                facts = list(self.facts_for(uid).latest().values())
                names = EntityStore.suggest(facts)
                if not names:
                    yield event.plain_result("没有从事实里抽出候选实体")
                    return
                yield event.plain_result(
                    "候选实体（只是建议，未写入）：\n- " + "\n- ".join(names)
                    + "\n\n添加：/allmem_entities add 规范名 别名1,别名2")
                return

            if action == "add":
                head = value.replace("，", ",").split(None, 1)
                if not head:
                    yield event.plain_result(
                        "用法：/allmem_entities add 规范名 别名1,别名2")
                    return
                canonical = head[0]
                aliases = []
                if len(head) > 1:
                    aliases = [x.strip() for x in
                               head[1].replace("、", ",").split(",")
                               if x.strip()]
                item = store.add(canonical, aliases)
                yield event.plain_result(
                    f"已添加：{item['canonical']}（{item['type']}）"
                    + (f" 别名：{'、'.join(item['aliases'])}"
                       if item["aliases"] else ""))
                return

            if action == "remove":
                if not value:
                    yield event.plain_result("用法：/allmem_entities remove 规范名")
                    return
                ok = store.remove(value)
                yield event.plain_result("已删除" if ok else "没有这个实体")
                return

            items = store.all()
            if not items:
                yield event.plain_result(
                    "还没有实体词典。\n"
                    "添加：/allmem_entities add 规范名 别名1,别名2\n"
                    "候选：/allmem_entities suggest")
                return
            lines = ["实体词典："]
            for item in items[:40]:
                aliases = "、".join(item.get("aliases") or [])
                lines.append(f"- {item['canonical']}（{item.get('type', '')}）"
                             + (f"：{aliases}" if aliases else ""))
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"实体词典操作失败: {exc}")

    @filter.command("allmem_synonyms")
    async def cmd_synonyms(self, event: AstrMessageEvent,
                           action: str = "", value: str = ""):
        """同义词分组：不带参数列出；add/remove。"""
        try:
            store = self.synonyms_for(self._uid(event))
            action = (action or "").strip().lower()
            value = (value or "").strip()
            if action == "add":
                head = value.replace("，", ",").split(None, 1)
                if not head:
                    yield event.plain_result(
                        "用法：/allmem_synonyms add 主词 同义词1,同义词2")
                    return
                terms = []
                if len(head) > 1:
                    terms = [x.strip() for x in
                             head[1].replace("、", ",").split(",")
                             if x.strip()]
                item = store.add(head[0], terms)
                yield event.plain_result(
                    f"已添加：{item['canonical']} → "
                    f"{'、'.join(item['terms']) or '（无别名）'}")
                return
            if action == "remove":
                if not value:
                    yield event.plain_result(
                        "用法：/allmem_synonyms remove 主词")
                    return
                ok = store.remove(value)
                yield event.plain_result("已删除" if ok else "没有这个同义词组")
                return
            groups = store.all()
            if not groups:
                yield event.plain_result("还没有同义词组")
                return
            lines = ["同义词组："]
            for item in groups[:50]:
                lines.append(
                    f"- {item['canonical']}："
                    + ("、".join(item.get("terms") or []) or "（无）"))
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"同义词操作失败: {exc}")

    @filter.command("allmem_show")
    async def cmd_show(self, event: AstrMessageEvent, fact_id: str = ""):
        try:
            fact = self.facts_for(self._uid(event)).load((fact_id or "").strip())
            if not fact:
                yield event.plain_result("没有这条事实")
                return
            lines = [f"{fact['id']}（{len(fact.get('versions', []))} 个版本）"]
            for ver in fact.get("versions", []):
                lines.append(f"\nv{ver.get('version')}：{ver.get('claim')}")
                lines.append(f"  来源：{', '.join(ver.get('source_fp') or []) or '无'}")
                lines.append(f"  时间：{ver.get('extracted_at', '')}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"读取失败: {exc}")

    @filter.command("allmem_reextract")
    async def cmd_reextract(self, event: AstrMessageEvent):
        """手动全量重提炼：扫当前 id 的全部历史，归档旧版后整体替换。"""
        try:
            uid = self._uid(event)
            info = self.start_refine(uid, reason="manual", replace=True)
            if not info.get("ok"):
                yield event.plain_result(info.get("error") or "启动失败")
                return
            yield event.plain_result(
                f"已开始全量重提炼：预算约 {info['facts_budget']} tok"
                f"（{info['facts_ratio']:.0f}%），旧版会先归档。\n"
                f"跑完用 /allmem_facts 查看。")
        except Exception as exc:
            yield event.plain_result(f"启动失败: {exc}")

    @filter.command("allmem_repair")
    async def cmd_repair(self, event: AstrMessageEvent):
        """清理旧纪要的重复/占位、合并近义事实（只动派生层）。"""
        try:
            uid = self._uid(event)
            summary_info = await asyncio.to_thread(
                self.summaries_for(uid).repair)
            facts_info = await asyncio.to_thread(self.facts_for(uid).repair)
            lines = []
            if summary_info.get("ok"):
                lines.append(
                    f"纪要：{summary_info.get('before', 0)} → "
                    f"{summary_info.get('after', 0)} 字，"
                    f"清掉 {summary_info.get('removed', 0)} 字")
            else:
                lines.append(f"纪要：{summary_info.get('error') or '未修改'}")
            if facts_info.get("ok"):
                lines.append(
                    f"事实：{facts_info.get('facts', 0)} 条，"
                    f"合并 {facts_info.get('merged', 0)} 条；"
                    f"保留最早的一条，其余标记 deleted")
            else:
                lines.append(f"事实：{facts_info.get('error') or '未修改'}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"修复失败: {exc}")

    @filter.command("allmem_summary")
    async def cmd_summary(self, event: AstrMessageEvent):
        """手动生成/更新前情纪要：滚动合并，只处理还没纪要过的部分。"""
        try:
            uid = self._uid(event)
            info = self.start_summary(uid, reason="manual")
            if not info.get("ok"):
                yield event.plain_result(info.get("error") or "启动失败")
                return
            yield event.plain_result(
                f"{info['message']}。\n"
                f"跑完去面板点「纪要」查看，或 /allmem 看字数。")
        except Exception as exc:
            yield event.plain_result(f"启动失败: {exc}")

    @filter.command("allmem_restore")
    async def cmd_restore(self, event: AstrMessageEvent, path: str = ""):
        """从本插件导出的 zip 合并恢复：只增不删，已有的跳过。"""
        try:
            target = (path or "").strip().strip('"')
            if not target:
                yield event.plain_result(
                    "用法：/allmem_restore <备份包路径>\n"
                    "只合并、不覆盖；包里的 astrbot_data/ 不会还原。")
                return

            info = restore_mod.inspect_zip(target)
            if not info.get("ok"):
                yield event.plain_result(
                    f"无法识别：{info.get('error') or '不是本插件的导出包'}")
                return

            yield event.plain_result(
                f"开始恢复（{round(info['size'] / 1048576, 1)} MB）：\n"
                f"原始层 {info['vault_days']} 天 / 人格快照 {info['personas']} / "
                f"事实 {info['facts']} / 附件 {info['blobs']}")

            result = await asyncio.to_thread(
                restore_mod.restore_zip, target, self.root,
                self.vault, self.index)

            # 先建好 lines 再往里 append。之前是先 append、后赋值，
            # 一进到这段就 NameError，数据其实恢复了却报「恢复失败」。
            lines = [
                "恢复完成：" if result.get("ok") else "恢复未完成：",
                f"新增记录 {result.get('events_added', 0)} 条"
                f"（跳过重复 {result.get('events_skipped', 0)}）",
                f"人格快照 {result.get('personas_added', 0)} / "
                f"事实 {result.get('facts_added', 0)} / "
                f"附件 {result.get('blobs_added', 0)}",
            ]

            if result.get("ok"):
                index_info = await asyncio.to_thread(
                    self.finalize_index, bool(result.get("index_copied")))
                if index_info.get("index_kept"):
                    lines.append(f"索引沿用备份（{index_info['index_events']} 条），未重建")
                else:
                    lines.append(f"索引已重建：{index_info.get('index_rebuilt', 0)} 条")

            if result.get("settings_restored"):
                lines.append("面板设置已恢复（阈值、检索条数等）")
            if result.get("state_added"):
                lines.append(f"会话状态恢复 {result['state_added']} 份"
                             f"（接管标记、校准系数）")

            sessions = list(result.get("sessions") or [])
            if sessions and self._cfg("takeover_after_restore"):
                enabled = self.enable_takeover(sessions + [self._session_of(event)])
                if enabled:
                    lines.append("已开启全局接管：所有会话（含新会话）每轮带记忆")
                else:
                    lines.append("全局接管未开启：先填上下文窗口")

            if self._cfg("restore_personas_to_astrbot") and result.get("personas_added"):
                persona_result = await self.sync_personas_to_astrbot()
                created = persona_result.get("created") or []
                if created:
                    lines.append("人格已写回 AstrBot：" + "、".join(created)
                                 + "（去 WebUI 人格页选一下即可）")
                elif persona_result.get("error"):
                    lines.append(f"人格写回失败：{persona_result['error']}")
                else:
                    lines.append("人格快照已存在，未重复创建")

            if result.get("astrbot_data_ignored"):
                lines.append("包里的 astrbot_data/ 未还原，需要停机手动替换")
            if result.get("error"):
                lines.append(f"错误：{result['error']}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"恢复失败: {exc}")

    @filter.command("allmem_backfill")
    async def cmd_backfill(self, event: AstrMessageEvent):
        """把宿主库里内嵌的历史图片补进 blobs/（插件晚装、或归档出过错的补救）。"""
        try:
            path = self._find_astrbot_db()
            if not path:
                yield event.plain_result(
                    "没找到 data_v*.db。检查数据目录，或在配置里填 astrbot_db。")
                return
            yield event.plain_result("开始回填历史图片（只读宿主库，可能要一会儿）…")
            stats = await asyncio.to_thread(
                importer.backfill_media_from_astrbot, path,
                self.media_for(self._uid(event)), ids=self.ids)
            lines = [
                f"扫描会话 {stats['conversations']} 个，带图消息 {stats['messages']} 条",
                f"图片 {stats['images']} 张：补入 {stats['saved']}，跳过 {stats['skipped']}",
                f"合计 {round(stats['bytes'] / 1024, 1)} KB",
            ]
            if stats["errors"]:
                lines.append(f"提示 {len(stats['errors'])} 条（多半是外链图）："
                             + str(stats["errors"][0])[:80])
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"回填失败: {exc}")

    @filter.command("allmem_rebuild")
    async def cmd_rebuild(self, event: AstrMessageEvent):
        try:
            count = self.index.rebuild(self.vault)
            yield event.plain_result(f"索引已从原始层重建：{count} 条")
        except Exception as exc:
            yield event.plain_result(f"重建失败: {exc}")

    @filter.command("allmem_persona")
    async def cmd_persona(self, event: AstrMessageEvent, digest: str = ""):
        """不带参数：列快照。带指纹：把当时的人格设定原文打出来。"""
        try:
            personas = self.vault.personas()
            if not personas:
                yield event.plain_result("还没有人格快照")
                return

            target = (digest or "").strip()
            if not target:
                lines = ["人格快照（内容寻址）："]
                for sha, size in personas[:15]:
                    lines.append(f"- {sha}  {size} 字节")
                if len(personas) > 15:
                    lines.append(f"…还有 {len(personas) - 15} 个")
                lines.append("")
                lines.append("想看全文：/allmem_persona <指纹前几位>")
                yield event.plain_result("\n".join(lines))
                return

            # 支持只写前几位
            match = next((sha for sha, _ in personas if sha.startswith(target)), "")
            if not match:
                match = next((sha for sha, _ in personas if target in sha), "")
            if not match:
                yield event.plain_result(f"没有匹配「{target}」的快照")
                return

            text = self.vault.read_persona(match)
            if not text:
                yield event.plain_result("快照文件读不出来")
                return

            limit = 3000
            head = text[:limit]
            lines = [f"人格快照 {match}（共 {len(text)} 字）：", "", head]
            if len(text) > limit:
                lines.append("")
                lines.append(f"…后面还有 {len(text) - limit} 字，"
                             f"完整原文在 personas\\{match}.txt")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"读取失败: {exc}")


# ---------------------------------------------------------------- 工具函数

def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _downloads_dir() -> str:
    """用户默认下载目录。浏览器实际存哪由它自己决定，这里只用于提示。"""
    try:
        home = os.path.expanduser("~")
        guess = os.path.join(home, "Downloads")
        return guess if os.path.isdir(guess) else home
    except Exception:
        return ""


def _parse_stamp(value) -> datetime | None:
    """把 state 里存的 ISO 时间字符串读回来（时间游标用）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=TZ)


def _pick_request(args, kwargs):
    """
    从钩子参数里认出 ProviderRequest。

    正常情况是 (event, req)，但被别的插件包一层之后参数个数会变，
    所以按「有没有 contexts / system_prompt」来认，而不是按位置。
    """
    for key in ("req", "request", "provider_request"):
        value = kwargs.get(key)
        if value is not None and (hasattr(value, "contexts")
                                  or hasattr(value, "system_prompt")):
            return value
    for value in args:
        if hasattr(value, "contexts") or hasattr(value, "system_prompt"):
            return value
    return None


def _pick_response(event, args, kwargs):
    """从钩子参数里认出 LLMResponse。"""
    for key in ("resp", "response", "llm_response"):
        value = kwargs.get(key)
        if value is not None and not isinstance(value, AstrMessageEvent):
            return value
    for value in args:
        if isinstance(value, AstrMessageEvent) or value is event:
            continue
        if hasattr(value, "completion_text") or hasattr(value, "usage"):
            return value
    # 兜底：第一个不是 event 的参数
    for value in args:
        if value is not event and not isinstance(value, AstrMessageEvent):
            return value
    return None


def _pick_run_context(args, kwargs):
    """
    从 on_agent_done 的参数里认出 run_context（ContextWrapper）。

    它有个 messages 列表，就是宿主即将回存的那份历史。
    """
    for key in ("run_context", "context_wrapper", "ctx"):
        value = kwargs.get(key)
        if value is not None and hasattr(value, "messages"):
            return value
    for value in args:
        if isinstance(value, AstrMessageEvent):
            continue
        if hasattr(value, "messages") and hasattr(value, "context"):
            return value
    # 只认 messages：有些版本的参数顺序不一样
    for value in args:
        if not isinstance(value, AstrMessageEvent) and hasattr(value, "messages"):
            return value
    return None


def _seconds_until(hhmm: str) -> float | None:
    """距离下一个 HH:MM 还有多少秒（按本地时区算）。"""
    return backup_mod.seconds_until(hhmm, now=datetime.now(TZ).replace(tzinfo=None))


def _log_task_error(task) -> None:
    """后台任务失败不要让异常静默消失。"""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        return
    if exc:
        logger.warning(f"[all_memory] 后台任务失败: {exc}")


def _conversation_tokens(req) -> int:
    """AstrBot 会在 conversation.token_usage 里留最近一次请求的总 token。"""
    conversation = getattr(req, "conversation", None)
    if conversation is None:
        return 0
    value = getattr(conversation, "token_usage", 0)
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return 0


def _tool_schema_tokens(req) -> int:
    """
    AstrBot 的工具定义不塞在 system_prompt 里，而是在 req.func_tool。
    不数它，面板会把「人格/工具」算得只剩人格，95k 的真实请求显示成 4.8%。
    """
    tool_set = getattr(req, "func_tool", None)
    if not tool_set:
        return 0
    try:
        schema = None
        openai_schema = getattr(tool_set, "openai_schema", None)
        if callable(openai_schema):
            schema = openai_schema()
        else:
            tools = getattr(tool_set, "tools", None)
            if tools:
                schema = [tool.model_dump() if hasattr(tool, "model_dump")
                          else tool for tool in tools]
        if not schema:
            return 0
        raw = json.dumps(schema, ensure_ascii=False, separators=(",", ":"),
                         default=str)
        return estimate_tokens(raw)
    except Exception:
        return 0


def _prompt_tokens(usage) -> int:
    """从不同形态的 usage 里抠出 prompt token 数。"""
    return usage_input_tokens(usage)
