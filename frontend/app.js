/* Recourse cockpit (R6). Dependency-free. Every fact on screen comes from
   the API; the ledger is rendered from the audit hash chain; the countdown
   ticks locally from a server-authoritative snapshot and re-syncs — the
   backend remains the deadline authority regardless of anything here. */

const $ = (s, el = document) => el.querySelector(s);
const main = $("#main");
const rupee = (n) => "\u20b9" + Number(n || 0).toLocaleString("en-IN");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const prov = (p) => p ? `<span class="prov ${esc(p)}">${esc(p).replace("_", " ")}</span>` : "";

async function api(path, opts) {
  const r = await fetch(path, opts);
  const b = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(b.error || r.status); e.body = b; throw e; }
  return b;
}

/* ---------- routing ---------- */
let cleanup = [];
function onLeave(fn) { cleanup.push(fn); }
async function route() {
  cleanup.forEach((f) => { try { f(); } catch (_) {} });
  cleanup = [];
  const [view, arg] = (location.hash.replace(/^#\//, "") || "intake").split("/");
  if (window.__lastView === "cases" && view !== "cases")
    window.__casesScroll = scrollY;
  document.querySelectorAll("nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === view));
  rcReveal(document.getElementById("main"));
  if (view === "cases" && window.__casesScroll != null) {
    scrollTo({ top: window.__casesScroll, behavior: "auto" });
    window.__casesScroll = null;
  }
  window.__lastView = view;
  main.innerHTML = "<p class='muted'>Loading\u2026</p>";
  try {
    if (view === "case" && arg) { await renderCase(arg); bindAsk(arg); }
    else await ({intake: renderIntake, overview: renderOverview,
                 cases: renderQueue, metrics: renderMetrics,
                 home: renderLanding}[view] || renderLanding)();
  } catch (e) { main.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
  main.focus();
}
window.addEventListener("hashchange", route);

/* ---------- landing ---------- */
async function renderLanding() {
  main.innerHTML = `<div class="landing">
    <div class="kicker">Merchant revenue recovery</div>
    <h1>AI that investigates before it acts.</h1>
    <p class="lede">When a chargeback arrives, Recourse doesn't blindly
      fight it. It assembles the fragments — payment, order, shipment,
      courier record, policy, the customer's own words — verifies every
      claim deterministically, and moves money only through a bounded,
      audited executor.</p>
    <div class="flow">
      <div><b>1</b>INVESTIGATE</div><div><b>2</b>VERIFY</div>
      <div><b>3</b>DECIDE</div><div><b>4</b>ACT</div><div><b>5</b>PROVE</div>
    </div>
    <div class="creed">AI investigates. <b>Evidence proves.</b> Policy
      decides. <b>Execution acts.</b> Audit proves.</div>
    <div class="actions">
      <a class="btn primary" href="#/intake">Start an investigation</a>
      <a class="btn" href="#/overview">Open operations</a>
      <a class="btn" href="#/metrics">See the evaluation</a>
    </div></div>`;
}

/* ---------- P1: live clock + ambience + entrance reveals ---------- */
const RC_REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
(() => {
  const top = document.getElementById("top-status");
  if (top && !document.getElementById("liveclock")) {
    const c = document.createElement("span");
    c.id = "liveclock";
    top.prepend(c);
    const tick = () => {
      const n = new Date();
      c.textContent = n.toLocaleDateString("en-GB", { day: "2-digit",
        month: "short", year: "numeric" }).toUpperCase()
        + " \u00b7 " + n.toLocaleTimeString("en-US", { hour: "2-digit",
        minute: "2-digit", second: "2-digit", hour12: true });
    };
    tick();
    setInterval(tick, 1000);   // topbar lives outside #main: keeps
  }                            // ticking across views and case opens
  if (!RC_REDUCED && !document.querySelector(".rc-sweep")) {
    const s = document.createElement("div");
    s.className = "rc-sweep";
    document.body.appendChild(s);
    for (let i = 0; i < 10; i++) {
      const d = document.createElement("span");
      d.className = "rc-dust";
      d.style.left = ((i * 9.7 + 4) % 100) + "%";
      d.style.animationDelay = -((i * 2.1) % 20) + "s";
      document.body.appendChild(d);
    }
  }
})();
function rcReveal(scope) {
  const els = [...(scope || document).querySelectorAll(
    ".hero-row>*, .kpi, .card, .panel")].slice(0, 40);
  els.forEach((el, i) => {
    el.classList.add("rv");
    el.style.transitionDelay = RC_REDUCED ? "0s" : `${(i % 8) * 60}ms`;
  });
  requestAnimationFrame(() => requestAnimationFrame(() =>
    els.forEach((el) => el.classList.add("on"))));
}

/* ---------- intake ---------- */
const WF = [
  ["\ud83d\udcc4", "INTAKE", "Understand what happened."],
  ["\ud83d\udd0d", "INVESTIGATE", "Search orders, payments, shipments and policy."],
  ["\ud83d\udee1", "VERIFY", "AI findings pass deterministic evidence verification."],
  ["\u2696\ufe0f", "DECIDE", "Policy engine evaluates FIGHT / ACCEPT / ESCALATE."],
  ["\ud83d\udcb0", "RECOVER", "Only the controlled executor can submit financial actions."]];
const EXAMPLES = [
  "The customer says the order never arrived, but we dispatched it on time",
  "Customer disputes the payment for order #0019 and wants a refund",
  "Courier shows delivered for order #0019 but our POD file is missing",
  "Customer was charged twice for pay_0042 and wants one charge reversed"];

async function renderIntake() {
  let cases = [];
  try { cases = (await api("/cases")).cases || []; } catch (e) {}
  main.innerHTML = `<div class="cc">
    <div class="hero-wrap"><div>
      <div class="hero-sys">RECOURSE \u00b7 SYSTEM <b>ONLINE</b>
        \u00b7 <b>${cases.length}</b> ACTIVE DISPUTES \u00b7
        <b>${cases.filter((c) => c.state !== "CLOSED").length}</b>
        UNDER INVESTIGATION</div>
    <div class="kicker">Recourse \u00b7 merchant revenue recovery</div>
    <h1>Investigate before you pay.</h1>
    <p class="support">Recourse uses AI to investigate disputes, verify
      evidence, and recover revenue \u2014 without letting AI make unsafe
      financial decisions.</p></div>
    <div class="hero-orn">MERCHANTS<br>KEEP COMMERCE<br>MOVING. \u2192</div>
    </div>
    <div class="wf">${WF.map(([ico, k, d], i) =>
      `<div><span class="num">${i + 1}</span><span class="ico">${ico}</span><b>${k}</b><span class="wfd">${d}</span></div>`).join("")}</div>
    <div class="cc-grid">
      <div class="workspace">
        <div class="whead">Start an investigation</div>
        <p class="sub" style="margin:4px 0 10px">Tell Recourse what happened
          \u2014 your own words, the customer's pasted message, or a
          payment / order reference.</p>
        <textarea id="story" maxlength="4000" placeholder="The customer says they never received order #1042, but our courier says it was delivered yesterday\u2026"></textarea>
        <div class="counter"><span id="charn">0</span> / 4000</div>
        <div class="fields">
          <div><label>PAYMENT ID (OPTIONAL)</label>
            <input type="text" id="payid" placeholder="e.g. pay_0019"></div>
          <div><label>ORDER REFERENCE (OPTIONAL)</label>
            <input type="text" id="ordref" placeholder="e.g. ORD-0019"></div>
        </div>
        <div class="chips">${EXAMPLES.map((e, i) =>
          `<button data-ex="${i}">\u201c${esc(e.slice(0, 44))}\u2026\u201d</button>`).join("")}</div>
        <div class="hero-row">
          <button class="btn voice" id="mic" aria-pressed="false" hidden>\ud83c\udf99 Dictate</button>
          <button class="btn primary" id="go">\u2192&nbsp; Start investigation</button>
        </div>
        <div class="trust">
          <div><b>YOUR MESSAGE</b><span>Stored verbatim</span></div>
          <div><b>AI INTERPRETATION</b><span class="u">Untrusted</span></div>
          <div><b>FINAL DECISION</b><span class="d">Deterministic</span></div>
        </div>
        <div class="trust-line">Recourse investigates with AI \u2014 but a
          financial decision is never made by the LLM alone.</div>
        <div class="interp" id="interp"></div>
      </div>
      <div>
        <div class="preview">
          <div class="plabel">EXAMPLE INVESTIGATION \u2014 PRODUCT PREVIEW,
            NOT LIVE DATA</div>
          <div class="pbody">
            <div class="claim">\u201cI never received my order.\u201d</div>
            <div class="exrow"><span>Order</span><span class="ok">\u2713 found</span></div>
            <div class="exrow"><span>Shipment</span><span class="ok">\u2713 found</span></div>
            <div class="exrow"><span>Courier tracking</span><span class="ok">\u2713 delivered</span></div>
            <div class="exrow"><span>Proof of delivery</span><span class="warn">\u26a0 missing</span></div>
            <div class="exrow"><span>Policy</span><span class="ok">\u2713 retrieved</span></div>
            <div class="pneeds"><b>RECOURSE NEEDS INPUT</b><br>
              \u201cUpload the courier proof of delivery.\u201d</div>
            <div class="pdone">EVIDENCE VERIFIED \u2192 POLICY EVALUATED
              \u2192 DECISION READY</div>
            <div class="pdl"><div class="k">EVERY DISPUTE HAS A DEADLINE
              (example)</div><div class="t">23:41:18</div>
              <div class="k">live cases use the server-authoritative
              countdown</div></div>
          </div>
        </div>
        <div class="panel" id="sysstatus"><h3>System status</h3>
          <div class="muted">checking\u2026</div></div>
      </div>
    </div></div>`;
  const story = $("#story"), charn = $("#charn");
  if (story && charn) story.addEventListener("input",
    () => { charn.textContent = story.value.length; });
  api("/cases").then(({ cases }) => {
    const set = (id, n) => { const el = $(id);
      if (el) { el.hidden = !n; el.textContent = n; } };
    set("#n-needs", cases.filter((c) => c.state === "needs_input").length);
    set("#n-review", cases.filter((c) => c.escalated).length);
  }).catch(() => {});
  document.querySelectorAll(".chips button").forEach((b) =>
    b.addEventListener("click", () => {
      $("#story").value = EXAMPLES[b.dataset.ex]; $("#story").focus();
    }));
  api("/health").then((h) => {
    const rows = Object.entries(h.integrations || {}).map(([k, v]) =>
      `<div class="st"><span>${esc(k.toUpperCase())}</span>
       <span><span class="dot ${esc(v.mode)}"></span>${esc(v.mode)}</span></div>`);
    $("#sysstatus").innerHTML = `<h3>System status</h3>${rows.join("")}
      <div class="muted" style="font:10px var(--mono);margin-top:6px">
      simulated surfaces are labeled \u2014 never claimed real</div>`;
    const rail = $("#rail-status");
    if (rail) rail.innerHTML = "<b style=\"font:700 10px var(--mono);letter-spacing:.12em\">SYSTEM STATUS</b>" + rows.join("");
    const top = $("#top-status");
    if (top) top.innerHTML = `<span class="pill">\u25cf DEMO</span>
      <span>${Object.values(h.integrations || {}).every((v) =>
        v.mode !== "unavailable") ? "all surfaces reporting" : "some surfaces unavailable"}</span>`;
  }).catch(() => {});
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    const mic = $("#mic"); mic.hidden = false;
    let rec = null;
    mic.onclick = () => {
      if (rec) { rec.stop(); return; }
      rec = new SR(); rec.lang = "en-IN"; rec.interimResults = false;
      mic.setAttribute("aria-pressed", "true");
      rec.onresult = (e) => { $("#story").value +=
        (($("#story").value && " ") || "") + e.results[0][0].transcript; };
      rec.onend = () => { mic.setAttribute("aria-pressed", "false"); rec = null; };
      rec.start();
    };
  }
  $("#go").onclick = async () => {
    $("#go").disabled = true;
    try {
      let text = $("#story").value;
      const ord = ($("#ordref") ? $("#ordref").value : "").trim();
      if (ord && !text.includes(ord))
        text += ` (order reference: ${ord})`;   // backend anchors from text
      const body = { text };
      if ($("#payid").value.trim()) body.payment_id = $("#payid").value.trim();
      const r = await api("/intake", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      location.hash = `#/case/${r.case_id}`;
    } catch (e) {
      const b = e.body || {};
      $("#interp").innerHTML = `<div class="panel">
        <h3>Recourse needs a little more</h3>
        <div>${esc(e.message)}</div>
        ${b.missing ? `<div class="mono">missing: ${esc(b.missing.join(", "))}</div>` : ""}
        ${b.interpretation && b.interpretation.reason_code ? `
          <div class="rule-line">how I read it: ${esc(b.interpretation.customer_claim || "")}
          \u2192 <b>${esc(b.interpretation.reason_code)}</b>
          ${confMeter(b.interpretation.confidence)}</div>` : ""}</div>`;
      $("#go").disabled = false;
    }
  };
}
const confMeter = (c) => c == null ? "" :
  `<span class="conf ${c < 0.7 ? "low" : ""}"><span class="bar">
   <i style="width:${Math.round(c * 100)}%"></i></span>
   <span class="mono">${Math.round(c * 100)}%</span></span>`;

/* ---------- countdown (server-authoritative snapshot, local ticks) ---------- */
function fmtCountdown(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60), sec = s % 60;
  return (d ? d + "d " : "") + String(h).padStart(2, "0") + "h " +
         String(m).padStart(2, "0") + "m " + String(sec).padStart(2, "0") + "s";
}
const statusOf = (s) => s <= 0 ? "EXPIRED" : s < 86400 ? "CRITICAL"
                        : s < 172800 ? "WARNING" : "SAFE";
function mountCountdown(el, snapshot, onExpire) {
  let base = performance.now(), snap = snapshot, expired = false;
  function tick() {
    const rem = snap.remaining_seconds - (performance.now() - base) / 1000;
    const st = snap.status === "EXPIRED" ? "EXPIRED" : statusOf(rem);
    el.className = "countdown " + st;
    el.innerHTML = `<div class="label">DEADLINE \u2014 ${st}</div>
      <div class="clock">${st === "EXPIRED" ? "EXPIRED" : fmtCountdown(rem) + " remaining"}</div>
      <div class="meta">respond by ${esc(snap.respond_by)} \u00b7 server is the
        time authority</div>`;
    if (st === "EXPIRED" && !expired) { expired = true; onExpire && onExpire(); }
  }
  tick();
  const t = setInterval(tick, 1000);
  const sync = setInterval(async () => {
    try { snap = await api(el.dataset.href); base = performance.now(); }
    catch (_) {}
  }, 30000);
  onLeave(() => { clearInterval(t); clearInterval(sync); });
}

/* ---------- case cockpit ---------- */
const AGENT_STATES = {
  needs_input: ["waiting", "waiting for the merchant"],
  closed: ["done", "resolved"], acted: ["done", "executed"],
  escalated: ["stopped", "handed to a human"],
};
async function renderCase(caseId) {
  const [c, ev, audit] = await Promise.all([
    api(`/cases/${caseId}`), api(`/cases/${caseId}/evidence`),
    api(`/cases/${caseId}/audit`)]);
  const [pill, pillText] = AGENT_STATES[c.state] || ["working", "investigating"];
  const flowHit = (name) => ({
    DISPUTE: true, ORDER: !!c.order, PAYMENT: !!c.order,
    EVIDENCE: ev.evidence.length > 0,
    VERIFICATION: ev.evidence.some((e) => e.verdict),
    POLICY: !!c.decision_math, DECISION: !!c.decision,
    EXECUTION: !!c.execution })[name];
  const chain = ev.evidence.length ? ev.evidence.map((e) =>
      `<span class="n ${e.verdict === "PASS" ? "VERIFIED" :
        "INADMISSIBLE"}">${esc(e.key).slice(0, 26)}</span>`)
      .join('<span class="e"></span>')
    : `<span class="n MISSING">MISSING EVIDENCE</span>`;
  const needsChip = c.needs_input
    ? '<span class="e"></span><span class="n NEEDS">NEEDS INPUT</span>'
    : "";
  main.innerHTML = `<div class="dossier-view">
    <button class="dclose" id="dclose"
      aria-label="Close case dossier">CLOSE \u2715</button>
    <div class="dseq" aria-hidden="true">${["CASE IDENTIFIED",
      "LOADING RECORDS", "GATHERING EVIDENCE", "VERIFYING CLAIMS",
      "ADMISSIBILITY CHECK", "DECISION"].map((s) =>
      `<span>${s}</span>`).join("<i></i>")}</div>
    <div class="dhead">
      <div><div class="lab">CASE</div>
        <div class="val">${esc(caseId)}</div></div>
      <div><div class="lab">DISPUTE REASON</div>
        <div class="val">${esc(c.reason_code)}</div></div>
      <div><div class="lab">STATUS</div>
        <div class="val">${esc(c.state)}</div></div>
      <div><div class="lab">AMOUNT</div>
        <div class="val amt">${rupee(c.amount)}</div></div>
      <div><div class="lab">RESPOND BY</div>
        <div class="val">${esc((c.respond_by || "").slice(0, 10))}</div>
      </div>
      <div><div class="lab">DECISION</div>
        <div class="val">${esc(c.decision || "PENDING")}</div></div>
    </div>
    <div class="dflow" aria-label="dispute flow">${["DISPUTE", "ORDER",
      "PAYMENT", "EVIDENCE", "VERIFICATION", "POLICY", "DECISION",
      "EXECUTION"].map((s) => `<span class="n ${flowHit(s) ? "hit" :
      "miss"}">${s}</span>`).join('<span class="e"></span>')}</div>
    <div class="echain" aria-label="evidence chain">
      <span class="n hit" style="border-color:var(--rule)">CUSTOMER
      CLAIM</span><span class="e"></span>${chain}${needsChip}</div>
    <div class="dladder" aria-hidden="false">
      <div class="rung ai"><span>AI INVESTIGATES</span>
        <span>proposes, never decides</span></div>
      <div class="arrow">\u2193</div>
      <div class="rung det"><span>EVIDENCE PROVES</span>
        <span>verbatim, source-verified</span></div>
      <div class="arrow">\u2193</div>
      <div class="rung det"><span>POLICY DECIDES</span>
        <span>deterministic, versioned</span></div>
      <div class="arrow">\u2193</div>
      <div class="rung det"><span>EXECUTION ACTS</span>
        <span>single controlled executor</span></div>
      <div class="arrow">\u2193</div>
      <div class="rung det"><span>AUDIT RECORDS</span>
        <span>tamper-evident chain</span></div>
    </div>
    <div class="casebar">
      <a href="#/cases">\u2190</a>
      <span class="dnum">DISPUTE #${esc(c.dispute_id)}</span>
      <span class="damt rupee">${rupee(c.amount)}</span>
      <span class="badge neutral">${esc(c.reason_code)}</span>
      ${prov(c.dispute_provenance)}
      <span class="agent-pill ${pill}"><span class="dot"></span>
        agent: ${esc(pillText)}</span>
      ${c.decision ? `<span class="stamp ${c.decision === "FIGHT" ? "pass" : "fail"}">${esc(c.decision)}</span>` : ""}
      <div class="countdown" id="cd" data-href="/cases/${esc(caseId)}/deadline"></div>
    </div>
    ${c.needs_input ? askPanel(c.needs_input) : ""}
    ${c.escalation ? `<div class="panel escalation"><h3>HUMAN REVIEW REQUIRED</h3>
       <pre>${esc(c.escalation.merchant_summary)}</pre>
       <div class="actions" id="hactions"></div>
       <div class="notice" id="act-result"></div></div>` : ""}
    <div class="cols">
      <section>
        <h2>Investigation ledger <span class="muted"
          style="font:11px var(--mono)">rendered from the tamper-evident
          audit chain</span></h2>
        <div class="ledger" id="ledger">${ledger(audit.entries)}</div>
        <h2>Evidence exhibits</h2>
        ${ev.evidence.length ? ev.evidence.map(exhibit).join("")
          : "<p class='muted'>No evidence yet.</p>"}
      </section>
      <section>
        ${c.decision_math ? mathPanel(c) : ""}
        ${c.draft ? draftPanel(c) : ""}
        ${c.execution ? execPanel(c) : ""}
        <div class="panel"><h3>Audit integrity</h3>
          <span class="chainbadge ${audit.chain.valid ? "ok" : "bad"}">
          ${audit.chain.valid ? "\u2713 CHAIN VERIFIED (" + audit.chain.entries + " entries)"
            : "\u2717 TAMPER DETECTED \u00b7 entry " + audit.chain.broken_at}</span></div>
      </section>
    </div>`;
  mountCountdown($("#cd"), c.deadline, () => {
    document.querySelectorAll(".actions .btn, #resume-btn").forEach(
      (b) => { b.disabled = true; });
    const n = $("#act-result");
    if (n) n.textContent = "Deadline expired \u2014 actions disabled (the " +
      "server rejects them regardless).";
  });
  if (c.escalation && c.deadline.status !== "EXPIRED") humanActions(c, caseId);
  bindCitations(c);
  bindKb(c);
  if (!["closed", "acted", "escalated"].includes(c.state)) {
    const poll = setInterval(async () => {
      const fresh = await api(`/cases/${caseId}`).catch(() => null);
      if (fresh && fresh.state !== c.state) {
        clearInterval(poll); renderCase(caseId).then(() => bindAsk(caseId));
      }
    }, 4000);
    onLeave(() => clearInterval(poll));
  }
}

/* the Investigation Ledger — audit steps -> typed, ordered entries */
const LEDGER_MAP = {
  CASE_SUBMITTED: (p) => ["PLAN", "", `merchant reported the dispute in their own words \u2014 interpretation: ${p.interpretation ? esc(p.interpretation.reason_code) : ""} ${p.interpretation ? "(" + Math.round((p.interpretation.confidence || 0) * 100) + "% confidence, untrusted)" : ""}`],
  CASE_CREATED: (p) => ["PLAN", "", `dispute received \u2014 ${rupee(p.amount)}, ${esc(p.reason_code)}, ${esc(p.hours_left)}h to deadline`],
  AGENT_PLAN: (p) => ["PLAN", "", `investigation planned: establish ${esc((p.checklist || []).join(", "))} \u00b7 budget ${p.limits ? esc(p.limits.tool_budget) : ""} tool calls`],
  LINK_COMPLETED: (p) => ["CHECK", "", `order linked via ${esc(p.method)} \u2192 ${esc(p.order_id)} (confidence ${esc(p.confidence)})`],
  TOOL_CALL: (p) => ["TOOL", "t-tool", `${esc(p.tool)}(${Object.values(p.args || {}).map(esc).join(", ")}) ${p.ok ? "" : "\u2192 " + esc(p.error)}`],
  AGENT_OBSERVATION: (p) => ["OBSERVATION", "", `${esc(p.goal)} \u2014 ${esc(String(p.observation || "").slice(0, 110))} ${(p.provenance || []).map(prov).join("")}`],
  EVIDENCE_EXTRACTED: (p) => ["CHECK", "", `${esc(p.count)} evidence candidates proposed: ${esc((p.keys || []).join(", "))}`],
  EVIDENCE_ADMITTED: (p) => ["CHECK", "t-check", `gate ADMITTED ${(p.ids || []).length} exhibit(s) \u2014 verbatim + system-of-record verified`],
  EVIDENCE_REJECTED: (p) => ["REJECTED", "t-fail", `gate REJECTED ${(p.items || []).length}: ${esc((p.items || []).map((i) => i.reason).join("; ").slice(0, 110))}`],
  AGENT_NEEDS_INPUT: (p) => ["ASK", "t-ask", `agent asks the merchant: ${esc(p.request_to_user)}`],
  DOCUMENT_UPLOADED: (p) => ["OBSERVATION", "", `merchant provided ${esc(p.filename)} (${esc(p.kind)}) ${prov(p.provenance)} \u00b7 sha ${esc(String(p.sha256 || "").slice(0, 10))}\u2026`],
  USER_INPUT_RECEIVED: () => ["RESUME", "", "the requested input arrived"],
  INVESTIGATION_RESUMED: (p) => ["RESUME", "", `investigation resumed with ${(p.uploads_attached || []).length} merchant document(s), ${esc(p.hours_left)}h left`],
  DECISION_MADE: (p) => ["DECISION", "t-decide", `${esc(p.action)} via ${esc(p.rule_fired)} \u2014 EV(fight) ${rupee(p.ev_fight)} vs EV(accept) ${rupee(p.ev_accept)}`],
  DRAFT_VALIDATED: () => ["CHECK", "t-check", "representment citations validated \u2014 every factual sentence cites an admitted exhibit"],
  ACTION_SUBMITTED: (p) => ["ACTION", "t-decide", `${esc(p.action)} executed via ${esc(p.adapter)}${p.simulated ? " [SIMULATED]" : ""} by ${esc(p.actor)} \u00b7 idempotency ${esc(p.idempotency_key)}`],
  HUMAN_APPROVED: (p) => ["ACTION", "", `human approval: ${esc(p.action)} by ${esc(p.actor_name)}`],
  CASE_ESCALATED: (p) => ["ASK", "t-ask", `escalated to a human: ${esc(String(p.reason || "").slice(0, 110))}`],
  CASE_CLOSED: (p) => ["DECISION", "t-decide", `case closed \u2014 dispute ${esc(p.dispute_status)}`],
};
function ledger(entries) {
  let i = 0;
  return entries.map((e) => {
    const f = LEDGER_MAP[e.step] || (e.step.startsWith("DEADLINE_")
      ? (p) => ["DEADLINE", "", `deadline ${esc(e.step.replace("DEADLINE_", "").toLowerCase())} \u2014 ${esc(p.remaining_seconds)}s remaining`]
      : null);
    if (!f) return "";
    const [chip, cls, text] = f(e.payload || {});
    return `<div class="entry ${cls || ""}" style="animation-delay:${Math.min(i++ * 60, 1200)}ms">
      <span class="chip ${chip}">${chip}</span><span class="etext">${text}</span>
      <div class="emeta">${esc(e.at)} \u00b7 #${e.seq} \u00b7 ${esc(String(e.entry_hash))}\u2026</div></div>`;
  }).join("");
}

function askPanel(req) {
  return `<div class="panel ask-panel"><h3>What the agent needs from you</h3>
    <div>${esc(req.action)}</div>
    <div class="mono" style="margin-top:6px">missing: ${esc((req.requested || []).join(", "))}
      \u00b7 status: ${esc(req.status)}</div>
    <label class="dropzone" id="dz">Drop a .txt / .eml / POD photo here, or
      click to choose a file
      <input type="file" id="fpick" accept=".txt,.eml,.png,.jpg,.jpeg,.webp"></label>
    <ul class="uplist" id="uplist"></ul>
    <div class="actions"><button class="btn primary" id="resume-btn">Resume
      investigation</button></div>
    <div class="notice" id="up-note">uploads are untrusted until they pass the
      same admissibility checks as every other document</div></div>`;
}
function bindAsk(caseId) {
  const dz = $("#dz"), pick = $("#fpick");
  if (!dz) return;
  const send = async (file) => {
    const kind = /pod|delivery/i.test(file.name) || file.type.startsWith("image")
      ? "pod" : file.name.endsWith(".eml") ? "email" : "log";
    const fd = new FormData(); fd.append("file", file);
    try {
      const r = await api(`/cases/${caseId}/upload?kind=${kind}`,
                          { method: "POST", body: fd });
      $("#uplist").insertAdjacentHTML("beforeend",
        `<li>${esc(file.name)} \u2192 ${esc(r.doc_id)} ${prov(r.provenance)}
         ${r.duplicate ? "(already on file \u2014 deduplicated)" : ""}</li>`);
      $("#up-note").innerHTML = `<span class="upstate">DOCUMENT RECEIVED
        \u2192 stored ${prov(r.provenance)} \u2192 awaiting RESUME \u2192
        the gate verifies before anything counts</span>`;
    } catch (e) {
      $("#uplist").insertAdjacentHTML("beforeend",
        `<li>\u2717 ${esc(file.name)}: ${esc(e.message)}</li>`);
    }
  };
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("hover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("hover"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("hover");
    [...e.dataTransfer.files].forEach(send); });
  pick.addEventListener("change", () => [...pick.files].forEach(send));
  $("#resume-btn").onclick = async () => {
    $("#resume-btn").disabled = true;
    try { await api(`/cases/${caseId}/resume`, { method: "POST" }); }
    catch (e) { $("#up-note").textContent = "Server refused: " + e.message; }
    renderCase(caseId).then(() => bindAsk(caseId));
  };
}

function exhibit(e) {
  const verdict = e.verdict === "PASS" ? "pass" : "fail";
  return `<article class="exhibit ${verdict}" id="ex-${esc(e.id)}">
    <span class="tag">[${esc(e.id.split("-").pop())}]</span>
    <span class="ekey">${esc(e.key)}</span>
    ${e.source ? prov(e.source.provenance || "") : ""}
    <span class="stamp ${verdict}">${esc(e.verdict)}</span>
    <blockquote>${esc(e.quoted_span)}</blockquote>
    <div class="src">source: ${e.source ? `${esc(e.source.id)} (${esc(e.source.type)}, ${esc(e.source.source)})` : "\u2014"}</div>
    ${e.checks.length ? `<ul class="checks">${e.checks.map((k) =>
      `<li class="${k.passed ? "ok" : "bad"}">${esc(k.name)}${k.detail && !k.passed ? " \u2014 " + esc(k.detail) : ""}</li>`).join("")}</ul>` : ""}
    ${e.fail_reason ? `<div class="fail-reason">${esc(e.fail_reason)}</div>` : ""}
  </article>`;
}

function mathPanel(c) {
  const m = c.decision_math;
  return `<div class="panel"><h3>Decision math (deterministic, versioned)</h3>
    <table class="math">
      <tr><td>Potential recovery</td><td class="rupee">${rupee(c.amount)}</td></tr>
      <tr><td>p(win) \u00b7 playbook band</td><td>${esc(m.p_win)}</td></tr>
      <tr><td>Evidence completeness</td><td>${esc(m.completeness)}</td></tr>
      <tr><td>EV(fight)</td><td class="rupee">${rupee(m.ev_fight)}</td></tr>
      <tr><td>EV(accept)</td><td class="rupee">${rupee(m.ev_accept)}</td></tr>
      <tr class="total"><td>Decision</td><td>${esc(m.action)}</td></tr></table>
    <div class="rule-line">rule: ${esc(m.rule_fired)} \u00b7 ${esc(m.playbook_version)} / ${esc(m.thresholds_version)}</div>
    ${(m.reasons || []).map((r) => `<div class="rule-line">\u2022 ${esc(r)}</div>`).join("")}</div>`;
}
function draftPanel(c) {
  let t = esc(c.draft.text)
    .replace(/\[(E\d+)\]/g, (_, id) => `<button class="cite" data-e="${id}">[${id}]</button>`)
    .replace(/\[(KB\d+)\]/g, (_, id) => `<button class="kbcite" data-kb="${id}">[${id}]</button>`);
  return `<div class="panel"><h3>Representment (citation-locked)</h3>
    <div class="draft">${t}</div><div id="kbpop"></div>
    <div class="notice">[E#] = gate-admitted exhibit \u00b7 [KB#] = verbatim-verified
      policy citation \u00b7 a deterministic validator rejected anything else</div></div>`;
}
function bindKb(c) {
  const map = (c.draft && c.draft.kb_citations) || {};
  document.querySelectorAll("button.kbcite").forEach((b) =>
    b.addEventListener("click", () => {
      const k = map[b.dataset.kb];
      $("#kbpop").innerHTML = !k ? "" : `<div class="kbpop">
        \u201c${esc(k.quote)}\u201d<br>
        <span class="src">\u2014 ${esc(k.source_id)}:${esc(k.chunk_id)}
        (${esc(k.document_version)}) ${prov("kb_local")} \u00b7 verified verbatim
        against the source chunk</span></div>`;
    }));
}
function bindCitations(c) {
  const map = (c.draft && c.draft.display_map) || {};
  document.querySelectorAll("button.cite").forEach((b) =>
    b.addEventListener("click", () => {
      const el = map[b.dataset.e] && document.getElementById(`ex-${map[b.dataset.e]}`);
      if (!el) return;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    }));
}
function execPanel(c) {
  const x = c.execution;
  return `<div class="panel"><h3>Execution</h3>
    <div class="mono">${esc(x.type)} by ${esc(x.actor)} \u00b7 ${esc(x.at)}</div>
    <div class="mono">idempotency: ${esc(x.idempotency_key)} (one money action
      per dispute, ever)</div>
    <div class="mono">${x.response.simulated ? "SIMULATED \u2014 labeled" : "real adapter"}</div></div>`;
}
function humanActions(c, caseId) {
  const allowed = c.allowed_human_actions || [];
  $("#hactions").innerHTML =
    (allowed.includes("FIGHT") ? '<button class="btn fight" data-act="FIGHT">Approve fight</button>' : "") +
    (allowed.includes("ACCEPT") ? '<button class="btn accept" data-act="ACCEPT">Accept dispute</button>' : "") +
    (allowed.includes("REJECT") ? '<button class="btn reject" data-act="REJECT">Reject / close</button>' : "");
  document.querySelectorAll("#hactions .btn").forEach((b) =>
    b.addEventListener("click", async () => {
      const actor = prompt("Reviewer name (recorded in the audit chain):");
      if (!actor) return;
      b.disabled = true;
      try {
        if (b.dataset.act === "REJECT") {
          const reason = prompt("Reason:");
          if (!reason) { b.disabled = false; return; }
          await api(`/cases/${caseId}/reject`, { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ actor, reason }) });
        } else {
          await api(`/cases/${caseId}/approve`, { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: b.dataset.act, actor }) });
        }
        renderCase(caseId).then(() => bindAsk(caseId));
      } catch (e) {
        $("#act-result").textContent = "Refused by the server: " + e.message;
        b.disabled = false;
      }
    }));
}

/* ---------- overview / queue / metrics ---------- */
async function renderOverview() {
  const [{ cases }, h] = await Promise.all([api("/cases"), api("/health")]);
  const waiting = cases.filter((c) => c.state === "needs_input");
  const open = cases.filter((c) => !["closed", "acted"].includes(c.state));
  const atRisk = open.reduce((s, c) => s + (c.amount || 0), 0);
  const resolvedFight = cases.filter((c) => c.state === "closed"
    && c.decision === "FIGHT");
  const contested = resolvedFight.reduce((s, c) => s + (c.amount || 0), 0);
  const pending = cases.filter((c) => c.escalated)
    .reduce((s, c) => s + (c.amount || 0), 0);
  const auto = cases.length ? cases.filter((c) =>
    ["closed", "acted"].includes(c.state)).length / cases.length : 0;
  main.innerHTML = `<h1>Operations</h1>
    <div class="kpis">
      <div class="kpi risk"><div class="v rupee">${rupee(atRisk)}</div>
        <div class="k">revenue at risk (open)</div></div>
      <div class="kpi good"><div class="v rupee">${rupee(contested)}</div>
        <div class="k">contested via verified evidence</div></div>
      <div class="kpi"><div class="v rupee">${rupee(pending)}</div>
        <div class="k">pending human review</div></div>
      <div class="kpi good"><div class="v">${Math.round(auto * 100)}%</div>
        <div class="k">automation rate</div></div>
      <div class="kpi ${cases.some((c) => c.urgent) ? "risk" : ""}">
        <div class="v">${cases.filter((c) => c.urgent).length}</div>
        <div class="k">deadline risk (&lt;24h)</div></div>
      <div class="kpi"><div class="v">${waiting.length}</div>
        <div class="k">needs your input</div></div>
    </div>
    <h2>Integrations</h2>
    <p class="mono">${Object.entries(h.integrations || {}).map(([k, v]) =>
      `${esc(k)}: <b>${esc(v.mode)}</b>`).join(" \u00b7 ")}</p>
    <h2>Recent</h2>${queueTable(cases.slice(0, 10))}`;
  bindRows();
  $("#rail-foot").textContent =
    `playbook ${h.playbook_version} \u00b7 clock ${h.clock_mode}`;
}
function queueTable(cases) {
  if (!cases.length) return "<p class='muted'>No cases yet \u2014 start one from New investigation.</p>";
  return `<table><thead><tr><th>case</th><th>amount</th><th>reason</th>
    <th>agent</th><th>deadline</th><th>status</th></tr></thead><tbody>
    ${cases.map((c) => `<tr class="row ${c.urgent ? "urgent" : ""}" data-id="${esc(c.case_id)}">
      <td class="mono">${esc(c.dispute_id)}</td>
      <td class="rupee">${rupee(c.amount)}</td>
      <td class="mono">${esc(c.reason_code)}</td>
      <td>${c.decision ? esc(c.decision) : c.state === "needs_input"
        ? '<span class="badge urgent">needs you</span>' : "investigating"}</td>
      <td class="mono">${esc(c.hours_left)}h</td>
      <td><span class="badge ${esc(c.state)}">${esc(c.state)}</span></td>
    </tr>`).join("")}</tbody></table>`;
}
function bindRows() {
  document.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", () => (location.hash = `#/case/${tr.dataset.id}`)));
}
async function renderQueue() {
  const { cases, total } = await api("/cases");
  main.innerHTML = `<h1>Case queue</h1><p class="sub">${total} cases, urgent
    first.</p>${queueTable(cases)}`;
  bindRows();
}
async function renderMetrics() {
  const m = await api("/metrics");
  const ev = m.evaluation, r = ev.money.recourse;
  const v2 = m.v2;
  main.innerHTML = `<h1>Held-out evaluation</h1>
    <p class="sub">40 frozen disputes, never tuned on (seed ${esc(m.config.seed)}).</p>
    <div class="stats">
      <div class="stat"><div class="v">${(ev.decision.accuracy * 100).toFixed(1)}%</div><div class="k">decision agreement</div></div>
      <div class="stat good"><div class="v">${(ev.extraction.precision * 100).toFixed(1)}%</div><div class="k">extraction precision</div></div>
      <div class="stat good"><div class="v">${(ev.deadline_compliance.rate * 100).toFixed(0)}%</div><div class="k">deadline compliance</div></div>
      <div class="stat"><div class="v">${ev.audit.chains_valid}/${ev.audit.chains_total}</div><div class="k">chains verified</div></div>
      <div class="stat"><div class="v rupee">${rupee(r.recovered)}</div><div class="k">recovered</div></div>
      <div class="stat warn"><div class="v rupee">${rupee(r.escalated_amount_pending)}</div><div class="k">pending human action</div></div></div>
    <h2>Where Recourse stops (priced coverage gaps)</h2>
    ${Object.entries(m.coverage_gaps || {}).map(([code, g]) => `<div class="panel gap">
      <h3 class="mono">${esc(code)}</h3>
      <div class="mono">${g.cases} cases \u00b7 ${rupee(g.amount_at_risk)} at risk
        \u00b7 ${rupee(g.gt_winnable_amount)} winnable</div>
      <div class="muted">${esc(g.needs)}</div></div>`).join("")}
    <p class="mono">zero wrong fights \u00b7 zero wrong accepts \u00b7 escalation
      precision (strict): ${esc(ev.automation.escalation_precision_strict)}</p>
    ${!v2 ? "" : `<h2>Eval v2 \u2014 fixed vs agentic (run twice, byte-identical)</h2>
    <div class="kpis">
      <div class="kpi good"><div class="v">${v2.headline.fixed_escalations_recovered_by_agent}</div>
        <div class="k">where the agent wins: escalations resolved</div></div>
      <div class="kpi good"><div class="v">+${v2.headline.additional_evidence_admitted}</div>
        <div class="k">additional gate-admitted exhibits</div></div>
      <div class="kpi good"><div class="v">${v2.prompt_injection.results.filter((x) => x.blocked).length}/${v2.prompt_injection.attempts}</div>
        <div class="k">prompt injections blocked</div></div>
      <div class="kpi good"><div class="v">${v2.safety_totals.unsafe_actions}</div>
        <div class="k">unsafe actions</div></div>
      <div class="kpi bad"><div class="v rupee">\u2212${rupee(Math.abs(v2.headline.net_money_delta_on_v1_labels))}</div>
        <div class="k">net on v1 labels (honest)</div></div>
      <div class="kpi"><div class="v">${v2.recoverable_gap.entered_needs_input_and_resolved}/${v2.recoverable_gap.cases}</div>
        <div class="k">recoverable gaps resolved</div></div>
    </div>
    <div class="panel honest"><h3>The negative number, explained</h3>
      <div>${esc(v2.headline.label_caveat)} Where the fixed pipeline wins:
      clean, complete cases \u2014 identical decisions at zero tool cost.
      Where the agent wins: every gap the fixed path abandoned.</div></div>`}`;
}

route();
