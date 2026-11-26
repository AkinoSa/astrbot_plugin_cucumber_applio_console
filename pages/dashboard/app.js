/* Applio 控制台 · 页面逻辑（原生 JS，无构建依赖） */
(function () {
  "use strict";

  let bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;

  const TAB_META = {
    overview: { title: "概览", desc: "插件与 Gateway 状态、最近任务一览" },
    tasks: { title: "任务", desc: "Applio 翻唱与 TTS 语音的当前/历史任务，可试听回放" },
    rvc: { title: "Applio 控制台", desc: "Applio v2 现代推理引擎 · 音色转换与人声/伴奏分离" },
    tts: { title: "TTS 控制台", desc: "输入文字试听音色：原始 TTS 与 Applio 转换后对比" },
    models: { title: "模型管理", desc: "音色列表、别名、性别与默认音色" },
    audio: { title: "音频效果", desc: "歌曲翻唱与 TTS 各自独立的 Reverb 混响参数" },
    guide: { title: "使用说明", desc: "插件指令大全、Kaggle 混合云配置教程与常见问题" },
    settings: { title: "插件设置", desc: "按分组修改插件参数，保存后部分项需重载插件" },
    diagnostics: { title: "诊断", desc: "连通性、配置检查与最近的错误日志" },
  };

  const state = {
    tab: "overview",
    taskPage: 1,
    taskPageSize: 30,
    rvcMode: "convert",
    rvcEngine: "local",
    overview: null,
    models: null,
    fields: [],
    values: {},
    dirty: {},
    needsReload: [],
    pollers: {},
    urls: [],
    uploadId: null,
    sepUploadId: null,
    uploading: false,
    sepRunning: false,
    taskItems: [],
    taskStatus: "all",
    taskMode: "current",
    taskBiz: "rvc",
    ttsItems: [],
    ttsSnap: null,
    gatewayModelStems: [],
    gatewayModelFiles: [],
    activeTimer: null,
    elapsedTimer: null,
    activeAnchorAt: 0,
    activeClockKey: "",
    activeLastSec: -1,
    waitAnchors: {},
    audioEls: [],
    clipUploadId: null,
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const on = (sel, evt, handler) => {
    try {
      const el = typeof sel === "string" ? $(sel) : sel;
      if (el) el.addEventListener(evt, handler);
    } catch (e) {
      console.warn(`[Bind] Failed to bind ${sel} ${evt}:`, e);
    }
  };

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "-";
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function fmtDuration(sec) {
    const n = Number(sec);
    if (!Number.isFinite(n) || n <= 0) return "-";
    return n >= 60 ? `${Math.floor(n / 60)}m${Math.round(n % 60)}s` : `${n.toFixed(1)}s`;
  }

  function toast(text, ms) {
    const el = $("#toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, ms || 2800);
  }

  function banner(text) {
    const el = $("#banner");
    if (!text) { el.hidden = true; el.textContent = ""; return; }
    el.textContent = text;
    el.hidden = false;
  }

  /* ---------------- bridge / API ---------------- */
  async function ensureBridge() {
    if (bridge) {
      try { await bridge.ready(); } catch (e) { /* ignore */ }
      return bridge;
    }
    for (let i = 0; i < 50; i += 1) {
      bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
      if (bridge) {
        try { await bridge.ready(); } catch (e) { /* ignore */ }
        return bridge;
      }
      await new Promise((r) => setTimeout(r, 200));
    }
    throw new Error("未检测到 AstrBot 页面桥接（bridge-sdk.js 未加载）。请重载插件后从左侧「Applio 控制台」进入本页。");
  }

  function unwrap(res) {
    if (res && typeof res === "object") {
      if (res.status === "error") throw new Error(res.message || "请求失败");
      if (res.status === "ok" && Object.prototype.hasOwnProperty.call(res, "data")) return res.data;
    }
    return res || {};
  }

  async function api(path, options) {
    const opt = typeof options === "string" ? { method: options } : (options || {});
    const b = await ensureBridge();
    const endpoint = "page/" + String(path).replace(/^\/+/, "");
    const raw = (opt.method || "GET").toUpperCase() === "GET"
      ? await b.apiGet(endpoint, opt.params || {})
      : await b.apiPost(endpoint, opt.body || {});
    return unwrap(raw);
  }

  /* ---------------- 状态 ---------------- */
  function setStatus(id, stateName, label) {
    const el = $(id);
    if (!el) return;
    el.dataset.state = stateName;
    el.innerHTML = `<i></i>${label}`;
  }

  function setLoading(sel, text) {
    const el = typeof sel === "string" ? $(sel) : sel;
    if (!el) return;
    el.innerHTML = `<div class="loading-state"><span class="spin-ring" aria-hidden="true"></span><span>${esc(text || "正在加载数据，请稍候...")}</span></div>`;
  }

  function statusTag(status) {
    const map = {
      succeeded: ["ok", "成功"], failed: ["err", "失败"],
      running: ["run", "进行中"], pending: ["run", "排队"], unknown: ["", "未知"],
    };
    const [cls, text] = map[status] || ["", status || "未知"];
    return `<span class="tag ${cls}">${esc(text)}</span>`;
  }

  function genderLabel(g) {
    return { male: "男声", female: "女声", unknown: "未知" }[g] || "未知";
  }

  /* ---------------- 导航 ---------------- */
  function switchTab(name) {
    state.tab = name;
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    const meta = TAB_META[name] || {};
    const titleEl = $("#pageTitle");
    const descEl = $("#pageDesc");
    if (titleEl) titleEl.textContent = meta.title || "Applio 控制台";
    if (descEl) descEl.textContent = meta.desc || "";
    document.title = (meta.title ? meta.title + " · " : "") + "Applio 控制台";
    const scrollEl = $("#content");
    if (scrollEl) scrollEl.scrollTop = 0;
    const actions = $(".top-actions");
    if (actions) actions.style.display = name === "overview" ? "" : "none";
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    if (name === "overview") loadOverview();
    if (name === "tasks") {
      switchTaskBiz(state.taskBiz);
    } else {
      stopActivePolling();
    }
    if (name === "models") loadModels();
    if (name === "rvc") prepareRvc();
    if (name === "tts") prepareTts();
    if (name === "audio") loadAudioTab();
    if (name === "settings") loadConfig();
    if (name === "diagnostics") loadDiagnostics();
  }

  function switchRvcMode(mode) {
    state.rvcMode = mode;
    $$("#rvcSeg .seg-item").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    $("#paneConvert").hidden = mode !== "convert";
    $("#paneSeparate").hidden = mode !== "separate";
    $("#paneClip").hidden = mode !== "clip";
    if (mode === "separate") prepareSeparate();
    if (mode === "convert") prepareRvc();
  }

  async function runClip() {
    const btn = $("#clipRun");
    const origText = btn ? btn.textContent : "开始切段";
    const fileInput = $("#clipFile");
    if (!fileInput || !fileInput.files || !fileInput.files.length) {
      if ($("#clipStatus")) $("#clipStatus").textContent = "请先选择待切段音频文件";
      toast("请先选择待切段的音频文件", 4000);
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "切段中...";
    }
    if ($("#clipStatus")) $("#clipStatus").textContent = "准备中...";
    try {
      const uploadId = await uploadAudio("#clipFile", "#clipUploadNote", "clipUploadId");
      const body = {
        upload_id: uploadId,
        seconds: Number($("#clipSeconds").value) || 25,
        output_format: $("#clipFormat").value,
      };
      const d = await api("rvc/clip", { method: "POST", body });
      $("#clipOutputs").innerHTML = "";
      $("#clipStatus").textContent = "已提交，正在切段...";
      pollJob(d.job_id, "#clipStatus", (job) => {
        if (btn) {
          btn.disabled = false;
          btn.textContent = origText;
        }
        if (job.status === "succeeded") {
          const range = job.clip_range || [];
          $("#clipInfo").textContent = range.length === 2
            ? `已切出 ${Number(range[1]).toFixed(2)} 秒（起点 ${Number(range[0]).toFixed(2)} 秒）`
            : "切段完成";
          showOutputs(job.id, "#clipOutputs", null);
        }
      });
    } catch (err) {
      $("#clipStatus").textContent = "失败：" + err.message;
      if (btn) {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }
  }

  /* ---------------- 概览 ---------------- */
  let overviewReqId = 0;
  let deferredKaggleTimer = null;
  async function loadOverview() {
    const curReq = ++overviewReqId;
    const reloadBtn = $("#btnReloadData");
    const cardsEl = $("#tab-overview .cards");
    if (reloadBtn) { reloadBtn.disabled = true; reloadBtn.classList.add("is-loading"); reloadBtn.textContent = "加载中..."; }
    if (cardsEl) cardsEl.classList.add("is-loading-cards");
    if ($("#ovTasks") && !state.overview) {
      setLoading("#ovTasks", "正在连接插件与 Gateway，重启后首次检测约需 2~5 秒...");
    }
    if ($("#ovMeta") && !state.overview) {
      $("#ovMeta").innerHTML = `<dt>状态</dt><dd><span class="spin-ring" aria-hidden="true"></span> 正在同步运行环境...</dd>`;
    }
    try {
      const d = await api("overview");
      if (curReq !== overviewReqId) return;
      state.overview = d;
      banner("");
      const gw = d.gateway || {};
      setStatus("#stPlugin", "ok", "插件 <b>已加载</b>");
      setStatus("#stGateway", gw.ok ? "ok" : "err", `Gateway <b>${gw.ok ? "正常" : "异常"}</b>`);
      $("#ovPlugin").textContent = d.plugin && d.plugin.loaded ? "运行中" : "未知";
      $("#ovPluginHint").textContent = `版本 ${(d.plugin && d.plugin.version) || "未知"}`;
      $("#ovGateway").textContent = gw.ok ? "正常" : "异常";
      $("#ovGatewayHint").textContent = gw.ok
        ? `${gw.latency_ms}ms · ${gw.base}`
        : (gw.error || "不可达");
      $("#ovModels").textContent = `${(d.models && d.models.gateway_count) ?? "-"} 个`;
      $("#ovModelsHint").textContent = d.models && d.models.gateway_ok
        ? `已配置 ${d.models.configured_count} 个 · 今日 ${(d.today && d.today.count) ?? 0} 次`
        : ((d.models && d.models.error) || "Gateway 不可用");
      $("#ovDefault").textContent = (d.default_model && d.default_model.value) || "未设置";
      $("#ovDefaultHint").textContent = [
        d.default_model && d.default_model.alias ? `别名 ${d.default_model.alias}` : "",
        `性别 ${genderLabel(d.default_model && d.default_model.gender)}`,
      ].filter(Boolean).join(" · ");

      const execMode = (d.config && (d.config.execution_mode || (d.config.architecture && d.config.architecture.execution_mode))) || (d.kaggle && d.kaggle.exec_mode) || state.execMode || "local_full";
      state.execMode = execMode;
      const isHybrid = execMode === "kaggle_hybrid";
      if ($("#ovExecMode")) {
        $("#ovExecMode").textContent = isHybrid ? "Kaggle 混合云" : "本地一体化";
        $("#ovExecModeHint").textContent = isHybrid ? "Kaggle云端翻唱 + 本地TTS" : "本地翻唱 + 本地TTS";
      }
      if ($("#badgeExecMode")) {
        if (!isHybrid) {
          $("#badgeExecMode").textContent = "本地运行中";
          $("#badgeExecMode").className = "mode-badge ready";
        } else {
          const k = d.kaggle || {};
          if (k.online) {
            $("#badgeExecMode").textContent = "云端在线";
            $("#badgeExecMode").className = "mode-badge ready";
          } else {
            $("#badgeExecMode").textContent = k.configured ? "云端休眠" : "云端未配置";
            $("#badgeExecMode").className = "mode-badge pending";
          }
        }
      }
      updateModeCardsUI(isHybrid);
      renderKaggleStatus(d.kaggle, isHybrid);

      renderOverviewTasks(d);
      const rows = [
        ["Gateway 目录", (d.models && d.models.dir) || "-"],
        ["任务目录", d.paths && d.paths.tasks_available ? "可读" : "不可读"],
        ["成品目录", d.paths && d.paths.outputs_available ? "可读" : "不可读"],
        ["服务器时间", d.server_time || "-"],
      ];
      $("#ovMeta").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

      if (isHybrid && d.kaggle && d.kaggle.has_token && !(d.kaggle.quota_info && d.kaggle.quota_info.gpu_quota_text)) {
        clearTimeout(deferredKaggleTimer);
        deferredKaggleTimer = setTimeout(() => {
          if (state.tab === "overview" && typeof state.fetchKaggleStatus === "function") {
            state.fetchKaggleStatus(true, false);
          }
        }, 1600);
      }
    } catch (err) {
      setStatus("#stGateway", "err", "Gateway <b>无法读取</b>");
      banner(err.message);
      $("#ovTasks").innerHTML = `<p class="empty">读取失败：${esc(err.message)}</p>`;
    } finally {
      if (curReq === overviewReqId) {
        if (cardsEl) cardsEl.classList.remove("is-loading-cards");
        if (reloadBtn) { reloadBtn.disabled = false; reloadBtn.classList.remove("is-loading"); reloadBtn.textContent = "刷新数据"; }
      }
    }
  }

  function updateModeCardsUI(isHybridMode) {
    $$("#overviewModeCards .mode-card").forEach((card) => {
      card.classList.toggle("active", card.dataset.mode === (isHybridMode ? "kaggle_hybrid" : "local_full"));
    });
    const bLocal = $("#badgeLocalSub");
    const bHybrid = $("#badgeHybridSub");
    if (bLocal && bHybrid) {
      if (isHybridMode) {
        bHybrid.textContent = "当前启用";
        bHybrid.className = "mode-badge ready";
        bLocal.textContent = "点击切换";
        bLocal.className = "mode-badge pending";
      } else {
        bLocal.textContent = "当前启用";
        bLocal.className = "mode-badge ready";
        bHybrid.textContent = "点击切换";
        bHybrid.className = "mode-badge pending";
      }
    }
  }

  function renderKaggleStatus(k, isHybrid) {
    const card = $("#kaggleStatusCard");
    if (!card) return;
    if (!k) k = {};
    const dot = $("#kgDot");
    const statusText = $("#kgStatusText");
    const devTag = $("#kgDeviceTag");
    const countdown = $("#kgCountdown");
    const epNote = $("#kgEndpointNote");
    const tip = $("#kgQuotaTip");
    const btnCold = $("#btnColdStartKaggle");
    const btnShutdown = $("#btnShutdownKaggle");

    // 同步控制台元素
    const rvcDot = $("#rvcKgDot");
    const rvcStatusText = $("#rvcKgStatusText");
    const rvcDevTag = $("#rvcKgDeviceTag");
    const rvcCountdown = $("#rvcKgCountdown");
    const btnRvcCold = $("#btnRvcColdStartKaggle");
    const btnRvcShutdown = $("#btnRvcShutdownKaggle");

    if (devTag) devTag.style.display = "none";
    if (rvcDevTag) rvcDevTag.style.display = "none";

    if (!isHybrid) {
      if (dot) dot.className = "status-dot offline";
      if (statusText) statusText.textContent = "当前处于本地一体化模式 (全部在本地运行)";
      if (countdown) countdown.textContent = "切换到 Kaggle 混合云模式可调用云端 GPU 算力";
      if (epNote) epNote.textContent = "";
      if (tip) tip.style.display = "none";
      if (btnCold) btnCold.style.display = "none";
      if (btnShutdown) btnShutdown.style.display = "none";
      if (rvcDot) rvcDot.className = "status-dot offline";
      if (rvcStatusText) rvcStatusText.textContent = "当前处于本地一体化模式";
      if (btnRvcCold) btnRvcCold.style.display = "none";
      if (btnRvcShutdown) btnRvcShutdown.style.display = "none";
      return;
    }

    if (tip) tip.style.display = "inline-block";

    // 检查是否处于开机建联中（由后端 _kaggle_starting_since / kernel_status=RUNNING 持续锁定，避免重复点击）
    const memStarting = Number(state.kgStartingAt || 0) > Date.now() - 360000 && !(k && k.online);
    const isStarting = Boolean((k && k.is_starting) || memStarting);

    if (isStarting) {
      if (dot) dot.className = "status-dot pending";
      if (statusText) statusText.textContent = "Kaggle 云端正在开机初始化中...";
      if (countdown) countdown.textContent = (k && k.tip) || "云端实例正在启动与加载服务，预计 2~4 分钟就绪（后台静默探测中）";
      if (epNote) epNote.textContent = "";
      if (btnCold) {
        btnCold.style.display = "inline-flex";
        btnCold.disabled = true;
        btnCold.textContent = "开机中...";
      }
      if (btnShutdown) btnShutdown.style.display = "none";

      if (rvcDot) {
        rvcDot.className = "status-dot pending";
        if (rvcStatusText) rvcStatusText.textContent = "Kaggle 云端正在开机初始化中...";
        if (rvcCountdown) rvcCountdown.textContent = "云端实例正在启动与建联，请耐心等待，无需重复点击...";
        if (btnRvcCold) {
          btnRvcCold.style.display = "inline-flex";
          btnRvcCold.disabled = true;
          btnRvcCold.textContent = "开机中...";
        }
        if (btnRvcShutdown) btnRvcShutdown.style.display = "none";
      }

      // 保证切回插件页面时由串行轮询自动接力
      if (typeof state.ensureKaggleStartPolling === "function") {
        state.ensureKaggleStartPolling();
      }
      return;
    }

    if (!k.configured) {
      if (dot) dot.className = "status-dot offline";
      if (statusText) statusText.textContent = "Kaggle 端点未配置";
      if (countdown) countdown.textContent = "请在插件设置中填入 Kaggle 隧道地址 (trycloudflare.com)";
      if (epNote) epNote.textContent = "";
      if (btnCold) btnCold.style.display = "none";
      if (btnShutdown) btnShutdown.style.display = "none";

      if (rvcDot) {
        rvcDot.className = "status-dot offline";
        if (rvcStatusText) rvcStatusText.textContent = "Kaggle 端点未配置";
        if (rvcCountdown) rvcCountdown.textContent = "请在插件设置中填入 Kaggle 隧道地址";
        if (btnRvcCold) btnRvcCold.style.display = "none";
        if (btnRvcShutdown) btnRvcShutdown.style.display = "none";
      }
    } else if (k.online) {
      state.kgStartingAt = 0;
      if (dot) dot.className = "status-dot online";
      if (statusText) statusText.textContent = "Kaggle 云端已连上";
      if (countdown) {
        if (k.is_busy) {
          countdown.innerHTML = `看门狗倒计时：<b style="color:var(--accent,#8b5cf6);">GPU 任务推理中</b>（倒计时已暂停，任务完成后重置为 10 分钟）`;
        } else {
          countdown.innerHTML = `看门狗空闲关机倒计时：<b style="color:var(--ok,#10b981);">${esc(k.countdown_text || "10分钟")}</b>（新任务将重置）`;
        }
      }
      if (epNote) epNote.textContent = "";
      if (btnCold) btnCold.style.display = "none";
      if (btnShutdown) {
        btnShutdown.style.display = "inline-flex";
        btnShutdown.disabled = false;
        btnShutdown.textContent = "远程关机";
      }

      if (rvcDot) {
        rvcDot.className = "status-dot online";
        if (rvcStatusText) rvcStatusText.textContent = "Kaggle 云端已连上";
        if (rvcCountdown) {
          if (k.is_busy) {
            rvcCountdown.innerHTML = `看门狗倒计时：<b style="color:var(--accent,#8b5cf6);">GPU 任务推理中</b>（倒计时已暂停，任务完成后重置为 10 分钟）`;
          } else {
            rvcCountdown.innerHTML = `看门狗空闲关机倒计时：<b style="color:var(--ok,#10b981);">${esc(k.countdown_text || "10分钟")}</b>（新任务将重置）`;
          }
        }
        if (btnRvcCold) btnRvcCold.style.display = "none";
        if (btnRvcShutdown) {
          btnRvcShutdown.style.display = "inline-flex";
          btnRvcShutdown.disabled = false;
          btnRvcShutdown.textContent = "远程关机";
        }
      }
    } else {
      if (dot) dot.className = "status-dot offline";
      if (statusText) statusText.textContent = k.status_text || "Kaggle 处于休眠关机状态";
      if (countdown) countdown.textContent = k.tip || "可点击远程开机唤醒；当前翻唱将自动由本地设备兜底";
      if (epNote) epNote.textContent = "";
      if (btnCold) {
        btnCold.style.display = "inline-flex";
        btnCold.disabled = false;
        btnCold.textContent = "远程开机";
      }
      if (btnShutdown) btnShutdown.style.display = "none";

      if (rvcDot) {
        rvcDot.className = "status-dot offline";
        if (rvcStatusText) rvcStatusText.textContent = k.status_text || "Kaggle 处于休眠关机状态";
        if (rvcCountdown) rvcCountdown.textContent = k.tip || "可点击远程开机唤醒云端 GPU 实例";
        if (btnRvcCold) {
          btnRvcCold.style.display = "inline-flex";
          btnRvcCold.disabled = false;
          btnRvcCold.textContent = "远程开机";
        }
        if (btnRvcShutdown) btnRvcShutdown.style.display = "none";
      }
    }
    if (tip) {
      if (k.quota_info && k.quota_info.gpu_quota_text) {
        tip.innerHTML = `每周 GPU 配额剩余：<b style="color:var(--accent,#8b5cf6);">${esc(k.quota_info.gpu_quota_text)}</b> (10分钟无任务自动关机防扣费)`;
      } else if (k.quota_tip) {
        tip.textContent = k.quota_tip;
      }
    }
  }

  function renderOverviewTasks(d) {
    const rows = (d.recent_tasks || []).slice(0, 6);
    if (!rows.length) { $("#ovTasks").innerHTML = `<p class="empty">暂无任务记录</p>`; return; }
    $("#ovTasks").innerHTML = rows.map((t) => {
      const isCloud = t.engine && t.engine.includes("云端");
      const engineBadge = `<span class="tag sm ${isCloud ? 'cloud' : 'local'}" style="margin-left:4px;">${esc(t.engine || '本地')}</span>`;
      return `
      <div class="row">
        <div class="r-main">
          <div class="r-title">${esc(t.song || t.id)}</div>
          <div class="r-sub">${esc([t.artist, t.model].filter(Boolean).join(" · ") || "-")} · ${fmtTime(t.created_at)} · ${fmtDuration(t.duration)}</div>
        </div>
        <div style="display:flex; align-items:center;">
          ${statusTag(t.status)}
          ${engineBadge}
        </div>
      </div>`;
    }).join("");
  }

  /* ---------------- 任务 ---------------- */
  function _resetCurrentHostCache() {
    const host = $("#paneCurrent");
    if (host) {
      host.dataset.lastCoverSig = "";
      host.dataset.lastTtsSig = "";
      host.dataset.activeView = "";
    }
  }

  function switchTaskBiz(biz) {
    state.taskBiz = biz === "tts" ? "tts" : "rvc";
    state.taskLoadSeq = (state.taskLoadSeq || 0) + 1;
    stopActivePolling();
    _resetCurrentHostCache();
    $$("#taskBizSeg .seg-item").forEach((b) => b.classList.toggle("active", b.dataset.biz === state.taskBiz));
    const thead = $("#taskTable thead");
    if (thead) {
      thead.innerHTML = state.taskBiz === "tts"
        ? "<tr><th>时间</th><th>用户</th><th>请求文本</th><th>朗读内容</th><th>音色</th><th>时长</th><th>状态</th><th></th></tr>"
        : "<tr><th>时间</th><th>歌曲 / 歌手</th><th>用户</th><th>文件 / 输入</th><th>音色</th><th>时长</th><th>状态</th><th></th></tr>";
    }
    switchTaskMode(state.taskMode);
  }

  async function loadTasks() {
    const reqBiz = state.taskBiz;
    const seq = (state.taskLoadSeq = (state.taskLoadSeq || 0) + 1);
    $("#tasksHint").innerHTML = `<span class="spin-ring" aria-hidden="true"></span> 正在加载历史任务...`;
    if (reqBiz === "tts") {
      try {
        const d = await api("tts/tasks", { params: { limit: 200 } });
        if (state.taskLoadSeq !== seq || state.taskBiz !== reqBiz) return;
        state.ttsItems = d.items || [];
        state.ttsSnap = d;
        $("#tasksHint").textContent = `共 ${d.total || 0} 条 · 队列上限 ${d.queue_limit ?? 3}`;
        state.taskPage = 1;
        renderTasks();
      } catch (err) {
        if (state.taskBiz === reqBiz) $("#tasksHint").textContent = "读取失败：" + err.message;
      }
      return;
    }
    try {
      const d = await api("tasks", { params: { limit: 200 } });
      if (state.taskLoadSeq !== seq || state.taskBiz !== reqBiz) return;
      state.taskItems = d.items || [];
      $("#tasksHint").textContent = `共 ${d.total || 0} 条`;
      state.taskPage = 1;
      renderTasks();
    } catch (err) {
      if (state.taskBiz === reqBiz) $("#tasksHint").textContent = "读取失败：" + err.message;
    }
  }

  function filteredTasks() {
    const kw = ($("#taskSearch").value || "").trim().toLowerCase();
    return state.taskItems.filter((t) => {
      if (state.taskStatus !== "all" && t.status !== state.taskStatus) return false;
      if (!kw) return true;
      return [t.song, t.artist, t.id, t.model, t.input_name, t.output_name]
        .some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  function renderTasks() {
    if (state.taskBiz === "tts") return renderTtsHistory();
    const rows = filteredTasks();
    const size = state.taskPageSize;
    const pages = Math.max(1, Math.ceil(rows.length / size));
    if (state.taskPage > pages) state.taskPage = pages;
    const start = (state.taskPage - 1) * size;
    const pageRows = rows.slice(start, start + size);

    $("#taskTable tbody").innerHTML = pageRows.map((t) => {
      const isCloud = t.engine && t.engine.includes("云端");
      const engineBadge = `<span class="tag sm ${isCloud ? 'cloud' : 'local'}" style="margin-left:5px;">${esc(t.engine || '本地')}</span>`;
      return `
      <tr>
        <td>${fmtTime(t.created_at)}</td>
        <td><div class="t-title">${esc(t.song || t.input_name || "-")}</div>
          <div class="t-sub">${esc(t.artist || "歌手未知")}</div></td>
        <td><div class="t-title">${esc(t.user_name || t.user || "-")}</div>
          <div class="t-sub">${esc(t.user || "")}</div></td>
        <td><div class="t-file">${esc(t.output_name || t.input_name || t.id)}</div></td>
        <td>${esc(t.model || "-")}</td>
        <td><div class="t-title">${fmtDuration(t.duration)}</div>${t.elapsed ? `<div class="t-sub" title="翻唱耗时">耗时 ${Number(t.elapsed).toFixed(1)}s</div>` : ''}</td>
        <td><div style="display:flex;align-items:center;">${statusTag(t.status)}${engineBadge}</div></td>
        <td class="actions"><button class="btn ghost sm" data-task="${esc(t.id)}" type="button">详情</button></td>
      </tr>`;
    }).join("");
    $("#taskEmpty").style.display = rows.length ? "none" : "block";

    const range = $("#taskRange");
    if (range) {
      range.textContent = rows.length
        ? `第 ${start + 1}–${start + pageRows.length} 条，共 ${rows.length} 条`
        : "共 0 条";
    }
    renderTaskPager(pages);
  }

  function renderTtsHistory() {
    const kw = ($("#taskSearch").value || "").trim().toLowerCase();
    const rows = state.ttsItems.filter((t) => !kw || [t.user, t.request, t.speak, t.id, t.model]
      .some((v) => String(v || "").toLowerCase().includes(kw)));
    const size = state.taskPageSize;
    const pages = Math.max(1, Math.ceil(rows.length / size));
    if (state.taskPage > pages) state.taskPage = pages;
    const start = (state.taskPage - 1) * size;
    const pageRows = rows.slice(start, start + size);

    $("#taskTable tbody").innerHTML = pageRows.map((t) => `
      <tr>
        <td>${fmtTime(t.created_at)}</td>
        <td><div class="t-title">${esc(t.user_name || t.user || "-")}</div>
          <div class="t-sub">${esc(t.user || "")}</div></td>
        <td><div class="t-clip" title="${esc(t.request)}">${esc(t.request || "-")}</div></td>
        <td><div class="t-clip" title="${esc(t.speak)}">${esc(t.speak || "-")}</div></td>
        <td>${esc(t.model || "-")}</td>
        <td><div class="t-title">${fmtDuration(t.duration)}</div>${t.elapsed ? `<div class="t-sub" title="翻唱耗时">耗时 ${Number(t.elapsed).toFixed(1)}s</div>` : ''}</td>
        <td><div style="display:flex;align-items:center;">${statusTag("succeeded")}<span class="tag sm local" style="margin-left:5px;">本地</span></div></td>
        <td class="actions">${t.playable ? `<button class="btn ghost sm" data-tts-play="${esc(t.id)}" type="button">播放</button>` : ""}</td>
      </tr>`).join("");
    $("#taskEmpty").style.display = rows.length ? "none" : "block";

    const range = $("#taskRange");
    if (range) {
      range.textContent = rows.length
        ? `第 ${start + 1}–${start + pageRows.length} 条，共 ${rows.length} 条`
        : "共 0 条";
    }
    renderTaskPager(pages);
  }

  function syncClockAnchor(prevAnchor, prevKey, curKey, serverElapsedSec) {
    const serverMs = Math.max(0, Number(serverElapsedSec || 0) * 1000);
    if (!prevAnchor || curKey !== prevKey) return Date.now() - serverMs;
    const localMs = Date.now() - prevAnchor;
    if (Math.abs(serverMs - localMs) > 3000) return Date.now() - serverMs;
    return prevAnchor;
  }

  function anchorSecs(anchorAt) {
    return Math.max(0, Math.floor((Date.now() - (anchorAt || Date.now())) / 1000));
  }

  function getWaitAnchor(key, serverWaitedSec) {
    const k = String(key || "");
    const serverMs = Math.max(0, Number(serverWaitedSec || 0) * 1000);
    const prev = state.waitAnchors[k];
    const next = (!prev || Math.abs(serverMs - (Date.now() - prev)) > 3000)
      ? Date.now() - serverMs
      : prev;
    state.waitAnchors[k] = next;
    return next;
  }

  function renderTtsCurrent() {
    const host = $("#paneCurrent");
    if (!host) return;
    const snap = state.ttsSnap || {};
    const active = snap.active;
    const waiting = snap.waiting || [];
    const limit = snap.queue_limit == null ? 3 : snap.queue_limit;
    const regWaitTts = (snap.registry_waiting || []).filter((r) => r.type === "tts");

    const activeKey = active ? `tts:${active.id || active.label || active.text || ""}` : "";
    if (active) {
      state.activeAnchorAt = syncClockAnchor(state.activeAnchorAt, state.activeClockKey, activeKey, active.elapsed);
      state.activeClockKey = activeKey;
    } else {
      state.activeAnchorAt = 0;
      state.activeClockKey = "";
      state.activeLastSec = -1;
    }
    const waitList = waiting.map((w, i) => ({
      key: `tts:w:${w.id || w.label || w.text || i}`,
      waited: w.waited || 0,
    })).concat(regWaitTts.map((r, i) => ({
      key: `tts:r:${r.task_id || r.label || i}`,
      waited: r.waited || 0,
    })));
    waitList.forEach((item) => getWaitAnchor(item.key, item.waited));

    const sig = JSON.stringify({
      has_active: Boolean(active),
      id: active ? (active.id || active.label || active.text) : null,
      stage: active ? active.stage : null,
      waiting_keys: waitList.map((x) => x.key),
    });
    if (host.dataset.activeView === "tts" && host.dataset.lastTtsSig === sig) {
      if (active || waitList.length) startElapsedTicker();
      else stopElapsedTicker();
      return;
    }
    host.dataset.activeView = "tts";
    host.dataset.lastCoverSig = "";
    host.dataset.lastTtsSig = sig;

    let nowCard;
    if (active) {
      const stageStep = { "提取台词": 1, "TTS 生成与变声": 2, "TTS 原音色生成": 2, "发送语音": 4 };
      const cur = Number(stageStep[active.stage] || 1);
      const steps = [["提取台词", 1], ["TTS 生成", 2], ["RVC 变声", 3], ["发送语音", 4]]
        .map(([name, idx]) => [name, idx < cur ? "done" : idx === cur ? "cur" : "todo"]);
      const stepsHtml = steps.map(([name, st]) =>
        `<li class="${st}"><span class="s-ico">${st === "done" ? "✓" : st === "cur" ? "●" : "○"}</span>${esc(name)}</li>`).join("");
      const curSecs = anchorSecs(state.activeAnchorAt);
      state.activeLastSec = curSecs;
      nowCard = `
        <article class="card">
          <div class="card-head">
            <h3>正在处理</h3>
            <div style="display:flex;align-items:center;gap:6px;">
              <span class="tag sm local">本地</span>
              <span class="tag run">${esc(active.stage || "处理中")}</span>
            </div>
          </div>
          <div class="now-row">
            <div>
              <div class="t-title">${esc(active.label || active.text || "-")}</div>
              <div class="t-sub">${esc(active.user_name || active.user || "")} · ${esc(active.model || "TTS 语音")}</div>
            </div>
            <div class="hint" id="nowElapsed">已运行 ${curSecs} 秒</div>
          </div>
          <ul class="steps">${stepsHtml}</ul>
          <p class="hint">台词经二次 LLM 提取后送 RVC 变声；反应文字会先于语音发出。</p>
        </article>`;
    } else {
      nowCard = `<article class="card">
        <div class="card-head"><h3>正在处理</h3><span class="tag">空闲</span></div>
        <p class="hint">当前没有正在进行的 TTS 任务。有人排队时这里会显示进度。</p></article>`;
    }

    const queueRows = waiting.map((w, i) => {
      const wk = `tts:w:${w.id || w.label || w.text || i}`;
      const wa = getWaitAnchor(wk, w.waited);
      const ws = anchorSecs(wa);
      return `
      <li><span class="q-pos">#${w.position}</span>
        <span class="t-title">${esc(w.label || w.text || "-")}</span>
        <span class="t-sub">${esc(w.user_name || w.user || "")}</span>
        <span class="tag sm local" style="margin-left:auto;margin-right:8px;">本地</span>
        <span class="hint queue-waited" data-wait-key="${esc(wk)}" data-last-sec="${ws}">已等 ${ws} 秒</span></li>`;
    }).join("")
      + regWaitTts.map((r, i) => {
      const wk = `tts:r:${r.task_id || r.label || i}`;
      const wa = getWaitAnchor(wk, r.waited);
      const ws = anchorSecs(wa);
      return `
      <li><span class="q-pos">#${waiting.length + i + 1}</span>
        <span class="t-title">${esc(r.label || "-")}</span>
        <span class="t-sub">${esc(r.user || "")} · 准备中</span>
        <span class="tag sm local" style="margin-left:auto;margin-right:8px;">本地</span>
        <span class="hint queue-waited" data-wait-key="${esc(wk)}" data-last-sec="${ws}">已等 ${ws} 秒</span></li>`;
    }).join("");

    host.innerHTML = `
      <div class="now-grid">
        ${nowCard}
        <article class="card">
          <div class="card-head"><h3>排队中（${waiting.length + regWaitTts.length}/${limit}）</h3></div>
          ${queueRows ? `<ul class="queue">${queueRows}</ul>` : `<p class="hint">队列为空</p>`}
          <p class="hint">同时只处理一个 TTS 转换，其余按顺序等待；超过 ${limit} 条会跳过新请求。</p>
        </article>
      </div>`;
    if (active || waitList.length) startElapsedTicker();
    else stopElapsedTicker();
  }

  async function ttsPlayModal(id) {
    try {
      const d = await api("tts/audio", { params: { id } });
      $("#modalTitle").textContent = "TTS 回放";
      $("#modalContent").innerHTML = `
        <p class="f-hint">${esc(d.speak || "-")} · 触发用户 ${esc(d.user || "-")} · ${fmtDuration(d.duration)}${d.group_name || d.group_id ? ` · 群聊 ${esc(d.group_name || "")}（${esc(d.group_id)}）` : " · 私聊 / 控制台"}</p>
        <div id="ttsPlayerHost"></div>`;
      $("#modal").hidden = false;
      await mountPlayer($("#ttsPlayerHost"), {
        base64: d.audio_base64, mime: d.mime, name: d.name, duration: d.duration,
      });
    } catch (err) {
      toast("回放失败：" + err.message, 4200);
    }
  }

  function renderTaskPager(pages) {
    const box = $("#taskPages");
    if (!box) return;
    const cur = state.taskPage;
    const items = [];
    const add = (p) => items.push(
      `<button data-page="${p}" class="${p === cur ? "active" : ""}" type="button">${p}</button>`);
    if (pages <= 7) {
      for (let i = 1; i <= pages; i += 1) add(i);
    } else {
      add(1);
      if (cur > 3) items.push('<span class="gap">…</span>');
      for (let i = Math.max(2, cur - 1); i <= Math.min(pages - 1, cur + 1); i += 1) add(i);
      if (cur < pages - 2) items.push('<span class="gap">…</span>');
      add(pages);
    }
    box.innerHTML = items.join("");
    const prev = $("#taskPrev");
    const next = $("#taskNext");
    if (prev) prev.disabled = cur <= 1;
    if (next) next.disabled = cur >= pages;
  }

  function showTaskDetail(id) {
    const t = (state.taskItems || []).find((x) => String(x.id) === String(id))
      || (state.ttsItems || []).find((x) => String(x.id) === String(id));
    if (!t) {
      toast("未找到对应任务详情（ID: " + id + "）", 3500);
      return;
    }

    function fmtElapsedDetail(item) {
      if (!item) return "";
      const timings = item.timings || {};
      const parts = [];
      if (timings.separate) parts.push(`分离 ${Number(timings.separate).toFixed(1)}s`);
      if (timings.rvc) parts.push(`变声 ${Number(timings.rvc).toFixed(1)}s`);
      if (timings.trim) parts.push(`裁剪 ${Number(timings.trim).toFixed(1)}s`);

      let totalSec = item.elapsed;
      if (totalSec == null && (timings.separate || timings.rvc)) {
        totalSec = (timings.separate || 0) + (timings.rvc || 0) + (timings.trim || 0);
      }
      if (totalSec == null || totalSec <= 0) return "";

      const m = Math.floor(totalSec / 60);
      const s = Math.round(totalSec % 60);
      const totalStr = m > 0 ? `${m}分${s}秒` : `${Number(totalSec).toFixed(1)}秒`;
      if (parts.length > 0) {
        return `${totalStr} (${parts.join(" · ")})`;
      }
      return totalStr;
    }

    const rows = [
      ["任务号", t.id], ["状态", t.status], ["算力节点", t.engine || "本地"], ["来源", t.source],
      ["歌曲 / 输入", t.song || t.input_name], ["音色", t.model],
      ["升降调", t.pitch == null ? "-" : (Number(t.pitch) > 0 ? `+${t.pitch}` : t.pitch) + " 半音"],
      ["人声分离模型", t.sep_model || ""],
      ["翻唱耗时", fmtElapsedDetail(t)],
      ["触发群聊", t.group_name ? `${t.group_name}（${t.group_id}）` : (t.group_id || "私聊 / 控制台")],
      ["输出时长", fmtDuration(t.duration)], ["请求时长", t.requested],
      ["片段长度", t.clip], ["模式", t.mode], ["时间", fmtTime(t.created_at)],
      ["错误", t.error || "-"],
    ];
    $("#modalTitle").textContent = "任务详情";
    $("#modalContent").innerHTML = `<dl class="meta">${rows
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      <div id="taskAudio" class="task-audio"></div>`;
    $("#modal").hidden = false;

    const host = $("#taskAudio");
    if (t.status !== "succeeded") {
      host.innerHTML = `<p class="hint">该任务没有可播放的成品</p>`;
      return;
    }
    host.innerHTML = `<p class="hint">正在加载音频…</p>`;
    api("task_audio", { params: { id: t.id } })
      .then((d) => {
        host.innerHTML = "";
        return mountPlayer(host, {
          base64: d.audio_base64, mime: d.mime, name: d.name, duration: d.duration,
        });
      })
      .catch((err) => {
        host.innerHTML = `<p class="hint">无法播放：${esc(err.message)}</p>`;
      });
  }

  /* ---------------- 当前任务 / 队列 ---------------- */
  function stopElapsedTicker() {
    if (state.elapsedTimer) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }
  }

  function startElapsedTicker() {
    stopElapsedTicker();
    state.elapsedTimer = setInterval(() => {
      if (state.tab !== "tasks" || state.taskMode !== "current") {
        stopElapsedTicker();
        return;
      }
      const el = $("#nowElapsed");
      if (el && state.activeAnchorAt) {
        const secs = anchorSecs(state.activeAnchorAt);
        if (secs !== state.activeLastSec) {
          state.activeLastSec = secs;
          el.textContent = `已运行 ${secs} 秒`;
        }
      }
      $$(".queue-waited").forEach((qEl) => {
        const anchor = state.waitAnchors[qEl.dataset.waitKey || ""];
        if (!anchor) return;
        const ws = anchorSecs(anchor);
        if (String(ws) !== qEl.dataset.lastSec) {
          qEl.dataset.lastSec = String(ws);
          qEl.textContent = `已等 ${ws} 秒`;
        }
      });
    }, 200);
  }

  function pauseAllAudio() {
    (state.audioEls || []).forEach((a) => {
      try {
        a.pause();
      } catch (e) {
        /* ignore */
      }
    });
    state.audioEls = [];
  }

  function stopActivePolling() {
    if (state.activeTimer) {
      clearTimeout(state.activeTimer);
      state.activeTimer = null;
    }
  }

  async function loadActiveTasks() {
    const host = $("#paneCurrent");
    if (!host) return;
    const reqBiz = state.taskBiz;
    const seq = (state.taskLoadSeq = (state.taskLoadSeq || 0) + 1);
    if (!host.innerHTML.trim()) {
      setLoading(host, reqBiz === "tts" ? "正在同步 TTS 实时队列..." : "正在同步 RVC 实时任务...");
    }
    if (reqBiz === "tts") {
      try {
        const d = await api("tts/tasks", { params: { limit: 200 } });
        if (state.taskLoadSeq !== seq || state.taskBiz !== reqBiz) return;
        state.ttsItems = d.items || [];
        state.ttsSnap = d;
      } catch (err) { /* 保持上一次快照 */ }
      if (state.taskLoadSeq === seq && state.taskBiz === reqBiz && state.taskMode === "current") {
        renderTtsCurrent();
      }
      if (state.tab === "tasks" && state.taskMode === "current" && state.taskBiz === reqBiz) {
        stopActivePolling();
        state.activeTimer = setTimeout(loadActiveTasks, 3000);
      }
      return;
    }
    try {
      const d = await api("active_tasks");
      if (state.taskLoadSeq !== seq || state.taskBiz !== reqBiz) return;
      if (state.taskMode === "current") {
        renderCurrent(d);
      }
    } catch (err) {
      if (state.taskBiz === reqBiz) host.innerHTML = `<p class="empty">读取失败：${esc(err.message)}</p>`;
    }
    if (state.tab === "tasks" && state.taskMode === "current" && state.taskBiz === reqBiz) {
      stopActivePolling();
      state.activeTimer = setTimeout(loadActiveTasks, 3000);
    }
  }

  function renderCurrent(d) {
    const host = $("#paneCurrent");
    const active = d.active;
    const waiting = d.waiting || [];
    const limit = d.limit == null ? 0 : d.limit;

    const regWaitCover = (d.registry_waiting || []).filter((r) => r.type === "cover");
    const activeKey = active ? `rvc:${active.id || active.song || active.label || ""}` : "";
    if (active) {
      state.activeAnchorAt = syncClockAnchor(state.activeAnchorAt, state.activeClockKey, activeKey, active.elapsed);
      state.activeClockKey = activeKey;
    } else {
      state.activeAnchorAt = 0;
      state.activeClockKey = "";
      state.activeLastSec = -1;
    }
    const waitList = waiting.map((w, i) => ({
      key: `rvc:w:${w.id || w.song || w.label || i}`,
      waited: w.waited || 0,
    })).concat(regWaitCover.map((r, i) => ({
      key: `rvc:r:${r.task_id || r.label || i}`,
      waited: r.waited || 0,
    })));
    waitList.forEach((item) => getWaitAnchor(item.key, item.waited));

    const sig = JSON.stringify({
      has_active: Boolean(active),
      id: active ? (active.id || active.song || active.label) : null,
      stage: active ? active.stage : null,
      step: active ? active.step : null,
      waiting_keys: waitList.map((x) => x.key),
    });
    if (host && host.dataset.activeView === "rvc" && host.dataset.lastCoverSig === sig) {
      if (active || waitList.length) startElapsedTicker();
      else stopElapsedTicker();
      return;
    }
    if (host) {
      host.dataset.activeView = "rvc";
      host.dataset.lastTtsSig = "";
      host.dataset.lastCoverSig = sig;
    }

    let nowCard;
    if (active) {
      // 步骤：1 取歌 2 人声分离 3 变声推理 4 发送；网关有信号时用真实阶段
      const cur = Number(active.step || 2);
      const steps = [
        ["取歌与音源准备", 1],
        ["人声分离", 2],
        ["变声推理", 3],
        ["发送音频", 4],
      ].map(([name, idx]) => [name, idx < cur ? "done" : idx === cur ? "cur" : "todo"]);
      const stepsHtml = steps.map(([name, st]) =>
        `<li class="${st}"><span class="s-ico">${st === "done" ? "✓" : st === "cur" ? "●" : "○"}</span>${esc(name)}</li>`).join("");
      const curSecs = anchorSecs(state.activeAnchorAt);
      state.activeLastSec = curSecs;
      const isCloud = (active.engine && active.engine.includes("云端")) || (state.execMode === "kaggle_hybrid");
      const engineTag = `<span class="tag sm ${isCloud ? 'cloud' : 'local'}" style="margin-right:6px;">${esc(active.engine || (isCloud ? '云端' : '本地'))}</span>`;
      nowCard = `
        <article class="card">
          <div class="card-head">
            <h3>正在处理</h3>
            <div style="display:flex;align-items:center;">
              ${engineTag}
              <span class="tag run">${esc(active.stage || "处理中")}</span>
            </div>
          </div>
          <div class="now-row">
            <div>
              <div class="t-title">${esc(active.song || active.label || "-")}</div>
              <div class="t-sub">${esc(active.artist || "歌手未知")} · ${esc(active.model || "")} · 触发用户 ${esc(active.user_name || active.user || "-")}</div>
            </div>
            <div class="hint" id="nowElapsed">已运行 ${curSecs} 秒</div>
          </div>
          <ul class="steps">${stepsHtml}</ul>
          <p class="hint">${active.stage_from === "gateway"
            ? "步骤来自 Gateway 中间文件，实时反映进度。"
            : "Gateway 未提供分步信号，步骤按阶段估算。"}</p>
        </article>`;
    } else {
      nowCard = `<article class="card">
        <div class="card-head"><h3>正在处理</h3><span class="tag">空闲</span></div>
        <p class="hint">当前没有正在进行的翻唱。有人在排队时这里会显示进度。</p></article>`;
    }

    const queueRows = waiting.map((w, i) => {
      const isWCloud = (w.engine && w.engine.includes("云端")) || (state.execMode === "kaggle_hybrid");
      const wk = `rvc:w:${w.id || w.song || w.label || i}`;
      const wa = getWaitAnchor(wk, w.waited);
      const ws = anchorSecs(wa);
      return `
      <li><span class="q-pos">#${w.position}</span>
        <span class="t-title">${esc(w.song || w.label || "-")}</span>
        <span class="t-sub">${esc(w.user_name || w.user || "")}</span>
        <span class="tag sm ${isWCloud ? 'cloud' : 'local'}" style="margin-left:auto;margin-right:8px;">${esc(w.engine || (isWCloud ? '云端' : '本地'))}</span>
        <span class="hint queue-waited" data-wait-key="${esc(wk)}" data-last-sec="${ws}">已等 ${ws} 秒</span></li>`;
    }).join("")
      + regWaitCover.map((r, i) => {
      const isRCloud = (r.engine && r.engine.includes("云端")) || (state.execMode === "kaggle_hybrid");
      const wk = `rvc:r:${r.task_id || r.label || i}`;
      const wa = getWaitAnchor(wk, r.waited);
      const ws = anchorSecs(wa);
      return `
      <li><span class="q-pos">#${waiting.length + i + 1}</span>
        <span class="t-title">${esc(r.label || "-")}</span>
        <span class="t-sub">${esc(r.user || "")} · 准备中</span>
        <span class="tag sm ${isRCloud ? 'cloud' : 'local'}" style="margin-left:auto;margin-right:8px;">${esc(r.engine || (isRCloud ? '云端' : '本地'))}</span>
        <span class="hint queue-waited" data-wait-key="${esc(wk)}" data-last-sec="${ws}">已等 ${ws} 秒</span></li>`;
    }).join("");

    host.innerHTML = `
      <div class="now-grid">
        ${nowCard}
        <article class="card">
          <div class="card-head"><h3>排队中（${waiting.length + regWaitCover.length}/${limit}）</h3></div>
          ${queueRows ? `<ul class="queue">${queueRows}</ul>` : `<p class="hint">队列为空</p>`}
          <p class="hint">同时只处理一个翻唱，其余按顺序等待；超过 ${limit} 人会提示稍后再试。</p>
        </article>
      </div>`;
    if (active || waitList.length) startElapsedTicker();
    else stopElapsedTicker();
  }

  function switchTaskMode(mode) {
    state.taskMode = mode;
    $$("#taskModeSeg .seg-item").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    const cur = $("#paneCurrent");
    const his = $("#paneHistory");
    if (cur) cur.hidden = mode !== "current";
    if (his) his.hidden = mode !== "history";
    const bar = $("#taskHistoryBar");
    const pager = $("#taskPagerBar");
    if (bar) bar.hidden = mode !== "history";
    if (pager) pager.hidden = mode !== "history";
    if (mode === "history") {
      stopActivePolling();
      loadTasks();
    } else {
      _resetCurrentHostCache();
      loadActiveTasks();
    }
  }

  /* ---------------- 模型 ---------------- */
  async function loadModels() {
    $("#modelsHint").innerHTML = `<span class="spin-ring" aria-hidden="true"></span> 正在加载模型列表...`;
    if (!$("#modelRows").innerHTML.trim()) {
      setLoading("#modelRows", "正在同步网关音色与配置...");
      $("#modelEmpty").style.display = "none";
    }
    try {
      const d = await api("models");
      state.models = d;
      try {
        const cfg = await api("config");
        const vals = cfg.values || {};
        state.modelEdits = {
          cover: String(vals["rvc.models.rvc_default_model"] || "").replace(/\.pth$/i, ""),
          tts: String(vals["tts.model.tts_rvc_model"] || "").replace(/\.pth$/i, ""),
        };
      } catch (e) { state.modelEdits = { cover: "", tts: "" }; }
      $("#modelsHint").textContent = d.gateway_ok
        ? `${(d.models || []).length} 个模型${d.default_missing ? " · 默认音色不在列表中" : ""}`
        : "Gateway 不可用：" + (d.gateway_error || "");
      $("#modelsDir").textContent = d.dir ? `Gateway 模型目录：${d.dir}` : "";
      const def = String(d.default || "").replace(/\.pth$/i, "");
      $("#modelRows").innerHTML = (d.models || []).map((m) => `
        <div class="model-row ${m.name === def ? "selected" : ""}">
          <div class="model-name">${esc(m.filename)}
            <small>${m.alias ? esc(m.alias) : "未设置别名"}${m.missing_in_gateway ? " · Gateway 未发现" : ""}</small></div>
          <input class="input" data-alias="${esc(m.name)}" value="${esc(m.alias || "")}" placeholder="别名" />
          <select class="input" data-gender="${esc(m.name)}">
            ${["未知", "男声", "女声"].map((g) =>
              `<option value="${esc(g)}" ${genderCn(m.gender) === g ? "selected" : ""}>${esc(g)}</option>`).join("")}
          </select>
          <div class="sw-cell"><label class="switch" title="翻唱使用该音色"><input type="radio" name="coverDefault" data-cover="${esc(m.name)}"
            ${state.modelEdits.cover === m.name ? "checked" : ""} /><span class="track"></span></label><small class="sw-tag">翻唱</small></div>
          <div class="sw-cell"><label class="switch" title="TTS 使用该音色"><input type="radio" name="ttsDefault" data-tts="${esc(m.name)}"
            ${state.modelEdits.tts === m.name ? "checked" : ""} /><span class="track"></span></label><small class="sw-tag">TTS</small></div>
        </div>`).join("");
      $("#modelEmpty").style.display = (d.models || []).length ? "none" : "block";
    } catch (err) {
      $("#modelsHint").textContent = "读取失败：" + err.message;
    }
  }

  async function refreshModels() {
    const btn = $("#btnRefreshModels");
    btn.disabled = true; btn.textContent = "刷新中...";
    try {
      const d = await api("models/refresh", { method: "POST", body: {} });
      const miss = d.missing_default ? `；默认音色 ${d.missing_default} 不在新列表（已保留）` : "";
      toast(`刷新完成，${(d.models || []).length} 个模型${miss}`);
      await loadModels();
    } catch (err) {
      toast("刷新失败：" + err.message, 4200);
    } finally {
      btn.disabled = false; btn.textContent = "刷新模型列表";
    }
  }

  function genderCn(g) {
    return { male: "男声", female: "女声", unknown: "未知", "男声": "男声", "女声": "女声", "未知": "未知" }[g] || "未知";
  }

  async function saveModelsUnified() {
    const kw = [];
    const genders = {};
    const extra = {};
    let cover = "", tts = "";
    const coverChecked = document.querySelector('input[name="coverDefault"]:checked');
    const ttsChecked = document.querySelector('input[name="ttsDefault"]:checked');
    $$("#modelRows .model-row").forEach((row) => {
      const aliasInput = row.querySelector("[data-alias]");
      const modelName = aliasInput ? (aliasInput.dataset.alias || "") : "";
      const alias = aliasInput ? aliasInput.value.trim() : "";
      const genderSelect = row.querySelector("[data-gender]");
      const gender = genderSelect ? genderSelect.value : "未知";

      const filename = modelName.toLowerCase().endsWith(".pth") ? modelName : `${modelName}.pth`;
      const stem = modelName.replace(/\.pth$/i, "");

      kw.push(`${filename}|||${alias}`);
      genders[stem] = gender;
      if (coverChecked && coverChecked.dataset.cover === modelName) cover = stem;
      if (ttsChecked && ttsChecked.dataset.tts === modelName) tts = stem;
    });
    const values = {
      "rvc.models.rvc_models_keywords": kw,
      "rvc.models.rvc_default_model": cover,
      "tts.model.tts_rvc_model": tts ? `${tts}.pth` : "",
      "tuning.gender.model_genders": genders,
      "tuning.gender.model_genders_extra": extra,
    };
    const btn = $("#btnSaveModels");
    if (btn) { btn.disabled = true; btn.textContent = "保存中..."; }
    try {
      const d = await api("config/save", { method: "POST", body: { values } });
      toast(d.changed && d.changed.length ? `已保存 ${d.changed.length} 项设置` : "已保存（设置未变动）");
      if (d.errors && d.errors.length) toast("部分未保存：" + d.errors.join("；"), 5200);
      await loadModels();
    } catch (err) {
      toast("保存失败：" + err.message, 5200);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "保存设置"; }
    }
  }

  async function saveModelRow(name) {
    const aliasEl = $(`[data-alias="${CSS.escape(name)}"]`);
    const genderEl = $(`[data-gender="${CSS.escape(name)}"]`);
    try {
      await api("models/alias", { method: "POST", body: { model: name, alias: aliasEl.value.trim() } });
      await api("models/gender", { method: "POST", body: { model: name, gender: genderEl.value } });
      toast("已保存");
      loadModels();
    } catch (err) {
      toast("保存失败：" + err.message, 4200);
    }
  }

  async function setDefaultModel(name) {
    try {
      await api("models/default", { method: "POST", body: { model: name } });
      toast(`默认音色 → ${name}`);
      loadModels();
    } catch (err) {
      toast("设置失败：" + err.message, 4200);
    }
  }

  /* ---------------- TTS 控制台（CUSTOM-7） ---------------- */
  async function prepareTts() {
    try {
      const [prov, models] = await Promise.all([api("tts/providers"), api("models")]);
      const sel = $("#ttsProvider");
      const providers = prov.providers || [];
      const prevProv = sel ? sel.value : "";
      sel.innerHTML = providers.length
        ? providers.map((p) => `<option value="${esc(p.id)}">${esc(p.id || p.class)}${p.model ? `（${esc(p.model)}）` : ""}</option>`).join("")
        : `<option value="">（未配置 TTS 服务商）</option>`;
      const wanted = (prevProv && providers.some((p) => p.id === prevProv))
        ? prevProv
        : (prov.configured || prov.using || (providers[0] && providers[0].id) || "");
      if (wanted && providers.some((p) => p.id === wanted)) sel.value = wanted;
      if ($("#ttsProviderInfo")) $("#ttsProviderInfo").textContent = "";
      if ($("#ttsModelInfo")) $("#ttsModelInfo").textContent = "";
      const msel = $("#ttsModel");
      const prevModel = msel ? msel.value : "";
      const options = (models.models || []).filter((m) => !m.missing_in_gateway);
      msel.innerHTML = [
        `<option value="__raw__">使用 TTS 原音色（跳过 RVC 变声）</option>`,
        ...options.map((m) =>
          `<option value="${esc(m.name)}">${esc(m.alias || m.name)}${m.alias ? `（${esc(m.name)}）` : ""}</option>`),
      ].join("");
      const ttsDef = String(prov.tts_model || "").replace(/\.pth$/i, "");
      if (prevModel && (prevModel === "__raw__" || options.some((m) => m.name === prevModel))) {
        msel.value = prevModel;
      } else if (prov.tts_use_original_voice) {
        msel.value = "__raw__";
      } else if (ttsDef && options.some((m) => m.name === ttsDef)) {
        msel.value = ttsDef;
      } else if (options.length) {
        msel.value = options[0].name;
      } else {
        msel.value = "__raw__";
      }
      if (!state.pollers["#ttsStatus"]) {
        if (!providers.length) {
          $("#ttsStatus").textContent = "未配置 TTS 服务商（请先到 AstrBot 服务商配置添加）";
        } else {
          $("#ttsStatus").textContent = prov.tts_enable ? "就绪" : "就绪（提示：TTS 语音回复当前处于关闭状态，试听不受影响）";
        }
      }
    } catch (err) {
      if (!state.pollers["#ttsStatus"]) $("#ttsStatus").textContent = "初始化失败：" + err.message;
    }
  }

  async function runTts() {
    const btn = $("#ttsRun");
    const origText = btn ? btn.textContent : "生成语音";
    const textEl = $("#ttsText");
    const rawText = (textEl ? textEl.value : "").trim();
    const fallbackText = textEl ? (textEl.getAttribute("placeholder") || "").trim() : "";
    const text = rawText || fallbackText;
    if (!text) {
      if ($("#ttsStatus")) $("#ttsStatus").textContent = "请先输入文本";
      toast("请先输入要合成的文本内容", 3000);
      return;
    }
    const model = $("#ttsModel") ? $("#ttsModel").value : "";
    const skipRvc = model === "__raw__";
    if (!model) {
      if ($("#ttsStatus")) $("#ttsStatus").textContent = "请先选择音色模型";
      toast("请先选择音色模型（若模型列表为空，可选使用 TTS 原音色或先刷新模型）", 3500);
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "生成中...";
    }
    if ($("#ttsStatus")) $("#ttsStatus").textContent = "准备中...";
    if ($("#ttsResult")) $("#ttsResult").hidden = true;
    if ($("#ttsOutputs")) $("#ttsOutputs").innerHTML = "";
    try {
      const body = {
        text,
        provider_id: $("#ttsProvider") ? $("#ttsProvider").value : "",
        model: skipRvc ? "" : model,
        skip_rvc: skipRvc,
        reverb: $("#ttsReverb") ? $("#ttsReverb").value : "default",
        pitch: Number(($("#ttsPitch") ? $("#ttsPitch").value : 0) || 0),
        output_format: $("#ttsFormat") ? $("#ttsFormat").value : "wav",
      };
      toast(skipRvc ? "正在向后台提交 TTS 原始语音生成任务..." : "正在向后台提交 TTS 生成与变声任务...", 2500);
      const d = await api("tts/generate", { method: "POST", body });
      if ($("#ttsStatus")) $("#ttsStatus").textContent = skipRvc ? "已提交：TTS 生成原始语音（请稍候）" : "已提交：TTS 生成 → RVC 变声（请稍候）";
      pollJob(d.job_id, "#ttsStatus", (job) => {
        if (btn) {
          btn.disabled = false;
          btn.textContent = origText;
        }
        if (job.status === "succeeded") {
          toast(skipRvc ? "TTS 原音色语音生成完成！" : "TTS 语音生成与变声完成！", 4000);
          showOutputs(job.id, "#ttsOutputs", "#ttsResult");
        } else if (job.status === "failed") {
          toast("TTS 失败：" + (job.error || "未知错误"), 5200);
        }
      }, 1000);
    } catch (err) {
      if ($("#ttsStatus")) $("#ttsStatus").textContent = "失败：" + err.message;
      toast("TTS 提交失败：" + err.message, 5000);
      if (btn) {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }
  }

  /* ---------------- 音频效果（Reverb / echo） ---------------- */
  const AUDIO_KEYS_COVER = [
    "separation.reverb_enabled", "separation.reverb_wet", "separation.reverb_dry",
    "separation.reverb_room_size", "separation.reverb_decay", "separation.reverb_damping",
    "separation.reverb_pre_delay", "separation.reverb_width",
  ];
  const AUDIO_KEYS_TTS = [
    "tts.reverb.tts_reverb_enabled", "tts.reverb.tts_reverb_wet", "tts.reverb.tts_reverb_dry",
    "tts.reverb.tts_reverb_room_size", "tts.reverb.tts_reverb_decay", "tts.reverb.tts_reverb_damping",
    "tts.reverb.tts_reverb_pre_delay", "tts.reverb.tts_reverb_width",
  ];

  function bindFieldInputs(root) {
    $$("[data-key]", root).forEach((el) => {
      const onDirty = () => {
        state.dirty[el.dataset.key] = el.dataset.type === "bool" ? el.checked : el.value;
        const badge = el.closest(".field") && el.closest(".field").querySelector(".dirty");
        if (badge) badge.style.display = "inline";
      };
      el.addEventListener("input", onDirty);
      el.addEventListener("change", onDirty);
    });
  }

  function audioCard(title, desc, fields) {
    if (!fields.length) return "";
    return `<article class="card audio-card">
      <div class="card-head"><h3>${esc(title)}</h3></div>
      <p class="f-hint">${esc(desc)}</p>
      <div class="audio-card-body">${fields.map(renderField).join("")}</div>
    </article>`;
  }

  function applyAudioPreset(preset) {
    const map = {
      studio: {
        "separation.reverb_enabled": true,
        "separation.reverb_wet": 0.08,
        "separation.reverb_dry": 1.0,
        "separation.reverb_room_size": 0.35,
        "separation.reverb_decay": 0.8,
        "separation.reverb_damping": 0.60,
        "separation.reverb_pre_delay": 12,
        "separation.reverb_width": 0.60,
      },
      hall: {
        "separation.reverb_enabled": true,
        "separation.reverb_wet": 0.16,
        "separation.reverb_dry": 0.95,
        "separation.reverb_room_size": 0.75,
        "separation.reverb_decay": 1.6,
        "separation.reverb_damping": 0.45,
        "separation.reverb_pre_delay": 24,
        "separation.reverb_width": 0.85,
      },
      live: {
        "separation.reverb_enabled": true,
        "separation.reverb_wet": 0.12,
        "separation.reverb_dry": 1.0,
        "separation.reverb_room_size": 0.55,
        "separation.reverb_decay": 1.1,
        "separation.reverb_damping": 0.45,
        "separation.reverb_pre_delay": 16,
        "separation.reverb_width": 0.75,
      },
      off: {
        "separation.reverb_enabled": false,
      },
    };
    const target = map[preset];
    if (!target) return;
    Object.entries(target).forEach(([k, v]) => {
      state.dirty[k] = v;
      state.values[k] = v;
      const shortKey = k.split(".").pop();
      const els = Array.from(document.querySelectorAll(`[data-key="${k}"], [data-key="${shortKey}"]`));
      els.forEach((el) => {
        if (el.dataset.type === "bool" || el.type === "checkbox") {
          el.checked = Boolean(v);
        } else {
          el.value = v;
        }
        const badge = el.closest(".field") && el.closest(".field").querySelector(".dirty");
        if (badge) badge.style.display = "inline";
        el.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });
    // 同步控制台试听面板中的参数输入框
    if ($("#rvcReverbOn")) {
      $("#rvcReverbOn").checked = Boolean(target["separation.reverb_enabled"]);
      if (target["separation.reverb_wet"] != null && $("#rvcRevWet")) $("#rvcRevWet").value = target["separation.reverb_wet"];
      if (target["separation.reverb_dry"] != null && $("#rvcRevDry")) $("#rvcRevDry").value = target["separation.reverb_dry"];
      if (target["separation.reverb_room_size"] != null && $("#rvcRevRoom")) $("#rvcRevRoom").value = target["separation.reverb_room_size"];
      if (target["separation.reverb_decay"] != null && $("#rvcRevDecay")) $("#rvcRevDecay").value = target["separation.reverb_decay"];
      if (target["separation.reverb_damping"] != null && $("#rvcRevDamp")) $("#rvcRevDamp").value = target["separation.reverb_damping"];
      if (target["separation.reverb_pre_delay"] != null && $("#rvcRevPre")) $("#rvcRevPre").value = target["separation.reverb_pre_delay"];
      if (target["separation.reverb_width"] != null && $("#rvcRevWidth")) $("#rvcRevWidth").value = target["separation.reverb_width"];
    }
    const names = {
      studio: "录音棚 (通透温润·推荐)",
      hall: "音乐大厅 (空间宽广)",
      live: "现场演出 (Live 氛围)",
      off: "关闭混响 (原生干音)"
    };
    toast(`已切换为「${names[preset] || preset}」预设，点击右上角「保存音频效果设置」即可保存生效！`, 4000);
  }

  async function loadAudioTab() {
    $("#audioHint").innerHTML = `<span class="spin-ring" aria-hidden="true"></span> 正在加载音频效果设置...`;
    if (!$("#audioCards").innerHTML.trim()) setLoading("#audioCards", "正在同步混响配置...");
    try {
      await loadConfig();
      const byKey = {};
      state.fields.forEach((f) => { byKey[f.key] = f; });
      const pick = (keys) => keys.map((k) => byKey[k] || byKey[k.split(".").pop()]).filter(Boolean);
      let html = `
        <div class="audio-intro">
          <h4>✨ 什么是音频效果（Reverb 混响 / 空间声场）？</h4>
          <p>• <b>原声变声痛点</b>：原生 AI 变声后的人声处于完全无反射的干音状态，听感容易贴耳生硬、缺乏声场立体感。<br>
             • <b>Reverb 混响作用</b>：基于卷积算法为人声叠加自然的物理空间残响与声场宽度，为人声润色、消除毛刺感，更具通透动听的质感。<br>
             • <b>如何生效</b>：在下方开启混响开关并调节参数（或直接点击下方一键预设），点击右上角<b>「保存音频效果设置」</b>后立即生效，QQ 群翻唱与 WebUI 转换均会自动应用。<br>
             • <b>核心参数速览</b>：<b>Wet(湿音)</b> 残响音量，推荐 0.08~0.12 清澈不浑浊；<b>Dry(干音)</b> 原始人声音量保持 1.0；<b>Room Size</b> 空间宏大感；<b>Decay</b> 衰减时间（推荐 0.8s）；<b>Damping</b> 柔和度。</p>
          <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
            <span style="font-size: 12px; font-weight: 600; color: var(--text);">快捷一键预设：</span>
            <button class="btn sm" id="presetStudio" type="button" title="流行通俗人声，温润干净通透">录音棚 (通透温润·推荐)</button>
            <button class="btn sm" id="presetHall" type="button" title="空灵慢歌、抒情大歌">音乐大厅 (空间宽广)</button>
            <button class="btn sm" id="presetLive" type="button" title="现场 Live 氛围感">现场演出 (Live 氛围)</button>
            <button class="btn sm ghost" id="presetOff" type="button" title="关闭所有混响，输出干音原声">关闭混响 (原生干音)</button>
          </div>
        </div>
      `;
      html += audioCard(
        "歌曲翻唱 Reverb（卷积混响）",
        "只作用于翻唱成品的人声：合成 IR 卷积混响，8 个参数全部真实生效。开启后为人声增加空间感；关闭时输出与原流程一致。",
        pick(AUDIO_KEYS_COVER));
      html += audioCard(
        "TTS 语音 Reverb（独立配置）",
        "只作用于 TTS 转换后的语音，与歌曲翻唱的混响互不影响、可分别开关。",
        pick(AUDIO_KEYS_TTS));
      $("#audioCards").innerHTML = html;
      bindFieldInputs($("#audioCards"));
      const bStudio = $("#presetStudio");
      if (bStudio) bStudio.addEventListener("click", () => applyAudioPreset("studio"));
      const bHall = $("#presetHall");
      if (bHall) bHall.addEventListener("click", () => applyAudioPreset("hall"));
      const bLive = $("#presetLive");
      if (bLive) bLive.addEventListener("click", () => applyAudioPreset("live"));
      const bOff = $("#presetOff");
      if (bOff) bOff.addEventListener("click", () => applyAudioPreset("off"));
      $("#audioHint").textContent = "改完点「保存音频效果设置」；混响参数按次请求读取，无需重载插件。";
    } catch (err) {
      $("#audioHint").textContent = "读取失败：" + err.message;
    }
  }

  async function saveAudioCfg() {
    await saveConfig();
    await loadAudioTab();
  }

  /* ---------------- 设置 ---------------- */
  async function loadConfig() {
    $("#cfgHint").innerHTML = `<span class="spin-ring" aria-hidden="true"></span> 正在读取插件设置...`;
    if (!$("#cfgGroups").innerHTML.trim()) setLoading("#cfgGroups", "正在读取配置项与服务商列表...");
    try {
      const [d, models] = await Promise.all([api("config"), api("models").catch(() => ({}))]);
      state.fields = d.fields || [];
      state.values = d.values || {};
      state.sensitiveConfigured = d.sensitive_configured || {};
      state.dirty = {};
      state.needsReload = d.reload_required || [];
      // CUSTOM-9：默认音色 / TTS 音色模型下拉用网关实时列表（新模型刷新即见）
      const rows = (models && models.models ? models.models : []).filter((m) => !m.missing_in_gateway);
      state.gatewayModelStems = rows.map((m) => m.name);
      state.gatewayModelFiles = rows.map((m) => m.filename);
      $("#cfgHint").textContent = "全部配置项与 AstrBot 后台实时同步，改完点击「保存配置」即可（含 Cookie / Token）。";
      renderConfig();
    } catch (err) {
      $("#cfgHint").textContent = "读取失败：" + err.message;
    }
  }

  function selectCfgGroup(index) {
    $$("#cfgGroups .group").forEach((sec) => {
      sec.hidden = Number(sec.dataset.gi) !== index;
    });
    $$("#cfgGroupSeg .seg-item").forEach((b) => b.classList.toggle("active", Number(b.dataset.gi) === index));
    state.cfgGroup = index;
  }

  /* CUSTOM-7：设置页分类整合 —— 9 张 schema 卡片在页面上归并为 5 个大类，
     卡片内部保留原 schema 卡片名作小节标题；只影响本页展示，不改配置结构与键名。 */
  const CFG_CATEGORY = {
    basic: "基础与翻唱流程",
    selection: "基础与翻唱流程",
    architecture: "Kaggle 云端设置",
    rvc: "音色与推理",
    tuning: "音色与推理",
    separation: "音频输出与本地翻唱",
    local_audio: "音频输出与本地翻唱",
    tts: "TTS 语音回复",
    sources: "音源与高级",
    advanced: "音源与高级",
  };

  /* CUSTOM-7.2：把散在各卡片的通知类开关归并到「通知与播报」大类，
     并按 成功通知 / 过程通知 / 管理员告警 分小节；只影响本页展示，不改配置键。 */
  const CFG_KEY_CATEGORY = {
    "architecture.execution_mode": "基础与翻唱流程",
    "selection.smart.enable_llm_success_notify": "通知与播报",
    "selection.smart.enable_config_report": "通知与播报",
    "selection.smart.notify_progress": "通知与播报",
    "selection.smart.notify_retry": "通知与播报",
    "tuning.notify_persona_id": "通知与播报",
    "sources.alert_admin_on_cookie_expired": "通知与播报",
    "sources.cookie_alert_interval_hours": "通知与播报",
    "advanced.queue_position_notice": "通知与播报",
  };
  const CFG_NOTIFY_SUB = {
    "architecture.execution_mode": "算力架构与执行模式",
    "selection.smart.enable_llm_success_notify": "成功通知（二选一或同时开）",
    "selection.smart.enable_config_report": "成功通知（二选一或同时开）",
    "tuning.notify_persona_id": "成功通知（二选一或同时开）",
    "selection.smart.notify_progress": "过程通知",
    "selection.smart.notify_retry": "过程通知",
    "sources.alert_admin_on_cookie_expired": "管理员告警",
    "sources.cookie_alert_interval_hours": "管理员告警",
    "advanced.queue_position_notice": "排队通知",
  };

  /* 模型/别名/性别统一由「模型管理」页统一维护，插件设置中过滤掉 */
  const MODEL_EDITOR_FIELD_KEYS = new Set([
    "rvc.models.rvc_models_keywords",
    "tuning.gender.model_genders",
    "tuning.gender.model_genders_extra",
  ]);

  /* 混响效果统一由左侧「音频效果」专属选项卡维护，插件设置中过滤掉重复项 */
  const AUDIO_EFFECT_FIELD_KEYS = new Set([
    "separation.reverb_enabled", "separation.reverb_wet", "separation.reverb_dry",
    "separation.reverb_room_size", "separation.reverb_decay", "separation.reverb_damping",
    "separation.reverb_pre_delay", "separation.reverb_width",
    "tts.reverb.tts_reverb_enabled", "tts.reverb.tts_reverb_wet", "tts.reverb.tts_reverb_dry",
    "tts.reverb.tts_reverb_room_size", "tts.reverb.tts_reverb_decay", "tts.reverb.tts_reverb_damping",
    "tts.reverb.tts_reverb_pre_delay", "tts.reverb.tts_reverb_width",
  ]);

  function renderConfig() {
    const cats = new Map();
    state.fields.forEach((f) => {
      // 模型/别名/性别统一在「模型管理」页配置，插件设置中过滤掉
      if (MODEL_EDITOR_FIELD_KEYS.has(f.key) || f.key.startsWith("tuning.gender.model_genders.")) {
        return;
      }
      // 混响已整合到「音频效果」专属页，插件设置中过滤掉重复项
      if (AUDIO_EFFECT_FIELD_KEYS.has(f.key) ||
          f.key.startsWith("separation.reverb_") ||
          f.key.startsWith("tts.reverb.")) {
        return;
      }
      const prefix = String(f.key || "").split(".")[0];
      const cat = CFG_KEY_CATEGORY[f.key] || CFG_CATEGORY[prefix] || f.group || "其他";
      if (!cats.has(cat)) cats.set(cat, new Map());
      const inner = cats.get(cat);
      const g = CFG_KEY_CATEGORY[f.key] ? (CFG_NOTIFY_SUB[f.key] || f.group) : (f.group || "其他");
      if (!inner.has(g)) inner.set(g, []);
      inner.get(g).push(f);
    });
    const html = [];
    const names = [];
    cats.forEach((inner, cat) => {
      const gi = names.length;
      names.push(cat);
      const sections = [];
      inner.forEach((fields, g) => {
        if (g === cat) {
          sections.push(`<div class="group-body">${fields.map(renderField).join("")}</div>`);
        } else {
          sections.push(`<h4 class="cfg-sub">${esc(g)}</h4>
            <div class="group-body">${fields.map(renderField).join("")}</div>`);
        }
      });
      if (cat === "音频分离与输出" || cat === "音频输出与本地翻唱" || cat === "音频输出与文件发送") {
        sections.push(`
          <div class="audio-redirect-box" style="margin-top:16px;padding:14px 18px;background:linear-gradient(135deg,rgba(99,102,241,0.08),rgba(168,85,247,0.04));border:1px solid rgba(99,102,241,0.22);border-radius:12px;display:flex;align-items:center;justify-content:space-between;gap:12px;">
            <div>
              <h4 style="margin:0 0 4px;font-size:13px;color:var(--text);">✨ 人声混响 (Reverb) 与回声效果已统一整合</h4>
              <p style="margin:0;font-size:12px;color:var(--muted);line-height:1.5;">声学空间预设（录音棚 / 音乐大厅 / Live现场）、卷积残响试听及参数设置已移至左侧<b>「音频效果」</b>选项卡统一管理。</p>
            </div>
            <button class="btn sm primary" data-goto="audio" type="button" style="white-space:nowrap;">前往「音频效果」→</button>
          </div>
        `);
      }
      if (cat === "Kaggle 云端设置") {
        sections.push(`
          <div class="audio-redirect-box" style="margin-top:16px;padding:14px 18px;background:linear-gradient(135deg,rgba(139,92,246,0.08),rgba(59,130,246,0.04));border:1px solid rgba(139,92,246,0.22);border-radius:12px;">
            <h4 style="margin:0 0 6px;font-size:13.5px;color:var(--accent,#8b5cf6);">Kaggle 混合云与 API 远程开机配置指南</h4>
            <p style="margin:0;font-size:12px;color:var(--muted);line-height:1.6;">
              • <b>Kaggle API 访问令牌</b>：前往 Kaggle 网页端 <code>Settings → API</code> 点击 <b>Generate New Token</b>（或 Create Legacy API Key），将生成的单串 Token 或 kaggle.json 文本直接粘贴到上方输入框保存。配置后，系统在云端离线时可直接调用 Kaggle 官方接口远程分发 GPU 实例开机，无需手动常开网页！<br>
              • <b>云端端点</b>：填入你绑定的 Cloudflare 隧道域名（如 <code>https://rvc.yourdomain.com</code>）。<br>
              • <b>自动关机与防扣费</b>：云端内置看门狗，任务排队处理完后连续 10 分钟无任务将自动关机，节省免费算力时长。
            </p>
          </div>
        `);
      }
      html.push(`<section class="group" data-gi="${gi}"><h3>${esc(cat)}</h3>${sections.join("")}</section>`);
    });
    $("#cfgGroups").innerHTML = html.join("");
    const seg = $("#cfgGroupSeg");
    if (seg) {
      seg.innerHTML = names.map((g, i) =>
        `<button class="seg-item ${i === 0 ? "active" : ""}" data-gi="${i}" type="button">${esc(g)}</button>`).join("");
      $$("#cfgGroupSeg .seg-item").forEach((b) => b.addEventListener("click", () => selectCfgGroup(Number(b.dataset.gi))));
    }
    // 保存/重新读取后停留在当前分类，不跳回第一个
    const keep = Math.min(Number(state.cfgGroup) || 0, names.length - 1);
    selectCfgGroup(Math.max(0, keep));
    bindFieldInputs($("#cfgGroups"));
  }

  const DYNAMIC_MODEL_FIELDS = {
    "rvc.models.rvc_default_model": () => state.gatewayModelStems,
    "tts.model.tts_rvc_model": () => state.gatewayModelFiles,
  };

  function renderField(f) {
    const value = state.values[f.key];
    const tag = (state.needsReload || []).includes(f.key)
      ? '<span class="badge-warn">需重载插件生效</span>' : "";
    const dirty = '<span class="badge-warn dirty" style="display:none">已修改</span>';
    const hint = f.hint ? `<p class="f-hint">${esc(f.hint)}</p>` : "";
    let ctrl;
    const dynModels = DYNAMIC_MODEL_FIELDS[f.key];
    if (dynModels) {
      const opts = dynModels();
      const cur = String(value ?? "");
      const list = cur && !opts.includes(cur) ? [cur, ...opts] : opts.slice();
      if (!cur || f.key === "rvc.models.rvc_default_model") list.unshift("");
      const labelOf = (o) => (o === "" ? (f.key === "rvc.models.rvc_default_model" ? "（留空 = 用列表第 1 个）" : "（留空 = 用列表第 1 个）") : o);
      ctrl = `<select class="input" data-key="${esc(f.key)}" data-type="string">
        ${list.map((o) => `<option value="${esc(o)}" ${String(value ?? "") === o ? "selected" : ""}>${esc(labelOf(o))}</option>`).join("")}
      </select>${opts.length ? "" : `<p class="f-hint">网关模型列表为空/离线：可直接输入模型文件名，或先到「模型管理」刷新。</p>`}`;
    } else if (f.type === "bool") {
      ctrl = `<label class="switch"><input type="checkbox" data-key="${esc(f.key)}" data-type="bool"
        ${value ? "checked" : ""} /><span class="track"></span></label>`;
    } else if (f.options && f.options.length) {
      const labels = f.labels || [];
      ctrl = `<select class="input" data-key="${esc(f.key)}" data-type="${f.type || "string"}">
        ${f.options.map((o, idx) => {
          const lab = (labels[idx] != null && String(labels[idx]).trim()) ? labels[idx] : o;
          return `<option value="${esc(o)}" ${String(value) === String(o) ? "selected" : ""}>${esc(lab)}</option>`;
        }).join("")}
      </select>`;
    } else if (f.type === "int" || f.type === "float") {
      const step = f.type === "int" ? "1" : "0.01";
      ctrl = `<input class="input" type="number" data-key="${esc(f.key)}" data-type="${f.type}" step="${step}"
        ${f.minimum != null ? `min="${f.minimum}"` : ""} ${f.maximum != null ? `max="${f.maximum}"` : ""}
        value="${esc(value ?? "")}" />`;
    } else if (f.type === "list") {
      ctrl = `<textarea class="input" data-key="${esc(f.key)}" data-type="list">${esc(Array.isArray(value) ? value.join("\n") : value || "")}</textarea>`;
    } else if (f.type === "dict" || f.type === "object") {
      ctrl = `<textarea class="input" data-key="${esc(f.key)}" data-type="${f.type}">${esc(JSON.stringify(value ?? {}, null, 1))}</textarea>`;
    } else if (f.type === "text") {
      ctrl = `<textarea class="input" data-key="${esc(f.key)}" data-type="string">${esc(value ?? "")}</textarea>`;
    } else {
      ctrl = `<input class="input" data-key="${esc(f.key)}" data-type="string" value="${esc(value ?? "")}" />`;
    }
    return `<div class="field"><label>${esc(f.label)} ${tag} ${dirty}</label>
      <div class="f-ctrl">${ctrl}${hint}</div></div>`;
  }

  async function saveConfig() {
    const keys = Object.keys(state.dirty);
    if (!keys.length) { toast("设置未发生变动"); return; }
    if (state.dirty["architecture.execution_mode"] === "kaggle_hybrid") {
      const tokenVal = state.dirty["architecture.kaggle_api_token"] || state.values["architecture.kaggle_api_token"] || (state.overview && state.overview.kaggle && state.overview.kaggle.has_token);
      const epVal = state.dirty["architecture.kaggle_endpoint"] || state.values["architecture.kaggle_endpoint"] || (state.overview && state.overview.kaggle && state.overview.kaggle.configured);
      if ((!tokenVal || !String(tokenVal).trim()) && (!epVal || !String(epVal).trim())) {
        toast("保存失败：未检测到 Kaggle 配置！切换为 Kaggle 混合云需至少配置端点地址（临时/固定均可）或 API Token。", 5000);
        return;
      }
    }
    const values = {};
    for (const key of Object.keys(state.dirty)) {
      values[key] = state.dirty[key];
    }
    const btn = $("#btnSaveCfg");
    if (btn) { btn.disabled = true; btn.textContent = "保存中..."; }
    try {
      const d = await api("config/save", { method: "POST", body: { values } });
      state.needsReload = d.needs_reload || [];
      toast(d.changed && d.changed.length
        ? `已成功保存 ${d.changed.length} 项设置${d.needs_reload && d.needs_reload.length ? "（含需重载项）" : ""}`
        : "已保存（设置未变动）");
      if (d.errors && d.errors.length) toast("部分未保存：" + d.errors.join("；"), 5200);
      await loadConfig();
    } catch (err) {
      toast("保存失败：" + err.message, 5200);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "保存配置"; }
    }
  }

  /* ---------------- RVC 转换 ---------------- */
  function updateRvcIndexIndicator() {
    const sel = $("#rvcModel");
    const badge = $("#rvcIndexBadge");
    const note = $("#rvcIndexNote");
    if (!sel || !badge || !state.models || !state.models.models) return;
    const curName = sel.value;
    const m = (state.models.models || []).find((x) => x.name === curName || x.filename === curName);
    if (m && m.has_index) {
      badge.className = "tag ok";
      badge.style.background = "";
      badge.style.color = "";
      badge.textContent = "✓ 已配对 Index";
      if (note) note.textContent = "已自动匹配并挂载同名 .index 特征库（特征率建议 0.65~0.8）";
    } else {
      badge.className = "tag";
      badge.style.background = "var(--panel-2)";
      badge.style.color = "var(--muted)";
      badge.textContent = "○ 无 Index 特征库";
      if (note) note.textContent = "未检测到对应 .index 文件，将以原生模型权重推理（特征率建议保持 0）";
    }
  }

  async function prepareRvc() {
    try {
      const d = await api("models");
      state.models = d;
      const options = (d.models || []).filter((m) => !m.missing_in_gateway);
      const sel = $("#rvcModel");
      const keep = sel.value;
      sel.innerHTML = options.map((m) => {
        const idxTag = m.has_index ? " [✓ Index]" : " [无 Index]";
        const displayName = `${esc(m.alias || m.name)}${m.alias ? `（${esc(m.name)}）` : ""}${idxTag}`;
        return `<option value="${esc(m.name)}">${displayName}</option>`;
      }).join("") || `<option value="">（无可用音色）</option>`;
      const def = String(d.default || "").replace(/\.pth$/i, "");
      if (keep && options.some((m) => m.name === keep)) sel.value = keep;
      else if (def && options.some((m) => m.name === def)) sel.value = def;
      updateRvcIndexIndicator();
      if ($("#rvcGw")) {
        $("#rvcGw").textContent = d.gateway_ok ? "Gateway 在线" : "Gateway 离线";
        $("#rvcGw").className = d.gateway_ok ? "mode-badge ready" : "mode-badge pending";
        $("#rvcGw").title = d.dir || "";
      }
      if ($("#rvcGwDot")) {
        $("#rvcGwDot").className = d.gateway_ok ? "status-dot online" : "status-dot offline";
      }
      setStatus("#stGateway", d.gateway_ok ? "ok" : "err", `Gateway <b>${d.gateway_ok ? "正常" : "异常"}</b>`);
    } catch (err) {
      $("#rvcStatus").textContent = "模型读取失败：" + err.message;
    }
  }

  function readFileBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const s = String(reader.result || "");
        resolve(s.slice(s.indexOf(",") + 1));
      };
      reader.onerror = () => reject(new Error("读取文件失败"));
      reader.readAsDataURL(file);
    });
  }

  function fmtBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KiB";
    return (n / 1048576).toFixed(1) + " MiB";
  }

  async function uploadAudio(inputSel, noteSel, stateKey) {
    const file = $(inputSel).files && $(inputSel).files[0];
    if (!file) {
      if (state[stateKey]) return state[stateKey];
      throw new Error("请先选择音频文件");
    }
    if (state.uploading) throw new Error("正在上传，请稍候");
    state.uploading = true;
    $(noteSel).innerHTML = `<span class="tag run">上传中...</span> <small>正在同步音频至 Applio 网关...</small>`;
    try {
      const b = await ensureBridge();
      let d;
      // 优先通过 AstrBot 桥接二进制接口流式上传（支持大文件，免 base64 内存膨胀）
      if (b && typeof b.upload === "function") {
        try {
          d = await b.upload("page/rvc/upload", file);
          d = unwrap(d);
        } catch (bridgeErr) {
          console.warn("Bridge upload error, trying base64 fallback:", bridgeErr);
          if (file.size > 100 * 1024 * 1024) throw new Error("音频文件超过 100MB 限制");
          const data = await readFileBase64(file);
          d = await api("rvc/upload", { method: "POST", body: { name: file.name, data_base64: data } });
        }
      } else {
        if (file.size > 100 * 1024 * 1024) throw new Error("音频文件超过 100MB 限制");
        const data = await readFileBase64(file);
        d = await api("rvc/upload", { method: "POST", body: { name: file.name, data_base64: data } });
      }
      state[stateKey] = d.upload_id;
      $(noteSel).innerHTML = `<span class="tag ok">已就绪</span> <b>${esc(d.name)}</b> <small>(${esc(d.size_text)})</small>`;
      toast(`音频已成功上传至网关 (${d.size_text})`, 2500);
      return d.upload_id;
    } catch (err) {
      $(noteSel).innerHTML = `<span class="tag err">上传失败</span> <small>${esc(err.message)}</small>`;
      toast("音频上传失败: " + err.message, 4000);
      throw err;
    } finally {
      state.uploading = false;
    }
  }

  function rvcParams() {
    const rmode = $("#rvcRenderMode") ? $("#rvcRenderMode").value : "full";
    const sepMode = $("#rvcSepMode") ? $("#rvcSepMode").value : "skip";
    const fadeSec = rmode === "custom"
      ? Number(($("#rvcCustomFade") && $("#rvcCustomFade").value) || 0.5)
      : Number(($("#rvcFade") && $("#rvcFade").value) || 0.5);

    const p = {
      key_shift: Number($("#rvcPitch").value || 0),
      f0_method: $("#rvcF0").value,
      index_rate: Number($("#rvcIndexRate").value || 0),
      rms_mix_rate: Number($("#rvcRms").value !== "" ? $("#rvcRms").value : 1.0),
      protect: Number($("#rvcProtect").value !== "" ? $("#rvcProtect").value : 0.5),
      vocal_volume_db: Number($("#rvcVocalDb").value || 0),
      output_format: $("#rvcFormat").value,
      render_mode: rmode,
      sep_mode: sepMode,
      skip_separation: sepMode === "skip",
      chorus_seconds: Number(($("#rvcSeconds") && $("#rvcSeconds").value) || 25),
      chorus_buffer_seconds: Number($("#rvcChorusBuffer") && $("#rvcChorusBuffer").value !== "" ? $("#rvcChorusBuffer").value : 10),
      fade_seconds: fadeSec,
      speaker_id: Number(($("#rvcSpeaker") && $("#rvcSpeaker").value) || 0),
      f0_autotune: $("#rvcAutotune") ? $("#rvcAutotune").checked : false,
      formant_shifting: $("#rvcFormant") ? $("#rvcFormant").checked : false,
      cleanup_mode: $("#rvcClean") ? $("#rvcClean").value : "off",
    };
    if (rmode === "custom") {
      const st = $("#rvcStart") ? $("#rvcStart").value.trim() : "";
      const et = $("#rvcEnd") ? $("#rvcEnd").value.trim() : "";
      const du = $("#rvcDuration") ? $("#rvcDuration").value.trim() : "";
      if (st) p.start_time = st;
      if (et) p.end_time = et;
      if (du) p.duration = du;
    }
    // 人声混响（仅本次转换试听，勾选才提交）
    if ($("#rvcReverbOn").checked) {
      p.reverb_enabled = true;
      p.reverb_wet = Number($("#rvcRevWet").value || 0.08);
      p.reverb_dry = Number($("#rvcRevDry").value || 1.0);
      p.reverb_room_size = Number($("#rvcRevRoom").value || 0.35);
      p.reverb_decay = Number($("#rvcRevDecay").value || 0.8);
      p.reverb_damping = Number($("#rvcRevDamp").value || 0.60);
      p.reverb_pre_delay = Number($("#rvcRevPre").value || 12);
      p.reverb_width = Number($("#rvcRevWidth").value || 0.60);
    }
    return p;
  }

  async function runConvert() {
    const btn = $("#rvcRun");
    const origText = btn ? btn.textContent : "开始转换";
    const fileInput = $("#rvcFile");
    if (!fileInput || !fileInput.files || !fileInput.files.length) {
      if ($("#rvcStatus")) $("#rvcStatus").textContent = "请先选择待转换音频文件";
      toast("请先选择待转换的本地音频文件", 4000);
      return;
    }
    const model = $("#rvcModel") ? $("#rvcModel").value : "";
    if (!model) {
      if ($("#rvcStatus")) $("#rvcStatus").textContent = "请先选择音色模型";
      toast("请先选择音色模型（若模型列表为空，请先在模型管理中刷新）", 4000);
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "准备中...";
    }
    if ($("#rvcStatus")) $("#rvcStatus").textContent = "准备中...";
    try {
      const origFilename = (fileInput.files && fileInput.files[0]) ? fileInput.files[0].name : "";
      const uploadId = await uploadAudio("#rvcFile", "#rvcUploadNote", "uploadId");
      const isCloud = state.rvcEngine === "kaggle";
      const body = Object.assign({
        upload_id: uploadId,
        model: model,
        input_name: origFilename,
        engine: state.rvcEngine || "local",
      }, rvcParams());
      if ($("#rvcStatus")) $("#rvcStatus").textContent = "提交任务...";
      if (btn) btn.textContent = "提交中...";
      toast(isCloud ? "正在向 Kaggle 云端 GPU 提交转换任务..." : "正在向 Applio 网关提交转换任务...", 2000);
      const d = await api("rvc/convert", { method: "POST", body });
      if ($("#rvcResult")) $("#rvcResult").hidden = true;
      if ($("#rvcOutputs")) $("#rvcOutputs").innerHTML = "";
      if (btn) btn.textContent = isCloud ? "云端 GPU 推理中..." : "Applio 推理中...";
      if ($("#rvcStatus")) $("#rvcStatus").textContent = isCloud ? "已提交，Kaggle GPU 推理中..." : "已提交，Applio 推理中（CPU 耗时约 20-60s）";
      toast("任务已成功提交！后台开始推理，请稍候...", 3000);
      pollJob(d.job_id, "#rvcStatus", (job) => {
        if (btn) {
          btn.disabled = false;
          btn.textContent = origText;
        }
        if (job.status === "succeeded") {
          toast("Applio 音色转换完成！", 4000);
          showOutputs(job.id, "#rvcOutputs", "#rvcResult");
        } else if (job.status === "failed") {
          toast("转换失败：" + (job.error || "未知原因"), 6000);
        }
      });
    } catch (err) {
      if ($("#rvcStatus")) $("#rvcStatus").textContent = "失败：" + err.message;
      toast("转换失败：" + err.message, 6000);
      if (btn) {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }
  }

  /* ---------------- 分离 ---------------- */
  async function prepareSeparate() {
    try {
      const d = await api("rvc/sep_models");
      const sel = $("#sepModel");
      const keep = sel.value;
      sel.innerHTML = (d.models || []).map((m) =>
        `<option value="${esc(m.id)}">${esc(m.name || m.id)}</option>`).join("")
        || `<option value="">（无分离模型）</option>`;
      if (keep && (d.models || []).some((m) => m.id === keep)) sel.value = keep;
      else if (d.default) sel.value = d.default;
      $("#sepInfo").textContent = d.models && d.models.length
        ? `可用分离模型 ${d.models.length} 个，默认 ${d.default}`
        : "未读取到分离模型";
    } catch (err) {
      $("#sepStatus").textContent = "分离模型读取失败：" + err.message;
    }
  }

  async function runSeparate() {
    const btn = $("#sepRun");
    const origText = btn ? btn.textContent : "开始分离";
    const fileInput = $("#sepFile");
    if (!fileInput || !fileInput.files || !fileInput.files.length) {
      if ($("#sepStatus")) $("#sepStatus").textContent = "请先选择待分离音频文件";
      toast("请先选择待分离的本地音频文件", 4000);
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "分离中...";
    }
    state.sepRunning = true;
    if ($("#sepStatus")) $("#sepStatus").textContent = "准备中...";
    try {
      const origFilename = (fileInput.files && fileInput.files[0]) ? fileInput.files[0].name : "";
      const uploadId = await uploadAudio("#sepFile", "#sepUploadNote", "sepUploadId");
      const isCloud = state.rvcEngine === "kaggle";
      const body = {
        upload_id: uploadId,
        input_name: origFilename,
        model: $("#sepModel").value,
        mode: $("#sepMode").value,
        output_format: $("#sepFormat").value,
        engine: state.rvcEngine || "local",
      };
      toast(isCloud ? "正在向 Kaggle 云端提交分离任务 (固定 BS-Roformer 1297)..." : "正在向 Applio 网关提交分离任务...", 2000);
      const d = await api("rvc/separate", { method: "POST", body });
      $("#sepOutputs").innerHTML = "";
      $("#sepStatus").textContent = isCloud ? "已提交云端，BS-Roformer 1297 分离中..." : "已提交，分离中（约 1-2 分钟）";
      pollJob(d.job_id, "#sepStatus", (job) => {
        if (btn) {
          btn.disabled = false;
          btn.textContent = origText;
        }
        state.sepRunning = false;
        if (job.status === "succeeded") {
          $("#sepInfo").textContent = `完成 · 模型 ${job.sep_model || "-"} · 导出 ${job.format || "-"}`;
          showOutputs(job.id, "#sepOutputs", null);
        }
      });
    } catch (err) {
      $("#sepStatus").textContent = "失败：" + err.message;
      if (btn) {
        btn.disabled = false;
        btn.textContent = origText;
      }
      state.sepRunning = false;
    }
  }

  /* ---------------- 任务轮询与输出（按槽位独立，互不打断） ---------------- */
  function stopSlotPoller(slotKey) {
    const prev = state.pollers[slotKey];
    if (!prev) return;
    clearTimeout(prev.timer);
    if (prev.secondTimer) clearInterval(prev.secondTimer);
    delete state.pollers[slotKey];
  }

  function pollJob(jobId, statusSel, onDone, intervalMs) {
    const slotKey = String(statusSel || "default");
    stopSlotPoller(slotKey);
    const slot = {
      jobId,
      timer: null,
      secondTimer: null,
      anchorAt: Date.now(),
      lastSec: 0,
      synced: false,
      lastStatus: "",
      currentStage: "推理中...",
    };
    state.pollers[slotKey] = slot;
    const gap = Math.max(500, Number(intervalMs) || 2000);

    // 200ms 轻检查 + 整秒变化才渲染：单调时钟每秒精准 +1s，不因轮询回包抖动
    slot.secondTimer = setInterval(() => {
      if (state.pollers[slotKey] !== slot) {
        clearInterval(slot.secondTimer);
        slot.secondTimer = null;
        return;
      }
      const sec = anchorSecs(slot.anchorAt);
      if (sec !== slot.lastSec) {
        slot.lastSec = sec;
        const el = $(statusSel);
        if (el) el.textContent = `${slot.currentStage}（已 ${sec}s）`;
      }
    }, 200);

    const tick = async () => {
      if (state.pollers[slotKey] !== slot) {
        stopSlotPoller(slotKey);
        return;
      }
      try {
        const d = await api("rvc/status", { params: { job_id: jobId } });
        if (state.pollers[slotKey] !== slot) return;
        const prevStage = slot.currentStage;
        const prevStatus = slot.lastStatus;
        slot.currentStage = d.stage || d.status || "推理中...";
        slot.lastStatus = d.status || "";
        if (typeof d.elapsed === "number") {
          const serverMs = Math.max(0, Number(d.elapsed) * 1000);
          const localMs = Date.now() - slot.anchorAt;
          const enteredRunning = prevStatus === "pending" && d.status === "running";
          if (!slot.synced || enteredRunning || Math.abs(serverMs - localMs) > 3000) {
            slot.anchorAt = Date.now() - serverMs;
            slot.synced = true;
          }
        }
        const curSec = anchorSecs(slot.anchorAt);
        const secChanged = curSec !== slot.lastSec;
        if (prevStage !== slot.currentStage || secChanged) {
          slot.lastSec = curSec;
          const el = $(statusSel);
          if (el) el.textContent = `${slot.currentStage}（已 ${curSec}s）`;
        }
        if (d.status === "succeeded" || d.status === "failed" || d.status === "unknown") {
          stopSlotPoller(slotKey);
          if (onDone) onDone(d);
          return;
        }
        slot.timer = setTimeout(tick, gap);
      } catch (err) {
        if (state.pollers[slotKey] !== slot) return;
        const el = $(statusSel);
        if (el) el.textContent = "状态查询失败：" + err.message;
        slot.timer = setTimeout(tick, gap * 2);
      }
    };
    tick();
  }

  /* ---------------- 波形播放器 ---------------- */
  function fmtClock(sec) {
    const n = Math.max(0, Number(sec) || 0);
    return `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, "0")}`;
  }

  function audioBlobFromBase64(b64, mime) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime || "audio/wav" });
  }

  async function decodePeaks(arrayBuffer, buckets) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("浏览器不支持音频解码");
    const ctx = new Ctx();
    try {
      const buf = await ctx.decodeAudioData(arrayBuffer);
      const ch = buf.getChannelData(0);
      const step = Math.max(1, Math.floor(ch.length / buckets));
      const peaks = new Array(buckets).fill(0);
      for (let i = 0; i < buckets; i += 1) {
        let max = 0;
        const start = i * step;
        for (let j = 0; j < step; j += 1) {
          const v = Math.abs(ch[start + j] || 0);
          if (v > max) max = v;
        }
        peaks[i] = max;
      }
      const norm = Math.max(0.0001, Math.max.apply(null, peaks));
      return { peaks: peaks.map((v) => v / norm), duration: buf.duration };
    } finally {
      ctx.close();
    }
  }

  async function mountPlayer(host, opts) {
    host.innerHTML = `
      <div class="player">
        <button class="p-play" type="button" aria-label="播放">▶</button>
        <div class="p-track"><canvas></canvas></div>
        <span class="p-time">0:00 / --:--</span>
        <a class="btn ghost sm p-dl" download>下载</a>
      </div>`;
    const blob = audioBlobFromBase64(opts.base64, opts.mime);
    const url = URL.createObjectURL(blob);
    state.urls.push(url);
    const audio = new Audio(url);
    audio.preload = "metadata";
    state.audioEls.push(audio);
    const playBtn = host.querySelector(".p-play");
    const canvas = host.querySelector("canvas");
    const timeEl = host.querySelector(".p-time");
    const dl = host.querySelector(".p-dl");
    dl.href = url;
    dl.download = opts.name || "output.wav";

    let peaks = null;
    let duration = Number(opts.duration) || 0;
    let dragging = false;
    let raf = 0;

    const draw = () => {
      if (!peaks) return;
      const w = canvas.clientWidth || 320;
      const h = canvas.clientHeight || 44;
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
        canvas.width = Math.floor(w * dpr);
        canvas.height = Math.floor(h * dpr);
      }
      const g = canvas.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      const cs = getComputedStyle(document.documentElement);
      const played = (cs.getPropertyValue("--blue") || "#2f6fed").trim();
      const rest = (cs.getPropertyValue("--line-strong") || "#d8dce1").trim();
      const prog = duration ? Math.min(1, audio.currentTime / duration) : 0;
      const n = peaks.length;
      const bw = Math.max(2, Math.floor(w / n) - 1);
      peaks.forEach((p, i) => {
        const x = (i * w) / n;
        const bh = Math.max(2, p * (h - 8));
        g.fillStyle = (i + 0.5) / n <= prog ? played : rest;
        g.fillRect(x, (h - bh) / 2, bw, bh);
      });
    };

    const updateTime = () => {
      timeEl.textContent = `${fmtClock(audio.currentTime)} / ${fmtClock(duration || audio.duration)}`;
    };

    const loop = () => {
      if (audio.paused) return;
      draw();
      raf = requestAnimationFrame(loop);
    };

    audio.addEventListener("loadedmetadata", () => {
      if (!duration) duration = audio.duration;
      updateTime();
      draw();
    });
    audio.addEventListener("play", () => { playBtn.textContent = "❚❚"; loop(); });
    audio.addEventListener("pause", () => { playBtn.textContent = "▶"; cancelAnimationFrame(raf); });
    audio.addEventListener("ended", () => {
      playBtn.textContent = "▶";
      audio.currentTime = 0;
      updateTime();
      draw();
    });
    audio.addEventListener("timeupdate", () => { updateTime(); if (audio.paused) draw(); });

    playBtn.addEventListener("click", () => {
      if (audio.paused) {
        audio.play().catch((err) => toast("播放失败：" + err.message));
      } else {
        audio.pause();
      }
    });

    const seekTo = (clientX) => {
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      if (duration) audio.currentTime = ratio * duration;
      updateTime();
      draw();
    };
    canvas.addEventListener("pointerdown", (e) => {
      dragging = true;
      canvas.setPointerCapture(e.pointerId);
      seekTo(e.clientX);
    });
    canvas.addEventListener("pointermove", (e) => { if (dragging) seekTo(e.clientX); });
    canvas.addEventListener("pointerup", () => { dragging = false; });
    canvas.addEventListener("pointercancel", () => { dragging = false; });

    updateTime();
    try {
      const decoded = await decodePeaks(await blob.arrayBuffer(), 180);
      peaks = decoded.peaks;
      duration = decoded.duration || duration;
      updateTime();
      draw();
      if (typeof ResizeObserver === "function") {
        new ResizeObserver(() => { if (canvas.clientWidth > 0) draw(); }).observe(canvas);
      }
    } catch (err) {
      // 解码失败时退回原生控件，保证能听
      const fallback = document.createElement("audio");
      fallback.controls = true;
      fallback.src = url;
      host.innerHTML = "";
      host.appendChild(fallback);
    }
  }

  async function showOutputs(jobId, containerSel, revealSel) {
    const container = $(containerSel);
    try {
      const d = await api("rvc/result", { params: { job_id: jobId } });
      const outs = d.outputs || [];
      if (!outs.length) { container.innerHTML = `<p class="empty">没有可播放的结果</p>`; return; }
      container.innerHTML = outs.map((o, i) => `
        <div class="output-item">
          <div class="o-head"><span>${esc(o.label_text || o.label)} · ${esc(o.name)} · ${fmtDuration(o.duration)}</span>
            <span class="hint">${esc(o.size_text || "")}</span></div>
          <div class="player-host" data-idx="${i}"></div>
        </div>`).join("");
      const hosts = $$(".player-host", container);
      outs.forEach((o, i) => {
        if (o.error) {
          hosts[i].innerHTML = `<p class="hint">${esc(o.error)}</p>`;
          return;
        }
        mountPlayer(hosts[i], {
          base64: o.audio_base64, mime: o.mime, name: o.name, duration: o.duration,
        });
      });
      if (revealSel) $(revealSel).hidden = false;
    } catch (err) {
      container.innerHTML = `<p class="empty">读取结果失败：${esc(err.message)}</p>`;
    }
  }

  /* ---------------- 诊断 ---------------- */
  async function loadDiagnostics() {
    $("#diagHint").innerHTML = `<span class="spin-ring" aria-hidden="true"></span> 正在运行连通性与日志诊断...`;
    setLoading("#diagChecks", "正在检测网关、路径与系统环境...");
    try {
      const d = await api("diagnostics");
      $("#diagHint").textContent = "只读状态检查，不触发推理";
      $("#diagChecks").innerHTML = (d.checks || []).map((c) => `
        <li data-ok="${c.ok ? "1" : "0"}" data-level="${esc(c.level || "")}">
          <span class="dot"></span>
          <div><div class="c-name">${esc(c.name)}</div><div class="c-detail">${esc(c.detail)}</div></div>
        </li>`).join("");
      $("#diagPaths").innerHTML = Object.entries(d.paths || {})
        .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
      const errs = d.errors || [];
      $("#diagErrors").innerHTML = errs.length
        ? errs.map((e) => `<li><span class="l-time">${esc(e.time || "")}</span>${esc(e.text)}
            ${e.context ? `<span class="l-ctx">${esc(e.context)}</span>` : ""}</li>`).join("")
        : `<li>暂无相关错误</li>`;
    } catch (err) {
      $("#diagHint").textContent = "诊断失败：" + err.message;
    }
  }

  /* ---------------- 绑定 ---------------- */
  function bind() {
    $$(".nav-item").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
    $$("[data-goto]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.goto)));
    on("#btnReloadData", "click", () => switchTab(state.tab));
    on("#rvcModel", "change", updateRvcIndexIndicator);
    on("#cfgGroups", "click", (e) => {
      const btn = e.target.closest("[data-goto]");
      if (btn) switchTab(btn.dataset.goto);
    });

    $$("#taskModeSeg .seg-item").forEach((b) => b.addEventListener("click", () => switchTaskMode(b.dataset.mode)));
    $$("#taskBizSeg .seg-item").forEach((b) => b.addEventListener("click", () => switchTaskBiz(b.dataset.biz)));
    $$("#taskSeg .seg-item").forEach((b) => b.addEventListener("click", () => {
      state.taskStatus = b.dataset.status;
      state.taskPage = 1;
      $$("#taskSeg .seg-item").forEach((x) => x.classList.toggle("active", x === b));
      renderTasks();
    }));
    on("#btnTasksReload", "click", loadTasks);
    on("#taskSearch", "input", () => {
      state.taskPage = 1;
      renderTasks();
    });
    on("#taskPageSize", "change", (e) => {
      state.taskPageSize = Number(e.target.value) || 30;
      state.taskPage = 1;
      renderTasks();
    });
    on("#taskPrev", "click", () => {
      if (state.taskPage > 1) {
        state.taskPage -= 1;
        renderTasks();
      }
    });
    on("#taskNext", "click", () => {
      state.taskPage += 1;
      renderTasks();
    });
    on("#taskPages", "click", (e) => {
      const btn = e.target.closest("[data-page]");
      if (!btn) return;
      state.taskPage = Number(btn.dataset.page) || 1;
      renderTasks();
    });
    on("#taskTable", "click", (e) => {
      const play = e.target.closest("[data-tts-play]");
      if (play) { ttsPlayModal(play.dataset.ttsPlay); return; }
      const btn = e.target.closest("[data-task]");
      if (btn) showTaskDetail(btn.dataset.task);
    });
    on("#modalClose", "click", () => {
      if ($("#modal")) $("#modal").hidden = true;
      pauseAllAudio();
    });
    on("#modal", "click", (e) => {
      if (e.target === $("#modal")) {
        $("#modal").hidden = true;
        pauseAllAudio();
      }
    });
    on("#clipRun", "click", runClip);

    $$("#rvcSeg .seg-item").forEach((b) => b.addEventListener("click", () => switchRvcMode(b.dataset.mode)));

    on("#btnRefreshModels", "click", refreshModels);
    on("#btnSaveModels", "click", saveModelsUnified);
    on("#swCoverEnable", "change", async (e) => {
      try {
        await api("config/save", { method: "POST", body: { values: { "basic.enable_rvc": e.target.checked } } });
        toast(e.target.checked ? "翻唱已启用" : "翻唱已停用");
      } catch (err) { toast("保存失败：" + err.message, 4200); loadModels(); }
    });
    on("#swTtsEnable", "change", async (e) => {
      try {
        await api("config/save", { method: "POST", body: { values: { "tts.tts_enable": e.target.checked } } });
        toast(e.target.checked ? "TTS 已启用" : "TTS 已停用");
      } catch (err) { toast("保存失败：" + err.message, 4200); loadModels(); }
    });
    on("#modelRows", "click", (e) => {
      const radio = e.target.closest('input[name="coverDefault"], input[name="ttsDefault"]');
      if (radio) return;  // 启用单选由「统一保存」提交
    });
    on("#btnSaveCfg", "click", saveConfig);
    on("#btnReloadCfg", "click", loadConfig);

    $$("#guideSeg .seg-item").forEach((b) => b.addEventListener("click", () => {
      const key = b.dataset.guide;
      $$("#guideSeg .seg-item").forEach((x) => x.classList.toggle("active", x === b));
      const map = {
        features: $("#guideSec-features"),
        commands: $("#guideSec-commands"),
        kaggle: $("#guideSec-kaggle"),
      };
      Object.entries(map).forEach(([k, el]) => {
        if (el) el.style.display = k === key ? "flex" : "none";
      });
    }));

    $$("#overviewModeCards .mode-card").forEach((card) => {
      card.addEventListener("click", async () => {
        const mode = card.dataset.mode;
        if (state.execMode === mode) return;
        if (mode === "kaggle_hybrid") {
          const k = state.overview && state.overview.kaggle;
          const hasToken = k && (k.has_token || (k.quota_info && Object.keys(k.quota_info).length > 0));
          const hasEndpoint = k && k.configured;
          if (!hasToken && !hasEndpoint) {
            toast("未检测到 Kaggle 云端配置！请先前往「设置」配置 Kaggle 隧道端点地址或 API Token 后方可切换为混合云模式。", 5000);
            return;
          }
          if (!hasToken && hasEndpoint) {
            toast("已切换为混合云模式（提示：未配置 Kaggle API Token，无法使用远程一键冷启动，需在 Kaggle 网页端手动 Run All 启动）", 5000);
          }
        }
        overviewReqId++; // 递增请求 ID，让任何后台尚未完成的旧 loadOverview 请求直接失效作废
        const prevMode = state.execMode;
        state.execMode = mode;
        const isHybrid = mode === "kaggle_hybrid";
        updateModeCardsUI(isHybrid);
        if ($("#ovExecMode")) {
          $("#ovExecMode").textContent = isHybrid ? "Kaggle 混合云" : "本地一体化";
          $("#ovExecModeHint").textContent = isHybrid ? "Kaggle云端翻唱 + 本地TTS" : "本地翻唱 + 本地TTS";
        }
        renderKaggleStatus(state.overview && state.overview.kaggle, isHybrid);
        try {
          await api("config/save", { method: "POST", body: { values: { "architecture.execution_mode": mode } } });
          toast(isHybrid ? "已切换为混合云模式 (Kaggle云端翻唱 + 本地TTS)" : "已切换为本地一体化模式");
        } catch (e) {
          toast("切换模式失败：" + e.message, 4000);
          state.execMode = prevMode;
          updateModeCardsUI(prevMode === "kaggle_hybrid");
          renderKaggleStatus(state.overview && state.overview.kaggle, prevMode === "kaggle_hybrid");
        }
      });
    });

    // Kaggle 状态探测、串行静默轮询与沙箱安全二次确认
    let kaggleActionLock = false;
    let kagglePollTimer = null;
    let kaggleConfirmResetTimer = null;
    let kaggleShutdownConfirmUntil = 0;

    function stopKagglePolling() {
      if (kagglePollTimer) {
        clearTimeout(kagglePollTimer);
        kagglePollTimer = null;
      }
    }

    function resetShutdownConfirm() {
      kaggleShutdownConfirmUntil = 0;
      if (kaggleConfirmResetTimer) {
        clearTimeout(kaggleConfirmResetTimer);
        kaggleConfirmResetTimer = null;
      }
      [$("#btnShutdownKaggle"), $("#btnRvcShutdownKaggle")].forEach((b) => {
        if (b && !b.disabled) b.textContent = "远程关机";
      });
    }

    function setKaggleButtonsBusy(action, busy) {
      const coldBtns = [$("#btnColdStartKaggle"), $("#btnRvcColdStartKaggle")];
      const shutBtns = [$("#btnShutdownKaggle"), $("#btnRvcShutdownKaggle")];
      const allBtns = [...coldBtns, ...shutBtns];

      if (busy) {
        resetShutdownConfirm();
        allBtns.forEach((b) => {
          if (!b) return;
          b.disabled = true;
          if (action === "start" && coldBtns.includes(b)) b.textContent = "开机中...";
          if (action === "shutdown" && shutBtns.includes(b)) b.textContent = "关机中...";
        });
      } else {
        coldBtns.forEach((b) => {
          if (!b) return;
          b.disabled = false;
          b.textContent = "远程开机";
        });
        shutBtns.forEach((b) => {
          if (!b) return;
          b.disabled = false;
          b.textContent = "远程关机";
        });
      }
    }

    async function fetchKaggleStatus(silent = false, fast = false) {
      try {
        const res = await api("kaggle/status", fast ? { params: { fast: "1" } } : {});
        const k = (res && res.data) || res;
        if (k) {
          if (!state.overview) state.overview = {};
          state.overview.kaggle = k;
          const isHybrid = state.execMode === "kaggle_hybrid";
          renderKaggleStatus(k, isHybrid);
          if ($("#badgeExecMode") && isHybrid) {
            if (k.online) {
              $("#badgeExecMode").textContent = "云端在线";
              $("#badgeExecMode").className = "mode-badge ready";
            } else {
              $("#badgeExecMode").textContent = k.is_starting ? "云端启动中" : (k.configured ? "云端休眠" : "云端未配置");
              $("#badgeExecMode").className = "mode-badge pending";
            }
          }
        }
        return k;
      } catch (e) {
        if (!silent) toast("状态检测失败: " + e.message, 3500);
        return null;
      }
    }

    function ensureKaggleStartPolling() {
      if (kagglePollTimer) return;
      kaggleActionLock = true;
      let attempts = 0;
      const maxAttempts = 120; // 串行每 3s 轻探一次，最长 360 秒匹配真实冷启动耗时
      const pollNext = () => {
        kagglePollTimer = setTimeout(async () => {
          attempts++;
          const useFast = (attempts % 5 !== 0); // 每 5 次做一次完整状态校对，其余走 fast=1 毫秒级探针
          const status = await fetchKaggleStatus(true, useFast);
          if (status && status.online) {
            stopKagglePolling();
            kaggleActionLock = false;
            state.kgStartingAt = 0;
            setKaggleButtonsBusy("start", false);
            toast("云端已连上");
            return;
          }
          if (attempts >= maxAttempts && !(status && status.is_starting)) {
            stopKagglePolling();
            kaggleActionLock = false;
            state.kgStartingAt = 0;
            setKaggleButtonsBusy("start", false);
            toast("云端开机等待超时，请点击刷新状态确认当前状态", 5000);
            return;
          }
          pollNext();
        }, 3000);
      };
      pollNext();
    }
    state.ensureKaggleStartPolling = ensureKaggleStartPolling;
    state.fetchKaggleStatus = fetchKaggleStatus;

    async function refreshKaggleStatus() {
      const btns = [$("#btnRefreshKaggle"), $("#btnRvcRefreshKaggle")];
      btns.forEach((b) => { if (b) { b.disabled = true; b.textContent = "刷新中..."; } });
      try {
        const k = await fetchKaggleStatus(false, false);
        if (k) {
          if (k.online) {
            stopKagglePolling();
            kaggleActionLock = false;
            state.kgStartingAt = 0;
            toast("云端已连上");
          } else if (k.is_starting) {
            ensureKaggleStartPolling();
            toast(k.status_text || "云端正在开机初始化中，请耐心等待", 3500);
          } else {
            stopKagglePolling();
            kaggleActionLock = false;
            state.kgStartingAt = 0;
            toast(k.configured ? "云端处于休眠关机状态" : "未配置 Kaggle 端点");
          }
        } else {
          // 刷新失败时恢复状态展示，不永久卡死闲置按钮
          const stillStarting = Number(state.kgStartingAt || 0) > Date.now() - 360000;
          if (!stillStarting) {
            kaggleActionLock = false;
            renderKaggleStatus((state.overview && state.overview.kaggle) || {}, state.execMode === "kaggle_hybrid");
          }
        }
      } finally {
        btns.forEach((b) => { if (b) { b.disabled = false; b.textContent = "刷新状态"; } });
      }
    }

    async function triggerKaggleColdStart() {
      if (kaggleActionLock || Number(state.kgStartingAt || 0) > Date.now() - 360000) {
        toast("云端开机正在进行中，请勿重复点击", 3000);
        return;
      }
      const k = state.overview && state.overview.kaggle;
      const hasToken = k && (k.has_token || (k.quota_info && Object.keys(k.quota_info).length > 0));
      if (!hasToken) {
        toast("未配置 Kaggle API Token，无法发起远程开机。请在设置中填入 Token，或直接在 Kaggle 网页端点击「Run All」启动。", 6000);
        return;
      }

      kaggleActionLock = true;
      stopKagglePolling();
      setKaggleButtonsBusy("start", true);
      toast("正在调用 Kaggle 官方 API 发起远程冷启动...");

      try {
        const res = await api("kaggle/cold_start", "POST");
        state.kgStartingAt = Date.now();
        toast(res.message || "开机指令已下发！后台已开始静默探测连通状态...", 4500);
        ensureKaggleStartPolling();
      } catch (e) {
        kaggleActionLock = false;
        stopKagglePolling();
        state.kgStartingAt = 0;
        setKaggleButtonsBusy("start", false);
        toast("远程开机失败: " + e.message, 6000);
      }
    }

    async function triggerKaggleShutdown() {
      if (kaggleActionLock) {
        toast("操作处理中，请勿频繁点击", 2500);
        return;
      }
      // 沙箱 iframe 无 allow-modals 权限时原生 confirm() 会被静默拦截返回 false，改用页内 5 秒二次点击确认
      const now = Date.now();
      if (now > kaggleShutdownConfirmUntil) {
        kaggleShutdownConfirmUntil = now + 5000;
        [$("#btnShutdownKaggle"), $("#btnRvcShutdownKaggle")].forEach((b) => {
          if (b && !b.disabled) b.textContent = "确认关机？";
        });
        toast("请在 5 秒内再次点击「确认关机？」以关闭云端实例", 4000);
        if (kaggleConfirmResetTimer) clearTimeout(kaggleConfirmResetTimer);
        kaggleConfirmResetTimer = setTimeout(resetShutdownConfirm, 5000);
        return;
      }
      resetShutdownConfirm();

      kaggleActionLock = true;
      stopKagglePolling();
      state.kgStartingAt = 0;
      setKaggleButtonsBusy("shutdown", true);
      toast("正在向 Kaggle 实例发送关机指令...");

      try {
        const res = await api("kaggle/shutdown", { method: "POST" });
        toast(res.message || "关机指令已成功下发！后台正在探测断开状态...", 4000);

        let attempts = 0;
        const maxAttempts = 15; // 串行每 3s 探测一次，最多 45 秒
        const pollShut = () => {
          kagglePollTimer = setTimeout(async () => {
            attempts++;
            const status = await fetchKaggleStatus(true, true);
            if (!status || !status.online) {
              stopKagglePolling();
              kaggleActionLock = false;
              setKaggleButtonsBusy("shutdown", false);
              toast("云端已成功断开休眠");
              return;
            }
            if (attempts >= maxAttempts) {
              stopKagglePolling();
              kaggleActionLock = false;
              setKaggleButtonsBusy("shutdown", false);
              toast("云端关机等待超时，请手动刷新状态检查", 4000);
              return;
            }
            pollShut();
          }, 3000);
        };
        pollShut();
      } catch (e) {
        kaggleActionLock = false;
        stopKagglePolling();
        setKaggleButtonsBusy("shutdown", false);
        toast("远程关机失败: " + e.message, 5000);
      }
    }

    on("#btnRefreshKaggle", "click", refreshKaggleStatus);
    on("#btnRvcRefreshKaggle", "click", refreshKaggleStatus);
    on("#btnColdStartKaggle", "click", triggerKaggleColdStart);
    on("#btnRvcColdStartKaggle", "click", triggerKaggleColdStart);
    on("#btnShutdownKaggle", "click", triggerKaggleShutdown);
    on("#btnRvcShutdownKaggle", "click", triggerKaggleShutdown);

    $$("#rvcEngineSeg .seg-item").forEach((btn) => {
      btn.addEventListener("click", function () {
        const targetEngine = this.dataset.engine || "local";
        if (targetEngine === "kaggle") {
          const k = state.overview && state.overview.kaggle;
          const hasToken = k && (k.has_token || (k.quota_info && Object.keys(k.quota_info).length > 0));
          const hasEndpoint = k && k.configured;
          if (!hasToken && !hasEndpoint) {
            toast("未检测到 Kaggle 云端配置！请先前往「设置」填入 Kaggle 隧道端点地址或 API Token 后方可切换为云端算力。", 5000);
            return;
          }
          if (!hasToken && hasEndpoint) {
            toast("已切换为云端算力（提示：未配置 Kaggle API Token，无法使用远程开机，需在 Kaggle 网页端手动 Run All 启动）", 4000);
          }
        }
        $$("#rvcEngineSeg .seg-item").forEach((b) => b.classList.remove("active"));
        this.classList.add("active");
        state.rvcEngine = targetEngine;
        const bar = $("#rvcKaggleBar");
        if (bar) {
          bar.style.display = state.rvcEngine === "kaggle" ? "block" : "none";
        }
        if (state.rvcEngine === "kaggle") {
          fetchKaggleStatus();
        }
      });
    });

    on("#rvcRun", "click", runConvert);
    on("#rvcFile", "change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) {
        if ($("#rvcUploadNote")) $("#rvcUploadNote").innerHTML = `<span class="tag run">已选文件</span> <b>${esc(f.name)}</b> <small>(${fmtBytes(f.size)}) · 点击开始转换上传</small>`;
      } else {
        if ($("#rvcUploadNote")) $("#rvcUploadNote").textContent = "未选择文件";
      }
    });
    on("#sepFile", "change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) {
        if ($("#sepUploadNote")) $("#sepUploadNote").innerHTML = `<span class="tag run">已选文件</span> <b>${esc(f.name)}</b> <small>(${fmtBytes(f.size)}) · 点击开始分离上传</small>`;
      } else {
        if ($("#sepUploadNote")) $("#sepUploadNote").textContent = "未选择文件";
      }
    });
    on("#clipFile", "change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) {
        if ($("#clipUploadNote")) $("#clipUploadNote").innerHTML = `<span class="tag run">已选文件</span> <b>${esc(f.name)}</b> <small>(${fmtBytes(f.size)}) · 点击开始切段上传</small>`;
      } else {
        if ($("#clipUploadNote")) $("#clipUploadNote").textContent = "未选择文件";
      }
    });
    on("#rvcRenderMode", "change", (e) => {
      const mode = e.target.value;
      const chorusBox = $("#rvcChorusOptions");
      const customBox = $("#rvcCustomOptions");
      if (chorusBox) chorusBox.style.display = mode === "chorus" ? "block" : "none";
      if (customBox) customBox.style.display = mode === "custom" ? "block" : "none";
    });
    on("#rvcReset", "click", () => {
      if ($("#rvcSepMode")) $("#rvcSepMode").value = "skip";
      if ($("#rvcPitch")) $("#rvcPitch").value = 0;
      if ($("#rvcRenderMode")) $("#rvcRenderMode").value = "full";
      if ($("#rvcChorusOptions")) $("#rvcChorusOptions").style.display = "none";
      if ($("#rvcCustomOptions")) $("#rvcCustomOptions").style.display = "none";
      if ($("#rvcIndexRate")) $("#rvcIndexRate").value = 0.75;
      if ($("#rvcRms")) $("#rvcRms").value = 1.0;
      if ($("#rvcProtect")) $("#rvcProtect").value = 0.5;
      if ($("#rvcVocalDb")) $("#rvcVocalDb").value = 0;
      if ($("#rvcFade")) $("#rvcFade").value = 0.5;
      if ($("#rvcCustomFade")) $("#rvcCustomFade").value = 0.5;
      if ($("#rvcSeconds")) $("#rvcSeconds").value = 25;
      if ($("#rvcChorusBuffer")) $("#rvcChorusBuffer").value = 10;
      if ($("#rvcStart")) $("#rvcStart").value = "";
      if ($("#rvcEnd")) $("#rvcEnd").value = "";
      if ($("#rvcDuration")) $("#rvcDuration").value = "";
      if ($("#rvcSpeaker")) $("#rvcSpeaker").value = 0;
      if ($("#rvcReverbOn")) $("#rvcReverbOn").checked = false;
      if ($("#rvcRevWet")) $("#rvcRevWet").value = 0.08;
      if ($("#rvcRevDry")) $("#rvcRevDry").value = 1.0;
      if ($("#rvcRevRoom")) $("#rvcRevRoom").value = 0.35;
      if ($("#rvcRevDecay")) $("#rvcRevDecay").value = 0.8;
      if ($("#rvcRevDamp")) $("#rvcRevDamp").value = 0.60;
      if ($("#rvcRevPre")) $("#rvcRevPre").value = 12;
      if ($("#rvcRevWidth")) $("#rvcRevWidth").value = 0.60;
      if ($("#rvcAutotune")) $("#rvcAutotune").checked = false;
      if ($("#rvcFormant")) $("#rvcFormant").checked = false;
      if ($("#rvcClean")) $("#rvcClean").value = "off";
      if ($("#rvcStatus")) $("#rvcStatus").textContent = "参数已重置";
      toast("Applio 转换参数已恢复为默认值");
    });
    on("#sepRun", "click", runSeparate);
    on("#ttsRun", "click", runTts);
    on("#btnSaveAudio", "click", saveAudioCfg);
    on("#btnReloadAudio", "click", loadAudioTab);
    on("#btnDiag", "click", loadDiagnostics);

    window.addEventListener("hashchange", () => {
      const name = location.hash.replace("#", "");
      if (name && name !== state.tab) switchTab(name);
    });
  }

  /* ---------------- 全局悬浮提示 Tooltip 系统 (杜绝容器裁剪) ---------------- */
  let tooltipEl = null;
  function getTooltipEl() {
    if (!tooltipEl) {
      tooltipEl = document.createElement("div");
      tooltipEl.id = "globalTooltip";
      tooltipEl.className = "global-tooltip";
      tooltipEl.hidden = true;
      document.body.appendChild(tooltipEl);
    }
    return tooltipEl;
  }

  function initGlobalTooltips() {
    let currentTarget = null;
    document.addEventListener("mouseover", (e) => {
      const tipTarget = e.target && e.target.closest && e.target.closest("[data-tip]");
      if (!tipTarget || tipTarget === currentTarget) return;
      const text = tipTarget.getAttribute("data-tip");
      if (!text) return;
      currentTarget = tipTarget;
      const tip = getTooltipEl();
      tip.textContent = text;
      tip.hidden = false;
      tip.style.display = "block";

      const rect = tipTarget.getBoundingClientRect();
      const tipRect = tip.getBoundingClientRect();

      let left = rect.left + rect.width / 2 - tipRect.width / 2;
      const margin = 12;
      if (left < margin) left = margin;
      if (left + tipRect.width > window.innerWidth - margin) {
        left = window.innerWidth - tipRect.width - margin;
      }

      let top = rect.top - tipRect.height - 8;
      if (top < 10) {
        top = rect.bottom + 8;
      }

      tip.style.left = `${Math.round(left)}px`;
      tip.style.top = `${Math.round(top)}px`;
      requestAnimationFrame(() => {
        tip.classList.add("visible");
      });
    });

    document.addEventListener("mouseout", (e) => {
      if (!currentTarget) return;
      if (!e.relatedTarget || !currentTarget.contains(e.relatedTarget)) {
        currentTarget = null;
        if (tooltipEl) {
          tooltipEl.classList.remove("visible");
          tooltipEl.hidden = true;
          tooltipEl.style.display = "none";
        }
      }
    });

    window.addEventListener("scroll", () => {
      if (currentTarget && tooltipEl) {
        currentTarget = null;
        tooltipEl.classList.remove("visible");
        tooltipEl.hidden = true;
        tooltipEl.style.display = "none";
      }
    }, { passive: true, capture: true });
  }

  async function boot() {
    bind();
    initGlobalTooltips();
    const initial = (location.hash || "#overview").replace("#", "");
    try {
      await ensureBridge();
      banner("");
    } catch (err) {
      banner(err.message);
      setStatus("#stPlugin", "err", "插件 <b>页面桥接缺失</b>");
      setStatus("#stGateway", "err", "Gateway <b>未知</b>");
    }
    const tabs = ["overview", "tasks", "rvc", "tts", "models", "audio", "guide", "settings", "diagnostics"];
    switchTab(tabs.includes(initial) ? initial : "overview");
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
