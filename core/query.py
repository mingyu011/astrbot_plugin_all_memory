"""
query.py —— 把"这一轮说的话"变成能检索的东西

问题不在语义，而在**整句当关键词**：
    _search_fts 拼的是 MATCH '"整句"'，那是短语查询，
    只有原句一字不差出现过才命中；退回去的 LIKE 也是 %整句%，
    同一句话换个说法就是 0 条。

所以检索前先做三件事：
    拆     —— 按标点/空白切成检索词，去掉虚词
    扩     —— （可选）让模型补同义词，换个说法也能命中
    定时间 —— "昨天""上周""10月20" 解析成日期区间，用于加权
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta

from .ids import safe_component

TZ_OFFSET = 8  # 只用来判断"日期区间"，不参与展示

# 单字虚词：拆出来对检索没帮助，反而到处命中
STOPWORDS = set(
    "我 你 他 她 它 们 的 了 吗 呢 吧 啊 呀 是 在 有 就 都 也 和 跟 说 这 那 不 要 会 能 "
    "个 一 上 下 好 很 太 还 又 把 被 给 得 地 着 过 让 但 而 与 或 如果 因为 所以 可以 "
    "什么 怎么 为什么 哪里 哪儿 几点 多少 哪些 哪个 现在 最近 平时 一次 时候 那天 "
    "今天 昨天 知道 记得 说过 一个 有没有 是不是 什么 怎么 为什么".split()
)

_SPLIT_RE = re.compile(r"[\s，。！？、；：,\.\!\?;:~…\-—\"'“”‘’（）()\[\]【】{}<>《》/\\|]+")
_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
_MD_RE = re.compile(r"(\d{1,2})月(\d{1,2})(?:[日号])?")

RELATIVE = {
    "今天": 0, "今日": 0, "昨晚": -1, "昨天": -1, "昨日": -1,
    "前天": -2, "大前天": -3, "明天": 1,
}

# 这些词在检索里太泛，命中它们不代表命中问题；只在还有别的词时丢弃。
GENERIC_TERMS = {
    "小木", "主人", "用户", "助手", "什么", "怎么", "为什么", "最近",
    "平时", "现在", "时候", "那天", "一次", "知道", "记得", "说过",
}

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def normalize(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _bigrams(chunk: str, limit: int = 10) -> list[str]:
    """两字窗口，补 trigram 覆盖不到的短词。"""
    chars = [c for c in chunk if "\u4e00" <= c <= "\u9fff"]
    if len(chars) < 2:
        return []
    grams: list[str] = []
    for start in range(0, len(chars) - 1):
        gram = "".join(chars[start:start + 2])
        if gram not in grams:
            grams.append(gram)
        if len(grams) >= limit:
            break
    return grams


def _trigrams(chunk: str, limit: int = 4) -> list[str]:
    """
    长句没有标点时，整段拿去查是查不到的。切成三字窗口。

    FTS5 用的就是 trigram 分词器，三字短语能直接命中；
    两字太泛（"今天"到处都有），四字以上又容易错位。
    """
    chars = [c for c in chunk if "\u4e00" <= c <= "\u9fff"]
    if len(chars) < 3:
        return []
    grams: list[str] = []
    step = max(1, (len(chars) - 2) // max(1, limit))
    for start in range(0, len(chars) - 2, step):
        gram = "".join(chars[start:start + 3])
        if gram not in grams:
            grams.append(gram)
        if len(grams) >= limit:
            break
    return grams


def split_terms(text: str, limit: int = 8) -> list[str]:
    """
    粗拆词：按标点切段，去掉虚词和单字；长段再切三字窗口。

    中文没有分词器，这里不追求语言学正确——检索词是"用来查的原料"，
    宁可多切几段，也不要整句当关键词。
    """
    body = normalize(text)
    # “B 站”“A7M4 相机”这种空格会把一个词拆成两个；先粘回去再切。
    body = re.sub(r"(?<=[A-Za-z0-9])\s+(?=[\u3400-\u9fff])", "", body)
    body = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[A-Za-z0-9])", "", body)
    chunks = [c.strip() for c in _SPLIT_RE.split(body) if c.strip()]
    terms: list[str] = []
    seen = set()

    def push(value: str):
        value = value.strip()
        if len(value) < 2 or value in STOPWORDS:
            return
        if value in seen:
            return
        seen.add(value)
        terms.append(value[:24])

    for chunk in chunks:
        # 很长的中文整句当 phrase 基本不会命中；英文/型号/编号整词必须保留。
        has_cjk = any("\u3400" <= ch <= "\u9fff" for ch in chunk)
        if len(chunk) <= 4 or not has_cjk:
            push(chunk)
        for word in _ASCII_WORD_RE.findall(chunk):
            push(word)
        for gram in _bigrams(chunk):
            push(gram)
        # 一段太长时，整段当关键词基本不会有命中，补几个三字窗口。
        if len(chunk) >= 4:
            for gram in _trigrams(chunk):
                push(gram)

    if len(terms) > 2:
        filtered = [t for t in terms if t not in GENERIC_TERMS]
        if filtered:
            terms = filtered
    return terms[:max(1, limit)]


def time_window(text: str, now: datetime | None = None) -> tuple[str, str]:
    """
    从这句话里猜一个日期区间，返回 (起, 止) 的 YYYY-MM-DD；猜不出来返回 ("","")。

    只做"加分"用，不做过滤——猜错了顶多是没加分，不会把正确的排除掉。
    """
    body = normalize(text)
    base = (now or datetime.now()) - timedelta(hours=TZ_OFFSET - 8)

    for word, delta in RELATIVE.items():
        if word in body:
            day = (base + timedelta(days=delta)).strftime("%Y-%m-%d")
            return day, day

    if "上周" in body or "上个星期" in body:
        start = base - timedelta(days=base.weekday() + 7)
        return start.strftime("%Y-%m-%d"), (start + timedelta(days=6)).strftime("%Y-%m-%d")
    if "本周" in body or "这周" in body or "这星期" in body:
        start = base - timedelta(days=base.weekday())
        return start.strftime("%Y-%m-%d"), base.strftime("%Y-%m-%d")
    if "上个月" in body:
        first = base.replace(day=1)
        last_prev = first - timedelta(days=1)
        return last_prev.replace(day=1).strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d")
    if "本月" in body or "这个月" in body:
        return base.replace(day=1).strftime("%Y-%m-%d"), base.strftime("%Y-%m-%d")

    match = _DATE_RE.search(body)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            stamp = datetime(year, month, day).strftime("%Y-%m-%d")
            return stamp, stamp
        except ValueError:
            return "", ""

    match = _MD_RE.search(body)
    if match:
        month, day = (int(g) for g in match.groups())
        try:
            stamp = datetime(base.year, month, day).strftime("%Y-%m-%d")
            return stamp, stamp
        except ValueError:
            return "", ""

    return "", ""


_EARLIEST_WORDS = ("第一次", "第一次见", "最初", "最早", "开头", "刚认识", "初次",
                   "一开始", "刚开始", "我们认识", "第一句话", "第一天")


def is_earliest_intent(text: str) -> bool:
    """
    这句话是不是在问"最早/第一次"。

    有这种意图时，光靠检索词不保险（"第一次见面"未必出现在原句里），
    直接把最早那天的原话塞进记忆卡，别让模型凭印象编。
    """
    body = normalize(text)
    return any(word in body for word in _EARLIEST_WORDS)


_YES_NO_MARKERS = ("吗", "是否", "有没有", "是不是")

# 问句里的“话题词”提取：先去掉这些人称、动词、疑问词，剩下的才是
# “咖啡 / 猫 / 相机”这种真正需要证据的名词。
_TOPIC_DROP = set(
    "小木 主人 用户 助手 我 你 他 她 它 我们 你们 他们 她们 "
    "喜欢 爱 讨厌 养过 养 喝 吃 去 有 会 能 想 要 "
    "记得 知道 是不是 有没有 是否 吗 呢 吧 啊 呀 了 的 过".split()
)


def is_yes_no(text: str) -> bool:
    """是不是一个 yes/no 问句（“喜欢咖啡吗 / 养过猫吗”）。"""
    body = normalize(text)
    return any(marker in body for marker in _YES_NO_MARKERS)


def topic_terms(text: str, limit: int = 6) -> list[str]:
    """
    粗抽 yes/no 问句里的话题词。

    例：
      “小木喜欢喝咖啡吗” → ["咖啡"]
      “小木养过猫吗”     → ["猫"]

    只用于“命中必须包含话题词”的证据过滤；抽不出来就返回空，
    调用方不做过滤，宁可少拦也不误杀。
    """
    body = normalize(text).replace("？", " ").replace("?", " ")
    for word in sorted(_TOPIC_DROP, key=len, reverse=True):
        body = body.replace(word, " ")
    chunks = [chunk.strip() for chunk in _SPLIT_RE.split(body) if chunk.strip()]
    out: list[str] = []
    for chunk in chunks:
        for particle in ("吗", "呢", "吧", "啊", "呀", "了", "的", "过"):
            chunk = chunk.replace(particle, "")
        chunk = chunk.strip()
        if not chunk or chunk in STOPWORDS:
            continue
        if chunk not in out:
            out.append(chunk[:24])
        if len(out) >= max(1, int(limit or 6)):
            break
    return out


def build_terms(text: str, expansions: list[str] | None = None,
                limit: int = 8) -> list[str]:
    """
    检索词 = 原文拆出来的 + 模型扩出来的同义词。

    扩充词只是"多想几个说法"，去重后一起参与融合，
    命中与否交给打分，不在这里做取舍。
    """
    terms = split_terms(text, limit=limit)
    for extra in expansions or []:
        extra = normalize(extra)
        if len(extra) < 2 or extra in STOPWORDS:
            continue
        if extra in terms:
            continue
        terms.append(extra[:24])
        if len(terms) >= limit + 4:
            break
    return terms


class QueryCache:
    """
    同一个说法不重复调用模型扩词。

    检索是每轮都跑的，扩词要是一次小模型调用，不做缓存会白烧钱。
    key 里带上模板版本，改提示词自动失效。
    """

    VERSION = "v1"

    def __init__(self, root: str, uid: str = "default"):
        self.dir = os.path.join(root, "cache", "query",
                                safe_component(uid, fallback="default"))
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, text: str) -> str:
        digest = hashlib.sha1(f"{self.VERSION}|{normalize(text)}"
                              .encode("utf-8")).hexdigest()
        return os.path.join(self.dir, digest + ".json")

    def get(self, text: str) -> list[str] | None:
        try:
            with open(self._path(text), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return [str(x) for x in data] if isinstance(data, list) else None

    def put(self, text: str, terms: list[str]) -> None:
        path = self._path(text)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(terms, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass


EXPAND_PROMPT = """把下面这句话变成检索用的关键词，用来在聊天记录里找相关内容。

规则：
- 输出 3~5 个词，用逗号分隔，不要解释
- 包含原句里最关键的实词
- 再补 2~3 个"意思一样但字不同"的说法（同义词、口语说法），方便换个说法也搜得到
- 不要虚词、不要整句

这句话：{query}

关键词："""


def parse_expansions(raw: str) -> list[str]:
    """把模型输出收拾成词表，容忍逗号/顿号/换行/编号。"""
    text = str(raw or "").strip()
    if not text:
        return []
    text = text.splitlines()[0] if len(text.splitlines()) > 1 else text
    parts = re.split(r"[,，、;；\s]+", text)
    out: list[str] = []
    for part in parts:
        part = part.strip().strip("。.!！:：\"'“”")
        part = re.sub(r"^[\d\-\.\)、]+", "", part)
        if len(part) < 2:
            continue
        if part in STOPWORDS:
            continue
        if part not in out:
            out.append(part[:24])
    return out[:5]
