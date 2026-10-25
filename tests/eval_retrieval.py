"""
eval_retrieval.py —— 离线跑 tests/retrieval_cases.json 的检索层。

两种用法：

1. 单策略（旧行为，只测字面）：
    python -B tests/eval_retrieval.py --index <all_memory>/index.db --uid 1439600604

2. 多策略对比：
    python -B tests/eval_retrieval.py --index <all_memory>/index.db --uid 1439600604 \
        --compare fts,fts+boost,local --copy --workdir _eval-snapshot

--compare 的三路：
  fts        旧基线：只走 index.search_many
  fts+boost  字面 + earliest/按天补位
  local      字面 + 本地哈希向量 + RRF + MMR + earliest/按天 + 前后文

不依赖 AstrBot；provider embedding / rerank 不在这里跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import query  # noqa: E402
from core import retrieve_pipeline as pipeline  # noqa: E402
from core.entities import EntityStore  # noqa: E402
from core.index import Index  # noqa: E402
from core.synonyms import SynonymStore  # noqa: E402
from core.vectors import LocalEmbedder, VectorStore  # noqa: E402

ISOLATION_UID = "__eval_isolation_probe__"


def _backup_db(src: str, dst: str) -> None:
    if os.path.exists(dst):
        os.remove(dst)
    src_conn = sqlite3.connect(src, timeout=10)
    dst_conn = sqlite3.connect(dst, timeout=10)
    try:
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        src_conn.close()
        dst_conn.close()


def _snapshot(index_path: str, vectors_path: str, workdir: str) -> tuple[str, str]:
    os.makedirs(workdir, exist_ok=True)
    target_index = os.path.join(workdir, "index.db")
    _backup_db(index_path, target_index)
    target_vectors = ""
    if vectors_path and os.path.isfile(vectors_path):
        target_vectors = os.path.join(workdir, "vectors.db")
        _backup_db(vectors_path, target_vectors)
    return target_index, target_vectors


def _expansions(root: str, uid: str, text: str) -> list[str]:
    out: list[str] = []
    for store in (EntityStore(root, uid), SynonymStore(root, uid)):
        try:
            for value in store.expand(text, limit=8):
                if value and value not in out:
                    out.append(value)
        except Exception:
            continue
    return out


def _local_flags(name: str) -> dict:
    return {
        "use_vector": name != "local-novector",
        "use_mmr": name != "local-nommr",
        "boost": name != "local-noboost",
    }


def _run(name: str, index, ctx: dict, question: str, uid: str) -> list[dict]:
    if name == "fts":
        return pipeline.fts_only(index, question, uid=uid, limit=20)
    if name == "fts+boost":
        return pipeline.retrieve_fts(index, question, uid=uid, limit=20,
                                     boost=True)
    if name in ("local", "local-novector", "local-nommr", "local-noboost"):
        expansions = ctx.get("expansions")
        if expansions is None:
            expansions = _expansions(ctx.get("root", ""), uid, question)
        flags = _local_flags(name)
        return pipeline.retrieve(
            index, question, uid=uid, limit=50,
            vectors=ctx.get("vectors"), embedder=ctx.get("embedder"),
            expansions=expansions,
            use_vector=flags["use_vector"], use_mmr=flags["use_mmr"],
            boost=flags["boost"], expand_context=True,
            candidate_multiplier=2.0)
    raise ValueError(f"unknown strategy: {name}")


def _cross_id(name: str, index, ctx: dict, question: str) -> int:
    """用不存在的 uid 跑一次，返回命中条数；应为 0。"""
    try:
        if name == "fts":
            hits = pipeline.fts_only(index, question, uid=ISOLATION_UID, limit=1)
        elif name == "fts+boost":
            hits = pipeline.retrieve_fts(index, question, uid=ISOLATION_UID,
                                         limit=1, boost=False)
        else:
            flags = _local_flags(name)
            hits = pipeline.retrieve(
                index, question, uid=ISOLATION_UID, limit=1,
                vectors=ctx.get("vectors"), embedder=ctx.get("embedder"),
                expansions=[], use_vector=flags["use_vector"],
                use_mmr=flags["use_mmr"], boost=False, expand_context=False)
        return len(hits)
    except Exception:
        return 0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[index]


def _metrics_for(name: str, cases: list[dict], index, ctx: dict,
                 uid: str, limit: int) -> dict:
    result = {
        "name": name, "passed": 0, "total": 0,
        "recall": {1: 0, 5: 0, 10: 0},
        "mrr": 0.0, "empty": 0, "negative_hits": 0, "negative_total": 0,
        "cross_id_hits": 0, "cross_id_cases": 0,
        "latencies": [], "by_category": {}, "failures": [], "cases": [],
    }
    for case in cases:
        question = str(case.get("question") or "")
        expected = [str(fp) for fp in (case.get("expected_fp") or [])]
        category = str(case.get("category") or "other")
        started = time.perf_counter()
        try:
            hits = _run(name, index, ctx, question, uid)
            error = ""
        except Exception as exc:
            hits = []
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        result["latencies"].append(elapsed_ms)

        fps = [str(hit.get("fp") or "") for hit in hits[:limit]]
        if not expected:
            result["negative_total"] += 1
            if hits:
                result["negative_hits"] += 1
            result["cases"].append({
                "case_id": case.get("case_id"), "category": category,
                "question": question, "expected": [], "top": fps,
                "negative_hit": bool(hits), "error": error,
            })
            continue

        result["total"] += 1
        result["cases"].append({
            "case_id": case.get("case_id"), "category": category,
            "question": question, "expected": expected, "top": fps,
            "error": error,
        })
        if not hits:
            result["empty"] += 1

        stat = result["by_category"].setdefault(category, [0, 0])
        stat[1] += 1
        hit_at = {}
        rank = 0
        for position, fp in enumerate(fps, 1):
            if fp in expected:
                rank = position
                break
        for k in (1, 5, 10):
            hit = any(fp in fps[:k] for fp in expected)
            hit_at[k] = hit
            if hit:
                result["recall"][k] += 1
        if hit_at.get(5):
            result["passed"] += 1
            stat[0] += 1
        if rank:
            result["mrr"] += 1.0 / rank
        else:
            result["failures"].append({
                "case_id": case.get("case_id"), "category": category,
                "question": question, "expected": expected, "top": fps,
                "error": error,
            })

        cross = _cross_id(name, index, ctx, question)
        result["cross_id_cases"] += 1
        if cross:
            result["cross_id_hits"] += cross

    total = max(1, result["total"])
    result["recall_rate"] = {k: v / total for k, v in result["recall"].items()}
    result["mrr"] = result["mrr"] / total
    result["p50"] = _percentile(result["latencies"], 0.50)
    result["p95"] = _percentile(result["latencies"], 0.95)
    return result


def _print_report(results: list[dict], limit: int) -> None:
    print(f"recall@{limit}（主指标）；分母只算有 expected_fp 的用例")
    header = (f"{'策略':<16}{'recall@1':>10}{'recall@5':>10}{'recall@10':>11}"
              f"{'MRR@10':>9}{'空结果':>8}{'p50':>8}{'p95':>8}"
              f"{'负向命中':>10}{'跨id':>7}")
    print(header)
    print("-" * len(header))
    for item in results:
        total = max(1, item["total"])
        print(
            f"{item['name']:<16}"
            f"{item['recall'][1]:>4}/{total:<5}"
            f"{item['recall'][5]:>4}/{total:<5}"
            f"{item['recall'][10]:>4}/{total:<5}"
            f"{item['mrr']:>8.3f}"
            f"{item['empty']:>8}"
            f"{item['p50']:>7.1f}ms"
            f"{item['p95']:>7.1f}ms"
            f"{item['negative_hits']:>4}/{item['negative_total']:<5}"
            f"{item['cross_id_hits']:>6}")

    print("\n分类（recall@5）：")
    categories = sorted({key for item in results
                         for key in item["by_category"]})
    for item in results:
        parts = []
        for category in categories:
            hit, count = item["by_category"].get(category, [0, 0])
            if count:
                parts.append(f"{category} {hit}/{count}")
        print(f"  {item['name']}: " + (" / ".join(parts) or "（无）"))


def _print_failures(results: list[dict]) -> None:
    for item in results:
        if not item["failures"]:
            continue
        print(f"\n[{item['name']}] 未命中 {len(item['failures'])} 条：")
        for failure in item["failures"]:
            print(json.dumps(failure, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True, help="index.db 路径")
    parser.add_argument("--uid", required=True, help="测试 id")
    parser.add_argument(
        "--cases",
        default=os.path.join(os.path.dirname(__file__), "retrieval_cases.json"),
        help="测试集 JSON")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--show-fails", action="store_true")
    parser.add_argument(
        "--compare", default="",
        help="逗号分隔：fts,fts+boost,local,local-novector,local-nommr,local-noboost；留空 = 只跑 fts")
    parser.add_argument(
        "--vectors", default="",
        help="vectors.db 路径；留空 = index 同目录的 vectors.db")
    parser.add_argument(
        "--copy", action="store_true",
        help="先把 index/vectors 备份到 --workdir，再跑（推荐）")
    parser.add_argument(
        "--workdir", default="",
        help="--copy 的快照目录；留空 = ./_eval-snapshot")
    parser.add_argument("--json-out", default="", help="把结果写成 JSON")
    args = parser.parse_args()

    index_path = os.path.abspath(args.index)
    if not os.path.isfile(index_path):
        print(f"index 不存在: {index_path}")
        return 2
    if not index_path.lower().endswith(".db"):
        print(f"index 不是 .db: {index_path}")
        return 2

    vectors_path = (os.path.abspath(args.vectors) if args.vectors
                    else os.path.join(os.path.dirname(index_path), "vectors.db"))

    if args.copy:
        workdir = os.path.abspath(args.workdir or os.path.join(os.getcwd(), "_eval-snapshot"))
        try:
            index_path, vectors_path = _snapshot(index_path, vectors_path, workdir)
        except (OSError, sqlite3.Error) as exc:
            print(f"快照失败：{exc}")
            print("如果是 DSH 沙箱，先把 index.db / vectors.db 复制到工作区再跑；"
                  "或直接用 --index 指向可读的副本。")
            return 2
        print(f"快照: {workdir}")

    root = os.path.dirname(index_path)
    index = Index(root)
    embedder = LocalEmbedder()
    vectors = None
    if vectors_path and os.path.isfile(vectors_path):
        try:
            vectors = VectorStore(root, embedder.name)
        except Exception:
            vectors = None
    ctx = {"vectors": vectors, "embedder": embedder, "root": root}

    with open(args.cases, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    cases = data.get("cases") or []

    names = [name.strip() for name in (args.compare or "fts").split(",")
             if name.strip()]
    allowed = ("fts", "fts+boost", "local", "local-novector",
               "local-nommr", "local-noboost")
    unknown = [name for name in names if name not in allowed]
    if unknown:
        print("不认识的策略: " + ", ".join(unknown))
        return 2

    results = [_metrics_for(name, cases, index, ctx, args.uid, args.limit)
               for name in names]

    _print_report(results, args.limit)
    if args.show_fails:
        _print_failures(results)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({
                "index": index_path,
                "vectors": vectors_path if vectors else "",
                "uid": args.uid,
                "limit": args.limit,
                "results": results,
            }, handle, ensure_ascii=False, indent=2)
        print(f"\nJSON: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
