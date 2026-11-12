/* B站投稿台 —— 前端逻辑 */
(function () {
  "use strict";

  // 用 https://user:pass@host/ 打开时，地址栏里的凭据会让 fetch 构造相对 URL 直接报错
  (function stripUrlCredentials() {
    try {
      if (location.username || location.password) {
        history.replaceState(null, "", location.origin + location.pathname + location.search + location.hash);
      }
    } catch (e) { /* 老浏览器忽略 */ }
  })();

  const $ = (id) => document.getElementById(id);

  const PAGE_SIZE = 10;

  const state = {
    page: 1,
    tasks: [],
    templates: [],
    partitions: [],
    config: null,
    account: null,
    accounts: [],
    quota: null,
    quotaBusy: false,   // 额度统计请求进行中，避免连点重复打 B站
    env: null,
    files: [],          // { file, partTitle }
    coverFile: null,
    coverPreviewUrl: "",
    coverCleared: false,
    tags: [],
    mode: "single",
    expanded: {},
    logs: {},
    glogs: [],
    glogsSig: "",
    view: "tasks",
    archPage: 1,
    archStatus: "",    // 全部稿件的审核状态筛选："" | pubed | is_pubing | not_pubed
    archives: [],
    archSig: "",
    paused: false,
    pauseReason: "",       // "" | manual | quota | rate：额度满时队列是「等待」而非「停工」
    quotaResumeAt: 0,      // 额度预计腾出空位/解封的时刻，用于倒计时
    quotaBanUntil: 0,      // 24h 投稿封禁截止（额度归零）时刻
    quotaToast: false,
    uploading: false,
    sig: null,
    lastFetch: 0,
    seasons: [],
    seasonsLoaded: false,
    seasonsError: "",
    topic: { id: 0, name: "", mission: 0 },
    pollTimer: null,
    _lastSig: "",
    theme: "light",
    advOpen: true,
    taskFilter: "",
    counts: {},
    editTpl: null,      // 模板编辑态：{ id, name }，非空时「保存」覆盖该模板
    ovStats: {},       // 概览条：按 part 缓存 B站汇总数据
    ovBusy: {},        // 防止同一 part 并发重复请求
  };

  const $badge = {
    queued: ["排队中", "queued"],
    uploading: ["上传中", "uploading"],
    waiting_schedule: ["等待定时", "waiting"],
    success: ["投稿成功", "success"],
    failed: ["失败", "failed"],
    blocked: ["已拦截", "blocked"],
    cancelled: ["已取消", "cancelled"],
    interrupted: ["已中断", "failed"],
  };

  /* ---------------------------------------------------------- 工具 */
  function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || "GET", headers: { "Content-Type": "application/json" } };
    if (opts.body) init.body = JSON.stringify(opts.body);
    return fetch(new URL(path, location.origin).href, init).then(async (r) => {
      const data = await r.json().catch(() => ({ ok: false, error: "响应不是 JSON" }));
      if (r.status === 401) {
        toast("访问鉴权失败，请用 ?token=你的token 重新打开页面", "err", 6500);
      }
      if (!r.ok) throw Object.assign(new Error(data.error || ("HTTP " + r.status)), { data });
      return data;
    });
  }

  function toast(msg, kind, ms) {
    const div = document.createElement("div");
    div.className = "toast" + (kind ? " " + kind : "");
    div.textContent = msg;
    $("toastWrap").appendChild(div);
    setTimeout(() => div.remove(), ms || 2600);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(1) + " " + u[i];
  }

  function fmtDur(sec) {
    sec = Math.round(Number(sec) || 0);
    if (!sec) return "—";
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(Number(ts) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return (d.getMonth() + 1) + "/" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function partName(tid) {
    const p = state.partitions.find((x) => Number(x.tid) === Number(tid));
    return p ? p.name : "分区 " + tid;
  }

  /* ---------------------------------------------------------- 主题 */
  function applyTheme(theme, persist) {
    const t = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = t;
    state.theme = t;
    if (persist !== false) {
      try { localStorage.setItem("biliTheme", t); } catch (e) {}
    }
    const btn = $("btnTheme");
    if (btn) btn.title = t === "dark" ? "切换到浅色主题" : "切换到深色主题";
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("biliTheme"); } catch (e) {}
    const hadSaved = !!saved;
    if (!saved) {
      saved = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches)
        ? "dark" : "light";
    }
    applyTheme(saved, hadSaved);
    if (window.matchMedia) {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      const onChange = (e) => {
        let manual = null;
        try { manual = localStorage.getItem("biliTheme"); } catch (er) {}
        if (!manual) applyTheme(e.matches ? "dark" : "light", false);
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
    }
  }

  /* ---------------------------------------------------------- 定时倒计时 */
  // 倒计时放在前端按 publish_at 本地算：后端不再每 30 秒给每个定时任务
  // 写一次 message（原来会连带把整列任务卡重绘一遍）。
  function countdownText(at) {
    const sec = Number(at) - Date.now() / 1000;
    if (sec <= 0) return "即将投放";
    if (sec >= 86400) {
      return "还剩 " + Math.floor(sec / 86400) + " 天 " + Math.floor((sec % 86400) / 3600) + " 小时";
    }
    if (sec >= 3600) {
      return "还剩 " + Math.floor(sec / 3600) + " 小时 " + Math.floor((sec % 3600) / 60) + " 分";
    }
    return "还剩 " + Math.max(1, Math.floor(sec / 60)) + " 分钟";
  }

  function refreshCountdowns() {
    document.querySelectorAll(".task-countdown[data-at]").forEach((el) => {
      el.textContent = countdownText(Number(el.dataset.at));
    });
  }

  /* ---------------------------------------------------------- 初始化 */
  async function boot() {
    try {
      const r = await api("/api/bootstrap");
      applyBootstrap(r, true);
    } catch (e) {
      toast("加载失败：" + e.message, "err", 5000);
    }
    startPolling();
  }

  function applyBootstrap(r, first) {
    state.config = r.config;
    state.account = r.account;
    state.accounts = r.accounts || [];
    state.activeAccount = r.active_account || "";
    state.quota = r.quota;
    state.env = r.env;
    state.partitions = r.partitions || [];
    state.templates = r.templates || [];
    state.tasks = r.tasks || [];
    state.paused = !!(r.queue && r.queue.paused);
    state.pauseReason = (r.queue && r.queue.pause_reason) || "";
    state.quotaResumeAt = Number((r.queue && r.queue.quota_resume_at) || 0);
    state.quotaBanUntil = Number((r.queue && r.queue.quota_ban_until) || 0);
    state.rateUntil = (r.queue && r.queue.rate_until) || 0;
    state.rateWait = (r.queue && r.queue.rate_wait) || 0;
    renderRateBanner();
    if (r.version) $("appVer").textContent = r.version;

    buildPartitions();
    // 只在首次加载套用默认参数：扫码登录 / 保存设置也会触发 bootstrap，
    // 那时若重填表单，会把用户正在写的标题、简介、分区全部冲掉
    if (first) applyDefaults();
    renderAccount();
    renderQuota();
    renderTemplates();
    renderEnv();
    renderSettings();
    renderTasks(r.counts);
    renderFiles();
    fetchGlobalLogs();
    loadSeasons();
    // 账号或 Cookie 可能已变化（扫码登录 / 切号 / 保存设置），清掉前端概览缓存重新取
    if (!first) refreshOverview("bootstrap");
  }

  // 合集列表，登录后才拉得到；失败不打扰用户，只把提示写在旁边
  /* ---------------------------------------------------------- 话题 */
  function setTopic(t) {
    state.topic = { id: Number(t.id) || 0, name: t.name || "", mission: Number(t.mission_id) || 0 };
    const chip = $("topicChip");
    if (!chip) return;
    if (state.topic.id) {
      chip.hidden = false;
      $("topicChipText").textContent = "# " + state.topic.name;
      $("topicInput").value = "";
      $("topicHint").textContent = "已选话题 " + state.topic.id +
        (state.topic.mission ? "，参与活动 " + state.topic.mission : "");
    } else {
      chip.hidden = true;
      $("topicHint").textContent = "可选，输入后点搜索";
    }
    updateAdvCount();
  }

  function hideTopicSuggest() {
    const box = $("topicSuggest");
    if (!box) return;
    box.hidden = true;
    box.innerHTML = "";
  }

  // 从分享链接里抠出 topic_id / topic_name，也接受纯数字 ID
  function parseTopicText(text) {
    const raw = (text || "").trim();
    if (!raw) return null;
    const m = raw.match(/topic_id=(\d+)/i) || raw.match(/^\s*(\d{3,})\s*$/);
    if (!m) return null;
    let nm = raw.match(/topic_name=([^&\s]+)/i);
    nm = nm ? decodeURIComponent(nm[1]).replace(/\+/g, " ").trim() : "";
    if (/^\d+$/.test(nm)) nm = "";
    return { id: Number(m[1]) || 0, name: nm };
  }

  async function applyTopicText(text, opts) {
    const p = parseTopicText(text);
    if (!p) return false;
    const hint = $("topicHint");
    if (hint) hint.textContent = "正在识别话题 " + p.id + "…";
    try {
      const r = await api("/api/topics/info?id=" + p.id + "&name=" + encodeURIComponent(p.name || ""));
      if (!r.ok || !r.topic) throw new Error(r.error || "识别失败");
      const t = r.topic;
      setTopic({ id: t.id, name: t.name, mission_id: t.mission_id });
      if ($("topicInput")) $("topicInput").value = "";
      if (opts && opts.toast !== false) {
        toast("已识别话题「" + t.name + "」" + (t.mission_id ? "，含活动" : ""), "ok");
      }
      return true;
    } catch (e) {
      if (hint) hint.textContent = "识别失败：" + e.message;
      return true;
    }
  }

  async function searchTopics() {
    const inp = $("topicInput");
    const box = $("topicSuggest");
    if (!box) return;
    const kw = ((inp && inp.value) || "").trim();
    box.hidden = false;
    box.innerHTML = '<div class="suggest-empty">搜索中…</div>';
    try {
      const r = await api("/api/topics?kw=" + encodeURIComponent(kw));
      const items = r.items || [];
      if (!items.length) {
        box.innerHTML = '<div class="suggest-empty">' + (r.error || "没搜到相关话题") + '</div>';
        return;
      }
      box.innerHTML = "";
      items.forEach((t) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "suggest-item";
        b.innerHTML = '<strong># ' + esc(t.name) + '</strong><span>' + esc(t.desc || ("id " + t.id)) + '</span>';
        b.addEventListener("click", () => { setTopic(t); hideTopicSuggest(); });
        box.appendChild(b);
      });
    } catch (e) {
      box.innerHTML = '<div class="suggest-empty">' + esc(e.message) + '</div>';
    }
  }

  // 合集列表，登录后才拉得到；失败不打扰用户，只把提示写在旁边
  async function loadSeasons(force) {
    if (state.seasonsLoaded && !force) { renderSeasons(); return; }
    const hint = $("seasonHint");
    if (hint) hint.textContent = "加载中…";
    try {
      const r = await api("/api/seasons");
      if (!r.ok) throw new Error(r.error || "读取失败");
      state.seasons = r.items || [];
      state.seasonsLoaded = true;
    } catch (e) {
      state.seasons = [];
      state.seasonsLoaded = false;
      state.seasonsError = e.message;
    }
    renderSeasons();
  }

  // 转载稿件再勾「禁止转载」没意义：转载时把该选项置灰并取消勾选
  function syncCopyright() {
    const sel = $("copyrightSelect"), box = $("noReprint");
    if (!sel || !box) return;
    const isReprint = sel.value === "2";
    if (isReprint) box.checked = false;
    box.disabled = isReprint;
    const row = box.closest("label");
    if (row) {
      row.style.opacity = isReprint ? ".45" : "1";
      row.title = isReprint ? "转载稿件不适用" : "";
    }
  }

  function renderSeasons() {
    const sel = $("seasonSelect");
    const hint = $("seasonHint");
    if (!sel) return;
    const keep = Number(sel.value) || 0;
    sel.innerHTML = '<option value="0">不加入合集</option>' +
      '<option value="new">＋ 新建合集…</option>' +
      state.seasons.map((s) => '<option value="' + s.id + '">' +
        esc(s.title || ("合集 " + s.id)) + "（" + s.count + " 个）</option>").join("");
    if (keep && state.seasons.some((s) => s.id === keep)) sel.value = String(keep);
    if (hint) {
      if (state.seasons.length) hint.textContent = state.seasons.length + " 个可选";
      else hint.textContent = state.seasonsError ? "读取失败，点重试" : "账号下暂无合集";
    }
  }

  function applyDefaults() {
    const d = (state.config && state.config.defaults) || {};
    if (d.tid) $("tidSelect").value = String(d.tid);
    if (d.tags && d.tags.length && !state.tags.length) {
      state.tags = d.tags.slice(0, 10);
      renderTags();
    }
    if (d.desc && !$("descInput").value) {
      $("descInput").value = d.desc;
      updateCounters();
    }
    if (d.copyright) $("copyrightSelect").value = String(d.copyright);
    syncCopyright();
    if (state.config && state.config.channel && $("channelSelect")) $("channelSelect").value = state.config.channel;
  }

  // 前台基准 2.2 秒；空闲且无变化时按阶梯退避，页面切后台统一 10 秒
  const POLL_FAST = 2200;
  const POLL_SLOW = 10000;
  const POLL_STEPS = [2200, 5000, 10000, 20000];
  let pollStep = 0;

  function schedulePolling() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    const gap = document.hidden ? POLL_SLOW : POLL_STEPS[pollStep];
    state.pollTimer = setTimeout(pollTick, gap);
  }

  function pollTick() {
    Promise.resolve(pollTasks()).then(schedulePolling, schedulePolling);
  }

  function resetPollBackoff() { pollStep = 0; }

  function humanWait(secs) {
    const s = Math.max(0, Math.ceil(secs));
    if (s >= 3600) {
      const h = Math.floor(s / 3600), m = Math.ceil((s % 3600) / 60);
      return h + " 小时" + (m ? " " + m + " 分钟" : "");
    }
    return s >= 120 ? Math.ceil(s / 60) + " 分钟" : s + " 秒";
  }

  function renderRateBanner() {
    const box = $("rateBanner");
    if (!box) return;
    const now = Date.now() / 1000;
    const until = Number(state.rateUntil || 0);
    const wait = Number(state.rateWait || 0) || 60;
    const left = until - now;
    if (left > 0) {
      // 限流冷却优先展示：它是分钟级的即时状态，比额度等待更紧急
      box.hidden = false;
      box.dataset.mode = "rate";
      const longWait = wait >= 300;
      $("rateTitle").textContent = longWait
        ? "B站上传额度受限（10 分钟内投稿过多）"
        : "B站封面接口请求过频";
      $("rateText").textContent = "预计 " + humanWait(left) + "后自动继续投稿，稿件保持排队、不会重投"
        + (longWait ? "；若反复触发，请稍后再点「继续队列」。" : "。");
      $("rateFill").style.width = Math.min(100, Math.max(2, (left / wait) * 100)) + "%";
      return;
    }
    if (state.paused && state.pauseReason === "quota") {
      // 两种等待：n+1 触发的 24h 封禁（必须等额度归零）与刚好满 n（等一篇滑出）
      box.hidden = false;
      const n = (state.tasks || []).filter((t) => t.status === "queued").length;
      const ra = Number(state.quotaResumeAt || 0);
      const ban = Number(state.quotaBanUntil || 0);
      const banned = ban > now;
      box.dataset.mode = banned ? "ban" : "quota";
      const remain = ra > now ? "预计 " + humanWait(ra - now) + "后" : "正在复查额度，";
      if (banned) {
        $("rateTitle").textContent = "24h 投稿封禁中 · 稿件排队中";
        $("rateText").textContent = "投稿数已达上限 +1，触发 B站 24h 投稿封禁：封禁期内即使额度下降也不能投稿，"
          + "需等额度归零后自动解封。当前 " + n + " 个稿件在队列中，仍可继续添加；"
          + remain + "自动解封并继续投稿，不会重投、无需人工值守。";
      } else {
        $("rateTitle").textContent = "24h 投稿额度已满 · 稿件排队中";
        $("rateText").textContent = "当前 " + n + " 个稿件在队列中，仍可继续添加。"
          + remain + "腾出空位，队列会自动继续投稿，不会重投、无需人工点「继续队列」。";
      }
      $("rateFill").style.width = "100%";
      return;
    }
    box.hidden = true;
  }

  function startPolling() {
    setInterval(renderRateBanner, 1000);   // 倒计时按秒走，不依赖轮询退避
    schedulePolling();
    setInterval(refreshCountdowns, 30000);
    // 概览条：账号累计按天缓存，页面一直开着跨过中午 12:00 时自动换新
    setInterval(() => {
      const st = (state.ovStats || {}).workbench;
      if (st && st.day && st.day !== ovStatDay()) {
        clearOverviewCache("workbench");
        if (document.body.dataset.view === "workbench") applyOverviewMode("workbench");
      }
    }, 5 * 60 * 1000);
  }

  async function pollTasks() {
    if (state.uploading) return;          // 上传文件时不抢带宽
    try {
      const r = await api("/api/tasks");
      state.tasks = r.tasks || [];
      if (r.quota) { state.quota = r.quota; renderQuota(); }
      state.paused = !!(r.queue && r.queue.paused);
      state.pauseReason = (r.queue && r.queue.pause_reason) || "";
      state.quotaResumeAt = Number((r.queue && r.queue.quota_resume_at) || 0);
      state.quotaBanUntil = Number((r.queue && r.queue.quota_ban_until) || 0);
      state.rateUntil = (r.queue && r.queue.rate_until) || 0;
      state.rateWait = (r.queue && r.queue.rate_wait) || 0;
      renderRateBanner();
      renderTasks(r.counts);
      if (state.view === "all") renderArchives();
      $("btnPauseQueue").textContent = state.paused ? "继续队列" : "暂停队列";
      // 签名不含倒计时 message：等待中的定时任务每分钟改文案不会让退避失效
      const sig = (r.tasks || []).map(
        (t) => t.id + ":" + t.status + ":" + t.progress + ":" + t.stage).join("|")
        + ":" + JSON.stringify(r.counts);
      const changed = sig !== state._lastSig;
      pollStep = changed ? 0 : Math.min(pollStep + 1, POLL_STEPS.length - 1);
      state._lastSig = sig;
      fetchGlobalLogs();
      // 只在队列真的变了时刷新展开日志，避免每轮对每个展开任务各发一次请求
      if (changed && state.expanded && Object.keys(state.expanded).length) loadAllExpandedLogs();
    } catch (e) { /* 忽略轮询错误 */ }
  }

  /* ---------------------------------------------------------- 顶栏 */
  function renderAccount() {
    const a = state.account || {};
    const box = $("accountBox");
    if (a.logged_in) {
      box.className = "account";
      const ini = esc((a.uname || "?").slice(0, 1));
      box.innerHTML =
        (a.face ? '<img src="/api/proxy/cover?u=' + encodeURIComponent(a.face) + '" alt="" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline-flex\'"><span class="avatar-fallback" style="display:none">' + ini + "</span>"
                : '<span class="avatar-fallback">' + ini + "</span>") +
        '<span class="name">' + esc(a.uname || "已登录") + "</span>" +
        '<span class="dot"></span>';
    } else {
      box.className = "account off";
      box.innerHTML = '<span class="avatar-fallback">?</span><span class="name">未登录</span><span class="dot off"></span>';
    }
    renderLoginState();
  }

  // 未登录时直接把发布按钮禁用并给出引导，别让用户填完一堆字段点下去才被拒
  function renderLoginState() {
    const logged = !!(state.account && state.account.logged_in);
    const guide = $("loginGuide");
    if (guide) guide.hidden = logged;
    const btn = $("btnPublish");
    if (btn) {
      btn.disabled = !logged;
      btn.title = logged ? "" : "请先登录 B站账号";
    }
  }

  function renderQuota() {
    const q = state.quota || {};
    const limit = Number(q.limit || (state.config && state.config.quota_limit) || 50);
    const chip = $("quotaChip");
    if (typeof q.count !== "number" || q.logged_in === false) {
      $("quotaValue").textContent = "— / " + limit;
      $("quotaBar").style.width = "0%";
      chip.classList.remove("warn", "danger");
      chip.title = "未登录，无法统计近 24 小时投稿数";
      syncOverview();
      return;
    }
    const pct = Math.min(100, Math.round((q.count / limit) * 100));
    $("quotaValue").textContent = q.count + " / " + limit;
    $("quotaBar").style.width = pct + "%";
    chip.classList.toggle("warn", pct >= 60 && pct < 90);
    chip.classList.toggle("danger", pct >= 90);
    const by = q.by_status || {};
    const parts = Object.keys(by).map((k) => k + " " + by[k]).join("，");
    // 两个口径分开摆：已确认=B站列表能统计到的，未确认=本机已投出、列表还没显示的。
    // 相加（按 BV 去重后）才是真正用掉的额度，差在哪一眼能对上。
    const conf = (typeof q.confirmed === "number")
      ? "计数：已确认 " + q.confirmed
        + " + 未确认 " + (q.pending || 0) + " = " + q.count + "。"
        + ((q.pending || 0) > 0 ? "（未确认的是刚投出去、B站还没显示的）" : "")
      : "";
    const banUntil = Number(q.quota_ban_until || state.quotaBanUntil || 0);
    chip.title = (parts ? "近 24 小时投稿：" + parts + "。" : "") + conf
      + (q.ok ? "点击重新统计"
        : (banUntil > Date.now() / 1000
          ? "已达上限+1，24h 投稿封禁中：额度归零前不能投稿，新稿仍可排队"
          : "额度已满：新投稿仍可加入队列，额度恢复后自动继续"));
    syncOverview();
  }

  // 重新统计近 24 小时投稿数：顶栏额度胶囊与队列「剩余额度」格共用同一套逻辑
  async function refreshQuotaNow() {
    if (state.quotaBusy) return;
    state.quotaBusy = true;
    const el = $("quotaValue");
    const prev = el ? el.textContent : "";
    if (el) el.textContent = "统计中…";
    try {
      state.quota = await api("/api/quota/refresh", { method: "POST" });
      renderQuota();
      if (state.quota.error) toast(state.quota.error, "err", 3600);
      else {
        const by = state.quota.by_status || {};
        const parts = Object.keys(by).map((k) => k + " " + by[k]).join("，");
        toast("近 24 小时投稿 " + state.quota.count + " 个" + (parts ? "（" + parts + "）" : ""), "ok");
        if (state.quota.note) toast(state.quota.note, "err", 4200);
      }
    } catch (e) {
      if (el) el.textContent = prev;
      toast(e.message, "err");
    } finally {
      state.quotaBusy = false;
    }
  }

  /* ---------------------------------------------------------- 表单 */
  function buildPartitions() {
    const sel = $("tidSelect");
    if (sel.dataset.built === "1") return;
    sel.innerHTML = state.partitions.map((p) => '<option value="' + p.tid + '">' + esc(p.name) + "</option>").join("");
    sel.dataset.built = "1";
  }

  function renderTags() {
    const box = $("tagBox");
    const input = $("tagInput");
    box.querySelectorAll(".tag-chip").forEach((n) => n.remove());
    state.tags.forEach((t, i) => {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.innerHTML = esc(t) + '<button type="button" data-i="' + i + '" aria-label="移除">×</button>';
      box.insertBefore(chip, input);
    });
    $("tagCount").textContent = state.tags.length + " / 10";
  }

  function addTag(raw) {
    const t = String(raw).trim().replace(/[，,、]/g, "").slice(0, 20);
    if (!t) return;
    if (state.tags.indexOf(t) >= 0) return;
    if (state.tags.length >= 10) { toast("最多 10 个标签，先删掉一个"); return; }
    state.tags.push(t);
    renderTags();
  }

  // 一次可以粘/打多个标签：逗号、顿号、空格都当分隔符
  function addTagsFrom(raw) {
    String(raw).split(/[，,、\s]+/).forEach(addTag);
  }

  function updateCounters() {
    $("titleCount").textContent = $("titleInput").value.length + " / 80";
    $("descCount").textContent = $("descInput").value.length + " / 2000";
  }

  /* ---------------------------------------------------------- 文件 */
  function addFiles(list) {
    const accepted = [];
    Array.from(list).forEach((f) => {
      if (!/\.(mp4|flv|mkv|mov|avi|webm|ts|m4v|wmv)$/i.test(f.name) && !/video\//.test(f.type)) {
        toast("跳过非视频文件：" + f.name, "err");
        return;
      }
      state.files.push({ file: f, partTitle: f.name.replace(/\.[^.]+$/, "") });
      accepted.push(f);
    });
    renderFiles();
    if (accepted.length && !state.coverPreviewUrl && !state.coverCleared && state.files.length) {
      captureFrame(state.files[0].file);
    }
  }

  function captureFrame(file) {
    // 几个 GB 的视频不值得为了一张缩略图让浏览器去解码；上传时服务端
    // ffmpeg 抽帧兜底即可。同时给 objectURL 加超时回收，避免解码失败时泄漏。
    if (file.size > 500 * 1024 * 1024) return;
    const url = URL.createObjectURL(file);
    const timer = setTimeout(() => {
      try { URL.revokeObjectURL(url); } catch (e) {}
    }, 8000);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.muted = true;
    video.src = url;
    video.addEventListener("loadedmetadata", function () {
      try {
        video.currentTime = Math.min(1.2, (video.duration || 2) * 0.15);
      } catch (e) { URL.revokeObjectURL(url); }
    });
    video.addEventListener("seeked", function () {
      try {
        const c = document.createElement("canvas");
        c.width = 320;
        c.height = Math.round(320 * (video.videoHeight / video.videoWidth || 0.5625));
        c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
        state.coverPreviewUrl = c.toDataURL("image/jpeg", 0.72);
        renderCoverPreview();
      } catch (e) { /* 跨域或解码失败，忽略 */ }
      clearTimeout(timer);
      URL.revokeObjectURL(url);
    });
    video.addEventListener("error", () => { clearTimeout(timer); URL.revokeObjectURL(url); });
  }

  function renderFiles() {
    const wrap = $("fileList");
    if (!state.files.length) {
      wrap.innerHTML = "";
      return;
    }
    wrap.innerHTML = state.files.map((f, i) => {
      const withPart = state.mode === "multi";
      return (
        '<div class="file-row">' +
          '<span class="idx">' + (i + 1) + "</span>" +
          '<div style="min-width:0">' +
            '<div class="fname" title="' + esc(f.file.name) + '">' + esc(f.file.name) + "</div>" +
            '<div class="fmeta">' + fmtBytes(f.file.size) + "</div>" +
            (withPart ? '<input class="part-input" data-i="' + i + '" value="' + esc(f.partTitle) + '" placeholder="分P标题">' : "") +
          "</div>" +
          '<button class="remove" data-i="' + i + '" title="移除">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 6l12 12M18 6L6 18"/></svg>' +
          "</button>" +
        "</div>"
      );
    }).join("");
  }

  function renderCoverPreview() {
    const box = $("coverPreview");
    if (state.coverPreviewUrl) {
      box.style.backgroundImage = 'url("' + state.coverPreviewUrl + '")';
      box.textContent = "";
    } else {
      box.style.backgroundImage = "";
      box.textContent = state.files.length ? "上传时自动抽帧" : "无封面";
    }
  }

  /* ---------------------------------------------------------- 队列 */
  /* ---------------------------------------------------------- 工具栏 */
  // 视图切换时立刻同步工具栏，避免要等下一次 2.2s 轮询才隐藏/显示
  function syncToolbarForView() {
    const toolbar = document.querySelector(".queue-toolbar");
    if (!toolbar) return;
    const stats = toolbar.querySelector(".stats");
    if (stats) stats.style.display = state.view === "all" ? "none" : "";
    const pause = $("btnPauseQueue");
    if (pause) pause.style.display = state.view === "all" ? "none" : "";
    const tf = $("taskFilters");
    if (tf) tf.style.display = state.view === "all" ? "none" : "";
    // 全部稿件：队列状态胶囊换成审核状态筛选（概览格点击也是走这套状态）
    const af = $("archFilters");
    if (af) af.hidden = state.view !== "all";
    if (state.view === "all") syncArchFilters();
    // 刷新按钮在两种视图里含义不同，直接把文案写清楚，别让人猜它刷的是队列
    const rf = $("btnRefresh");
    if (rf) {
      rf.textContent = state.view === "all" ? "刷新稿件" : "刷新队列";
      rf.title = state.view === "all"
        ? "重新读取B站稿件列表，并更新顶部稿件数统计"
        : "重新读取本地投稿队列与额度状态";
    }
  }

  /* ---------------------------------------------------------- 状态胶囊 */
  // 顶部统计胶囊即状态筛选开关；再次点已激活项回到「全部」。
  function setTaskFilter(value, fromChip) {
    const v = value || "";
    state.taskFilter = (fromChip && state.taskFilter === v && v !== "") ? "" : v;
    state.page = 1;
    state.sig = "";
    // 胶囊只属于「本软件投稿」视图，若当前在稿件库则先切回来
    if (state.view === "all") studioSetView("queue", { render: false });
    renderTasks();
  }

  function syncStatChips() {
    const cur = state.taskFilter || "";
    const box = $("queueStats");
    if (box) {
      Array.from(box.querySelectorAll(".stat[data-filter]")).forEach((el) => {
        const on = (el.dataset.filter || "") === cur;
        el.classList.toggle("is-active", on);
        el.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
  }

  function filterTasks(tasks) {
    const f = state.taskFilter || "";
    if (!f) return tasks;
    return tasks.filter((t) => {
      const st = t.status || "";
      if (f === "active") {
        return ["queued", "uploading", "waiting_schedule", "blocked"].indexOf(st) >= 0;
      }
      if (f === "pending") {
        return ["queued", "waiting_schedule"].indexOf(st) >= 0;
      }
      if (f === "failed") {
        return ["failed", "blocked", "interrupted", "cancelled"].indexOf(st) >= 0;
      }
      return st === f;
    });
  }

  function renderTasks(counts) {
    const list = $("queueList");
    syncToolbarForView();
    syncOverview();
    if (state.view === "all") return renderArchives();
    const tasks = filterTasks(state.tasks || []);
    const filtering = !!state.taskFilter;
    if (counts) state.counts = counts;
    const cs = state.counts || {};
    // 胶囊显示全量统计，不随筛选变化（点胶囊才是切换筛选）
    $("statTotal").textContent = (state.tasks || []).length;
    $("statPending").textContent = (cs.queued || 0) + (cs.waiting_schedule || 0);
    $("statRunning").textContent = cs.uploading || 0;
    $("statSuccess").textContent = cs.success || 0;
    $("statFailed").textContent = (cs.failed || 0) + (cs.blocked || 0) +
      (cs.interrupted || 0) + (cs.cancelled || 0);
    $("btnPauseQueue").textContent = state.paused ? "继续队列" : "暂停队列";
    syncStatChips();
    syncOverview();

    // 每页 10 条，翻页后只渲染当前页
    const pages = Math.max(1, Math.ceil(tasks.length / PAGE_SIZE));
    if (state.page > pages) state.page = pages;
    if (state.page < 1) state.page = 1;
    const from = (state.page - 1) * PAGE_SIZE;
    const pageTasks = tasks.slice(from, from + PAGE_SIZE);

    // 内容没变就不重绘，避免每 2 秒把人正在看的东西重建一遍
    // 等待定时的任务忽略 message（倒计时文案由前端本地算），否则后端每分钟
    // 改一次文案就会让整列任务卡重建一遍
    const sig = state.page + "#" + state.taskFilter + "#" +
      tasks.map((t) => [t.id, t.status, Math.round((t.progress || 0) * 1000),
        t.stage, t.status === "waiting_schedule" ? "" : t.message,
        t.bvid, t.publish_at, t.cover_rev || 0].join("~")).join("|");
    if (sig === state.sig) return;
    state.sig = sig;

    if (!tasks.length) {
      list.innerHTML =
        '<div class="empty">' +
          '<div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 7h16M4 12h16M4 17h10"/></svg></div>' +
          '<div class="t">' + (filtering ? "没有匹配的任务" : "队列是空的") + '</div>' +
          '<div class="s">' + (filtering ? "点「全部」或换个状态看看" : "在左侧添加视频，填好标题和分区就能排进来") + '</div>' +
        "</div>";
    } else {
      list.innerHTML = pageTasks.map(renderTask).join("");
    }
    renderPager(tasks.length, pages);
  }

  function renderPager(total, pages) {
    const box = $("queuePager");
    if (!box) return;
    if (total <= PAGE_SIZE) {
      box.innerHTML = total ? '<span class="pg-info">共 ' + total + ' 条</span>' : "";
      return;
    }
    const start = (state.page - 1) * PAGE_SIZE + 1;
    const end = Math.min(total, state.page * PAGE_SIZE);
    box.innerHTML =
      '<span class="pg-info">' + start + "–" + end + " / 共 " + total + " 条</span>" +
      '<div class="pg-btns">' +
        '<button class="pg-btn" data-page="prev"' + (state.page <= 1 ? " disabled" : "") + ">上一页</button>" +
        '<span class="pg-num">' + state.page + " / " + pages + "</span>" +
        '<button class="pg-btn" data-page="next"' + (state.page >= pages ? " disabled" : "") + ">下一页</button>" +
        '<input class="pg-jump" id="queueJump" type="number" min="1" max="' + pages + '" placeholder="页码">' +
      "</div>";
  }


  /* ---------------------------------------------------------- 账号全部稿件 */
  const ARCH_PAGE = 10;
  const ARCH_STATUS_TEXT = { pubed: "审核通过", is_pubing: "审核中", not_pubed: "未通过" };
  const ARCH_STATE_CLASS = (it) => {
    const l = it.state_label || "";
    if (/仅自己/.test(l)) return "self";
    if (it.state === 0 || /开放/.test(l)) return "ok";
    if (/审核|待审/.test(l)) return "warn";
    return "err";
  };

  async function loadArchives(page) {
    state.archPage = Math.max(1, page || state.archPage || 1);
    try {
      const st = state.archStatus || "";
      const r = await api("/api/archives?page=" + state.archPage + "&ps=" + ARCH_PAGE +
        (st ? "&status=" + encodeURIComponent(st) : ""));
      state.archives = r.items || [];
      state.archHasMore = !!r.has_more;
      state.archTotalPages = r.total_pages || 0;
      state.archTotal = r.total || 0;
      renderArchives(true);
      syncArchFilters();
      if (r.ok === false) toast(r.error || "读取稿件失败", "err");
    } catch (e) {
      toast("读取B站稿件失败：" + e.message, "err");
    }
  }

  function setArchStatus(status) {
    const v = status || "";
    if (v && !ARCH_STATUS_TEXT[v]) return;
    state.archStatus = state.archStatus === v ? "" : v;   // 再点一次当前项 = 回到全部
    state.archPage = 1;
    state.archSig = "";
    if (document.body.dataset.view !== "archive") studioSetView("archive", { render: false });
    loadArchives(1);
  }

  function syncArchFilters() {
    const box = $("archFilters");
    if (!box) return;
    const cur = state.archStatus || "";
    Array.from(box.querySelectorAll(".arch-chip[data-arch-status]")).forEach((b) => {
      b.classList.toggle("on", (b.dataset.archStatus || "") === cur);
      b.setAttribute("aria-pressed", (b.dataset.archStatus || "") === cur ? "true" : "false");
    });
  }

  function renderArchives(force) {
    const list = $("queueList");
    const items = state.archives || [];
    const stText = ARCH_STATUS_TEXT[state.archStatus || ""] || "";
    const sig = state.archPage + "#" + (state.archStatus || "") + "#" +
      items.map((x) => [x.bvid, x.state, x.title, x.from_app].join("~")).join("|");
    if (!force && sig === state.archSig) return;
    state.archSig = sig;

    if (!items.length) {
      list.innerHTML = stText
        ? '<div class="empty"><div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 7h16M4 12h16M4 17h10"/></svg></div>' +
          '<div class="t">没有「' + stText + '」的稿件</div><div class="s">点上面「全部」看所有稿件</div></div>'
        : '<div class="empty"><div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 7h16M4 12h16M4 17h10"/></svg></div>' +
          '<div class="t">没有取到稿件</div><div class="s">确认账号已登录，或到设置里重新粘贴 Cookie</div></div>';
    } else {
      list.innerHTML = items.map(renderArchiveRow).join("");
    }
    const box = $("queuePager");
    const total = state.archTotalPages || (state.archPage + (state.archHasMore ? 1 : 0));
    const cur = state.archPage;
    const aStart = (cur - 1) * ARCH_PAGE + 1;
    const aEnd = state.archTotal ? Math.min(state.archTotal, cur * ARCH_PAGE) : null;
    const aRange = state.archTotal ? aStart + "–" + aEnd + " / 共 " + state.archTotal + " 条" : "第 " + cur + " 页";
    box.innerHTML =
      '<span class="pg-info">' + (stText ? stText + " · " : "") + aRange + "</span>" +
      '<div class="pg-btns">' +
        '<button class="pg-btn" data-arch="prev"' + (cur <= 1 ? " disabled" : "") + ">上一页</button>" +
        '<span class="pg-num">' + cur + " / " + total + "</span>" +
        '<button class="pg-btn" data-arch="next"' + (cur >= total ? " disabled" : "") + ">下一页</button>" +
        '<input class="pg-jump" id="archJump" type="number" min="1" max="' + total + '" placeholder="页码">' +
      "</div>";
  }

  function renderArchiveRow(it) {
    const cls = ARCH_STATE_CLASS(it);
    const badgeCls = cls === "ok" ? "success" : (cls === "self" ? "self" : (cls === "warn" ? "waiting" : "failed"));
    const when = it.created ? new Date(it.created * 1000) : null;
    const w = when ? (when.getMonth() + 1) + "/" + when.getDate() + " " +
      String(when.getHours()).padStart(2, "0") + ":" + String(when.getMinutes()).padStart(2, "0") : "";
    const stateText = it.state_label || (it.state === 0 ? "已发布" : "状态 " + it.state);
    return '<div class="task arch-row">' +
      '<div class="thumb">' + (it.cover ? '<img src="/api/proxy/cover?u=' + encodeURIComponent(it.cover) + '" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),{className:\'thumb-fallback\',textContent:\'无封面\'}))">' : '<span class="thumb-fallback">无封面</span>') +
        (it.duration ? '<span class="dur">' + esc(it.duration) + "</span>" : "") + "</div>" +
      '<div class="task-main">' +
        '<div class="task-top">' +
          '<div class="task-title" title="' + esc(it.title) + '">' + esc(it.title) + "</div>" +
          '<span class="badge ' + badgeCls + '">' + esc(stateText) + "</span>" +
        "</div>" +
        '<div class="meta-line">' +
          "<span>" + (it.from_app ? "本软件投稿" : "B站稿件") + "</span>" +
          (w ? "<span>" + w + "</span>" : "") +
          (it.bvid ? '<span><a class="bvid" href="https://www.bilibili.com/video/' + esc(it.bvid) + '" target="_blank" rel="noopener">' + esc(it.bvid) + "</a></span>" : "") +
          (it.is_only_self && !/仅自己/.test(it.state_label || "") ? '<span class="tag-self">仅自己可见</span>' : "") +
        "</div>" +
        '<div class="task-actions">' +
          (it.aid ? '<button class="ghost-btn" data-edit="' + it.aid + '">编辑</button>' : "") +
          (it.bvid ? '<a class="ghost-btn" href="https://www.bilibili.com/video/' + esc(it.bvid) + '" target="_blank" rel="noopener">在B站打开</a>' : "") +
        "</div>" +
      "</div>" +
    "</div>";
  }

  /* ---------------------------------------------------------- 稿件编辑 */
  function editStatus(text, kind) {
    const el = $("editStatus");
    if (!el) return;
    el.textContent = text || "";
    el.className = "edit-status" + (kind ? " " + kind : "");
  }

  function fillEditTid(tid) {
    const sel = $("editTid");
    sel.innerHTML = state.partitions.map((p) =>
      '<option value="' + p.tid + '">' + esc(p.name) + "</option>").join("");
    if (tid) sel.value = String(tid);
  }

  async function openEdit(aid) {
    editStatus("读取稿件信息…");
    $("editMask").classList.add("open");
    $("editModal").classList.add("open");
    $("editModal").setAttribute("aria-hidden", "false");
    try {
      const r = await api("/api/archive/" + aid);
      if (!r.ok) throw new Error(r.error || "读取失败");
      const a = r.archive;
      state.edit = { aid, detail: a };
      $("editBvid").textContent = a.bvid || ("aid " + aid);
      $("editTitle").value = a.title || "";
      $("editDesc").value = a.desc || "";
      $("editTags").value = (a.tag || "").split(",").filter(Boolean).join(", ");
      $("editOnlySelf").value = a.is_only_self ? "1" : "0";
      fillEditTid(a.tid);
      $("editCoverImg").src = a.cover ? "/api/proxy/cover?u=" + encodeURIComponent(a.cover) : "";
      $("editPartInfo").textContent = "共 " + (a.videos || []).length + " 个分P";
      $("btnReplaceVideo").disabled = (a.videos || []).length > 1;
      editStatus("");
    } catch (e) {
      editStatus("读取失败：" + e.message, "err");
    }
  }

  function closeEdit() {
    $("editMask").classList.remove("open");
    $("editModal").classList.remove("open");
    $("editModal").setAttribute("aria-hidden", "true");
    state.edit = null;
  }

  async function saveEdit() {
    if (!state.edit) return;
    const tags = $("editTags").value.split(/[,，]/).map((x) => x.trim()).filter(Boolean).slice(0, 10);
    const body = {
      title: $("editTitle").value.trim(),
      desc: $("editDesc").value,
      tag: tags,
      tid: Number($("editTid").value) || 0,
      is_only_self: $("editOnlySelf").value === "1" ? 1 : 0,
    };
    editStatus("保存中…");
    $("btnSaveEdit").disabled = true;
    try {
      const r = await api("/api/archive/" + state.edit.aid + "/edit", { method: "POST", body });
      if (!r.ok) throw new Error(r.error || "保存失败");
      editStatus("已保存", "ok");
      toast("稿件已更新", "ok");
      setTimeout(() => { closeEdit(); refreshAfterEdit(); }, 700);
    } catch (e) {
      editStatus("保存失败：" + e.message, "err");
    } finally {
      $("btnSaveEdit").disabled = false;
    }
  }

  function refreshAfterEdit() {
    if (state.view === "all") loadArchives(state.archPage);
    else pollTasks();
  }

  function uploadEditFile(path, field, file, extra, okText) {
    const fd = new FormData();
    fd.append(field, file);
    Object.keys(extra || {}).forEach((k) => fd.append(k, extra[k]));
    editStatus("上传中…");
    return fetch(new URL(path, location.origin).href, { method: "POST", body: fd })
      .then(async (res) => {
        const data = await res.json().catch(() => ({ ok: false, error: "响应不是 JSON" }));
        if (!res.ok || data.ok === false) throw new Error(data.error || ("HTTP " + res.status));
        editStatus(okText, "ok");
        return data;
      });
  }

  function pickEditVideo(mode) {
    if (!state.edit) return;
    const input = $("editVideoFile");
    input.dataset.mode = mode;
    input.value = "";
    input.click();
  }

  function renderTask(t) {
    const b = $badge[t.status] || [t.status, "queued"];
    const files = t.files || [];
    const pct = Math.round((Number(t.progress) || 0) * 100);
    const isRunning = t.status === "uploading";
    const active = ["queued", "uploading", "waiting_schedule", "blocked"].indexOf(t.status) >= 0;
    const thumb = t.has_cover
      ? '<div class="thumb" style="background-image:url(\'/api/tasks/' + t.id + "/cover?v=" + (t.cover_rev || 0) + '\')"></div>'
      : '<div class="thumb"><span class="ph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9l5 3-5 3z"/></svg></span></div>';

    const msgClass = t.status === "success" ? "ok" : (t.error ? "error" : "");
    let msg = t.message || t.error || "";
    if (t.status === "success") {
      msg = msg.replace(/^投稿成功[，,]?\s*(BV\s*号\s*)?[A-Za-z0-9]*[，,]?\s*/, "").trim();
      if (!msg) msg = "";
    }
    if (t.status === "waiting_schedule") msg = t.error || "";

    // stage 本身通常已含「上传 63% · 8.4 MB/s」，别再把百分比和速度拼一遍
    const stageText = t.stage || "上传中";
    const hasPct = /\d+\s*%/.test(stageText);
    const hasSpeed = /MB\/s/i.test(stageText);
    const runMsg = stageText + (hasPct ? "" : " · " + pct + "%") +
      (Number(t.speed) > 0 && !hasSpeed ? " · " + Number(t.speed).toFixed(1) + " MB/s" : "");

    return (
      '<div class="task" data-id="' + t.id + '">' +
        thumb +
        '<div class="task-main">' +
          '<div class="task-top">' +
            '<div class="task-title">' + esc(t.title) + "</div>" +
            '<span class="badge ' + b[1] + '">' + b[0] + "</span>" +
          "</div>" +
          '<div class="meta-line">' +
            '<span class="tag-acc" title="投稿账号" style="' + accChipStyle(t.account_id) + '">@' + esc(accountName(t.account_id)) + "</span>" +
            "<span>" + esc(partName(t.tid)) + "</span>" +
            "<span>" + files.length + (files.length > 1 ? " 个分P" : " 个文件") + "</span>" +
            (t.publish_at ? "<span>" + (t.schedule_mode === "local" ? "本地定时 " : "B站定时 ")
                          + fmtTime(t.publish_at) + "</span>" : "") +
            (t.status === "waiting_schedule" && t.publish_at
              ? '<span class="task-countdown" data-at="' + Number(t.publish_at) + '">'
                + esc(countdownText(t.publish_at)) + "</span>" : "") +
            (t.bvid ? '<span style="font-family:var(--mono)">' + esc(t.bvid) + "</span>" : "") +
            (t.is_only_self ? '<span class="tag-self">仅自己可见</span>' : "") +
            (t.attempts > 1 ? "<span>第 " + t.attempts + " 次尝试</span>" : "") +
          "</div>" +
          (isRunning
            ? '<div class="progress pulse"><i style="width:' + Math.max(3, pct) + '%"></i></div>' +
              '<div class="task-msg">' + esc(runMsg) + "</div>"
            : (msg ? '<div class="task-msg ' + msgClass + '">' + esc(msg) + "</div>" : "")) +
          '<div class="task-actions">' +
            '<button class="ghost-btn" data-act="logs" data-id="' + t.id + '">' + (state.expanded[t.id] ? "收起日志" : "日志") + "</button>" +
            (active ? '<button class="ghost-btn" data-act="cancel" data-id="' + t.id + '">取消</button>' : "") +
            (t.status === "waiting_schedule"
              ? '<button class="ghost-btn" data-act="upload_now" data-id="' + t.id + '">提前上传</button>' : "") +
            (["failed", "blocked", "interrupted"].indexOf(t.status) >= 0
              ? '<button class="ghost-btn" data-act="retry" data-id="' + t.id + '">' +
                (t.status === "interrupted" ? "核对后重投" : "重新排队") + "</button>" : "") +
            '<button class="ghost-btn danger" data-act="delete" data-id="' + t.id + '">删除</button>' +
          "</div>" +
          '<div class="logbox' + (state.expanded[t.id] ? " show" : "") + '" id="log-' + t.id + '">' + renderLogs(t.id) + "</div>" +
        "</div>" +
      "</div>"
    );
  }

  // 每个账号固定一个颜色，任务卡/账号行/主界面芯片都用同一套配色
  const ACC_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899", "#06b6d4", "#ef4444", "#84cc16"];

  function accountColor(accountId) {
    const idx = (state.accounts || []).findIndex(function (x) { return x.id === accountId; });
    if (idx < 0) return "#8b93a7";
    return ACC_COLORS[idx % ACC_COLORS.length];
  }

  function hexA(hex, alpha) {
    const h = hex.replace("#", "");
    const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  function accChipStyle(accountId) {
    const c = accountColor(accountId);
    return "color:" + c + ";background:" + hexA(c, 0.12) + ";border-color:" + hexA(c, 0.45) + ";";
  }

  function accountName(accountId) {
    if (!accountId) return "历史任务";
    const a = (state.accounts || []).find(function (x) { return x.id === accountId; });
    return a ? (a.uname || "账号") : "已删除账号";
  }

  function renderLogs(id) {
    const logs = state.logs[id];
    if (!logs) return '<div class="lg"><span class="ts">加载中…</span></div>';
    if (!logs.length) return '<div class="lg"><span class="ts">暂无日志</span></div>';
    return logs.map((l) => {
      const d = new Date(l.ts * 1000);
      const p = (n) => String(n).padStart(2, "0");
      return '<div class="lg"><span class="ts">' + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + "</span>" +
        '<span class="lv-' + esc(l.level || "info") + '">' + esc(l.msg) + "</span></div>";
    }).join("");
  }

  async function toggleLogs(id) {
    if (state.expanded[id]) {
      delete state.expanded[id];
      const box = $("log-" + id);
      if (box) box.classList.remove("show");
      const btn = document.querySelector('[data-act="logs"][data-id="' + id + '"]');
      if (btn) btn.textContent = "日志";
      return;
    }
    state.expanded[id] = true;
    const btn = document.querySelector('[data-act="logs"][data-id="' + id + '"]');
    if (btn) btn.textContent = "收起日志";
    const box = $("log-" + id);
    if (box) {
      box.classList.add("show");
      box.innerHTML = '<div class="lg"><span class="ts">加载中…</span></div>';
    }
    await fetchLogs(id);
    if ($("log-" + id)) $("log-" + id).innerHTML = renderLogs(id);
  }

  async function fetchLogs(id) {
    try {
      const r = await api("/api/tasks/" + id + "/logs");
      state.logs[id] = r.logs || [];
    } catch (e) {
      state.logs[id] = [];
    }
  }

  function loadAllExpandedLogs() {
    Object.keys(state.expanded).forEach(async (id) => {
      await fetchLogs(id);
      const box = $("log-" + id);
      if (box && state.expanded[id]) box.innerHTML = renderLogs(id);
    });
  }

  async function fetchGlobalLogs() {
    try {
      const r = await api("/api/logs");
      state.glogs = r.logs || [];
      renderGlobalLogs();
    } catch (e) { /* ignore */ }
  }

  function renderGlobalLogs() {
    const box = $("globalLogBox");
    if (!box) return;
    const cnt = $("logCount");
    if (cnt) cnt.textContent = state.glogs.length + " 条";
    if (!state.glogs.length) {
      box.innerHTML = '<div class="lg"><span class="ts">暂无记录</span></div>';
      return;
    }
    const sig = state.glogs.length + ":" + (state.glogs[state.glogs.length - 1] || {}).id;
    if (sig === state.glogsSig) return;       // 内容没变化，不重绘也就不会打断滚动
    state.glogsSig = sig;
    const oldTop = box.scrollTop;
    box.innerHTML = state.glogs.slice(-150).map((l) => {
      const d = new Date(l.ts * 1000);
      const p = (n) => String(n).padStart(2, "0");
      return '<div class="lg"><span class="ts">' + p(d.getMonth() + 1) + "/" + p(d.getDate()) + " " +
        p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + "</span>" +
        '<span class="lv-' + esc(l.level || "info") + '">' + esc(l.msg) + "</span></div>";
    }).join("");
    // 日志独立成页后默认跟随最新；取消勾选则保持用户当前阅读位置
    const auto = !$("logAutoFollow") || $("logAutoFollow").checked;
    box.scrollTop = auto ? box.scrollHeight : oldTop;
  }

  async function taskAction(id, action) {
    if (action === "delete") {
      if (!confirm("删除这个任务？已上传的本地文件也会清掉。")) return;
    }
    if (action === "retry") {
      // 中断的任务有可能已经投出去了，重投就是重复稿件，必须让人先确认一次
      const t = (state.tasks || []).find((x) => x.id === id);
      if (t && t.status === "interrupted" &&
          !confirm("这个任务是在上传中途中断的，稿件有可能已经发出去了。\n\n请先到B站创作中心确认没有重复稿件，再重新排队。")) return;
    }
    try {
      if (action === "retry" || action === "cancel" || action === "delete" || action === "upload_now") {
        await api("/api/tasks/" + id + "/action", { method: "POST", body: { action } });
        toast(action === "cancel" ? "已取消"
          : action === "retry" ? "已重新排队"
          : action === "upload_now" ? "已提前上传，立即投稿" : "已删除", "ok");
        if (action === "delete") {
          state.tasks = state.tasks.filter((t) => t.id !== id);
          delete state.expanded[id];
        }
        await pollTasks();
      }
    } catch (e) {
      toast(e.message, "err");
    }
  }

  /* ---------------------------------------------------------- 模板 */
  function renderTemplates() {
    const sel = $("tplSelect");
    const cur = sel.value;
    sel.innerHTML = '<option value="">（无）</option>' +
      state.templates.map((t) => '<option value="' + t.id + '">' + esc(t.name) + "</option>").join("");
    if (cur) sel.value = cur;

    const list = $("tplList");
    if (!state.templates.length) {
      list.innerHTML = '<div class="tpl-empty">还没有模板。在「工作台」填好一次标题 / 标签 / 简介后，点「存为模板」就能在这里复用。</div>';
      return;
    }
    list.innerHTML = state.templates.map((t) => {
      const editing = state.editTpl && String(state.editTpl.id) === String(t.id);
      return '<div class="tpl-row' + (editing ? " editing" : "") + '">' +
        '<span class="n">' + esc(t.name) + "</span>" +
        '<span class="tags">' + esc(((t.payload && t.payload.tags) || []).join(" / ") || "无标签") + "</span>" +
        '<button class="ghost-btn" data-edit-tpl="' + t.id + '">编辑</button>' +
        '<button class="ghost-btn" data-use-tpl="' + t.id + '">套用</button>' +
        '<button class="ghost-btn danger" data-del-tpl="' + t.id + '">删</button>' +
      "</div>";
    }).join("");
  }

  async function applyTemplate(id, opts) {
    opts = opts || {};
    const t = state.templates.find((x) => String(x.id) === String(id));
    if (!t) return;
    const p = t.payload || {};
    if (p.title) $("titleInput").value = p.title;
    if (p.desc != null) $("descInput").value = p.desc;
    if (Array.isArray(p.tags)) { state.tags = p.tags.slice(0, 10); renderTags(); }
    if (p.tid) $("tidSelect").value = String(p.tid);
    if (p.copyright) $("copyrightSelect").value = String(p.copyright);
    syncCopyright();
    if (p.channel && $("channelSelect")) $("channelSelect").value = p.channel;
    // 编辑态只是把参数载进来改，别顺手在账号下建合集
    if (p.season_id != null && $("seasonSelect")) {
      await applyTemplateSeason(p, { allowCreate: !opts.forEdit });
    }
    if ($("noPush")) $("noPush").checked = !!p.no_disturbance;
    if ($("onlySelf")) $("onlySelf").checked = !!p.is_only_self;
    if (p.topic_id) setTopic({ id: p.topic_id, name: p.topic_name || "", mission_id: p.mission_id });
    updateCounters();
    if (opts.forEdit) {
      setTplEdit(t);
      toast("已载入模板「" + t.name + "」，改完点「保存修改」覆盖原模板", "info", 3800);
      return;
    }
    if (state.editTpl) setTplEdit(null);
    toast("已套用模板：" + t.name, "ok");
  }

  /* 模板编辑态：工作台顶部显示提示条、保存按钮改成「保存修改」、模板卡片高亮 */
  function setTplEdit(t) {
    state.editTpl = t ? { id: t.id, name: t.name } : null;
    const wrap = $("tplEditWrap");
    if (wrap) wrap.hidden = !state.editTpl;
    if (state.editTpl) {
      // 提示条在表单顶部，正常在视口内；万一被滚走了，编辑时滚回来
      // （顺便展开「更多设置」，因为模板的合集/模板控件在那一块）
      const adv = $("advanced"), advBtn = $("advToggle");
      if (adv && adv.hidden) {
        adv.hidden = false;
        state.advOpen = true;
        if (advBtn) {
          advBtn.classList.add("open");
          advBtn.setAttribute("aria-expanded", "true");
        }
      }
      if ($("tplEditTip")) {
        $("tplEditTip").textContent = "正在编辑模板「" + state.editTpl.name +
          "」：改完点「保存修改」覆盖原模板，或者点右侧取消";
      }
      if ($("btnSaveTpl")) $("btnSaveTpl").textContent = "保存修改";
      if ($("tplSelect")) $("tplSelect").value = String(state.editTpl.id);
      const bar = $("tplEditBar");
      if (bar && bar.scrollIntoView) {
        try { bar.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) { /* 老浏览器忽略参数 */ }
      }
    } else if ($("btnSaveTpl")) {
      $("btnSaveTpl").textContent = "存为模板";
    }
    renderTemplates();
  }

  /* 当前表单 → 模板 payload。新建与覆盖保存共用，避免两处字段慢慢跑偏 */
  function collectTemplatePayload() {
    return {
      title: $("titleInput").value,
      desc: $("descInput").value,
      tags: state.tags,
      tid: Number($("tidSelect").value),
      copyright: Number($("copyrightSelect").value),
      channel: "",
      // 合集除了 ID 还要存名字：换账号套用时靠它找同名合集
      season_id: tplSeasonId(),
      season_title: tplSeasonTitle(),
      no_disturbance: $("noPush") && $("noPush").checked ? 1 : 0,
      is_only_self: $("onlySelf") && $("onlySelf").checked ? 1 : 0,
      topic_id: state.topic.id || 0,
      topic_name: state.topic.name || "",
      mission_id: state.topic.mission || 0,
    };
  }

  /* 存模板时一起记下合集 ID 与名字：ID 用于同账号直接命中，名字用于换号找同名 */
  function tplSeasonId() {
    const sel = $("seasonSelect");
    const n = sel ? Number(sel.value) : 0;
    return Number.isFinite(n) ? n : 0;
  }

  function tplSeasonTitle() {
    const sel = $("seasonSelect");
    if (!sel) return "";
    const id = tplSeasonId();
    if (!id) return "";
    const hit = (state.seasons || []).find((s) => s.id === id);
    if (hit) return (hit.title || "").trim();
    // 刚自动建、列表里还没有的合集，从占位项文本里取名字
    const opt = sel.options[sel.selectedIndex];
    return opt ? String(opt.textContent || "").replace(/（审核中）$/, "").trim() : "";
  }

  /* 模板里的合集属于某个账号，换号套用时不能直接照搬 ID：
     ID 命中就用 ID（同账号零变化），否则按模板快照的合集名在本账号里找同名，
     都没有就在本账号下自动新建一个同名合集并选中。 */
  async function applyTemplateSeason(p, opts) {
    opts = opts || {};
    const allowCreate = opts.allowCreate !== false;
    const sel = $("seasonSelect");
    if (!sel) return;
    if (!state.seasonsLoaded) await loadSeasons(true);   // 列表没就绪时先拉，否则名字匹配一定落空
    const sid = String(p.season_id || 0);
    if (Array.from(sel.options).some((o) => o.value === sid)) {
      sel.value = sid;
      return;
    }
    const want = String(p.season_title || "").trim();
    const hit = want ? (state.seasons || []).find((s) => (s.title || "").trim() === want) : null;
    if (hit) {
      sel.value = String(hit.id);
      toast("合集已按同名匹配到本账号：" + hit.title, "ok", 3000);
      return;
    }
    if (!want) {
      // 老模板没存合集名，无从匹配：退回「不加入合集」，别把别的账号的 ID 带进去
      sel.value = "0";
      toast("模板绑定的合集属于其他账号，本账号无法匹配，已改为不加入合集", "warn", 4600);
      return;
    }
    if (!allowCreate) {
      // 编辑模板时不建合集，只提示需要重新选一个
      sel.value = "0";
      toast("模板里的合集「" + want + "」在本账号下不存在，保存前请重新选一个合集", "warn", 5000);
      return;
    }
    await createSeasonForTemplate(want);
  }

  async function createSeasonForTemplate(title) {
    const sel = $("seasonSelect");
    toast("本账号没有「" + title + "」，正在自动新建合集…", "info", 4200);
    try {
      const r = await api("/api/seasons", { method: "POST", body: { title: title } });
      const id = String((r.item && r.item.id) || "");
      await loadSeasons(true);
      if (id && Array.from(sel.options).some((o) => o.value === id)) {
        sel.value = id;
        toast("已新建合集「" + title + "」并选中", "ok", 4000);
        return;
      }
      if (id) {
        // 新建合集要过B站侧审核，列表里可能暂时没有：先插一个占位项，别让选择丢失
        sel.add(new Option(title + "（审核中）", id, true, true));
        sel.value = id;
        toast("已新建合集「" + title + "」，需等B站审核通过后才会出现在合集列表；"
              + "审核前投稿不会进合集，请审核后再投", "warn", 5600);
        return;
      }
      sel.value = "0";
      toast("新建合集没有返回 ID，请到设置里手动确认", "err", 4600);
    } catch (e) {
      sel.value = "0";
      toast("自动新建合集失败：" + e.message, "err", 5000);
    }
  }

  /* ---------------------------------------------------------- 设置 */
  function renderEnv() {
    const env = state.env || {};
    const b = env.biliup || {};
    if (!b.available) {
      $("envBiliup").innerHTML = '<span class="pill no">未安装</span>';
    } else if (!b.logged_in) {
      $("envBiliup").innerHTML = '<span class="pill no">未登录</span> 需先运行 biliup login';
    } else {
      $("envBiliup").innerHTML = '<span class="pill ok">已登录</span> ' + esc(b.version || "");
    }
    $("envOfficial").innerHTML = env.official ? '<span class="pill ok">可用</span>' : '<span class="pill no">缺少依赖</span>';
    $("envFfmpeg").innerHTML = env.ffmpeg ? '<span class="pill ok">可用</span>' : '<span class="pill no">未安装</span>';
  }

  function syncNotifyRows() {
    if ($("notifyChatRow")) $("notifyChatRow").hidden = $("notifyKind").value !== "telegram";
  }

  function syncAuthRows() {
    const basic = $("webAuthMode") && $("webAuthMode").value === "basic";
    if ($("webAuthTokenRow")) $("webAuthTokenRow").hidden = !!basic;
    if ($("webAuthBasicRow")) $("webAuthBasicRow").hidden = !basic;
  }

  function renderSettings() {
    const c = state.config || {};
    const cookie = c.cookie || {};
    $("quotaLimit").value = c.quota_limit || 50;
    $("maxAttempts").value = c.max_attempts == null ? 2 : c.max_attempts;
    if ($("quotaStrict")) $("quotaStrict").checked = c.quota_strict !== false;
    if ($("autoResume")) $("autoResume").checked = c.auto_resume !== false;
    if ($("overdueGuard")) $("overdueGuard").checked = !!c.overdue_guard;
    if ($("overdueMinutes")) $("overdueMinutes").value = c.overdue_minutes || 30;
    if ($("maxFileMb")) $("maxFileMb").value = c.max_file_mb || 8192;
    if ($("maxParts")) $("maxParts").value = c.max_parts || 20;
    if ($("deleteAfter")) $("deleteAfter").value = c.delete_after || "off";
    $("cfgChannel").value = c.channel || "auto";
    if ($("cfgUploadLine")) $("cfgUploadLine").value = c.upload_line || "bda2";
    $("cfgBiliupBin").value = c.biliup_bin || "biliup";
    $("cfgBiliupLine").value = c.biliup_line || "";
    $("cfgBiliupCookie").value = c.biliup_cookie_file || "cookies.json";
    $("quotaWindow").textContent = "近 " + (c.quota_hours || 24) + " 小时";
    // v103：状态通知（Webhook）
    const nt = c.notify || {};
    if ($("notifyUrl")) $("notifyUrl").value = nt.url || "";
    if ($("notifyKind")) $("notifyKind").value = nt.kind || "auto";
    if ($("notifyChatId")) $("notifyChatId").value = nt.chat_id || "";
    if ($("notifyEnabled")) $("notifyEnabled").checked = nt.enabled !== false;
    const ev = nt.events || {};
    if ($("notifyEvTask")) $("notifyEvTask").checked = ev.task !== false;
    if ($("notifyEvRate")) $("notifyEvRate").checked = ev.rate !== false;
    if ($("notifyEvQuota")) $("notifyEvQuota").checked = ev.quota !== false;
    if ($("notifyEvResume")) $("notifyEvResume").checked = ev.resume !== false;
    syncNotifyRows();
    // v103：Web 访问鉴权（token / 密码回显为掩码，保存时后端会忽略掩码）
    const wa = c.web_auth || {};
    if ($("webAuthEnabled")) $("webAuthEnabled").checked = !!wa.enabled;
    if ($("webAuthMode")) $("webAuthMode").value = wa.mode || "token";
    if ($("webAuthUser")) $("webAuthUser").value = wa.username || "";
    if ($("webAuthToken")) $("webAuthToken").value = wa.token || "";
    if ($("webAuthPass")) $("webAuthPass").value = wa.password || "";
    syncAuthRows();
    $("drawerAccount").textContent = state.account && state.account.logged_in
      ? state.account.uname + "（UID " + state.account.mid + "）" : "未登录";
    $("drawerCookieState").textContent = cookie.set ? "已设置（SESSDATA " + (cookie.sessdata || "") + "）" : "未设置";
    renderAccounts();
  }

  /* ---------------------------------------------------------- 多账号 */
  function renderAccounts() {
    const box = $("accList");
    if (box) {
      const items = state.accounts || [];
      if (!items.length) {
        box.innerHTML = '<div class="hint">还没有账号，先扫码登录一个</div>';
      } else {
        box.innerHTML = items.map(function (a) {
          // 头像兜底用昵称里第一个非数字字符，数字 ID 作首字母辨识度太低
          const initial = esc(((a.uname || "?").replace(/[0-9]/g, "") || a.uname || "?").slice(0, 1));
          const av = a.face
            ? '<img src="/api/proxy/cover?u=' + encodeURIComponent(a.face) + '" alt="" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline-flex\'">' +
              '<span class="avatar-fallback" style="display:none">' + initial + "</span>"
            : '<span class="avatar-fallback">' + initial + "</span>";
          const ops = a.active ? '<span class="pill ok">当前</span>'
                               : '<span class="acc-hint">点击切换</span>';
          const del = items.length > 1
            ? '<button class="link-btn danger" data-remove="' + esc(a.id) + '">删除</button>' : "";
          // 整行可点：不用去点右边的小按钮，点行内任意位置即切换
          return '<div class="acc-row' + (a.active ? " active" : " switchable") + '" style="--acc-c:' + accountColor(a.id) + '"' +
            (a.active ? "" : ' data-switch="' + esc(a.id) + '" role="button" tabindex="0"') + '>' +
            '<div class="acc-main">' + av + '<div class="acc-meta"><b>' + esc(a.uname || "未命名账号") +
            '</b><span>UID ' + (a.mid || "—") + '</span></div></div>' +
            '<div class="acc-ops">' + ops + del + "</div></div>";
        }).join("");
        box.querySelectorAll("[data-switch]").forEach(function (row) {
          const go = function () { switchAccount(row.getAttribute("data-switch")); };
          row.addEventListener("click", go);
          row.addEventListener("keydown", function (e) {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
          });
        });
        box.querySelectorAll("[data-remove]").forEach(function (b) {
          b.addEventListener("click", function (e) {
            e.stopPropagation();   // 点删除别误触发整行切换
            removeAccount(b.getAttribute("data-remove"));
          });
        });
      }
    }
    const pub = $("pubAccount");
    if (pub) {
      const a = state.account || {};
      if (a.logged_in) {
        const ini = esc(((a.uname || "?").replace(/[0-9]/g, "") || a.uname || "?").slice(0, 1));
        const av = a.face
          ? '<img src="/api/proxy/cover?u=' + encodeURIComponent(a.face) + '" alt="" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline-flex\'">' +
            '<span class="avatar-fallback" style="display:none">' + ini + "</span>"
          : '<span class="avatar-fallback">' + ini + "</span>";
        pub.innerHTML = '<span class="pa-label">投稿账号</span>' + av +
          '<b class="pa-name">' + esc(a.uname || "已登录") + "</b>" +
          '<span class="pa-uid">UID ' + esc(String(a.mid || "")) + "</span>" +
          '<span class="pa-swap">切换</span>';
        pub.className = "pub-account chip";
        pub.setAttribute("style", accChipStyle(a.id || state.activeAccount));
        pub.title = "点击切换投稿账号";
        pub.onclick = openDrawer;
      } else {
        pub.textContent = "未登录，先点这里扫码登录";
        pub.className = "pub-account off";
        pub.title = "";
        pub.onclick = openDrawer;
      }
    }
  }

  async function switchAccount(id) {
    try {
      const r = await api("/api/accounts/switch", { method: "POST", body: { id: id } });
      state.account = r.account;
      state.accounts = r.accounts || [];
      if (r.quota) state.quota = r.quota;
      renderAccount(); renderAccounts(); renderQuota();
      // 概览数字属于账号：立刻作废并重取，避免还把上一个号的数字留在页面上
      refreshOverview("switch");
      // 合集属于账号：切换后必须换成本账号的合集列表
      state.seasonsLoaded = false; state.seasonsError = "";
      await loadSeasons(true);
      toast("已切换到：" + ((r.account && r.account.uname) || id), "ok");
    } catch (e) { toast("切换失败：" + e.message, "err"); }
  }

  async function removeAccount(id) {
    if (!confirm("确定删除这个账号？只删本地 cookie，不影响B站账号本身。")) return;
    try {
      const r = await api("/api/accounts/remove", { method: "POST", body: { id: id } });
      state.accounts = r.accounts || [];
      if (r.account) state.account = r.account;
      renderAccount(); renderAccounts();
      refreshOverview("remove");
      state.seasonsLoaded = false; state.seasonsError = "";
      await loadSeasons(true);
      toast("账号已删除", "ok");
    } catch (e) { toast("删除失败：" + e.message, "err"); }
  }

  function openDrawer() {
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    $("drawerMask").classList.add("open");
    document.body.classList.add("drawer-open");
    // 点击账号 / 设置时总是回到顶部，不残留上次的滚动位置
    const body = document.querySelector(".drawer-body");
    if (body) body.scrollTop = 0;
  }
  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("drawerMask").classList.remove("open");
    document.body.classList.remove("drawer-open");
    stopQrLogin();
  }

  // ---- 扫码登录 ----
  let qrTimer = null;
  let qrKey = "";

  function stopQrLogin() {
    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
  }

  async function startQrLogin() {
    stopQrLogin();
    qrKey = "";
    $("qrBox").hidden = false;
    $("qrImg").innerHTML = "";
    $("qrTip").className = "qr-tip";
    $("qrTip").textContent = "正在生成二维码…";
    try {
      const r = await api("/api/login/qr", { method: "POST", body: {} });
      if (!r.ok) throw new Error(r.error || "生成失败");
      qrKey = r.key;
      const pngUrl = location.origin + "/api/login/qr/image?key=" + encodeURIComponent(r.key);
      $("qrImg").innerHTML = '<img src="' + pngUrl + '" alt="B站登录二维码">';
      $("btnQrSave").href = pngUrl;
      $("qrTip").textContent = "等待扫码（同一部手机请用「相册」识图）";
      qrTimer = setInterval(pollQr, 1800);
    } catch (e) {
      $("qrTip").className = "qr-tip err";
      $("qrTip").textContent = e.message || "二维码生成失败";
    }
  }

  async function pollQr() {
    if (!qrKey) return;
    try {
      const r = await api("/api/login/qr/" + encodeURIComponent(qrKey));
      if (r.state === "waiting") {
        $("qrTip").textContent = "等待扫码…";
      } else if (r.state === "scanned") {
        $("qrTip").className = "qr-tip ok";
        $("qrTip").textContent = "已扫码，请在手机上点「确认登录」";
      } else if (r.state === "expired") {
        stopQrLogin();
        $("qrTip").className = "qr-tip err";
        $("qrTip").textContent = "二维码已失效，点下面刷新";
      } else if ((r.state || "") === "ok") {
        stopQrLogin();
        $("qrTip").className = "qr-tip ok";
        $("qrTip").textContent = "登录成功";
        toast("登录成功：" + ((r.account && r.account.uname) || ""), "ok");
        const bs = await api("/api/bootstrap");
        applyBootstrap(bs);
        loadSeasons(true);
        setTimeout(() => { $("qrBox").hidden = true; }, 1200);
      } else {
        stopQrLogin();
        $("qrTip").className = "qr-tip err";
        $("qrTip").textContent = r.message || "登录失败";
      }    } catch (e) {
      /* 网络抖动就再等下一轮 */
    }
  }

  async function clearDoneTasks() {
    const done = ["success", "failed", "cancelled", "blocked", "interrupted"];
    const n = (state.tasks || []).filter((t) => done.indexOf(t.status) >= 0).length;
    if (!n) { toast("没有已结束的任务", "ok"); return; }
    if (!confirm("删除 " + n + " 个已结束的任务？任务记录、BV 号与本地视频副本都会清掉，此操作不可恢复。")) return;
    try {
      const r = await api("/api/tasks/clear_done", { method: "POST" });
      toast("已清理 " + r.removed + " 个任务", "ok");
      state.sig = "";
      state.page = 1;
      await pollTasks();
    } catch (e) {
      toast("清理失败：" + e.message, "err");
    }
  }

  async function cleanNow() {
    if (!confirm("删除所有成功任务的落盘视频副本和孤儿文件？任务记录与 BV 号保留，不影响已投稿件。")) return;
    const btn = $("btnCleanNow");
    btn.disabled = true;
    try {
      const r = await api("/api/storage/cleanup", { method: "POST" });
      toast(`已释放 ${fmtBytes(r.bytes_freed)}：任务文件 ${r.files} 个、孤儿文件 ${r.orphan_files} 个`, "ok", 5000);
      pollTasks();
    } catch (e) {
      toast("清理失败：" + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  }

  async function saveSettings() {
    try {
      await api("/api/config", {
        method: "POST",
        body: {
          quota_limit: Number($("quotaLimit").value) || 50,
          quota_strict: $("quotaStrict") ? !!$("quotaStrict").checked : true,
          auto_resume: $("autoResume") ? !!$("autoResume").checked : true,
          overdue_guard: $("overdueGuard") ? !!$("overdueGuard").checked : false,
          overdue_minutes: Number($("overdueMinutes") ? $("overdueMinutes").value : 0) || 30,
          delete_after: $("deleteAfter") ? $("deleteAfter").value : "off",
          max_attempts: Number($("maxAttempts").value) || 0,
          max_file_mb: Number($("maxFileMb") ? $("maxFileMb").value : 0) || 8192,
          max_parts: Number($("maxParts") ? $("maxParts").value : 0) || 20,
          channel: $("cfgChannel").value,
          upload_line: $("cfgUploadLine") ? $("cfgUploadLine").value : "bda2",
          biliup_bin: $("cfgBiliupBin").value.trim() || "biliup",
          biliup_line: $("cfgBiliupLine").value.trim(),
          biliup_cookie_file: $("cfgBiliupCookie").value.trim() || "cookies.json",
          notify: {
            enabled: $("notifyEnabled") ? !!$("notifyEnabled").checked : true,
            url: $("notifyUrl") ? $("notifyUrl").value.trim() : "",
            kind: $("notifyKind") ? $("notifyKind").value : "auto",
            chat_id: $("notifyChatId") ? $("notifyChatId").value.trim() : "",
            events: {
              task: $("notifyEvTask") ? !!$("notifyEvTask").checked : true,
              rate: $("notifyEvRate") ? !!$("notifyEvRate").checked : true,
              quota: $("notifyEvQuota") ? !!$("notifyEvQuota").checked : true,
              resume: $("notifyEvResume") ? !!$("notifyEvResume").checked : true,
            },
          },
          web_auth: {
            enabled: $("webAuthEnabled") ? !!$("webAuthEnabled").checked : false,
            mode: $("webAuthMode") ? $("webAuthMode").value : "token",
            username: $("webAuthUser") ? $("webAuthUser").value.trim() : "",
            token: $("webAuthToken") ? $("webAuthToken").value.trim() : "",
            password: $("webAuthPass") ? $("webAuthPass").value : "",
          },
        },
      });
      toast("设置已保存", "ok");
      const r = await api("/api/bootstrap");
      applyBootstrap(r);
    } catch (e) {
      toast(e.message, "err");
    }
  }

  async function testNotify() {
    const btn = $("btnNotifyTest");
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = "发送中…";
    try {
      const r = await api("/api/notify/test", {
        method: "POST",
        body: {
          url: $("notifyUrl") ? $("notifyUrl").value.trim() : "",
          kind: $("notifyKind") ? $("notifyKind").value : "auto",
          chat_id: $("notifyChatId") ? $("notifyChatId").value.trim() : "",
        },
      });
      toast(r.sent ? "测试通知已发出（" + (r.kind || "raw") + "）"
                   : "发送失败：" + (r.detail || "未知原因"),
            r.sent ? "ok" : "err", 5200);
    } catch (e) {
      toast("测试失败：" + e.message, "err", 5200);
    } finally {
      btn.disabled = false;
      btn.textContent = "发送测试通知";
    }
  }

  async function saveCookie() {
    const raw = $("cookieRaw").value.trim();
    if (!raw) { toast("请先粘贴 Cookie", "err"); return; }
    try {
      const r = await api("/api/cookie", { method: "POST", body: { raw } });
      state.account = r.account;
      state.quota = r.quota || state.quota;
      renderAccount();
      renderQuota();
      renderSettings();
      $("cookieRaw").value = "";
      toast("登录成功：" + (r.account.uname || ""), "ok", 3200);
      refreshOverview("login");
      loadSeasons(true);
    } catch (e) {
      toast("校验失败：" + e.message, "err", 4200);
    }
  }

  /* ---------------------------------------------------------- 发布 */
  function baseTitleFor(file, count) {
    const typed = $("titleInput").value.trim();
    const own = file.partTitle || file.file.name.replace(/\.[^.]+$/, "");
    if (!typed) return "";
    return count > 1 ? typed + " " + own : typed;
  }

  function buildFormData(files, opts) {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f.file));
    if (state.coverFile) fd.append("cover", state.coverFile);

    const list = files || state.files;
    const partTitles = {};
    list.forEach((f, i) => { partTitles[i] = f.partTitle || f.file.name; });

    const meta = {
      mode: opts && opts.mode ? opts.mode : state.mode,
      title: opts && typeof opts.title === "string" ? opts.title : $("titleInput").value.trim(),
      desc: $("descInput").value,
      tags: state.tags,
      tid: Number($("tidSelect").value) || 21,
      copyright: Number($("copyrightSelect").value) || 1,
      no_reprint: $("noReprint").checked,
      channel: "",
      season_id: Number($("seasonSelect").value) || 0,
      no_disturbance: $("noPush") && $("noPush").checked ? 1 : 0,
      is_only_self: $("onlySelf") && $("onlySelf").checked ? 1 : 0,
      topic_id: state.topic.id || 0,
      topic_name: state.topic.name || "",
      mission_id: state.topic.mission || 0,
      part_titles: partTitles,
      quota_live: !(opts && opts.quotaCached),
    };
    const pa = $("publishAt").value;
    if (pa) {
      meta.publish_at = Math.floor(new Date(pa).getTime() / 1000);
      meta.schedule_mode = $("scheduleMode").value || "local";
    }
    fd.append("meta", JSON.stringify(meta));
    return { fd: fd, meta: meta };
  }

  function uploadBlock(fd, label, onProgress) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", location.origin + "/api/publish", true);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        onProgress(Math.round((e.loaded / e.total) * 100), e.loaded, e.total);
      };
      xhr.onload = () => {
        let r = {};
        try { r = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300 && r.ok) resolve({ ok: true, r: r });
        else resolve({ ok: false, r: r, status: xhr.status });
      };
      xhr.onerror = () => resolve({ ok: false, r: {}, status: 0, net: true });
      xhr.send(fd);
    });
  }

  /* 单个文件上传成功即刻入队：云端部署时不必等全部文件传完 */
  async function submitPublish() {
    if (state.uploading) return;
    if (!state.account || !state.account.logged_in) {
      toast("请先在设置里粘贴 Cookie 登录B站账号", "err", 3600);
      openDrawer();
      return;
    }
    if (!state.files.length) { toast("先选视频文件", "err"); return; }

    const bar = $("uploadBar");
    const fill = $("uploadFill");
    const txt = $("uploadTxt");
    bar.classList.add("show");
    fill.style.width = "0";
    state.uploading = true;
    $("btnPublish").disabled = true;

    const files = state.files.slice();
    const mode = state.mode;
    const total = files.length;
    let queued = 0;
    let quotaFull = false;      // 本次提交时额度已满：入队成功但要说明会排队等待

    try {
      if (mode === "multi") {
        // 多分P是一个稿件，必须整包提交
        const built = buildFormData(files, { mode: "multi", title: $("titleInput").value.trim() });
        txt.textContent = "正在传送 " + total + " 个文件（多P合并）0%";
        const res = await uploadBlock(built.fd, "multi", (pct, loaded, all) => {
          fill.style.width = pct + "%";
          txt.textContent = "正在传送多P " + pct + "% · " + fmtBytes(loaded) + " / " + fmtBytes(all);
        });
        if (!res.ok) {
          if (res.r && res.r.quota) { state.quota = res.r.quota; renderQuota(); }
          throw new Error(res.net ? "上传中断，请重试" : (res.r.error || ("HTTP " + res.status)));
        }
        if (res.r && res.r.quota) { state.quota = res.r.quota; renderQuota(); }
        quotaFull = quotaFull || !!(res.r && res.r.quota_full);
        queued = (res.r.tasks || []).length;
      } else {
        for (let i = 0; i < total; i++) {
          const f = files[i];
          const built = buildFormData([f], {
            mode: "single",
            title: baseTitleFor(f, total),
            quotaCached: i > 0,
          });
          const label = "第 " + (i + 1) + "/" + total + " 个 · 已入队 " + queued;
          txt.textContent = label + " · 正在传送 0%";
          const res = await uploadBlock(built.fd, label, (pct, loaded, all) => {
            fill.style.width = Math.round(((i + pct / 100) / total) * 100) + "%";
            txt.textContent = label + " · " + pct + "% · " + fmtBytes(loaded) + " / " + fmtBytes(all);
          });
          if (!res.ok) {
            if (res.r && res.r.quota) { state.quota = res.r.quota; renderQuota(); }
            throw new Error("《" + f.file.name + "》" + (res.net ? "上传中断" : (res.r.error || ("HTTP " + res.status))));
          }
          if (res.r && res.r.quota) { state.quota = res.r.quota; renderQuota(); }
          quotaFull = quotaFull || !!(res.r && res.r.quota_full);
          queued += (res.r.tasks || []).length;
          fill.style.width = Math.round(((i + 1) / total) * 100) + "%";
          txt.textContent = "第 " + (i + 1) + "/" + total + " 个已入队 · 共 " + queued + " 个任务";
          await pollTasks();
        }
      }

      txt.textContent = "已加入队列：" + queued + " 个任务";
      fill.style.width = "100%";
      if (quotaFull) {
        // 人工暂停时队列不会自己动，不能谎称「自动续投」
        const manualHold = state.paused && state.pauseReason === "manual";
        const banned = Number(state.quotaBanUntil || 0) > Date.now() / 1000;
        const why = banned
          ? "当前处于 24h 投稿封禁，需等额度归零后自动解封"
          : "当前 24h 额度已满，稿件排队等候";
        toast("已加入队列 " + queued + " 个任务；" + why
          + (manualHold
            ? "；队列当前是人工暂停，点「继续队列」后才会自动继续"
            : "；队列会自动继续投稿，无需人工值守"), "ok", 6000);
      } else {
        toast("已加入投稿队列 " + queued + " 个任务", "ok");
      }
      // 稿件数/审核中会变：作废稿件计数，停在「全部稿件」时立刻重取
      clearOverviewCache("archive");
      if (document.body.dataset.view === "archive") loadOverviewStats("archive", true);
      setTimeout(() => { bar.classList.remove("show"); fill.style.width = "0"; }, 1400);
      resetCompose();
      await pollTasks();
    } catch (e) {
      bar.classList.remove("show");
      toast("提交失败：" + e.message, "err", 4600);
      if (queued) toast("已成功入队 " + queued + " 个，其余未提交", "ok", 4600);
    } finally {
      state.uploading = false;
      renderLoginState();
      fill.style.width = "0";
      resetPollBackoff();
    }

    if ($("publishAt").value) {
      const mode = $("scheduleMode").value;
      if (mode === "local") {
        toast("已在本地排队，到点程序自动上传（期间请保持程序运行）", "ok", 4000);
      }
    }
  }

  function resetCompose() {
    // 批量投稿时，分区 / 标签 / 简介 / 合集 / 话题 / 模板这些「可复用设置」
    // 应该留着，只清掉属于这一批稿件的文件、标题、封面和定时时间。
    // 旧实现一次性全清，投第二个视频要重新填一遍。
    state.files = [];
    state.coverFile = null;
    state.coverPreviewUrl = "";
    state.coverCleared = false;
    $("titleInput").value = "";
    $("publishAt").value = "";
    $("scheduleModeRow").hidden = true;
    $("scheduleMode").value = "local";
    $("tagInput").value = "";
    renderFiles();
    renderTags();
    renderCoverPreview();
    updateCounters();
    updateAdvCount();
  }

  // 「恢复默认」只重置可复用的投稿设置（分区 / 标签 / 模板 / 合集 / 话题 /
  // 创作类型 / 可见性 / 定时模式），不动当前已选的稿件本身
  // （视频文件、封面、标题、简介、定时时间），免得选好的稿子被一起清掉。
  function resetAllToDefaults() {
    // 恢复默认同时退出模板编辑态，免得下次保存把空表单覆盖回模板
    if (state.editTpl) setTplEdit(null);
    // 可复用设置：标签 / 创作类型 / 合集 / 可见性 / 话题 / 模板 / 分区
    state.tags = [];
    $("tagInput").value = "";
    $("copyrightSelect").value = "1";
    $("noReprint").checked = false;
    if ($("seasonSelect")) $("seasonSelect").value = "0";
    if ($("noPush")) $("noPush").checked = false;
    if ($("onlySelf")) $("onlySelf").checked = false;
    $("topicInput").value = "";
    hideTopicSuggest();
    setTopic({ id: 0, name: "", mission_id: 0 });
    if ($("tplSelect")) $("tplSelect").value = "";
    // 分区先归零，再套用设置里的默认参数（分区 / 标签 / 简介 / 创作类型）
    if ($("tidSelect").options.length) $("tidSelect").selectedIndex = 0;
    applyDefaults();
    // 统一刷新渲染与计数
    syncCopyright();
    renderTags();
    updateCounters();
    updateAdvCount();
    toast("已恢复默认投稿设置，当前稿件保留", "ok");
  }

  function updateAdvCount() {
    const el = $("advCount");
    if (!el) return;
    let n = 0;
    if ($("seasonSelect") && Number($("seasonSelect").value) > 0) n++;
    if (state.topic && state.topic.id) n++;
    if ($("tplSelect") && $("tplSelect").value) n++;
    if (($("noPush") && $("noPush").checked) || ($("onlySelf") && $("onlySelf").checked)) n++;
    if (($("noReprint") && $("noReprint").checked) ||
        ($("copyrightSelect") && $("copyrightSelect").value === "2")) n++;
    el.textContent = n ? n + " 项已设置" : "合集 / 话题 / 模板";
    el.classList.toggle("has-value", n > 0);
  }

  /* ---------------------------------------------------------- 事件绑定 */
  function bind() {
    // 文件
    $("dropzone").addEventListener("click", () => $("fileInput").click());
    $("dropzone").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("fileInput").click(); }
    });
    $("fileInput").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });

    ["dragenter", "dragover"].forEach((ev) =>
      $("dropzone").addEventListener(ev, (e) => { e.preventDefault(); $("dropzone").classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) =>
      $("dropzone").addEventListener(ev, (e) => { e.preventDefault(); $("dropzone").classList.remove("drag"); }));
    $("dropzone").addEventListener("drop", (e) => {
      if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
    });

    // 整个窗口也接住拖放，避免浏览器直接打开视频
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());

    // 文件列表
    $("fileList").addEventListener("click", (e) => {
      const btn = e.target.closest(".remove");
      if (!btn) return;
      state.files.splice(Number(btn.dataset.i), 1);
      renderFiles();
      renderCoverPreview();
    });
    $("fileList").addEventListener("input", (e) => {
      const inp = e.target.closest(".part-input");
      if (!inp) return;
      state.files[Number(inp.dataset.i)].partTitle = inp.value;
    });

    // 模式
    $("modeSeg").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-mode]");
      if (!b) return;
      state.mode = b.dataset.mode;
      Array.from($("modeSeg").children).forEach((x) => x.classList.toggle("on", x === b));
      renderFiles();
      if (state.files.length > 1) {
        toast(state.mode === "multi"
          ? "已切到多分P：" + state.files.length + " 个文件会合成 1 个稿件"
          : "已切到单独投稿：" + state.files.length + " 个文件会各成 1 个稿件",
          "ok", 3200);
      }
    });

    // 计数
    ["titleInput", "descInput"].forEach((id) => $(id).addEventListener("input", updateCounters));

    // 标签
    const tagInput = $("tagInput");
    const commitTags = () => { addTagsFrom(tagInput.value); tagInput.value = ""; };
    tagInput.addEventListener("keydown", (e) => {
      // 输入法选词过程中的回车/逗号属于输入法，不能拿来提交标签
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === "Enter" || e.key === "," || e.key === "，" || e.key === "、"
          || e.key === "Unidentified" || e.key === "Process") {
        e.preventDefault();
        commitTags();
      } else if (e.key === "Backspace" && !tagInput.value && state.tags.length) {
        state.tags.pop();
        renderTags();
      }
    });
    // 手机键盘/输入法不保证发得出回车键，输入分隔符或失焦也自动落标签
    tagInput.addEventListener("input", () => {
      const v = tagInput.value;
      if (/[，,、]/.test(v)) {
        const parts = v.split(/[，,、]/);
        tagInput.value = parts.pop();
        addTagsFrom(parts.join(","));
      }
    });
    tagInput.addEventListener("blur", () => { if (tagInput.value.trim()) commitTags(); });
    $("btnAddTag").addEventListener("click", (e) => { e.preventDefault(); commitTags(); tagInput.focus(); });

    // 话题
    if ($("btnTopicSearch")) {
      $("btnTopicSearch").addEventListener("click", async (e) => {
        e.preventDefault();
        const v = ($("topicInput") && $("topicInput").value) || "";
        if (parseTopicText(v)) await applyTopicText(v);
        else searchTopics();
      });
    }
    if ($("topicInput")) {
      $("topicInput").addEventListener("keydown", (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === "Enter") {
          e.preventDefault();
          const v = $("topicInput").value;
          if (parseTopicText(v)) applyTopicText(v); else searchTopics();
        }
      });
    }
    if ($("topicInput")) {
      $("topicInput").addEventListener("paste", () => {
        setTimeout(() => {
          const v = ($("topicInput") && $("topicInput").value) || "";
          if (parseTopicText(v)) applyTopicText(v);
        }, 0);
      });
    }
    if ($("topicClear")) {
      $("topicClear").addEventListener("click", (e) => { e.preventDefault(); setTopic({ id: 0 }); });
    }
    document.addEventListener("click", (e) => {
      const box = $("topicSuggest");
      if (!box || box.hidden) return;
      if (box.contains(e.target)) return;
      if ($("topicBox") && $("topicBox").contains(e.target)) return;
      hideTopicSuggest();
    });
    $("tagBox").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-i]");
      if (b) { state.tags.splice(Number(b.dataset.i), 1); renderTags(); }
    });

    // 封面
    $("btnPickCover").addEventListener("click", () => $("coverInput").click());
    $("coverInput").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      state.coverFile = f;
      state.coverPreviewUrl = URL.createObjectURL(f);
      state.coverCleared = false;
      renderCoverPreview();
      e.target.value = "";
    });
    $("btnClearCover").addEventListener("click", () => {
      state.coverFile = null;
      state.coverPreviewUrl = "";
      state.coverCleared = true;
      renderCoverPreview();
    });

    // 创作类型：转载时禁用「禁止转载」
    if ($("copyrightSelect")) $("copyrightSelect").addEventListener("change", syncCopyright);
    // 选了定时时间才显示定时方式
    $("publishAt").addEventListener("input", () => {
      $("scheduleModeRow").hidden = !$("publishAt").value;
    });
    // 模板
    $("tplSelect").addEventListener("change", async (e) => {
      if (!e.target.value) { if (state.editTpl) setTplEdit(null); return; }
      await applyTemplate(e.target.value);
    });
    if ($("btnResetAll")) $("btnResetAll").addEventListener("click", resetAllToDefaults);
    $("seasonSelect").addEventListener("change", async (e) => {
      if (e.target.value !== "new") return;
      const name = (prompt("新合集名称（创建后在B站侧会走审核）") || "").trim();
      if (!name) { e.target.value = "0"; return; }
      e.target.disabled = true;
      try {
        const r = await api("/api/seasons", { method: "POST", body: { title: name } });
        if (!r.ok) throw new Error(r.error || "创建失败");
        await loadSeasons(true);
        const id = String((r.item && r.item.id) || "");
        if (id && Array.from(e.target.options).some((o) => o.value === id)) e.target.value = id;
        toast("已新建合集「" + name + "」", "ok", 3400);
      } catch (err) {
        toast("新建合集失败：" + err.message, "err", 4400);
        e.target.value = "0";
      } finally {
        e.target.disabled = false;
      }
    });

    $("btnSaveTpl").addEventListener("click", async () => {
      const editing = state.editTpl;
      const name = prompt(
        editing ? "模板名字（留空则不改名，点确定后覆盖原模板参数）" : "给这套参数起个名字（分区、标签、简介会一起保存）",
        editing ? editing.name : ""
      );
      if (name === null) return;                                  // 点取消就什么都不做
      const finalName = (name || "").trim() || (editing ? editing.name : "");
      if (!finalName) return;
      try {
        const payload = collectTemplatePayload();
        if (editing) {
          const r = await api("/api/templates/" + editing.id, {
            method: "PUT",
            body: { name: finalName, payload: payload },
          });
          state.templates = state.templates.map((t) => (String(t.id) === String(editing.id) ? r.template : t));
          setTplEdit(null);
          toast("模板已更新：" + r.template.name, "ok", 3200);
          return;
        }
        const r = await api("/api/templates", { method: "POST", body: { name: finalName, payload: payload } });
        state.templates.unshift(r.template);
        renderTemplates();
        toast("模板已保存", "ok");
      } catch (e) { toast(e.message, "err"); }
    });
    if ($("btnCancelTplEdit")) {
      $("btnCancelTplEdit").addEventListener("click", () => {
        if (!state.editTpl) return;
        setTplEdit(null);
        toast("已退出模板编辑", "info");
      });
    }
    $("tplList").addEventListener("click", async (e) => {
      const ed = e.target.closest("button[data-edit-tpl]");
      if (ed) {
        const t = state.templates.find((x) => String(x.id) === String(ed.dataset.editTpl));
        if (t) {
          await applyTemplate(t.id, { forEdit: true });
          studioSetView("workbench");
        }
        return;
      }
      const u = e.target.closest("button[data-use-tpl]");
      if (u) {
        await applyTemplate(u.dataset.useTpl);
        studioSetView("workbench");
        return;
      }
      const b = e.target.closest("button[data-del-tpl]");
      if (!b) return;
      try {
        await api("/api/templates/" + b.dataset.delTpl, { method: "DELETE" });
        state.templates = state.templates.filter((t) => String(t.id) !== b.dataset.delTpl);
        if (state.editTpl && String(state.editTpl.id) === b.dataset.delTpl) setTplEdit(null);
        else renderTemplates();
        toast("模板已删除", "ok");
      } catch (err) { toast(err.message, "err"); }
    });

    // 模板页 → 工作台
    $("btnGoCompose").addEventListener("click", () => studioSetView("workbench"));

    // 发布
    $("btnPublish").addEventListener("click", submitPublish);

    // 队列
    $("queuePager").addEventListener("click", (e) => {
      const ab = e.target.closest("[data-arch]");
      if (ab && !ab.disabled) {
        const kind = ab.dataset.arch;
        if (kind === "goto") loadArchives(Number(ab.dataset.p));
        else if (kind === "jump") {
          const box = $("archJump");
          const n = Math.max(1, parseInt((box && box.value) || "0", 10) || 0);
          if (n) loadArchives(n);
        } else if (kind === "next") loadArchives(state.archPage + 1);
        else loadArchives(state.archPage - 1);
        return;
      }
      const btn = e.target.closest("[data-page]");
      if (!btn || btn.disabled) return;
      const pages = Math.max(1, Math.ceil(filterTasks(state.tasks || []).length / PAGE_SIZE));
      const kind = btn.dataset.page;
      if (kind === "jump") {
        const box = $("queueJump");
        const n = Math.max(1, parseInt((box && box.value) || "0", 10) || 0);
        if (n) state.page = Math.min(pages, n);
      } else {
        state.page = kind === "next"
          ? Math.min(pages, state.page + 1)
          : Math.max(1, state.page - 1);
      }
      renderTasks();
    });

    $("queuePager").addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const t = e.target;
      if (t && t.id === "queueJump") {
        e.preventDefault();
        const pages = Math.max(1, Math.ceil(filterTasks(state.tasks || []).length / PAGE_SIZE));
        const n = Math.max(1, parseInt(t.value || "0", 10) || 0);
        if (n) { state.page = Math.min(pages, n); t.value = ""; renderTasks(); }
      } else if (t && t.id === "archJump") {
        e.preventDefault();
        const n = Math.max(1, parseInt(t.value || "0", 10) || 0);
        if (n) { t.value = ""; loadArchives(n); }
      }
    });

    $("queueList").addEventListener("click", (e) => {
      const eb = e.target.closest("button[data-edit]");
      if (eb) {
        e.preventDefault();
        openEdit(Number(eb.dataset.edit) || 0);
        return;
      }
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const id = b.dataset.id;
      const act = b.dataset.act;
      if (act === "logs") toggleLogs(id);
      else taskAction(id, act);
    });

    $("btnCloseEdit").addEventListener("click", closeEdit);
    $("btnCancelEdit").addEventListener("click", closeEdit);
    $("editMask").addEventListener("click", closeEdit);
    $("btnSaveEdit").addEventListener("click", saveEdit);
    $("btnEditCover").addEventListener("click", () => {
      const input = $("editCoverFile");
      input.value = "";
      input.click();
    });
    $("editCoverFile").addEventListener("change", (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file || !state.edit) return;
      uploadEditFile("/api/archive/" + state.edit.aid + "/cover", "cover", file, {}, "封面已更换")
        .then((data) => {
          if (data.archive && data.archive.cover) {
            $("editCoverImg").src = "/api/proxy/cover?u=" + encodeURIComponent(data.archive.cover);
          }
          refreshAfterEdit();
        })
        .catch((err) => editStatus("封面更换失败：" + err.message, "err"));
    });
    $("btnReplaceVideo").addEventListener("click", () => pickEditVideo("replace"));
    $("btnAppendVideo").addEventListener("click", () => pickEditVideo("append"));
    $("editVideoFile").addEventListener("change", (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file || !state.edit) return;
      const mode = e.target.dataset.mode === "replace" ? "replace" : "append";
      uploadEditFile("/api/archive/" + state.edit.aid + "/video", "video", file,
        { mode: mode, title: $("editTitle").value.trim() },
        mode === "replace" ? "视频已更换" : "分P已追加")
        .then(() => { setTimeout(() => { closeEdit(); refreshAfterEdit(); }, 900); })
        .catch((err) => editStatus((mode === "replace" ? "更换视频" : "追加分P") + "失败：" + err.message, "err"));
    });

    $("btnPauseQueue").addEventListener("click", async () => {
      try {
        const r = await api("/api/queue/pause", { method: "POST", body: { paused: !state.paused } });
        state.paused = r.paused;
        $("btnPauseQueue").textContent = state.paused ? "继续队列" : "暂停队列";
        toast(state.paused ? "队列已暂停" : "队列已恢复", "ok");
      } catch (e) { toast(e.message, "err"); }
    });
    $("btnRefresh").addEventListener("click", async () => {
      // 按当前视图刷各自的数据：全部稿件刷 B站列表 + 顶部件数统计，其余刷本地队列。
      // 以前只重拉列表、不更新概览数字，点了像没反应，容易被误读成「刷的是队列」。
      const btn = $("btnRefresh");
      const isArch = state.view === "all";
      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "刷新中…";
      try {
        if (isArch) {
          await Promise.all([loadArchives(state.archPage), loadOverviewStats("archive", true)]);
        } else {
          await pollTasks();
          await loadOverviewStats("workbench", true);
          syncOverview();
        }
        toast(isArch ? "稿件列表已刷新" : "队列已刷新", "ok", 1200);
      } catch (e) {
        toast("刷新失败：" + e.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });
    $("btnLogBottom").addEventListener("click", () => {
      const box = $("globalLogBox");
      if (box) box.scrollTop = box.scrollHeight;
    });
    $("btnClearLogView").addEventListener("click", async () => {
      if (!confirm("确定清空全部日志吗？此操作不可恢复。")) return;
      const r = await api("/api/logs", { method: "DELETE" });
      state.glogs = r.logs || [];
      state.glogsSig = "";
      renderGlobalLogs();
      toast("日志已清空", "ok", 1500);
    });

    // 额度：顶栏胶囊与「剩余额度」概览格是同一件事
    $("quotaChip").addEventListener("click", refreshQuotaNow);

    // 设置抽屉
    $("btnSettings").addEventListener("click", openDrawer);
    $("accountBox").addEventListener("click", openDrawer);
    $("btnCloseDrawer").addEventListener("click", closeDrawer);
    $("drawerMask").addEventListener("click", closeDrawer);
    $("btnSaveSettings").addEventListener("click", saveSettings);
    $("btnSaveCookie").addEventListener("click", saveCookie);
    $("btnCleanNow").addEventListener("click", cleanNow);
    $("btnNotifyTest").addEventListener("click", testNotify);
    $("notifyKind").addEventListener("change", syncNotifyRows);
    $("webAuthMode").addEventListener("change", syncAuthRows);
    // 手机切回浏览器时立刻补一次状态查询，避免后台挂起期间错过结果
    document.addEventListener("visibilitychange", () => {
      resetPollBackoff();
      schedulePolling();
      if (!document.hidden) {
        if (qrKey) pollQr();
        if (!state.uploading) pollTasks();
      }
    });
    $("btnQrLogin").addEventListener("click", startQrLogin);
    $("btnQrRefresh").addEventListener("click", startQrLogin);

    // 主题 / 登录引导 / 更多设置折叠
    if ($("btnTheme")) {
      $("btnTheme").addEventListener("click", () =>
        applyTheme(state.theme === "dark" ? "light" : "dark"));
    }
    if ($("btnGuideLogin")) $("btnGuideLogin").addEventListener("click", openDrawer);
    const adv = $("advanced"), advBtn = $("advToggle");
    if (adv && advBtn) {
      advBtn.addEventListener("click", () => {
        state.advOpen = !state.advOpen;
        adv.hidden = !state.advOpen;
        advBtn.classList.toggle("open", state.advOpen);
        advBtn.setAttribute("aria-expanded", state.advOpen ? "true" : "false");
      });
    }
    ["seasonSelect", "tplSelect", "copyrightSelect", "noReprint", "noPush", "onlySelf"]
      .forEach((id) => {
        const el = $(id);
        if (el) el.addEventListener("change", updateAdvCount);
      });
    updateAdvCount();

    // 队列状态筛选：顶部统计胶囊（状态下拉已移除，避免与胶囊重复）
    if ($("queueStats")) {
      $("queueStats").addEventListener("click", (e) => {
        const chip = e.target.closest(".stat[data-filter]");
        if (chip) setTaskFilter(chip.dataset.filter, true);
      });
      $("queueStats").addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const chip = e.target.closest(".stat[data-filter]");
        if (!chip) return;
        e.preventDefault();
        setTaskFilter(chip.dataset.filter, true);
      });
    }
    if ($("btnClearDone")) $("btnClearDone").addEventListener("click", clearDoneTasks);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submitPublish();
    });
  }

  /* ---------------------------------------------------------- v76 Studio 外壳 */
  // 左侧 Rail 导航：工作台 / 队列 / 全部稿件 / 模板 / 运行日志，均为独立视图
  function setNum(id, v) {
    const el = $(id);
    if (el) el.textContent = v;
  }

  /* ------------------------------------------------ 概览条：按视图切换指标
     一套 DOM（4 个格子）复用，切换视图时只换图标/配色/标签/取数 key。
     队列沿用 v82 的本地派生数据；工作台与全部稿件读 B站创作中心汇总接口。 */
  const ICO = (d) => '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">' + d + '</svg>';
  const OV_ARCHIVE_TTL_MS = 90 * 1000;   // 与后端 ARCHIVE_TTL 一致
  const OV_MODES = {
    workbench: {
      ico: ["pink", "blue", "green", "amber"],
      svg: [
        ICO('<path d="M8 5v14l11-7z"/>'),
        ICO('<path d="M7 10v10H3V10zM7 10l4-7a2 2 0 0 1 2 2v4h6a2 2 0 0 1 2 2l-1 7a2 2 0 0 1-2 2H7"/>'),
        ICO('<circle cx="12" cy="12" r="9"/><path d="M12 7v10M9 10h6M9 14h6"/>'),
        ICO('<path d="M6 4h12v16l-6-4-6 4z"/>'),
      ],
      labels: ["播放数", "点赞数", "投币数", "收藏数"],
      keys: ["play", "like", "coin", "fav"],
      incrKeys: ["play", "like", "coin", "fav"],
      // 纯展示：浏览器里没有 B站登录态，跳创作中心只会看到未登录页，所以不给下钻
      acts: ["", "", "", ""],
      hints: ["", "", "", ""],
    },
    archive: {
      ico: ["blue", "green", "amber", "pink"],
      svg: [
        ICO('<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>'),
        ICO('<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>'),
        ICO('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>'),
        ICO('<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>'),
      ],
      labels: ["稿件数", "审核通过", "审核中", "未通过"],
      keys: ["total", "pubed", "is_pubing", "not_pubed"],
      acts: ["all", "pubed", "is_pubing", "not_pubed"],
      hints: ["查看全部稿件", "只看审核通过的稿件", "只看审核中的稿件", "只看未通过的稿件"],
    },
    queue: {
      ico: ["pink", "blue", "green", "amber"],
      svg: [
        ICO('<path d="M12 16V4m0 0L8 8m4-4l4 4"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>'),
        ICO('<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5M3 17l9 5 9-5"/>'),
        ICO('<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>'),
        ICO('<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>'),
      ],
      labels: ["近 24h 已投", "队列待办", "剩余额度", "累计成功"],
      keys: [],
      acts: ["all", "pending", "quota", "success"],
      hints: ["查看全部任务", "只看等待中的任务", "重新统计近 24 小时投稿数", "只看投稿成功的任务"],
    },
  };

  // 概览条显示完整数字（带千分位），不做「万/亿」缩写；太长时缩小字号而不是省略
  function fmtFull(n) {
    if (typeof n !== "number" || !isFinite(n)) return "—";
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  // 较短昨日增量：+12 / -3 / 0，B站接口缺字段时不显示
  function fmtIncr(n) {
    if (typeof n !== "number" || !isFinite(n)) return "";
    if (n > 0) return "+" + fmtFull(n);
    if (n < 0) return "-" + fmtFull(Math.abs(n));
    return "0";
  }

  function ovPartFor(view) {
    return view === "workbench" || view === "archive" ? view : null;
  }

  // 概览格点击下钻：全部稿件按审核状态筛，队列按任务状态筛 / 查 24h 额度
  function ovDrill(i, view) {
    const mode = OV_MODES[view] || OV_MODES.queue;
    const act = (mode.acts || [])[i] || "";
    if (!act) return;
    if (act === "quota") { refreshQuotaNow(); return; }
    if (view === "archive") setArchStatus(act === "all" ? "" : act);
    else setTaskFilter(act === "all" ? "" : act);
  }

  // 只在切到对应视图时取数；是否重取由「与后端一致的 TTL」决定，绝不轮询打 B站
  async function loadOverviewStats(part, force) {
    if (!part) return;
    state.ovStats = state.ovStats || {};
    state.ovBusy = state.ovBusy || {};
    state.ovSeq = state.ovSeq || {};
    const cached = state.ovStats[part];
    // 账号累计按统计日（12:00 换一份）；稿件计数 90 秒 TTL —— 与后端 ARCHIVE_TTL 同一口径。
    // 之前对 archive 是「有缓存就直接 return」，导致后端 90 秒 TTL 实际不生效、
    // 停在「全部稿件」时数字长期不更新；这里改成按时间戳判断，过期就重取。
    if (!force && cached) {
      const fresh = part === "archive"
        ? (Date.now() - Number(cached.at || 0) < OV_ARCHIVE_TTL_MS)
        : cached.day === ovStatDay();
      if (fresh) return;
    }
    if (state.ovBusy[part]) return;
    state.ovBusy[part] = true;
    // 序号用于丢弃“切号/清缓存之前发出、切号之后才回来”的旧响应
    const seq = (state.ovSeq[part] = (state.ovSeq[part] || 0) + 1);
    try {
      const r = await api("/api/account/stats?part=" + part + (force ? "&force=1" : ""));
      if (state.ovSeq[part] !== seq) return;
      state.ovStats[part] = { ok: !!r.ok, items: r.items || {}, incr: r.incr || {},
        error: r.error || "", note: r.note || "", cached: !!r.cached, day: r.day || "",
        at: Date.now() };
    } catch (e) {
      if (state.ovSeq[part] !== seq) return;
      state.ovStats[part] = { ok: false, items: {}, incr: {}, error: e.message || "读取失败", at: Date.now() };
    } finally {
      if (state.ovSeq[part] === seq) state.ovBusy[part] = false;
    }
    if (document.body.dataset.view === part) syncOverview();
  }

  /* 与后端一致的统计日：北京时间 12:00 换日（B站账号累计次日中午才更新） */
  function ovStatDay(at) {
    const t = at || Date.now();
    const shifted = new Date(t + 8 * 3600 * 1000 - 12 * 3600 * 1000);
    const y = shifted.getUTCFullYear();
    const m = String(shifted.getUTCMonth() + 1).padStart(2, "0");
    const d = String(shifted.getUTCDate()).padStart(2, "0");
    return y + "-" + m + "-" + d;
  }

  /* 切号 / 重新登录 / 投稿后清概览缓存：作废正在飞的旧请求，避免把上一个号的数据画回来 */
  function clearOverviewCache(part) {
    state.ovStats = state.ovStats || {};
    state.ovBusy = state.ovBusy || {};
    state.ovSeq = state.ovSeq || {};
    const parts = part ? [part] : ["workbench", "archive"];
    parts.forEach((p) => {
      delete state.ovStats[p];
      state.ovSeq[p] = (state.ovSeq[p] || 0) + 1;
      state.ovBusy[p] = false;
    });
  }

  /* 清完缓存后立刻重画：先显示「—」和加载态，再按当前视图重新取数 */
  function refreshOverview(reason) {
    clearOverviewCache();
    const view = document.body.dataset.view;
    if (view === "workbench" || view === "archive" || view === "queue") {
      applyOverviewMode(view);
      return;
    }
    syncOverview();
  }

  // 换视图时重绘 4 个格子的图标/配色/标签，并触发对应数据（不阻塞渲染）
  function applyOverviewMode(view) {
    const mode = OV_MODES[view] || OV_MODES.queue;
    const strip = $("statStrip");
    if (strip) strip.dataset.mode = view;
    for (let i = 0; i < 4; i++) {
      const slot = $("ovSlot" + i);
      if (!slot) continue;
      const ico = slot.querySelector(".si-ico");
      if (ico) { ico.className = "si-ico " + mode.ico[i]; ico.innerHTML = mode.svg[i]; }
      const lab = slot.querySelector(".l");
      if (lab) lab.textContent = mode.labels[i];
      const act = (mode.acts || [])[i] || "";
      slot.dataset.act = act;
      // 有下钻动作才具备按钮语义；纯展示的格子不抢 Tab 焦点
      if (act) {
        slot.setAttribute("role", "button");
        slot.tabIndex = 0;
      } else {
        slot.removeAttribute("role");
        slot.removeAttribute("tabindex");
      }
      slot.title = (mode.hints || [])[i] || "";
      const d = $("ovd" + i);
      if (d) { d.hidden = true; d.textContent = ""; d.className = "d"; }
    }
    syncOverview();
    loadOverviewStats(ovPartFor(view));
  }

  // 导航外观唯一入口：active / aria-current / 面包屑 / 页面标题 / 独立区块显隐
  const STUDIO_VIEWS = ["workbench", "queue", "archive", "templates", "logs"];
  const STUDIO_META = {
    workbench: ["工作台", "Workspace", "开始今天的投稿",
      "填写稿件、加入队列，程序会按顺序自动上传。账号额度、失败重试与定时发布都在后台替你把关。"],
    queue: ["队列", "Queue", "投稿队列",
      "本软件投过的稿件都在这里。可按状态查看进度、暂停队列或清除已完成。"],
    archive: ["全部稿件", "Library", "账号全部稿件",
      "直接读取账号在 B 站的稿件列表，可编辑或跳转，支持按状态筛选。"],
    templates: ["模板", "Templates", "投稿模板",
      "把常用的分区、标签、简介、合集与话题存成模板，下次一键套用。在新建投稿里点「存为模板」即可新增。"],
    logs: ["运行日志", "Logs", "运行日志",
      "程序上传、重试、风控拦截与账号额度变化都记录在这里。默认展示最近 150 条，随队列轮询自动更新。"],
  };

  function syncStudioChrome(view) {
    const nav = $("railNav");
    if (nav) {
      Array.from(nav.querySelectorAll(".rail-item[data-view]")).forEach((b) => {
        const on = b.dataset.view === view;
        b.classList.toggle("active", on);
        if (on) b.setAttribute("aria-current", "page");
        else b.removeAttribute("aria-current");
      });
    }
    const meta = STUDIO_META[view] || STUDIO_META.workbench;
    const crumb = $("crumb");
    if (crumb) crumb.textContent = meta[0];
    // 五个视图共用同一个页头（标题→横幅→内容），模板/日志不再自带一份：
    // 自带的那份在横幅之后，视觉会变成「横幅在上、标题在下」，与其它页面不一致。
    if ($("viewEyebrow")) $("viewEyebrow").textContent = meta[1];
    if ($("viewTitle")) $("viewTitle").textContent = meta[2];
    if ($("viewSub")) $("viewSub").textContent = meta[3];
    document.body.dataset.view = view;
    const tpl = $("viewTemplates");
    if (tpl) tpl.hidden = view !== "templates";
    const lg = $("viewLogs");
    if (lg) lg.hidden = view !== "logs";
  }

  function currentStudioView() {
    const v = document.body.dataset.view;
    return STUDIO_VIEWS.indexOf(v) >= 0 ? v : "workbench";
  }

  // 全站唯一视图切换入口：左侧导航与所有跳转都走这里
  function studioSetView(view, opts) {
    const o = opts || {};
    if (STUDIO_VIEWS.indexOf(view) < 0) view = "workbench";
    const target = view === "archive" ? "all" : "tasks";
    syncStudioChrome(view);
    applyOverviewMode(view);
    if (target !== state.view) {
      state.view = target;
      state.sig = "";
      state.archSig = "";
    }
    syncToolbarForView();
    if (o.render === false) return;
    if (view === "archive") loadArchives(o.page || state.archPage || 1);
    else if (view === "templates") renderTemplates();
    else if (view === "logs") {
      fetchGlobalLogs();
      const b = $("globalLogBox");
      if (b) setTimeout(() => { b.scrollTop = b.scrollHeight; }, 0);
    }
    else renderTasks();
    closeRail();
  }

  function closeRail() {
    const app = document.querySelector(".app");
    if (!app || !app.classList.contains("rail-open")) return;
    app.classList.remove("rail-open");
    const t = $("railToggle");
    if (t) { t.setAttribute("aria-expanded", "false"); t.focus(); }
  }

  function openRail() {
    const app = document.querySelector(".app");
    if (!app) return;
    if ($("drawerMask") && $("drawerMask").classList.contains("open")) closeDrawer();
    app.classList.add("rail-open");
    const t = $("railToggle");
    if (t) t.setAttribute("aria-expanded", "true");
    const first = document.querySelector(".rail-item[data-view].active") ||
                  document.querySelector(".rail-item[data-view]");
    if (first) first.focus();
  }

  // 概览条与 Rail 上的账号/角标都从已有的 state 派生，不额外请求
  function syncOverview() {
    const view = document.body.dataset.view || "workbench";
    const q = state.quota || {};
    const cs = state.counts || {};
    const limit = Number(q.limit || (state.config && state.config.quota_limit) || 50);
    const counted = typeof q.count === "number" ? q.count : null;
    const strip = $("statStrip");

    if (view === "workbench" || view === "archive") {
      const st = state.ovStats[view] || null;
      const items = (st && st.ok && st.items) || {};
      const incr = (st && st.ok && st.incr) || {};
      const mode = OV_MODES[view];
      const keys = mode.keys;
      const incrKeys = mode.incrKeys || [];
      for (let i = 0; i < 4; i++) {
        const v = items[keys[i]];
        const el = $("ov" + i);
        if (!el) continue;
        const txt = typeof v === "number" ? fmtFull(v) : "—";
        el.textContent = txt;
        el.title = typeof v === "number" ? String(v) : "";
        el.classList.toggle("num-long", txt.length > 9);
        el.classList.toggle("num-xlong", txt.length > 12);
        el.classList.toggle("num-xxlong", txt.length > 15);
        // 工作台：数字下方显示 B站给的「较昨日」增量；为 0 或字段缺失时不占位（避免四格都挂个 0）
        const d = $("ovd" + i);
        if (d) {
          const iv = incrKeys.length ? incr[incrKeys[i]] : undefined;
          const itxt = fmtIncr(iv);
          if (itxt && iv !== 0) {
            d.hidden = false;
            d.textContent = itxt;
            d.className = "d " + (iv > 0 ? "up" : "down");
            d.title = "较昨日 " + itxt;
            if (typeof v === "number") el.title = String(v) + " · 较昨日 " + itxt;
          } else {
            d.hidden = true;
            d.textContent = "";
            d.className = "d";
            if (itxt) el.title = (typeof v === "number" ? String(v) + " · " : "") + "较昨日 0";
          }
        }
      }
      if (strip) {
        strip.dataset.state = (st && st.ok) ? "ok" : ((st && st.error) ? "error" : "loading");
        strip.title = (st && st.ok)
          ? (st.note || "数据来自 B站创作中心")
          : ((st && st.error) ? st.error : "正在读取 B站汇总数据…");
      }
    } else {
      // 队列：保持 v82 口径（近24h已投 / 队列待办 / 剩余额度 / 累计成功）
      setNum("ov0", counted === null ? "—" : counted);
      setNum("ov1", (cs.queued || 0) + (cs.waiting_schedule || 0));
      setNum("ov2", counted === null ? "—" : Math.max(0, limit - counted));
      setNum("ov3", cs.success || 0);
      for (let i = 0; i < 4; i++) {
        const el = $("ov" + i); if (el) el.title = "";
        const d = $("ovd" + i);
        if (d) { d.hidden = true; d.textContent = ""; d.className = "d"; }
      }
      if (strip) { strip.dataset.state = "ok"; strip.title = ""; }
    }

    const badge = $("railQueueBadge");
    if (badge) {
      const n = (cs.queued || 0) + (cs.waiting_schedule || 0) + (cs.uploading || 0);
      badge.textContent = n;
      badge.hidden = n === 0;
    }
    const a = state.account || {};
    const av = $("railAvatar");
    const nm = $("railName");
    const sb = $("railSub");
    if (nm) nm.textContent = a.logged_in ? (a.uname || "已登录") : "未登录";
    if (sb) sb.textContent = a.logged_in ? ("UID " + (a.mid || "")) : "点击登录";
    if (av) {
      if (a.logged_in && a.face) {
        av.style.backgroundImage = 'url("/api/proxy/cover?u=' + encodeURIComponent(a.face) + '")';
        av.textContent = "";
      } else {
        av.style.backgroundImage = "";
        av.textContent = a.logged_in ? String(a.uname || "?").slice(0, 1) : "?";
      }
    }
  }

  function bindStudio() {
    const nav = $("railNav");
    if (nav) {
      nav.addEventListener("click", (e) => {
        const btn = e.target.closest(".rail-item[data-view]");
        if (btn) studioSetView(btn.dataset.view);
      });
    }
    const toggle = $("railToggle");
    if (toggle) toggle.addEventListener("click", () => {
      const app = document.querySelector(".app");
      if (app && app.classList.contains("rail-open")) closeRail();
      else openRail();
    });
    const backdrop = $("railBackdrop");
    if (backdrop) backdrop.addEventListener("click", closeRail);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeRail();
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth > 900) closeRail();
    });
    if ($("railAccount")) $("railAccount").addEventListener("click", openDrawer);

    // 概览条：4 个格子都是下钻入口（点击 / 回车 / 空格）
    const strip = $("statStrip");
    if (strip) {
      const run = (e) => {
        const si = e.target.closest(".si[data-ov]");
        if (!si || !si.dataset.act) return;
        ovDrill(Number(si.dataset.ov), document.body.dataset.view);
      };
      strip.addEventListener("click", run);
      strip.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const si = e.target.closest(".si[data-ov]");
        if (!si || !si.dataset.act) return;
        e.preventDefault();
        ovDrill(Number(si.dataset.ov), document.body.dataset.view);
      });
    }

    // 全部稿件：审核状态筛选胶囊
    const af = $("archFilters");
    if (af) {
      af.addEventListener("click", (e) => {
        const b = e.target.closest(".arch-chip[data-arch-status]");
        if (b) setArchStatus(b.dataset.archStatus || "");
      });
    }

    // 页面从后台切回来：按同一 TTL 检查一次，过期才重取（不轮询、不额外打接口）
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      const v = document.body.dataset.view;
      if (v === "workbench" || v === "archive") loadOverviewStats(v);
    });

    syncStudioChrome("workbench");
    applyOverviewMode("workbench");
  }

  initTheme();
  bind();
  bindStudio();
  updateCounters();
  renderCoverPreview();
  boot();
})();
