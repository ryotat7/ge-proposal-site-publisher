/*!
 * NY deck runtime v1 — shared presentation runtime for free-form decks (Apache-2.0).
 *
 * Authoring contract (agents write HTML/CSS/SVG/JSON only, never JavaScript):
 *   <main id="pd-deck"> <section class="pd-slide" data-pd-title="..."> ... </section> ... </main>
 *   data-pd-reveal[="fade|zoom|left|right"] [data-pd-delay="ms"]   staged entrance on slide activation
 *   data-pd-countup="1234.5" [data-pd-decimals data-pd-prefix data-pd-suffix data-pd-duration]
 *   data-pd-tabs > [data-pd-tab="k"] + [data-pd-panel="k"]          accessible tabs
 *   <div class="pd-chart" data-chart="charts/x.json" [data-pd-estimate[="イメージ"]]>  ECharts (SVG renderer)
 *   <img src="assets/ai/x.png" data-pd-ai-image>                    adds the 「AI生成イメージ」 caption
 *   data-pd-bleed / aria-hidden="true"                              excluded from layout audits
 * Query flag ?pd_capture=1 disables animation and hides chrome (used by the preview renderer).
 * window.PdDeck exposes ready / go / capture / audit for automated visual review.
 */
(function () {
  "use strict";

  var W = 1920;
  var H = 1080;
  var doc = document;
  var root = doc.documentElement;
  var api = {
    version: "1",
    count: 0,
    current: 0,
    errors: [],
    warnings: [],
    chartErrors: [],
    ready: null
  };
  window.PdDeck = api;

  var deck = doc.getElementById("pd-deck");
  if (!deck) {
    api.errors.push({ type: "no_deck", detail: "main#pd-deck が見つかりません" });
    api.ready = Promise.resolve(api);
    return;
  }

  var slides = Array.prototype.filter.call(deck.children, function (el) {
    return el.tagName === "SECTION" && el.classList.contains("pd-slide");
  });
  api.count = slides.length;
  if (!slides.length) {
    api.errors.push({ type: "no_slides", detail: "section.pd-slide がありません" });
  }

  var params;
  try {
    params = new URLSearchParams(window.location.search);
  } catch (e) {
    params = { get: function () { return null; } };
  }
  var captureMode = params.get("pd_capture") === "1";
  root.classList.add("pd-runtime");
  if (captureMode) {
    root.classList.add("pd-capture", "pd-instant");
  }

  /* ---------- Stage ---------- */
  var stage = doc.createElement("div");
  stage.id = "pd-stage";
  deck.parentNode.insertBefore(stage, deck);
  stage.appendChild(deck);
  var scale = 1;
  function fit() {
    var vw = window.innerWidth || W;
    var vh = window.innerHeight || H;
    scale = Math.min(vw / W, vh / H) || 1;
    deck.style.setProperty("transform", "translate(-50%, -50%) scale(" + scale + ")", "important");
  }
  fit();
  window.addEventListener("resize", fit);

  /* ---------- Chrome (progress, counter, prev/next) ---------- */
  var progress = doc.createElement("div");
  progress.id = "pd-progress";
  progress.className = "pd-chrome";
  var progressBar = doc.createElement("i");
  progress.appendChild(progressBar);

  var nav = doc.createElement("div");
  nav.id = "pd-nav";
  nav.className = "pd-chrome pd-autohide";
  nav.setAttribute("role", "navigation");
  nav.setAttribute("aria-label", "スライド操作");
  function makeButton(label, text) {
    var b = doc.createElement("button");
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.textContent = text;
    return b;
  }
  var prevBtn = makeButton("前のスライド", "\u2039");
  var nextBtn = makeButton("次のスライド", "\u203A");
  var counter = doc.createElement("span");
  counter.id = "pd-counter";
  counter.setAttribute("aria-live", "polite");
  nav.appendChild(prevBtn);
  nav.appendChild(counter);
  nav.appendChild(nextBtn);
  if (!captureMode) {
    doc.body.appendChild(progress);
    doc.body.appendChild(nav);
  }

  /* ---------- Helpers ---------- */
  function clamp(i) {
    var n = parseInt(i, 10);
    if (isNaN(n)) { n = 0; }
    return Math.max(0, Math.min(slides.length - 1, n));
  }
  function raf2() {
    return new Promise(function (resolve) {
      window.requestAnimationFrame(function () { window.requestAnimationFrame(resolve); });
    });
  }
  function slideTitle(s, i) {
    var t = s.getAttribute("data-pd-title");
    if (t) { return t.trim().slice(0, 80); }
    var h = s.querySelector("h1, h2, h3");
    return h ? (h.textContent || "").trim().replace(/\s+/g, " ").slice(0, 80) : "スライド " + (i + 1);
  }

  /* ---------- Reveal ---------- */
  function resetReveal(slide) {
    Array.prototype.forEach.call(slide.querySelectorAll("[data-pd-reveal]"), function (el) {
      el.classList.remove("is-revealed");
    });
  }
  function runReveal(slide, instant) {
    var items = slide.querySelectorAll("[data-pd-reveal]");
    Array.prototype.forEach.call(items, function (el, idx) {
      var d = parseInt(el.getAttribute("data-pd-delay") || "", 10);
      if (isNaN(d)) { d = Math.min(idx * 90, 1200); }
      el.style.setProperty("--pd-delay", (instant ? 0 : Math.max(0, Math.min(d, 4000))) + "ms");
    });
    if (instant) {
      Array.prototype.forEach.call(items, function (el) { el.classList.add("is-revealed"); });
      return;
    }
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        Array.prototype.forEach.call(items, function (el) { el.classList.add("is-revealed"); });
      });
    });
  }

  /* ---------- Count-up ---------- */
  function formatNumber(value, decimals) {
    try {
      return value.toLocaleString("ja-JP", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    } catch (e) {
      return value.toFixed(decimals);
    }
  }
  function runCountups(slide, instant) {
    Array.prototype.forEach.call(slide.querySelectorAll("[data-pd-countup]"), function (el) {
      var raw = String(el.getAttribute("data-pd-countup") || "").replace(/,/g, "").trim();
      var target = parseFloat(raw);
      if (!isFinite(target)) {
        api.warnings.push({ type: "countup_invalid", detail: raw.slice(0, 40) });
        return;
      }
      var dAttr = parseInt(el.getAttribute("data-pd-decimals") || "", 10);
      var decimals = isNaN(dAttr) ? ((raw.split(".")[1] || "").length) : Math.max(0, Math.min(dAttr, 4));
      var prefix = el.getAttribute("data-pd-prefix") || "";
      var suffix = el.getAttribute("data-pd-suffix") || "";
      var duration = parseInt(el.getAttribute("data-pd-duration") || "1200", 10);
      if (isNaN(duration)) { duration = 1200; }
      var finalText = prefix + formatNumber(target, decimals) + suffix;
      if (instant || duration <= 0) {
        el.textContent = finalText;
        return;
      }
      var token = {};
      el.__nyCount = token;
      var start = null;
      function step(ts) {
        if (el.__nyCount !== token) { return; }
        if (start === null) { start = ts; }
        var p = Math.min(1, (ts - start) / duration);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = prefix + formatNumber(target * eased, decimals) + suffix;
        if (p < 1) { window.requestAnimationFrame(step); } else { el.textContent = finalText; }
      }
      window.requestAnimationFrame(step);
    });
  }

  /* ---------- Tabs ---------- */
  function initTabs() {
    Array.prototype.forEach.call(deck.querySelectorAll("[data-pd-tabs]"), function (box) {
      var tabs = Array.prototype.slice.call(box.querySelectorAll("[data-pd-tab]"));
      var panels = Array.prototype.slice.call(box.querySelectorAll("[data-pd-panel]"));
      if (!tabs.length) { return; }
      function select(key) {
        tabs.forEach(function (t) {
          var on = t.getAttribute("data-pd-tab") === key;
          t.classList.toggle("is-active", on);
          t.setAttribute("aria-selected", on ? "true" : "false");
          t.setAttribute("tabindex", on ? "0" : "-1");
        });
        panels.forEach(function (p) {
          p.classList.toggle("is-active", p.getAttribute("data-pd-panel") === key);
        });
        window.setTimeout(resizeAllCharts, 30);
      }
      tabs.forEach(function (t) {
        t.setAttribute("role", "tab");
        if (t.tagName !== "BUTTON" && !t.hasAttribute("tabindex")) { t.setAttribute("tabindex", "0"); }
        t.addEventListener("click", function (ev) {
          ev.preventDefault();
          select(t.getAttribute("data-pd-tab"));
        });
        t.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            ev.stopPropagation();
            select(t.getAttribute("data-pd-tab"));
          }
        });
      });
      var initial = tabs.filter(function (t) { return t.classList.contains("is-active"); })[0] || tabs[0];
      select(initial.getAttribute("data-pd-tab"));
    });
  }

  /* ---------- Charts (ECharts, SVG renderer) ---------- */
  var charts = [];
  var CHART_PATH = /^charts\/[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json$/;
  function scrub(value, depth) {
    if (depth > 40) { return null; }
    if (typeof value === "string") { return value.replace(/[<>]/g, ""); }
    if (Array.isArray(value)) {
      return value.map(function (v) { return scrub(v, depth + 1); });
    }
    if (value && typeof value === "object") {
      var out = {};
      Object.keys(value).forEach(function (k) {
        if (k === "__proto__" || k === "constructor" || k === "prototype") { return; }
        out[k] = scrub(value[k], depth + 1);
      });
      return out;
    }
    return value;
  }
  function forceRichTooltip(opt) {
    var tips = Array.isArray(opt.tooltip) ? opt.tooltip : (opt.tooltip ? [opt.tooltip] : []);
    tips.forEach(function (t) {
      if (t && typeof t === "object") { t.renderMode = "richText"; }
    });
  }
  function addBadge(el, cls, text) {
    var b = doc.createElement("span");
    b.className = cls;
    b.textContent = text;
    el.appendChild(b);
    return b;
  }
  function chartError(el, message) {
    api.chartErrors.push({ chart: el.getAttribute("data-chart") || "", detail: String(message).slice(0, 200) });
    var box = doc.createElement("div");
    box.className = "pd-chart-error";
    box.textContent = "グラフを表示できませんでした";
    el.appendChild(box);
  }
  /* Slides are designed on a 1920x1080 canvas: ECharts' 12px defaults are unreadable there.
     Fill in larger sizes only where the author did not specify one. */
  function withDefaults(target, defaults) {
    return Object.assign({}, defaults, (target && typeof target === "object") ? target : {});
  }
  function eachObj(value, fn) {
    (Array.isArray(value) ? value : [value]).forEach(function (v) {
      if (v && typeof v === "object") { fn(v); }
    });
  }
  function readableDefaults(opt, el) {
    opt.textStyle = withDefaults(opt.textStyle, { fontSize: 22, fontFamily: window.getComputedStyle(el).fontFamily });
    ["xAxis", "yAxis", "radiusAxis", "angleAxis", "singleAxis", "parallelAxis"].forEach(function (k) {
      if (!opt[k]) { return; }
      eachObj(opt[k], function (ax) {
        ax.axisLabel = withDefaults(ax.axisLabel, { fontSize: 20 });
        ax.nameTextStyle = withDefaults(ax.nameTextStyle, { fontSize: 20 });
      });
    });
    if (opt.legend) { eachObj(opt.legend, function (lg) { lg.textStyle = withDefaults(lg.textStyle, { fontSize: 20 }); }); }
    if (opt.title) {
      eachObj(opt.title, function (t) {
        t.textStyle = withDefaults(t.textStyle, { fontSize: 28 });
        t.subtextStyle = withDefaults(t.subtextStyle, { fontSize: 20 });
      });
    }
    if (opt.radar) {
      eachObj(opt.radar, function (r) { r.axisName = withDefaults(r.axisName, { fontSize: 20 }); });
    }
    if (Array.isArray(opt.series)) {
      opt.series.forEach(function (s) {
        if (s && typeof s === "object" && s.label && typeof s.label === "object") {
          s.label = withDefaults(s.label, { fontSize: 20 });
        }
      });
    }
  }
  function initCharts() {
    var jobs = [];
    Array.prototype.forEach.call(deck.querySelectorAll(".pd-chart"), function (el) {
      if (el.hasAttribute("data-pd-estimate")) {
        addBadge(el, "pd-chart-badge", (el.getAttribute("data-pd-estimate") || "").trim() || "試算");
      }
      var src = (el.getAttribute("data-chart") || "").trim();
      if (!CHART_PATH.test(src)) {
        chartError(el, "data-chart は charts/<name>.json 形式で指定してください: " + src);
        return;
      }
      if (typeof window.echarts === "undefined") {
        chartError(el, "ECharts が読み込まれていません");
        return;
      }
      if (el.clientHeight < 40 || el.clientWidth < 40) {
        api.warnings.push({ type: "chart_unsized", detail: src });
        if (el.clientHeight < 40) { el.style.height = "480px"; }
        if (el.clientWidth < 40) { el.style.width = "100%"; }
      }
      jobs.push(
        window.fetch(src, { credentials: "same-origin", cache: "no-cache" })
          .then(function (r) {
            if (!r.ok) { throw new Error("HTTP " + r.status + " " + src); }
            return r.json();
          })
          .then(function (opt) {
            if (!opt || typeof opt !== "object" || Array.isArray(opt)) {
              throw new Error("option は JSON オブジェクトである必要があります: " + src);
            }
            opt = scrub(opt, 0);
            forceRichTooltip(opt);
            readableDefaults(opt, el);
            if (captureMode) { opt.animation = false; }
            var theme = el.getAttribute("data-chart-theme") === "dark" ? "dark" : null;
            var inst = window.echarts.init(el, theme, { renderer: "svg" });
            inst.setOption(opt);
            charts.push(inst);
            return new Promise(function (resolve) {
              var done = false;
              function finish() { if (!done) { done = true; resolve(); } }
              try { inst.on("finished", finish); } catch (e) { finish(); }
              window.setTimeout(finish, captureMode ? 800 : 2500);
            });
          })
          .catch(function (e) {
            chartError(el, (e && e.message) || e);
          })
      );
    });
    return Promise.all(jobs);
  }
  function resizeAllCharts() {
    charts.forEach(function (c) {
      try { c.resize(); } catch (e) { /* ignore */ }
    });
  }

  /* ---------- AI image captions ---------- */
  function placeAiBadges() {
    Array.prototype.forEach.call(deck.querySelectorAll("img[data-pd-ai-image]"), function (img) {
      var host = img.offsetParent;
      if (!host) { return; }
      var badge = img.__nyBadge;
      if (!badge) {
        badge = doc.createElement("span");
        badge.className = "pd-ai-badge";
        badge.setAttribute("aria-hidden", "true");
        badge.textContent = (img.getAttribute("data-pd-ai-image") || "").trim() || "AI生成イメージ";
        host.appendChild(badge);
        img.__nyBadge = badge;
      }
      badge.style.left = (img.offsetLeft + img.offsetWidth) + "px";
      badge.style.top = (img.offsetTop + img.offsetHeight) + "px";
    });
  }
  function imagesSettled() {
    var imgs = Array.prototype.slice.call(deck.querySelectorAll("img"));
    return Promise.all(imgs.map(function (img) {
      if (img.complete) { return Promise.resolve(); }
      return new Promise(function (resolve) {
        img.addEventListener("load", resolve, { once: true });
        img.addEventListener("error", resolve, { once: true });
      });
    }));
  }

  /* ---------- Navigation ---------- */
  var storeKey = "pd-slide:" + window.location.pathname;
  function initialIndex() {
    var m = /^#\/?(\d+)$/.exec(window.location.hash || "");
    if (m) { return clamp(parseInt(m[1], 10) - 1); }
    try {
      var saved = window.sessionStorage.getItem(storeKey);
      if (saved !== null) { return clamp(saved); }
    } catch (e) { /* storage disabled */ }
    return 0;
  }
  function updateChrome() {
    counter.textContent = (api.current + 1) + " / " + slides.length;
    progressBar.style.width = (slides.length ? ((api.current + 1) / slides.length) * 100 : 0) + "%";
    prevBtn.disabled = api.current <= 0;
    nextBtn.disabled = api.current >= slides.length - 1;
  }
  function activate(i, opts) {
    if (!slides.length) { return; }
    opts = opts || {};
    var next = clamp(i);
    var prev = api.current;
    slides.forEach(function (s, k) {
      var on = k === next;
      s.classList.toggle("is-active", on);
      s.setAttribute("aria-hidden", on ? "false" : "true");
      if (on) { s.removeAttribute("inert"); } else { s.setAttribute("inert", ""); }
    });
    if (prev !== next && slides[prev] && !opts.keepPrev) { resetReveal(slides[prev]); }
    api.current = next;
    updateChrome();
    try { window.sessionStorage.setItem(storeKey, String(next)); } catch (e) { /* ignore */ }
    if (!opts.noHash && !captureMode) {
      try { window.history.replaceState(null, "", "#/" + (next + 1)); } catch (e) { /* ignore */ }
    }
    runReveal(slides[next], !!opts.instant || captureMode);
    runCountups(slides[next], !!opts.instant || captureMode);
    window.setTimeout(resizeAllCharts, 0);
  }
  function go(i, opts) { activate(i, opts); return api.current; }
  api.go = go;
  api.next = function () { return go(api.current + 1); };
  api.prev = function () { return go(api.current - 1); };
  prevBtn.addEventListener("click", function () { api.prev(); });
  nextBtn.addEventListener("click", function () { api.next(); });

  function isInteractive(el) {
    return !!(el && el.closest && el.closest("button, a, input, textarea, select, [data-pd-tab], [contenteditable='true']"));
  }
  doc.addEventListener("keydown", function (ev) {
    if (ev.defaultPrevented || ev.altKey || ev.ctrlKey || ev.metaKey) { return; }
    var k = ev.key;
    if (k === "ArrowRight" || k === "PageDown" || (k === " " && !isInteractive(ev.target))) {
      ev.preventDefault();
      api.next();
    } else if (k === "ArrowLeft" || k === "PageUp") {
      ev.preventDefault();
      api.prev();
    } else if (k === "Home") {
      ev.preventDefault();
      go(0);
    } else if (k === "End") {
      ev.preventDefault();
      go(slides.length - 1);
    }
  });
  window.addEventListener("hashchange", function () {
    var m = /^#\/?(\d+)$/.exec(window.location.hash || "");
    if (m) { go(parseInt(m[1], 10) - 1, { noHash: true }); }
  });
  var touchX = null;
  var touchY = null;
  doc.addEventListener("touchstart", function (ev) {
    if (ev.touches.length === 1) { touchX = ev.touches[0].clientX; touchY = ev.touches[0].clientY; }
  }, { passive: true });
  doc.addEventListener("touchend", function (ev) {
    if (touchX === null || !ev.changedTouches.length) { return; }
    var dx = ev.changedTouches[0].clientX - touchX;
    var dy = ev.changedTouches[0].clientY - touchY;
    touchX = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.3) {
      if (dx < 0) { api.next(); } else { api.prev(); }
    }
  }, { passive: true });
  var idleTimer = null;
  function wake() {
    root.classList.remove("pd-idle");
    if (idleTimer) { window.clearTimeout(idleTimer); }
    idleTimer = window.setTimeout(function () { root.classList.add("pd-idle"); }, 2600);
  }
  doc.addEventListener("mousemove", wake, { passive: true });
  wake();

  /* ---------- Layout audit (used by the preview renderer) ---------- */
  function describe(el) {
    var s = el.tagName.toLowerCase();
    if (el.id) { s += "#" + el.id; }
    if (el.classList && el.classList.length) {
      s += "." + Array.prototype.slice.call(el.classList, 0, 3).join(".");
    }
    var t = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 36);
    return t ? s + " 「" + t + "」" : s;
  }
  function ownText(el) {
    var out = "";
    for (var n = el.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) { out += n.nodeValue; }
    }
    return out.replace(/\s+/g, " ").trim();
  }
  function parseColor(str) {
    var m = /rgba?\(([^)]+)\)/.exec(str || "");
    if (!m) { return null; }
    var p = m[1].split(/[,\s/]+/).filter(Boolean).map(parseFloat);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function luminance(c) {
    function ch(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * ch(c.r) + 0.7152 * ch(c.g) + 0.0722 * ch(c.b);
  }
  function effectiveBackground(el) {
    for (var cur = el; cur && cur.nodeType === 1; cur = cur.parentElement) {
      var cs = window.getComputedStyle(cur);
      if (cs.backgroundImage && cs.backgroundImage !== "none") { return null; }
      var c = parseColor(cs.backgroundColor);
      if (c && c.a >= 0.85) { return c; }
      if (cur === deck) { break; }
    }
    var bodyBg = parseColor(window.getComputedStyle(doc.body).backgroundColor);
    return bodyBg && bodyBg.a >= 0.85 ? bodyBg : { r: 255, g: 255, b: 255, a: 1 };
  }
  function excluded(el) {
    return !!el.closest("[data-pd-bleed], [aria-hidden='true'], .pd-chart-badge, .pd-ai-badge");
  }
  function audit(i) {
    var idx = clamp(i === undefined ? api.current : i);
    var slide = slides[idx];
    var result = { index: idx, title: slideTitle(slide, idx), issues: [], stats: {} };
    if (!slide) { return result; }
    var sr = slide.getBoundingClientRect();
    var k = (sr.width / W) || 1;
    var textBoxes = [];
    var smallText = [];
    var textChars = 0;
    var media = 0;
    function push(type, severity, el, detail) {
      if (result.issues.length >= 40) { return; }
      result.issues.push({ type: type, severity: severity, element: el ? describe(el) : "", detail: detail || "" });
    }
    var all = slide.querySelectorAll("*");
    for (var n = 0; n < all.length; n++) {
      var el = all[n];
      if (el.ownerSVGElement) { continue; }
      if (el.closest(".pd-chart") && !el.classList.contains("pd-chart")) { continue; }
      var cs = window.getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) { continue; }
      var r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) { continue; }
      var tag = el.tagName;
      var text = ownText(el);
      var isMedia = tag === "IMG" || tag === "svg" || tag === "SVG" || el.classList.contains("pd-chart") || tag === "VIDEO" || tag === "CANVAS";
      if (isMedia) { media += 1; }
      if (text) { textChars += text.length; }
      if (excluded(el)) { continue; }
      if (text || isMedia) {
        var ox = Math.max(0, sr.left - r.left, r.right - sr.right) / k;
        var oy = Math.max(0, sr.top - r.top, r.bottom - sr.bottom) / k;
        if (ox > 4 || oy > 4) {
          push("overflow", "error", el, "スライド枠から " + Math.round(Math.max(ox, oy)) + "px はみ出しています");
        }
      }
      if (tag === "IMG" && el.complete && el.naturalWidth === 0) {
        push("broken_image", "error", el, el.getAttribute("src") || "");
      }
      if (text) {
        var clipsX = /(hidden|clip|auto|scroll)/.test(cs.overflowX);
        var clipsY = /(hidden|clip|auto|scroll)/.test(cs.overflowY);
        if ((clipsY && el.scrollHeight - el.clientHeight > 4) || (clipsX && el.scrollWidth - el.clientWidth > 4)) {
          push("text_clipped", "error", el, "文字が枠内に収まらず切れています");
        }
        var fs = parseFloat(cs.fontSize) || 0;
        if (fs && fs < 18 && text.length >= 4) { smallText.push(describe(el) + " " + Math.round(fs) + "px"); }
        var fg = parseColor(cs.color);
        var bg = effectiveBackground(el);
        if (fg && bg) {
          var a = isNaN(fg.a) ? 1 : fg.a;
          var blended = { r: fg.r * a + bg.r * (1 - a), g: fg.g * a + bg.g * (1 - a), b: fg.b * a + bg.b * (1 - a) };
          var l1 = luminance(blended);
          var l2 = luminance(bg);
          var ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
          var need = fs >= 24 ? 3 : 4.5;
          if (ratio < need) {
            push("low_contrast", "warning", el, "コントラスト比 " + ratio.toFixed(2) + "（目安 " + need + " 以上）");
          }
        }
        textBoxes.push({ el: el, r: r });
      }
    }
    for (var a1 = 0; a1 < textBoxes.length; a1++) {
      for (var b1 = a1 + 1; b1 < textBoxes.length; b1++) {
        var A = textBoxes[a1];
        var B = textBoxes[b1];
        if (A.el.contains(B.el) || B.el.contains(A.el)) { continue; }
        var ix = Math.min(A.r.right, B.r.right) - Math.max(A.r.left, B.r.left);
        var iy = Math.min(A.r.bottom, B.r.bottom) - Math.max(A.r.top, B.r.top);
        if (ix <= 2 || iy <= 2) { continue; }
        var inter = (ix * iy) / (k * k);
        var smaller = Math.min(A.r.width * A.r.height, B.r.width * B.r.height) / (k * k);
        if (smaller > 300 && inter / smaller > 0.25) {
          push("text_overlap", "error", A.el, "テキストが重なっています: " + describe(B.el));
        }
      }
    }
    if (smallText.length) {
      push("small_text", "warning", null, "18px 未満の文字が " + smallText.length + " 箇所: " + smallText.slice(0, 4).join(" / "));
    }
    if (!textChars && !media) {
      push("empty_slide", "error", slide, "表示される内容がありません");
    }
    result.stats = { text_chars: textChars, media: media, charts: slide.querySelectorAll(".pd-chart").length };
    return result;
  }
  api.audit = audit;
  api.listSlides = function () {
    return slides.map(function (s, i) { return { index: i, title: slideTitle(s, i) }; });
  };
  api.capture = function (i) {
    root.classList.add("pd-instant");
    activate(i, { instant: true, noHash: true });
    Array.prototype.forEach.call(slides[api.current].querySelectorAll("[data-pd-reveal]"), function (el) {
      el.classList.add("is-revealed");
    });
    placeAiBadges();
    resizeAllCharts();
    return raf2().then(function () { return api.current; });
  };

  /* ---------- Boot ---------- */
  initTabs();
  activate(initialIndex(), { noHash: true });
  var fontsReady = (doc.fonts && doc.fonts.ready) ? doc.fonts.ready.catch(function () {}) : Promise.resolve();
  var timeout = new Promise(function (resolve) { window.setTimeout(resolve, 9000); });
  api.ready = Promise.race([
    Promise.all([fontsReady, imagesSettled()]).then(function () {
      return Promise.all([initCharts(), raf2()]);
    }),
    timeout
  ]).then(function () {
    placeAiBadges();
    resizeAllCharts();
    root.classList.add("pd-ready");
    return api;
  });
  window.addEventListener("resize", function () { window.setTimeout(placeAiBadges, 50); });
  window.addEventListener("beforeprint", function () { resizeAllCharts(); placeAiBadges(); });
})();
