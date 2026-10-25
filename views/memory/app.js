// 全量记忆体 · WebUI
// 通过 AstrBot 插件页面 bridge 调后端，不直接访问 cookie / 父页面。

const PLUGIN = "astrbot_plugin_all_memory";
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
const BLOB_BASE = `/api/v1/plugins/extensions/${PLUGIN}/blob?path=`;

const state = {
  sessions: [],
  kind: "all",
  filter: "",
  session: null,
  uid: "",
  total: 0,
  loading: false,
  // 左侧列表的模式：sessions（会话记录）/ facts（人物卡）
  mode: "sessions",
  // 勾选的会话（下载用），和"当前打开"是两件事
  selected: new Set(),
  facts: [],
  factFilter: "",
  fact: null,
  // 全局接管状态：auto / on / off（对所有会话生效）
  takeover: "auto",
  // 右侧历史面板顶部的纪要是否展开
  summaryOpen: false,
  // 是否贴在最底部：打开会话默认贴底，像 QQ 一样直接看到最新一条。
  // 用户往上翻就变 false，图片加载完也不会把他拽回去。
  stickBottom: true,
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};

// ---------------------------------------------------------------- bridge

async function apiGet(path, params) {
  if (!bridge) throw new Error("插件页面 bridge 未加载，请从 AstrBot 插件页进入");
  if (typeof bridge.apiGet === "function") return bridge.apiGet(path, params || {});
  throw new Error("bridge 不支持 apiGet");
}

async function apiPost(path, body) {
  if (!bridge) throw new Error("插件页面 bridge 未加载，请从 AstrBot 插件页进入");
  if (typeof bridge.apiPost === "function") return bridge.apiPost(path, body);
  if (typeof bridge.api === "function") return bridge.api("POST", path, body);
  throw new Error("bridge 不支持 POST");
}

function unwrap(res) {
  if (res == null) return {};
  if (typeof res === "object" && "status" in res && "data" in res) {
    if (res.status === "error") throw new Error(res.message || "请求失败");
    return res.data || {};
  }
  return res;
}

// ---------------------------------------------------------------- 提示

let toastTimer = null;
function toast(message, isError = false, ms = 0) {
  const box = $("toast");
  box.textContent = message;
  box.classList.toggle("error", isError);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; },
    ms || (isError ? 6000 : 3500));
}

function fmtSize(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtTime(ts) {
  if (!ts) return "";
  const text = String(ts).replace("T", " ");
  // 同一天内只显示时分，跨天带上日期
  const today = new Date();
  const month = String(today.getMonth() + 1).padStart(2, "0");
  const day = String(today.getDate()).padStart(2, "0");
  const datePart = `${today.getFullYear()}-${month}-${day}`;
  return text.startsWith(datePart) ? text.slice(11, 16) : text.slice(5, 16);
}

// ---------------------------------------------------------------- 总览

async function loadOverview() {
  try {
    const data = unwrap(await apiGet("overview"));
    const cards = [
      ["原始层事件", data.events ?? 0],
      ["会话数", data.sessions ?? 0],
      ["长期事实", data.facts ?? 0],
      ["前情纪要", `${data.summaries ?? 0} 份 · ${data.summary_chars ?? 0} 字`],
      ["附件", `${data.media_files ?? 0} 个 · ${fmtSize(data.media_bytes)}`],
      ["索引条数", data.indexed ?? 0],
      ["覆盖天数", data.days ?? 0],
    ];
    const box = $("stats");
    box.replaceChildren();
    for (const [key, value] of cards) {
      const card = el("div", "stat");
      card.append(el("div", "k", key), el("div", "v", String(value)));
      box.append(card);
    }
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------------------------------------------------------------- 会话列表

async function loadSessions() {
  try {
    const data = unwrap(await apiGet("sessions"));
    state.sessions = Array.isArray(data.items) ? data.items : [];
    renderSessions();
  } catch (err) {
    toast(err.message, true);
  }
}

function renderSessions() {
  const list = $("session-list");
  list.replaceChildren();

  const keyword = state.filter.trim().toLowerCase();
  const items = state.sessions.filter((item) => {
    if (state.kind !== "all" && item.kind !== state.kind) return false;
    if (!keyword) return true;
    return `${item.session} ${item.sample}`.toLowerCase().includes(keyword);
  });

  if (!items.length) {
    list.append(el("li", "empty", "没有匹配的会话"));
    syncSelectionUI(items);
    return;
  }

  for (const item of items) {
    const li = el("li");
    if (state.session === item.session) li.classList.add("active");

    const name = el("div", "name");
    const pick = document.createElement("input");
    pick.type = "checkbox";
    pick.className = "pick";
    pick.checked = state.selected.has(item.session);
    pick.title = "勾选后可以一起下载";
    pick.addEventListener("click", (event) => event.stopPropagation());
    pick.addEventListener("change", () => {
      if (pick.checked) state.selected.add(item.session);
      else state.selected.delete(item.session);
      syncSelectionUI(items);
    });

    const badge = el("span", `badge ${item.kind === "group" ? "group" : ""}`);
    badge.textContent = item.kind === "group" ? "群" : item.kind === "private" ? "私" : "?";
    name.append(pick, badge, document.createTextNode(item.session || "(未分类)"));

    const meta = el("div", "meta",
      `${item.count} 条 · ${fmtTime(item.last_ts)}` +
      (item.sample ? ` · ${item.sample.slice(0, 18)}` : ""));

    li.append(name, meta);
    li.addEventListener("click", () => selectSession(item));
    list.append(li);
  }

  syncSelectionUI(items);
}

function syncSelectionUI(items) {
  const visible = Array.isArray(items) ? items : [];
  const chosen = visible.filter((item) => state.selected.has(item.session)).length;

  const all = $("select-all");
  all.checked = visible.length > 0 && chosen === visible.length;
  all.indeterminate = chosen > 0 && chosen < visible.length;

  const button = $("btn-download");
  const total = state.selected.size;
  button.disabled = total === 0;
  button.title = total ? `下载勾选的 ${total} 个会话` : "下载勾选的会话";
}

// ---------------------------------------------------------------- 历史

function selectSession(item) {
  state.session = item.session;
  state.uid = item.uid || "";
  state.total = item.count;
  state.stickBottom = true;
  $("history-list").replaceChildren();
  $("history-title").textContent = item.session || "(未分类)";
  $("history-meta").textContent = `${item.count} 条`;
  closeSessionSummary();                 // 换会话，旧纪要不能留着
  $("btn-session-summary").hidden = false;
  renderUsage(item);
  renderSessions();
  loadHistory();
}

// ---------------------------------------------------------------- 接管开关

function renderTakeover(data) {
  const btn = $("btn-takeover");
  if (!btn) return;
  const mode = (data && data.mode) || "auto";
  const windowSize = Number(data && data.window);
  state.takeover = mode;
  btn.hidden = false;
  btn.classList.toggle("on", mode !== "off" && windowSize > 0);
  btn.classList.toggle("warn", mode !== "off" && !(windowSize > 0));

  if (mode === "off") {
    btn.textContent = "全局接管：关";
    btn.title = "所有会话都不再裁剪、不再注入，交回 AstrBot 原生上下文。点击全局开启。";
    return;
  }
  if (mode === "on") {
    btn.textContent = data && data.manual ? "全局接管：开" : "全局接管：开（自动）";
    btn.title = "所有会话每轮注入纪要/事实/地图/检索。点击全局关闭。";
  } else {
    btn.textContent = "全局接管：关（自动）";
    btn.title = "默认关；任意会话第一次裁剪时自动开启，之后所有会话都注入。点一下立即开启。";
  }
  if (!(windowSize > 0)) {
    btn.textContent = "全局接管：未生效";
    btn.title = "还没填上下文窗口，接管不会注入；先去设置页填窗口。";
  }
}

async function loadTakeover() {
  try {
    const data = unwrap(await apiGet("takeover"));
    renderTakeover(data);
  } catch (err) {
    // 老宿主没有这个接口时不显示按钮，不影响其它功能
    $("btn-takeover").hidden = true;
  }
}

async function setTakeover(on) {
  const btn = $("btn-takeover");
  btn.disabled = true;
  try {
    let data;
    try {
      // 用字符串而不是布尔：有些 bridge 会把 falsy 参数吞掉
      data = unwrap(await apiPost("takeover", { on: on ? "on" : "off" }));
    } catch (postErr) {
      // 有些版本的 bridge 只给 GET
      data = unwrap(await apiGet("takeover", { on: on ? "1" : "0" }));
    }
    renderTakeover(data);
    toast(on ? "已开启全局接管：所有会话（含新会话）都会注入记忆\n"
                + "人物卡按人跨会话；纪要仍按当前会话"
             : "已关闭全局接管：所有会话回到官方上下文");
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function toggleTakeover() {
  const mode = state.takeover || "auto";
  if (mode === "off" || mode === "auto") {
    await setTakeover(true);
    return;
  }
  if (!window.confirm(
    "关闭全局接管？\n\n" +
    "关闭后所有会话都不再裁剪、不再注入记忆块，交回 AstrBot 原生上下文管理。\n" +
    "已经被裁掉的历史不会自动回来；如果宿主内置压缩也关了，长对话可能撞模型上限。")) {
    return;
  }
  await setTakeover(false);
}

// ---------------------------------------------------------------- 前情纪要

function closeSessionSummary() {
  state.summaryOpen = false;
  $("summary-box").hidden = true;
}

async function toggleSessionSummary() {
  if (!state.session) return;
  if (!$("summary-box").hidden) {
    closeSessionSummary();
    return;
  }
  $("summary-box").hidden = false;
  state.summaryOpen = true;
  await loadSessionSummary();
}

async function loadSessionSummary() {
  if (!state.session) return;
  $("summary-meta").textContent = "前情纪要";
  $("summary-text").textContent = "读取中…";
  try {
    const data = unwrap(await apiGet("summary", { session: state.session }));
    renderSummary(data);
  } catch (err) {
    $("summary-text").textContent = err.message;
  }
}

function renderSummary(data) {
  if (!data || !data.exists) {
    $("summary-meta").textContent = "前情纪要：还没有";
    $("summary-text").textContent =
      "点右上角「生成纪要」：从第一条开始写，只追加不覆盖；" +
      "之后每次裁剪会自动把新内容续在后面。";
    return;
  }
  $("summary-meta").textContent =
    `前情纪要 · ${data.chars} 字 · 覆盖到 ${fmtTime(data.covered_until)} · v${data.version}`;
  $("summary-text").textContent = data.text;
}

let summaryPoll = null;
let summaryPollFails = 0;

function stopSummaryPoll() {
  if (summaryPoll) {
    clearInterval(summaryPoll);
    summaryPoll = null;
  }
  summaryPollFails = 0;
}

function setSummaryButton(running) {
  const button = $("btn-summary");
  if (!button) return;
  button.disabled = running;
  button.textContent = running ? "生成中…" : "生成纪要";
}

function startSummaryPoll() {
  if (summaryPoll) return;
  summaryPollFails = 0;
  summaryPoll = setInterval(async () => {
    try {
      const status = unwrap(await apiGet("summary/status"));
      summaryPollFails = 0;
      if (status.building) return;
      stopSummaryPoll();
      setSummaryButton(false);
      await loadOverview();
      if (state.summaryOpen) await loadSessionSummary();
      toast(`纪要已更新：共 ${status.sessions ?? 0} 份 · ${status.chars ?? 0} 字`);
    } catch (err) {
      // 连续失败就别一直转：恢复按钮，把错误说出来
      summaryPollFails += 1;
      if (summaryPollFails >= 5) {
        stopSummaryPoll();
        setSummaryButton(false);
        toast(`纪要进度查不到：${err.message}`, true);
      }
    }
  }, 3000);
}

async function loadSummaryStatus() {
  try {
    const status = unwrap(await apiGet("summary/status"));
    setSummaryButton(Boolean(status.building));
    if (status.building) startSummaryPoll();
  } catch (err) {
    // 老宿主没有这个接口，静默跳过
  }
}

async function buildSummary() {
  if (!state.session) {
    toast("先在左侧选一个会话", true);
    return;
  }
  if (!window.confirm(
    "生成前情纪要？\n\n" +
    "只追加：从上次覆盖到的那条之后，一直写到当前最后一条。\n" +
    "已有纪要原样保留，不覆盖、不合并重写。")) return;

  const btn = $("btn-summary");
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    let info;
    try {
      info = unwrap(await apiPost("summary/build", { session: state.session }));
    } catch (postErr) {
      info = unwrap(await apiGet("summary/build", { session: state.session }));
    }
    toast(info.message || "已开始生成纪要");
    startSummaryPoll();
  } catch (err) {
    const message = String((err && err.message) || "");
    // 后台其实已经在跑：别报错、别让按钮回到可点，接上进度就好
    if (/正在生成|等它结束|已经在生成/.test(message)) {
      btn.textContent = "生成中…";
      startSummaryPoll();
      toast("纪要正在后台生成，完成后会自动刷新");
      return;
    }
    toast(message, true);
    btn.disabled = false;
    btn.textContent = "生成纪要";
  }
}

// 每次切换会话换一个号，慢请求回来时如果号变了就直接丢掉
let historyToken = 0;

async function loadHistory() {
  if (!state.session) return;
  const token = ++historyToken;
  const list = $("history-list");
  state.loading = true;
  list.classList.add("loading");

  try {
    // limit: 0 = 整段一次拉完。分页按钮去掉了，滚动条要能一口气拖到底。
    const data = unwrap(await apiGet("history", {
      session: state.session,
      offset: 0,
      limit: 0,
      newest_first: 0,          // 正序：最新的在最后，跟聊天窗口一样翻
    }));
    if (token !== historyToken) return;      // 期间又点了别的会话

    const items = Array.isArray(data.items) ? data.items : [];
    list.replaceChildren();
    if (!items.length) {
      list.append(el("div", "empty", "这个会话还没有记录"));
    } else {
      const frag = document.createDocumentFragment();
      for (const msg of items) frag.append(renderMessage(msg));
      list.append(frag);
    }

    state.total = data.total ?? items.length;
    $("history-meta").textContent = `共 ${state.total} 条`;
    scrollToBottom(true);                    // 打开就在最新一条
  } catch (err) {
    if (token === historyToken) toast(err.message, true);
  } finally {
    if (token === historyToken) {
      state.loading = false;
      list.classList.remove("loading");
    }
  }
}

// 贴底工具：布局要两帧才稳定（字体、图片占位），所以连贴几次
function isNearBottom() {
  const list = $("history-list");
  return list.scrollHeight - list.scrollTop - list.clientHeight < 80;
}

function scrollToBottom(force = false) {
  const list = $("history-list");
  if (!force && !state.stickBottom) return;
  state.stickBottom = true;
  const pin = () => { list.scrollTop = list.scrollHeight; };
  pin();
  requestAnimationFrame(() => {
    pin();
    requestAnimationFrame(pin);
  });
}

// 每轮各块实际占窗口多少 —— 用来看预算有没有被吃满
const USAGE_LABELS = [
  ["system", "人格/工具"],
  ["history", "历史"],
  ["summary", "纪要"],
  ["facts", "事实"],
  ["map", "地图"],
  ["recall", "检索"],
  ["total", "合计"],
];

function renderUsage(item) {
  const box = $("usage");
  const usage = item && item.usage;
  const pct = usage && usage.pct;
  if (!pct || !Object.keys(pct).length) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  box.replaceChildren();
  for (const [key, label] of USAGE_LABELS) {
    if (pct[key] === undefined) continue;
    const chip = el("span", `chip ${key === "total" ? "total" : ""}`);
    chip.textContent = `${label} ${Number(pct[key]).toFixed(1)}%`;
    box.append(chip);
  }
  if (usage.facts_ratio) {
    const chip = el("span", "chip muted");
    chip.textContent = `facts 配额 ${usage.facts_ratio}%（第 ${usage.trim_count || 0} 次裁剪）`;
    box.append(chip);
  }
  box.hidden = false;
}

const REMINDER_RE = /<system_reminder>[\s\S]*?<\/system_reminder>/gi;

function cleanText(text) {
  return String(text || "").replace(REMINDER_RE, "").trim();
}

// 来源标记：告诉人"这条是怎么进库的"。常态（回填）不显示，省得满屏都是。
const ORIGIN_LABELS = {
  astrbot_context: "",
  astrbot_turn: "实时",
  import_astrbot: "导入",
  import_beyond: "导入",
  restore: "恢复",
  retry: "补写",
  response_hook: "",
};

function originLabel(origin) {
  const value = String(origin || "");
  if (!value) return "";
  return Object.prototype.hasOwnProperty.call(ORIGIN_LABELS, value)
    ? ORIGIN_LABELS[value]
    : value;
}

function renderMessage(msg) {
  const role = String(msg.role || "user");
  const node = el("div", `msg ${role}`);

  const head = el("div", "role");
  head.append(el("span", "", roleLabel(role)));
  head.append(el("span", "", fmtTime(msg.ts)));
  if (msg.backfill) {
    const tag = el("span", "tag", "回填");
    tag.title = "历史是补录的，时间取的是归档时刻，不是当时发生的时刻";
    head.append(tag);
  }
  if (msg.origin) {
    const label = originLabel(msg.origin);
    if (label) head.append(el("span", "", label));
  }

  node.append(head, el("div", "text", cleanText(msg.text)));

  const media = Array.isArray(msg.media) ? msg.media : [];
  if (media.length) {
    const box = el("div", "media");
    for (const item of media) {
      const src = BLOB_BASE + encodeURIComponent(item.path || "");
      if (item.kind === "image") {
        const img = el("img");
        img.src = src;
        img.alt = item.name || "图片";
        img.loading = "lazy";
        // 图片解码完高度会变：贴底时顺手再贴一次，否则会被顶上去
        img.addEventListener("load", () => {
          if (state.stickBottom) scrollToBottom(true);
        });
        box.append(img);
      } else {
        const link = el("a", "", item.kind === "voice" ? "🎙 语音" : `📎 ${item.name || "文件"}`);
        link.href = src;
        link.target = "_blank";
        link.rel = "noopener";
        box.append(link);
      }
    }
    node.append(box);
  }
  return node;
}

function roleLabel(role) {
  return {
    user: "对方",
    assistant: "机器人",
    tool: "工具",
    memory: "记忆",
    summary: "摘要",
    system: "系统",
  }[role] || role;
}

// ---------------------------------------------------------------- 事实

const KIND_LABELS = {
  identity: "身份",
  relationship: "关系",
  preference: "偏好",
  fact: "事实",
  event: "事件",
};

function kindLabel(kind) {
  return KIND_LABELS[kind] || kind || "事实";
}

async function loadFacts() {
  try {
    const data = unwrap(await apiGet("facts"));
    state.facts = Array.isArray(data.items) ? data.items : [];
    renderFacts();
  } catch (err) {
    toast(err.message, true);
  }
}

function renderFacts() {
  const list = $("fact-list");
  list.replaceChildren();

  const keyword = state.factFilter.trim().toLowerCase();
  const items = state.facts.filter((item) => {
    if (!keyword) return true;
    return `${item.claim} ${item.kind} ${item.id} ${item.uid || ""}`.toLowerCase().includes(keyword);
  });

  if (!items.length) {
    list.append(el("li", "empty",
      state.facts.length ? "没有匹配的事实" : "还没有提炼出任何事实，点右上角「提炼事实」"));
    return;
  }

  for (const item of items) {
    const li = el("li");
    if (state.fact && state.fact.id === item.id) li.classList.add("active");

    const claim = el("div", "claim", item.claim || "(空)");
    const meta = el("div", "meta");
    meta.append(el("span", `badge kind-${item.kind}`, kindLabel(item.kind)));
    if (item.scope === "people") {
      const shared = el("span", "badge", "长期");
      shared.title = "关于这个人的长期事实";
      meta.append(shared);
    }
    if (item.uid) {
      meta.append(el("span", "chip muted", `id ${item.uid}`));
    }
    meta.append(document.createTextNode(
      `${item.sources} 处出处 · ${fmtTime(item.updated_at)}`));

    li.append(claim, meta);
    li.addEventListener("click", () => selectFact(item));
    list.append(li);
  }
}

async function selectFact(item) {
  state.fact = item;
  renderFacts();

  $("fact-title").textContent = item.claim || "(没有内容)";
  $("fact-meta").textContent = "读取中…";
  const body = $("fact-body");
  body.replaceChildren(el("div", "empty", "读取中…"));

  try {
    const data = unwrap(await apiGet("fact", { id: item.id, uid: item.uid || state.uid }));
    renderFactDetail(data);
  } catch (err) {
    $("fact-meta").textContent = "";
    body.replaceChildren(el("div", "empty", err.message));
  }
}

function renderFactDetail(data) {
  const body = $("fact-body");
  body.replaceChildren();

  const versions = Array.isArray(data.versions) ? data.versions : [];
  const sources = Array.isArray(data.sources) ? data.sources : [];

  $("fact-title").textContent = data.claim || "(空)";
  $("fact-meta").textContent =
    `${data.id} · v${data.version} · ${versions.length} 个版本 · ${fmtTime(data.updated_at)}`;

  const head = el("div", "fact-head");
  const tags = el("div", "fact-tags");
  tags.append(el("span", `badge kind-${data.kind}`, kindLabel(data.kind)));
  if (data.deleted) tags.append(el("span", "tag", "已删除"));
  tags.append(el("span", "chip muted",
    data.scope === "people" ? "长期事实" : "本会话事实"));
  if (data.uid) tags.append(el("span", "chip muted", `id ${data.uid}`));
  if (data.subject) tags.append(el("span", "chip muted", `主角 ${data.subject}`));
  tags.append(el("span", "chip", `${sources.length} 处出处`));
  head.append(tags);
  body.append(head);

  body.append(el("div", "fact-label", "来源原话（引用前可以对照这里的全文）"));
  if (!sources.length) {
    body.append(el("div", "empty", "这条事实没有可用的出处"));
  }
  for (const src of sources) {
    const box = el("div", "source");
    const head2 = el("div", "role");
    head2.append(el("span", "", fmtTime(src.ts)),
                 el("span", "", roleLabel(src.role)),
                 el("span", "dim", String(src.fp || "").slice(0, 8)));
    box.append(head2);
    box.append(el("blockquote", "q",
      src.found ? (src.text + (src.truncated ? "…" : ""))
                : "（原文不在索引里，可能重建过）"));
    body.append(box);
  }

  body.append(el("div", "fact-label", `版本历史（${versions.length} 个）`));
  for (const ver of versions.slice().reverse()) {
    const row = el("div", "version");
    const stamp = `v${ver.version} · ${fmtTime(ver.extracted_at)}` +
      (ver.model ? ` · ${ver.model}` : "");
    row.append(el("div", "vhead", stamp));
    row.append(el("div", "vclaim", ver.claim || ""));
    body.append(row);
  }
}

// ---------------------------------------------------------------- 提炼

const REFINE_POLL_MS = 3000;
let refinePoll = null;

function applyRefineState(status) {
  const btn = $("btn-refine");
  if (!btn) return;
  const running = Boolean(status && status.refining);
  btn.disabled = running;
  btn.textContent = running ? "提炼中…" : "提炼事实";
  if (running) startRefinePoll(); else stopRefinePoll();
}

function startRefinePoll() {
  if (refinePoll) return;
  refinePoll = setInterval(async () => {
    try {
      const status = unwrap(await apiGet("facts/status"));
      if (status.refining) return;
      stopRefinePoll();
      applyRefineState(status);
      await Promise.all([loadFacts(), loadOverview()]);
      const last = status.last_refine || {};
      if (last.written !== undefined) {
        toast(`提炼完成：${last.batches || 0} 批 → 写 ${last.written} 条事实`);
      } else {
        toast("提炼完成");
      }
      if (state.mode === "facts" && !state.fact && state.facts.length) {
        selectFact(state.facts[0]);
      }
    } catch (err) {
      // 轮询期间的瞬时错误不打扰用户，下一轮再说
    }
  }, REFINE_POLL_MS);
}

function stopRefinePoll() {
  if (refinePoll) {
    clearInterval(refinePoll);
    refinePoll = null;
  }
}

async function loadRefineStatus() {
  try {
    applyRefineState(unwrap(await apiGet("facts/status")));
  } catch (err) {
    // 老版本后台没有这个接口，静默跳过
  }
}

async function refineFacts() {
  if (!state.session) {
    toast("先在左侧选一个会话，再提炼这个 id 的事实", true);
    return;
  }
  if (!window.confirm(
    "开始全量提炼？\n\n" +
    "会扫一遍这个 id 的整个记忆库，按每批字数调用模型；库大的话要几分钟。\n" +
    "已有的人物卡会先归档，再整体替换。")) return;

  const btn = $("btn-refine");
  btn.disabled = true;
  btn.textContent = "启动中…";
  try {
    let info;
    try {
      info = unwrap(await apiPost("facts/refine", { session: state.session }));
    } catch (postErr) {
      // 有些版本的 bridge 只给 GET
      info = unwrap(await apiGet("facts/refine", { session: state.session }));
    }
    toast(info.message || "已开始提炼，完成后会自动刷新");
    btn.textContent = "提炼中…";
    startRefinePoll();
  } catch (err) {
    toast(err.message, true);
    btn.disabled = false;
    btn.textContent = "提炼事实";
  }
}

// ---------------------------------------------------------------- 模式切换

function setMode(mode) {
  state.mode = mode === "facts" ? "facts" : "sessions";
  for (const tab of $("modes").querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.mode === state.mode);
  }
  const facts = state.mode === "facts";
  $("session-tools").hidden = facts;
  $("session-bar").hidden = facts;
  $("fact-search").hidden = !facts;
  $("session-list").hidden = facts;
  $("fact-list").hidden = !facts;
  $("history-panel").hidden = facts;
  $("fact-panel").hidden = !facts;

  if (facts) {
    loadFacts().then(() => {
      if (!state.fact && state.facts.length) selectFact(state.facts[0]);
    });
  } else if (state.session) {
    loadHistory();
  }
}

// ---------------------------------------------------------------- 设置

async function loadSettings() {
  try {
    const data = unwrap(await apiGet("settings"));
    const values = data.values || {};
    applySlider("trigger", values.trigger_ratio, 80);
    applySlider("total", values.total_ratio, 40);
    if (values.recall_limit !== undefined) $("recall_limit").value = values.recall_limit;
    if (values.map_days !== undefined) $("map_days").value = values.map_days;
    if (values.dry_run !== undefined) $("dry_run").checked = Boolean(values.dry_run);
  } catch (err) {
    toast(err.message, true);
  }
}

function applySlider(key, value, fallbackPercent) {
  const input = $(key);
  const label = $(`${key}-val`);
  const percent = Math.round((Number(value) || fallbackPercent / 100) * 100);
  input.value = String(percent);
  label.textContent = `${percent}%`;
}

// 面板上的五项默认值，和 _conf_schema.json / settings.py 保持一致
const DEFAULTS_UI = {
  trigger: 80,          // 80%
  total: 40,            // 40%
  recall_limit: 50,     // 50 条
  map_days: 365,        // 365 天
  dry_run: false,
};

// 一键回到默认值，顺手保存（配置页也会跟着变）
async function resetSettings() {
  applySlider("trigger", DEFAULTS_UI.trigger / 100, DEFAULTS_UI.trigger);
  applySlider("total", DEFAULTS_UI.total / 100, DEFAULTS_UI.total);
  $("recall_limit").value = DEFAULTS_UI.recall_limit;
  $("map_days").value = DEFAULTS_UI.map_days;
  $("dry_run").checked = DEFAULTS_UI.dry_run;
  await saveSettings();
}

async function saveSettings() {
  const trigger = Number($("trigger").value) / 100;
  const total = Number($("total").value) / 100;
  if (total > trigger) {
    toast("裁剪后总量不能大于触发阈值", true);
    return;
  }
  const values = {
    trigger_ratio: trigger,
    total_ratio: total,
    recall_limit: Number($("recall_limit").value) || DEFAULTS_UI.recall_limit,
    map_days: Number($("map_days").value) || DEFAULTS_UI.map_days,
    dry_run: $("dry_run").checked,
  };
  try {
    let data;
    try {
      data = unwrap(await apiPost("settings/save", { values }));
    } catch (postErr) {
      // 有些版本的 bridge 没有 apiPost，退回 GET + query
      data = unwrap(await apiGet("settings/save", values));
    }
    const got = data.values || {};
    applySlider("trigger", got.trigger_ratio, 80);
    applySlider("total", got.total_ratio, 40);
    if (got.recall_limit !== undefined) $("recall_limit").value = got.recall_limit;
    if (got.map_days !== undefined) $("map_days").value = got.map_days;
    if (got.dry_run !== undefined) $("dry_run").checked = Boolean(got.dry_run);
    $("save-hint").textContent = data.message || "已保存";
    toast("设置已保存，下一轮对话生效");
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------------------------------------------------------------- 导出

// 页面被 iframe 嵌入时，Dashboard 通常会把 api_key 放在 query 上。
// 直接跳转到 export 接口下载时必须带上它，否则服务端会回 "Missing API key"。
function apiKey() {
  try {
    const fromQuery = new URLSearchParams(window.location.search).get("api_key");
    if (fromQuery) return fromQuery;
    return localStorage.getItem("api_key")
      || localStorage.getItem("astrbot_api_key")
      || "";
  } catch (err) {
    return "";
  }
}

function showExportResult(info) {
  const parts = [
    `${info.name || "压缩包"}`,
    fmtSize(info.size),
    `${info.files || 0} 个文件`,
  ];
  toast(`已生成：${parts.join(" · ")}`);
  if (info.path) {
    $("history-meta").textContent = `导出包：${info.path}`;
    $("history-meta").title = info.path;
    console.log("[all_memory] 导出包路径:", info.path);
  }
}

async function exportAll() {
  const button = $("btn-export");
  button.disabled = true;
  button.textContent = "打包中…";
  try {
    // 用 GET：接口两边都接受，且 bridge 的 GET 通道最稳
    const info = unwrap(await apiGet("export", {}));
    showExportResult(info);
    toast(`打包完成：${info.files} 个文件，${fmtSize(info.size)}`);

    await saveFile(info);
    toast(`打包完成：${info.files} 个文件，${fmtSize(info.size)}\n`
        + `保存位置：${savedHint(info)}\n`
        + `（若浏览器改过下载目录，以浏览器为准）`,
        false, 12000);
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "一键保存全量数据";
  }
}

async function verifyMemory() {
  const button = $("btn-verify-m");
  button.disabled = true;
  button.textContent = "校验中…";
  try {
    const info = unwrap(await apiGet("verify_m", {}));
    const counts = info.counts || {};
    const lines = [
      info.ok ? "记忆体校验通过" : "记忆体校验失败",
      `事件 ${counts.events || 0} / 事实 ${counts.facts || 0} / `
        + `纪要 ${counts.summaries || 0}`,
      `校验文件 ${info.checked_files || 0} 个`,
    ];
    for (const item of (info.errors || []).slice(0, 5)) lines.push(`错误：${item}`);
    for (const item of (info.warnings || []).slice(0, 3)) lines.push(`警告：${item}`);
    toast(lines.join("\n"), !info.ok, info.ok ? 8000 : 15000);
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "校验记忆体";
  }
}

// ---------------------------------------------------------------- 下载

/**
 * 让浏览器存一个服务器上的文件。
 *
 * 优先走宿主的 bridge.download：父页面用**仪表盘自己的凭据**去取，
 * 再触发下载，所以我们不需要 api_key。老宿主没有这个能力时，
 * 才退回"带 api_key 的直链"。
 */
async function saveFile(info) {
  const filename = info.name || "download.bin";
  if (bridge && typeof bridge.download === "function") {
    const result = await bridge.download("download", { name: filename }, filename);
    return (result && result.filename) || filename;
  }

  const key = apiKey();
  if (!key || !info.url) {
    throw new Error(`文件已生成在服务器 exports/ 下：${info.path || filename}`);
  }
  const join = info.url.includes("?") ? "&" : "?";
  const link = document.createElement("a");
  link.href = `${info.url}${join}api_key=${encodeURIComponent(key)}`;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  return filename;
}

function savedHint(info) {
  return info.downloads_path || info.name || "（浏览器默认下载目录）";
}

// ---------------------------------------------------------------- 会话下载

async function downloadSelected() {
  const sessions = [...state.selected];
  if (!sessions.length) {
    toast("先勾选要下载的会话", true);
    return;
  }
  const button = $("btn-download");
  button.disabled = true;
  try {
    // 三条路兜底：单段路径最稳，子路径留给老宿主
    const attempts = [
      () => apiPost("sessionexport", { sessions }),
      () => apiPost("session/export", { sessions }),
      () => apiGet("sessionexport", { sessions: sessions.join("|") }),
    ];
    let info = null;
    let lastError = null;
    for (const attempt of attempts) {
      try {
        info = unwrap(await attempt());
        break;
      } catch (err) {
        lastError = err;
        // 只有"路由没找到"才换下一条；其它错误（比如没勾会话）直接抛
        const message = String((err && err.message) || "");
        if (!/未找到|找不到|not\s*found|404/i.test(message)) throw err;
      }
    }
    if (!info) throw lastError || new Error("导出失败");

    await saveFile(info);
    toast(`已导出 ${sessions.length} 个会话 / ${info.count} 条 · ${fmtSize(info.bytes)}\n`
        + `保存位置：${savedHint(info)}\n`
        + `（若浏览器改过下载目录，以浏览器为准）`,
        false, 12000);
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = state.selected.size === 0;
  }
}

// ---------------------------------------------------------------- 导入

function openImport() {
  $("import-modal").hidden = false;
  $("import-result").textContent = "";
  const input = $("import-path");
  input.focus();
  input.select();
}

function closeImport() {
  $("import-modal").hidden = true;
}

function importPath() {
  return $("import-path").value.trim().replace(/^"|"$/g, "");
}

async function checkImport() {
  const path = importPath();
  if (!path) { toast("先填备份包路径", true); return; }
  try {
    const info = unwrap(await apiGet("import/check", { path }));
    const lines = [
      `可恢复：${info.ok ? "是" : "否"}`,
      `原始层天数：${info.vault_days}`,
      `人格快照：${info.personas}`,
      `事实：${info.facts}`,
      `附件：${info.blobs}`,
      `附件索引天数：${info.media_days}`,
      `包含 astrbot_data：${info.has_astrbot_data ? "是（不会还原）" : "否"}`,
      `体积：${fmtSize(info.size)}`,
    ];
    if (info.error) lines.push(`说明：${info.error}`);
    $("import-result").textContent = lines.join("\n");
  } catch (err) {
    toast(err.message, true);
  }
}

async function runImport() {
  const path = importPath();
  if (!path) { toast("先填备份包路径", true); return; }
  if (!window.confirm(`确认从这份备份合并恢复？\n\n${path}\n\n只合并，不会删除现有数据。`)) {
    return;
  }
  const button = $("btn-import-run");
  button.disabled = true;
  button.textContent = "恢复中…";
  $("import-result").textContent = "正在恢复，几百 MB 的包可能要一两分钟…";
  try {
    const info = unwrap(await apiGet("import/path", { path }));
    const lines = [
      `读取记录：${info.events_seen ?? 0}`,
      `新增记录：${info.events_added ?? 0}`,
      `跳过重复：${info.events_skipped ?? 0}`,
      `新增人格快照：${info.personas_added ?? 0}`,
      `新增事实：${info.facts_added ?? 0}`,
      `新增附件：${info.blobs_added ?? 0}`,
      `新增附件索引：${info.media_added ?? 0}`,
      `面板设置：${info.settings_restored ? "已恢复" : "沿用当前"}`,
      `会话状态：${info.state_added ?? 0} 份`,
    ];
    if (info.takeover_global) lines.push("已开启全局接管（所有会话）");
    if (info.index_rebuilt) lines.push(`索引已重建：${info.index_rebuilt} 条`);
    if (info.astrbot_data_ignored) {
      lines.push("包里的 astrbot_data/ 未还原（要还原请停机手动替换）");
    }
    if (info.error) lines.push(`错误：${info.error}`);
    $("import-result").textContent = lines.join("\n");
    toast(`恢复完成：新增 ${info.events_added ?? 0} 条，跳过 ${info.events_skipped ?? 0} 条`);
    await Promise.all([loadOverview(), loadSessions()]);
  } catch (err) {
    toast(err.message, true);
    $("import-result").textContent = `失败：${err.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "开始恢复";
  }
}

// ---------------------------------------------------------------- 事件绑定

function bind() {
  $("modes").addEventListener("click", (event) => {
    const button = event.target.closest(".tab");
    if (!button) return;
    setMode(button.dataset.mode || "sessions");
  });

  $("tabs").addEventListener("click", (event) => {
    const button = event.target.closest(".tab");
    if (!button) return;
    state.kind = button.dataset.kind || "all";
    for (const tab of $("tabs").querySelectorAll(".tab")) {
      tab.classList.toggle("active", tab === button);
    }
    renderSessions();
  });

  $("search").addEventListener("input", (event) => {
    state.filter = event.target.value || "";
    renderSessions();
  });

  $("fact-search").addEventListener("input", (event) => {
    state.factFilter = event.target.value || "";
    renderFacts();
  });

  // 贴底状态跟着滚动走：用户往上翻就不要再把他拽回底部
  $("history-list").addEventListener("scroll", () => {
    state.stickBottom = isNearBottom();
  });
  $("btn-save").addEventListener("click", saveSettings);
  $("btn-reset").addEventListener("click", resetSettings);
  $("btn-export").addEventListener("click", exportAll);
  $("btn-verify-m").addEventListener("click", verifyMemory);
  $("btn-download").addEventListener("click", downloadSelected);

  $("select-all").addEventListener("change", (event) => {
    const checked = event.target.checked;
    const keyword = state.filter.trim().toLowerCase();
    const visible = state.sessions.filter((item) => {
      if (state.kind !== "all" && item.kind !== state.kind) return false;
      if (!keyword) return true;
      return `${item.session} ${item.sample}`.toLowerCase().includes(keyword);
    });
    for (const item of visible) {
      if (checked) state.selected.add(item.session);
      else state.selected.delete(item.session);
    }
    renderSessions();
  });
  $("btn-refine").addEventListener("click", refineFacts);
  $("btn-summary").addEventListener("click", buildSummary);
  $("btn-takeover").addEventListener("click", toggleTakeover);
  $("btn-session-summary").addEventListener("click", toggleSessionSummary);

  $("btn-import").addEventListener("click", openImport);
  $("btn-import-close").addEventListener("click", closeImport);
  $("btn-import-check").addEventListener("click", checkImport);
  $("btn-import-run").addEventListener("click", runImport);
  $("import-modal").addEventListener("click", (event) => {
    if (event.target === $("import-modal")) closeImport();
  });
  $("import-path").addEventListener("keydown", (event) => {
    if (event.key === "Enter") checkImport();
  });

  for (const key of ["trigger", "total"]) {
    $(key).addEventListener("input", (event) => {
      $(`${key}-val`).textContent = `${event.target.value}%`;
    });
  }
}

// ---------------------------------------------------------------- 启动

(async function init() {
  bind();
  try {
    if (bridge && typeof bridge.ready === "function") await bridge.ready();
  } catch (err) {
    // ready 失败不阻塞，后面每个请求自己会报错
  }
  await Promise.all([loadOverview(), loadSessions(), loadSettings(), loadTakeover()]);

  // 打开面板就直接落在最近一次对话上，而不是一个空白的「选择左侧一个会话」
  if (!state.session && state.sessions.length) {
    selectSession(state.sessions[0]);
  }

  // 上次没跑完的提炼/纪要，进来就接着显示进度
  await loadRefineStatus();
  await loadSummaryStatus();

  // 首次裁剪会自动开全局接管；页面开着时也要跟着变，不能一直显示“关”
  setInterval(() => {
    if (!document.hidden) loadTakeover();
  }, 30000);
})();
