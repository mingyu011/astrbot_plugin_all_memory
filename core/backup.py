"""
backup.py —— 打包逻辑

WebUI 的「一键保存」和验证脚本共用这一份代码，避免两边规则走偏。

规则：
  - 整个目录递归，一个文件都不落
  - 跳过 .tmp / .part 临时文件
  - 跳过指定路径（导出目录自身、插件自己的数据目录）
  - 归档内同名只写一次
"""

from __future__ import annotations

import os
import zipfile
from datetime import datetime, timedelta

SKIP_SUFFIXES = (".tmp", ".part")


def seconds_until(hhmm: str, now: datetime | None = None) -> float | None:
    """距离下一个 HH:MM 还有多少秒（当天的已过就顺延到明天）。格式不对返回 None。"""
    text = (hhmm or "").strip()
    if not text:
        return None
    try:
        hour_s, minute_s = text.split(":")[:2]
        hour, minute = int(hour_s), int(minute_s)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None

    current = now or datetime.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


def iter_entries(root: str, prefix: str, skip_abs=()):
    """
    遍历 root，产出 (真实路径, 压缩包内路径, 是否目录)。

    - 文件：正常产出
    - 空目录：产出一个以 / 结尾的条目，避免解压后少文件夹
      （非空目录不用单独产出，文件路径自带）
    """
    root_abs = os.path.abspath(root)
    skip = {os.path.abspath(p) for p in skip_abs if p}

    for dirpath, dirnames, filenames in os.walk(root_abs):
        here = os.path.abspath(dirpath)
        # 注意：跳过列表只对「源目录以下的子树」生效。
        # 源目录自己永远遍历——否则以插件目录为源时会把整棵树跳过。
        if here != root_abs and any(
                here == s or here.startswith(s + os.sep) for s in skip):
            dirnames[:] = []
            continue

        rel_dir = os.path.relpath(here, root_abs)
        if rel_dir == ".":
            rel_dir = ""

        # 空目录也留个条目
        if not filenames and not dirnames:
            arc_dir = os.path.normpath(os.path.join(prefix, rel_dir)) if rel_dir \
                else os.path.normpath(prefix)
            arc_dir = arc_dir.replace(os.sep, "/").rstrip("/") + "/"
            yield here, arc_dir, True
            continue

        for name in filenames:
            if name.endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(here, name)
            arc = os.path.normpath(os.path.join(prefix, rel_dir, name)) \
                if rel_dir else os.path.normpath(os.path.join(prefix, name))
            yield full, arc.replace(os.sep, "/"), False


def build_zip(target: str, sources, compresslevel: int = 1) -> dict:
    """
    sources: [(源目录, 压缩包内前缀)] 或 [(源目录, 压缩包内前缀, 该源要跳过的子树)]

    跳过列表是**按来源**的：插件目录那趟要跳过自己的 exports/，
    但 astrbot_data 那趟要跳过整个插件目录。混用会把插件数据整棵漏掉。

    返回 {files, skipped, bytes, size}
    """
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)

    added = 0
    skipped = 0
    total = 0
    seen: set[str] = set()

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=compresslevel) as zf:
        for item in sources:
            root, prefix = item[0], item[1]
            skips = item[2] if len(item) > 2 else ()
            if not os.path.isdir(root):
                skipped += 1
                continue
            for full, arc, is_dir in iter_entries(root, prefix, skips):
                if arc in seen:
                    skipped += 1
                    continue
                try:
                    if is_dir:
                        zf.writestr(arc, b"")
                    else:
                        zf.write(full, arc)
                except OSError:
                    skipped += 1
                    continue
                seen.add(arc)
                added += 1
                if is_dir:
                    continue
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass

    return {
        "files": added,
        "skipped": skipped,
        "bytes": total,
        "size": os.path.getsize(target),
    }
