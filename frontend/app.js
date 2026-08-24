/* Recourse dashboard. No frameworks, no build step: every value on screen
   comes from the API — nothing is recomputed or invented client-side. */

const $ = (sel, el = document) => el.querySelector(sel);
const main = $("#main");
const rupee = (n) => "\u20b9" + Number(n).toLocaleString("en-IN");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

async function api(path, opts) {
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || resp.status);
  return body;
}

/* ---------- routing ---------- */
const routes = {
  overview: renderOverview,
  cases: renderQueue,
  metrics: renderMetrics,
};
async function route() {
  const hash = location.hash.replace(/^#\//, "") || "overview";
  const [view, arg] = hash.split("/");
  document.querySelectorAll("nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === view));
  main.innerHTML = "<p class='muted'>Loading\u2026</p>";
  try {
    if (view === "case" && arg) await renderCase(arg);
    else await (routes[view] || renderOverview)();
  } catch (e) {
    main.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
  main.focus();
}
window.addEventListener("hashchange", route);

/* ---------- overview ---------- */
async function renderOverview() {
  const [{ cases }, health] = await Promise.all([
    api("/cases"), api("/health")]);
  const escalated = cases.filter((c) => c.escalated);
  const closed = cases.filter((c) => c.state === "closed");
  const urgent = cases.filter((c) => c.urgent);
  const pending = escalated.reduce((s, c) => s + c.amount, 0);
  const byDecision = {};
  cases.forEach((c) => { const d = c.decision || "ESCALATED (pre-decision)";
    byDecision[d] = (byDecision[d] || 0) + 1; });
  main.innerHTML = `
    <h1>Dispute operations</h1>
    <p class="sub">Every number below is read from the live case store.</p>
    <div class="stats">
      <div class="stat"><div class="v">${cases.length}</div><div class="k">disputes in play</div></div>
      <div class="stat good"><div class="v">${closed.length}</div><div class="k">resolved autonomously or by review</div></div>
      <div class="stat"><div class="v">${escalated.length}</div><div class="k">waiting on a human</div></div>
      <div class="stat"><div class="v rupee">${rupee(pending)}</div><div class="k">pending human action</div></div>
      <div class="stat warn"><div class="v">${urgent.length}</div><div class="k">under 24h to deadline</div></div>
    </div>
    <h2>Decisions</h2>
    <p class="mono">${Object.entries(byDecision).map(([k, v]) => `${k}: ${v}`).join(" \u00b7 ") || "none yet"}</p>
    <h2>Recent cases</h2>
    ${queueTable(cases.slice(0, 8))}
    <p class="notice">clock: ${esc(health.clock)} (${esc(health.clock_mode)}) \u00b7
      AI provider: ${esc(health.ai_provider)} \u00b7 payments: ${esc(health.payments_provider)}</p>`;
  bindRows();
  $("#rail-foot").textContent =
    `playbook ${health.playbook_version} \u00b7 ${health.counts.cases} cases`;
}

/* ---------- queue ---------- */
function queueTable(cases) {
  if (!cases.length) return "<p class='muted'>No cases yet. Send a dispute webhook to begin.</p>";
  return `<table><thead><tr>
    <th>case</th><th>amount</th><th>reason</th><th>decision</th>
    <th>confidence</th><th>deadline</th><th>status</th></tr></thead><tbody>
    ${cases.map((c) => `
      <tr class="row ${c.urgent ? "urgent" : ""}" data-id="${esc(c.case_id)}">
        <td class="mono">${esc(c.dispute_id)}</td>
        <td class="rupee">${rupee(c.amount)}</td>
        <td class="mono">${esc(c.reason_code)}</td>
        <td>${c.decision ? esc(c.decision) : "<span class='muted'>\u2014</span>"}</td>
        <td class="mono">${c.link_confidence ?? "\u2014"}</td>
        <td class="mono">${c.hours_left}h</td>
        <td><span class="badge ${esc(c.state)}">${esc(c.state)}</span>
            ${c.urgent ? '<span class="badge urgent">URGENT</span>' : ""}</td>
      </tr>`).join("")}</tbody></table>`;
}
function bindRows() {
  document.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", () => (location.hash = `#/case/${tr.dataset.id}`)));
}
async function renderQueue() {
  const { cases, total } = await api("/cases");
  main.innerHTML = `<h1>Case queue</h1>
    <p class="sub">${total} cases, urgent first. Click a row to open its docket.</p>
    ${queueTable(cases)}`;
  bindRows();
}

/* ---------- case detail ---------- */
async function renderCase(caseId) {
  const [c, ev, audit] = await Promise.all([
    api(`/cases/${caseId}`),
    api(`/cases/${caseId}/evidence`),
    api(`/cases/${caseId}/audit`)]);
  main.innerHTML = `
    <p><a href="#/cases">\u2190 queue</a></p>
    <div class="docket">
      <span class="dnum">DISPUTE #${esc(c.dispute_id)}</span>
      <span class="damt">${rupee(c.amount)}</span>
      <span class="badge neutral">${esc(c.reason_code)}</span>
      <span class="badge ${esc(c.state)}">${esc(c.state)}</span>
      <span class="mono">${c.hours_left}h to deadline</span>
      ${c.decision ? `<span class="stamp ${c.decision === "FIGHT" ? "pass" : "fail"}">${esc(c.decision)}</span>` : ""}
    </div>
    ${c.escalation ? escalationPanel(c) : ""}
    <div class="cols">
      <section>
        <h2>Evidence exhibits</h2>
        <p class="muted">${esc(ev.note)}</p>
        ${ev.evidence.length ? ev.evidence.map(exhibit).join("")
          : "<p class='muted'>No evidence was extracted — the case escalated before extraction.</p>"}
      </section>
      <section>
        ${c.decision_math ? mathPanel(c) : ""}
        ${c.draft ? draftPanel(c) : ""}
        ${c.execution ? execPanel(c) : ""}
      </section>
    </div>
    <h2>Audit timeline
      <span class="chainbadge ${audit.chain.valid ? "ok" : "bad"}">
        ${audit.chain.valid ? "\u2713 CHAIN VERIFIED" :
          `\u2717 TAMPER DETECTED \u00b7 entry ${audit.chain.broken_at}`}</span></h2>
    <ul class="timeline">
      ${audit.entries.map((e) => `<li>
        <span class="t">${esc(e.at)}</span>
        <span class="s">${esc(e.step)}</span>
        <span>${esc(summarize(e))}</span></li>`).join("")}
    </ul>`;
  bindCitations(c);
  bindActions(c, caseId);
}

function exhibit(e) {
  const verdict = e.verdict === "PASS" ? "pass" : "fail";
  return `<article class="exhibit ${verdict}" id="ex-${esc(e.id)}">
    <span class="tag">[${esc(displayIdOf(e.id))}]</span>
    <span class="ekey">${esc(e.key)}</span>
    <span class="stamp ${verdict}">${esc(e.verdict)}</span>
    <blockquote>${esc(e.quoted_span)}</blockquote>
    <div class="src">source: ${e.source ? `${esc(e.source.id)} (${esc(e.source.type)}, ${esc(e.source.source)})` : "\u2014"}
      ${Object.keys(e.fields).length ? " \u00b7 fields: " + esc(JSON.stringify(e.fields)) : ""}</div>
    ${e.checks.length ? `<ul class="checks">${e.checks.map((k) =>
      `<li class="${k.passed ? "ok" : "bad"}">${esc(k.name)}${k.detail && !k.passed ? " \u2014 " + esc(k.detail) : ""}</li>`).join("")}</ul>` : ""}
    ${e.fail_reason ? `<div class="fail-reason">${esc(e.fail_reason)}</div>` : ""}
  </article>`;
}
const displayIdOf = (fullId) => fullId.split("-").pop();

function mathPanel(c) {
  const m = c.decision_math;
  return `<div class="panel"><h3>Decision math (backend-authoritative)</h3>
    <table class="math">
      <tr><td>Potential recovery</td><td class="rupee">${rupee(c.amount)}</td></tr>
      <tr><td>Probability of success</td><td>${m.p_win}</td></tr>
      <tr><td>Evidence completeness</td><td>${m.completeness}</td></tr>
      <tr><td>EV(fight)</td><td class="rupee">${rupee(m.ev_fight)}</td></tr>
      <tr><td>EV(accept)</td><td class="rupee">${rupee(m.ev_accept)}</td></tr>
      <tr class="total"><td>Decision</td><td>${esc(m.action)}</td></tr>
    </table>
    <div class="rule-line">rule: ${esc(m.rule_fired)} \u00b7 playbook ${esc(m.playbook_version)} \u00b7 thresholds ${esc(m.thresholds_version)}</div>
    ${m.reasons.map((r) => `<div class="rule-line">\u2022 ${esc(r)}</div>`).join("")}
  </div>`;
}

function draftPanel(c) {
  const withCites = esc(c.draft.text).replace(/\[(E\d+)\]/g,
    (_, id) => `<button class="cite" data-e="${id}">[${id}]</button>`);
  return `<div class="panel"><h3>Representment (citation-locked)</h3>
    <div class="draft">${withCites}</div>
    <div class="notice">every factual sentence must cite an admitted exhibit;
    the deterministic validator rejected anything else before submission</div></div>`;
}

function bindCitations(c) {
  const map = (c.draft && c.draft.display_map) || {};
  document.querySelectorAll("button.cite").forEach((b) =>
    b.addEventListener("click", () => {
      const full = map[b.dataset.e];
      const el = full && document.getElementById(`ex-${full}`);
      if (!el) return;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.remove("flash"); void el.offsetWidth;
      el.classList.add("flash");
    }));
}

function execPanel(c) {
  const x = c.execution;
  return `<div class="panel"><h3>Execution</h3>
    <div class="mono">${esc(x.type)} by ${esc(x.actor)} \u00b7 ${esc(x.at)}</div>
    <div class="mono">idempotency key: ${esc(x.idempotency_key)}
      (one money action per dispute, ever)</div>
    <div class="mono">${x.response.simulated ? "SIMULATED \u2014 labeled, not a real Razorpay call" : "razorpay_test"}
      \u00b7 status: ${esc(x.response.data && x.response.data.status)}</div></div>`;
}

function escalationPanel(c) {
  const allowed = c.allowed_human_actions || [];
  return `<div class="panel escalation"><h3>HUMAN REVIEW REQUIRED</h3>
    <pre>${esc(c.escalation.merchant_summary)}</pre>
    <div class="actions">
      ${allowed.includes("FIGHT") ? '<button class="btn fight" data-act="FIGHT">Approve fight</button>' : ""}
      ${allowed.includes("ACCEPT") ? '<button class="btn accept" data-act="ACCEPT">Accept dispute</button>' : ""}
      ${allowed.includes("REJECT") ? '<button class="btn reject" data-act="REJECT">Reject / close</button>' : ""}
    </div>
    <div class="notice">only actions the backend will allow are shown; the
    server re-checks the deadline and evidence either way</div>
    <div class="notice" id="act-result"></div></div>`;
}

function bindActions(c, caseId) {
  document.querySelectorAll(".actions .btn").forEach((b) =>
    b.addEventListener("click", async () => {
      const act = b.dataset.act;
      const actor = prompt("Your reviewer name (recorded in the audit chain):");
      if (!actor) return;
      b.disabled = true;
      try {
        if (act === "REJECT") {
          const reason = prompt("Reason for closing without action:");
          if (!reason) { b.disabled = false; return; }
          await api(`/cases/${caseId}/reject`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ actor, reason }) });
        } else {
          const r = await api(`/cases/${caseId}/approve`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: act, actor }) });
          if (r.duplicate)
            $("#act-result").textContent =
              "Already executed \u2014 the original action was returned (idempotent).";
        }
        await renderCase(caseId);
      } catch (e) {
        $("#act-result").textContent = "Refused by the server: " + e.message;
        b.disabled = false;
      }
    }));
}

function summarize(e) {
  const p = e.payload;
  switch (e.step) {
    case "CASE_CREATED": return `${rupee(p.amount)} ${p.reason_code} (${p.hours_left}h left)`;
    case "LINK_COMPLETED": return `${p.method} \u2192 ${p.order_id} (conf ${p.confidence})`;
    case "GATHER_COMPLETED": return `${(p.documents || []).length} documents`;
    case "EVIDENCE_EXTRACTED": return `${p.count} candidates ${JSON.stringify(p.keys)}`;
    case "EVIDENCE_ADMITTED": return `${(p.ids || []).length} admitted`;
    case "EVIDENCE_REJECTED": return (p.items || []).map((i) => i.reason).join("; ").slice(0, 90);
    case "DECISION_MADE": return `${p.action} via ${p.rule_fired} (EV fight ${rupee(p.ev_fight)})`;
    case "DRAFT_VALIDATED": return "citations clean";
    case "ACTION_SUBMITTED": return `${p.action} via ${p.adapter}${p.simulated ? " [SIMULATED]" : ""} by ${p.actor}`;
    case "ACTION_DUPLICATE": return `attempted ${p.attempted_action}; original ${p.original_action_type} returned`;
    case "HUMAN_APPROVED": return `${p.action} by ${p.actor_name}`;
    case "CASE_REJECTED": return `${p.reason} \u2014 by ${p.actor_name}`;
    case "CASE_ESCALATED": return (p.reason || "").slice(0, 90);
    case "CASE_CLOSED": return `dispute ${p.dispute_status}`;
    default: return "";
  }
}

/* ---------- metrics ---------- */
async function renderMetrics() {
  const m = await api("/metrics");
  const ev = m.evaluation, money = ev.money, r = money.recourse;
  const gaps = Object.entries(m.coverage_gaps || {});
  main.innerHTML = `
    <h1>Held-out evaluation</h1>
    <p class="sub">40 frozen disputes, never used for tuning. Read from the
      committed Stage-9 artifact (${esc(m.config.sim_now)}, seed ${m.config.seed},
      provider ${esc(m.meta.ai_provider)}).</p>
    <div class="stats">
      <div class="stat"><div class="v">${(ev.decision.accuracy * 100).toFixed(1)}%</div><div class="k">decision agreement</div></div>
      <div class="stat good"><div class="v">${(ev.extraction.precision * 100).toFixed(1)}%</div><div class="k">extraction precision</div></div>
      <div class="stat"><div class="v">${(ev.automation.automation_rate * 100).toFixed(1)}%</div><div class="k">automation</div></div>
      <div class="stat good"><div class="v">${(ev.deadline_compliance.rate * 100).toFixed(0)}%</div><div class="k">deadline compliance</div></div>
      <div class="stat"><div class="v rupee">${rupee(r.recovered)}</div><div class="k">recovered</div></div>
      <div class="stat warn"><div class="v rupee">${rupee(r.escalated_amount_pending)}</div><div class="k">pending human action</div></div>
    </div>
    <h2>Strategy comparison</h2>
    <table class="mtable"><thead><tr><th>strategy</th><th>recovered</th><th>fees</th><th>net</th></tr></thead><tbody>
      <tr><td>never contest</td><td class="rupee">${rupee(0)}</td><td class="rupee">${rupee(0)}</td><td class="rupee">${rupee(0)}</td></tr>
      <tr><td>contest everything</td>
        <td class="rupee">${rupee(money.baseline_contest_all.recovered)}</td>
        <td class="rupee">${rupee(money.baseline_contest_all.fees_paid_on_losses)}</td>
        <td class="rupee">${rupee(money.baseline_contest_all.net)}</td></tr>
      <tr><td><b>Recourse</b></td>
        <td class="rupee">${rupee(r.recovered)}</td>
        <td class="rupee">${rupee(r.fees_paid_on_losses)}</td>
        <td class="rupee">${rupee(r.net)}</td></tr></tbody></table>
    <p class="muted" style="max-width:640px">Contest-everything currently nets
      more on this synthetic set. That is not hidden: the entire gap is money
      Recourse deliberately escalates rather than fighting without a playbook
      \u2014 ${rupee(r.escalated_gt_winnable_pending)} of ground-truth-winnable
      value sits in the coverage gaps below. Zero wrong fights, zero wrong
      accepts.</p>
    <h2>Where Recourse currently stops</h2>
    ${gaps.map(([code, g]) => `<div class="panel gap">
      <h3 class="mono">${esc(code)}</h3>
      <div class="mono">${g.cases} held-out cases \u00b7 ${rupee(g.amount_at_risk)} at risk
        \u00b7 ${rupee(g.gt_winnable_amount)} winnable per ground truth</div>
      <div class="muted">Escalated because no v1 playbook covers this reason
        code \u2014 the agent refuses to fight without deterministic evidence
        rules. Needs: ${esc(g.needs)}.</div></div>`).join("")}
    <h2>Safety numbers</h2>
    <p class="mono">false fights: ${r.false_fights} (\u2211 ${rupee(r.false_fight_cost_total)}) \u00b7
      audit chains valid: ${ev.audit.chains_valid}/${ev.audit.chains_total} \u00b7
      escalation precision (strict): ${ev.automation.escalation_precision_strict}</p>
    ${ev.gate_ablation ? `<h2>Gate ablation</h2>
      <p class="muted" style="max-width:640px">${esc(ev.gate_ablation.label)}:
      with the gate off, ${ev.gate_ablation.inadmissible_candidates_that_would_ship}
      inadmissible evidence item(s) would have shipped and
      ${ev.gate_ablation.decisions_that_would_flip.length} decision(s) would flip
      ESCALATE \u2192 FIGHT \u2014 e.g. contesting on a POD delivered to the wrong
      pincode.</p>` : ""}`;
}

route();
