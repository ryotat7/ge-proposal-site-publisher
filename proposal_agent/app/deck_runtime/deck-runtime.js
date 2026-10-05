/*!
 * NY deck runtime v1 — shared presentation & web proposal portal runtime for free-form decks (Apache-2.0).
 *
 * Authoring contract (agents write HTML/CSS/SVG/JSON only, never JavaScript):
 *   <main id="pd-deck" [data-pd-layout="portal|slides"]>
 *     <section class="pd-slide" data-pd-title="..." [data-pd-chapter="..."]> ... </section>
 *   </main>
 *   data-pd-reveal[="fade|zoom|left|right"] [data-pd-delay="ms"]   staged entrance on slide activation
 *   data-pd-countup="1234.5" [data-pd-decimals data-pd-prefix data-pd-suffix data-pd-duration]
 *   data-pd-tabs > [data-pd-tab="k"] + [data-pd-panel="k"]          accessible tabs
 *   <div class="pd-chart" data-chart="charts/x.json" [data-pd-estimate[="イメージ"]]>  ECharts (SVG renderer)
 *   <img src="assets/ai/x.png" data-pd-ai-image>                    adds the 「AI生成イメージ」 caption
 *   data-pd-bleed / aria-hidden="true"                              excluded from layout audits
 * Query flag ?pd_capture=1 disables animation and hides chrome (used by the preview renderer).
 * Query flag ?view=portal|slides overrides the default layout mode.
 * window.PdDeck exposes ready / go / capture / audit / setPortalMode / setSlideView.
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
  var viewParam = (params.get("view") || "").toLowerCase();
  var deckLayout = (deck.getAttribute("data-pd-layout") || "").toLowerCase();
  var hasPortalMarkers = slides.some(function (s) {
    return s.hasAttribute("data-pd-chapter") || !!s.querySelector(".pd-hero, .hero, .kpi-strip, .pd-kpi-strip");
  });
  var initialPortal = viewParam === "portal" || (viewParam !== "slides" && (deckLayout === "portal" || (deckLayout !== "slides" && hasPortalMarkers)));

  root.classList.add("pd-runtime");
  if (initialPortal) {
    root.classList.add("pd-portal");
  }
  if (captureMode) {
    root.classList.add("pd-capture", "pd-instant");
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
    if (!s) { return "スライド " + (i + 1); }
    var t = s.getAttribute("data-pd-title");
    if (t) { return t.trim().slice(0, 80); }
    var h = s.querySelector("h1, h2, h3");
    return h ? (h.textContent || "").trim().replace(/\s+/g, " ").slice(0, 80) : "スライド " + (i + 1);
  }
  function slideChapter(s, i) {
    if (!s) { return "Chapter " + (i + 1); }
    var c = s.getAttribute("data-pd-chapter");
    if (c) { return c.trim().slice(0, 60); }
    return slideTitle(s, i);
  }
  function slideCode(s, i) {
    if (!s) { return ("0" + (i + 1)).slice(-2); }
    var code = s.getAttribute("data-pd-code");
    return code ? code.trim().slice(0, 6) : ("0" + (i + 1)).slice(-2);
  }

  /* ---------- Stage & 4-Column Portal Shell ---------- */
  var stage = doc.createElement("div");
  stage.id = "pd-stage";
  deck.parentNode.insertBefore(stage, deck);

  var portalShell = doc.createElement("div");
  portalShell.className = "pd-portal-shell";
  stage.parentNode.insertBefore(portalShell, stage);

  var rail = doc.createElement("nav");
  rail.className = "pd-rail pd-portal-only";
  rail.setAttribute("aria-label", "章ナビゲーション");

  var sidebar = doc.createElement("aside");
  sidebar.className = "pd-sidebar pd-portal-only";
  sidebar.setAttribute("aria-label", "目次と構成");

  var reader = doc.createElement("div");
  reader.className = "pd-reader";

  var slideBar = doc.createElement("div");
  slideBar.className = "pd-slide-bar pd-portal-only";
  var slideBarTitle = doc.createElement("div");
  slideBarTitle.className = "pd-slide-bar-title";
  var slideExitBtn = doc.createElement("button");
  slideExitBtn.type = "button";
  slideExitBtn.id = "pd-portal-article-toggle";
  slideExitBtn.className = "pd-slide-exit-btn";
  slideExitBtn.setAttribute("aria-label", "記事に戻る");
  slideExitBtn.textContent = "✕ 記事に戻る";
  slideBar.appendChild(slideBarTitle);
  slideBar.appendChild(slideExitBtn);

  var confBar = doc.createElement("div");
  confBar.className = "pd-confbar pd-portal-only";
  var confLeft = doc.createElement("span");
  confLeft.textContent = deck.getAttribute("data-pd-confidential") || "CONFIDENTIAL — 取扱注意 · ご提案ポータル";
  var confRight = doc.createElement("span");
  confRight.id = "pd-readtime";
  confBar.appendChild(confLeft);
  confBar.appendChild(confRight);

  var readerBar = doc.createElement("header");
  readerBar.className = "pd-reader-bar pd-portal-only";
  var breadcrumb = doc.createElement("div");
  breadcrumb.id = "pd-breadcrumb";
  breadcrumb.className = "pd-breadcrumb";
  var readerActions = doc.createElement("div");
  readerActions.className = "pd-reader-actions";
  var slideToggleBtn = doc.createElement("button");
  slideToggleBtn.type = "button";
  slideToggleBtn.id = "pd-portal-slide-toggle";
  slideToggleBtn.className = "pd-slide-toggle-btn";
  slideToggleBtn.setAttribute("aria-label", "スライドで見る");
  slideToggleBtn.textContent = "▢ スライドで見る";
  readerActions.appendChild(slideToggleBtn);
  readerBar.appendChild(breadcrumb);
  readerBar.appendChild(readerActions);

  var articleWrap = doc.createElement("div");
  articleWrap.className = "pd-article-wrap";
  articleWrap.appendChild(stage);
  stage.appendChild(deck);

  var pager = doc.createElement("nav");
  pager.className = "pd-pager pd-portal-only";
  pager.setAttribute("aria-label", "前後の章へ移動");
  var pagerPrev = doc.createElement("button");
  pagerPrev.type = "button";
  pagerPrev.id = "pd-pager-prev";
  pagerPrev.className = "pd-pager-btn is-prev";
  var pagerNext = doc.createElement("button");
  pagerNext.type = "button";
  pagerNext.id = "pd-pager-next";
  pagerNext.className = "pd-pager-btn is-next";
  pager.appendChild(pagerPrev);
  pager.appendChild(pagerNext);
  articleWrap.appendChild(pager);

  var footer = doc.createElement("footer");
  footer.className = "pd-footer pd-portal-only";
  var footLeft = doc.createElement("span");
  footLeft.textContent = deck.getAttribute("data-pd-footer") || (doc.title || "Interactive Proposal Portal");
  var footRight = doc.createElement("span");
  footRight.textContent = "← / → キーまたは左メニューで章を切り替え";
  footer.appendChild(footLeft);
  footer.appendChild(footRight);

  reader.appendChild(slideBar);
  reader.appendChild(confBar);
  reader.appendChild(readerBar);
  reader.appendChild(articleWrap);
  reader.appendChild(footer);

  var refsCol = doc.createElement("aside");
  refsCol.className = "pd-refs pd-portal-only";
  refsCol.setAttribute("aria-label", "根拠・KPIリファレンス");

  portalShell.appendChild(rail);
  portalShell.appendChild(sidebar);
  portalShell.appendChild(reader);
  portalShell.appendChild(refsCol);

  /* Populate Rail & Sidebar */
  var railBtns = [];
  var chapterBtns = [];
  var tocList = doc.createElement("div");
  tocList.id = "pd-toc-list";
  tocList.className = "pd-toc-list";

  function buildPortalChrome() {
    var brand = doc.createElement("div");
    brand.className = "pd-rail-brand";
    brand.textContent = (deck.getAttribute("data-pd-brand") || "NY").slice(0, 3);
    rail.appendChild(brand);

    var sideHead = doc.createElement("div");
    sideHead.className = "pd-sidebar-head";
    var badge = doc.createElement("span");
    badge.className = "pd-doc-badge";
    badge.textContent = deck.getAttribute("data-pd-badge") || "PROPOSAL PORTAL";
    var clientEl = doc.createElement("h2");
    clientEl.className = "pd-client";
    clientEl.textContent = deck.getAttribute("data-pd-client") || doc.title || "ご提案ポータル";
    var metaEl = doc.createElement("div");
    metaEl.className = "pd-doc-meta";
    metaEl.textContent = deck.getAttribute("data-pd-meta") || ("全 " + slides.length + " 章 · Web提案ポータル");
    sideHead.appendChild(badge);
    sideHead.appendChild(clientEl);
    sideHead.appendChild(metaEl);
    sidebar.appendChild(sideHead);

    var chapGroup = doc.createElement("div");
    var chapLabel = doc.createElement("div");
    chapLabel.className = "pd-sidebar-label";
    chapLabel.textContent = "CHAPTERS";
    var chapList = doc.createElement("div");
    chapList.className = "pd-chapter-list";
    chapGroup.appendChild(chapLabel);
    chapGroup.appendChild(chapList);
    sidebar.appendChild(chapGroup);

    slides.forEach(function (s, idx) {
      var code = slideCode(s, idx);
      var chap = slideChapter(s, idx);
      var title = slideTitle(s, idx);
      var sub = s.getAttribute("data-pd-subtitle") || (chap !== title ? title : "");

      var rb = doc.createElement("button");
      rb.type = "button";
      rb.className = "pd-rail-btn";
      rb.setAttribute("data-pd-goto", String(idx));
      rb.setAttribute("title", code + " " + chap);
      rb.setAttribute("aria-label", code + " " + chap);
      rb.textContent = code;
      rb.addEventListener("click", function () { go(idx); });
      rail.appendChild(rb);
      railBtns.push(rb);

      var cb = doc.createElement("button");
      cb.type = "button";
      cb.className = "pd-chapter-btn";
      cb.setAttribute("data-pd-goto", String(idx));
      var codeSpan = doc.createElement("span");
      codeSpan.className = "pd-chapter-code";
      codeSpan.textContent = code;
      var textWrap = doc.createElement("span");
      var nameSpan = doc.createElement("span");
      nameSpan.className = "pd-chapter-name";
      nameSpan.textContent = chap;
      textWrap.appendChild(nameSpan);
      if (sub) {
        var subSpan = doc.createElement("span");
        subSpan.className = "pd-chapter-sub";
        subSpan.textContent = sub.slice(0, 48);
        textWrap.appendChild(subSpan);
      }
      cb.appendChild(codeSpan);
      cb.appendChild(textWrap);
      cb.addEventListener("click", function () { go(idx); });
      chapList.appendChild(cb);
      chapterBtns.push(cb);
    });

    var spacer = doc.createElement("div");
    spacer.className = "pd-rail-spacer";
    rail.appendChild(spacer);

    var railSlideBtn = doc.createElement("button");
    railSlideBtn.type = "button";
    railSlideBtn.className = "pd-rail-btn pd-rail-foot";
    railSlideBtn.setAttribute("title", "スライド表示切替");
    railSlideBtn.setAttribute("aria-label", "スライド表示切替");
    railSlideBtn.textContent = "▢";
    railSlideBtn.addEventListener("click", function () {
      setSlideView(!root.classList.contains("pd-portal-slides"));
    });
    rail.appendChild(railSlideBtn);

    var tocBox = doc.createElement("div");
    tocBox.className = "pd-toc-box";
    var tocLabel = doc.createElement("div");
    tocLabel.className = "pd-sidebar-label";
    tocLabel.textContent = "ON THIS PAGE";
    tocBox.appendChild(tocLabel);
    tocBox.appendChild(tocList);
    sidebar.appendChild(tocBox);
  }
  buildPortalChrome();

  var scale = 1;
  function fit() {
    if (root.classList.contains("pd-portal")) {
      scale = 1;
      deck.style.removeProperty("transform");
      return;
    }
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
  var modeBtn = makeButton("表示モード切替", "⇄");
  modeBtn.className = "pd-mode-toggle";
  var counter = doc.createElement("span");
  counter.id = "pd-counter";
  counter.setAttribute("aria-live", "polite");
  nav.appendChild(prevBtn);
  nav.appendChild(counter);
  nav.appendChild(nextBtn);
  nav.appendChild(modeBtn);
  if (!captureMode) {
    doc.body.appendChild(progress);
    doc.body.appendChild(nav);
  }

  function setSlideView(on) {
    if (!root.classList.contains("pd-portal")) {
      root.classList.toggle("pd-portal", !on);
    } else {
      root.classList.toggle("pd-portal-slides", !!on);
    }
    fit();
    try { window.scrollTo(0, 0); } catch (e) { /* ignore */ }
    window.setTimeout(function () {
      resizeAllCharts();
      placeAiBadges();
    }, 60);
  }
  function setPortalMode(on) {
    root.classList.toggle("pd-portal", !!on);
    if (!on) { root.classList.remove("pd-portal-slides"); }
    fit();
    window.setTimeout(function () {
      resizeAllCharts();
      placeAiBadges();
    }, 60);
  }
  api.setSlideView = setSlideView;
  api.setPortalMode = setPortalMode;

  slideToggleBtn.addEventListener("click", function () { setSlideView(true); });
  slideExitBtn.addEventListener("click", function () { setSlideView(false); });
  modeBtn.addEventListener("click", function () {
    if (root.classList.contains("pd-portal-slides")) {
      setSlideView(false);
    } else if (root.classList.contains("pd-portal")) {
      setSlideView(true);
    } else {
      setPortalMode(true);
    }
  });

  /* ---------- Portal TOC & Right Reference Column ---------- */
  var currentTocHeadings = [];
  function updatePortalToc(slide) {
    while (tocList.firstChild) { tocList.removeChild(tocList.firstChild); }
    currentTocHeadings = [];
    if (!slide) { return; }
    var headings = Array.prototype.slice.call(slide.querySelectorAll("h1, h2, h3"));
    headings.forEach(function (h, idx) {
      var text = (h.textContent || "").trim().replace(/\s+/g, " ");
      if (!text) { return; }
      var btn = doc.createElement("button");
      btn.type = "button";
      btn.className = "pd-toc-link" + (h.tagName === "H3" ? " is-sub" : "") + (idx === 0 ? " is-active" : "");
      btn.textContent = text.slice(0, 42);
      btn.addEventListener("click", function () {
        Array.prototype.forEach.call(tocList.querySelectorAll(".pd-toc-link"), function (el) {
          el.classList.toggle("is-active", el === btn);
        });
        try { h.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) { /* ignore */ }
      });
      tocList.appendChild(btn);
      currentTocHeadings.push({ el: h, btn: btn });
    });
  }

  window.addEventListener("scroll", function () {
    if (!root.classList.contains("pd-portal") || !currentTocHeadings.length) { return; }
    var activeIdx = 0;
    for (var i = 0; i < currentTocHeadings.length; i++) {
      var r = currentTocHeadings[i].el.getBoundingClientRect();
      if (r.top <= 140) { activeIdx = i; }
    }
    currentTocHeadings.forEach(function (item, idx) {
      item.btn.classList.toggle("is-active", idx === activeIdx);
    });
  }, { passive: true });

  function updatePortalRefs(slide, idx) {
    while (refsCol.firstChild) { refsCol.removeChild(refsCol.firstChild); }
    if (!slide) { return; }

    var authorRefs = slide.querySelectorAll("aside.pd-refs, [data-pd-ref]");
    Array.prototype.forEach.call(authorRefs, function (asideEl) {
      var card = doc.createElement("div");
      card.className = "pd-ref-card";
      var hEl = asideEl.querySelector("h3, h4, h5, .pd-ref-title");
      var titleText = (hEl ? hEl.textContent : (asideEl.getAttribute("data-pd-ref-title") || "REFERENCES & NOTES")) || "REFERENCES & NOTES";
      var tDiv = doc.createElement("div");
      tDiv.className = "pd-ref-title";
      tDiv.textContent = titleText.trim().slice(0, 48);
      card.appendChild(tDiv);
      var items = asideEl.querySelectorAll("li, p");
      if (items.length) {
        Array.prototype.forEach.call(items, function (it) {
          var txt = (it.textContent || "").trim().replace(/\s+/g, " ");
          if (!txt) { return; }
          var d = doc.createElement("div");
          d.className = "pd-ref-item";
          d.textContent = txt.slice(0, 140);
          card.appendChild(d);
        });
      } else {
        var rawTxt = (asideEl.textContent || "").replace(titleText, "").trim().replace(/\s+/g, " ");
        if (rawTxt) {
          var d2 = doc.createElement("div");
          d2.className = "pd-ref-item";
          d2.textContent = rawTxt.slice(0, 160);
          card.appendChild(d2);
        }
      }
      refsCol.appendChild(card);
    });

    var kpiCard = doc.createElement("div");
    kpiCard.className = "pd-ref-card";
    var kpiTitle = doc.createElement("div");
    kpiTitle.className = "pd-ref-title";
    kpiTitle.textContent = "KEY HIGHLIGHTS";
    kpiCard.appendChild(kpiTitle);

    var highlights = [];
    var kpiNodes = slide.querySelectorAll(".pd-kpi, .kpi, [data-pd-countup], strong");
    Array.prototype.forEach.call(kpiNodes, function (node) {
      if (highlights.length >= 4) { return; }
      var t = (node.textContent || "").trim().replace(/\s+/g, " ").slice(0, 64);
      if (t && t.length >= 2 && highlights.indexOf(t) === -1) {
        highlights.push(t);
      }
    });
    if (!highlights.length) {
      highlights.push(slideTitle(slide, idx));
    }
    highlights.forEach(function (text) {
      var item = doc.createElement("div");
      item.className = "pd-ref-item";
      item.textContent = text;
      kpiCard.appendChild(item);
    });
    refsCol.appendChild(kpiCard);

    var navCard = doc.createElement("div");
    navCard.className = "pd-ref-card";
    var navTitle = doc.createElement("div");
    navTitle.className = "pd-ref-title";
    navTitle.textContent = "DOCUMENT STRUCTURE";
    navCard.appendChild(navTitle);
    var chartCount = slide.querySelectorAll(".pd-chart").length;
    var svgCount = slide.querySelectorAll("svg").length;
    var tableCount = slide.querySelectorAll("table").length;
    var statsItem = doc.createElement("div");
    statsItem.className = "pd-ref-item";
    statsItem.textContent = "章番号: " + slideCode(slide, idx) + " / " + ("0" + slides.length).slice(-2);
    navCard.appendChild(statsItem);
    if (chartCount || svgCount || tableCount) {
      var figItem = doc.createElement("div");
      figItem.className = "pd-ref-item";
      figItem.textContent = "図表・構成図: チャート " + chartCount + " / 図解 " + svgCount + " / 表 " + tableCount;
      navCard.appendChild(figItem);
    }
    var hintItem = doc.createElement("div");
    hintItem.className = "pd-ref-item";
    hintItem.textContent = "右上の「スライドで見る」からプレゼン投影モードへ切り替えられます。";
    navCard.appendChild(hintItem);
    refsCol.appendChild(navCard);
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
  function withDefaults(target, defaults) {
    return Object.assign({}, defaults, (target && typeof target === "object") ? target : {});
  }
  function eachObj(value, fn) {
    (Array.isArray(value) ? value : [value]).forEach(function (v) {
      if (v && typeof v === "object") { fn(v); }
    });
  }
  function readableDefaults(opt, el) {
    var portal = root.classList.contains("pd-portal");
    var baseFs = portal ? 13 : 22;
    var axisFs = portal ? 12 : 20;
    var titleFs = portal ? 16 : 28;
    opt.textStyle = withDefaults(opt.textStyle, { fontSize: baseFs, fontFamily: window.getComputedStyle(el).fontFamily });
    ["xAxis", "yAxis", "radiusAxis", "angleAxis", "singleAxis", "parallelAxis"].forEach(function (k) {
      if (!opt[k]) { return; }
      eachObj(opt[k], function (ax) {
        ax.axisLabel = withDefaults(ax.axisLabel, { fontSize: axisFs });
        ax.nameTextStyle = withDefaults(ax.nameTextStyle, { fontSize: axisFs });
      });
    });
    if (opt.legend) { eachObj(opt.legend, function (lg) { lg.textStyle = withDefaults(lg.textStyle, { fontSize: axisFs }); }); }
    if (opt.title) {
      eachObj(opt.title, function (t) {
        t.textStyle = withDefaults(t.textStyle, { fontSize: titleFs });
        t.subtextStyle = withDefaults(t.subtextStyle, { fontSize: axisFs });
      });
    }
    if (opt.radar) {
      eachObj(opt.radar, function (r) { r.axisName = withDefaults(r.axisName, { fontSize: axisFs }); });
    }
    if (Array.isArray(opt.series)) {
      opt.series.forEach(function (s) {
        if (s && typeof s === "object" && s.label && typeof s.label === "object") {
          s.label = withDefaults(s.label, { fontSize: axisFs });
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
        if (el.clientHeight < 40) { el.style.height = root.classList.contains("pd-portal") ? "320px" : "480px"; }
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
    var cur = api.current;
    var activeSlide = slides[cur];
    counter.textContent = (cur + 1) + " / " + slides.length;
    progressBar.style.width = (slides.length ? ((cur + 1) / slides.length) * 100 : 0) + "%";
    prevBtn.disabled = cur <= 0;
    nextBtn.disabled = cur >= slides.length - 1;

    railBtns.forEach(function (b, k) {
      b.classList.toggle("is-active", k === cur);
      b.setAttribute("aria-current", k === cur ? "page" : "false");
    });
    chapterBtns.forEach(function (b, k) {
      b.classList.toggle("is-active", k === cur);
      b.setAttribute("aria-current", k === cur ? "page" : "false");
    });

    if (activeSlide) {
      var code = slideCode(activeSlide, cur);
      var chap = slideChapter(activeSlide, cur);
      var title = slideTitle(activeSlide, cur);
      breadcrumb.textContent = "";
      var bcPrefix = doc.createElement("span");
      bcPrefix.textContent = "Chapter " + code + "  /  ";
      var bcStrong = doc.createElement("strong");
      bcStrong.textContent = chap !== title ? (chap + " — " + title) : title;
      breadcrumb.appendChild(bcPrefix);
      breadcrumb.appendChild(bcStrong);

      slideBarTitle.textContent = "プレゼン表示モード  ·  " + code + " " + title + " (" + (cur + 1) + " / " + slides.length + ")";
      var rt = activeSlide.getAttribute("data-pd-readtime") || (Math.max(1, Math.round(((activeSlide.textContent || "").length) / 500)) + " min read");
      confRight.textContent = "読了目安: " + rt;
    }

    pagerPrev.disabled = cur <= 0;
    pagerPrev.textContent = "";
    var pDir = doc.createElement("span");
    pDir.className = "pd-pager-dir";
    pDir.textContent = "← PREVIOUS CHAPTER";
    var pTitle = doc.createElement("span");
    pTitle.className = "pd-pager-title";
    pTitle.textContent = cur > 0 ? (slideCode(slides[cur - 1], cur - 1) + " " + slideChapter(slides[cur - 1], cur - 1)) : "先頭の章です";
    pagerPrev.appendChild(pDir);
    pagerPrev.appendChild(pTitle);

    pagerNext.disabled = cur >= slides.length - 1;
    pagerNext.textContent = "";
    var nDir = doc.createElement("span");
    nDir.className = "pd-pager-dir";
    nDir.textContent = "NEXT CHAPTER →";
    var nTitle = doc.createElement("span");
    nTitle.className = "pd-pager-title";
    nTitle.textContent = cur < slides.length - 1 ? (slideCode(slides[cur + 1], cur + 1) + " " + slideChapter(slides[cur + 1], cur + 1)) : "最後の章です";
    pagerNext.appendChild(nDir);
    pagerNext.appendChild(nTitle);

    updatePortalToc(activeSlide);
    updatePortalRefs(activeSlide, cur);
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
    if (root.classList.contains("pd-portal") && prev !== next && !captureMode) {
      try { window.scrollTo({ top: 0, behavior: "instant" }); } catch (e) {
        try { window.scrollTo(0, 0); } catch (e2) { /* ignore */ }
      }
    }
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
  pagerPrev.addEventListener("click", function () { api.prev(); });
  pagerNext.addEventListener("click", function () { api.next(); });

  function isInteractive(el) {
    return !!(el && el.closest && el.closest("button, a, input, textarea, select, [data-pd-tab], [contenteditable='true']"));
  }
  doc.addEventListener("keydown", function (ev) {
    if (ev.defaultPrevented || ev.altKey || ev.ctrlKey || ev.metaKey) { return; }
    var k = ev.key;
    var portalReading = root.classList.contains("pd-portal") && !root.classList.contains("pd-portal-slides");
    if (k === "ArrowRight" || (!portalReading && (k === "PageDown" || (k === " " && !isInteractive(ev.target))))) {
      ev.preventDefault();
      api.next();
    } else if (k === "ArrowLeft" || (!portalReading && k === "PageUp")) {
      ev.preventDefault();
      api.prev();
    } else if (!portalReading && k === "Home") {
      ev.preventDefault();
      go(0);
    } else if (!portalReading && k === "End") {
      ev.preventDefault();
      go(slides.length - 1);
    } else if (k === "Escape" && root.classList.contains("pd-portal-slides")) {
      ev.preventDefault();
      setSlideView(false);
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
      if (cur === doc.body) { break; }
    }
    var bodyBg = parseColor(window.getComputedStyle(doc.body).backgroundColor);
    if (bodyBg && bodyBg.a >= 0.85) { return bodyBg; }
    return root.classList.contains("pd-portal") ? { r: 250, g: 250, b: 249, a: 1 } : { r: 255, g: 255, b: 255, a: 1 };
  }
  function excluded(el) {
    return !!el.closest("[data-pd-bleed], [aria-hidden='true'], .pd-chart-badge, .pd-ai-badge");
  }
  function audit(i) {
    var idx = clamp(i === undefined ? api.current : i);
    var slide = slides[idx];
    var result = { index: idx, title: slideTitle(slide, idx), issues: [], stats: {} };
    if (!slide) { return result; }
    var portal = root.classList.contains("pd-portal");
    var sr = slide.getBoundingClientRect();
    var k = portal ? 1 : ((sr.width / W) || 1);
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
        var oy = portal ? 0 : (Math.max(0, sr.top - r.top, r.bottom - sr.bottom) / k);
        var tol = portal ? 12 : 4;
        if (ox > tol || oy > tol) {
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
        var minFs = portal ? 10 : 18;
        if (fs && fs < minFs && text.length >= 4) { smallText.push(describe(el) + " " + Math.round(fs) + "px"); }
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
      push("small_text", "warning", null, (portal ? "10px" : "18px") + " 未満の文字が " + smallText.length + " 箇所: " + smallText.slice(0, 4).join(" / "));
    }
    if (!textChars && !media) {
      push("empty_slide", "error", slide, "表示される内容がありません");
    }
    result.stats = { text_chars: textChars, media: media, charts: slide.querySelectorAll(".pd-chart").length };
    return result;
  }
  api.audit = audit;
  api.listSlides = function () {
    return slides.map(function (s, i) { return { index: i, title: slideTitle(s, i), chapter: slideChapter(s, i) }; });
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
