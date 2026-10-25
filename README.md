# 全量记忆体（astrbot_plugin_all_memory）

**当前版本：1.1.0**

给陪伴型 AI 用的记忆插件：**说过的每句话原封不动存下来，聊得再久也不断片。**

它不改模型、不改人格，只在上下文快满的时候接手：把旧原文裁掉之前先提炼成人物卡和前情纪要，
被裁掉的原文仍然留在自己的库里，需要时按关键词或时间查回来。

---

## ⚠️ 装之前：三件必做的事

**这三件不做，插件要么被宿主抢先压缩，要么根本不会接管——装了等于没装。**

| # | 在哪 | 改成 | 为什么 |
|---|---|---|---|
| **1** | AstrBot → 配置 → AI → 高级 → **上下文管理策略** → 「压缩前最多保留对话轮数」 | **`-1`** | 默认 50 轮，跟 token 无关；不改它，聊到 50 轮就被硬切，轮不到我们 |
| **2** | 同一页 → 「上下文窗口兜底值」 | **一个天文数字**，如 `10000000000000000000` | AstrBot 在窗口 82% 处压缩，比例写死改不了，只能让它的窗口大到碰不到 |
| **3** | 插件设置 → 「上下文窗口」(`window_tokens`) | **你的真实窗口**，如 `1000000` | 填 0 就是「只存档、不接管」，面板的「接管」按钮也不会生效。**这是接管生效的前提** |

改完重启 AstrBot。启动日志里出现这一行才算真的加载了（当前 23 条，数字要和版本对得上）：

```
[all_memory] WebUI 已注册 23/23 个接口
```

> AstrBot 自带的压缩不会消失，它留在后面当保险：插件挂了、或单条消息太大，它还能兜一次。

---

## 安装

1. 整个 `astrbot_plugin_all_memory/` 放进 `AstrBot/data/plugins/`
2. 删掉目录里的 `__pycache__`（覆盖升级后尤其要删，否则可能跑旧字节码）
3. **完整重启 AstrBot**；只点插件重载不会重新 import 模块，旧进程会继续跑旧代码
4. 私聊说一句话，看这里有没有长出文件（`<id>` 是按对方 ID 建的文件夹）：

```
AstrBot/data/all_memory/vault/<id>/2026-10-23.jsonl
```

有就成功了。**装上默认只存档**，填了 `window_tokens` 才开始接管。

---

## 数据目录

默认在 `<AstrBot 数据目录>/all_memory/`，可以在设置里改 `data_dir`。一个私聊 id 一套：

```
vault/<id>/YYYY-MM-DD.jsonl     原文，只追加
facts/<id>/fact-*.json          人物卡
facts_archive/<id>/<时间>/      每次全量重炼前的旧人物卡
facts_staging/<id>/             提炼临时目录（平时没有）
summaries/<id>/summary.json     这个 id 的滚动纪要
entities/<id>.json              实体/别名词典（名字、昵称、类型）
synonyms/<id>.json              同义词分组（累/疲惫、怕/害怕）
media/<id>/YYYY-MM-DD.jsonl     附件索引
state/<id>/state.json           校准、裁剪次数、纪要/提炼游标
cache/extract/<id>/             提炼缓存
cache/summary/<id>/             纪要缓存
cache/query/<id>/               扩词缓存
failed/<id>/                    归档失败待补写
manifest.json                   M 快照说明（导出或 /allmem_manifest 生成）
README_M.md                     给人看的 M 说明（同上）

personas/<指纹>.txt             人格快照（全局，内容寻址）
blobs/<ab>/<hash>.<ext>         附件本体（全局，内容哈希去重）
exports/                        导出包
index.db                        检索索引（全局，按 id 隔离；可重建）
vectors.db                      向量（全局，按 id 隔离；可重建）
settings.json                   面板旧设置（兜底）
state/_sessions.json            session → id 文件夹 的映射
state/__global__/state.json     全局接管开关
```

`vault` 是唯一不可再生的原文；`personas`、`blobs` 尽量留；`facts`、`summaries`、
`index.db`、`vectors.db`、`cache` 都是派生物，删了能重算。

---

## 设置项说明

| 设置 | 作用 | 默认 | 说明 |
|---|---|---|---|
| **总开关** `enabled` | 关掉之后既不归档也不接管 | 开 | 关了就完全不影响对话 |
| **上下文窗口** `window_tokens` | 模型真实窗口，单位 token | `0` | **0 = 只存档不接管**。填 `128000` / `1000000`。填小了会疯狂裁剪，填大了该裁不裁 |
| **数据目录** `data_dir` | 总库放哪 | 空 | 空 = `AstrBot数据目录/all_memory`。想放别的盘就填绝对路径，**别填错，等于换一个库** |
| **触发裁剪的比例** `trigger_ratio` | 上下文占窗口多少时动手 | `0.80` | 必须小于宿主内置的 0.82，否则宿主先压 |
| **裁剪后压到多少** `total_ratio` | 裁完整个请求的目标大小 | `0.40` | 里面还要装纪要/人物卡/地图/检索，历史拿剩下的 |
| **地图按天列出** `map_days` | 当前 id 最近多少天按天列 | `365` | 更早的按月汇总；地图本身覆盖该 id 全部历史，不会砍掉早期记忆 |
| **每轮检索条数上限** `recall_limit` | 安全上限 | `50` | 只查当前 id；实际装几条由 5% 预算决定，装满为止 |
| **提炼每批字数** `extract_chunk_chars` | 批数 = 该 id 正文字数 ÷ 它 | `6000` | 改了会让提炼缓存失效，并触发该 id 人物卡**全量重做**（旧版归档） |
| **演练模式** `dry_run` | 只把"本来会怎么裁"写进日志 | 关 | 不裁、不注入，用来先看效果 |
| **每天定时导出** `auto_export_at` | 填 `03:00` 就每天打包一次 | 空 | 留空不自动导出 |
| **保留几个导出包** `export_keep` | 只留最近 N 个 zip | `0` | 0 = 永不删。包很大（含整个 data 目录），建议设 1~2 |

> 另有几个内部开关不在设置页里（`recall_expand` 同义扩词、`extract_concurrency` 并发数、
> `extract_skip_tool` 跳过工具输出等），默认值写在 `main.DEFAULTS` 里，改代码即可。

---

## 它到底在干什么

三层的分工，一句话：**原文是证据，人物卡是认知，索引是检索。**

```
第一层  原始层  vault/<id>/YYYY-MM-DD.jsonl     只追加，永不删改   → 存得全
第二层  提炼层  facts/<id>/fact-*.json          带出处，可重算     → 记得住
第三层  检索层  index.db (FTS5) + vectors.db    可随时重建         → 查得到
```

一个私聊会话绑定一个 id；`vault`、`facts`、`summaries`、`media`、`state`、`cache`、`failed`
都按 id 分文件夹，互不串味。`personas/`、`blobs/` 和两个索引库是全局的（人格/附件本体/索引）。

对应的运行时：

```
收到消息
   │
   ├─ 归档：把新内容追加进 vault/<id>/，附件落 blobs/
   ├─ 算一下：现在上下文占窗口多少？
   │
   ├─ 不到 80% → 什么都不做，原样发出去（和没装插件一个字节都不差）
   │
   └─ 到 80%  → 裁到 40%，并且从此进入「接管模式」：
                每轮都带 前情纪要 + 人物卡 + 相关旧话 + 记忆地图
```

---

## 功能详解

### 1. 原文归档：所有说过的话都留着

- 每条消息单独一行 JSON，按 `id/天` 分文件，**只追加、从不修改**。
- 指纹 = `sha256(角色 + 正文)` 前 16 位；索引按 `id + 指纹` 存，所以同一句话在不同 id
  里是两条记录，不会互相覆盖。同一句话重复说，每次都留一条（`abc`、`abc-2`、`abc-3`）。
- 文件夹名优先用平台读到的对方 ID（QQ 号等），读不到用账户名称，再没有用安全会话名；
  映射存在 `state/_sessions.json`，同一个人不会越写越分散。
- 时间取宿主塞在消息里的 `Current datetime`，**精确到分钟**；没有时间提示的标「回填」。
- 宿主的会话记录会被它自己覆盖掉（`conversations.content` 是整段替换），
  所以这份 `vault` 是**唯一不可再生的资产**。

### 2. 裁剪与接管

- 到窗口 **80%** 触发一次，把请求压到 **40%**，只保留最近的原文。
- 裁过一次之后进入**接管模式**：每轮都注入记忆块；全局开关打开时，所有会话（含新会话）都这样。
- 也可以不等 80%，手动 `/allmem_takeover` 立刻全局接管（恢复备份、换新窗口时用）。
- 预算固定：人物卡 10%、记忆地图 3%、检索片段 5%、前情纪要 5%，历史拿剩下的。

### 3. 人物卡（提炼）

- **按 id 扫全量库**：只扫这个 id 的 `vault`，按 6000 字切批，每批问模型"有哪些长期成立的事实"。
- 每条必须带出处（`source_fp` 指回原话），**没出处的事实在解析阶段就丢掉**。
- 类型分五档，注入时按权重排：身份 > 关系 > 偏好 > 事实 > 事件。
- 核心事实（identity / relationship / preference）优先保留；超过 10% 预算时，
  先留核心，再按相关度补事件事实（`inject.select_facts`）。
- **近义去重**：两条 claim 只共用“主人/助手/喜欢”这类泛词时不会合并；
  但保留了同一串具体名词或动作的改写会合并，来源取并集。读取层 `latest()` 也会先去重，
  所以旧数据没整理时，注入到模型的也不会是一串重复卡。
- **增量游标**：自动裁剪时用 `facts_covered_index` + 精确 `covered_fp`，只处理游标之后的新历史；
  有批次失败就不推进，下次重试。旧版只按基础指纹回退，重复句会把尾部重新提炼一遍。
- **provenance**：新版本记录实际使用的 `model` 和 `prompt_version`，面板里能看到是哪版模型/提示词抽的。
- 手动 `/allmem_reextract` 或改了 `extract_chunk_chars` 时**全量重做**：旧版先归档到
  `facts_archive/<id>/<时间戳>/`，再整体替换，注入只用新版。
- **旧重复项**：`/allmem_repair` 会保留最早一条，其余标 `deleted` 并写 `merged_into`，不删文件、不碰 vault。
- 按批缓存：历史只追加，前面的批永远命中缓存；并发 6 路跑批；工具输出默认不参与提炼。

### 4. 前情纪要

- 补的是**叙事**：旧原文不在眼前了，"最近发生了什么"会断，纪要把它接上。
- **正常只追加**：从上次覆盖到的位置继续，一直写到该 id 当前最后一条；旧纪要原样保留。
- **加载时自动清洗**：过滤 `（空）/（无内容）/（无值得记录的内容）` 等占位变体，去掉精确重复，
  按 `MM-DD + 时间` 重排，并把同一时刻的近义句合并；`晚/早` 简写和 `16:07-09` 这种时间范围也认。
- **覆盖游标**：`covered_index` + 精确 `covered_fp`，不再只靠基础指纹回退；重复句不会让尾部被重新总结。
- 想彻底重写：移走 `summaries/<id>/summary.json` 再点「生成纪要」；只会重读 vault，不会改原文。
- 带日期锚（`10-21 晚上：聊到工作压力，她情绪低落`），模型想深挖时能顺着时间回查原文。
- 块尾固定带一句：「这是压缩纪要，可能有偏差；引用前先用检索核对原文」。
- 一个 id 一个滚动文件 `summaries/<id>/summary.json`，预算 5%；装不下时注入最近的部分。

### 5. 检索

实现统一在 `core/retrieve_pipeline.py`（`main.retrieve()` 只负责把它放进线程）：

```
字面：trigram + 词级 bigram FTS，多词合成一条 OR/BM25
向量：本地哈希 embedding（字符 bigram），按余弦相似度取候选
融合：RRF 倒数排名 + 时间命中加分 + 角色加权 + MMR 去重
```

- 多词不再每个词各查一次；FTS5 的 BM25 直接对整条 OR 查询排序。
- 两字中文词有独立的 bigram 词级索引，不再只靠 `LIKE`。
- 先保留前 5 条字面锚点，再做上下文扩展；扩展出来的邻居不会把锚点挤掉。
- 显式日期问题会走按天模式：当天有结果就优先当天，没有才退回全部。
- 问「第一次见面」「最早」这类，自动把该 id 最早那天的原话排到最前。
- 否定/负向问题会先过滤掉只含反向证据的候选，避免“没去过”被“去过”顶掉。
- 候选原文用批量 `get_many` 一次取回，MMR 的相似度按原文只算一次并缓存。
- 实体/别名词典存在 `entities/<id>.json`，同义词分组存在 `synonyms/<id>.json`；
  分别用 `/allmem_entities`、`/allmem_synonyms` 维护。
- 模型也可以用工具主动查：`search_memory` 搜关键词、`read_memory` 读全文或整天的对话、`memory_timeline` 看时间线。
- 检索范围只限当前 id；地图也只统计当前 id。
- 同义词扩展默认关闭（每轮多一次模型调用 = 多一秒延迟），需要时把 `recall_expand` 打开；
  本地实体/同义词词典不需要模型，始终生效。

### 6. 身份：按 id 隔离

- 每条消息尽力记下发送者；一个私聊会话的所有原文只进 `vault/<id>/`。
- 人物卡、纪要、地图、检索都先看当前 id，**不会读别的 id 的内容**。
- 文件夹名优先平台读到的用户 ID（QQ 号等），其次账户名称，再没有用安全会话名。
- 历史消息宿主不给发送者：单聊按会话尾号推算；群聊不接，钩子直接跳过。
- 暂时不接群聊：带 `group` 的会话在钩子里直接跳过，不归档、不注入。

### 7. 附件（图片 / 语音）

- 按内容哈希存 `blobs/<ab>/<sha256>.<ext>`，同一张图只占一份。
- 归档分两条路：你发进来的（轮末抓）、机器人发出去的（发送前那个钩子抓，
  包括表情包插件在回复之后自己追加的图）。
- 历史图片能从宿主库里补回来：图在 `data_v4.db` 里是 base64 内嵌的，附件库为空时启动后自动补一次。
- 导出时附件**内嵌成 data URI**，换台机器打开也看得见图。

### 8. 导出 / 导入 / 恢复

| 动作 | 得到什么 | 会不会覆盖 |
|---|---|---|
| 面板「一键保存全量数据」 | 一个 zip：`all_memory/` + `astrbot_data/`；`all_memory/` 根带 `manifest.json` + `README_M.md` | — |
| 面板「下载会话」 | 一个 jsonl（选多个则一个 zip，每个会话一个文件），附件内嵌 | — |
| `/allmem_verify_m <zip\|目录>` | 校验 manifest、checksums、vault 行、fact 出处、附件本体 | 只读 |
| `/allmem_restore <zip>` | 从包里的 `all_memory/` **合并**回来 | 只增不删，已存在的跳过 |
| `/allmem_import <db或目录>` | 把宿主库 / 另一个记忆插件的数据收进 vault | 按内容去重 |

包里的 `astrbot_data/` **不会还原**——那是宿主自己的目录，运行中覆盖会出事。

`manifest.json` 说明 `all_memory/` 里哪些是权威记忆（vault / facts / summaries / personas / media / blobs / entities / synonyms / failed）、哪些是可重建的（index.db / vectors.db / cache）、哪些是运行态；`README_M.md` 是给人和未来读取器看的一页说明。`tools/m_read.py` 可以只拿 zip 里的 `all_memory/` 直接读，不需要装 AstrBot。

### 9. 面板

AstrBot 插件页 → 可视化视图：

- **左侧**：会话 / 事实两个模式；会话列表带勾选框和全选，列表右上角 ↓ 直接下载
- **右侧**：会话历史（图片内联）、事实详情（版本历史 + 每条出处原话）
- **顶部**：原始层条数、会话数、事实数、前情纪要、附件、索引、覆盖天数
- **右上**：生成纪要、提炼事实、导入备份、校验记忆体、一键保存全量数据
- **「生成纪要」**：先选中左侧会话；正常从上次覆盖点往后追加，纪要文件被删掉时会从第一条重新写。
- **「提炼事实」**：先选中左侧会话；它是**全量替换**，旧人物卡先进 `facts_archive/`，再重建。
- **「校验记忆体」**：只读体检，不是清理。对照上次导出的 `manifest.json` 核对文件、大小、SHA256、
  vault 行、事实出处、纪要覆盖指纹和附件本体；没有 manifest 时会提示先导出一次。
  它不会删除重复、不会重排纪要、不会修复任何内容。
- **「一键保存全量数据」**：导出 zip（`all_memory/` + `astrbot_data/`），并在 `all_memory/` 根生成
  `manifest.json` + `README_M.md`；校验按钮才有东西可对。
- **顶部工具栏「全局接管」**：一次对所有会话生效，包括以后新开的
  - 默认「关（自动）」：第一次裁剪时自动开启
  - 手动开启：不用等裁剪，立刻所有会话每轮注入记忆
  - 手动关闭：所有会话都不再裁剪、不再注入，交回 AstrBot 原生上下文（已被裁掉的历史不会自动回来）
  - 窗口 `window_tokens` 为 0 时显示「未生效」
- **会话标题旁**：点「纪要」展开这个会话的纪要全文
- **事实模式**：跨 id 汇总列出，每条带 `id` 徽章；点一条看版本和出处。
  「提炼事实」按钮针对当前选中的会话/id
- **底栏**：触发比例、裁后比例、检索条数、地图天数、演练开关

### 10. 指令

| 指令 | 作用 |
|---|---|
| `/allmem` | 状态总览、处理失败队列 |
| `/allmem_verify` | 核对来源数据库，报告缺口 |
| `/allmem_repair` | 清理纪要里的占位/重复/乱序，合并近义事实（只动派生层） |
| `/allmem_verify_m [zip\|目录]` | 校验记忆体：manifest、checksums、vault 行、fact 出处、附件本体 |
| `/allmem_manifest` | 给当前数据目录生成/刷新 `manifest.json` + `README_M.md` |
| `/allmem_search <词>` | 手动检索（走混合检索） |
| `/allmem_recall` | 看上一轮检索贡献：候选/已在上下文/最终注入 |
| `/allmem_facts [词]` | 列出当前 id 的人物卡 |
| `/allmem_entities` | 实体/别名词典：列表 / add / remove / suggest |
| `/allmem_synonyms` | 同义词分组：列表 / add / remove |
| `/allmem_show <id>` | 看当前 id 某条事实的全部版本和出处 |
| `/allmem_reextract` | 对当前 id 手动全量重炼 |
| `/allmem_summary` | 对当前 id 追加还没纪要过的历史 |
| `/allmem_takeover [off]` | 全局开关：关闭后所有会话都不裁不注入，交回官方上下文 |
| `/allmem_rebuild` | 从 vault 重建索引 |
| `/allmem_backfill` | 从宿主库回填历史图片 |
| `/allmem_import <路径>` | 超集导入 |
| `/allmem_restore <zip>` | 从导出包合并恢复 |
| `/allmem_persona [指纹]` | 查看/写回人格快照 |

### 11. 检索评测

测试集在：

```text
tests/retrieval_cases.json   结构化用例
tests/retrieval_cases.md     可读表格 + 记录栏
```

离线跑检索层：

```powershell
python -B astrbot_plugin_all_memory/tests/eval_retrieval.py `
  --index <all_memory>/index.db `
  --uid 1439600604 `
  --limit 5
```

多策略对比（推荐）：

```powershell
python -B astrbot_plugin_all_memory/tests/eval_retrieval.py `
  --index <all_memory>/index.db `
  --uid 1439600604 `
  --compare fts,fts+boost,local,local-novector,local-nommr,local-noboost `
  --copy --workdir _eval-snapshot
```

`--compare` 会并排给 `fts`（只字面）/ `fts+boost`（字面+最早/按天）/ `local`（字面+本地向量+RRF+MMR）以及三个消融项的 recall@1/5/10、MRR、空结果、p50/p95、负向命中、跨 id 泄漏。`--copy` 先把 index/vectors 备份到快照再跑，避免写生产库。

2026-10-25 快照（重建前 628 条）的离线基线：

```text
fts               13/30    p50 3.3ms
fts+boost         16/30    p50 3.9ms
local             18/30    p50 104ms
local-novector    18/30    p50 28.7ms
local-nommr       18/30    p50 74.4ms
local-noboost     16/30    p50 143ms
```

结论：锚点保留 + 按天模式 + 负向过滤后，`local` 从 10/30 回到 18/30，date 3/3、negative 0/2、p50 约 0.10s。**本地哈希向量在这套题上没有额外增益**。  
无模型原文检索在这套 32 题/当前数据上已经到顶；剩下的 12 条集中在 style / recent / relationship / preference，继续调 FTS/RRF/MMR 容易过拟合。事实层、答案层、embedding/rerank 是另一个议题，不自动排期。

> 这套成绩已经接进 `main.retrieve()`：自动召回、`search_memory`、`/allmem_search` 都走同一条 pipeline；
> 1.1.0 起 `main.on_llm_request` 不再自己重复做 earliest/day/negative/context，统一由 pipeline 负责。

报告的 `recall@5` 按 category 分开；事实型问题（人物/偏好/关系）要看事实层，不能只看原文 top5。

---

## 常见问题

**Q：装了之后对话会变吗？**
填了 `window_tokens` 但没到 80% 之前，一个字节都不改；到 80% 才裁一次，之后进入接管模式。

**Q：裁剪会不会把原文弄丢？**
不会。宿主那份会变短（它保存的是"实际发出去的消息"），vault 里永远是全的。

**Q：面板里明明有几百条，模型却说不记得？**
模型只看到裁完的上下文 + 记忆块。要它想起来，得让它调检索工具——块尾的提示就是干这个的。

**Q：80% 那条线到底按什么算？**
按 AstrBot **实际注入给模型的 token**。1.1.0 修好后，优先用响应里的
`TokenUsage.input`（含工具定义），下一轮用它判断压力；没有时退回
`conversation.token_usage`。之前的 4.8% 是字符估算，不是这条线。

**Q：面板显示多少才算对？**
按 AstrBot 实际注入量。1.1.0 起：

```text
on_llm_response → state.last_prompt_tokens = usage.input
on_llm_request  → 优先用它
                 → 没有就读 conversation.token_usage
                 → 再没有才退回字符估算
```

所以真实 9.9% 时，面板「合计」应该接近 9.9%，而不是旧的 4.8%。
如果仍显示 4.8%，先确认 AstrBot 完整重启过、日志行号是新版、state 里有
`last_prompt_tokens`。

**Q：开了接管发现效果不好，关掉能回到原样吗？**
没真正裁过：可以，关掉只是不再附加记忆块，宿主历史没动。
已经真裁过：关掉只能停止继续裁，宿主已经被裁短的历史不会自动恢复。

**Q：不用 embedding 模型，检索能做到什么程度？**
字面、两字词、名字/别名、日期、原话、工具结果都很可靠；同义词靠实体/同义词词典，
能覆盖一部分。真正的模糊改写（“累” vs “疲惫”）仍然弱，这是无模型方案的上限。
不用模型也可以把 FTS/BM25、前后文、去重、事实分层、评测全部做完。

**Q：回答很慢，软件/插件卡？**
先看 `logs/event_loop_watchdog.log` 里的 `Event loop lag detected` 和主线程栈。
旧版踩过两个坑：

- 每个 SQLite 连接都执行 `PRAGMA journal_mode=WAL`，遇到后台写入时会等锁，
  实测卡 25~35 秒；现在只在初始化时设置一次。
- MMR 对 100 条候选、每条几万字的工具输出重复算 bigram；现在每条只算一次、
  截断到 1200 字，并把检索/排序放到线程里。

**Q：提炼很慢？**
一次调用要一两分钟的话，是模型本身慢。正在做的是让提炼走独立模型（`extract_model`）。

**Q：群聊能用吗？**
暂时不能。带 `group` 的会话会被钩子直接跳过，不归档、不注入；只做私聊。

**Q：两个人的记忆会串吗？**
不会。一个私聊会话绑定一个 id，`vault/facts/summaries/media/state` 都按 id 分文件夹；
检索、地图、纪要、人物卡都只读当前 id。暂时不接群聊。

**Q：改了 `extract_chunk_chars` 会怎样？**
该 id 的人物卡会全量重做：旧版先归档到 `facts_archive/<id>/<时间戳>/`，再整体替换，
注入只用新版；不会新旧混着用。

**Q：纪要会不会越写越长？**
会，但正常只追加，不会为了省 token 把旧行删掉；加载和整理时会去掉占位/精确重复/同一时刻的近义句，
并按时间重排。注入时只装最近 5%，装不下丢最旧的；早期纪要仍在
`summaries/<id>/summary.json` 里。想彻底重写，移走这个文件再点「生成纪要」。

**Q：图片显示不出来？**
先看 `blobs/` 里有没有文件。旧版本有过归档失效的 bug，历史图用 `/allmem_backfill` 补。
表情（mface 贴纸）是表情码不是文件，宿主都没存，抓不到。

**Q：改了代码但界面没反应？**
浏览器缓存。`index.html` 里 `app.js?v=<版本>` 的版本号要跟着改，否则永远跑旧脚本。

---

## 三条铁律

1. **原文只追加** —— 任何功能都不许删改 `vault/`。
2. **认知可重算** —— 人物卡、纪要、索引都是派生物，删掉能重建。
3. **需要时查得到** —— 检索比"记得多"更重要。

改任何东西之前，先确认不会破坏这三条。

---

## 版本更新

### 1.1.0

- **检索**：新增 `core/retrieve_pipeline.py`，把字面锚点、向量召回、RRF、MMR、日期模式、最早事件、负向证据过滤和上下文扩展收进同一条链路；`main.retrieve()` 不再自己拼这些逻辑，并放到线程里跑。
- **记忆体**：新增 `manifest.json` + `README_M.md`，导出时自动附带；`/allmem_verify_m` 和面板「校验记忆体」可以核对文件、校验和、vault 行、事实出处和附件本体；`tools/m_read.py` 可以脱离 AstrBot 直接读导出包。
- **纪要**：加载时自动清洗占位符、重复句和乱序；覆盖游标改成 `covered_index` + 精确指纹，不再因为重复指纹把尾部重总结。
- **事实**：提炼、增量、读取三处统一做近义去重；新增 `facts_covered_index`，新版本记录 `model` 和 `prompt_version`，并支持把旧重复项合并/标记删除。
- **面板**：新增「校验记忆体」；脚本缓存号升到 1.1.0；「生成纪要」仍是追加，「提炼事实」仍是全量替换。
- **测试**：131/131 通过。

### 1.0.0

- 按私聊 id 隔离：`vault`、`facts`、`summaries`、`media`、`state` 各自分目录，互不串味。
- 前情纪要只追加、5% 预算；事实支持增量提炼和全量重做归档。
- 80% 压力改用 AstrBot 实际注入的 token，不再只靠字符估算。
- 无模型检索链路成形：FTS5 + 本地哈希向量 + RRF + MMR，支持日期、最早事件和检索工具。

