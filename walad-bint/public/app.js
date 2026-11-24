/* ولد بنت حيوان جماد — client.
 * The server is the single source of truth. This file only renders the state it
 * pushes and sends user intents back. Inputs are never re-created while you type.
 */
(() => {
  "use strict";

  const app = document.getElementById("app");
  const connBanner = document.getElementById("conn");
  const LETTERS = "أبتثجحخدذرزسشصضطظعغفقكلمنهوي".split("");
  const CATS = [["👦", "ولد"], ["👧", "بنت"], ["🐫", "حيوان"], ["🪑", "جماد"], ["🍽️", "طعام"], ["🗺️", "بلاد"]];
  const PHASE_LABEL = {
    lobby: "غرفة الانتظار", letter: "اختيار الحرف", countdown: "استعد", playing: "جارية الآن",
    collecting: "جمع الإجابات", judging: "التحكيم", review: "مراجعة الإجابات", final: "النهاية",
  };

  // ------------------------------------------------------------ state
  let ws = null;
  let S = null;                 // latest server state
  let hello = null;             // message used to (re)join
  let joined = false;
  let reconnectDelay = 500;
  let reconnectTimer = null;
  let deadline = null;          // performance.now() when the current timer ends
  let builtKey = null;
  let chat = [];
  let unread = 0;
  let chatOpen = false;
  let rtt = null;
  let reviewTab = 0;
  let draft = {};
  let draftTimer = null;
  let autoSubmitted = false;

  const tokenKey = (code) => "wbhj:tok:" + code;
  const savedName = () => localStorage.getItem("wbhj:name") || "";

  // ------------------------------------------------------------ dom helper
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else if (k === "html") el.innerHTML = v; // only used with static strings
        else if (k in el && k !== "list") el[k] = v;
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  function toast(text, err) {
    const t = h("div", { class: "toast" + (err ? " err" : "") }, text);
    document.getElementById("toasts").append(t);
    setTimeout(() => t.remove(), 3500);
  }

  const me = () => S && S.players.find((p) => p.id === S.me);
  const isHost = () => S && S.hostId === S.me;
  const pname = (id) => (S && S.players.find((p) => p.id === id) || {}).name || "لاعب";

  // ------------------------------------------------------------ connection
  function connect(msg) {
    hello = msg;
    clearTimeout(reconnectTimer);
    if (ws) { ws.onclose = null; ws.close(); }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => ws.send(JSON.stringify(hello));
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      ws = null;
      if (!joined) { if (!S) showHome(); return; }
      connBanner.classList.remove("hidden");
      reconnectTimer = setTimeout(() => connect(hello), reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 5000);
    };
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(msg)); return true; }
    toast("غير متصل — جارٍ إعادة الاتصال…", true);
    return false;
  }

  function leaveRoom(text) {
    joined = false;
    S = null;
    builtKey = null;
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    connBanner.classList.add("hidden");
    history.replaceState(null, "", "/");
    showHome();
    if (text) toast(text, true);
  }

  function onMessage(m) {
    switch (m.t) {
      case "welcome":
        joined = true;
        reconnectDelay = 500;
        connBanner.classList.add("hidden");
        localStorage.setItem(tokenKey(m.code), m.token);
        hello = { t: "join", code: m.code, token: m.token, name: savedName() };
        if (location.pathname !== "/room/" + m.code) history.replaceState(null, "", "/room/" + m.code);
        chat = m.chat || [];
        builtKey = null;
        break;
      case "state": {
        const prev = S;
        S = m.s;
        deadline = S.remainingMs == null ? null : performance.now() + S.remainingMs;
        if (!prev || prev.round !== S.round || prev.phase !== S.phase) onPhaseChange(prev);
        render();
        break;
      }
      case "chat":
        chat.push(m.m);
        if (chat.length > 100) chat.shift();
        appendChat(m.m);
        break;
      case "error":
        toast(m.msg, true);
        break;
      case "kicked":
        if (S) localStorage.removeItem(tokenKey(S.code));
        leaveRoom("تم طردك من الغرفة");
        break;
      case "fatal":
        if (hello && hello.code && /طرد/.test(m.msg)) localStorage.removeItem(tokenKey(hello.code));
        leaveRoom(m.msg);
        break;
      case "pong":
        rtt = Math.round(performance.now() - m.c);
        const r = document.getElementById("rtt");
        if (r) r.textContent = rtt + "ms";
        break;
    }
  }

  function onPhaseChange(prev) {
    if (S.phase === "playing" && (!prev || prev.phase !== "playing")) {
      draft = Object.assign({}, S.myAnswers || {});
      autoSubmitted = false;
    }
    if (S.phase === "review" && (!prev || prev.phase !== "review")) reviewTab = 0;
  }

  setInterval(() => { if (joined) send({ t: "ping", c: performance.now() }); }, 5000);

  // ------------------------------------------------------------ home
  function showHome(prefillCode) {
    S = null; builtKey = null;
    app.replaceChildren();
    const nameIn = h("input", { class: "input", placeholder: "مثال: سعود", maxLength: 20, value: savedName(), autocomplete: "nickname" });
    const codeIn = h("input", { class: "input code-input", placeholder: "رمز الغرفة", maxLength: 5, value: prefillCode || "", autocomplete: "off" });
    const getName = () => {
      const n = nameIn.value.trim();
      if (!n) { toast("اكتب اسمك أولًا", true); nameIn.focus(); return null; }
      localStorage.setItem("wbhj:name", n);
      return n;
    };
    const create = h("button", { class: "btn primary block", onclick: () => {
      const n = getName(); if (!n) return;
      create.disabled = true; connect({ t: "create", name: n });
      setTimeout(() => (create.disabled = false), 4000);
    } }, "إنشاء غرفة جديدة");
    const join = h("button", { class: "btn block", onclick: () => {
      const n = getName(); if (!n) return;
      const code = codeIn.value.trim().toUpperCase();
      if (code.length !== 5) { toast("رمز الغرفة من 5 خانات", true); codeIn.focus(); return; }
      join.disabled = true;
      connect({ t: "join", code, name: n, token: localStorage.getItem(tokenKey(code)) });
      setTimeout(() => (join.disabled = false), 4000);
    } }, "انضمام");
    codeIn.addEventListener("keydown", (e) => { if (e.key === "Enter") join.click(); });
    nameIn.addEventListener("keydown", (e) => { if (e.key === "Enter") (prefillCode ? join : create).click(); });

    app.append(h("div", { class: "home" },
      h("div", { class: "logo" }, "🎲"),
      h("h1", { class: "title" }, "ولد بنت حيوان جماد"),
      h("div", { class: "subtitle" }, "لعبة الحروف العربية مع أصدقائك — لحظيًا"),
      h("div", { class: "chips" }, CATS.map(([e, l]) => h("span", { class: "chip" }, e + " " + l))),
      h("div", { class: "card stack" },
        h("label", { class: "stack", style: "gap:6px" }, h("b", null, "اسمك"), nameIn),
        prefillCode ? null : create,
        prefillCode ? null : h("div", { class: "divider" }, "أو انضم لغرفة"),
        codeIn, join),
      h("p", { class: "center muted small" }, "٦ تصنيفات • اختيار الحروف بالدور • تحكيم بالذكاء الاصطناعي • اعتراضات بالتصويت • دردشة")
    ));
    (prefillCode && nameIn.value ? join : nameIn).focus();
  }

  // ------------------------------------------------------------ room shell
  let els = {};

  function buildShell() {
    app.replaceChildren();
    els.phase = h("span", { class: "badge" });
    els.round = h("span", { class: "badge gold" });
    els.main = h("div", { class: "stack" });
    els.players = h("ul", { class: "players" });
    els.playersTitle = h("b");
    els.chatList = h("div", { class: "chat-list" });
    const chatIn = h("input", { class: "input", placeholder: "اكتب رسالة…", maxLength: 300, autocomplete: "off", enterKeyHint: "send" });
    const chatForm = h("form", { class: "chat-form", onsubmit: (e) => {
      e.preventDefault();
      const text = chatIn.value.trim();
      if (text && send({ t: "chat", text })) chatIn.value = "";
    } }, chatIn, h("button", { class: "btn primary", type: "submit", "aria-label": "إرسال" }, "➤"));
    els.chatPanel = h("div", { class: "card chat-panel" },
      h("div", { class: "row between" }, h("b", null, "💬 الدردشة"),
        h("button", { class: "btn ghost sm chat-close", onclick: () => toggleChat(false) }, "✕")),
      els.chatList, chatForm);
    els.backdrop = h("div", { class: "chat-backdrop hidden", onclick: () => toggleChat(false) });
    els.fabCount = h("span", { class: "count hidden" });
    els.fab = h("button", { class: "chat-fab", "aria-label": "الدردشة", onclick: () => toggleChat(true) }, "💬", els.fabCount);

    app.append(
      h("div", { class: "topbar card" },
        h("div", { class: "stack", style: "gap:2px" },
          h("b", { class: "gold" }, "غرفة " + S.code),
          h("div", { class: "row small muted" }, els.phase, h("span", { id: "rtt" }, rtt ? rtt + "ms" : ""))),
        h("div", { class: "row" }, els.round,
          h("button", { class: "btn danger sm", onclick: () => {
            if (confirm("هل تريد مغادرة الغرفة؟")) { send({ t: "leave" }); localStorage.removeItem(tokenKey(S.code)); leaveRoom(); }
          } }, "مغادرة"))),
      h("div", { class: "layout" },
        els.main,
        h("div", { class: "side stack" },
          h("div", { class: "card stack" }, els.playersTitle, els.players),
          els.chatPanel)),
      els.backdrop, els.fab);
    els.chatList.replaceChildren(...chat.map(chatNode));
    scrollChat();
  }

  function toggleChat(open) {
    chatOpen = open;
    els.chatPanel.classList.toggle("open", open);
    els.backdrop.classList.toggle("hidden", !open);
    if (open) { unread = 0; updateUnread(); scrollChat(); }
  }

  function chatNode(m) {
    if (m.sys) return h("div", { class: "msg sys" }, m.text);
    const mine = S && m.pid === S.me;
    return h("div", { class: "msg" + (mine ? " mine" : "") }, mine ? null : h("div", { class: "who" }, m.name), m.text);
  }

  function appendChat(m) {
    if (!els.chatList || !S) return;
    els.chatList.append(chatNode(m));
    while (els.chatList.childElementCount > 100) els.chatList.firstChild.remove();
    scrollChat();
    const desktop = window.matchMedia("(min-width: 900px)").matches;
    if (!chatOpen && !desktop && !m.sys && m.pid !== S.me) { unread++; updateUnread(); }
  }

  function scrollChat() { if (els.chatList) els.chatList.scrollTop = els.chatList.scrollHeight; }
  function updateUnread() {
    els.fabCount.textContent = unread > 9 ? "9+" : unread;
    els.fabCount.classList.toggle("hidden", unread === 0);
  }

  function renderPlayers() {
    const online = S.players.filter((p) => p.online).length;
    els.playersTitle.textContent = `👥 اللاعبون (${online}/${S.players.length})`;
    const showScore = S.phase !== "lobby";
    const ranked = showScore ? [...S.players].sort((a, b) => b.score - a.score) : S.players;
    els.players.replaceChildren(...ranked.map((p, i) => {
      const status = [];
      if (p.host) status.push(h("span", { class: "badge gold", title: "المضيف" }, "👑"));
      if (S.phase === "letter" && p.id === S.pickerId) status.push(h("span", { class: "badge" }, "✋ يختار"));
      if (S.phase === "playing" && p.submitted) status.push(h("span", { class: "badge green" }, "✓ أنهى"));
      if (S.phase === "review" && p.ready) status.push(h("span", { class: "badge green" }, "✓ جاهز"));
      if (!p.online) status.push(h("span", { class: "badge" }, "غير متصل"));
      const kick = isHost() && p.id !== S.me
        ? h("button", { class: "btn danger sm", onclick: () => { if (confirm(`طرد ${p.name}؟`)) send({ t: "kick", pid: p.id }); } }, "طرد")
        : null;
      return h("li", { class: "player" + (p.id === S.me ? " me" : "") + (p.online ? "" : " offline") },
        h("div", { class: "row", style: "min-width:0" },
          h("span", { class: "dot" + (p.online ? "" : " off") }),
          h("span", { class: "num" }, showScore ? p.score : i + 1),
          h("span", { class: "name" }, p.name)),
        h("div", { class: "row" }, status, kick));
    }));
  }

  // ------------------------------------------------------------ render
  function render() {
    if (!S) return;
    const key = [S.code, S.phase, S.round, isHost(), S.phase === "letter" ? S.pickerId === S.me : ""].join("|");
    if (!els.main || !app.contains(els.main)) buildShell();
    els.phase.textContent = PHASE_LABEL[S.phase] || S.phase;
    els.round.textContent = S.phase === "lobby" ? `${S.settings.totalRounds} جولات` : `جولة ${S.round}/${S.settings.totalRounds}`;
    renderPlayers();
    const view = VIEWS[S.phase] || VIEWS.judging;
    if (key !== builtKey) {
      builtKey = key;
      els.main.replaceChildren();
      view.build(els.main);
    }
    if (view.update) view.update(els.main);
    tick();
  }

  function remaining() { return deadline == null ? null : Math.max(0, deadline - performance.now()); }

  function tick() {
    const ms = remaining();
    document.querySelectorAll("[data-timer]").forEach((el) => {
      el.textContent = ms == null ? "" : Math.ceil(ms / 1000) + "ث";
      el.classList.toggle("low", ms != null && ms <= 10000);
    });
    document.querySelectorAll("[data-bar]").forEach((el) => {
      const total = Number(el.dataset.bar);
      el.firstChild.style.width = ms == null ? "0%" : Math.min(100, (ms / total) * 100) + "%";
      el.classList.toggle("low", ms != null && ms <= 10000);
    });
    const cd = document.getElementById("countdown");
    if (cd && ms != null) {
      const n = Math.ceil(ms / 1000);
      const txt = n > 0 ? String(n) : "ابدأ!";
      if (cd.textContent !== txt) { cd.textContent = txt; cd.style.animation = "none"; void cd.offsetWidth; cd.style.animation = ""; }
    }
    if (S && S.phase === "playing" && ms === 0 && !autoSubmitted && !(me() || {}).submitted) {
      autoSubmitted = true;
      clearTimeout(draftTimer);
      send({ t: "submit", answers: draft });
    }
  }
  setInterval(tick, 200);

  // ------------------------------------------------------------ views
  const VIEWS = {};

  VIEWS.lobby = {
    build(root) {
      const link = location.origin + "/room/" + S.code;
      const share = h("button", { class: "btn block", onclick: async () => {
        try {
          if (navigator.share) await navigator.share({ title: "ولد بنت حيوان جماد", text: "تعال العب معي!", url: link });
          else { await navigator.clipboard.writeText(link); toast("تم نسخ رابط الغرفة"); }
        } catch (_) { /* user cancelled */ }
      } }, "🔗 مشاركة رابط الغرفة");
      els.settings = h("div", { class: "stack" });
      els.start = h("div");
      root.append(
        h("div", { class: "card stack center" }, h("div", { class: "muted" }, "رمز الغرفة"), h("div", { class: "code-big" }, S.code), share),
        h("div", { class: "card stack" }, h("b", null, "⚙️ الإعدادات"), els.settings),
        els.start);
      if (isHost()) {
        const secs = h("select", { class: "input", id: "set-secs", onchange: (e) => send({ t: "settings", roundSeconds: +e.target.value }) },
          [45, 60, 90, 120, 150, 180, 240].map((s) => h("option", { value: s }, s + " ثانية")));
        const rounds = h("select", { class: "input", id: "set-rounds", onchange: (e) => send({ t: "settings", totalRounds: +e.target.value }) },
          [3, 4, 5, 6, 8, 10, 12, 15].map((r) => h("option", { value: r }, r + " جولات")));
        els.settings.append(
          h("label", { class: "field" }, h("label", null, "وقت الجولة"), secs),
          h("label", { class: "field" }, h("label", null, "عدد الجولات"), rounds));
      } else {
        els.settingsText = h("div", { class: "muted" });
        els.settings.append(els.settingsText);
      }
    },
    update() {
      const st = S.settings;
      if (isHost()) {
        const secs = document.getElementById("set-secs"), rounds = document.getElementById("set-rounds");
        if (secs && document.activeElement !== secs) { ensureOption(secs, st.roundSeconds, " ثانية"); secs.value = st.roundSeconds; }
        if (rounds && document.activeElement !== rounds) { ensureOption(rounds, st.totalRounds, " جولات"); rounds.value = st.totalRounds; }
      } else if (els.settingsText) {
        els.settingsText.textContent = `⏱️ ${st.roundSeconds} ثانية لكل جولة • 🔁 ${st.totalRounds} جولات`;
      }
      const online = S.players.filter((p) => p.online).length;
      if (isHost()) {
        if (!els.startBtn || !els.start.contains(els.startBtn)) {
          els.startBtn = h("button", { class: "btn primary block", onclick: () => send({ t: "start" }) });
          els.start.replaceChildren(els.startBtn);
        }
        els.startBtn.disabled = online < 2;
        els.startBtn.textContent = online < 2 ? "بانتظار لاعب آخر…" : "▶ ابدأ اللعبة";
      } else if (!els.start.firstChild || els.start.contains(els.startBtn)) {
        els.start.replaceChildren(h("p", { class: "center muted" }, "بانتظار المضيف لبدء اللعبة…"));
      }
    },
  };

  function ensureOption(sel, val, suffix) {
    if (![...sel.options].some((o) => +o.value === val)) sel.append(h("option", { value: val }, val + suffix));
  }

  VIEWS.letter = {
    build(root) {
      const mine = S.pickerId === S.me;
      const head = h("div", { class: "card center stack" },
        h("div", { class: "big" }, mine ? "دورك لاختيار الحرف" : `${pname(S.pickerId)} يختار الحرف الآن`),
        h("div", { class: "muted" }, mine ? "اختر حرفًا لم يُستخدم في هذه المباراة" : "انتظر قليلًا…"),
        h("div", { class: "timer", "data-timer": "" }));
      root.append(head);
      if (mine) {
        let extraIn = null;
        if (S.canAddCategory) {
          extraIn = h("input", { class: "input", maxLength: 20, placeholder: "أضف فئة جديدة (اختياري)" });
          root.append(h("div", { class: "card stack" },
            h("b", { class: "gold" }, "🏆 أنت ضمن الثلاثة الأوائل!"), extraIn,
            h("div", { class: "muted small" }, "تُستخدم هذه الفئة في هذه الجولة فقط.")));
        }
        const grid = h("div", { class: "letters" }, LETTERS.map((l) => {
          const used = S.usedLetters.includes(l);
          return h("button", { class: "letter" + (used ? " used" : ""), disabled: used, onclick: () => {
            grid.querySelectorAll("button").forEach((b) => (b.disabled = true));
            send({ t: "pick", letter: l, extra: extraIn ? extraIn.value.trim() : "" });
          } }, l);
        }));
        root.append(h("div", { class: "card" }, grid));
      } else if (isHost()) {
        root.append(h("button", { class: "btn block", onclick: () => send({ t: "force" }) }, "🎲 اختر حرفًا عشوائيًا الآن"));
      }
    },
  };

  VIEWS.countdown = {
    build(root) {
      root.append(h("div", { class: "card center stack", style: "padding:40px 16px" },
        h("div", { class: "muted" }, "الحرف"),
        h("div", { class: "letter-tile", style: "margin:0 auto" }, S.letter),
        extraNote(),
        h("div", { class: "muted" }, "تبدأ الجولة بعد…"),
        h("div", { class: "countdown", id: "countdown" }, "3")));
    },
  };

  function extraNote() {
    const ex = S.categories.find((c) => c.key === "extra");
    return ex ? h("div", { class: "badge gold", style: "margin:0 auto" }, "⭐ فئة إضافية: " + ex.label) : null;
  }

  VIEWS.playing = {
    build(root) {
      const total = S.settings.roundSeconds * 1000;
      els.finished = h("span", { class: "small muted" });
      root.append(h("div", { class: "card playbar" },
        h("div", { class: "letter-tile" }, S.letter),
        h("div", { style: "flex:1" },
          h("div", { class: "row between" }, els.finished, h("span", { class: "timer", "data-timer": "" })),
          h("div", { class: "bar", "data-bar": total }, h("div")))));

      els.inputs = [];
      const fields = h("div", { class: "stack" });
      S.categories.forEach((c, i) => {
        const inp = h("input", {
          class: "input", maxLength: 40, value: draft[c.key] || "", placeholder: `${c.label} بحرف ${S.letter}`,
          autocomplete: "off", autocorrect: "off", spellcheck: false, enterKeyHint: i === S.categories.length - 1 ? "done" : "next",
        });
        inp.addEventListener("input", () => {
          draft[c.key] = inp.value;
          clearTimeout(draftTimer);
          els.saved.textContent = "…";
          draftTimer = setTimeout(() => { if (send({ t: "draft", answers: draft })) els.saved.textContent = "✓ تم الحفظ تلقائيًا"; }, 350);
        });
        inp.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); const nx = els.inputs[i + 1]; if (nx) nx.focus(); else inp.blur(); }
        });
        els.inputs.push(inp);
        fields.append(h("div", { class: "card field" }, h("label", null, c.emoji + " " + c.label), inp));
      });
      els.saved = h("div", { class: "saved center" });
      els.submit = h("button", { class: "btn primary block", onclick: () => {
        clearTimeout(draftTimer);
        send({ t: "submit", answers: draft });
      } }, "✅ انتهيت");
      els.forceEnd = isHost() ? h("button", { class: "btn block", onclick: () => { if (confirm("إنهاء الجولة للجميع الآن؟")) send({ t: "force" }); } }, "⏹ إنهاء الجولة الآن") : null;
      root.append(fields, els.saved, els.submit, els.forceEnd);
      if (!(me() || {}).submitted && window.matchMedia("(pointer:fine)").matches) els.inputs[0].focus();
    },
    update() {
      const done = S.players.filter((p) => p.submitted).length;
      const online = S.players.filter((p) => p.online).length;
      els.finished.textContent = `أنهى ${done} من ${online}`;
      const submitted = (me() || {}).submitted;
      els.inputs.forEach((i) => (i.disabled = !!submitted));
      els.submit.disabled = !!submitted;
      els.submit.textContent = submitted ? "بانتظار بقية اللاعبين…" : "✅ انتهيت";
    },
  };

  VIEWS.collecting = VIEWS.judging = {
    build(root) {
      root.append(h("div", { class: "card center stack", style: "padding:40px 16px" },
        h("div", { class: "spinner" }),
        h("div", { class: "big" }, S.phase === "collecting" ? "انتهى الوقت!" : "الذكاء الاصطناعي يراجع الإجابات…"),
        h("div", { class: "muted" }, "لحظات من فضلك")));
    },
  };

  VIEWS.review = {
    build() {},
    update(root) {
      const keep = window.scrollY;
      root.replaceChildren();
      const cats = S.categories;
      const roundPts = (pid) => cats.reduce((s, c) => s + (S.points[pid + ":" + c.key] || 0), 0);

      if (S.judgeError) root.append(h("div", { class: "banner" }, "⚠️ تعذّر وصول الحكم الآلي لبعض الإجابات — سيتم الحكم عليها بالتصويت."));
      const open = S.appeals.filter((a) => a.status === "open");
      if (open.length) root.append(h("div", { class: "banner", style: "background:rgba(245,179,36,.1);border-color:rgba(245,179,36,.4)" },
        `🗳️ ${open.length} تصويت مفتوح — صوّت قبل المتابعة`));

      // results summary
      const sorted = [...S.players].sort((a, b) => (b.score + roundPts(b.id)) - (a.score + roundPts(a.id)));
      root.append(h("div", { class: "card stack" },
        h("div", { class: "row between" }, h("b", { class: "big", style: "font-size:20px" }, `نتائج الجولة — حرف ${S.letter}`), extraNote()),
        h("ul", { class: "players" }, sorted.map((p, i) => h("li", { class: "player" + (i === 0 ? " rank-1" : "") },
          h("div", { class: "row" }, h("span", { class: "num" }, i + 1), h("span", { class: "name" }, p.name)),
          h("div", { class: "row" }, h("span", { class: "badge gold" }, "+" + roundPts(p.id)), h("b", null, p.score + roundPts(p.id))))))));

      // answers per category
      const tabs = h("div", { class: "tabs" }, cats.map((c, i) => {
        const pending = open.some((a) => a.key.endsWith(":" + c.key));
        return h("button", { class: "tab" + (i === reviewTab ? " active" : ""), onclick: () => { reviewTab = i; render(); } },
          c.emoji + " " + c.label + (pending ? " 🗳️" : ""));
      }));
      const c = cats[Math.min(reviewTab, cats.length - 1)];
      const list = h("div", { class: "stack" }, S.players.map((p) => answerCard(p, c)));
      root.append(h("div", { class: "card stack" }, h("b", null, "📝 مراجعة الإجابات"), tabs, list,
        h("div", { class: "row between" },
          h("button", { class: "btn sm", disabled: reviewTab === 0, onclick: () => { reviewTab--; render(); } }, "→ السابق"),
          h("button", { class: "btn sm", disabled: reviewTab >= cats.length - 1, onclick: () => { reviewTab++; render(); } }, "التالي ←"))));

      const amReady = (me() || {}).ready;
      const last = S.round >= S.settings.totalRounds;
      root.append(h("button", { class: "btn primary block", disabled: amReady, onclick: () => send({ t: "ready" }) },
        amReady ? "بانتظار البقية…" : last ? "✓ جاهز — عرض النتيجة النهائية" : "✓ جاهز للجولة التالية"));
      if (isHost()) root.append(h("button", { class: "btn block", onclick: () => { if (confirm("المتابعة دون انتظار الجميع؟")) send({ t: "force" }); } }, "⏭ متابعة الآن (المضيف)"));
      window.scrollTo(0, keep);
    },
  };

  function answerCard(p, c) {
    const key = p.id + ":" + c.key;
    const text = ((S.answers[p.id] || {})[c.key] || "").trim();
    const v = S.verdicts[key] || { valid: false, reason: "" };
    const pts = S.points[key] || 0;
    const appeal = [...S.appeals].reverse().find((a) => a.key === key);
    const state = v.valid === null ? "pending" : v.valid ? "valid" : "invalid";
    const badge = v.valid === null ? h("span", { class: "badge gold" }, "⏳ لم يُحكم")
      : v.valid ? h("span", { class: "badge green" }, "✓ صحيحة") : h("span", { class: "badge red" }, "✗ خاطئة");
    const byLabel = { ai: "🤖", rule: "📏", vote: "🗳️", pending: "⏳" }[v.by] || "";

    const kids = [
      h("div", { class: "row between" },
        h("div", { class: "row" }, h("b", null, p.name), p.id === S.me ? h("span", { class: "muted small" }, "(أنت)") : null),
        h("div", { class: "row" }, badge, h("span", { class: "badge gold" }, "+" + pts))),
      h("div", { class: "word" }, text || h("span", { class: "muted" }, "— لا توجد إجابة —")),
      v.reason ? h("div", { class: "muted small" }, byLabel + " " + v.reason) : null,
    ];

    if (appeal && appeal.status === "open") {
      const yes = Object.values(appeal.votes).filter(Boolean).length;
      const no = Object.values(appeal.votes).length - yes;
      const canVote = p.id !== S.me;
      const myVote = appeal.votes[S.me];
      kids.push(h("div", { class: "vote stack", style: "gap:8px" },
        h("b", null, appeal.opener ? `🗳️ اعتراض من ${pname(appeal.opener)} — هل هذه الإجابة صحيحة؟` : "🗳️ هل هذه الإجابة صحيحة؟"),
        canVote ? h("div", { class: "row" },
          h("button", { class: "btn sm" + (myVote === true ? " primary" : ""), onclick: () => send({ t: "vote", aid: appeal.id, yes: true }) }, `👍 نعم (${yes})`),
          h("button", { class: "btn sm" + (myVote === false ? " primary" : ""), onclick: () => send({ t: "vote", aid: appeal.id, yes: false }) }, `👎 لا (${no})`))
          : h("div", { class: "muted small" }, `الآخرون يصوّتون على إجابتك… (نعم ${yes} • لا ${no})`)));
    } else if (appeal && appeal.opener) {
      kids.push(h("div", { class: "muted small" }, appeal.status === "accepted" ? "✅ قُبلت بالتصويت" : "❌ رُفضت بالتصويت"));
    } else if (text && v.valid !== null) {
      kids.push(h("div", null, h("button", { class: "btn sm", onclick: () => send({ t: "appeal", pid: p.id, cat: c.key }) }, "✋ اعتراض")));
    }
    return h("div", { class: "answer " + state }, kids);
  }

  VIEWS.final = {
    build(root) {
      const ranked = [...S.players].sort((a, b) => b.score - a.score);
      const pod = [ranked[1], ranked[0], ranked[2]];
      root.append(h("div", { class: "card stack center" },
        h("div", { class: "logo" }, "🏆"),
        h("div", { class: "big gold" }, ranked[0] ? `الفائز: ${ranked[0].name}` : "انتهت اللعبة"),
        h("div", { class: "podium" }, pod.map((p, i) => p ? h("div", { class: ["p2", "p1", "p3"][i] },
          h("div", { style: "font-size:26px" }, ["🥈", "🥇", "🥉"][i]), h("b", null, p.name), h("div", null, p.score)) : null)),
        h("ul", { class: "players" }, ranked.map((p, i) => h("li", { class: "player" },
          h("div", { class: "row" }, h("span", { class: "num" }, i + 1), h("span", { class: "name" }, p.name)), h("b", null, p.score)))),
        isHost() ? h("button", { class: "btn primary block", onclick: () => send({ t: "again" }) }, "🔁 العب مرة أخرى")
          : h("div", { class: "muted" }, "بانتظار المضيف لبدء مباراة جديدة…")));
    },
  };

  // ------------------------------------------------------------ boot
  function boot() {
    const m = location.pathname.match(/^\/room\/([A-Za-z0-9]{5})/);
    if (!m) { showHome(); return; }
    const code = m[1].toUpperCase();
    const fresh = new URLSearchParams(location.search).has("new");
    const token = fresh ? null : localStorage.getItem(tokenKey(code));
    if (token || (fresh && savedName())) {
      app.replaceChildren(h("div", { class: "home center muted" }, h("div", { class: "spinner" }), "جارٍ الدخول…"));
      connect({ t: "join", code, token, name: savedName() });
    } else {
      showHome(code);
    }
  }

  window.addEventListener("online", () => { if (joined && !ws) connect(hello); });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && joined && (!ws || ws.readyState > 1)) connect(hello);
  });
  boot();
})();
