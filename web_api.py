"""
web_api.py —— WebUI 后端

每个会话一个视图：左侧列出私聊/群聊，右侧看历史（含图片语音预览），
底部两个滑块调裁剪阈值，一个按钮打包全量数据。

导出包 = 插件数据根目录的完整副本 + 配置里指定的额外目录（表情包插件等）。
换句话说：压缩包里有什么，跟你直接打开数据目录看到的一样多。
"""

from __future__ import annotations

import os
import time
import zipfile
import asyncio

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request

from .core import backup, restore
from .core import extract
from .core.settings import limits as settings_limits

PLUGIN_NAME = "astrbot_plugin_all_memory"
PREFIX = f"/{PLUGIN_NAME}/"
LOG = "[all_memory] "


def _state_key(session: str) -> str:
    """和 StateStore._key 保持一致，用来把会话名映射到状态文件名。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                   for ch in (session or "default"))[:120]
    return safe or "default"


def _count_kinds(items: list[dict]) -> dict:
    """按类型数一数，面板顶部显示「身份 3 / 偏好 12…」。"""
    out: dict[str, int] = {}
    for item in items:
        kind = item.get("kind") or "fact"
        out[kind] = out.get(kind, 0) + 1
    return out


class WebApi:
    def __init__(self, plugin):
        self.plugin = plugin
        self.root = plugin.root

    # ------------------------------------------------------------ 注册

    def register(self) -> bool:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            logger.warning(LOG + "当前 AstrBot 版本不支持插件 Web API，管理页不可用")
            return False

        routes = [
            ("overview", self.overview, ["GET"], "总览"),
            ("sessions", self.sessions, ["GET"], "会话列表"),
            ("history", self.history, ["GET"], "历史记录"),
            ("settings", self.get_settings, ["GET"], "读取设置"),
            ("settings/save", self.save_settings, ["POST"], "保存设置"),
            ("facts", self.facts_list, ["GET"], "事实列表"),
            ("fact", self.fact_detail, ["GET"], "事实详情"),
            ("facts/refine", self.facts_refine, ["GET", "POST"], "提炼事实"),
            ("facts/status", self.facts_status, ["GET"], "提炼进度"),
            ("summary", self.summary_get, ["GET"], "查看前情纪要"),
            ("summary/build", self.summary_build, ["GET", "POST"], "生成前情纪要"),
            ("summary/status", self.summary_status, ["GET"], "纪要进度"),
            ("export", self.export, ["GET", "POST"], "打包全量数据"),
            # 单段路径：子路径在某些宿主版本上匹配不到，这里保留两种写法
            ("sessionexport", self.session_export, ["GET", "POST"], "下载选中的会话"),
            ("session/export", self.session_export, ["GET", "POST"], "下载选中的会话"),
            ("download", self.download, ["GET"], "下载导出包"),
            ("import/check", self.import_check, ["GET", "POST"], "检查备份包"),
            ("import/path", self.import_path, ["GET", "POST"], "按路径恢复"),
            ("import/upload", self.import_upload, ["POST"], "上传并恢复"),
            ("blob", self.blob, ["GET"], "附件预览"),
        ]
        ok = 0
        failed = []
        for path, handler, methods, desc in routes:
            try:
                register(PREFIX + path, handler, methods, desc)
                ok += 1
            except Exception as exc:
                failed.append(path)
                logger.warning(LOG + f"注册 {path} 失败: {exc}")
        logger.info(LOG + f"WebUI 已注册 {ok}/{len(routes)} 个接口"
                    + (f"，失败: {', '.join(failed)}" if failed else ""))
        return ok > 0

    # ------------------------------------------------------------ 总览

    async def overview(self):
        try:
            vault_stats = self.plugin.vault.stats()
            media_stats = self.plugin.media.stats()
            digest_stats = self.plugin.summaries.stats()
            sessions = self.plugin.index.sessions(limit=500)
            return json_response({
                "events": vault_stats["events"],
                "days": vault_stats["days"],
                "by_origin": vault_stats["by_origin"],
                "indexed": self.plugin.index.count(),
                "facts": len(self.plugin.facts.all_facts()),
                "summaries": digest_stats["sessions"],
                "summary_chars": digest_stats["chars"],
                "media_files": media_stats["files"],
                "media_bytes": media_stats["bytes"],
                "sessions": len(sessions),
                "private_sessions": sum(1 for s in sessions if s["kind"] == "private"),
                "group_sessions": sum(1 for s in sessions if s["kind"] == "group"),
                "root": self.root,
                "astrbot_data_root": self.plugin.astrbot_data_root(),
                "backup_astrbot_data": bool(self.plugin._cfg("backup_astrbot_data")),
                "settings": self.plugin.settings.effective({
                    "trigger_ratio": self.plugin._cfg("trigger_ratio"),
                    "total_ratio": self.plugin._cfg("total_ratio"),
                }),
            })
        except Exception as exc:
            logger.warning(LOG + f"overview 失败: {exc}")
            return error_response(str(exc))

    async def sessions(self):
        try:
            items = self.plugin.index.sessions(limit=500)
            states = self.plugin.states.all_states()
            for item in items:
                state = states.get(_state_key(item["session"]), {})
                item["usage"] = state.get("last_usage") or {}
                item["trim_count"] = state.get("trim_count", 0)
            return json_response({"items": items})
        except Exception as exc:
            return error_response(str(exc))

    # ------------------------------------------------------------ 历史

    async def history(self):
        try:
            session = request.query.get("session", "", type=str)
            offset = request.query.get("offset", 0, type=int) or 0
            offset = max(0, offset)
            # limit 默认 0 = 整段拉完。面板没有「加载更多」了，
            # 一次性给全，滚动条才能直接拖到底。
            limit = request.query.get("limit", 0, type=int)
            if limit is None:
                limit = 0
            limit = int(limit)
            if limit > 0:
                limit = min(limit, 20000)     # 正数才当分页用
            # 面板默认从最早开始读，像翻聊天记录；API 仍可要最新优先
            newest = request.query.get("newest_first", "", type=str)
            newest_first = str(newest).lower() in ("1", "true", "yes")

            data = self.plugin.index.history(
                session, offset=offset, limit=limit, newest_first=newest_first)
            fps = {item["fp"] for item in data["items"]}
            media_map = self.plugin.media.records_for(fps)
            for item in data["items"]:
                item["media"] = media_map.get(item["fp"], [])
            data["offset"] = offset
            data["limit"] = limit
            data["session"] = session
            data["newest_first"] = newest_first
            return json_response(data)
        except Exception as exc:
            logger.warning(LOG + f"history 失败: {exc}")
            return error_response(str(exc))

    # ------------------------------------------------------------ 设置

    async def get_settings(self):
        try:
            values = self.plugin.settings.effective({
                "trigger_ratio": self.plugin._cfg("trigger_ratio"),
                "total_ratio": self.plugin._cfg("total_ratio"),
                "recall_limit": self.plugin._cfg("recall_limit"),
                "map_days": self.plugin._cfg("map_days"),
                "dry_run": self.plugin._cfg("dry_run"),
            })
            return json_response({"values": values, "ranges": settings_limits()})
        except Exception as exc:
            return error_response(str(exc))

    async def save_settings(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                payload = {}
            values = payload.get("values") if "values" in payload else payload
            if not isinstance(values, dict) or not values:
                # 兼容 bridge 只支持 GET 的版本：参数走 query
                values = {}
                for key in ("trigger_ratio", "total_ratio",
                            "recall_limit", "map_days", "dry_run"):
                    raw = request.query.get(key, None)
                    if raw is not None and raw != "":
                        values[key] = raw
            if not values:
                return error_response("没有收到任何设置项")
            merged = self.plugin.settings.save(values)
            # 只把这次真的提交上来的项写回插件配置，别把 settings.json 里的旧值
            # 顺手带过去覆盖设置页刚改的东西
            submitted = {key: merged[key] for key in values if key in merged}
            applied = self.plugin.apply_settings(submitted)
            logger.info(LOG + f"设置已更新: {applied or submitted}")
            return json_response({
                "values": self.plugin.settings.effective({
                    "trigger_ratio": self.plugin._cfg("trigger_ratio"),
                    "total_ratio": self.plugin._cfg("total_ratio"),
                    "recall_limit": self.plugin._cfg("recall_limit"),
                    "map_days": self.plugin._cfg("map_days"),
                    "dry_run": self.plugin._cfg("dry_run"),
                }),
                "message": "已保存，下一轮对话生效",
            })
        except Exception as exc:
            return error_response(str(exc))

    # ------------------------------------------------------------ 事实

    async def facts_list(self):
        """人物卡列表：一条事实一行，最近更新的在前。"""
        try:
            items = extract.summaries(self.plugin.facts)
            return json_response({
                "items": items,
                "count": len(items),
                "by_kind": _count_kinds(items),
            })
        except Exception as exc:
            logger.warning(LOG + f"facts 失败: {exc}")
            return error_response(str(exc))

    async def fact_detail(self):
        """单条事实：版本历史 + 每条出处对应的原话。"""
        try:
            fact_id = (request.query.get("id", "", type=str) or "").strip()
            if not fact_id:
                return error_response("缺少 id")
            data = extract.detail(self.plugin.facts, fact_id,
                                  resolve=self.plugin.index.get)
            if not data:
                return error_response("没有这条事实，可能已被重新提炼替换掉了")
            return json_response(data)
        except Exception as exc:
            logger.warning(LOG + f"fact 失败: {exc}")
            return error_response(str(exc))

    async def facts_refine(self):
        """手动全量提炼 —— 和 /allmem_reextract 走同一条路。"""
        try:
            info = self.plugin.start_refine("__web__", reason="web")
            if not info.get("ok"):
                return error_response(info.get("error") or "启动失败")
            return json_response(info)
        except Exception as exc:
            logger.warning(LOG + f"提炼启动失败: {exc}")
            return error_response(str(exc))

    async def facts_status(self):
        """提炼进度：面板轮询这里，跑完自己刷新列表。"""
        try:
            state = self.plugin.states.load("__web__")
            last = state.get("last_refine") or {}
            if not last:
                # 从 QQ 用 /allmem_reextract 起的任务只会写在自己那个会话的 state 里
                last = self._newest_refine()
            return json_response({
                "refining": any(bool(v) for v in self.plugin._refining.values()),
                "facts": len(self.plugin.facts.all_facts()),
                "window": self.plugin._window(),
                "chunk_chars": int(self.plugin._cfg("extract_chunk_chars") or 6000),
                "last_refine": last,
            })
        except Exception as exc:
            return error_response(str(exc))

    def _newest_refine(self) -> dict:
        """哪份 state 里记着最近一次提炼，就拿哪份。"""
        newest: dict = {}
        try:
            states = self.plugin.states.all_states()
        except Exception:
            return newest
        for data in states.values():
            item = (data or {}).get("last_refine") or {}
            if item.get("at") and item["at"] >= str(newest.get("at") or ""):
                newest = item
        return newest

    # ------------------------------------------------------------ 前情纪要

    async def summary_get(self):
        """某个会话的纪要全文（历史面板里点「纪要」看的就是它）。"""
        try:
            session = request.query.get("session", "", type=str) or ""
            if not session:
                return error_response("缺少 session")
            data = self.plugin.summaries.load(session)
            if not data:
                return json_response({
                    "session": session, "exists": False, "text": "",
                    "chars": 0, "updated_at": "", "covered_until": "",
                    "version": 0,
                })
            text = str(data.get("text") or "")
            return json_response({
                "session": session,
                "exists": bool(text),
                "text": text,
                "chars": len(text),
                "updated_at": data.get("updated_at", ""),
                "covered_until": data.get("covered_until", ""),
                "version": int(data.get("version", 0) or 0),
            })
        except Exception as exc:
            logger.warning(LOG + f"summary 读取失败: {exc}")
            return error_response(str(exc))

    async def summary_build(self):
        """生成/更新纪要：和 /allmem_summary 走同一条路。"""
        try:
            session = request.query.get("session", "", type=str) or ""
            if not session:
                return error_response("缺少 session（先选中一个会话）")
            info = self.plugin.start_summary(session, reason="web")
            if not info.get("ok"):
                return error_response(info.get("error") or "启动失败")
            return json_response(info)
        except Exception as exc:
            logger.warning(LOG + f"纪要启动失败: {exc}")
            return error_response(str(exc))

    async def summary_status(self):
        """纪要进度 + 总览（面板轮询这里）。"""
        try:
            stats = self.plugin.summaries.stats()
            return json_response({
                "building": any(bool(v) for v in self.plugin._summarizing.values()),
                "sessions": stats["sessions"],
                "chars": stats["chars"],
            })
        except Exception as exc:
            return error_response(str(exc))

    # ------------------------------------------------------------ 导出

    async def export(self):
        try:
            # 可能几个 GB，扔到线程里跑，别卡住 Dashboard 的事件循环
            info = await asyncio.to_thread(self._build_zip)
            return json_response(info)
        except Exception as exc:
            logger.warning(LOG + f"导出失败: {exc}")
            return error_response(f"打包失败：{exc}")

    def _targets(self) -> list[tuple]:
        """要打包的 (源目录, zip 内前缀, 该源要跳过的子树)。"""
        return self.plugin.export_targets()

    def _build_zip(self) -> dict:
        # 打包和清理都交给插件侧统一实现，WebUI 和定时任务共用一套逻辑
        return self.plugin.run_export()

    async def session_export(self):
        """
        导出勾选的会话：一个会话一个文件，多个打一个 zip。

        参数两种都给：POST body {sessions:[...]} / GET ?sessions=a|b| 或 ?session=a
        """
        try:
            sessions: list[str] = []
            try:
                payload = await request.json(default={})
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                raw = payload.get("sessions")
                if isinstance(raw, list):
                    sessions = [str(item) for item in raw]
                elif isinstance(raw, str) and raw:
                    sessions = raw.split("|")
                elif payload.get("session"):
                    sessions = [str(payload["session"])]

            if not sessions:
                raw = request.query.get("sessions", "", type=str) or ""
                if raw:
                    sessions = raw.split("|")
                else:
                    single = request.query.get("session", "", type=str) or ""
                    if single:
                        sessions = [single]

            sessions = [s for s in dict.fromkeys(sessions) if s]
            if not sessions:
                return error_response("先勾选一个会话")

            info = await asyncio.to_thread(self.plugin.run_session_export, sessions)
            return json_response(info)
        except Exception as exc:
            logger.warning(LOG + f"会话导出失败: {exc}")
            return error_response(f"导出失败：{exc}")

    async def download(self):
        try:
            name = os.path.basename(request.query.get("name", "", type=str) or "")
            if not name.lower().endswith((".zip", ".jsonl")):
                return error_response("文件名不合法")
            path = os.path.join(self.root, "exports", name)
            if not os.path.isfile(path):
                return error_response("导出包不存在，请重新生成")
            return file_response(path, filename=name,
                                 content_type="application/zip")
        except Exception as exc:
            return error_response(str(exc))

    # ------------------------------------------------------------ 导入

    async def import_check(self):
        """先看包里有什么，不动任何数据。"""
        try:
            path = await self._import_target()
            if not path:
                return error_response("需要提供备份包路径")
            info = restore.inspect_zip(path)
            return json_response(info)
        except Exception as exc:
            return error_response(str(exc))

    async def import_path(self):
        """按服务器上的绝对路径恢复（大文件推荐走这个）。"""
        try:
            path = await self._import_target()
            if not path:
                return error_response("需要提供备份包路径")
            if not os.path.isfile(path):
                return error_response(f"文件不存在：{path}")

            info = restore.inspect_zip(path)
            if not info["ok"]:
                return error_response(info["error"] or "无法识别这个压缩包")

            result = await asyncio.to_thread(
                restore.restore_zip, path, self.root,
                self.plugin.vault, self.plugin.index)
            return json_response(await self._finish_import(result))
        except Exception as exc:
            logger.warning(LOG + f"导入失败: {exc}")
            return error_response(f"导入失败：{exc}")

    async def import_upload(self):
        """浏览器上传备份包后恢复。"""
        try:
            files = await request.files()
            upload = files.get("file") if files else None
            if upload is None or not hasattr(upload, "save"):
                return error_response("没有收到文件")

            name = os.path.basename(str(getattr(upload, "filename", "") or "backup.zip"))
            if not name.lower().endswith(".zip"):
                name += ".zip"
            imports = os.path.join(self.root, "imports")
            os.makedirs(imports, exist_ok=True)
            target = os.path.join(imports, name)
            await upload.save(target)

            info = restore.inspect_zip(target)
            if not info["ok"]:
                return error_response(info["error"] or "无法识别这个压缩包")

            result = await asyncio.to_thread(
                restore.restore_zip, target, self.root,
                self.plugin.vault, self.plugin.index)
            result["source"] = target
            return json_response(await self._finish_import(result))
        except Exception as exc:
            logger.warning(LOG + f"上传导入失败: {exc}")
            return error_response(f"导入失败：{exc}")

    async def _import_target(self) -> str:
        path = request.query.get("path", "", type=str) or ""
        if not path:
            payload = await request.json(default={})
            if isinstance(payload, dict):
                path = str(payload.get("path") or "")
        return path.strip().strip('"')

    async def _finish_import(self, result: dict) -> dict:
        """导入后重建索引，顺便把正文/时间修一遍，再让会话进入接管模式。"""
        if result.get("ok"):
            try:
                info = await asyncio.to_thread(
                    self.plugin.finalize_index, bool(result.get("index_copied")))
                result.update(info)
            except Exception as exc:
                result["index_rebuilt"] = 0
                result["index_error"] = str(exc)

        sessions = list(result.get("sessions") or [])
        if result.get("ok") and sessions and self.plugin._cfg("takeover_after_restore"):
            try:
                result["takeover_sessions"] = self.plugin.enable_takeover(sessions)
            except Exception as exc:
                result["takeover_sessions"] = 0
                result["takeover_error"] = str(exc)

        if result.get("ok") and result.get("personas_added") \
                and self.plugin._cfg("restore_personas_to_astrbot"):
            try:
                result["personas_in_astrbot"] = \
                    await self.plugin.sync_personas_to_astrbot()
            except Exception as exc:
                result["personas_in_astrbot"] = {"ok": False, "error": str(exc)}

        logger.info(LOG + f"导入完成: {result}")
        return result

    # ------------------------------------------------------------ 附件预览

    async def blob(self):
        try:
            rel = request.query.get("path", "", type=str) or ""
            rel = rel.replace("\\", "/").strip()
            if not rel.startswith("blobs/") or ".." in rel:
                return error_response("路径不合法")
            path = os.path.abspath(os.path.join(self.root, rel))
            root_abs = os.path.abspath(self.root)
            if not path.startswith(root_abs + os.sep) or not os.path.isfile(path):
                return error_response("文件不存在")
            return file_response(path)
        except Exception as exc:
            return error_response(str(exc))
