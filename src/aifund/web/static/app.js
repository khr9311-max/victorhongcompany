/* 빅터홍컴퍼니 대시보드 스크립트(외부 라이브러리 없음)
   - 선 그래프: 서버가 넣은 JSON(<script class="chart-data">)으로 SVG를 그리고, 교차선·툴팁·키보드 이동을 제공한다.
   - [data-tip] 요소 툴팁, 테마 전환(자동/밝게/어둡게), [data-confirm] 확인창, 자동 새로고침(읽는 중에는 멈춤).
   라벨은 신뢰하지 않는 데이터로 보고 textContent로만 넣는다. 스크립트가 꺼져도 표·숫자는 모두 서버 HTML에 있다. */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var HOUR = 3600e3, DAY = 24 * HOUR, KST_OFFSET = 9 * HOUR;
  var nf = new Intl.NumberFormat("ko-KR");
  var lastActive = 0;

  function svg(tag, attrs, parent) {
    var el = document.createElementNS(SVGNS, tag);
    for (var k in attrs) { if (Object.prototype.hasOwnProperty.call(attrs, k)) el.setAttribute(k, attrs[k]); }
    if (parent) parent.appendChild(el);
    return el;
  }
  function node(tag, cls, parent, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    if (parent) parent.appendChild(el);
    return el;
  }

  // ---------- 숫자·시간 표시 ----------
  function fmtValue(v, kind) {
    if (v === null || v === undefined || !isFinite(v)) return "-";
    if (kind === "pct") return (v > 0 ? "+" : "") + v.toFixed(2) + "%";
    return nf.format(Math.round(v)) + "원";
  }
  function decimals(step) { return step >= 1 ? 0 : step >= 0.1 ? 1 : 2; }
  function fmtAxis(v, kind, step) {
    if (Math.abs(v) < step * 1e-6) v = 0;  // 부동소수 오차로 생기는 "-0.0%" 방지
    if (kind === "pct") return (v > 0 ? "+" : "") + v.toFixed(decimals(step)) + "%";
    var a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(decimals(step / 1e8)) + "억";
    if (a >= 1e4) return (v / 1e4).toFixed(decimals(step / 1e4)) + "만";
    return nf.format(v);
  }
  function kstParts(t) {
    var d = new Date(t + KST_OFFSET);
    return { mo: d.getUTCMonth() + 1, day: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes() };
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function fmtTime(t) { var p = kstParts(t); return p.mo + "/" + p.day + " " + pad(p.h) + ":" + pad(p.mi); }
  function fmtTick(t, step) {
    var p = kstParts(t);
    if (step >= DAY || (p.h === 0 && p.mi === 0)) return p.mo + "/" + p.day;
    return pad(p.h) + ":" + pad(p.mi);
  }
  function niceStep(span, count) {
    var raw = span / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag;
    return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  }
  function timeTicks(t0, t1, width) {
    var steps = [HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];
    var maxTicks = Math.max(2, Math.floor(width / 80));
    var step = steps[steps.length - 1];
    for (var i = 0; i < steps.length; i++) { if ((t1 - t0) / steps[i] <= maxTicks) { step = steps[i]; break; } }
    var first = Math.ceil((t0 + KST_OFFSET) / step) * step - KST_OFFSET;  // 한국시간 경계에 맞춤
    var out = [];
    for (var t = first; t <= t1; t += step) out.push(t);
    return { ticks: out, step: step };
  }
  function nearestIndex(arr, t, key) {
    var lo = 0, hi = arr.length - 1;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (key(arr[mid]) < t) lo = mid; else hi = mid; }
    return Math.abs(key(arr[lo]) - t) <= Math.abs(key(arr[hi]) - t) ? lo : hi;
  }

  // ---------- 선 그래프 ----------
  function renderLine(fig) {
    var dataEl = fig.querySelector("script.chart-data");
    var plot = fig.querySelector(".chart-plot");
    if (!dataEl || !plot) return;
    var spec;
    try { spec = JSON.parse(dataEl.textContent); } catch (e) { return; }
    var kind = spec.format || "krw";
    var series = (spec.series || []).filter(function (s) { return s.points && s.points.length; });
    plot.textContent = "";
    var W = plot.clientWidth, H = plot.clientHeight || 220;
    if (!series.length || W < 60) {
      node("div", "chart-empty", plot, fig.getAttribute("data-empty") || "아직 기록이 없습니다.");
      return;
    }
    var xs = [], ys = [];
    series.forEach(function (s) { s.points.forEach(function (p) { xs.push(p[0]); ys.push(p[1]); }); });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    if (x1 - x0 < 60e3) { x0 -= 30 * 60e3; x1 += 30 * 60e3; }
    var ref = spec.reference && isFinite(spec.reference.value) ? spec.reference : null;
    var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (ref) { y0 = Math.min(y0, ref.value); y1 = Math.max(y1, ref.value); }
    if (y1 - y0 < 1e-9) { var padv = Math.abs(y0) * 0.002 || 0.5; y0 -= padv; y1 += padv; }
    var step = niceStep(y1 - y0, 4);
    var lo = Math.floor(y0 / step) * step, hi = Math.ceil(y1 / step) * step;
    var ticks = [];
    for (var v = lo; v <= hi + step / 2; v += step) ticks.push(v);
    var tickLabels = ticks.map(function (t) { return fmtAxis(t, kind, step); });
    var ml = Math.max.apply(null, tickLabels.map(function (s) { return s.length; })) * 6.4 + 14;

    // 끝점 직접 라벨: 1개 계열은 값, 2~4개는 짧은 이름+값. 서로 겹치면 범례·툴팁에 맡긴다.
    var endLabels = series.length <= 4 ? series.map(function (s) {
      var last = s.points[s.points.length - 1];
      return (series.length > 1 ? (s.short || s.label) + " " : "") + fmtValue(last[1], kind);
    }) : [];
    var mr = endLabels.length ? Math.max.apply(null, endLabels.map(function (s) { return s.length; })) * 6.6 + 18 : 12;
    if (W - ml - mr < 160) { endLabels = []; mr = 12; }
    var mt = 12, mb = 24, iw = W - ml - mr, ih = H - mt - mb;
    function X(t) { return ml + (t - x0) / (x1 - x0) * iw; }
    function Y(val) { return mt + (hi - val) / (hi - lo) * ih; }

    var root = svg("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, "aria-hidden": "true" }, plot);
    ticks.forEach(function (t, i) {
      var y = Y(t);
      svg("line", { x1: ml, x2: ml + iw, y1: y, y2: y, "class": "grid-line" }, root);
      svg("text", { x: ml - 8, y: y + 4, "text-anchor": "end" }, root).textContent = tickLabels[i];
    });
    var tt = timeTicks(x0, x1, iw);
    tt.ticks.forEach(function (t) {
      svg("text", { x: X(t), y: H - 6, "text-anchor": "middle" }, root).textContent = fmtTick(t, tt.step);
    });
    if (ref) {
      var ry = Y(ref.value);
      svg("line", { x1: ml, x2: ml + iw, y1: ry, y2: ry, "class": "ref-line" }, root);
      svg("text", { x: ml + 6, y: ry - 5, "class": "ref-label" }, root).textContent = ref.label;
    }
    var colors = series.map(function (s) { return "var(--series-" + (s.slot || 1) + ")"; });
    series.forEach(function (s, i) {
      var d = s.points.map(function (p, k) { return (k ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1); }).join("");
      if (spec.area && series.length === 1 && s.points.length > 1) {
        var first = s.points[0], last = s.points[s.points.length - 1];
        var area = svg("path", { d: d + "L" + X(last[0]).toFixed(1) + "," + (mt + ih) + "L" + X(first[0]).toFixed(1) + "," + (mt + ih) + "Z",
          "fill-opacity": "0.10" }, root);
        area.style.fill = colors[i];
      }
      var line = svg("path", { d: d, fill: "none", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);
      line.style.stroke = colors[i];
      var lp = s.points[s.points.length - 1];
      var dot = svg("circle", { cx: X(lp[0]), cy: Y(lp[1]), r: 4, "stroke-width": 2 }, root);
      dot.style.fill = colors[i];
      dot.style.stroke = "var(--surface)";
    });
    if (endLabels.length) {
      var ends = series.map(function (s, i) { return { y: Y(s.points[s.points.length - 1][1]), i: i }; })
        .sort(function (a, b) { return a.y - b.y; });
      var clash = ends.some(function (e, k) { return k > 0 && e.y - ends[k - 1].y < 14; });
      if (!clash) {
        ends.forEach(function (e) {
          svg("text", { x: ml + iw + 9, y: e.y + 4, "class": "end-label" }, root).textContent = endLabels[e.i];
        });
      }
    }

    // 교차선 + 툴팁(모든 계열 값을 한 번에). 키보드 좌우 화살표로도 이동.
    var cross = svg("line", { y1: mt, y2: mt + ih, "class": "crosshair", visibility: "hidden" }, root);
    var marks = series.map(function (s, i) {
      var c = svg("circle", { r: 4, "stroke-width": 2, visibility: "hidden" }, root);
      c.style.fill = colors[i];
      c.style.stroke = "var(--surface)";
      return c;
    });
    var stamps = xs.slice().sort(function (a, b) { return a - b; }).filter(function (t, k, a) { return !k || t !== a[k - 1]; });
    var tol = Math.max(5 * 60e3, (x1 - x0) / 60);
    var tip = node("div", "tip", plot);
    tip.hidden = true;
    var current = -1;
    function show(idx) {
      current = Math.max(0, Math.min(stamps.length - 1, idx));
      var t = stamps[current], x = X(t);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      tip.textContent = "";
      node("div", "t", tip, fmtTime(t));
      series.forEach(function (s, i) {
        var p = s.points[nearestIndex(s.points, t, function (q) { return q[0]; })];
        if (!p || Math.abs(p[0] - t) > tol) { marks[i].setAttribute("visibility", "hidden"); return; }
        marks[i].setAttribute("cx", X(p[0])); marks[i].setAttribute("cy", Y(p[1])); marks[i].setAttribute("visibility", "visible");
        var row = node("div", "r", tip);
        node("span", "k", row).style.background = colors[i];
        node("b", "", row, fmtValue(p[1], kind));
        node("span", "", row, s.label);
      });
      if (ref) node("div", "t", tip, ref.label + " " + fmtValue(ref.value, kind));
      tip.hidden = false;
      var left = x + 14;
      if (left + tip.offsetWidth > W) left = x - tip.offsetWidth - 14;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = mt + "px";
    }
    function hide() {
      current = -1;
      tip.hidden = true;
      cross.setAttribute("visibility", "hidden");
      marks.forEach(function (m) { m.setAttribute("visibility", "hidden"); });
    }
    var overlay = svg("rect", { x: ml, y: 0, width: Math.max(0, iw), height: H, fill: "transparent" }, root);
    overlay.addEventListener("pointermove", function (e) {
      lastActive = Date.now();
      var r = root.getBoundingClientRect();
      var t = x0 + (e.clientX - r.left - ml) / iw * (x1 - x0);
      show(nearestIndex(stamps, t, function (q) { return q; }));
    });
    overlay.addEventListener("pointerleave", hide);
    fig.onkeydown = function (e) {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        show(current < 0 ? stamps.length - 1 : current + (e.key === "ArrowLeft" ? -1 : 1));
        e.preventDefault();
      } else if (e.key === "Escape") { hide(); }
    };
    fig.onblur = hide;
  }

  function setupCharts() {
    var figs = Array.prototype.slice.call(document.querySelectorAll("figure.chart[data-chart]"));
    figs.forEach(function (fig) {
      renderLine(fig);
      if (window.ResizeObserver) {
        var width = fig.clientWidth, timer = null;
        new ResizeObserver(function () {
          if (Math.abs(fig.clientWidth - width) < 2) return;
          width = fig.clientWidth;
          clearTimeout(timer);
          timer = setTimeout(function () { renderLine(fig); }, 120);
        }).observe(fig);
      }
    });
  }

  // ---------- [data-tip] 툴팁(구성 막대 등) ----------
  function setupTips() {
    var tip = null;
    function place(el) {
      if (!tip) { tip = node("div", "tip", document.body); tip.style.position = "fixed"; }
      tip.textContent = el.getAttribute("data-tip");
      tip.hidden = false;
      var r = el.getBoundingClientRect();
      var left = Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, r.left + r.width / 2 - tip.offsetWidth / 2));
      tip.style.left = left + "px";
      tip.style.top = Math.max(8, r.top - tip.offsetHeight - 8) + "px";
    }
    function off() { if (tip) tip.hidden = true; }
    document.querySelectorAll("[data-tip]").forEach(function (el) {
      el.addEventListener("pointerenter", function () { place(el); });
      el.addEventListener("pointerleave", off);
      el.addEventListener("focus", function () { place(el); });
      el.addEventListener("blur", off);
    });
  }

  // ---------- 테마 ----------
  function setupTheme() {
    var names = { auto: "테마: 자동", light: "테마: 밝게", dark: "테마: 어둡게" };
    var order = ["auto", "light", "dark"];
    var btn = document.querySelector("[data-theme-toggle]");
    if (!btn) return;
    var cur = "auto";
    try { cur = localStorage.getItem("aifund.theme") || "auto"; } catch (e) { /* 저장소 없음 */ }
    if (order.indexOf(cur) < 0) cur = "auto";
    btn.textContent = names[cur];
    btn.addEventListener("click", function () {
      cur = order[(order.indexOf(cur) + 1) % order.length];
      if (cur === "auto") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", cur);
      try { localStorage.setItem("aifund.theme", cur); } catch (e) { /* 저장소 없음 */ }
      btn.textContent = names[cur];
    });
  }

  // ---------- 위험 동작 확인 ----------
  function setupConfirm() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
      });
    });
  }

  // ---------- 자동 새로고침 ----------
  function setupRefresh() {
    var secs = parseInt(document.body.getAttribute("data-refresh") || "0", 10);
    if (!secs) return;
    var box = document.querySelector("[data-refresh-status]");
    var paused = false;
    try { paused = localStorage.getItem("aifund.refresh") === "off"; } catch (e) { /* 저장소 없음 */ }
    var label = box && box.querySelector("span"), btn = box && box.querySelector("button");
    function draw() {
      if (label) label.textContent = paused ? "자동 새로고침 꺼짐" : secs + "초마다 자동 새로고침(읽는 중에는 멈춤)";
      if (btn) btn.textContent = paused ? "켜기" : "끄기";
    }
    if (box) box.hidden = false;
    if (btn) btn.addEventListener("click", function () {
      paused = !paused;
      try { localStorage.setItem("aifund.refresh", paused ? "off" : "on"); } catch (e) { /* 저장소 없음 */ }
      draw();
    });
    draw();
    ["pointerdown", "keydown", "wheel"].forEach(function (ev) { document.addEventListener(ev, function () { lastActive = Date.now(); }, { passive: true }); });
    setInterval(function () {
      if (paused || document.visibilityState !== "visible") return;
      if (Date.now() - lastActive < 15000) return;
      var a = document.activeElement;
      if (a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return;
      if (document.querySelector("main details[open]")) return;
      window.location.reload();
    }, secs * 1000);
  }

  function init() { setupTheme(); setupCharts(); setupTips(); setupConfirm(); setupRefresh(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
