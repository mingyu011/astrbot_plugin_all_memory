"""
extract.py —— 第二层：提炼层

把「经历」变成「认知」，但守住一条底线：
    **每条提炼都必须带 source_fp，指回原始层。**

由此得到四个能力：
    可回溯（点开就知道是哪句话）
    可删版本（抽错了删掉重来，原文不动）
    可重算（换更强的模型，全量重跑）
    可审计（不会坚持一件没发生过的事）

这一层是可以修改、可以删除的。原始层才是唯一事实来源。
"""

from __future__ import annotations

import os
import json
import time
import random
import shutil
import re
import difflib
from datetime import datetime, timedelta, timezone

from .ids import safe_component
from .vault import fp_of, now
from .tokens import estimate_tokens

MAX_CLAIM_CHARS = 120    # 单条事实长度上限

# 事实分类与权重：身份类永远排在最前面，一次性的小事排最后
KIND_WEIGHTS = {
    "identity": 100,      # 名字、年龄、职业、身份
    "relationship": 80,   # 和谁亲近、和谁有过节
    "preference": 70,     # 怕黑、讨厌说教、喜欢被叫小名
    "fact": 50,           # 其他长期事实
    "event": 20,          # 一次性的事件、临时安排
}


def normalize_kind(value) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in KIND_WEIGHTS else "fact"


# 一条事实能被谁看见：
#   session —— 只在出处那个会话里认（群里的临时角色、本群约定）
#   people  —— 关于某个人的长期事实（职业、称呼、长期偏好），凡是他出现的会话都能用
SCOPES = ("session", "people")


def normalize_scope(value) -> str:
    scope = str(value or "").strip().lower()
    return scope if scope in SCOPES else "session"


def normalize_subject(value) -> str:
    """事实的"主角"：发送者 ID。拿不准就留空，留空按出处会话兜底。"""
    return str(value or "").strip()[:64]


def kind_weight(kind: str) -> int:
    return KIND_WEIGHTS.get(normalize_kind(kind), KIND_WEIGHTS["fact"])

EXTRACT_PROMPT = """你在整理一段对话里的长期记忆。

只提取「换个时间、换个场合依然成立」的事实，例如：
- 身份与背景（在考研、独居、养了一只猫）
- 稳定偏好（怕黑、喜欢被叫小名、讨厌说教）
- 重要关系（和谁关系好、和谁有过节）
- 长期目标与进行中的事

不要提取：一次性的情绪、当天的天气、临时的安排、你自己的回复内容。

输出严格 JSON 数组，每条一个对象：
[{"claim": "她怕黑，睡觉要留灯", "kind": "preference", "sources": [语句序号]}]

规则：
- claim 用第三人称陈述句，不超过 40 字
- kind 只能是：identity（身份）/ relationship（关系）/ preference（偏好）/ fact（其他长期事实）/ event（一次性事件）
- sources 填这句话来自下面哪几条（填序号数字）
- 最多 8 条，宁缺毋滥
- 没有任何值得记的，输出 []

以下是已有事实，不要重复提取：
{existing}

以下是要整理的对话（每行格式 序号|角色|内容）：
{lines}
"""


class FactStore:
    """facts/<id>/ 目录，每条事实一个 JSON，可多版本。"""

    def __init__(self, root: str, uid: str = "default"):
        self.root = root
        self.uid = safe_component(uid, fallback="default")
        self.dir = os.path.join(root, "facts", self.uid)
        self.archive_root = os.path.join(root, "facts_archive", self.uid)
        self.staging = os.path.join(root, "facts_staging", self.uid)
        os.makedirs(self.dir, exist_ok=True)

    # ------------------------------------------------------------ 整体替换

    def replace_all(self, items: list[dict], model: str = "",
                    scope: str = "session",
                    prompt_version: str = "") -> int:
        """
        用一次完整提炼的结果替换整个 facts/。

        因为提炼扫的是这个 id 的全量库，输出本来就覆盖上次的一切，所以不需要合并。
        但必须保证安全：

          1. 先写到 facts_staging/
          2. 旧目录整体移到 facts_archive/<id>/<时间戳>/
          3. 再把 staging 换上去
          4. 中途失败 → 把旧目录移回来，什么都不变

        scope / subject：一条事实属于"谁"、作用域是什么。0.9.17 起事实按 id 分文件夹，
        这个字段保留但不再承担跨会话可见性；以后接群聊再启用。
        返回写入条数。
        """
        staging = self.staging
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)

        stamp = now().isoformat(timespec="seconds")
        written = 0
        for item in items:
            claim = str(item.get("claim", "")).strip()
            sources = list(item.get("source_fp") or [])
            if not claim or not sources:
                continue
            payload = {
                "id": _new_id(),
                "scope": normalize_scope(item.get("scope") or scope),
                "subject": normalize_subject(item.get("subject")),
                "deleted": False,
                "versions": [{
                    "version": 1,
                    # 不截断：模型写了多少就存多少（prompt 里已经要求 40 字以内）
                    "claim": claim,
                    "kind": normalize_kind(item.get("kind")),
                    "source_fp": sources,
                    "extracted_at": stamp,
                    "model": model,
                    "prompt_version": prompt_version,
                }],
            }
            path = os.path.join(staging, f"{payload['id']}.json")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            written += 1

        archive = os.path.join(self.archive_root, _archive_stamp())
        os.makedirs(self.archive_root, exist_ok=True)

        moved_old = False
        if os.path.isdir(self.dir):
            if os.listdir(self.dir):
                try:
                    shutil.move(self.dir, archive)
                    moved_old = True
                except OSError:
                    # 归档失败就不替换，保住现状
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            else:
                # 空目录直接删掉：否则 shutil.move 会把 staging 塞进它里面
                shutil.rmtree(self.dir, ignore_errors=True)

        try:
            shutil.move(staging, self.dir)
        except OSError:
            # 换上去失败 → 把旧目录移回来
            if moved_old and os.path.isdir(archive):
                shutil.rmtree(self.dir, ignore_errors=True)
                shutil.move(archive, self.dir)
            raise

        return written

    # ------------------------------------------------------------ 读

    def _path(self, fact_id: str) -> str:
        return os.path.join(self.dir, f"{fact_id}.json")

    def load(self, fact_id: str) -> dict | None:
        path = self._path(fact_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def all_facts(self, include_deleted: bool = False) -> list[dict]:
        facts = []
        if not os.path.isdir(self.dir):
            return facts
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".json"):
                continue
            fact = self.load(name[:-5])
            if not fact:
                continue
            if not include_deleted and fact.get("deleted"):
                continue
            facts.append(fact)
        return facts

    def current_claims(self) -> list[str]:
        out = []
        for fact in self.all_facts():
            versions = fact.get("versions") or []
            if versions:
                out.append(str(versions[-1].get("claim", "")))
        return [c for c in out if c]

    def latest(self) -> dict[str, dict]:
        """id -> 当前版本内容（含 kind / scope / subject）。"""
        items: list[dict] = []
        for fact in self.all_facts():
            versions = fact.get("versions") or []
            if versions:
                item = dict(versions[-1])
                item["id"] = fact["id"]
                item["kind"] = normalize_kind(item.get("kind"))
                item["scope"] = normalize_scope(fact.get("scope"))
                item["subject"] = normalize_subject(fact.get("subject"))
                items.append(item)

        # 旧数据里已经存在的近义事实，先在读取层合并掉，
        # 这样即使还没跑 repair，注入到模型的卡也是干净的。
        items.sort(key=lambda item: (str(item.get("extracted_at", "")),
                                     str(item.get("id", ""))))
        chosen: list[dict] = []
        for item in items:
            duplicate = False
            for other in chosen:
                exact = _normalize(item.get("claim", "")) == _normalize(
                    other.get("claim", ""))
                similar = (item["kind"] == other["kind"]
                           and claim_similarity(item.get("claim", ""),
                                                other.get("claim", ""))
                           >= CLAIM_SIMILARITY)
                if exact or similar:
                    duplicate = True
                    if other["kind"] == "fact" and item["kind"] != "fact":
                        other["kind"] = item["kind"]
                    break
            if not duplicate:
                chosen.append(item)
        return {item["id"]: item for item in chosen}

    # ------------------------------------------------------------ 写

    def _write(self, fact: dict) -> None:
        path = self._path(fact["id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(fact, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _find_by_claim(self, claim: str, kind: str = "") -> dict | None:
        target = _normalize(claim)
        wanted_kind = normalize_kind(kind) if kind else ""
        best: tuple[float, dict | None] = (0.0, None)
        for fact in self.all_facts():
            versions = fact.get("versions") or []
            if not versions:
                continue
            current = versions[-1]
            if _normalize(current.get("claim", "")) == target:
                return fact
            if wanted_kind and normalize_kind(current.get("kind")) != wanted_kind:
                continue
            score = claim_similarity(claim, current.get("claim", ""))
            if score > best[0]:
                best = (score, fact)
        if best[1] is not None and best[0] >= CLAIM_SIMILARITY:
            return best[1]
        return None

    def upsert(self, claim: str, source_fps: list[str],
               model: str = "", scope: str = "session",
               kind: str = "fact", prompt_version: str = "") -> str:
        """
        新增或更新一条事实。返回 fact_id。

        同一 claim（或近义 claim）再次出现时，版本号 +1、来源合并，
        不覆盖旧版本。
        """
        claim = claim.strip()
        if not claim:
            return ""
        if len(claim) > MAX_CLAIM_CHARS:
            claim = claim[:MAX_CLAIM_CHARS] + "…"
        kind = normalize_kind(kind)

        stamp = now().isoformat(timespec="seconds")

        existing = self._find_by_claim(claim, kind)
        if existing:
            versions = existing.setdefault("versions", [])
            latest = versions[-1] if versions else {}
            merged = list(dict.fromkeys(list(latest.get("source_fp") or []) + source_fps))
            versions.append({
                "version": int(latest.get("version", 0)) + 1,
                "claim": claim,
                "kind": kind or latest.get("kind", "fact"),
                "source_fp": merged,
                "extracted_at": stamp,
                "model": model,
                "prompt_version": prompt_version,
            })
            existing["deleted"] = False
            self._write(existing)
            return existing["id"]

        fact_id = _new_id()
        self._write({
            "id": fact_id,
            "scope": scope,
            "deleted": False,
            "versions": [{
                "version": 1,
                "claim": claim,
                "kind": kind,
                "source_fp": source_fps,
                "extracted_at": stamp,
                "model": model,
                "prompt_version": prompt_version,
            }],
        })
        return fact_id

    def delete(self, fact_id: str) -> bool:
        """标记删除（保留文件，便于审计与恢复）。"""
        fact = self.load(fact_id)
        if not fact:
            return False
        fact["deleted"] = True
        fact["deleted_at"] = now().isoformat(timespec="seconds")
        self._write(fact)
        return True

    def purge(self) -> int:
        """彻底删除已标记删除的事实。"""
        count = 0
        for fact in self.all_facts(include_deleted=True):
            if fact.get("deleted"):
                try:
                    os.remove(self._path(fact["id"]))
                    count += 1
                except OSError:
                    pass
        return count

    def repair(self) -> dict:
        """
        合并已经存在的近义事实：保留最早的一条，其余标记 deleted 并写入 merged_into。

        不删文件（仍可在面板/审计里看到）；latest() 只返回保留的那条。
        """
        facts = self.all_facts()
        facts.sort(key=lambda fact: (
            str(((fact.get("versions") or [{}])[-1]).get("extracted_at", "")),
            str(fact.get("id", ""))))
        groups: list[list[dict]] = []
        for fact in facts:
            versions = fact.get("versions") or []
            if not versions:
                continue
            kind = normalize_kind(versions[-1].get("kind"))
            claim = versions[-1].get("claim", "")
            target = None
            for group in groups:
                current = (group[0].get("versions") or [{}])[-1]
                if normalize_kind(current.get("kind")) != kind:
                    continue
                if claim_similarity(claim, current.get("claim", "")) >= CLAIM_SIMILARITY:
                    target = group
                    break
            if target is None:
                groups.append([fact])
            else:
                target.append(fact)

        merged = 0
        for group in groups:
            if len(group) < 2:
                continue
            canonical = group[0]
            can_versions = canonical.setdefault("versions", [])
            if not can_versions:
                continue
            latest = can_versions[-1]
            sources = list(latest.get("source_fp") or [])
            for duplicate in group[1:]:
                dup_versions = duplicate.get("versions") or []
                if dup_versions:
                    sources.extend(dup_versions[-1].get("source_fp") or [])
                duplicate["deleted"] = True
                duplicate["merged_into"] = canonical["id"]
                duplicate["repaired_at"] = now().isoformat(timespec="seconds")
                self._write(duplicate)
                merged += 1
            latest["source_fp"] = list(dict.fromkeys(sources))
            canonical["deleted"] = False
            canonical["repaired_at"] = now().isoformat(timespec="seconds")
            self._write(canonical)

        return {"ok": True, "facts": len(facts), "groups": len(groups),
                "merged": merged}


# ------------------------------------------------------------------ 抽取

def build_prompt(events: list[dict], existing_claims: list[str]) -> tuple[str, list[str]]:
    """返回 (prompt, 行号到 fingerprint 的映射)。"""
    lines = []
    fps = []
    for i, ev in enumerate(events, 1):
        text = " ".join(str(ev.get("text", "")).split())
        if not text:
            continue
        if len(text) > 300:
            text = text[:300] + "…"
        lines.append(f"{i}|{ev.get('role', '')}|{text}")
        fps.append(str(ev.get("fp", "")))

    existing = "\n".join(f"- {c}" for c in existing_claims[-60:]) or "（无）"
    prompt = EXTRACT_PROMPT.format(existing=existing, lines="\n".join(lines))
    return prompt, fps


def parse_output(raw: str) -> list[dict]:
    """从模型输出里抠出 JSON 数组，容忍前后废话和 ``` 包裹。"""
    if not raw:
        return []
    text = raw.strip()

    if "```" in text:
        chunks = text.split("```")
        for chunk in chunks:
            body = chunk.strip()
            if body.startswith("json"):
                body = body[4:].strip()
            if body.startswith("["):
                text = body
                break

    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    out = []
    for item in data:
        if isinstance(item, str):
            out.append({"claim": item, "sources": [], "kind": "fact"})
        elif isinstance(item, dict):
            claim = item.get("claim") or item.get("text") or ""
            if isinstance(claim, str) and claim.strip():
                sources = item.get("sources") or item.get("source") or []
                if not isinstance(sources, list):
                    sources = [sources]
                out.append({"claim": claim.strip(), "sources": sources,
                            "kind": normalize_kind(item.get("kind")),
                            "subject": item.get("subject"),
                            "scope": item.get("scope")})
    return out


def resolve_sources(sources: list, fps: list[str]) -> list[str]:
    """把模型给的行号换成真实的 source_fp。行号越界就丢弃。"""
    resolved = []
    for src in sources:
        try:
            idx = int(src)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(fps) and fps[idx - 1]:
            resolved.append(fps[idx - 1])
    return list(dict.fromkeys(resolved))


# ------------------------------------------------------------------ 工具

def _normalize(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def _claim_key(text: str) -> str:
    """归一化到“只比内容”的形态：去标点、统一 User/Bot 称呼。"""
    body = _normalize(text)
    body = re.sub(r"[\s，。！？、；：,.!?;:~…\-—\"'“”‘’（）()\[\]【】{}<>《》/\\|]+",
                  "", body)
    for old, new in (("用户", "主人"), ("对方", "主人"),
                     ("她", "主人"), ("他", "主人"),
                     ("小木", "助手"), ("机器人", "助手")):
        body = body.replace(old, new)
    return body


_GENERIC_CLAIM_FEATURES = {
    "主人", "助手", "喜欢", "的", "了", "是", "人喜", "手是",
}


def claim_similarity(left: str, right: str) -> float:
    """两条 claim 是否在说同一件事；0~1。"""
    a = _claim_key(left)
    b = _claim_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.95

    sequence = difflib.SequenceMatcher(None, a, b).ratio()

    # 中文改写经常换语序/换虚词，字符序列相似度会偏低；
    # 再用 2~3 字片段的重合度补一刀。
    # 只共用“主人/助手/喜欢”这类泛词的（如“喜欢猫/喜欢狗”）不算同一件事。
    def features(text: str) -> set[str]:
        out: set[str] = set()
        for run in re.findall(r"[\u3400-\u9fff]+", text):
            if len(run) == 1:
                out.add(run)
            else:
                for size in (2, 3):
                    for start in range(len(run) - size + 1):
                        out.add(run[start:start + size])
        for word in re.findall(r"[a-z0-9]+", text):
            if len(word) >= 2:
                out.add(word)
        return out

    left_features = features(a)
    right_features = features(b)
    if not left_features or not right_features:
        return sequence
    shared = left_features & right_features
    specific = shared - _GENERIC_CLAIM_FEATURES
    overlap = len(shared) / max(1, min(len(left_features), len(right_features)))

    if len(specific) >= 3 and (overlap >= 0.35 or sequence >= 0.60):
        # 改写但保留同一串具体名词/动作（如“B站分享卡片 / 自动配图插件”）。
        return max(sequence, 0.80)
    if len(specific) < 3:
        # 只共用泛词时宁可放过，别把“喜欢猫/喜欢狗”合成一条。
        return min(sequence, 0.65)
    if len(shared) >= 3 and overlap >= 0.55:
        return max(sequence, overlap)
    return sequence


CLAIM_SIMILARITY = 0.80


def _new_id() -> str:
    return "fact-{}-{:04d}".format(
        datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S"),
        random.randint(0, 9999),
    )


def _archive_stamp() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")


# ------------------------------------------------------------------ 面板用的视图

def summarize(fact: dict) -> dict:
    """
    一条事实的「当前版本」摘要，给面板列表用。

    只取最后一个版本：历史版本留在 versions 里，点开详情才展开。
    """
    versions = fact.get("versions") or []
    current = versions[-1] if versions else {}
    return {
        "id": str(fact.get("id", "")),
        "claim": str(current.get("claim", "")),
        "kind": normalize_kind(current.get("kind")),
        "scope": normalize_scope(fact.get("scope")),
        "subject": normalize_subject(fact.get("subject")),
        "version": int(current.get("version", 1) or 1),
        "versions": len(versions),
        "updated_at": str(current.get("extracted_at", "")),
        "sources": len(current.get("source_fp") or []),
        "model": str(current.get("model", "")),
        "prompt_version": str(current.get("prompt_version", "")),
    }


def summaries(store) -> list[dict]:
    """全部事实的摘要，最近更新的在前。"""
    items = [summarize(fact) for fact in store.all_facts()]
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items


def detail(store, fact_id: str, resolve=None, max_chars: int = 2000) -> dict | None:
    """
    一条事实的完整视图：版本历史 + 当前版本的出处原文。

    resolve(fp) 由调用方注入（通常是 index.get），这样这一层不依赖索引层，
    索引丢了也不会让详情页崩掉——找不到就如实标 found=False。
    """
    fact = store.load(str(fact_id or "").strip())
    if not fact:
        return None

    versions = []
    for ver in fact.get("versions") or []:
        versions.append({
            "version": int(ver.get("version", 0) or 0),
            "claim": str(ver.get("claim", "")),
            "kind": normalize_kind(ver.get("kind")),
            "extracted_at": str(ver.get("extracted_at", "")),
            "model": str(ver.get("model", "")),
            "prompt_version": str(ver.get("prompt_version", "")),
            "source_fp": list(ver.get("source_fp") or []),
        })

    current = versions[-1] if versions else {}
    sources = []
    for fp in current.get("source_fp") or []:
        event = None
        if callable(resolve):
            try:
                event = resolve(fp)
            except Exception:
                event = None
        text = str((event or {}).get("text", ""))
        sources.append({
            "fp": str(fp),
            "found": event is not None,
            "ts": str((event or {}).get("ts", "")),
            "role": str((event or {}).get("role", "")),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        })

    return {
        "id": str(fact.get("id", "")),
        "claim": str(current.get("claim", "")),
        "kind": normalize_kind(current.get("kind")),
        "scope": normalize_scope(fact.get("scope")),
        "subject": normalize_subject(fact.get("subject")),
        "deleted": bool(fact.get("deleted")),
        "updated_at": str(current.get("extracted_at", "")),
        "version": int(current.get("version", 0) or 0),
        "versions": versions,
        "sources": sources,
    }


def visible_facts(facts: list[dict], session: str,
                  subjects=None, source_session=None) -> list[dict]:
    """
    这个会话能看见哪些事实。

    0.9.17 起事实按 id 分文件夹，主路径不再调用本函数（一个 id 里的都可见）；
    保留它是为了以后接群聊时恢复"按人/按群"的过滤。

    subjects：本会话出现过的人（发送者 ID）。
    source_session：指纹 → 会话，用来判断"这条事实是哪个会话里说的"。

    规则：
      1. 有 subject 的，先要求"这个人本会话出现过"，否则一律不给——
         不然 A 的私事会漏到 B 的对话里
      2. people 作用域：只要是他出现的会话，都给（跨会话共享）
      3. session 作用域：还要求出处就在本会话
      4. 没有 subject 的老数据：按出处会话判断；查不到出处就放行（宁多不少）
    """
    subject_set = set(subjects or ())
    source_session = source_session or {}
    out: list[dict] = []

    for fact in facts:
        subject = normalize_subject(fact.get("subject"))
        scope = normalize_scope(fact.get("scope"))
        sources = [str(fp) for fp in (fact.get("source_fp") or []) if fp]

        if subject:
            if subject not in subject_set:
                continue
            if scope == "people" or not sources:
                out.append(fact)
                continue
            if any(source_session.get(fp, session) == session for fp in sources):
                out.append(fact)
            continue

        if not sources:
            out.append(fact)
            continue
        if any(source_session.get(fp, session) == session for fp in sources):
            out.append(fact)

    return out
