/* The Sindarin map: the whole system as one 3D graph. People ask, agents draft, named groups
   approve in named rooms, two deterministic executors move the books, and everything hangs off
   Switch. Rendered with 3d-force-graph (WebGL); labels with three-spritetext.
   window.renderSindarinMap(container, { autoRotate, height, exposeAs }) → { setAngle, stop } */
(function () {
  "use strict";

  var C = {
    person: "#fbbf24", // people and their groups
    agent: "#7dd3fc", // LLM agents: draft only
    exec: "#ffffff", // the two executors: the only processes that act
    gate: "#f87171", // the seven artifacts a person approves
    room: "#a78bfa", // Switch rooms bridged to Mattermost
    data: "#86efac", // the record
    read: "#c4b5fd", // what anyone can read
    hub: "#e879f9", // Switch itself
  };
  var LINK = { plan: "#38bdf8", close: "#fbbf24", sales: "#34d399", data: "#9ca3af", switch: "#c026d3", approve: "#f87171", read: "#8b5cf6" };

  function N(id, label, type, size, note) {
    return { id: id, label: label, type: type, size: size, note: note || "" };
  }
  var nodes = [
    N("switch", "Switch", "hub", 16, "identity · rooms · documents · groups · budgets · attachments · agent protocol"),
    N("ask", "a person asks", "person", 6, "plan November · close October · deal Contoso Ltd on Team at 10,200.00"),
    N("cfo", "cfo", "person", 5, "approver group: a list of Switch participant ids"),
    N("controllers", "controllers", "person", 5, "approver group: a list of Switch participant ids"),
    N("board", "board", "person", 5, "approver group: budgets above the limit"),
    N("analyst", "analyst", "agent", 6, "reports the month from the ledger · reads any linked file"),
    N("planner", "planner", "agent", 6, "GOALS-n from the basis month · BUDGET-n under the cap"),
    N("bookkeeper", "bookkeeper", "agent", 6, "ENTRIES-n · CLOSE-n · LOCK-n"),
    N("treasury", "treasury", "agent", 6, "PAY-n from the open payables"),
    N("sales", "sales", "agent", 6, "DEAL-n priced from the price book"),
    N("sw", "sindarin-switch", "exec", 11, "verifies by participant id · opens rooms · reports, charts, exports, audit"),
    N("lg", "sindarin-ledger", "exec", 11, "verifies by participant id · the only writer of the books"),
    N("GOALS", "GOALS-n", "gate", 7, "cfo · in #acme-finance · opens the plan room"),
    N("BUDGET", "BUDGET-n", "gate", 7, "board · cfo alone up to 10,000.00 USD · in #planning"),
    N("ENTRIES", "ENTRIES-n", "gate", 7, "controllers · in the plan room · period planned"),
    N("CLOSE", "CLOSE-n", "gate", 7, "controllers · in #acme-finance · opens the period room"),
    N("PAY", "PAY-n", "gate", 7, "cfo · controllers alone up to 10,000.00 USD · in #payments"),
    N("LOCK", "LOCK-n", "gate", 7, "controllers · in the period room · period locked"),
    N("DEAL", "DEAL-n", "gate", 7, "cfo · controllers alone up to a 1,000.00 USD discount · in #sales"),
    N("r_fin", "#acme-finance", "room", 3.5, "the month is asked for here"),
    N("r_plan", "#planning", "room", 3.5, "budgets"),
    N("r_pay", "#payments", "room", 3.5, "payment runs"),
    N("r_sales", "#sales", "room", 3.5, "deals"),
    N("r_period", "#plan-<period>", "room", 3.5, "opened by sindarin-switch when the goals pass"),
    N("r_close", "#close-<period>", "room", 3.5, "opened by sindarin-switch when the plan passes"),
    N("ledger", "ledger", "data", 9, "SQLite · one writer · read-only for everyone else"),
    N("sheet", "Google Sheet · Excel", "data", 7, "read · load · write-back"),
    N("audit", "audit trail", "data", 5, "every event with the participant id behind it"),
    N("reports", "report · chart · export", "read", 4, "from the ledger, as text and PNG and CSV"),
    N("checks", "A2 · A4 · A5 · A6", "gate", 4, "group · single use · room · ledger re-read, on every approve"),
  ];

  function L(s, t, chain, flow, dash) {
    return { source: s, target: t, chain: chain, flow: !!flow, dash: !!dash };
  }
  var links = [
    // the plan chain
    L("ask", "analyst", "plan", true), L("analyst", "planner", "plan", true), L("planner", "GOALS", "plan", true),
    L("GOALS", "sw", "plan", true), L("sw", "r_period", "plan", true), L("planner", "BUDGET", "plan", true),
    L("BUDGET", "lg", "plan", true), L("bookkeeper", "ENTRIES", "plan", true), L("ENTRIES", "lg", "plan", true),
    // the close chain
    L("analyst", "bookkeeper", "close", true), L("bookkeeper", "CLOSE", "close", true), L("CLOSE", "sw", "close", true),
    L("sw", "r_close", "close", true), L("treasury", "PAY", "close", true), L("PAY", "lg", "close", true),
    L("bookkeeper", "LOCK", "close", true), L("LOCK", "lg", "close", true),
    // the sales chain
    L("ask", "sales", "sales", true), L("sales", "DEAL", "sales", true), L("DEAL", "lg", "sales", true),
    // who approves what
    L("GOALS", "cfo", "approve", false, true), L("BUDGET", "board", "approve", false, true), L("BUDGET", "cfo", "approve", false, true),
    L("ENTRIES", "controllers", "approve", false, true), L("CLOSE", "controllers", "approve", false, true),
    L("PAY", "cfo", "approve", false, true), L("PAY", "controllers", "approve", false, true),
    L("LOCK", "controllers", "approve", false, true), L("DEAL", "cfo", "approve", false, true), L("DEAL", "controllers", "approve", false, true),
    // where
    L("GOALS", "r_fin", "switch"), L("CLOSE", "r_fin", "switch"), L("BUDGET", "r_plan", "switch"), L("PAY", "r_pay", "switch"),
    L("DEAL", "r_sales", "switch"), L("ENTRIES", "r_period", "switch"), L("LOCK", "r_close", "switch"),
    // the record
    L("lg", "ledger", "data", true), L("ledger", "sheet", "data", true), L("sheet", "ledger", "data", true),
    L("lg", "audit", "data"), L("sw", "audit", "data"), L("ledger", "reports", "read", true), L("sw", "reports", "read"),
    // the checks sit under every gate
    L("checks", "GOALS", "approve", false, true), L("checks", "BUDGET", "approve", false, true), L("checks", "PAY", "approve", false, true),
    L("checks", "DEAL", "approve", false, true), L("checks", "ENTRIES", "approve", false, true), L("checks", "LOCK", "approve", false, true),
    L("checks", "CLOSE", "approve", false, true),
    // Switch is under all of it
    L("switch", "r_fin", "switch"), L("switch", "r_plan", "switch"), L("switch", "r_pay", "switch"), L("switch", "r_sales", "switch"),
    L("switch", "cfo", "switch"), L("switch", "controllers", "switch"), L("switch", "board", "switch"),
    L("switch", "sw", "switch"), L("switch", "lg", "switch"),
    L("switch", "analyst", "switch"), L("switch", "planner", "switch"), L("switch", "bookkeeper", "switch"), L("switch", "treasury", "switch"), L("switch", "sales", "switch"),
  ];

  function renderSindarinMap(el, opts) {
    opts = opts || {};
    if (!window.ForceGraph3D) {
      el.textContent = "The 3D map needs WebGL and the graph library; neither loaded here.";
      return null;
    }
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var autoRotate = opts.autoRotate !== false && !reduce;
    var height = opts.height || el.clientHeight || 560;
    var width = el.clientWidth || 960;
    var neighbors = {};
    links.forEach(function (l) {
      (neighbors[l.source] = neighbors[l.source] || {})[l.target] = 1;
      (neighbors[l.target] = neighbors[l.target] || {})[l.source] = 1;
    });
    var hovered = null;

    var Graph = ForceGraph3D({ controlType: "orbit" })(el)
      .width(width)
      .height(height)
      .backgroundColor("rgba(0,0,0,0)")
      .showNavInfo(false)
      .graphData({ nodes: nodes.map(function (n) { return Object.assign({}, n); }), links: links.map(function (l) { return Object.assign({}, l); }) })
      .nodeRelSize(4.2)
      .nodeVal(function (n) { return n.size; })
      .nodeColor(function (n) {
        if (!hovered) return C[n.type];
        return n.id === hovered.id || (neighbors[hovered.id] || {})[n.id] ? C[n.type] : "rgba(120,120,130,0.35)";
      })
      .nodeOpacity(0.95)
      .nodeLabel(function (n) {
        return '<div style="font:12px Inter,system-ui,sans-serif;color:#e5e7eb;background:rgba(0,0,0,.85);padding:6px 9px;border:1px solid rgba(255,255,255,.15);border-radius:8px;max-width:260px"><b>' + n.label + "</b>" + (n.note ? "<br/>" + n.note : "") + "</div>";
      })
      .nodeThreeObjectExtend(true)
      .nodeThreeObject(function (n) {
        if (!window.SpriteText) return undefined;
        var s = new SpriteText(n.label);
        s.color = n.type === "exec" || n.type === "hub" ? "#ffffff" : "#d1d5db";
        s.textHeight = n.type === "hub" ? 9 : n.type === "exec" ? 6.5 : 4.6;
        s.fontFace = "Inter, system-ui, sans-serif";
        s.fontWeight = n.type === "exec" || n.type === "hub" ? "600" : "400";
        s.backgroundColor = "rgba(0,0,0,0.45)";
        s.padding = 0.8;
        s.borderRadius = 1.2;
        s.position.y = -(Math.cbrt(n.size) * 4.2 + 4);
        return s;
      })
      .linkColor(function (l) {
        if (hovered) {
          var s = typeof l.source === "object" ? l.source.id : l.source;
          var t = typeof l.target === "object" ? l.target.id : l.target;
          if (s !== hovered.id && t !== hovered.id) return "rgba(120,120,130,0.12)";
        }
        return LINK[l.chain] || "#9ca3af";
      })
      .linkOpacity(0.75)
      .linkWidth(function (l) { return l.flow ? 1.6 : l.dash ? 0.5 : 0.7; })
      .linkDirectionalArrowLength(function (l) { return l.flow ? 5 : 0; })
      .linkDirectionalArrowRelPos(1)
      .linkDirectionalArrowColor(function (l) { return LINK[l.chain] || "#9ca3af"; })
      .linkDirectionalParticles(function (l) { return l.flow && !reduce ? 2 : 0; })
      .linkDirectionalParticleWidth(2.4)
      .linkDirectionalParticleSpeed(0.0045)
      .linkDirectionalParticleColor(function (l) { return LINK[l.chain] || "#9ca3af"; })
      .onNodeHover(function (n) {
        hovered = n || null;
        el.style.cursor = n ? "pointer" : "default";
        Graph.nodeColor(Graph.nodeColor()).linkColor(Graph.linkColor());
      })
      .warmupTicks(80)
      .cooldownTicks(220);

    Graph.d3Force("charge").strength(-260);
    Graph.d3Force("link").distance(function (l) {
      return l.chain === "switch" ? 95 : l.chain === "approve" ? 55 : l.chain === "data" || l.chain === "read" ? 50 : 60;
    });

    var distance = opts.distance || 420;
    var angle = 0;
    var state = { ready: false, setAngle: setAngle, stop: stop };
    function setAngle(a) {
      angle = a;
      Graph.cameraPosition({ x: distance * Math.sin(a), y: distance * 0.18, z: distance * Math.cos(a) }, { x: 0, y: 0, z: 0 }, 0);
    }
    setAngle(0.6);
    var fitted = false;
    function fit() {
      if (fitted) return;
      fitted = true;
      // once the layout settles, frame the whole graph and orbit at that distance
      Graph.zoomToFit(0, 24);
      setTimeout(function () {
        var p = Graph.cameraPosition();
        var d = Math.sqrt(p.x * p.x + p.y * p.y + p.z * p.z);
        if (d > 50 && !opts.distance) distance = d * 0.92;
        setAngle(angle);
        state.ready = true;
      }, 60);
    }
    Graph.onEngineStop(fit);
    setTimeout(fit, 9000);

    var running = autoRotate;
    var raf = null;
    function spin() {
      if (!running) return;
      angle += 0.0022;
      setAngle(angle);
      raf = requestAnimationFrame(spin);
    }
    if (running) raf = requestAnimationFrame(spin);
    function stop() { running = false; if (raf) cancelAnimationFrame(raf); }
    el.addEventListener("pointerdown", stop, { passive: true });
    el.addEventListener("wheel", stop, { passive: true });
    if ("IntersectionObserver" in window && autoRotate) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (!running) return;
          Graph.pauseAnimation && (e.isIntersecting ? Graph.resumeAnimation() : Graph.pauseAnimation());
        });
      }, { threshold: 0.05 }).observe(el);
    }
    window.addEventListener("resize", function () {
      Graph.width(el.clientWidth || width).height(opts.height || el.clientHeight || height);
    });
    if (opts.exposeAs) window[opts.exposeAs] = state;
    return state;
  }

  window.renderSindarinMap = renderSindarinMap;
  window.SINDARIN_MAP_LEGEND = [
    ["agents, which only draft", C.agent],
    ["the two executors, which act", C.exec],
    ["the seven gates a person approves", C.gate],
    ["people and their groups", C.person],
    ["Switch rooms", C.room],
    ["the record", C.data],
    ["Switch", C.hub],
  ];
})();
