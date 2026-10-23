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
import base64
import zipfile
import asyncio
from datetime import datetime, timezone, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .core import budget, extract, importer, index as index_mod, inject, refine, restore as restore_mod, sync, vault as vault_mod
from .core import backup as backup_mod
from .core import media as media_mod, pending as pending_mod, settings as settings_mod
from .core import summary as summary_mod
from .core import query as query_mod
from .core import search as search_mod
from .core import vectors as vectors_mod
from .core.tokens import Calibrator, estimate_tokens
from .web_api import WebApi

TZ = timezone(timedelta(hours=8))

DEFAULTS = {
    "data_dir": "",
    "enabled": True,
    "dry_run": False,
    "window_tokens": 0,
    "trigger_ratio": 0.80,
    "total_ratio": 0.40,
    "map_ratio": 0.03,
    "recall_ratio": 0.05,
    "summary_ratio": 0.03,
    "summary_max_chars": 0,
    "summary_keep_recent": 20,
    "facts_before_trigger": False,
    "map_days": 365,
    "recall_limit": 50,
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
        self.facts = extract.FactStore(self.root)
        self.summaries = summary_mod.SummaryStore(self.root)
        # 检索：字面（index）+ 向量（vectors）两路，由 search.combine 融合
        self.embedder = vectors_mod.LocalEmbedder()
        self.vectors = vectors_mod.VectorStore(self.root, self.embedder.name)
        self.query_cache = query_mod.QueryCache(self.root)
        # 向量不是"配置项"，是自动补的：启动后补历史，每轮结束补增量
        self._vector_started = False
        # 附件同理：库里一张图都没有时，自动从宿主库回填一次
        self._media_backfill_started = False
        self.states = budget.StateStore(self.root)
        self.settings = settings_mod.Settings(self.root)
        self.media = media_mod.MediaStore(self.root)
        # 归档失败的重试队列（failed/）
        self.failed = pending_mod.FailedQueue(self.root)

        self._turn_counter: dict[str, int] = {}
        self._extract_cooldown: dict[str, float] = {}
        self._last_raw_estimate: dict[str, int] = {}
        self._refining: dict[str, bool] = {}
        # 正在滚动纪要的会话（和提炼分开：两件事可以并行）
        self._summarizing: dict[str, bool] = {}
        # 本轮的人格快照，等 on_agent_done 归档这一轮时一起带上
        self._pending_turn: dict[str, dict] = {}
        # 这一轮最后一条助手消息的指纹：回复里追加的图片/语音挂它身上
        self._last_assistant_fp: dict[str, str] = {}

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
                    f"事实 {len(self.facts.all_facts())} 条｜"
                    f"附件 {self.media.stats()['files']} 个")

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

        # 第一次进来时把历史向量补齐（后台跑，不阻塞本轮）
        self._ensure_vector_backfill()
        self._ensure_media_backfill()

        session = self._session_of(event)
        try:
            raw, parsed = sync.parse_contexts(req)
            system_prompt = sync.system_of(req)
            persona = self.vault.put_persona(system_prompt)

            # 0) 先把上次没写进去的补上（补写发生在新增之前，顺序不会乱）
            self._flush_failed()

            # 记住人格和「历史到哪为止」，等这一轮跑完在 on_agent_done 里
            # 只把这一轮的消息补进去（历史已经在库里了，不能整段重写）
            self._pending_turn[session] = {
                "persona": persona or "",
                "context_count": len(req.contexts) if isinstance(req.contexts, list) else 0,
                "last_fp": (vault_mod.fp_of(f"{parsed[-1]['role']}\x00{parsed[-1]['content']}")
                            if parsed else ""),
            }

            # 1) 归档：先把看到的一切存下来，再谈裁剪
            state = self.states.load(session)
            stats = sync.sync(
                self.vault, self.index, parsed, persona,
                session=session, ts_hint=self._message_time(event),
                carry=_parse_stamp(state.get("last_carry")))
            if stats.get("new"):
                logger.info(f"[all_memory] 新增 {stats['new']} 条记忆 "
                            f"(去重 {stats.get('duplicate', 0)})")
            if stats.get("carry"):
                state["last_carry"] = stats["carry"]

            # 附件不在这一层抓：这里的 parsed 是历史，本轮消息还没进来。
            # 真正的归档在 on_agent_done（拿到本轮用户消息的指纹之后）。

            calibrator = Calibrator.from_dict(state.get("calibration"))
            calibrator.enabled = bool(self._cfg("calibrate_tokens"))

            system_tokens = estimate_tokens(system_prompt)
            self._last_raw_estimate[session] = (
                sum(estimate_tokens(m["content"]) for m in parsed) + system_tokens
            )

            window = self._window()
            if window <= 0:
                # 没有可信窗口就不接管上下文，只归档
                self._maybe_extract(event, session)
                return

            # 2) 预算决策（滞回）
            #    facts 固定占窗口 10%（facts_ratio_for 直接返回上限，不随裁剪次数爬坡）；
            #    早期库小、抽不满这个额度，历史自然还能多留一些
            trim_count = int(state.get("trim_count", 0) or 0)
            projected = trim_count + 1
            plan = budget.decide(
                parsed, system_tokens, window, calibrator,
                trigger_ratio=float(self._cfg("trigger_ratio")),
                total_ratio=float(self._cfg("total_ratio")),
                facts_ratio=budget.facts_ratio_for(projected),
                map_ratio=float(self._cfg("map_ratio")),
                recall_ratio=float(self._cfg("recall_ratio")),
                summary_ratio=float(self._cfg("summary_ratio")),
            )
            if plan.applied:
                trim_count = projected
                state["trim_count"] = trim_count
            facts_ratio = budget.facts_ratio_for(trim_count) if trim_count else 0.0

            # 裁剪那一刻触发两件后台活：提炼人物卡、滚动前情纪要。
            # 都扔到后台，本轮回复不等它们。
            if plan.applied and not self._cfg("dry_run"):
                self._schedule_refine(session, budget.block_budget(
                    window, facts_ratio,
                    getattr(calibrator, "ratio", 1.0) or 1.0))
                boundary = self._first_kept_fp(plan)
                # 记住这次裁到哪：手动生成纪要时要用同一个边界，
                # 否则会去纪要"还在上下文里"的对话，白白重复一遍。
                if boundary:
                    state["summary_boundary_fp"] = boundary
                self._schedule_summary(session, boundary)

            # 演练模式：只记账、只写日志，绝不改动本轮请求
            if self._cfg("dry_run"):
                if plan.applied:
                    logger.info(f"[all_memory] DRY-RUN {plan.summary()} → 未改动任何内容")
                self._maybe_extract(event, session)
                return

            # 3) 触发裁剪，回写原始对象（保住宿主消息格式）
            #    trimmed_mode：只要裁剪过一次，此后每轮都处于「接管模式」，
            #    持续注入事实/地图/检索。否则裁完回到 40% 压力，
            #    后续几十轮 applied=False，被裁掉的内容会有一段很长的失忆期。
            trimmed_mode = bool(state.get("trimmed_mode"))
            if plan.applied:
                keep_ids = {m["idx"] for m in plan.kept}
                req.contexts = [item for i, item in enumerate(raw) if i in keep_ids]
                trimmed_mode = True
                # 裁剪后真正发出去的历史短了，边界要跟着改
                pending = self._pending_turn.get(session)
                if isinstance(pending, dict):
                    pending["context_count"] = len(req.contexts)
                logger.info(f"[all_memory] {plan.summary()}")

            # 4) 组装记忆块
            #    第一次裁剪之前与原版完全一致：不裁剪、不注入任何块。
            blocks: list[str] = []
            sizes = {
                "system": system_tokens,
                "history": sum(estimate_tokens(m["content"])
                               for m in (plan.kept if plan.applied else parsed)),
                "summary": 0,
                "facts": 0,
                "map": 0,
                "recall": 0,
            }

            # 纪要排最前面：先给"前情"，再给"她是谁"，最后给细节。
            # 只在接管模式下注入——触发裁剪之前，一块都不加。
            if trimmed_mode:
                summary_block = inject.build_summary_block(
                    str(self.summaries.load(session).get("text") or ""),
                    self._block_budget(
                        window, float(self._cfg("summary_ratio")), calibrator))
                if summary_block:
                    blocks.append(summary_block)
                    sizes["summary"] = estimate_tokens(summary_block)

            # 本轮用户刚说的话：它不在 parsed 里（contexts 只有历史），
            # 得从 req.prompt 拿；拿不到才退回最后一条上下文。
            query_text = (getattr(req, "prompt", "") or "").strip() \
                or (parsed[-1]["content"] if parsed else "")

            if trimmed_mode or self._cfg("facts_before_trigger"):
                facts_block = inject.build_facts_block(
                    self._active_facts(session, query_text),
                    self._block_budget(window, facts_ratio, calibrator))
                if facts_block:
                    blocks.append(facts_block)
                    sizes["facts"] = estimate_tokens(facts_block)

            if trimmed_mode:
                if self._cfg("auto_recall"):
                    limit = max(1, int(self._cfg("recall_limit")))
                    # 两路召回（字面 + 向量），多取一些：下面要剔掉已在本轮上下文里的
                    hits = await self.retrieve(
                        query_text, session=session, limit=limit * 2,
                        use_llm=bool(self._cfg("recall_expand")))

                    # 最近 N 轮已经原样发出去了，检索再把它们捞回来纯属浪费，
                    # 还会让同一句话重复出现。按内容指纹剔除。
                    in_context = plan.kept if plan.applied else parsed
                    skip = {vault_mod.fp_of(f"{m['role']}\x00{m['content']}")
                            for m in in_context}
                    # 问"第一次/最早"时，把最早那天的原话直接排前面：
                    # 这类问题检索词未必命中（"第一次见面"不一定出现在原句里），
                    # 交给模型自觉去查不如直接把证据递到眼前。
                    if query_mod.is_earliest_intent(query_text):
                        hits = self._earliest_hits(hits)
                    # 指名道姓问了某一天（昨天 / 10月20 / 2026-10-22），
                    # 就把那天的原话也直接递上去——问"那天吃了什么"时，
                    # 原文很可能只写"对付了两口"，单靠检索词命不中。
                    start_day, end_day = query_mod.time_window(query_text)
                    if start_day and start_day == end_day:
                        hits = self._day_hits(hits, start_day)
                    hits = inject.drop_known(hits, skip)[:limit]

                    recall_block = inject.build_recall_block(
                        hits,
                        self._block_budget(
                            window, float(self._cfg("recall_ratio")), calibrator),
                        max_chars=int(self._cfg("recall_snippet_chars") or 800))
                    if recall_block:
                        blocks.append(recall_block)
                        sizes["recall"] = estimate_tokens(recall_block)

                # 地图覆盖全部历史，不是只回看 N 天
                day_rows = self.index.day_summary(days=None)
                map_block = inject.build_map_block(
                    day_rows,
                    self._block_budget(
                        window, float(self._cfg("map_ratio")), calibrator),
                    detail_days=int(self._cfg("map_days")))
                if map_block:
                    blocks.append(map_block)
                    sizes["map"] = estimate_tokens(map_block)

            payload = inject.compose(blocks)
            if payload and not inject.attach(req, payload):
                logger.debug("[all_memory] 无法附加记忆块（TextPart 不可用）")

            # 5) 状态回写（含各块实际用量，供面板查看）
            #    payload 本来就是 facts + 地图 + 检索拼起来的（外加使用提示和 _no_save 戳），
            #    所以合计只能 = 主体（system + history）+ payload。
            #    把那三块再加一遍会让「合计」凭空多算一次，拿它调参会被带偏。
            sizes["payload"] = estimate_tokens(payload) if payload else 0
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
            self.states.save(session, state)

            self._maybe_extract(event, session)
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
        session = self._session_of(event)
        try:
            # 用真实用量校准估算
            usage = getattr(resp, "usage", None)
            actual = _prompt_tokens(usage)
            raw_estimate = self._last_raw_estimate.get(session, 0)
            if actual and raw_estimate:
                state = self.states.load(session)
                calibrator = Calibrator.from_dict(state.get("calibration"))
                calibrator.enabled = bool(self._cfg("calibrate_tokens"))
                calibrator.learn(raw_estimate, actual)
                state["calibration"] = calibrator.to_dict()
                self.states.save(session, state)
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
                # 挂在最后一条助手消息上；找不到就退到本会话最后一条记录
                msg_fp = self._last_assistant_fp.get(session) or self.index.last_fp(session)
                if not msg_fp:
                    return
                task = asyncio.create_task(
                    self.media.ingest(msg_fp, session, sources))
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
            pending = self._pending_turn.pop(session, {}) or {}
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
                state = self.states.load(session)
                stats = sync.sync(self.vault, self.index, parsed, persona,
                                  session=session, origin="astrbot_turn",
                                  force=exact,
                                  carry=_parse_stamp(state.get("last_carry")),
                                  sender=self._sender_of(event))
                if stats.get("carry") and stats["carry"] != state.get("last_carry"):
                    state["last_carry"] = stats["carry"]
                    self.states.save(session, state)
                if stats.get("new"):
                    logger.info(f"[all_memory] 本轮补入 {stats['new']} 条 "
                                f"(编号 {stats.get('renamed', 0)} 条)")
                # 附件：本轮用户消息的图片/语音，键就是刚才存进去的那条指纹
                self._capture_turn_media(event, parsed, session)
                # 记下最后一条助手消息：有些插件会在回复之后自己追加表情，
                # 那些图不在 run_context.messages 里，只能由 decorator 钩子接住
                for item in reversed(parsed):
                    if item.get("role") == "assistant":
                        self._last_assistant_fp[session] = vault_mod.fp_of(
                            f"assistant\x00{item['content']}")
                        break
                # 向量：纯本地计算，几十毫秒，后台补，别拖慢回复
                try:
                    asyncio.create_task(asyncio.to_thread(self.vectorize_recent))
                except RuntimeError:
                    pass
            except Exception as exc:
                # 写失败不能只留一行日志：先落盘，下一轮请求进来时补写
                path = self.failed.push(session, parsed, persona, str(exc))
                logger.warning(
                    f"[all_memory] 本轮归档失败，"
                    + (f"已存入重试队列（{os.path.basename(path)}）" if path
                       else "重试队列也写不进去")
                    + f": {exc}")

    # ------------------------------------------------------------ 接管模式

    def enable_takeover(self, sessions) -> int:
        """
        让这些会话进入「接管模式」：每轮都带记忆地图 + 相关检索（+ facts）。

        恢复备份之后要开：新会话上下文才 1%，永远到不了 80% 触发线，
        不开的话旧记忆只能靠她自己 search_memory 去查。
        """
        count = 0
        for session in sessions or []:
            if not session:
                continue
            state = self.states.load(session)
            if state.get("trimmed_mode"):
                continue
            state["trimmed_mode"] = True
            state["trim_count"] = max(1, int(state.get("trim_count", 0) or 0))
            state["takeover_at"] = _now_iso()
            self.states.save(session, state)
            count += 1
        if count:
            logger.info(f"[all_memory] 已让 {count} 个会话进入接管模式")
        return count

    def disable_takeover(self, session: str) -> None:
        state = self.states.load(session)
        state["trimmed_mode"] = False
        self.states.save(session, state)

    @filter.command("allmem_takeover")
    async def cmd_takeover(self, event: AstrMessageEvent, mode: str = ""):
        """手动开关接管模式：开=每轮都带记忆，关=触发前零注入。"""
        try:
            session = self._session_of(event)
            mode = (mode or "").strip().lower()
            if mode in ("off", "0", "关", "关闭"):
                self.disable_takeover(session)
                yield event.plain_result(
                    "已关闭接管模式：回到「触发前零注入」，和原版一致。")
                return

            self.enable_takeover([session])
            yield event.plain_result(
                "已开启接管模式：\n"
                "此后每轮都会带记忆地图 + 相关性检索（预算约 8%），"
                "有事实的话也会带人物卡。\n"
                "关闭：/allmem_takeover off")
        except Exception as exc:
            yield event.plain_result(f"操作失败: {exc}")

    # ------------------------------------------------------------ 导出

    def export_targets(self) -> list[tuple]:
        """
        导出时要打包的 (源目录, 包内前缀, 该源跳过的子树)。

        跳过列表必须按来源分开，否则插件数据会被跳过或重复打包。
        """
        exports = os.path.abspath(os.path.join(self.root, "exports"))
        targets: list[tuple] = [(self.root, "all_memory", (exports,))]

        if self._cfg("backup_astrbot_data"):
            data_root = self.astrbot_data_root()
            if data_root:
                targets.append((data_root, "astrbot_data", (self.root,)))
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

    def _write_session_rows(self, session: str, handle) -> int:
        """把一个会话写成 jsonl，一行一条；返回条数。"""
        data = self.index.history(session, offset=0, limit=0, newest_first=False)
        items = data.get("items") or []
        fingerprints = {str(item.get("fp") or "") for item in items}
        media_map = self.media.records_for(fingerprints)

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
            name = f"{self._session_slug(session)}_{stamp}.jsonl"
            path = os.path.join(exports, name)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                count = self._write_session_rows(session, handle)
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
                total += self._write_session_rows(session, buffer)
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

    def _maybe_extract(self, event, session: str) -> None:
        """可选的周期性提炼（默认关闭，0）。主路径是裁剪时触发。"""
        interval = int(self._cfg("auto_extract_interval") or 0)
        if interval <= 0:
            return
        count = self._turn_counter.get(session, 0) + 1
        self._turn_counter[session] = count
        if count % interval != 0:
            return
        if self._extract_cooldown.get(session, 0) > asyncio.get_event_loop().time():
            return
        try:
            asyncio.create_task(self._refine_job(session, reason="periodic"))
        except RuntimeError:
            pass

    def start_refine(self, session: str = "__web__", reason: str = "web") -> dict:
        """
        启动一次全量提炼（指令与面板共用这一个入口）。

        **全局只允许一个在跑**：提炼最后是 replace_all（旧目录归档 → staging 换上去），
        两路同时跑会互相覆盖，所以这里查的是所有会话，不只是自己那个。
        """
        if any(bool(flag) for flag in self._refining.values()):
            return {"ok": False, "error": "已经有一轮提炼在跑，等它结束"}

        window = self._window()
        if window <= 0:
            return {"ok": False, "error": "还没填 window_tokens，算不出 facts 预算"}

        state = self.states.load(session)
        ratio = budget.facts_ratio_for(int(state.get("trim_count", 0) or 0))
        facts_budget = budget.block_budget(window, ratio, 1.0)

        try:
            task = asyncio.create_task(self._refine_job(
                session, facts_budget=facts_budget, reason=reason))
            task.add_done_callback(_log_task_error)
        except RuntimeError:
            return {"ok": False, "error": "当前没有可用的后台任务循环"}

        return {
            "ok": True,
            "window": window,
            "facts_budget": facts_budget,
            "facts_ratio": round(ratio * 100, 2),
            "message": f"已开始全量提炼：预算约 {facts_budget} tok"
                       f"（{ratio * 100:.0f}%），旧人物卡会先归档",
        }

    def _schedule_refine(self, session: str, facts_budget: int) -> None:
        """
        裁剪那一刻触发提炼：扫全量库，输出受 facts 预算约束。

        这是主路径。放在后台任务里跑，不阻塞这一轮回复。
        """
        if not self._cfg("extract_on_trim"):
            return
        if self._refining.get(session):
            logger.info("[all_memory] 上一轮提炼还没跑完，本次跳过")
            return
        try:
            task = asyncio.create_task(
                self._refine_job(session, facts_budget=facts_budget,
                                 reason="trim"))
            task.add_done_callback(_log_task_error)
        except RuntimeError:
            pass

    async def _refine_job(self, session: str, facts_budget: int = 0,
                          reason: str = "trim") -> None:
        if self._refining.get(session):
            return
        self._refining[session] = True
        try:
            window = self._window()
            if facts_budget <= 0 and window > 0:
                state = self.states.load(session)
                facts_budget = budget.block_budget(
                    window, budget.facts_ratio_for(
                        int(state.get("trim_count", 0) or 0)), 1.0)

            # 读全量库是 IO，扔线程里，别卡事件循环。
            # 同时把工具输出滤掉：它们是噪声（"File written successfully"），
            # 既拖慢提炼，又稀释 prompt 里的有效信息。
            events = await asyncio.to_thread(self._extract_events)
            if len(events) < 4:
                return

            chunk_chars = int(self._cfg("extract_chunk_chars") or 6000)
            # 每批最多出几条事实：facts 预算（窗口 10%）÷ 每批字数。
            # 窗口越大、预算越大，单批能存的条数就越多，不需要别的上限。
            per_batch = max(1, int(max(200, facts_budget) / max(1, chunk_chars)))
            concurrency = max(1, int(self._cfg("extract_concurrency") or 6))

            total_chars = sum(len(str(e.get("text", ""))) for e in events)
            model_name = str(self._cfg("extract_model") or "")

            state = self.states.load(session)
            previous = state.get("last_refine") or {}
            estimate = ""
            if previous.get("batches") and previous.get("seconds"):
                per_batch_seconds = float(previous["seconds"]) / max(1, int(previous["batches"]))
                rough = int(per_batch_seconds * max(1, total_chars // max(1, chunk_chars))
                            / concurrency)
                estimate = f"，预计约 {rough} 秒"
            logger.info(
                f"[all_memory] 开始提炼（{reason}）：{len(events)} 条 / "
                f"{total_chars} 字，按 {chunk_chars} 字切批，"
                f"每批最多 {per_batch} 条，并发 {concurrency}{estimate}")

            started = time.monotonic()
            result = await refine.run(
                self._call_llm, events, self.facts,
                target_tokens=max(200, facts_budget),
                model=model_name,
                chunk_chars=chunk_chars,
                max_facts=per_batch,
                cache=refine.ExtractionCache(self.root),
                concurrency=concurrency,
            )
            elapsed = time.monotonic() - started
            logger.info(
                f"[all_memory] 提炼完成：{result.get('batches')} 批"
                f"（复用 {result.get('cached_batches', 0)} 批）→ "
                f"{result.get('written')} 条事实，"
                f"{result.get('tokens')} tok / 预算 {result.get('target_tokens')}，"
                f"用时 {elapsed:.0f} 秒"
                + ("（超出一部分，注入时按权重截）"
                   if result.get("over_budget") else ""))

            state = self.states.load(session)
            state["last_refine"] = {
                "at": _now_iso(),
                "reason": reason,
                "events": len(events),
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
            self.states.save(session, state)
        except Exception as exc:
            logger.warning(f"[all_memory] 提炼失败（已跳过）: {exc}")
            self._extract_cooldown[session] = (
                asyncio.get_event_loop().time() + 300)
        finally:
            self._refining[session] = False

    # ------------------------------------------------------------ 前情纪要

    @staticmethod
    def _first_kept_fp(plan) -> str:
        """裁剪后还留在上下文里的第一条消息 = 纪要的右边界（它之前都该被纪要到）。"""
        kept = getattr(plan, "kept", None) or []
        if not kept:
            return ""
        first = kept[0]
        return vault_mod.fp_of(f"{first['role']}\x00{first['content']}")

    def start_summary(self, session: str, boundary_fp: str = "",
                      reason: str = "manual") -> dict:
        """
        启动一次滚动纪要（指令与面板共用）。

        边界优先级：
          1. 调用方给的（裁剪那一刻算出来的）
          2. state 里记的"上次裁到哪"——手动生成时要和自动那次用同一个边界，
             否则会去纪要还留在上下文里的对话
          3. 都没有（还没裁剪过）→ 退回「最近 N 条之前」，纯粹是给个能看的演示
        """
        if self._summarizing.get(session):
            return {"ok": False, "error": "这个会话正在生成纪要，等它结束"}

        if not boundary_fp:
            boundary_fp = str(
                self.states.load(session).get("summary_boundary_fp") or "")

        try:
            task = asyncio.create_task(
                self._summary_job(session, boundary_fp=boundary_fp, reason=reason))
            task.add_done_callback(_log_task_error)
        except RuntimeError:
            return {"ok": False, "error": "当前没有可用的后台任务循环"}

        return {
            "ok": True,
            "session": session,
            "reason": reason,
            "boundary": "trim" if boundary_fp else "fallback",
            "message": ("已开始生成前情纪要：覆盖到上次裁剪保留的第一条之前"
                        if boundary_fp else
                        "已开始生成前情纪要：还没裁剪过，先按最近 20 条之前演示"),
        }

    def _schedule_summary(self, session: str, boundary_fp: str) -> None:
        """裁剪那一刻触发。配了 0 预算就不生成，省得白调模型。"""
        try:
            if float(self._cfg("summary_ratio") or 0) <= 0:
                return
        except (TypeError, ValueError):
            return
        if self._summarizing.get(session):
            logger.info("[all_memory] 上一轮纪要还没跑完，本次跳过")
            return
        try:
            task = asyncio.create_task(
                self._summary_job(session, boundary_fp, "trim"))
            task.add_done_callback(_log_task_error)
        except RuntimeError:
            pass

    def _summary_max_chars(self, event_count: int = 0) -> int:
        """
        纪要的字数上限。

        显式配了 `summary_max_chars` 就用它；配 0 表示"按预算自动算"：
        纪要占窗口的 3%，100 万窗口对应约 3 万 token ≈ 5 万汉字，
        真长到那个量级每轮都要重写一遍、也读不完，所以自动封在 8000 字，
        再小不低于 2000——太短会把早期经历挤没。
        """
        configured = int(self._cfg("summary_max_chars") or 0)
        if configured > 0:
            return configured
        window = self._window()
        if window <= 0:
            return 2000
        try:
            ratio = float(self._cfg("summary_ratio") or 0.03)
        except (TypeError, ValueError):
            ratio = 0.03
        budget_chars = int(window * ratio * 1.6)
        return max(2000, min(8000, budget_chars))

    async def _summary_job(self, session: str, boundary_fp: str = "",
                           reason: str = "trim") -> None:
        if self._summarizing.get(session):
            return
        self._summarizing[session] = True
        try:
            events = await asyncio.to_thread(self._session_events, session)
            if len(events) < 4:
                logger.info("[all_memory] 内容太少，跳过纪要")
                return

            chunk_chars = int(self._cfg("extract_chunk_chars") or 6000)
            max_chars = self._summary_max_chars(len(events))
            keep_recent = int(self._cfg("summary_keep_recent") or 20)

            logger.info(f"[all_memory] 开始生成纪要（{reason}）：{len(events)} 条，"
                        f"边界 {boundary_fp[:8] or f'最近 {keep_recent} 条之前'}")

            result = await summary_mod.build(
                self._call_llm, events, self.summaries, session,
                boundary_fp=boundary_fp, chunk_chars=chunk_chars,
                max_chars=max_chars,
                cache=summary_mod.SummaryCache(self.root),
                keep_recent=keep_recent)

            if result.get("skipped"):
                logger.info("[all_memory] 纪要不更新：没有新的可纪要内容")
            elif result.get("ok"):
                logger.info(
                    f"[all_memory] 纪要已更新：{result.get('batches')} 批"
                    f"（复用 {result.get('cached')} 批）→ {result.get('chars')} 字，"
                    f"覆盖到 {result.get('covered_until', '')}")
            else:
                logger.warning(f"[all_memory] 纪要失败：{result.get('error')}")

            state = self.states.load(session)
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
            self.states.save(session, state)
        except Exception as exc:
            logger.warning(f"[all_memory] 纪要失败（已跳过）: {exc}")
        finally:
            self._summarizing[session] = False

    def _extract_events(self) -> list[dict]:
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
        for event in self.vault.iter_events():
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

    def _session_events(self, session: str) -> list[dict]:
        """这个会话的全部原文，按写入顺序（跨会话混起来就没法写叙事了）。"""
        events = list(self.vault.iter_events())
        picked = [e for e in events
                  if str(e.get("session") or "") == (session or "")]
        # 单会话/导入数据可能没有 session 字段，那样退回全量
        return picked if len(picked) >= 4 else events

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

    async def _expand_terms(self, text: str, use_llm: bool = False) -> list[str]:
        """
        让模型补几个同义词（可选）。

        自动召回默认不走这条路：它每轮都跑，多一次模型调用就多一秒延迟。
        工具检索走这条路——模型主动查的时候，多等一会儿值得。
        开关：recall_expand（默认关）。
        """
        if not use_llm:
            return []
        cached = self.query_cache.get(text)
        if cached is not None:
            return cached
        try:
            raw = await self._call_llm(
                query_mod.EXPAND_PROMPT.replace("{query}", text[:200]))
        except Exception:
            return []
        terms = query_mod.parse_expansions(raw)
        if terms:
            self.query_cache.put(text, terms)
        return terms

    async def retrieve(self, text: str, session: str = "", limit: int = 20,
                       use_llm: bool = False) -> list[dict]:
        """
        字面 + 向量两路召回，融合排序。

        自动召回、search_memory 工具、/allmem_search 共用这一个入口——
        三处各写一套的话，调好一处另外两处还是旧的。
        """
        text = (text or "").strip()
        if len(text) < 2:
            return []

        expansions = await self._expand_terms(text, use_llm=use_llm)
        terms = query_mod.build_terms(text, expansions)
        per_term = max(10, int(limit) * 2)

        literal = []
        for term in terms:
            try:
                literal.append(self.index.search(term, limit=per_term, session=session))
            except Exception:
                literal.append([])

        vector_hits: list[tuple[str, float]] = []
        try:
            query_vector = self.embedder.vector(text)
            raw = self.vectors.search(query_vector, limit=per_term)
            if session and raw:
                sessions = self.index.sessions_of([fp for fp, _ in raw])
                raw = [(fp, score) for fp, score in raw
                       if sessions.get(fp, session) == session]
            vector_hits = raw
        except Exception:
            vector_hits = []

        return search_mod.combine(
            literal, vector_hits, fetch=self.index.get,
            window=query_mod.time_window(text), limit=limit)

    def vectorize_recent(self, limit: int = 400) -> int:
        """给最近的事件补向量。纯本地计算，几十毫秒，扔后台跑。"""
        try:
            events = self.vault.recent_events(limit)
            if not events:
                return 0
            fingerprints = [str(e.get("fp") or "") for e in events]
            known = self.vectors.has_many(fingerprints)
            todo = [e for e in events
                    if str(e.get("fp") or "") and str(e["fp"]) not in known]
            if not todo:
                return 0
            payload = [(str(e["fp"]), self.embedder.vector(e.get("text", "")))
                       for e in todo]
            return self.vectors.put(payload)
        except Exception as exc:
            logger.debug(f"[all_memory] 向量化失败: {exc}")
            return 0

    def vectorize_missing(self, batch: int = 400) -> int:
        """
        从最早的一天开始补，补满 batch 条就返回，下次接着来。

        按天读、按天查，不把整库塞进内存——历史再长也不会一下吃爆。
        """
        written = 0
        payload: list[tuple[str, list[float]]] = []
        try:
            for day in self.vault.days():
                if written >= batch:
                    break
                events = self.vault.read_day(day)
                if not events:
                    continue
                fingerprints = [str(e.get("fp") or "") for e in events]
                known = self.vectors.has_many(fingerprints)
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
                self.vectors.put(payload)
            except Exception as exc:
                logger.debug(f"[all_memory] 向量写入失败: {exc}")
                return 0
        return written

    async def _vector_backfill(self) -> None:
        """启动后把历史补齐。补完就退出；增量由每轮结束时的 vectorize_recent 负责。"""
        while True:
            try:
                done = await asyncio.to_thread(self.vectorize_missing, 400)
            except Exception:
                return
            if not done:
                return
            logger.info(f"[all_memory] 已补 {done} 条向量，继续…")
            await asyncio.sleep(0)

    def _ensure_vector_backfill(self) -> None:
        """第一次用到检索时把补齐任务拉起来，只拉一次。"""
        if self._vector_started:
            return
        try:
            asyncio.create_task(self._vector_backfill())
            self._vector_started = True
        except RuntimeError:
            pass

    def _ensure_media_backfill(self) -> None:
        """附件库是空的时候，自动从宿主库补一次历史图片。"""
        if self._media_backfill_started:
            return
        try:
            asyncio.create_task(self._media_backfill())
            self._media_backfill_started = True
        except RuntimeError:
            pass

    async def _media_backfill(self) -> None:
        """
        只在附件库为空时跑：旧版本的归档是坏的，库里一张图都没有，
        而宿主库里图是 base64 内嵌的，补得回来。补过就不再动。
        """
        try:
            if self.media.stats().get("files"):
                return
            path = self._find_astrbot_db()
            if not path:
                return
            result = await asyncio.to_thread(
                importer.backfill_media_from_astrbot, path, self.media)
            if result.get("saved"):
                logger.info(
                    f"[all_memory] 历史附件回填 {result['saved']} 张"
                    f"（{round(result['bytes'] / 1024, 1)} KB，"
                    f"扫描 {result['images']} 张）")
            elif result.get("images"):
                logger.info(f"[all_memory] 历史附件已齐（扫描 {result['images']} 张）")
        except Exception as exc:
            logger.debug(f"[all_memory] 附件回填失败: {exc}")

    def _earliest_hits(self, hits: list[dict], limit: int = 8) -> list[dict]:
        """
        把最早那天的开头几条排到召回最前面。

        问"第一次见面说了什么"时，检索词可能完全命不中（原句里未必有"第一次"），
        所以这条不靠模型自觉：只要识别出意图，证据直接递到眼前。
        """
        try:
            day = self.index.earliest_day()
            if not day:
                return hits
            seeds = self.index.day_messages(day, limit=limit)
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

    def _day_hits(self, hits: list[dict], day: str, limit: int = 20) -> list[dict]:
        """
        把某一天的原话排到召回最前面。

        问"那天几点出门、几点回来"时，原文可能写成"中午对付了两口"，
        检索词命中不了；既然日期是明确的，就把那天的对话直接递上去。

        按**均匀取样**而不是只取开头结尾：一天里的关键节点可能在中间，
        只取两头会漏掉"下午两点我回来了"这种。
        """
        try:
            items = self.index.day_messages(day, limit=200)
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

    def _flush_failed(self) -> int:
        """
        把之前归档失败的内容补写进去。

        放在每条请求的最前面：补的是旧轮次，先写进去，顺序才不会乱。
        写成功才删文件，写不进去就留着下次再试。
        """
        written = 0
        try:
            for path, data in self.failed.pending(limit=3):
                items = data.get("items") or []
                parsed = [
                    {"idx": i, "role": str(item.get("role") or "user"),
                     "content": str(item.get("content") or ""), "raw": None}
                    for i, item in enumerate(items)
                    if str(item.get("content") or "").strip()
                ]
                if not parsed:
                    self.failed.discard(path)
                    continue
                stats = sync.sync(self.vault, self.index, parsed,
                                  str(data.get("persona") or ""),
                                  session=str(data.get("session") or ""),
                                  origin="retry")
                self.failed.discard(path)
                written += int(stats.get("new") or 0)
            if written:
                logger.info(f"[all_memory] 重试队列补写 {written} 条")
        except Exception as exc:
            logger.warning(f"[all_memory] 补写重试队列失败: {exc}")
        return written

    def _capture_turn_media(self, event, parsed: list[dict], session: str) -> None:
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
                self.media.ingest(msg_fp, session, sources))
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

    def _active_facts(self, session: str = "", query: str = "") -> list[dict]:
        """
        按「身份 > 关系 > 偏好 > 其他 > 事件」排序，同权重内新的在前。

        排序之前先按身份过滤：只留这个会话看得见的。
        群聊里 A 的私事不能漏到 B 的对话里，所以这一步不能省。

        带了本轮的话时，再按"沾不沾边"往前排——几百条全量塞得下，
        但相关的内容放前面，模型读起来更有用；等上千条，这一步就是省预算的关键。
        """
        facts = list(self.facts.latest().values())
        if session:
            facts = self._visible_facts(facts, session)
        if query:
            return search_mod.rank_facts(facts, query_mod.split_terms(query))
        facts.sort(key=lambda f: (extract.kind_weight(f.get("kind")),
                                  str(f.get("extracted_at", ""))),
                   reverse=True)
        return facts

    def _visible_facts(self, facts: list[dict], session: str) -> list[dict]:
        """把"谁能看见"需要的两个索引一次性查出来，别在循环里查库。"""
        fingerprints: list[str] = []
        for fact in facts:
            fingerprints.extend(fact.get("source_fp") or [])
        try:
            source_session = self.index.sessions_of(fingerprints)
        except Exception:
            source_session = {}
        try:
            subjects = self.index.subjects(session)
        except Exception:
            subjects = set()
        # 提炼时会给历史消息补一个推算的 sender，这里必须同步认它，
        # 否则事实带着这个 ID，却因为"本会话没这个人"被过滤掉——静默丢记忆。
        guess = sync.infer_sender(session, subjects)
        if guess:
            subjects = set(subjects) | {guess}
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
                query, session=self._session_of(event), limit=limit,
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
            day = (day or "").strip()
            if day:
                start = max(0, int(offset or 0))
                items = self.index.day_messages(day, limit=40, offset=start)
                if not items:
                    if start:
                        return f"{day} 第 {start} 条之后没有更多了。"
                    return (f"{day} 没有记录。先用 memory_timeline 看有哪几天，"
                            "或用 search_memory 搜关键词。")
                total = self.index.day_count(day)
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
            target = self.index.get(memory_id)
            if not target:
                return f"没有找到编号为 {memory_id} 的记忆，编号可能已经过期。"

            lines = []
            for item in self.index.neighbors(memory_id, span=span):
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
            rows = self.index.day_summary(days=None)
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
            session = self._session_of(event)
            stats = self.vault.stats()
            facts = len(self.facts.all_facts())
            digest = self.summaries.load(session)
            digest_chars = len(str(digest.get("text") or ""))
            state = self.states.load(session)
            plan = state.get("last_plan") or {}
            queued = self.failed.count()
            lines = [
                f"原始层：{stats['events']} 条 / {stats['days']} 天",
                f"提炼层：{facts} 条事实",
                f"前情纪要：{digest_chars} 字"
                + (f"（覆盖到 {digest.get('covered_until', '')[:16]}）"
                   if digest_chars else "（还没有）"),
                f"索引层：{self.index.count()} 条",
                f"向量层：{self.vectors.count()} 条（本地哈希）",
                f"窗口：{self._window() or '未配置'}",
                f"上次：{plan.get('pressure', 0) * 100:.1f}% "
                f"{'已裁剪' if plan.get('applied') else '未触发'}",
            ]
            if queued:
                lines.append(f"待补写：{queued} 轮（下条消息会自动补）")
            lines.append(f"目录：{self.root}")
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
        if not root:
            return configured
        try:
            names = [n for n in os.listdir(root)
                     if n.startswith("data_v") and n.endswith(".db")]
        except OSError:
            return configured
        if not names:
            return configured
        paths = [os.path.join(root, n) for n in names]
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return paths[0]

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

            if os.path.isdir(path):
                stats = importer.import_beyond(path, self.vault, self.index)
                yield event.plain_result(
                    f"导入 memory_beyond：\n"
                    f"扫描文件 {stats['files']} 个\n"
                    f"新增事件 {stats['events']} 条\n"
                    + (f"错误 {len(stats['errors'])} 个"
                       if stats["errors"] else "无错误"))
            else:
                stats = importer.import_astrbot(path, self.vault, self.index)
                yield event.plain_result(
                    f"导入 AstrBot 数据库：\n"
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
                keyword, session=self._session_of(event), limit=8,
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
            latest = list(self.facts.latest().values())
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

    @filter.command("allmem_show")
    async def cmd_show(self, event: AstrMessageEvent, fact_id: str = ""):
        try:
            fact = self.facts.load((fact_id or "").strip())
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
        """手动全量重提炼：扫当前全量库，归档旧版后整体替换。"""
        try:
            session = self._session_of(event)
            info = self.start_refine(session, reason="manual")
            if not info.get("ok"):
                yield event.plain_result(info.get("error") or "启动失败")
                return
            yield event.plain_result(
                f"已开始全量重提炼：预算约 {info['facts_budget']} tok"
                f"（{info['facts_ratio']:.0f}%），旧版会先归档。\n"
                f"跑完用 /allmem_facts 查看。")
        except Exception as exc:
            yield event.plain_result(f"启动失败: {exc}")

    @filter.command("allmem_summary")
    async def cmd_summary(self, event: AstrMessageEvent):
        """手动生成/更新前情纪要：滚动合并，只处理还没纪要过的部分。"""
        try:
            session = self._session_of(event)
            info = self.start_summary(session, reason="manual")
            if not info.get("ok"):
                yield event.plain_result(info.get("error") or "启动失败")
                return
            yield event.plain_result(
                f"{info['message']}，旧纪要会滚动合并进来。\n"
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
                sessions.append(self._session_of(event))
                enabled = self.enable_takeover(sessions)
                if enabled:
                    lines.append(f"已让 {enabled} 个会话进入接管模式（每轮带记忆）")

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
                importer.backfill_media_from_astrbot, path, self.media)
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


def _prompt_tokens(usage) -> int:
    """从不同形态的 usage 里抠出 prompt token 数。"""
    if usage is None:
        return 0
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "input_tokens", "promptTokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return 0
    for key in ("prompt_tokens", "input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0
