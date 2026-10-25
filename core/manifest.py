"""
manifest.py —— M 快照说明与完整性校验

`all_memory/manifest.json` 回答四个问题：

1. 这是什么（schema / plugin_version / created_at）
2. 哪些是权威记忆，哪些是可重建的，哪些是运行态
3. 每个权威文件多大、sha256 是多少
4. 事实的出处、纪要的覆盖范围、附件本体是否都在

它不依赖 AstrBot，只用标准库；导出时写进 zip 的 `all_memory/` 根，
也可以单独对现有目录调用 `write()` / `verify()`。

设计边界
--------
- 不碰 vault：只读、只算 hash、只解析。
- 不给插件内部做兼容：schema 只保证 M 的读取，不保证旧插件数据。
- `files[]` 只覆盖 authoritative / history / metadata；
  `index.db`、`vectors.db`、`cache/` 是派生物，不进 checksum。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))

SCHEMA = "all_memory.manifest.v1"
MANIFEST_NAME = "manifest.json"
README_NAME = "README_M.md"
CONTAINER = "all_memory/"

# 权威层：未来 B0 真正需要读的东西。缺一个都算不完整。
AUTHORITATIVE_DIRS = (
    "vault",
    "facts",
    "summaries",
    "personas",
    "media",
    "blobs",
    "entities",
    "synonyms",
    "failed",
)
# 历史层：旧版 facts，可选，但带了就该校验。
HISTORY_DIRS = ("facts_archive",)
# 元数据：session → id 映射，恢复时有用，但不是记忆正文。
METADATA_FILES = ("state/_sessions.json",)
# 可重建：删了能从 vault 重算，不进 checksum。
REBUILDABLE_FILES = ("index.db", "vectors.db")
REBUILDABLE_DIRS = ("cache",)
# 运行态：跟当前进程/机器有关，读取器应忽略。
RUNTIME_DIRS = ("state", "exports", "facts_staging", "imports")
RUNTIME_FILES = ("settings.json",)
# zip 里可能出现，但不属于 M。
IGNORE_OUTSIDE = ("astrbot_data",)

REQUIRED_VAULT_FIELDS = ("fp", "ts", "day", "role", "text")

# 这些是派生/可变层：重炼、重新纪要、手改词典都会让它们变化。
# 在线目录校验时，缺文件/数量变少不应直接判失败；zip 快照仍然严格。
MUTABLE_PREFIXES = ("facts/", "facts_archive/", "summaries/",
                    "entities/", "synonyms/", "failed/")
MUTABLE_FILES = ("state/_sessions.json",)
MUTABLE_COUNTS = {"facts", "summaries", "entities", "synonyms", "failed", "files"}

_SKIP_SUFFIXES = (".tmp", ".part")


def _is_mutable(rel: str) -> bool:
    return rel in MUTABLE_FILES or rel.startswith(MUTABLE_PREFIXES)


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _norm(rel) -> str:
    return str(rel or "").replace("\\", "/").strip("/")


def _safe_rel(name: str) -> str:
    """只接受相对路径；挡掉绝对路径、盘符、`..`。"""
    rel = _norm(name)
    if not rel:
        return ""
    first = rel.split("/", 1)[0]
    if rel.startswith("/") or (len(first) == 2 and first[1] == ":"):
        return ""
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return ""
    return rel


def _temp_dir(parent: str = "") -> str:
    """
    取一个真的能写子目录的临时目录。

    某些受限环境（Windows 沙箱、AppContainer）里 tempfile.mkdtemp 建出来的
    目录 ACL 不允许再写子目录；这里用 os.makedirs 建普通目录，并实际探测
    一次子目录写入。优先 zip 所在目录（一定可写），再退系统临时目录。
    """
    candidates = []
    if parent:
        candidates.append(parent)
    candidates.append(tempfile.gettempdir())
    last_error = None
    for base in candidates:
        path = os.path.join(base, f"allmem-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            os.makedirs(os.path.join(path, "probe", "sub"), exist_ok=False)
            os.rmdir(os.path.join(path, "probe", "sub"))
            os.rmdir(os.path.join(path, "probe"))
            return path
        except OSError as exc:
            last_error = exc
            shutil.rmtree(path, ignore_errors=True)
            continue
    raise OSError(f"没有可写的临时目录: {last_error}")


def _sha256(path: str, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _count_lines(path: str) -> int:
    total = 0
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                total += 1
    return total


def _iter_files(root: str):
    """按路径排序遍历文件；跳过临时文件。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(_SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            yield rel, full


def classify(rel: str) -> str:
    """把一个相对路径归到 manifest 的某一类。"""
    rel = _norm(rel)
    if rel in (MANIFEST_NAME, README_NAME):
        return "manifest"
    if rel in METADATA_FILES:
        return "metadata"
    if rel in REBUILDABLE_FILES:
        return "rebuildable"
    if any(rel == d or rel.startswith(d + "/") for d in REBUILDABLE_DIRS):
        return "rebuildable"
    if any(rel == d or rel.startswith(d + "/") for d in HISTORY_DIRS):
        return "history"
    if any(rel == d or rel.startswith(d + "/") for d in RUNTIME_DIRS):
        return "runtime"
    if rel in RUNTIME_FILES:
        return "runtime"
    if any(rel == d or rel.startswith(d + "/") for d in AUTHORITATIVE_DIRS):
        return "authoritative"
    return "unknown"


def _empty_counts() -> dict:
    return {
        "events": 0,
        "events_by_uid": {},
        "days": 0,
        "ids": 0,
        "facts": 0,
        "summaries": 0,
        "personas": 0,
        "media_records": 0,
        "blobs": 0,
        "failed": 0,
        "entities": 0,
        "synonyms": 0,
        "files": 0,
    }


def scan(root: str, with_hashes: bool = True) -> dict:
    """
    扫一遍 `all_memory/`。

    with_hashes=False 时只数数，不读文件算 hash；verify 用它做二次对账，
    避免把每个 blob 读两遍。
    """
    counts = _empty_counts()
    files: list[dict] = []
    ids: set[str] = set()
    days: set[str] = set()
    events_by_uid: dict[str, int] = {}
    unknown: list[str] = []
    file_count = 0

    for rel, full in _iter_files(root):
        category = classify(rel)
        if category == "unknown":
            unknown.append(rel)
            continue

        if category in ("authoritative", "history", "metadata"):
            file_count += 1
            if with_hashes:
                try:
                    size = os.path.getsize(full)
                    digest = _sha256(full)
                except OSError:
                    continue
                files.append({"path": rel, "size": size, "sha256": digest})

        parts = rel.split("/")
        if rel.startswith("vault/") and rel.endswith(".jsonl"):
            uid = parts[1] if len(parts) >= 3 else "default"
            try:
                number = _count_lines(full)
            except OSError:
                number = 0
            counts["events"] += number
            events_by_uid[uid] = events_by_uid.get(uid, 0) + number
            ids.add(uid)
            days.add(os.path.basename(rel)[:-6])
        elif rel.startswith("facts/") and rel.endswith(".json"):
            counts["facts"] += 1
        elif rel.startswith("summaries/") and rel.endswith("/summary.json"):
            counts["summaries"] += 1
        elif rel.startswith("personas/") and rel.endswith(".txt"):
            counts["personas"] += 1
        elif rel.startswith("media/") and rel.endswith(".jsonl"):
            try:
                counts["media_records"] += _count_lines(full)
            except OSError:
                pass
        elif rel.startswith("blobs/"):
            counts["blobs"] += 1
        elif rel.startswith("failed/") and rel.endswith(".json"):
            counts["failed"] += 1
        elif rel.startswith("entities/") and rel.endswith(".json"):
            counts["entities"] += 1
        elif rel.startswith("synonyms/") and rel.endswith(".json"):
            counts["synonyms"] += 1

    counts["events_by_uid"] = dict(sorted(events_by_uid.items()))
    counts["days"] = len(days)
    counts["ids"] = len(ids)
    counts["files"] = file_count

    return {
        "files": files,
        "counts": counts,
        "ids": sorted(ids),
        "unknown": sorted(unknown),
    }


def build(root: str, plugin_version: str = "", models=None,
          prompt_versions=None) -> dict:
    """从目录生成 manifest；不写盘。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ValueError(f"不是目录: {root}")
    data = scan(root, with_hashes=True)
    return {
        "schema": SCHEMA,
        "created_at": _now_iso(),
        "plugin_version": str(plugin_version or "unknown"),
        "base": MANIFEST_NAME,
        "container": CONTAINER,
        "ids": data["ids"],
        "counts": data["counts"],
        "authoritative": [name + "/" for name in AUTHORITATIVE_DIRS],
        "history": [name + "/" for name in HISTORY_DIRS],
        "metadata": list(METADATA_FILES),
        "rebuildable": list(REBUILDABLE_FILES) + [name + "/" for name in REBUILDABLE_DIRS],
        "runtime": [name + "/" for name in RUNTIME_DIRS] + list(RUNTIME_FILES),
        "ignore": [name + "/" for name in IGNORE_OUTSIDE],
        "models": dict(models or {}),
        "prompt_versions": dict(prompt_versions or {}),
        "files": data["files"],
        "unknown": data["unknown"],
        "notes": (
            "files[].path 相对于 manifest.json；container 是 zip 内的前缀。"
            "authoritative 是记忆本体；rebuildable/runtime 读取器应忽略。"
        ),
    }


def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def write(root: str, plugin_version: str = "", models=None,
          prompt_versions=None) -> dict:
    """把 manifest.json + README_M.md 写进目录；返回 manifest。"""
    root = os.path.abspath(root)
    manifest = build(root, plugin_version=plugin_version, models=models,
                     prompt_versions=prompt_versions)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2,
                         sort_keys=True).encode("utf-8")
    _atomic_write(os.path.join(root, MANIFEST_NAME), payload)
    _atomic_write(os.path.join(root, README_NAME),
                  readme_text(manifest).encode("utf-8"))
    return manifest


def _fp_exists(fp: str, known: set[str]) -> bool:
    """facts 的 source_fp 可能只记基础指纹（abc），库里存的是 abc-2。"""
    value = str(fp or "")
    if not value:
        return False
    return value in known or value.split("-", 1)[0] in known


def _finish(result: dict, errors: list[str], warnings: list[str],
            max_errors: int) -> dict:
    result["error_count"] = len(errors)
    result["warning_count"] = len(warnings)
    result["errors"] = errors[:max(1, int(max_errors or 50))]
    result["warnings"] = warnings[:max(1, int(max_errors or 50))]
    result["ok"] = not errors
    return result


def verify(root: str, strict: bool = False, max_errors: int = 50) -> dict:
    """
    校验一个 `all_memory/` 目录。

    strict=True：目录里多出 manifest 没记录的文件也算错误（zip 快照用）。
    strict=False：多出的只是警告（在线目录的 manifest 可能落后）。
    """
    root = os.path.abspath(root)
    result = {
        "ok": False, "root": root, "schema": "", "counts": {},
        "checked_files": 0, "errors": [], "warnings": [],
        "error_count": 0, "warning_count": 0,
    }
    errors: list[str] = []
    warnings: list[str] = []

    if not os.path.isdir(root):
        errors.append(f"不是目录: {root}")
        return _finish(result, errors, warnings, max_errors)

    manifest_path = os.path.join(root, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        errors.append(
            f"缺少 {MANIFEST_NAME}；先导出一次，或对目录运行 /allmem_manifest")
        return _finish(result, errors, warnings, max_errors)

    try:
        manifest = _read_json(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{MANIFEST_NAME} 读不出来: {exc}")
        return _finish(result, errors, warnings, max_errors)
    if not isinstance(manifest, dict):
        errors.append(f"{MANIFEST_NAME} 不是对象")
        return _finish(result, errors, warnings, max_errors)

    schema = str(manifest.get("schema") or "")
    result["schema"] = schema
    if not schema.startswith("all_memory.manifest."):
        errors.append(f"不认识的 schema: {schema or '(空)'}")
    elif schema != SCHEMA:
        warnings.append(f"schema 版本不同: {schema}（当前 {SCHEMA}）")

    declared: dict[str, dict] = {}
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            errors.append("files[] 里有非对象条目")
            continue
        rel = _safe_rel(entry.get("path"))
        if not rel:
            errors.append(f"非法路径: {entry.get('path')!r}")
            continue
        declared[rel] = entry

    for rel, entry in sorted(declared.items()):
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            if strict or not _is_mutable(rel):
                errors.append(f"缺少文件: {rel}")
            else:
                warnings.append(f"缺少文件（派生/可变层）: {rel}")
            continue
        try:
            size = os.path.getsize(full)
        except OSError as exc:
            errors.append(f"读不到文件: {rel} ({exc})")
            continue
        if int(entry.get("size", -1)) != int(size):
            message = (
                f"大小不符: {rel}（manifest {entry.get('size')} / 实际 {size}）")
            if strict:
                errors.append(message)
            else:
                warnings.append(message + "（manifest 落后，重新导出/生成即可）")
        expected_hash = str(entry.get("sha256") or "")
        if expected_hash:
            actual_hash = _sha256(full)
            if actual_hash != expected_hash:
                message = f"sha256 不符: {rel}"
                if strict:
                    errors.append(message)
                else:
                    warnings.append(message + "（manifest 落后，重新导出/生成即可）")
        result["checked_files"] += 1

    # 目录里多出来的权威文件：快照应报错，在线目录只警告。
    actual: set[str] = set()
    for rel, _full in _iter_files(root):
        if classify(rel) in ("authoritative", "history", "metadata"):
            actual.add(rel)
    for rel in sorted(actual - set(declared)):
        message = f"目录里有、manifest 没记录: {rel}"
        if strict:
            errors.append(message)
        else:
            warnings.append(message)

    data = scan(root, with_hashes=False)
    result["counts"] = data["counts"]
    expected_counts = manifest.get("counts") or {}
    for key in ("events", "days", "facts", "summaries", "personas",
                "media_records", "blobs", "failed", "entities", "synonyms",
                "files"):
        if key not in expected_counts:
            continue
        try:
            expected = int(expected_counts[key])
        except (TypeError, ValueError):
            errors.append(f"counts.{key} 不是整数")
            continue
        actual = int(data["counts"].get(key, 0))
        if expected != actual:
            message = f"counts.{key} 不符: manifest {expected} / 实际 {actual}"
            if not strict and (actual > expected or key in MUTABLE_COUNTS):
                warnings.append(message + "（manifest 落后，重新导出/生成即可）")
            else:
                errors.append(message)
    expected_ids = set(manifest.get("ids") or [])
    actual_ids = set(data["ids"])
    missing_ids = sorted(expected_ids - actual_ids)
    new_ids = sorted(actual_ids - expected_ids)
    if missing_ids:
        errors.append("ids 缺少: " + ", ".join(missing_ids))
    if new_ids:
        message = "ids 多出: " + ", ".join(new_ids)
        if strict:
            errors.append(message)
        else:
            warnings.append(message + "（manifest 落后，重新导出/生成即可）")

    known: set[str] = set()
    known_pairs: set[tuple[str, str]] = set()
    persona_refs: set[str] = set()
    for rel, full in _iter_files(root):
        if not (rel.startswith("vault/") and rel.endswith(".jsonl")):
            continue
        uid = rel.split("/")[1] if len(rel.split("/")) >= 3 else "default"
        try:
            handle = open(full, "r", encoding="utf-8")
        except OSError as exc:
            errors.append(f"读不到 {rel}: {exc}")
            continue
        with handle:
            for lineno, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    errors.append(f"{rel}:{lineno} JSON 解析失败")
                    continue
                if not isinstance(event, dict):
                    errors.append(f"{rel}:{lineno} 不是对象")
                    continue
                for field in REQUIRED_VAULT_FIELDS:
                    if field not in event:
                        errors.append(f"{rel}:{lineno} 缺字段 {field}")
                fp = str(event.get("fp") or "")
                if not fp:
                    errors.append(f"{rel}:{lineno} 缺指纹")
                    continue
                pair = (uid, fp)
                if pair in known_pairs:
                    errors.append(f"{rel}:{lineno} 重复指纹 {fp}")
                known_pairs.add(pair)
                known.add(fp)
                stamp = str(event.get("ts") or "")[:10]
                day = str(event.get("day") or "")
                if stamp and day and stamp != day:
                    warnings.append(f"{rel}:{lineno} day 与 ts 不一致")
                persona = str(event.get("persona") or "")
                if persona:
                    persona_refs.add(persona)

    for rel, full in _iter_files(root):
        if rel.startswith("facts/") and rel.endswith(".json"):
            try:
                fact = _read_json(full)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"fact 读不出来 {rel}: {exc}")
                continue
            if not isinstance(fact, dict):
                errors.append(f"fact 不是对象: {rel}")
                continue
            versions = fact.get("versions") or []
            if not isinstance(versions, list) or not versions:
                errors.append(f"fact 没有版本: {rel}")
                continue
            for version in versions:
                if not isinstance(version, dict):
                    errors.append(f"fact 版本不是对象: {rel}")
                    continue
                if not str(version.get("claim") or "").strip():
                    errors.append(f"fact 版本没有 claim: {rel}")
                sources = version.get("source_fp") or []
                if isinstance(sources, str):
                    sources = [sources]
                if not isinstance(sources, (list, tuple)):
                    sources = []
                if not sources:
                    errors.append(f"fact 没有出处: {rel}")
                for fp in sources:
                    if not _fp_exists(str(fp), known):
                        errors.append(f"fact 出处不存在: {rel} → {fp}")

        elif rel.startswith("summaries/") and rel.endswith("/summary.json"):
            try:
                summary = _read_json(full)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"summary 读不出来 {rel}: {exc}")
                continue
            if not isinstance(summary, dict):
                errors.append(f"summary 不是对象: {rel}")
                continue
            covered = str(summary.get("covered_fp") or "")
            if covered and not _fp_exists(covered, known):
                errors.append(f"summary 覆盖指纹不存在: {rel} → {covered}")
            if not str(summary.get("text") or "").strip():
                warnings.append(f"summary 正文为空: {rel}")

        elif rel.startswith("media/") and rel.endswith(".jsonl"):
            try:
                handle = open(full, "r", encoding="utf-8")
            except OSError as exc:
                errors.append(f"读不到 {rel}: {exc}")
                continue
            with handle:
                for lineno, line in enumerate(handle, 1):
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        errors.append(f"{rel}:{lineno} JSON 解析失败")
                        continue
                    if not isinstance(row, dict):
                        continue
                    fu = str(row.get("fu") or "")
                    if fu and not _fp_exists(fu, known):
                        warnings.append(f"{rel}:{lineno} 附件挂载的消息不在 vault: {fu}")
                    items = row.get("items") or []
                    if isinstance(items, dict):
                        items = [items]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        blob_rel = _safe_rel(item.get("path"))
                        if not blob_rel:
                            errors.append(f"{rel}:{lineno} 附件路径非法")
                            continue
                        blob = os.path.join(root, blob_rel.replace("/", os.sep))
                        if not os.path.isfile(blob):
                            errors.append(f"{rel}:{lineno} 附件本体缺失: {blob_rel}")
                            continue
                        blob_hash = str(item.get("hash") or "")
                        if blob_hash and not os.path.basename(blob_rel).startswith(blob_hash):
                            warnings.append(
                                f"{rel}:{lineno} 附件 hash 与文件名不一致: {blob_rel}")
                        size = item.get("size")
                        if isinstance(size, int) and size >= 0:
                            try:
                                if os.path.getsize(blob) != size:
                                    warnings.append(
                                        f"{rel}:{lineno} 附件大小不一致: {blob_rel}")
                            except OSError:
                                pass

    for rel, full in _iter_files(root):
        if rel.startswith("personas/") and rel.endswith(".txt"):
            try:
                if os.path.getsize(full) <= 0:
                    warnings.append(f"人格快照为空: {rel}")
            except OSError:
                pass
        elif rel.startswith(("entities/", "synonyms/")) and rel.endswith(".json"):
            if classify(rel) != "authoritative":
                continue
            try:
                _read_json(full)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"{rel} 读不出来: {exc}")
        elif rel.startswith("failed/") and rel.endswith(".json"):
            try:
                _read_json(full)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"{rel} 读不出来: {exc}")

    if int(data["counts"].get("failed", 0)) > 0:
        warnings.append(
            f"有 {data['counts']['failed']} 个待补写轮次（已包含在包里，恢复后下一轮会补写）")
    if data["unknown"]:
        warnings.append(
            "未分类文件: " + ", ".join(data["unknown"][:5])
            + (f" …共 {len(data['unknown'])} 个" if len(data["unknown"]) > 5 else ""))
    missing_personas = sorted(x for x in persona_refs
                              if not os.path.isfile(os.path.join(
                                  root, "personas", x + ".txt")))
    if missing_personas:
        warnings.append(
            "事件引用的人格快照不存在: " + ", ".join(missing_personas[:5]))

    return _finish(result, errors, warnings, max_errors)


def _find_prefix(names: list[str]) -> str:
    """manifest 只在 `all_memory/` 根；找不到返回空串。"""
    return CONTAINER if CONTAINER + MANIFEST_NAME in names else ""


def _extract_memory(zip_path: str, dest: str, prefix: str) -> str:
    """只解 `all_memory/` 到 dest；返回实际根目录。"""
    root = os.path.join(dest, "all_memory") if prefix else dest
    os.makedirs(root, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            rel = _safe_rel(info.filename)
            if not rel or not rel.startswith(prefix):
                continue
            tail = rel[len(prefix):]
            if not tail:
                continue
            target = os.path.join(root, tail.replace("/", os.sep))
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return root


def verify_zip(zip_path: str, max_errors: int = 50) -> dict:
    """校验导出 zip 里的 `all_memory/`；只看 M，不碰 astrbot_data。"""
    if not os.path.isfile(zip_path):
        return _finish({
            "ok": False, "zip": zip_path, "schema": "", "counts": {},
            "checked_files": 0, "errors": [], "warnings": [],
            "error_count": 0, "warning_count": 0,
        }, [f"文件不存在: {zip_path}"], [], max_errors)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        return _finish({
            "ok": False, "zip": zip_path, "schema": "", "counts": {},
            "checked_files": 0, "errors": [], "warnings": [],
            "error_count": 0, "warning_count": 0,
        }, [f"不是有效的 zip: {exc}"], [], max_errors)

    prefix = _find_prefix(names)
    if not prefix:
        return _finish({
            "ok": False, "zip": zip_path, "schema": "", "counts": {},
            "checked_files": 0, "errors": [], "warnings": [],
            "error_count": 0, "warning_count": 0,
        }, [f"包里没有 {CONTAINER}{MANIFEST_NAME}，不是本插件导出的包"], [],
            max_errors)

    temp = _temp_dir(os.path.dirname(os.path.abspath(zip_path)))
    try:
        root = _extract_memory(zip_path, temp, prefix)
        result = verify(root, strict=True, max_errors=max_errors)
        result["zip"] = zip_path
        return result
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def attach_to_zip(zip_path: str, plugin_version: str = "", models=None,
                  prompt_versions=None) -> dict:
    """
    给已经生成的导出 zip 补 `all_memory/manifest.json` + `README_M.md`。

    只解压 `all_memory/` 来算 hash；`astrbot_data/` 不碰。
    """
    if not os.path.isfile(zip_path):
        raise ValueError(f"文件不存在: {zip_path}")

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        if CONTAINER + MANIFEST_NAME in names:
            return _read_json_from_zip(zip_path, CONTAINER + MANIFEST_NAME)
        if not any(name.startswith(CONTAINER) for name in names):
            raise ValueError(f"包里没有 {CONTAINER}")

    temp = _temp_dir(os.path.dirname(os.path.abspath(zip_path)))
    try:
        root = _extract_memory(zip_path, temp, CONTAINER)
        manifest = build(root, plugin_version=plugin_version, models=models,
                         prompt_versions=prompt_versions)
        payload = json.dumps(manifest, ensure_ascii=False, indent=2,
                             sort_keys=True).encode("utf-8")
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(CONTAINER + MANIFEST_NAME, payload)
            archive.writestr(CONTAINER + README_NAME,
                             readme_text(manifest).encode("utf-8"))
        return manifest
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _read_json_from_zip(zip_path: str, member: str):
    with zipfile.ZipFile(zip_path) as archive:
        return json.loads(archive.read(member).decode("utf-8"))


def readme_text(manifest: dict) -> str:
    """给人和未来读取器看的一页说明。"""
    counts = manifest.get("counts") or {}
    models = manifest.get("models") or {}
    prompts = manifest.get("prompt_versions") or {}
    ids = manifest.get("ids") or []
    return f"""# 全量记忆体 · M 说明

- schema：`{manifest.get("schema", "")}`
- 生成时间：{manifest.get("created_at", "")}
- 插件版本：{manifest.get("plugin_version", "")}
- id：{", ".join(str(x) for x in ids) or "（空）"}
- 事件：{counts.get("events", 0)} 条 / {counts.get("days", 0)} 天
- 事实：{counts.get("facts", 0)} 条
- 纪要：{counts.get("summaries", 0)} 份
- 人格快照：{counts.get("personas", 0)} 个
- 附件：{counts.get("media_records", 0)} 条索引 / {counts.get("blobs", 0)} 个本体

## 哪些是记忆本体

| 目录 | 是什么 | 能不能重建 |
|---|---|---|
| `vault/` | 原文，按 `<id>/YYYY-MM-DD.jsonl` 追加 | **不能**，唯一不可再生 |
| `facts/` | 提炼出的事实，每条带 `source_fp` | 能，但会变 |
| `summaries/` | 滚动前情纪要，带 `covered_fp` | 能，但会变 |
| `personas/` | 人格快照，内容寻址 | 能（如果宿主还留着） |
| `media/` | 附件索引（哪条消息、哪些附件） | 能，但依赖 blobs |
| `blobs/` | 附件本体，内容哈希去重 | 不能从文本重建 |
| `entities/` | 手工实体/别名词典 | 不能自动重建 |
| `synonyms/` | 手工同义词分组 | 不能自动重建 |
| `failed/` | 归档失败待补写的轮次 | 补写后进 vault |

`index.db`、`vectors.db`、`cache/` 是派生物，删掉可以用 `vault/` 重建；
`state/`、`settings.json` 是运行态，读取器应忽略。
如果这个包外面还有 `astrbot_data/`，那是宿主数据，不属于 M。

## vault 每行的字段

```json
{{"fp":"...","uid":"...","ts":"2026-10-24T21:00:00+08:00","day":"2026-10-24",
 "role":"user","origin":"astrbot_context","session":"...","text":"...",
 "raw":null,"tokens":12,"persona":null,"sender":null,"backfill":false}}
```

- `fp`：`sha256(role + "\\0" + text)` 前 16 位；重复说同一句会编成 `abc-2`。
- `text`：原文，逐字节保存，不做 strip。
- `role`：`user` / `assistant` / `tool`。
- `day`：本地日期，按天分文件。

## facts 的结构

```json
{{"id":"fact-...","versions":[{{"version":1,"claim":"...","kind":"preference",
 "source_fp":["abc..."],"extracted_at":"...","model":"...","prompt_version":"..."}}]}}
```

`source_fp` 必须能在 `vault/` 里找到；找不到就是坏数据。

## 校验与读取

```bash
python tools/m_read.py stats  <zip或all_memory目录>
python tools/m_read.py search <zip或目录> "关键词" --uid <id> --limit 5
python tools/m_read.py day    <zip或目录> 2026-10-24 --uid <id>
python tools/m_read.py fact   <zip或目录> fact-...
```

`m_read.py` 只用标准库，不需要 AstrBot，也不需要 `index.db` / `vectors.db`。

## 重建检索层

删掉 `index.db`、`vectors.db`、`cache/`，回到插件里运行 `/allmem_rebuild`；
或让读取器自己按 `vault/` 建临时 FTS。

## 模型与提示词

- models：{json.dumps(models, ensure_ascii=False)}
- prompt_versions：{json.dumps(prompts, ensure_ascii=False)}

这些字段用于回答“这条事实/纪要是什么时候、用什么模型和提示词算出来的”。
"""
