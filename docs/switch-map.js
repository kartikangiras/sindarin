/* The Switch surface Sindarin stands on, as one 3D scene. Switch at the core; every Switch
   primitive the project uses on the inner ring; what Sindarin does with each on the outer ring;
   the chat apps the collaboration bridge carries behind it. Rendered with 3d-force-graph on
   fixed positions, so it is a diagram that turns, not a layout that settles.
   window.renderSwitchMap(container, { autoRotate, height, exposeAs, distance }) → { ready, setAngle, stop } */
(function () {
  "use strict";

  var FAMILY = {
    hub: { color: "#e879f9", label: "Switch" },
    identity: { color: "#fbbf24", label: "identity" },
    rooms: { color: "#a78bfa", label: "rooms" },
    material: { color: "#86efac", label: "what a room carries" },
    control: { color: "#7dd3fc", label: "limits and routing" },
    protocol: { color: "#ffffff", label: "the protocol and the Gateway" },
    console: { color: "#f9a8d4", label: "the Console" },
    bridge: { color: "#fb923c", label: "the bridge and the chat apps" },
  };

  // inner ring: the primitives, in the order they sit around the core
  var PRIMS = [
    ["identity", "Participant identity", "identity", "every event carries a Switch participant id; the executors verify nothing else"],
    ["events", "Event stream", "protocol", "server-sent events in: the bootstrap learns the ids, the executors read every message"],
    ["protocol", "Agent protocol", "protocol", "SSE in, HTTP out: seven external agents, each with its own key"],
    ["gateway", "Gateway API", "protocol", "the bootstrap registers agents, rooms, groups, documents, links and budgets through it"],
    ["rooms", "Rooms", "rooms", "bridged to the chat; the executor opens one when a gate passes"],
    ["members", "Members", "rooms", "who is in the room is who may speak there"],
    ["instructions", "Room instructions", "rooms", "the brief every member and agent in the room reads"],
    ["documents", "Documents", "material", "attached to the room"],
    ["links", "Linked rooms", "material", "the month hangs off the finance room"],
    ["groups", "Room groups", "material", "one group for the company's finance"],
    ["attachments", "Attachments", "material", "files in the room through Switch's attachment route"],
    ["budgets", "Usage budgets", "control", "Switch's own token limits on each agent"],
    ["addressing", "Addressing policies", "control", "which agents a message can reach; layer 0 under the executors' checks"],
    ["console", "Switch Console", "console", "the desktop app that runs agents and shows the rooms"],
    ["bridge", "Collaboration bridge", "bridge", "how the chat app connects"],
  ];

  // outer ring: what Sindarin does with each primitive (parent, label, note)
  var USES = [
    ["identity", "cfo · controllers · board", "approver groups are lists of participant ids in policy.yaml"],
    ["identity", "display names ignored", "an impostor named Isaac is refused like anyone outside the group"],
    ["events", "ids learned at bootstrap", "nothing is typed by hand; the stream says who is who"],
    ["protocol", "5 LLM agents", "analyst · planner · bookkeeper · treasury · sales, drafting only"],
    ["protocol", "2 executors", "sindarin-switch · sindarin-ledger, one credential each, acting"],
    ["gateway", "one bootstrap, idempotent", "agents, rooms, people, policy and budgets from one command"],
    ["rooms", "#acme-finance · #planning · #payments · #sales", "the four standing rooms"],
    ["rooms", "#plan-<period> · #close-<period>", "opened by sindarin-switch when GOALS or CLOSE passes"],
    ["members", "approvers · the chain's agents · the ledger", "put in the room by the executor that opens it"],
    ["instructions", "the brief", "the goals or the close plan, set as the room's instructions; `brief` re-posts it"],
    ["documents", "the brief as a document", "attached to the opened room"],
    ["links", "linked from #acme-finance", "the month is one click from the room it was asked in"],
    ["groups", "\"Acme finance\" group", "every room of the company's finance in one group"],
    ["attachments", "charts as PNG", "revenue · budget · target · costs · deals, with every report"],
    ["attachments", "exports as CSV", "sales · payables · budget · journal · audit"],
    ["budgets", "200,000 output tokens a day", "per LLM agent, set by the bootstrap"],
    ["addressing", "scoped to the rooms", "widened by the executor for the agents it puts in a new room"],
    ["console", "agents · rooms · group · documents · budgets", "one view of the month"],
    ["console", "sindarin.template.yaml", "the five roles as Console-run Claude Code agents"],
    ["bridge", "bundled Mattermost, or your own server", "the demo runs on the Mattermost Switch ships; set MATTERMOST_URL and the bootstrap bridges yours"],
  ];

  var APPS = [
    ["Slack", "#E01E5A"],
    ["Microsoft Teams", "#7B83EB"],
    ["Mattermost", "#1E90FF"],
    ["Discord", "#5865F2"],
    ["Telegram", "#26A5E4"],
  ];

  var R1 = 150, R2 = 255, R3 = 335;
  var nodes = [], links = [];
  nodes.push({ id: "switch", label: "Switch", type: "hub", family: "hub", size: 15, fx: 0, fy: 0, fz: 0,
    note: "open source infrastructure for agents in team chat; Sindarin stands on all of this" });

  var byParent = {};
  USES.forEach(function (u) { (byParent[u[0]] = byParent[u[0]] || []).push(u); });

  PRIMS.forEach(function (p, i) {
    var a = (i / PRIMS.length) * Math.PI * 2;
    var y = (i % 2 ? 1 : -1) * 32;
    nodes.push({ id: p[0], label: p[1], type: "prim", family: p[2], size: 6.5, note: p[3],
      fx: R1 * Math.cos(a), fy: y, fz: R1 * Math.sin(a), angle: a });
    links.push({ source: "switch", target: p[0], family: p[2], kind: "core" });
    var uses = byParent[p[0]] || [];
    uses.forEach(function (u, j) {
      var spread = (2 * Math.PI / PRIMS.length) * 0.62;
      var ua = a + (uses.length > 1 ? (j / (uses.length - 1) - 0.5) * spread : 0);
      var uy = y + (uses.length > 1 ? (j % 2 ? 22 : -22) : -20);
      var id = "u" + nodes.length;
      nodes.push({ id: id, label: u[1], type: "use", family: p[2], size: 3.2, note: u[2],
        fx: R2 * Math.cos(ua), fy: uy, fz: R2 * Math.sin(ua) });
      links.push({ source: p[0], target: id, family: p[2], kind: "use" });
    });
  });

  // the chat apps fan out behind the bridge, one ring further
  var bridge = nodes.filter(function (n) { return n.id === "bridge"; })[0];
  APPS.forEach(function (app, j) {
    var spread = 1.15;
    var aa = bridge.angle + (j / (APPS.length - 1) - 0.5) * spread;
    var id = "app" + j;
    nodes.push({ id: id, label: app[0], type: "app", family: "bridge", size: 4.2, brand: app[1],
      note: "connected through the Gateway whenever the team moves; the workflow does not change",
      fx: R3 * Math.cos(aa), fy: 96 + (j % 2 ? 14 : -14), fz: R3 * Math.sin(aa) });
    links.push({ source: "bridge", target: id, family: "bridge", kind: "app" });
  });

  function glowTexture() {
    var c = document.createElement("canvas");
    c.width = c.height = 128;
    var g = c.getContext("2d");
    var grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    grad.addColorStop(0, "rgba(255,255,255,0.9)");
    grad.addColorStop(0.35, "rgba(255,255,255,0.35)");
    grad.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = grad;
    g.fillRect(0, 0, 128, 128);
    var t = new THREE.CanvasTexture(c);
    t.needsUpdate = true;
    return t;
  }

  function renderSwitchMap(el, opts) {
    opts = opts || {};
    if (!window.ForceGraph3D || !window.THREE) {
      el.textContent = "The 3D scene needs WebGL and the graph library; neither loaded here.";
      return null;
    }
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var autoRotate = opts.autoRotate !== false && !reduce;
    var height = opts.height || el.clientHeight || 560;
    var width = el.clientWidth || 960;
    var glow = glowTexture();
    var hubHalo = null;
    var hovered = null;
    var neighbors = {};
    links.forEach(function (l) {
      (neighbors[l.source] = neighbors[l.source] || {})[l.target] = 1;
      (neighbors[l.target] = neighbors[l.target] || {})[l.source] = 1;
    });
    function colorOf(n) { return n.brand || FAMILY[n.family].color; }
    function dim(n) { return hovered && !(n.id === hovered.id || (neighbors[hovered.id] || {})[n.id]); }

    var Graph = ForceGraph3D({ controlType: "orbit" })(el)
      .width(width)
      .height(height)
      .backgroundColor("rgba(0,0,0,0)")
      .showNavInfo(false)
      .graphData({ nodes: nodes.map(function (n) { return Object.assign({}, n); }), links: links.map(function (l) { return Object.assign({}, l); }) })
      .nodeLabel(function (n) {
        return '<div style="font:12px Inter,system-ui,sans-serif;color:#e5e7eb;background:rgba(0,0,0,.85);padding:6px 9px;border:1px solid rgba(255,255,255,.15);border-radius:8px;max-width:280px"><b>' + n.label + "</b>" + (n.note ? "<br/>" + n.note : "") + "</div>";
      })
      .nodeThreeObjectExtend(false)
      .nodeThreeObject(function (n) {
        var group = new THREE.Group();
        var color = new THREE.Color(colorOf(n));
        var r = n.type === "hub" ? 11 : n.type === "prim" ? 4.6 : n.type === "app" ? 3.6 : 2.6;
        var sphere = new THREE.Mesh(
          new THREE.SphereGeometry(r, 28, 28),
          new THREE.MeshPhongMaterial({ color: color, emissive: color, emissiveIntensity: n.type === "hub" ? 0.55 : 0.35, shininess: 60 })
        );
        group.add(sphere);
        var halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, color: color, transparent: true, opacity: n.type === "hub" ? 0.9 : 0.55, blending: THREE.AdditiveBlending, depthWrite: false }));
        var hs = r * (n.type === "hub" ? 6.5 : 4.2);
        halo.scale.set(hs, hs, 1);
        group.add(halo);
        if (n.type === "hub") hubHalo = halo;
        if (window.SpriteText) {
          var s = new SpriteText(n.label);
          s.color = n.type === "use" ? "#d1d5db" : "#ffffff";
          s.textHeight = n.type === "hub" ? 12 : n.type === "prim" ? 6.6 : n.type === "app" ? 6 : 5.6;
          s.fontFace = "Inter, system-ui, sans-serif";
          s.fontWeight = n.type === "use" ? "400" : "600";
          s.backgroundColor = "rgba(0,0,0,0.55)";
          s.padding = 1;
          s.borderRadius = 1.4;
          s.position.y = -(r + (n.type === "hub" ? 9 : 5));
          group.add(s);
        }
        group.userData.sphere = sphere;
        group.userData.halo = halo;
        return group;
      })
      .linkColor(function (l) {
        var s = typeof l.source === "object" ? l.source : null;
        var t = typeof l.target === "object" ? l.target : null;
        if (hovered && s && t && s.id !== hovered.id && t.id !== hovered.id) return "rgba(120,120,130,0.10)";
        return FAMILY[l.family].color;
      })
      .linkOpacity(0.55)
      .linkWidth(function (l) { return l.kind === "core" ? 1.4 : l.kind === "app" ? 0.9 : 0.6; })
      .linkCurvature(0.14)
      .linkDirectionalParticles(function (l) { return reduce ? 0 : l.kind === "core" ? 3 : 1; })
      .linkDirectionalParticleWidth(function (l) { return l.kind === "core" ? 2.8 : 1.8; })
      .linkDirectionalParticleSpeed(function (l) { return l.kind === "core" ? 0.006 : 0.0045; })
      .linkDirectionalParticleColor(function (l) { return FAMILY[l.family].color; })
      .onNodeHover(function (n) {
        hovered = n || null;
        el.style.cursor = n ? "pointer" : "default";
        Graph.linkColor(Graph.linkColor());
        Graph.graphData().nodes.forEach(function (m) {
          var o = m.__threeObj;
          if (!o || !o.userData.sphere) return;
          var faded = dim(m);
          o.userData.sphere.material.opacity = faded ? 0.25 : 1;
          o.userData.sphere.material.transparent = true;
          o.userData.halo.material.opacity = faded ? 0.08 : (m.type === "hub" ? 0.9 : 0.55);
          o.children.forEach(function (ch) { if (ch.isSprite && ch !== o.userData.halo) ch.material.opacity = faded ? 0.25 : 1; });
        });
      })
      .warmupTicks(5)
      .cooldownTicks(0);

    // the orbit rings, so the layers read as layers
    var scene = Graph.scene();
    [[R1, "#e879f9", 0.16], [R2, "#ffffff", 0.07], [R3, "#fb923c", 0.08]].forEach(function (ring) {
      var m = new THREE.Mesh(
        new THREE.RingGeometry(ring[0] - 0.35, ring[0] + 0.35, 160),
        new THREE.MeshBasicMaterial({ color: ring[1], transparent: true, opacity: ring[2], side: THREE.DoubleSide, depthWrite: false })
      );
      m.rotation.x = -Math.PI / 2;
      m.position.y = ring[0] === R3 ? 96 : 0;
      scene.add(m);
    });
    // a faint floor of radial spokes under the primitives
    PRIMS.forEach(function (p, i) {
      var a = (i / PRIMS.length) * Math.PI * 2;
      var geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, -1, 0), new THREE.Vector3(R2 * Math.cos(a), -1, R2 * Math.sin(a))]);
      scene.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: "#ffffff", transparent: true, opacity: 0.045 })));
    });

    var distance = opts.distance || 640;
    var cam = Graph.camera();
    cam.fov = 40;
    cam.updateProjectionMatrix();
    var angle = 0.6;
    var state = { ready: false, setAngle: setAngle, stop: stop };
    function setAngle(a) {
      angle = a;
      var y = distance * (0.56 + 0.03 * Math.sin(a * 2));
      Graph.cameraPosition({ x: distance * Math.sin(a), y: y, z: distance * Math.cos(a) }, { x: 0, y: -42, z: 0 }, 0);
      if (hubHalo) {
        var s = 11 * 6.5 * (1 + 0.08 * Math.sin(a * 4));
        hubHalo.scale.set(s, s, 1);
      }
    }
    setAngle(angle);
    setTimeout(function () { setAngle(angle); state.ready = true; }, 900);

    var running = autoRotate;
    var raf = null;
    function spin() {
      if (!running) return;
      setAngle(angle + 0.0024);
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

  window.renderSwitchMap = renderSwitchMap;
  window.SWITCH_MAP_LEGEND = ["identity", "rooms", "material", "control", "protocol", "console", "bridge"].map(function (k) {
    return [FAMILY[k].label, FAMILY[k].color];
  });
})();
