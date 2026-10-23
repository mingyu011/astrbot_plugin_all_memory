"""
core 模块自测。既能用 pytest 跑，也能直接 python tests/test_core.py。

只测不依赖 AstrBot 运行时的部分（tokens / vault / index / sync / budget /
extract / refine / inject / media / settings / importer）。
"""

from __future__ import annotations

import os
import sys
import json
import base64
import shutil
import sqlite3
import asyncio
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import budget, extract, importer, inject  # noqa: E402
from core import backup  # noqa: E402
from core import media as media_mod  # noqa: E402
from core import pending as pending_mod  # noqa: E402
from core import refine as refine_mod  # noqa: E402
from core import restore as restore_mod  # noqa: E402
from core import settings as settings_mod  # noqa: E402
from core import summary as summary_mod  # noqa: E402
from core import query as query_mod  # noqa: E402
from core import search as search_mod  # noqa: E402
from core import vectors as vectors_mod  # noqa: E402
from datetime import datetime  # noqa: E402
from core import sync  # noqa: E402
from core.index import Index  # noqa: E402
from core.tokens import Calibrator, estimate_tokens  # noqa: E402
from core.vault import Vault, fp_of  # noqa: E402


def _pick_tmp_base() -> str:
    """
    选一个真的能写子目录的位置。

    有些受限环境（Windows 沙箱、AppContainer）里 tempfile.mkdtemp 建出来的目录
    不允许再写子目录，所以这里实际探测一次，失败就退到插件目录的兄弟位置，
    保证插件目录本身始终保持干净。
    """
    candidates = [
        os.path.join(tempfile.gettempdir(), "allmem-tests"),
        os.path.join(os.path.dirname(ROOT), "_allmem-tests"),
    ]
    for base in candidates:
        probe = os.path.join(base, "probe", "sub")
        try:
            os.makedirs(probe, exist_ok=True)
            with open(os.path.join(probe, "ok.txt"), "w", encoding="utf-8") as f:
                f.write("ok")
            shutil.rmtree(os.path.join(base, "probe"), ignore_errors=True)
            return base
        except OSError:
            continue
    return candidates[-1]


TMP_ROOT = _pick_tmp_base()
_CASE_SEQ = 0


def _tmp() -> str:
    global _CASE_SEQ
    _CASE_SEQ += 1
    path = os.path.join(TMP_ROOT, f"case-{os.getpid()}-{_CASE_SEQ}")
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def _ev(fp: str, ts: str, text: str, session: str | None = None,
        origin: str = "astrbot_context", role: str = "user") -> dict:
    return {"fp": fp, "ts": ts, "day": ts[:10], "role": role, "origin": origin,
            "session": session, "text": text, "tokens": 10, "persona": None}


# ---------------------------------------------------------------- tokens

def test_estimate_tokens():
    assert estimate_tokens("") == 0
    chinese = estimate_tokens("她" * 100)
    english = estimate_tokens("a" * 100)
    assert chinese > english, "中文应比等长英文字符更贵"
    assert 0 < english < 100


def test_calibrator():
    cal = Calibrator()
    assert cal.apply(100) == 100
    cal.learn(raw_estimate=100, actual_prompt_tokens=200)
    assert cal.ratio > 1.0
    assert cal.apply(100) > 100
    for _ in range(50):
        cal.learn(1, 10_000)
    assert cal.ratio <= 3.0


# ---------------------------------------------------------------- vault

def test_vault_append_and_read():
    root = _tmp()
    try:
        vault = Vault(root)
        event = _ev("aaa", "2026-10-22T10:00:00+08:00", "今天好累")
        assert vault.append([event]) == 1
        assert vault.append([event]) == 1, "原始层不去重，重复写就是重复"

        assert vault.days() == ["2026-10-22"]
        assert len(vault.read_day("2026-10-22")) == 2
        stats = vault.stats()
        assert stats["events"] == 2
        assert stats["by_origin"]["astrbot_context"] == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_vault_persona_content_addressed():
    root = _tmp()
    try:
        vault = Vault(root)
        a = vault.put_persona("你是小木")
        b = vault.put_persona("你是小木")
        c = vault.put_persona("你是小橘")
        assert a == b, "相同人格只存一份"
        assert a != c
        assert len(vault.personas()) == 2
        assert vault.read_persona(a) == "你是小木"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- index

def test_index_add_search_rebuild():
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        events = [_ev("b1", "2026-10-22T10:00:00+08:00", "她说她怕黑，睡觉要留灯")]
        assert index.add(events) == 1
        assert index.add(events) == 0, "同 fingerprint 不重复入库"
        assert index.has("b1")

        hits = index.search("怕黑")
        assert hits, "应当能检索到"
        assert "怕黑" in hits[0]["text"]

        vault.append(events)
        assert index.rebuild(vault) == 1
        assert index.count() == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- sync

def test_sync_dedup():
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        parsed = [
            {"idx": 0, "role": "user", "content": "你好呀"},
            {"idx": 1, "role": "assistant", "content": "你好~"},
        ]
        assert sync.sync(vault, index, parsed, persona="p1", session="s1")["new"] == 2
        second = sync.sync(vault, index, parsed, persona="p1", session="s1")
        assert second["new"] == 0, "同内容不应重复入库"
        assert second["duplicate"] == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sync_skips_no_save_block():
    root = _tmp()
    try:
        req = type("Req", (), {})()
        req.contexts = [
            {"role": "user", "content": "正经消息"},
            {"role": "user", "content": "【记忆地图】…<!-- _no_save -->"},
        ]
        _raw, parsed = sync.parse_contexts(req)
        assert len(parsed) == 1, "带 _no_save 的注入块不应被再次归档"
        assert parsed[0]["content"] == "正经消息"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- budget

def _msgs(count: int, size: int = 100) -> list[dict]:
    return [{"idx": i, "role": "user", "content": "字" * size}
            for i in range(count)]


def test_budget_below_trigger_passthrough():
    cal = Calibrator()
    plan = budget.decide(_msgs(5), system_tokens=100, window=100000,
                         calibrator=cal, trigger_ratio=0.8, total_ratio=0.4)
    assert plan.applied is False, "远未到阈值时不应改动上下文"


def test_facts_ratio_fixed_at_ten_percent():
    """facts 预算固定 10%，不再随裁剪次数增长。"""
    assert budget.facts_ratio_for(0) == 0.10
    assert budget.facts_ratio_for(1) == 0.10
    assert budget.facts_ratio_for(50) == 0.10


def test_fact_kind_weights():
    """身份类权重最高，一次性事件最低——超预算时先牺牲事件。"""
    assert extract.kind_weight("identity") > extract.kind_weight("relationship")
    assert extract.kind_weight("relationship") > extract.kind_weight("preference")
    assert extract.kind_weight("preference") > extract.kind_weight("event")
    assert extract.normalize_kind("IDENTITY") == "identity"
    assert extract.normalize_kind("胡说的分类") == "fact"


def test_budget_total_includes_everything():
    """total_ratio 是整个请求的目标，历史只拿剩下的部分（含纪要那一份）。"""
    cal = Calibrator()
    window = 10000
    plan = budget.decide(_msgs(200), system_tokens=100, window=window,
                         calibrator=cal, trigger_ratio=0.8, total_ratio=0.4,
                         facts_ratio=0.01, map_ratio=0.03, recall_ratio=0.05,
                         summary_ratio=0.03)
    assert plan.applied is True

    b = plan.budgets
    total = window * 0.4
    used = (100 + b["facts"] + b["map"] + b["recall"]
            + b["summary"] + b["history"])
    assert abs(used - total) <= 5, f"各块之和应接近总量: {used} vs {total}"
    assert b["summary"] == int(window * 0.03), "纪要拿固定的 3%"
    assert b["history"] < window * 0.4, "历史必须小于总量（要给其它块让位）"


def test_budget_keeps_recent_messages():
    cal = Calibrator()
    parsed = _msgs(200)
    plan = budget.decide(parsed, system_tokens=100, window=10000,
                         calibrator=cal, trigger_ratio=0.8, total_ratio=0.4,
                         facts_ratio=0.01, map_ratio=0.03, recall_ratio=0.05)
    assert plan.applied is True
    assert plan.kept, "裁剪后必须保留内容"
    assert len(plan.kept) < len(parsed)
    assert plan.kept[-1]["content"] == parsed[-1]["content"]
    assert plan.kept[0]["content"] == parsed[len(parsed) - len(plan.kept)]["content"]
    assert plan.after < plan.before


def test_budget_window_zero_never_trims():
    cal = Calibrator()
    plan = budget.decide(_msgs(50), 0, window=0, calibrator=cal)
    assert plan.applied is False


def test_total_request_tokens_no_double_count():
    """
    合计 = 主体（system + 历史）+ 注入块。

    payload 本身就是 facts + 地图 + 检索拼起来的，那三块不能再加一遍，
    否则面板的「合计」会比真实占用多出一个注入块——用它调参必然被带偏。
    """
    system, history, payload = 3000, 12000, 9000
    total = budget.total_request_tokens(system, history, payload)
    assert total == system + history + payload

    # 没有注入块时就是纯主体，差多少正好是注入块本身
    bare = budget.total_request_tokens(system, history, 0)
    assert bare == system + history
    assert total - bare == payload

    assert budget.total_request_tokens(0, 0, 0) == 0
    assert budget.total_request_tokens(-5, 10, 0) == 10, "负值不该把合计拉低"


# ---------------------------------------------------------------- extract

def test_parse_output_tolerates_fences():
    raw = '好的，结果如下：\n```json\n[{"claim":"她怕黑","kind":"preference","sources":[1]}]\n```\n完毕'
    items = extract.parse_output(raw)
    assert len(items) == 1
    assert items[0]["claim"] == "她怕黑"
    assert items[0]["sources"] == [1]
    assert items[0]["kind"] == "preference"


def test_parse_output_garbage():
    assert extract.parse_output("模型这次没给 JSON") == []
    assert extract.parse_output("```json\n[broken\n```") == []


def test_resolve_sources_bounds():
    fps = ["f1", "f2", "f3"]
    assert extract.resolve_sources([1, 3], fps) == ["f1", "f3"]
    assert extract.resolve_sources([0, 9, "x"], fps) == []


def test_factstore_versioning():
    root = _tmp()
    try:
        store = extract.FactStore(root)
        fid = store.upsert("她怕黑", ["f1"], model="m1")
        assert fid
        assert store.upsert("她怕黑", ["f2"], model="m1") == fid, \
            "同一 claim 应合并到同一条事实"

        fact = store.load(fid)
        assert len(fact["versions"]) == 2
        assert fact["versions"][-1]["source_fp"] == ["f1", "f2"], \
            "来源应合并而不是覆盖"

        assert store.delete(fid) is True
        assert len(store.all_facts()) == 0
        assert len(store.all_facts(include_deleted=True)) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_visible_facts_by_subject_and_scope():
    """谁能看见哪条事实：别人的私事不能漏，people 的能跨会话。"""
    facts = [
        {"id": "a", "claim": "A 怕黑", "scope": "people", "subject": "u1",
         "source_fp": ["f1"]},
        {"id": "b", "claim": "B 在群里的角色", "scope": "session", "subject": "u2",
         "source_fp": ["f2"]},
        {"id": "c", "claim": "老数据（没 subject）", "scope": "session",
         "subject": "", "source_fp": ["f3"]},
        {"id": "d", "claim": "别的会话的老数据", "scope": "session",
         "subject": "", "source_fp": ["f4"]},
        {"id": "e", "claim": "没有出处的老数据", "scope": "session",
         "subject": "", "source_fp": []},
        {"id": "f", "claim": "别人的 people 事实", "scope": "people",
         "subject": "u9", "source_fp": ["f5"]},
        {"id": "g", "claim": "A 在别的群说的长期事实", "scope": "people",
         "subject": "u1", "source_fp": ["f6"]},
        {"id": "h", "claim": "A 在别的群说的临时事", "scope": "session",
         "subject": "u1", "source_fp": ["f6"]},
    ]
    source_session = {"f1": "s1", "f2": "s1", "f3": "s1",
                      "f4": "s2", "f5": "s2", "f6": "s2"}
    got = {fact["id"] for fact in extract.visible_facts(
        facts, "s1", {"u1", "u2"}, source_session)}
    assert got == {"a", "b", "c", "e", "g"}, got
    # d：别的会话、没 subject → 不给
    # f：subject 不在本会话出现过 → 不给（防串人）
    # h：subject 对，但 people 才跨会话，session 的不给


def test_index_identity_queries():
    """索引要能回答两个身份问题：这句话哪个会话说的、这个会话有哪些人。"""
    root = _tmp()
    try:
        idx = Index(root)
        idx.add([
            {"fp": "a", "ts": "2026-10-22T10:00:00+08:00", "day": "2026-10-22",
             "role": "user", "session": "s1", "text": "甲说话", "sender": "u1"},
            {"fp": "b", "ts": "2026-10-22T10:01:00+08:00", "day": "2026-10-22",
             "role": "user", "session": "s1", "text": "乙说话", "sender": "u2"},
            {"fp": "c", "ts": "2026-10-22T10:02:00+08:00", "day": "2026-10-22",
             "role": "user", "session": "s2", "text": "别处说话", "sender": "u9"},
        ])
        assert idx.subjects("s1") == {"u1", "u2"}
        assert idx.subjects("s2") == {"u9"}
        assert idx.sessions_of(["a", "c", "不存在"]) == {"a": "s1", "c": "s2"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_fact_summaries_and_detail():
    """面板用：列表摘要只取当前版本，详情带版本历史和出处原文。"""
    root = _tmp()
    try:
        store = extract.FactStore(root)
        store.upsert("她养了一只猫", ["f1"], kind="fact", model="m1")
        second = store.upsert("她怕黑", ["f2"], kind="preference", model="m1")
        store.upsert("她怕黑", ["f3"], kind="preference", model="m2")

        items = {item["claim"]: item for item in extract.summaries(store)}
        assert set(items) == {"她养了一只猫", "她怕黑"}
        fear = items["她怕黑"]
        assert fear["id"] == second
        assert fear["kind"] == "preference"
        assert fear["versions"] == 2, "第二次 upsert 应该只合并版本，不新增一条"
        assert fear["sources"] == 2, "来源合并后是两条"

        events = {
            "f2": _ev("f2", "2026-10-22T11:00:00+08:00", "我怕黑"),
        }
        data = extract.detail(store, second, resolve=events.get)
        assert data["claim"] == "她怕黑"
        assert len(data["versions"]) == 2
        # f3 在索引里不存在 → 如实标 found=False，不能让详情页崩
        assert data["sources"][0]["found"] is True
        assert data["sources"][0]["text"] == "我怕黑"
        assert data["sources"][1]["found"] is False
        assert extract.detail(store, "fact-nope") is None

        store.delete(second)
        assert [i["claim"] for i in extract.summaries(store)] == ["她养了一只猫"], \
            "已删除的事实不该出现在面板列表里"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- inject

def test_summary_builds_with_anchors_and_rolls():
    """纪要：写盘、带边界、第二次只处理新裁掉的那一段。"""
    root = _tmp()
    try:
        store = summary_mod.SummaryStore(root)
        events = [
            {"fp": f"f{i}", "ts": f"2026-10-2{i}T10:00:00+08:00",
             "day": f"2026-10-2{i}",
             "role": "user" if i % 2 == 0 else "assistant", "origin": "x",
             "session": "s1", "text": f"第{i}条消息内容", "tokens": 5}
            for i in range(6)
        ]
        calls = []

        async def fake_llm(prompt: str) -> str:
            calls.append(prompt)
            return "10-20 晚上：初次见面\n10-21：聊了工作"

        first = asyncio.run(summary_mod.build(
            fake_llm, events, store, "s1", boundary_fp="f4",
            chunk_chars=40, max_chars=500))
        assert first["ok"] and first["written"]
        data = store.load("s1")
        assert data["covered_fp"] == "f3", "边界之前的那条才是最后被纪要的"
        assert "初次见面" in data["text"]
        assert data["version"] == 1
        before_calls = len(calls)

        second = asyncio.run(summary_mod.build(
            fake_llm, events, store, "s1", boundary_fp="f5",
            chunk_chars=40, max_chars=500))
        assert second["ok"] and second["written"]
        assert len(calls) > before_calls, "新增了一段，必须再调一次"
        assert store.load("s1")["version"] == 2
        assert any("旧纪要" in c for c in calls[before_calls:]), \
            "第二段要和旧纪要合并，不是直接追加"

        same = asyncio.run(summary_mod.build(
            fake_llm, events, store, "s1", boundary_fp="f5",
            chunk_chars=40, max_chars=500))
        assert same["skipped"] is True, "没有新内容就不该再调模型"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_summary_cache_reuses_batches():
    """同一段原文第二次纪要用缓存，一次模型都不调。"""
    root = _tmp()
    try:
        cache = summary_mod.SummaryCache(root)
        events = [{"fp": f"c{i}", "ts": f"2026-10-22T0{i}:00:00+08:00",
                   "role": "user", "session": "s1", "text": f"内容{i}" * 3}
                  for i in range(4)]
        calls = []

        async def fake_llm(prompt: str) -> str:
            calls.append(prompt)
            return "10-22：聊了点事"

        store_a = summary_mod.SummaryStore(_tmp())
        asyncio.run(summary_mod.build(fake_llm, events, store_a, "s1",
                                      boundary_fp="", keep_recent=1,
                                      chunk_chars=30, cache=cache))
        assert len(calls) > 0

        store_b = summary_mod.SummaryStore(_tmp())
        calls.clear()
        result = asyncio.run(summary_mod.build(fake_llm, events, store_b, "s1",
                                               boundary_fp="", keep_recent=1,
                                               chunk_chars=30, cache=cache))
        assert result["ok"] and result["cached"] > 0
        assert calls == [], "内容没变就不该再调模型"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_summary_block_and_budget():
    """纪要块要带"核对原文"的提示；预算从历史里扣。"""
    block = inject.build_summary_block("10-20：初次见面\n10-21：聊了工作", budget=200)
    assert "前情纪要" in block
    assert "核对原文" in block
    assert inject.build_summary_block("", 200) == ""
    assert inject.build_summary_block("有内容", 0) == ""

    big = [{"idx": 0, "role": "user", "content": "字" * 1000}]
    plan = budget.decide(big, 0, 1000, Calibrator(enabled=False),
                         trigger_ratio=0.5, total_ratio=0.4,
                         facts_ratio=0.10, map_ratio=0.03,
                         recall_ratio=0.05, summary_ratio=0.03)
    assert plan.applied
    assert plan.budgets["summary"] == 30
    assert plan.budgets["history"] == 400 - 100 - 30 - 50 - 30, \
        "纪要的 3% 要从历史里扣，不能白加"


def test_inject_blocks_and_marker():
    facts = [{"claim": "她怕黑", "id": "f1"}, {"claim": "她在考研", "id": "f2"}]
    block = inject.build_facts_block(facts, budget=200)
    assert "怕黑" in block

    recall = inject.build_recall_block(
        [{"ts": "2026-10-22T10:00:00", "role": "user", "text": "我怕黑"}],
        budget=200)
    assert "怕黑" in recall and "[2026-10-22" in recall

    payload = inject.compose([block, recall])
    assert inject.NO_SAVE_MARKER in payload, "必须带 _no_save 标记"


def test_drop_known_removes_context_duplicates():
    """最近 N 轮已经原样发过去了，检索不该把它们再捞一遍。"""
    hits = [{"fp": "a", "text": "最近说过的"},
            {"fp": "b", "text": "很久以前说过的"}]
    kept = inject.drop_known(hits, {"a"})
    assert [h["fp"] for h in kept] == ["b"]
    assert len(inject.drop_known(hits, set())) == 2
    assert inject.drop_known([], {"a"}) == []


def test_inject_budget_respected():
    facts = [{"claim": "很长的记忆" * 50, "id": f"f{i}"} for i in range(50)]
    block = inject.build_facts_block(facts, budget=50)
    assert len(block) < 2000


# ---------------------------------------------------------------- importer

def test_import_beyond_directory():
    root = _tmp()
    src = _tmp()
    try:
        os.makedirs(os.path.join(src, "state"), exist_ok=True)
        os.makedirs(os.path.join(src, "archive", "s1"), exist_ok=True)
        os.makedirs(os.path.join(src, "session", "s1"), exist_ok=True)

        with open(os.path.join(src, "state", "s1.json"), "w", encoding="utf-8") as f:
            json.dump({"summary": "她最近在准备搬家"}, f, ensure_ascii=False)

        with open(os.path.join(src, "archive", "s1", "a.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": "我好累"},
                               ensure_ascii=False) + "\n")

        with open(os.path.join(src, "session", "s1", "user-1.md"), "w",
                  encoding="utf-8") as f:
            f.write("# 关于她\n- 怕黑\n")

        vault = Vault(root)
        index = Index(root)
        stats = importer.import_beyond(src, vault, index)
        assert stats["events"] >= 3, f"应至少导入 3 条，实际 {stats['events']}"
        assert index.search("搬家")
        assert index.search("怕黑")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


def test_import_astrbot_sqlite():
    import sqlite3
    root = _tmp()
    try:
        db = os.path.join(root, "fake.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE conversations "
                     "(id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
        conn.executemany(
            "INSERT INTO conversations(role, content) VALUES(?,?)",
            [("user", "明天要考试"), ("assistant", "别紧张，你可以的")])
        conn.execute("CREATE TABLE unrelated (a INTEGER)")
        conn.commit()
        conn.close()

        vault = Vault(os.path.join(root, "store"))
        index = Index(os.path.join(root, "store"))
        stats = importer.import_astrbot(db, vault, index)
        assert stats["events"] == 2
        assert index.search("考试")
        assert "unrelated" in stats["skipped_tables"], "认不出的表应跳过而不是乱猜"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_import_dry_run_writes_nothing():
    """dry_run 只算指纹，一个字节都不写 —— /allmem_verify 靠这个做校验。"""
    root = _tmp()
    src = _tmp()
    try:
        os.makedirs(os.path.join(src, "state"), exist_ok=True)
        with open(os.path.join(src, "state", "s1.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"summary": "她最近在准备搬家",
                       "messages": [{"role": "user", "content": "我好累"}]},
                      f, ensure_ascii=False)

        vault = Vault(root)
        index = Index(root)

        dry = importer.import_beyond(src, vault, index, dry_run=True)
        assert dry["events"] == 2, f"应扫出 2 条候选，实际 {dry['events']}"
        assert len(dry["fps"]) == 2
        assert index.count() == 0, "dry_run 不能写索引"
        assert vault.stats()["events"] == 0, "dry_run 不能写原始层"

        # 真导入一遍：dry_run 算出的指纹必须和真实落库的完全对得上
        real = importer.import_beyond(src, vault, index)
        assert real["events"] == 2
        assert index.count() == 2
        assert importer.missing_fps(index, dry["fps"]) == 0, \
            "dry_run 的指纹口径必须和真导入一致，否则校验会误报缺口"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


def test_import_dry_run_detects_gap():
    """库里少一条时，dry_run 必须能算出来 —— 这是 /allmem_verify 的判据。"""
    root = _tmp()
    src = _tmp()
    try:
        os.makedirs(os.path.join(src, "session"), exist_ok=True)
        with open(os.path.join(src, "session", "user-1.md"), "w",
                  encoding="utf-8") as f:
            f.write("# 关于她\n- 怕黑\n")

        vault = Vault(root)
        index = Index(root)
        importer.import_beyond(src, vault, index)

        clean = importer.import_beyond(src, vault, index, dry_run=True)
        assert importer.missing_fps(index, clean["fps"]) == 0, "全导进来后不应有缺口"

        # 来源里新增一条还没导入的 → 缺口必须立刻是 1
        with open(os.path.join(src, "session", "user-2.md"), "w",
                  encoding="utf-8") as f:
            f.write("# 关于她\n- 讨厌被叫全名\n")
        dirty = importer.import_beyond(src, vault, index, dry_run=True)
        assert importer.missing_fps(index, dirty["fps"]) == 1, \
            "新增内容还没导入时，缺口应为 1"

        importer.import_beyond(src, vault, index)
        after = importer.import_beyond(src, vault, index, dry_run=True)
        assert importer.missing_fps(index, after["fps"]) == 0, "补齐后缺口应归零"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


def test_import_astrbot_dry_run_matches_real_import():
    """SQLite 来源同样要能 dry_run：扫出的候选和真导入的指纹一致。"""
    import sqlite3
    root = _tmp()
    try:
        db = os.path.join(root, "fake.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE conversations "
                     "(id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
        conn.executemany(
            "INSERT INTO conversations(role, content) VALUES(?,?)",
            [("user", "明天要考试"), ("assistant", "别紧张，你可以的")])
        conn.commit()
        conn.close()

        vault = Vault(os.path.join(root, "store"))
        index = Index(os.path.join(root, "store"))

        dry = importer.import_astrbot(db, vault, index, dry_run=True)
        assert dry["events"] == 2
        assert index.count() == 0, "dry_run 不能写索引"

        real = importer.import_astrbot(db, vault, index)
        assert real["events"] == 2
        assert importer.missing_fps(index, dry["fps"]) == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_import_astrbot_splits_json_conversation():
    """
    AstrBot v4 把整段会话存成一段 JSON（conversations.content）。

    必须拆回逐条消息：整段收成一条的话，角色和时间全丢，
    而且原始层里会出现一个几百条的 JSON 正文——这违反「只存原话」。
    """
    import sqlite3
    root = _tmp()
    try:
        db = os.path.join(root, "fake_v4.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE conversations "
                     "(id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
        blob = json.dumps([
            {"role": "user",
             "content": [{"type": "text", "text": "初次见面，你好小木"}]},
            {"role": "assistant",
             "content": [{"type": "think", "think": "她来了"},
                         {"type": "text", "text": "你好呀"}]},
            {"role": "assistant",
             "content": [{"type": "text", "text": "注入块<!-- _no_save -->"}]},
        ], ensure_ascii=False)
        conn.execute("INSERT INTO conversations(role, content) VALUES(?,?)",
                     ("小木", blob))
        conn.commit()
        conn.close()

        vault = Vault(os.path.join(root, "store"))
        index = Index(os.path.join(root, "store"))
        stats = importer.import_astrbot(db, vault, index)

        assert stats["events"] == 2, \
            f"应拆成 2 条（第三条是注入块），实际 {stats['events']}"
        rows = list(vault.iter_events())
        assert [r["role"] for r in rows] == ["user", "assistant"]
        assert rows[0]["text"] == "初次见面，你好小木"
        assert rows[1]["text"] == "你好呀"
        assert all(not r["text"].startswith("[{") for r in rows), \
            "原始层里不许出现 JSON 正文"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_expand_messages_only_for_message_arrays():
    """拆 JSON 会话要有分寸：不是消息数组就原样返回空，让上层按纯文本收。"""
    assert importer.expand_messages("今天好累") == []
    assert importer.expand_messages("[1, 2, 3]") == []
    assert importer.expand_messages('[{"type": "text", "text": "不是消息"}]') == []
    assert importer.expand_messages("不是 JSON 的 [ 开头") == []

    got = importer.expand_messages(json.dumps(
        [{"role": "user", "content": "直接是字符串"}], ensure_ascii=False))
    assert got == [("user", "直接是字符串")]


def test_import_astrbot_skips_config_table():
    """preferences 是 key/value 配置，不该被当成对话收进原始层。"""
    import sqlite3
    root = _tmp()
    try:
        db = os.path.join(root, "fake_cfg.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE preferences "
                     "(id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
        conn.execute("INSERT INTO preferences(key, value) VALUES(?,?)",
                     ("sel_conv_id", '{"val": "a3d4e6bc"}'))
        conn.commit()
        conn.close()

        vault = Vault(os.path.join(root, "store"))
        index = Index(os.path.join(root, "store"))
        stats = importer.import_astrbot(db, vault, index)

        assert stats["events"] == 0
        assert "preferences" in stats["skipped_tables"]
        assert index.count() == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_import_uses_reminder_time_and_carries_forward():
    """
    导入的历史时间也要真：从用户消息里的 Current datetime 还原，
    助手回复沿用上一条用户消息的时间（和实时归档同一套规则）。
    """
    import sqlite3
    root = _tmp()
    try:
        db = os.path.join(root, "fake_v4.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE conversations "
                     "(id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
        blob = json.dumps([
            {"role": "user", "content": [
                {"type": "text", "text": "第一句"},
                {"type": "text", "text": "<system_reminder>"
                                         "Current datetime: 2026-10-20 21:56 (CST), "
                                         "Weekday: Tuesday</system_reminder>"}]},
            {"role": "assistant",
             "content": [{"type": "text", "text": "回复一"}]},
            {"role": "user", "content": [
                {"type": "text", "text": "第二句"},
                {"type": "text", "text": "<system_reminder>"
                                         "Current datetime: 2026-10-21 09:30 (CST)"
                                         "</system_reminder>"}]},
            {"role": "assistant",
             "content": [{"type": "text", "text": "回复二"}]},
        ], ensure_ascii=False)
        conn.execute("INSERT INTO conversations(role, content) VALUES(?,?)",
                     ("小木", blob))
        conn.commit()
        conn.close()

        vault = Vault(os.path.join(root, "store"))
        index = Index(os.path.join(root, "store"))
        importer.import_astrbot(db, vault, index)

        rows = list(vault.iter_events())
        assert len(rows) == 4
        assert rows[0]["ts"].startswith("2026-10-20T21:56")
        assert rows[1]["ts"].startswith("2026-10-20T21:56"), \
            "助手回复应沿用上一条用户消息的时间"
        assert rows[2]["ts"].startswith("2026-10-21T09:30")
        assert rows[3]["ts"].startswith("2026-10-21T09:30")
        assert not any(r["backfill"] for r in rows), "有时间就不该标回填"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_import_marks_backfill_without_time():
    """来源里没有时间就如实标回填，不能假装刚刚发生。"""
    import sqlite3
    root = _tmp()
    try:
        db = os.path.join(root, "fake_v4.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE conversations "
                     "(id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
        conn.execute("INSERT INTO conversations(role, content) VALUES(?,?)",
                     ("user", "一句没有时间的话"))
        conn.commit()
        conn.close()

        vault = Vault(os.path.join(root, "store"))
        index = Index(os.path.join(root, "store"))
        importer.import_astrbot(db, vault, index)

        rows = list(vault.iter_events())
        assert len(rows) == 1
        assert rows[0]["backfill"] is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 会话范围与单条读取

def test_search_includes_unscoped_records():
    """导入的历史没有会话键，按会话检索时也必须能被搜到。"""
    root = _tmp()
    try:
        index = Index(root)
        index.add([
            _ev("s1", "2026-10-22T10:00:00+08:00", "会话里的小猫", session="qq:1"),
            _ev("imp", "2026-10-19T10:00:00+08:00", "导入的小猫", session=None,
                origin="import_astrbot"),
            _ev("o1", "2026-10-18T10:00:00+08:00", "别处的小猫", session="qq:2"),
        ])
        fps = {h["fp"] for h in index.search("小猫", session="qq:1")}
        assert "s1" in fps
        assert "imp" in fps, "没有会话键的导入记录不该被过滤掉"
        assert "o1" not in fps, "别的会话不该串进来"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_get_and_neighbors():
    root = _tmp()
    try:
        index = Index(root)
        index.add([
            _ev("a", "2026-10-22T10:00:00+08:00", "第一句"),
            _ev("b", "2026-10-22T10:01:00+08:00", "第二句"),
            _ev("c", "2026-10-22T10:02:00+08:00", "第三句"),
        ])
        target = index.get("b")
        assert target and target["text"] == "第二句"
        assert [n["fp"] for n in index.neighbors("b", span=1)] == ["a", "c"]
        assert index.neighbors("b", span=0) == []
        assert index.get("nope") is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_index_sessions_and_history_paging():
    root = _tmp()
    try:
        index = Index(root)
        index.add([
            _ev("a1", "2026-10-20T10:00:00+08:00", "私聊第一句", session="q:1"),
            _ev("a2", "2026-10-20T10:01:00+08:00", "私聊第二句", session="q:1"),
            _ev("b1", "2026-10-21T10:00:00+08:00", "群里说话", session="group:9"),
            _ev("c1", "2026-10-19T10:00:00+08:00", "导入的旧记录", session=None,
                origin="import_astrbot"),
        ])
        kinds = {s["session"]: s["kind"] for s in index.sessions()}
        assert kinds.get("q:1") == "private"
        assert kinds.get("group:9") == "group"
        assert "(未分类)" in kinds

        page = index.history("q:1", offset=0, limit=1)
        assert page["total"] == 2
        assert page["items"][0]["fp"] == "a2", "默认按最新优先"
        assert index.history("q:1", offset=1, limit=1)["items"][0]["fp"] == "a1"

        legacy = index.history("(未分类)")
        assert legacy["total"] == 1
        assert legacy["items"][0]["text"] == "导入的旧记录"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_history_limit_zero_returns_everything():
    """
    limit=0 = 不分页，整段拉完。

    面板去掉「加载更多」之后，一打开会话就要拿到全部记录，
    滚动条才能一口气拖到底。
    """
    root = _tmp()
    try:
        index = Index(root)
        index.add([
            _ev(f"m{i:02d}", "2026-10-22T10:00:00+08:00", f"第{i}句",
                session="q:1")
            for i in range(30)
        ])
        page = index.history("q:1", offset=0, limit=0, newest_first=False)
        assert page["total"] == 30
        assert len(page["items"]) == 30, "limit=0 必须把 30 条全给出来"
        assert page["items"][0]["text"] == "第0句"
        assert page["items"][-1]["text"] == "第29句", "正序：最新的在最后"

        # 负数也当「全部」，别因为没有分页参数就只给一部分
        assert len(index.history("q:1", limit=-1)["items"]) == 30

        # 正数还是分页，老调用不受影响（这里显式要正序，和面板一致）
        assert len(index.history("q:1", offset=0, limit=5,
                                 newest_first=False)["items"]) == 5
        assert index.history("q:1", offset=28, limit=5,
                             newest_first=False)["items"][0]["text"] == "第28句"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_rebuild_keeps_rows_with_duplicate_fingerprints():
    """
    旧数据里多行可能共用一个指纹（新版不会）。

    重建索引时必须把它们按出现顺序编号保留，否则索引会比 vault 少几条，
    检索永远缺那几句，/allmem_verify 也会一直报"和原始层对不上"。
    """
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        rows = [
            {"fp": "dup", "ts": f"2026-10-2{d}T10:00:00+08:00",
             "day": f"2026-10-2{d}", "role": "assistant",
             "origin": "astrbot_context", "session": "s", "text": "晚安",
             "raw": None, "tokens": 3, "persona": None, "backfill": False}
            for d in (0, 1, 2)
        ]
        vault.append(rows)

        assert index.rebuild(vault) == 3
        assert index.count() == 3, "重建出来的条数必须和 vault 一致"
        assert index.has("dup") and index.has("dup-2") and index.has("dup-3")
        assert len(index.search("晚安", limit=10)) == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_backup_zip_is_superset_and_no_duplicate():
    """
    打包必须一个不漏，且不能把插件数据重复打两遍。

    这里复刻真实布局：插件数据目录嵌在 AstrBot data/ 里面。
    """
    import zipfile
    root = _tmp()
    try:
        plugin = os.path.join(root, "all_memory")
        os.makedirs(os.path.join(plugin, "vault"), exist_ok=True)
        os.makedirs(os.path.join(plugin, "exports"), exist_ok=True)
        os.makedirs(os.path.join(root, "plugin_data", "p1"), exist_ok=True)

        def write(path, data="x"):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)

        write(os.path.join(plugin, "vault", "2026-10-22.jsonl"), '{"a":1}')
        write(os.path.join(plugin, "exports", "old.zip"), "old")
        write(os.path.join(root, "plugin_data", "p1", "cfg.json"), "{}")
        write(os.path.join(root, "data_v4.db"), "db")
        write(os.path.join(root, "junk.tmp"), "tmp")

        target = os.path.join(_tmp(), "out.zip")
        exports = os.path.join(plugin, "exports")
        info = backup.build_zip(target, [
            (plugin, "all_memory", (exports,)),
            (root, "astrbot_data", (plugin,)),
        ])

        with zipfile.ZipFile(target) as zf:
            names = set(zf.namelist())

        assert "all_memory/vault/2026-10-22.jsonl" in names, "插件数据不能漏"
        assert "astrbot_data/plugin_data/p1/cfg.json" in names, "其它插件数据不能漏"
        assert "astrbot_data/data_v4.db" in names

        assert not any(n.startswith("all_memory/exports/") for n in names), \
            "导出目录不该被再打包一次"
        assert not any(n.startswith("astrbot_data/all_memory/") for n in names), \
            "插件数据不该在 astrbot_data 下重复出现"
        assert "astrbot_data/junk.tmp" not in names, "临时文件应跳过"

        assert info["files"] == len(names)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_extract_content_blocks():
    """AstrBot 的 content 是块数组：正文抽出来，思考块留档不展示。"""
    blocks = [
        {"type": "think", "think": "我应该先打招呼"},
        {"type": "text", "text": "你好呀"},
        {"type": "tool_use", "name": "search", "input": {"q": "x"}},
    ]
    text, raw = sync.extract_content(blocks)
    assert text == "你好呀", f"正文应只含 text 块，实际 {text!r}"
    assert raw and "think" in raw, "思考/工具块必须留在 raw 里，不能丢"

    plain = sync.extract_content("就是一句话")
    assert plain == ("就是一句话", None)

    only_think = sync.extract_content([{"type": "think", "think": "略"}])
    assert only_think[0] == "[思考] 略", only_think[0]
    assert only_think[1] and "think" in only_think[1], "原始块仍要留档"


def test_parse_contexts_maps_tool_role():
    req = type("Req", (), {})()
    req.contexts = [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
        {"role": "tool", "content": "搜索结果"},
        {"role": "assistant", "content": [{"type": "think", "think": "想"}]},
    ]
    _raw, parsed = sync.parse_contexts(req)
    roles = [p["role"] for p in parsed]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert parsed[1]["content"] == "好的"


def test_sync_marks_backfill_and_uses_hint():
    """只有最后一条是「这一轮」的，前面标回填；真实时间只给最后一条。"""
    from datetime import datetime, timezone, timedelta

    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        parsed = [
            {"idx": 0, "role": "user", "content": "很久以前的话", "raw": None},
            {"idx": 1, "role": "user", "content": "刚刚这句话", "raw": None},
        ]
        hint = datetime(2026, 10, 22, 20, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        sync.sync(vault, index, parsed, persona="p", session="s", ts_hint=hint)

        first = index.get(sync.fp_of("user\x00很久以前的话"))
        last = index.get(sync.fp_of("user\x00刚刚这句话"))
        assert first["backfill"] is True
        assert last["backfill"] is False
        assert last["ts"].startswith("2026-10-22T20:00:00")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_history_orders_by_insertion_not_timestamp():
    """回填的消息时间戳全挤在同一秒，排序必须靠写入顺序。"""
    root = _tmp()
    try:
        index = Index(root)
        same = "2026-10-22T19:42:27+08:00"
        index.add([
            _ev("e1", same, "第一句"),
            _ev("e2", same, "第二句"),
            _ev("e3", same, "第三句"),
        ])
        page = index.history("(未分类)", limit=3)
        assert [i["fp"] for i in page["items"]] == ["e3", "e2", "e1"], \
            "默认最新优先，且同秒内按写入顺序"
        assert [n["fp"] for n in index.neighbors("e2", span=1)] == ["e1", "e3"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_extract_content_think_and_tool_only():
    """只有思考/工具块时，给可读摘要，不要把 JSON 塞进正文。"""
    think_only = sync.extract_content([{"type": "think", "think": "先看看目录"}])
    assert think_only[0].startswith("[思考]"), think_only[0]
    assert "{" not in think_only[0]

    tool_only = sync.extract_content(
        [{"type": "tool_use", "name": "run_shell", "input": {}}])
    assert tool_only[0] == "[工具] run_shell", tool_only[0]
    assert tool_only[1], "工具块要留在 raw 里"


def test_extract_content_drops_empty_block():
    """
    空的 think 块（"think": ""）什么内容都没有。

    以前会退回去把整块 JSON 当正文，原始层里就会出现
    [{"type":"think","think":"","encrypted":null}] 这种"她说过的话"。
    这条消息本来就没有内容，应该直接不存。
    """
    text, raw = sync.extract_content(
        [{"type": "think", "think": "", "encrypted": None}])
    assert text == "", f"空块不该产生正文，实际 {text!r}"
    assert raw is None
    assert sync.extract_content([]) == ("", None)


def test_sync_carry_survives_turns_without_reminder():
    """
    逐轮归档时，一轮里可能没有用户消息（纯工具轮）。

    这时候得沿用上一轮最后那句用户消息的时间，不能掉成"归档那一刻"，
    否则逐轮开启和一次性补录的时间会对不上。
    """
    root = _tmp()
    try:
        from datetime import datetime

        vault = Vault(root)
        index = Index(root)

        first = [{"idx": 0, "role": "user", "content":
                  "在吗\n<system_reminder>Current datetime: 2026-10-22 21:00 "
                  "(CST), Weekday: Thursday</system_reminder>", "raw": None}]
        stats = sync.sync(vault, index, first, "", session="s",
                          origin="astrbot_turn", force=True)
        carry = datetime.fromisoformat(stats["carry"])
        assert carry is not None

        # 下一轮只有工具消息，没有任何 reminder
        tool = [{"idx": 0, "role": "tool",
                 "content": "Command completed with exit code 0", "raw": None}]
        sync.sync(vault, index, tool, "", session="s",
                  origin="astrbot_turn", force=True, carry=carry)

        row = list(vault.iter_events())[-1]
        assert row["ts"].startswith("2026-10-22T21:00"), row["ts"]
        assert row["backfill"] is False, "沿用已知时间就不该标回填"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_turn_messages_skips_system_temp_and_empty():
    """run_context.messages → 可归档条目：system、临时块、空消息都要跳过。"""
    from types import SimpleNamespace

    messages = [
        SimpleNamespace(role="system", content="你是小木"),
        SimpleNamespace(role="assistant", content=[]),
        SimpleNamespace(role="assistant",
                        content=[SimpleNamespace(type="image_url", _no_save=False)]),
        SimpleNamespace(role="tool", content="Command completed with exit code 0"),
        SimpleNamespace(role="assistant",
                        content=[SimpleNamespace(type="think", think="",
                                                 _no_save=False)]),
        SimpleNamespace(role="user", content=[
            SimpleNamespace(type="text", text="真话", _no_save=False),
            SimpleNamespace(type="text", text="注入块<!-- _no_save -->",
                            _no_save=True),
        ]),
    ]
    parsed = sync.turn_messages(messages)
    assert [p["role"] for p in parsed] == ["tool", "user"], \
        [p["role"] for p in parsed]
    assert parsed[1]["content"] == "真话", "带 _no_save 的临时块不能被抄进来"


def test_turn_messages_keeps_think_when_no_text():
    """只有思考块时按 [思考] … 收，和 parse_contexts 的口径一致。"""
    from types import SimpleNamespace

    messages = [
        SimpleNamespace(role="assistant", content=[
            SimpleNamespace(type="think", think="先把日志翻一遍", _no_save=False),
        ]),
    ]
    parsed = sync.turn_messages(messages)
    assert parsed[0]["content"].startswith("[思考]")


def test_turn_sync_matches_context_sync():
    """
    回归测试：先按 run_context.messages 归档这一轮，下一轮再从 req.contexts
    看到同样的内容时，必须一条都不新增。

    上一版的错位就是这条对不上：回复先进库、问句下一轮才补，顺序翻了。
    只要两条来源的正文/指纹完全一致，这种错位就不可能再出现。
    """
    from types import SimpleNamespace
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)

        reminder = ("<system_reminder>Current datetime: 2026-10-22 22:40 (CST), "
                    "Weekday: Thursday</system_reminder>")
        messages = [
            SimpleNamespace(role="system", content="你是小木"),
            SimpleNamespace(role="user", content=[
                SimpleNamespace(type="text", text="没事，暂时还不困", _no_save=False),
                SimpleNamespace(type="text", text=reminder, _no_save=False),
                SimpleNamespace(type="text", text="记忆块<!-- _no_save -->",
                                _no_save=True),
            ]),
            SimpleNamespace(role="assistant", content=[
                SimpleNamespace(type="text", text="那不困就再陪主人待一会儿。",
                                _no_save=False),
            ]),
        ]

        parsed = sync.turn_messages(messages)
        sync.sync(vault, index, parsed, "p", session="s", origin="astrbot_turn")
        assert index.count() == 2, index.count()

        # 下一轮的 req.contexts：同样这两条，宿主回存后的形态
        req = SimpleNamespace(
            system_prompt="你是小木",
            contexts=[
                {"role": "user", "content": [
                    {"type": "text", "text": "没事，暂时还不困"},
                    {"type": "text", "text": reminder},
                ]},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "那不困就再陪主人待一会儿。"},
                ]},
            ],
        )
        _raw, parsed2 = sync.parse_contexts(req)
        stats = sync.sync(vault, index, parsed2, "p", session="s")
        assert stats["new"] == 0, f"同一轮内容被重复入库了：{stats}"
        assert index.count() == 2

        # 时间也要真：都用消息里那句 reminder，不是归档时刻
        row = index.get(sync.fp_of(f"user\x00{parsed[0]['content']}"))
        assert row["ts"].startswith("2026-10-22T22:40"), row["ts"]
        assert row["backfill"] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_rebuild_fixes_json_text_from_raw():
    """索引重建时用 raw 重算正文，把早期的 JSON 文本修好。"""
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        blocks = [{"type": "think", "think": "嗯"},
                  {"type": "tool_use", "name": "search", "input": {}}]
        bad_text = str(blocks)
        vault.append([{
            "fp": "x1", "ts": "2026-10-22T20:00:00+08:00", "day": "2026-10-22",
            "role": "assistant", "origin": "astrbot_context", "session": "s",
            "text": bad_text, "raw": json.dumps(blocks, ensure_ascii=False),
            "tokens": 5, "persona": None, "backfill": True,
        }])
        assert index.rebuild(vault) == 1
        row = index.get("x1")
        assert row["text"].startswith("[思考]"), row["text"]
        assert "[工具] search" in row["text"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_parse_reminder_time():
    """AstrBot 把「当前时间」塞在用户消息里，这是唯一的真实时间来源。"""
    text = ("你好呀\n<system_reminder>Current datetime: 2026-10-20 21:56 (CST), "
            "Weekday: Tuesday</system_reminder>")
    got = sync.parse_reminder_time(text)
    assert got is not None
    assert got.strftime("%Y-%m-%d %H:%M") == "2026-10-20 21:56"
    assert sync.parse_reminder_time("没有时间戳的普通消息") is None


def test_sync_uses_reminder_time_and_carries_forward():
    """用户消息用自带的真实时间；助手回复沿用上一条的时间。"""
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        reminder = "<system_reminder>Current datetime: 2026-10-20 21:56 (CST)</system_reminder>"
        parsed = [
            {"idx": 0, "role": "user", "content": "第一句\n" + reminder, "raw": None},
            {"idx": 1, "role": "assistant", "content": "第一句的回复", "raw": None},
            {"idx": 2, "role": "user",
             "content": "第二句\n<system_reminder>Current datetime: 2026-10-21 09:30 (CST)</system_reminder>",
             "raw": None},
            {"idx": 3, "role": "assistant", "content": "第二句的回复", "raw": None},
        ]
        sync.sync(vault, index, parsed, persona="p", session="s")

        first = index.get(sync.fp_of("user\x00第一句\n" + reminder))
        reply = index.get(sync.fp_of("assistant\x00第一句的回复"))
        assert first["ts"].startswith("2026-10-20T21:56")
        assert reply["ts"].startswith("2026-10-20T21:56"), "助手回复应沿用用户那条的时间"
        assert first["backfill"] is False
        assert reply["backfill"] is False, "从提醒里还原出来的时间不算回填"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_rebuild_recovers_timestamps():
    """重建索引时用提醒里的时间覆盖归档时间，并清掉回填标记。"""
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        reminder = "<system_reminder>Current datetime: 2026-10-20 21:56 (CST)</system_reminder>"
        vault.append([
            {"fp": "u1", "ts": "2026-10-22T20:17:21+08:00", "day": "2026-10-22",
             "role": "user", "origin": "astrbot_context", "session": "s",
             "text": "第一句\n" + reminder, "raw": None, "tokens": 5,
             "persona": None, "backfill": True},
            {"fp": "a1", "ts": "2026-10-22T20:17:21+08:00", "day": "2026-10-22",
             "role": "assistant", "origin": "astrbot_context", "session": "s",
             "text": "回复", "raw": None, "tokens": 5, "persona": None,
             "backfill": True},
        ])
        assert index.rebuild(vault) == 2
        u = index.get("u1")
        a = index.get("a1")
        assert u["ts"].startswith("2026-10-20T21:56")
        assert a["ts"].startswith("2026-10-20T21:56")
        assert u["backfill"] is False and a["backfill"] is False
        assert u["day"] == "2026-10-20"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_map_covers_all_history():
    """地图从第一天到现在全在，越久越粗，不是砍掉早期。"""
    rows = [
        {"day": "2026-10-22", "count": 40, "samples": ["今天"]},
        {"day": "2026-10-21", "count": 30, "samples": ["昨天"]},
        {"day": "2025-06-01", "count": 12, "samples": ["很久以前"]},
        {"day": "2025-05-01", "count": 8, "samples": ["更久"]},
    ]
    block = inject.build_map_block(rows, budget=4000, detail_days=1)

    assert "共 4 天" in block, "天数要覆盖全部历史"
    assert "90 条" in block, "总条数要含早期数据"
    assert "2025-06-01" in block or "2025-06" in block, "早期记忆必须在图上"
    assert "2026-10-22" in block, "最近一天要按天列出"


def test_map_degrades_to_month_when_budget_tight():
    rows = [{"day": f"2026-{m:02d}-{d:02d}", "count": 5, "samples": ["x"]}
            for m in range(1, 13) for d in range(1, 29)]
    block = inject.build_map_block(rows, budget=120, detail_days=30)
    assert block, "预算再紧也要给出可读的地图"
    assert "2026-01" in block or "共 336 天" in block


def test_recall_fills_budget_beyond_old_cap():
    """旧版被 12 条卡死，实际预算只用了零头；现在按预算装满。"""
    hits = [{"ts": "2026-10-20T21:56:00+08:00", "role": "user",
             "text": ("这是第 %d 条回忆内容。" % i) + "内容" * 60}
            for i in range(80)]
    block = inject.build_recall_block(hits, budget=3000, max_chars=800)
    assert block.count("- [") > 12, "不该再被 12 条限制住"
    assert block.count("- [") < 80, "也不能无视预算全倒进去"


def test_backup_keeps_empty_directories():
    """空目录也要留条目，否则解压回来会少文件夹。"""
    import zipfile
    root = _tmp()
    try:
        os.makedirs(os.path.join(root, "attachments"), exist_ok=True)
        os.makedirs(os.path.join(root, "webchat", "imgs"), exist_ok=True)
        with open(os.path.join(root, "data_v4.db"), "w", encoding="utf-8") as f:
            f.write("db")

        target = os.path.join(_tmp(), "out.zip")
        backup.build_zip(target, [(root, "astrbot_data")])

        with zipfile.ZipFile(target) as zf:
            names = set(zf.namelist())
        assert "astrbot_data/attachments/" in names, "空目录要有条目"
        assert "astrbot_data/webchat/imgs/" in names, "嵌套空目录也要有"
        assert "astrbot_data/data_v4.db" in names
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 设置

def test_settings_clamped_to_allowed_range():
    root = _tmp()
    try:
        settings = settings_mod.Settings(root)
        settings.save({"trigger_ratio": 0.95, "total_ratio": 0.10})
        values = settings.effective({})
        assert values["trigger_ratio"] == 0.80, "上限必须钳到 80%"
        assert values["total_ratio"] == 0.30, "下限必须钳到 30%"

        settings.save({"trigger_ratio": 0.60, "total_ratio": 0.35})
        values = settings.effective({})
        assert values["trigger_ratio"] == 0.60
        assert values["total_ratio"] == 0.35
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_settings_counts_clamped():
    """计数型设置也要钳制，且取出来是整数。"""
    root = _tmp()
    try:
        settings = settings_mod.Settings(root)
        settings.save({"recall_limit": 999, "map_days": 0})
        values = settings.effective({})
        assert values["recall_limit"] == 200
        assert values["map_days"] == 1
        assert isinstance(values["recall_limit"], int)

        settings.save({"recall_limit": 20})
        assert settings.get("recall_limit") == 20
        assert settings_mod.is_runtime("recall_limit") is True
        assert settings_mod.is_runtime("window_tokens") is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_settings_bool_toggle():
    """演练模式这类开关也能在面板里改。"""
    root = _tmp()
    try:
        settings = settings_mod.Settings(root)
        assert settings.effective({})["dry_run"] is False, "默认不演练"
        settings.save({"dry_run": "true"})
        assert settings.get("dry_run") is True
        settings.save({"dry_run": 0})
        assert settings.get("dry_run") is False
        assert settings_mod.is_runtime("dry_run") is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_settings_effective_prefers_plugin_config():
    """
    设置页（插件配置）是唯一事实来源。

    settings.json 只在配置没给这一项时才补位——否则面板改完、设置页再改，
    旧值会一直压着新值，就变成「设置页改了面板能看到、面板改了设置页看不到」
    的单向同步（就是之前那个 bug）。
    """
    root = _tmp()
    try:
        settings = settings_mod.Settings(root)
        settings.save({"recall_limit": 12, "map_days": 295,
                       "trigger_ratio": 0.7})

        got = settings.effective({"recall_limit": 50, "map_days": 365})
        assert got["recall_limit"] == 50, got
        assert got["map_days"] == 365, got
        # 配置没给的项，才回落到存过的旧值
        assert got["trigger_ratio"] == 0.7, got

        # 默认值也得是对的
        bare = settings.effective({})
        assert settings_mod.DEFAULTS["recall_limit"] == 50
        assert settings_mod.DEFAULTS["map_days"] == 365
        assert bare["recall_limit"] == 12, "存过的值仍然兜底"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 附件

def test_media_blob_content_addressed():
    root = _tmp()
    try:
        payload = b"\x89PNG fake image bytes"
        h1, p1 = media_mod.save_blob(root, payload, ".png")
        h2, p2 = media_mod.save_blob(root, payload, ".png")
        assert h1 == h2 and p1 == p2, "相同内容只存一份"
        assert os.path.isfile(os.path.join(root, p1))

        media = media_mod.MediaStore(root)
        media.append_record("msg1", "qq:1", [
            {"kind": "image", "hash": h1, "path": p1, "size": len(payload),
             "name": "a.png", "mime": "image/png"}])
        assert len(media.records_for({"msg1"})["msg1"]) == 1
        assert media.records_for({"other"}) == {}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 全量提炼

def test_chunk_events_splits_by_chars():
    events = [{"fp": f"f{i}", "role": "user", "text": "字" * 100}
              for i in range(30)]
    chunks = refine_mod.chunk_events(events, chunk_chars=500)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 30
    assert all(len(c) <= 5 for c in chunks)


def test_chunk_events_keeps_everything():
    """
    不设批数上限：批数 = 总字数 ÷ 每批字数，一条都不能丢。

    超过每批字数的单条会按字符切成几段，每段都指向同一条原文（同一个 fp），
    文本本身一个字不少。
    """
    events = [{"fp": f"f{i}", "role": "user", "text": "x" * 100}
              for i in range(20)]
    chunks = refine_mod.chunk_events(events, chunk_chars=100)
    assert len(chunks) == 20, f"20 条各 100 字按 100 字切应是 20 批，实际 {len(chunks)}"
    assert [c[0]["fp"] for c in chunks] == [f"f{i}" for i in range(20)]
    assert sum(len(c) for c in chunks) == 20

    # 单条 250 字、每批 100 字 → 切成 3 段，拼回去还是原文
    long_one = [{"fp": "L", "role": "assistant", "text": "abcdefghij" * 25}]
    pieces = [event["text"]
              for chunk in refine_mod.chunk_events(long_one, chunk_chars=100)
              for event in chunk]
    assert len(pieces) == 3
    assert "".join(pieces) == "abcdefghij" * 25, "切段不能丢字"
    assert all(len(p) <= 100 for p in pieces)
    assert all(event["fp"] == "L" for chunk in refine_mod.chunk_events(
        long_one, chunk_chars=100) for event in chunk), "切段仍指向同一条原文"

    assert refine_mod.chunk_events([], chunk_chars=6000) == []


def test_map_prompt_keeps_full_text_and_fact_limit():
    """正文不再截 300 字；每批条数由预算换算后写进 prompt；发送者跟着行号走。"""
    text = "长" * 900
    prompt, fps, senders = refine_mod.build_map_prompt(
        [{"fp": "s1", "role": "user", "text": text}], [], max_facts=17)
    assert "最多 17 条" in prompt
    assert text in prompt, "正文不能被截断"
    assert fps == ["s1"]
    assert senders == [""]
    assert "|未知|" in prompt, "没有发送者时也要给出占位，别让模型乱编 ID"


def test_dedupe_and_budget():
    claims = [
        {"claim": "他怕黑", "kind": "fact", "source_fp": ["a"]},
        {"claim": "他怕黑 ", "kind": "preference", "source_fp": ["b"]},
        {"claim": "她在考研", "kind": "identity", "source_fp": ["c"]},
    ]
    merged = refine_mod.dedupe_claims(claims)
    assert len(merged) == 2, "同一件事的不同写法应合并"
    assert set(merged[0]["source_fp"]) == {"a", "b"}
    assert merged[0]["kind"] == "preference", \
        "合并时更具体的类型要盖过 fact，别被先到的那条吃掉"
    assert refine_mod.total_tokens(merged) > 0

    long_claim = "这是一条很长的说法" * 20
    kept = refine_mod.dedupe_claims([{"claim": long_claim, "source_fp": ["d"]}])
    assert kept[0]["claim"] == long_claim, "事实不再被截断"


def test_refine_run_writes_facts_with_provenance():
    """端到端：事实写入带来源、带类型、带主角；模型编的 ID 不能信。"""
    root = _tmp()
    try:
        store = extract.FactStore(root)

        async def fake_llm(prompt: str) -> str:
            return ('[{"claim": "他怕黑，睡觉要留灯", "kind": "preference",'
                    ' "subject": "u1", "scope": "people", "sources": [1]},'
                    ' {"claim": "他编了一个人", "kind": "fact",'
                    ' "subject": "u-bogus", "sources": [1]}]')

        events = [{"fp": "src1", "role": "user", "sender": "u1",
                   "text": "我怕黑，睡觉要留灯"}]
        result = asyncio.run(
            refine_mod.run(fake_llm, events, store, target_tokens=2000))

        assert result["written"] == 2
        facts = {f["versions"][-1]["claim"]: f for f in store.all_facts()}
        fear = facts["他怕黑，睡觉要留灯"]
        assert fear["versions"][-1]["source_fp"] == ["src1"], "必须能回溯到原话"
        assert fear["versions"][-1]["kind"] == "preference", \
            "kind 不能在提炼环节丢掉，否则权重排序全变成 fact"
        assert fear["subject"] == "u1"
        assert fear["scope"] == "people"
        assert facts["他编了一个人"]["subject"] == "", \
            "模型给的 subject 不在本批发送者里，必须丢掉"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_refine_covers_every_batch():
    """批数只由字数决定：不砍尾部，每批都真的调用一次。"""
    root = _tmp()
    try:
        store = extract.FactStore(root)
        calls = []

        async def fake_llm(prompt: str) -> str:
            calls.append(prompt)
            return "[]"

        events = [{"fp": f"f{i}", "role": "user", "text": "字" * 60}
                  for i in range(10)]
        result = asyncio.run(refine_mod.run(
            fake_llm, events, store, target_tokens=2000,
            chunk_chars=60, max_facts=5))

        assert result["batches"] == 10, result
        assert len(calls) == 10, f"每一批都要调用，实际只有 {len(calls)} 次"
        assert all("最多 5 条" in prompt for prompt in calls), \
            "每批条数应写进 prompt"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_refine_drops_claims_without_source():
    """没有出处的提炼一律丢弃——这是提炼层的底线。"""
    root = _tmp()
    try:
        store = extract.FactStore(root)

        async def fake_llm(prompt: str) -> str:
            return '[{"claim": "无出处的猜测", "sources": []}]'

        result = asyncio.run(
            refine_mod.run(fake_llm, [{"fp": "x", "role": "user", "text": "嗯"}],
                           store, target_tokens=2000))
        assert result["written"] == 0
        assert store.all_facts() == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_refine_replaces_stale_facts():
    """替换语义：旧事实不会残留，旧版进归档。"""
    root = _tmp()
    try:
        store = extract.FactStore(root)
        store.upsert("早就过时的旧事实", ["old"])

        async def fake_llm(prompt: str) -> str:
            return '[{"claim": "新的长期事实", "sources": [1]}]'

        events = [{"fp": "src9", "role": "user", "text": "我现在在学吉他"}]
        result = asyncio.run(
            refine_mod.run(fake_llm, events, store, target_tokens=2000))

        assert result["replaced"] is True
        claims = store.current_claims()
        assert claims == ["新的长期事实"], f"旧事实应被替换掉: {claims}"
        assert os.path.isdir(store.archive_root), "旧版应归档"
        assert os.listdir(store.archive_root), "归档目录不能是空的"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_refine_keeps_old_facts_when_all_batches_fail():
    """全部批次失败时绝不能替换，否则认知被清空成残缺集合。"""
    root = _tmp()
    try:
        store = extract.FactStore(root)
        store.upsert("重要的旧事实", ["old"])

        async def broken_llm(prompt: str) -> str:
            raise RuntimeError("模型挂了")

        events = [{"fp": "s1", "role": "user", "text": "随便说点"}]
        result = asyncio.run(
            refine_mod.run(broken_llm, events, store, target_tokens=2000))

        assert result["written"] == 0
        assert store.current_claims() == ["重要的旧事实"], "失败时必须保留旧事实"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 重复与重试

def test_repeats_are_kept_when_forced():
    """
    同一句话重复出现不能被吞掉。

    你说十次「哈哈哈」就是十条；她说十次「晚安」也是十条。
    force=True（一轮结束时的归档）每次都写，只把指纹编成 base-2/-3，
    历史重扫（force=False）才按 base 跳过，否则每轮都会翻倍。
    """
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        one = [{"idx": 0, "role": "assistant", "content": "晚安", "raw": None}]

        for _ in range(3):
            sync.sync(vault, index, one, "", session="s",
                      origin="astrbot_turn", force=True)

        rows = list(vault.iter_events())
        assert len(rows) == 3, f"三次晚安应该三条，实际 {len(rows)}"
        assert len({r["fp"] for r in rows}) == 3, "每条要有自己的指纹"
        assert index.count() == 3

        # 下一轮把历史整段重发过来：一条都不该新增
        stats = sync.sync(vault, index, one, "", session="s")
        assert stats["new"] == 0, stats
        assert index.count() == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_duplicates_in_one_batch_are_kept():
    """同一次扫到好几条一样的话（回填历史），也要全留下。"""
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        batch = [{"idx": i, "role": "user", "content": "哈哈哈", "raw": None}
                 for i in range(3)]

        stats = sync.sync(vault, index, batch, "", session="s")
        assert stats["new"] == 3, stats
        assert index.count() == 3
        assert len({r["fp"] for r in vault.iter_events()}) == 3

        # 同样的内容再来一次：按 base 指纹全部跳过
        assert sync.sync(vault, index, batch, "", session="s")["new"] == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_failed_queue_roundtrip():
    """归档失败的轮次要能落盘、能读回、补写完再删。"""
    root = _tmp()
    try:
        queue = pending_mod.FailedQueue(root)
        assert queue.count() == 0
        assert queue.push("s1", []) == "", "空内容不入队"

        path = queue.push("s1", [{"role": "user", "content": "这一轮丢了"},
                                 {"role": "assistant", "content": "回复也丢了"}],
                          persona="p1")
        assert path and os.path.isfile(path)
        assert queue.count() == 1

        items = queue.pending()
        assert len(items) == 1
        saved = items[0][1]
        assert saved["session"] == "s1"
        assert saved["persona"] == "p1"
        assert [i["content"] for i in saved["items"]] == ["这一轮丢了", "回复也丢了"]

        # 指定别的会话取不到
        assert queue.pending(session="s2") == []

        queue.discard(items[0][0])
        assert queue.count() == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_refine_cache_reuses_unchanged_batches():
    """缓存命中就不调模型：只有新内容才会产生调用。"""
    root = _tmp()
    try:
        store = extract.FactStore(root)
        cache = refine_mod.ExtractionCache(root)
        # 每条内容都不同，才是一批一个 key（内容一样的批会共用同一个缓存）
        events = [{"fp": f"f{i}", "role": "user",
                   "text": f"第{i}条：" + "字" * 50}
                  for i in range(10)]
        calls = []

        async def fake_llm(prompt: str) -> str:
            calls.append(prompt)
            return '[{"claim": "一条事实", "sources": [1]}]'

        first = asyncio.run(refine_mod.run(
            fake_llm, events, store, target_tokens=2000,
            chunk_chars=60, max_facts=5, cache=cache))
        assert first["cached_batches"] == 0
        assert len(calls) == 10

        second = asyncio.run(refine_mod.run(
            fake_llm, events, store, target_tokens=2000,
            chunk_chars=60, max_facts=5, cache=cache))
        assert second["cached_batches"] == 10, "整库没变，应该全部命中"
        assert len(calls) == 10, "命中缓存不该再调模型"

        grown = events + [{"fp": "f10", "role": "user",
                           "text": "第10条：" + "新" * 50}]
        third = asyncio.run(refine_mod.run(
            fake_llm, grown, store, target_tokens=2000,
            chunk_chars=60, max_facts=5, cache=cache))
        assert third["cached_batches"] == 10
        assert len(calls) == 11, "只该为新增那一批调用一次"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_split_turn_isolates_current_turn():
    """从 run_context.messages 里只切出这一轮，历史不能跟着重写。"""
    from types import SimpleNamespace

    def msg(role, text):
        return SimpleNamespace(role=role, content=text)

    history = [msg("user", "旧问题"), msg("assistant", "旧回答")]
    turn = [msg("user", "晚安"), msg("assistant", "晚安")]
    messages = [msg("system", "你是小木")] + history + turn

    picked, ok = sync.split_turn(messages, 2, sync.message_fp(history[-1]))
    assert ok is True
    assert [m.content for m in picked] == ["晚安", "晚安"]

    # 边界对不上（宿主插了别的消息）→ 不可信，调用方要退回去重模式
    _picked, bad = sync.split_turn(messages, 2, "deadbeefdeadbeef")
    assert bad is False


def test_repeat_across_turns_is_not_swallowed():
    """跨轮重复的同一句话，第二轮也要留下（旧版会被指纹去重吞掉）。"""
    from types import SimpleNamespace
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)

        def msg(role, text):
            return SimpleNamespace(role=role, content=text)

        def archive(messages, context_count, last_fp):
            turn, exact = sync.split_turn(messages, context_count, last_fp)
            parsed = sync.turn_messages(turn)
            return sync.sync(vault, index, parsed, "", session="s",
                             origin="astrbot_turn", force=exact)

        # 第一轮：历史为空
        turn1 = [msg("user", "哈哈哈"), msg("assistant", "笑得真开心")]
        archive([msg("system", "sys")] + turn1, 0, "")

        # 第二轮：历史是上一轮，用户又说了一遍「哈哈哈」
        history = turn1
        turn2 = [msg("user", "哈哈哈"), msg("assistant", "又说一遍")]
        last_fp = sync.message_fp(history[-1])
        archive([msg("system", "sys")] + history + turn2, 2, last_fp)

        texts = [r["text"] for r in vault.iter_events() if r["role"] == "user"]
        assert texts.count("哈哈哈") == 2, f"两轮的「哈哈哈」都要留下: {texts}"
        assert len({r["fp"] for r in vault.iter_events()}) == len(texts) + 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 备份恢复

def _make_backup_zip(path: str, plugin_root: str = "all_memory") -> None:
    """造一个和真实导出包结构一致的 zip。"""
    import zipfile
    events = [
        {"fp": "r1", "ts": "2026-10-20T21:56:00+08:00", "day": "2026-10-20",
         "role": "user", "origin": "astrbot_context", "session": "s",
         "text": "很久以前的一句话", "raw": None, "tokens": 5,
         "persona": "p1", "backfill": False},
        {"fp": "r2", "ts": "2026-10-20T21:57:00+08:00", "day": "2026-10-20",
         "role": "assistant", "origin": "astrbot_context", "session": "s",
         "text": "回复", "raw": None, "tokens": 5, "persona": "p1",
         "backfill": False},
    ]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{plugin_root}/vault/2026-10-20.jsonl",
                    "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")
        zf.writestr(f"{plugin_root}/personas/p1.txt", "你是小木")
        zf.writestr(f"{plugin_root}/facts/fact-1.json",
                    json.dumps({"id": "fact-1", "versions": [{"claim": "怕黑"}]},
                               ensure_ascii=False))
        zf.writestr(f"{plugin_root}/blobs/ab/abcd.jpg", b"\xff\xd8jpeg")
        zf.writestr(f"{plugin_root}/media/2026-10-20.jsonl",
                    json.dumps({"fu": "r1", "session": "s",
                                "ts": "2026-10-20T21:56:00+08:00",
                                "items": [{"kind": "image", "hash": "abcd",
                                           "path": "blobs/ab/abcd.jpg",
                                           "size": 6, "name": "a.jpg"}]},
                               ensure_ascii=False) + "\n")
        # 面板设置 / 会话状态 / 旧事实归档
        zf.writestr(f"{plugin_root}/settings.json",
                    json.dumps({"trigger_ratio": 0.75, "total_ratio": 0.45,
                                "recall_limit": 30}))
        zf.writestr(f"{plugin_root}/state/s.json",
                    json.dumps({"trimmed_mode": True, "trim_count": 2}))
        zf.writestr(f"{plugin_root}/facts_archive/20261020-120000/fact-0.json",
                    json.dumps({"id": "fact-0"}))
        zf.writestr("astrbot_data/data_v4.db", b"fake-db")


def test_restore_merges_and_is_idempotent():
    root = _tmp()
    src = os.path.join(_tmp(), "backup.zip")
    try:
        _make_backup_zip(src)

        info = restore_mod.inspect_zip(src)
        assert info["ok"] is True
        assert info["vault_days"] == 1
        assert info["personas"] == 1
        assert info["facts"] == 1
        assert info["blobs"] == 1
        assert info["has_astrbot_data"] is True

        vault = Vault(root)
        index = Index(root)
        first = restore_mod.restore_zip(src, root, vault, index)
        assert first["ok"] is True
        assert first["events_added"] == 2
        assert first["personas_added"] == 1
        assert first["facts_added"] == 1
        assert first["blobs_added"] == 1
        assert first["media_added"] == 1
        assert first["astrbot_data_ignored"] is True
        assert first["sessions"] == ["s"], "要回报涉及哪些会话，便于恢复后开接管"
        assert first["settings_restored"] == 1, "面板设置要恢复"
        assert first["state_added"] == 1, "会话状态要恢复"
        assert first["archive_added"] == 1, "旧事实归档要恢复"
        assert index.count() == 2
        assert os.path.isfile(os.path.join(root, "blobs", "ab", "abcd.jpg"))

        # 再导一次：一条都不该重复
        second = restore_mod.restore_zip(src, root, vault, index)
        assert second["events_added"] == 0
        assert second["events_skipped"] == 2
        assert index.count() == 2, "重复导入不能把数据变多"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(os.path.dirname(src), ignore_errors=True)


def test_export_restore_keeps_repeated_messages():
    """
    这一版导出的包，重复消息（abc / abc-2 / abc-3）要原样回来。

    只保证「这一版导出 → 今后导入」；更早的包不再考虑。
    """
    src_root = _tmp()
    dst_root = _tmp()
    work = _tmp()
    try:
        vault = Vault(src_root)
        index = Index(src_root)
        one = [{"idx": 0, "role": "assistant", "content": "晚安", "raw": None}]
        for _ in range(3):
            sync.sync(vault, index, one, "p", session="s",
                      origin="astrbot_turn", force=True)
        assert len(list(vault.iter_events())) == 3

        zip_path = os.path.join(work, "all_memory.zip")
        info = backup.build_zip(zip_path, [(src_root, "all_memory")])
        assert info["files"] > 0

        dv = Vault(dst_root)
        di = Index(dst_root)
        first = restore_mod.restore_zip(zip_path, dst_root, dv, di)
        assert first["ok"] is True
        assert first["events_added"] == 3, first

        rows = list(dv.iter_events())
        assert [r["text"] for r in rows] == ["晚安", "晚安", "晚安"]
        assert len({r["fp"] for r in rows}) == 3, "重复消息要有各自的指纹"
        assert di.count() == 3

        # 同一个包再恢复一次：一条都不该重复
        again = restore_mod.restore_zip(zip_path, dst_root, dv, di)
        assert again["events_added"] == 0
        assert again["events_skipped"] == 3
        assert di.count() == 3
    finally:
        for path in (src_root, dst_root, work):
            shutil.rmtree(path, ignore_errors=True)


def test_restore_rejects_foreign_zip_and_traversal():
    import zipfile
    root = _tmp()
    work = _tmp()
    try:
        foreign = os.path.join(work, "foreign.zip")
        with zipfile.ZipFile(foreign, "w") as zf:
            zf.writestr("something/else.txt", "hi")
        assert restore_mod.inspect_zip(foreign)["ok"] is False

        evil = os.path.join(work, "evil.zip")
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("all_memory/vault/../evil.txt", "x")
            zf.writestr("all_memory/../../escape.txt", "x")
        vault = Vault(root)
        index = Index(root)
        restore_mod.restore_zip(evil, root, vault, index)
        assert not os.path.exists(os.path.join(root, "evil.txt"))
        assert not os.path.exists(os.path.join(os.path.dirname(root), "escape.txt"))
    finally:
        for p in (root, work):
            shutil.rmtree(p, ignore_errors=True)


def test_restore_can_reuse_copied_index():
    """备份里带 index.db 时，纯恢复可以直接沿用，不必重建。"""
    src_root = _tmp()
    dst_root = _tmp()
    zip_path = os.path.join(_tmp(), "withindex.zip")
    try:
        vault = Vault(src_root)
        index = Index(src_root)
        events = [
            {"fp": "k1", "ts": "2026-10-20T21:56:00+08:00", "day": "2026-10-20",
             "role": "user", "origin": "astrbot_context", "session": "s",
             "text": "第一句", "raw": None, "tokens": 3, "persona": None,
             "backfill": False},
            {"fp": "k2", "ts": "2026-10-20T21:57:00+08:00", "day": "2026-10-20",
             "role": "assistant", "origin": "astrbot_context", "session": "s",
             "text": "第二句", "raw": None, "tokens": 3, "persona": None,
             "backfill": False},
        ]
        vault.append(events)
        index.add(events)

        backup.build_zip(zip_path, [(src_root, "all_memory")])

        dv = Vault(dst_root)
        di = Index(dst_root)
        result = restore_mod.restore_zip(zip_path, dst_root, dv, di)

        assert result["index_copied"] == 1, "备份里的 index.db 应该被搬过来"
        info = di.verify()
        assert info["ok"] is True
        assert info["events"] == 2, "搬过来的索引条数要对得上"
    finally:
        for p in (src_root, dst_root, os.path.dirname(zip_path)):
            shutil.rmtree(p, ignore_errors=True)


def test_restore_keeps_legacy_duplicate_fingerprints():
    """
    旧包可能把重复消息写成同一个指纹，还散在不同天的文件里。

    恢复时要按出现顺序编号（dup / dup-2 / dup-3）全留下，
    而且同一个包导两次仍然不重复。
    """
    import zipfile

    root = _tmp()
    work = _tmp()
    zip_path = os.path.join(work, "legacy.zip")
    try:
        rows = [
            {"fp": "dup", "ts": f"2026-10-2{d}T10:00:00+08:00",
             "day": f"2026-10-2{d}", "role": "assistant",
             "origin": "astrbot_context", "session": "s", "text": "晚安",
             "raw": None, "tokens": 3, "persona": None, "backfill": False}
            for d in (0, 1, 2)
        ]
        with zipfile.ZipFile(zip_path, "w") as zf:
            for row in rows:
                zf.writestr(f"all_memory/vault/{row['day']}.jsonl",
                            json.dumps(row, ensure_ascii=False) + "\n")

        dv = Vault(root)
        di = Index(root)
        first = restore_mod.restore_zip(zip_path, root, dv, di)
        assert first["events_added"] == 3, first

        assert sorted(r["fp"] for r in dv.iter_events()) == [
            "dup", "dup-2", "dup-3"]
        assert di.count() == 3

        again = restore_mod.restore_zip(zip_path, root, dv, di)
        assert again["events_added"] == 0
        assert again["events_skipped"] == 3
        assert di.count() == 3
    finally:
        for path in (root, work):
            shutil.rmtree(path, ignore_errors=True)


def test_restore_rebuilds_stale_copied_index():
    """
    备份里的 index.db 可能落后于原文（导出那一刻索引还没写完）。

    这种索引不能沿用，否则恢复完检索会少一大截——对不上就重建。
    """
    root = _tmp()
    try:
        vault = Vault(root)
        index = Index(root)
        events = [
            {"fp": f"r{i}", "ts": f"2026-10-20T21:5{i}:00+08:00",
             "day": "2026-10-20", "role": "user", "origin": "astrbot_context",
             "session": "s", "text": f"第{i}句", "raw": None, "tokens": 3,
             "persona": None, "backfill": False}
            for i in range(5)
        ]
        vault.append(events)
        index.add(events[:2])          # 索引只写了 2 条，落后于 vault
        assert index.count() == 2

        stale = restore_mod.reconcile_index(vault, index, copied=True)
        assert stale["index_kept"] is False, "条数不符就不能沿用"
        assert stale["index_rebuilt"] == 5
        assert index.count() == 5

        # 条数一致时沿用，不白重建
        good = restore_mod.reconcile_index(vault, index, copied=True)
        assert good["index_kept"] is True
        assert good["index_events"] == 5
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_seconds_until_daily_export():
    """每天定时导出：当天没到就等，已过就顺延到明天。"""
    from datetime import datetime

    base = datetime(2026, 10, 22, 1, 0, 0)
    assert backup.seconds_until("03:00", now=base) == 7200

    later = datetime(2026, 10, 22, 4, 0, 0)
    assert backup.seconds_until("03:00", now=later) == 23 * 3600

    assert backup.seconds_until("", now=base) is None
    assert backup.seconds_until("25:00", now=base) is None
    assert backup.seconds_until("abc", now=base) is None


def test_media_records_for_matches_base_fp():
    """同一句话重复说（abc-2）时，附件仍要挂在基础指纹上，否则第二张图挂不上。"""
    root = _tmp()
    try:
        store = media_mod.MediaStore(root)
        store.append_record("abc", "s1", [{"kind": "image", "hash": "h1",
                                           "path": "blobs/h1.jpg", "size": 3}])
        got = store.records_for({"abc-2", "zzz"})
        assert "abc" in got
        assert got["abc"][0]["hash"] == "h1"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_backfill_images_from_astrbot_db():
    """插件晚装、或归档出过错时，把宿主库里内嵌的图补回来，且不重复。"""
    root = _tmp()
    try:
        store = media_mod.MediaStore(root)
        db = os.path.join(root, "data_v4.db")
        payload = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()
        url = "data:image/png;base64," + payload
        text = ("看这个\n<system_reminder>Current datetime: "
                "2026-10-22 21:00 (CST)</system_reminder>")
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": url}}],
        }]
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE conversations"
                     " (user_id TEXT, content TEXT, updated_at REAL)")
        conn.execute("INSERT INTO conversations VALUES (?,?,?)",
                     ("s1", json.dumps(messages, ensure_ascii=False), 1792723406.0))
        conn.commit()
        conn.close()

        stats = importer.backfill_media_from_astrbot(db, store)
        assert stats["images"] == 1 and stats["saved"] == 1, stats
        assert stats["bytes"] == 40

        fp = fp_of("user\x00" + text)
        got = store.records_for({fp})
        assert got and got[fp][0]["kind"] == "image"

        again = importer.backfill_media_from_astrbot(db, store)
        assert again["saved"] == 0 and again["skipped"] == 1, "重复跑不能重复存"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 检索

def test_split_terms_for_chinese_sentences():
    """整句当关键词是查不到的：要拆出短词，长的再切三字窗口。"""
    terms = query_mod.split_terms("我今天好累啊，你说我该不该辞职")
    assert "好累" not in terms, "两字太泛，不该单独出"
    assert any(len(t) == 3 for t in terms), "长句必须切出三字窗口"
    assert "我今天好累啊" in terms, "整段仍然保留，精确匹配时有用"

    short = query_mod.split_terms("考研")
    assert short == ["考研"]

    assert query_mod.split_terms("我 的 了") == [], "虚词拆不出东西"


def test_time_window_parses_relative_days():
    now = datetime(2026, 10, 23, 12, 0, 0)
    assert query_mod.time_window("昨天我们聊了什么", now) == ("2026-10-22", "2026-10-22")
    assert query_mod.time_window("10月20那天", now) == ("2026-10-20", "2026-10-20")
    assert query_mod.time_window("2026-10-21 晚上", now) == ("2026-10-21", "2026-10-21")
    assert query_mod.time_window("随便聊聊", now) == ("", "")


def test_local_embedding_is_similar_for_same_meaning_shape():
    """本地向量抓形态变化（累了/好累），抓不住真同义——那部分靠扩词。"""
    emb = vectors_mod.LocalEmbedder()
    a = emb.vector("我今天好累啊")
    b = emb.vector("好累，今天真的很累")
    c = emb.vector("B站视频下载失败")
    assert vectors_mod.cosine(a, b) > vectors_mod.cosine(a, c)


def test_vector_store_roundtrip_and_search():
    root = _tmp()
    try:
        emb = vectors_mod.LocalEmbedder()
        store = vectors_mod.VectorStore(root, emb.name)
        rows = [("f1", emb.vector("她怕黑，睡觉要留灯")),
                ("f2", emb.vector("B站视频下载失败"))]
        assert store.put(rows) == 2
        assert store.count() == 2
        assert store.has_many(["f1", "f9"]) == {"f1"}

        hits = store.search(emb.vector("睡觉怕黑"), limit=2)
        assert hits and hits[0][0] == "f1", f"应该是怕黑那条排前面: {hits}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_combine_fuses_literal_and_vector_with_time_bonus():
    literal = [[{"fp": "a", "ts": "2026-10-20T10:00:00+08:00", "role": "user",
                 "text": "甲"}],
               [{"fp": "b", "ts": "2026-10-22T10:00:00+08:00", "role": "user",
                 "text": "乙"}]]
    vector = [("c", 0.9)]
    payload = {"c": {"ts": "2026-10-22T11:00:00+08:00", "role": "tool", "text": "丙"}}
    got = search_mod.combine(literal, vector, fetch=payload.get, limit=5)
    assert {item["fp"] for item in got} == {"a", "b", "c"}

    # 时间命中给 a 加分：把它推进 10-20 那天
    boosted = search_mod.combine(
        literal, [], window=("2026-10-20", "2026-10-20"), limit=5)
    assert boosted[0]["fp"] == "a"

    # 工具输出降权
    tool_only = [{"fp": "t", "ts": "2026-10-22T10:00:00+08:00",
                  "role": "tool", "text": "工具"}]
    user_only = [{"fp": "u", "ts": "2026-10-22T10:00:00+08:00",
                  "role": "user", "text": "人"}]
    ranked = search_mod.combine([tool_only, user_only], [], limit=5)
    assert ranked[0]["fp"] == "u", "同样命中，用户说的话优先"


def test_refine_runs_batches_in_parallel():
    """并发跑批：结果和不并发时一致，只是更快。"""
    root = _tmp()
    try:
        store = extract.FactStore(root)
        calls = []

        async def fake_llm(prompt: str) -> str:
            calls.append(prompt)
            await asyncio.sleep(0)          # 让出控制权，模拟并发
            return '[{"claim": "她怕黑", "sources": [1]}]'

        events = [{"fp": f"p{i}", "role": "user", "text": "字" * 40}
                  for i in range(12)]
        result = asyncio.run(refine_mod.run(
            fake_llm, events, store, target_tokens=2000,
            chunk_chars=80, concurrency=4))
        assert result["batches"] == len(calls)
        assert result["written"] == 1, "同一句话跨批重复，去重后只留一条"
        assert len(store.all_facts()) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_day_messages_and_summary_show_opening():
    """问"第一次见面说了什么"要靠这天：样本要带开头，按天读要按顺序。"""
    root = _tmp()
    try:
        idx = Index(root)
        idx.add([
            {"fp": "a1", "ts": "2026-10-20T21:56:00+08:00", "day": "2026-10-20",
             "role": "user", "session": "s1", "text": "初次见面，你好小木"},
            {"fp": "a2", "ts": "2026-10-20T21:57:00+08:00", "day": "2026-10-20",
             "role": "assistant", "session": "s1", "text": "你好呀"},
            {"fp": "a3", "ts": "2026-10-20T23:30:00+08:00", "day": "2026-10-20",
             "role": "user", "session": "s1", "text": "晚安"},
            {"fp": "b1", "ts": "2026-10-21T20:00:00+08:00", "day": "2026-10-21",
             "role": "user", "session": "s1", "text": "第二天"},
        ])

        day = idx.day_messages("2026-10-20", limit=10)
        assert [item["fp"] for item in day] == ["a1", "a2", "a3"], "按写入顺序"
        assert idx.day_messages("2026-10-22") == []
        assert idx.day_count("2026-10-20") == 3
        page = idx.day_messages("2026-10-20", limit=2, offset=2)
        assert [item["fp"] for item in page] == ["a3"], "翻页要能读到尾巴"
        assert idx.last_fp("s1") == "b1", "回复附件要挂在本会话最后一条上"
        assert idx.last_fp("nope") == ""

        rows = {row["day"]: row for row in idx.day_summary(days=None)}
        assert "初次见面" in rows["2026-10-20"]["samples"][0], \
            "样本第一条必须是那天的开头，不能只给最近几条"
        assert "晚安" in " ".join(rows["2026-10-20"]["samples"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_earliest_intent_detection():
    """问"第一次/最早"时要能识别出来，后面才会把最早那天的原话递上去。"""
    assert query_mod.is_earliest_intent("还记得我们第一次见面说的话吗")
    assert query_mod.is_earliest_intent("我们刚认识的时候你在干嘛")
    assert query_mod.is_earliest_intent("最早那段聊天是什么")
    assert not query_mod.is_earliest_intent("今天吃什么")
    assert not query_mod.is_earliest_intent("帮我查一下B站视频")

    root = _tmp()
    try:
        idx = Index(root)
        idx.add([
            {"fp": "a1", "ts": "2026-10-19T09:00:00+08:00", "day": "2026-10-19",
             "role": "user", "session": "s1", "text": "最早那句"},
            {"fp": "b1", "ts": "2026-10-21T09:00:00+08:00", "day": "2026-10-21",
             "role": "user", "session": "s1", "text": "后来那句"},
        ])
        assert idx.earliest_day() == "2026-10-19"
        assert [m["fp"] for m in idx.day_messages("2026-10-19")] == ["a1"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_infer_sender_for_history():
    """历史消息没 sender 时怎么推：单人有真实值优先，私聊取尾号，群聊不猜。"""
    private = "小木:FriendMessage:1439600604"
    group = "小木:GroupMessage:123456"

    assert sync.infer_sender(private) == "1439600604", "私聊尾号就是对方 ID"
    assert sync.infer_sender(private, {"88888"}) == "88888", \
        "有真实发送者时以它为准，别用尾号另立一个身份"
    assert sync.infer_sender(private, {"88888", "99999"}) == "", "多人会话不猜"
    assert sync.infer_sender(group) == "", "群聊推不出是谁"
    assert sync.infer_sender("webchat:session:abc") == "", "认不出的格式就留空"
    assert sync.infer_sender("") == ""


# ---------------------------------------------------------------- 入口
def _run_all():
    tests = sorted(
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERR   {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
