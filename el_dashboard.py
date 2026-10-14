r"""
The operations screen: one page that is both the checkout that triggers a
held payment and the bank's view of what the agent does about it.

Served by el_tools at /, /checkout and /ui, and published to Firebase Hosting
by build_web.py. It polls /api/dashboard because the freeze the agent performs
on the call has to appear here within a breath of being spoken -- that
simultaneity is the thing being demonstrated.

API calls go to window.NOBA_API_BASE when set (the Firebase build sets it in
config.js) and to the page's own host otherwise. The ElevenLabs browser client
is loaded only when someone answers the call, so nothing on the page waits on
the CDN.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__BANK_NAME__ Fraud Operations</title>
<style>
  :root {
    --bg:#0b1014; --panel:#141b21; --line:#223039; --ink:#e8eef2;
    --muted:#8da2b0; --green:#2ea06a; --amber:#d9a21b; --red:#d6454a; --accent:#3d8bfd;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:18px 22px; border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  .brand { font-weight:700; font-size:18px; }
  .brand span { color:var(--accent); }
  .spacer { flex:1; }
  .pill { font-size:12px; padding:4px 10px; border-radius:999px;
    border:1px solid var(--line); color:var(--muted); }
  .wrap { padding:22px 16px; display:grid; gap:18px; max-width:1100px; margin:0 auto; }
  .grid2 { display:grid; gap:18px; grid-template-columns:1fr 1fr; }
  @media (max-width:820px) { .grid2 { grid-template-columns:1fr; } }
  .card { background:var(--panel); border:1px solid var(--line);
    border-radius:12px; padding:16px 18px; min-width:0; }
  h2 { margin:0 0 12px; font-size:13px; text-transform:uppercase;
    letter-spacing:.09em; color:var(--muted); font-weight:600; }
  .row { display:flex; justify-content:space-between; gap:12px; padding:6px 0; }
  .row + .row { border-top:1px solid rgba(255,255,255,.04); }
  .k { color:var(--muted); }
  .v { font-variant-numeric:tabular-nums; text-align:right; overflow-wrap:anywhere; }
  .big { font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }
  .status { display:inline-flex; align-items:center; gap:7px; font-weight:600; }
  .dot { width:9px; height:9px; border-radius:50%; }
  .ok { color:var(--green); } .ok .dot { background:var(--green); }
  .warn { color:var(--amber); } .warn .dot { background:var(--amber); }
  .bad { color:var(--red); } .bad .dot { background:var(--red); }
  .pan { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.14em; }
  .tbl { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); }
  th { font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); }
  td.amt { text-align:right; font-variant-numeric:tabular-nums; }
  tr.flagged { background:rgba(214,69,74,.09); }
  tr.flagged td:first-child { box-shadow:inset 3px 0 0 var(--red); }
  .tag { font-size:11px; padding:2px 7px; border-radius:5px;
    border:1px solid currentColor; white-space:nowrap; }
  ul.log { list-style:none; margin:0; padding:0; }
  ul.log li { padding:7px 0 7px 16px; position:relative;
    border-bottom:1px solid rgba(255,255,255,.04); }
  ul.log li::before { content:""; position:absolute; left:0; top:15px;
    width:6px; height:6px; border-radius:50%; background:var(--accent); }
  .empty { color:var(--muted); font-style:italic; }
  .sev { font-size:11px; font-weight:700; letter-spacing:.06em; }
  .flash { animation:flash 1.1s ease-out; }
  @keyframes flash { from { background:rgba(214,69,74,.42); } to { background:transparent; } }
  .pay { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
  .pay label { display:flex; flex-direction:column; gap:5px; font-size:12px;
    color:var(--muted); text-transform:uppercase; letter-spacing:.06em; }
  .pay input, .pay select {
    background:#0d1317; color:var(--ink); border:1px solid var(--line);
    border-radius:7px; padding:9px 11px; font-size:15px; min-width:120px;
    font-family:inherit; max-width:100%;
  }
  .pay button, .call button {
    background:var(--accent); color:#fff; border:0; border-radius:7px;
    padding:11px 20px; font-size:15px; font-weight:600; cursor:pointer; font-family:inherit;
  }
  .pay button:hover, .call button:hover { filter:brightness(1.1); }
  .pay button:disabled, .call button:disabled { opacity:.55; cursor:default; }
  .pay button.ghost, .call button.ghost { background:transparent; color:var(--muted);
    border:1px solid var(--line); font-weight:500; }
  .pay button.ghost:hover { color:var(--ink); }
  .payout { margin-top:14px; font-size:14px; }
  .payout.held { color:var(--amber); }
  .payout.okd { color:var(--green); }
  .payout.err { color:var(--red); }
  .why { font-size:12px; color:var(--muted); }
  .offline { border:1px solid var(--amber); color:var(--amber); border-radius:12px; padding:12px 16px; }

  /* The cardholder's side of the call, in the same panel style. */
  .call { border-color:var(--accent); }
  .ringing { display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  .ringing .ph { width:42px; height:42px; border-radius:50%; background:var(--accent);
    display:grid; place-items:center; font-size:19px; animation:ring 1.1s infinite; }
  @keyframes ring { 0%,100% { transform:rotate(0); } 10%,30% { transform:rotate(-14deg); }
    20%,40% { transform:rotate(14deg); } 50% { transform:rotate(0); } }
  .ringing .t { flex:1; min-width:200px; }
  .ringing .t b { display:block; }
  .call .answer { background:var(--green); }
  .call .hang { background:var(--red); }
  .eq { display:inline-flex; gap:3px; align-items:flex-end; height:16px; vertical-align:middle; margin-right:8px; }
  .eq b { width:3px; height:4px; background:var(--accent); border-radius:2px; }
  .eq.talk b { animation:eq .7s infinite ease-in-out; }
  .eq.talk b:nth-child(2) { animation-delay:.1s; } .eq.talk b:nth-child(3) { animation-delay:.2s; }
  .eq.talk b:nth-child(4) { animation-delay:.3s; }
  @keyframes eq { 50% { height:16px; } }
  .caps { max-height:240px; overflow:auto; display:grid; gap:7px; margin:12px 0; }
  .cap { padding:7px 10px; border-radius:8px; background:#0d1317; font-size:14px; max-width:88%; }
  .cap.ai { border-left:3px solid var(--accent); justify-self:start; }
  .cap.user { border-left:3px solid var(--green); justify-self:end; }
  .actions { display:flex; gap:8px; justify-content:flex-end; }

  .chips { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }
  .chip { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid currentColor; color:var(--muted); }
  .chip.ok { color:var(--green); } .chip.bad { color:var(--red); }
  .audit { border-top:1px solid var(--line); padding:12px 0; }
  .audit:first-child { border-top:0; padding-top:0; }
  .audit .h b { font-family:ui-monospace,Menlo,monospace; font-size:12px; }
  .audit audio { width:100%; margin-top:8px; height:34px; }
  .audit button { background:transparent; color:var(--accent); border:0; cursor:pointer; padding:0; font:inherit; font-size:13px; }
  .tx { margin-top:8px; display:grid; gap:4px; font-size:13px; }
  .tx div { padding:5px 9px; border-radius:7px; background:#0d1317; }
  .tx div.agent { border-left:3px solid var(--accent); }
  .tx div.user { border-left:3px solid var(--green); }
  [hidden] { display:none !important; }
</style>
</head>
<body>
<header>
  <div class="brand">__BANK_BRAND__ &middot; Fraud Operations</div>
  <div class="spacer"></div>
  <div class="pill" id="clock">&mdash;</div>
  <div class="pill" id="poll">live</div>
</header>

<div class="wrap">
  <div class="offline" id="offline" hidden>
    The demo bank server is offline right now, so payments can't be checked. Please try again later.
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Cardholder</h2>
      <div class="row"><span class="k">Name</span><span class="v" id="c-name">&mdash;</span></div>
      <div class="row"><span class="k">Customer ID</span><span class="v" id="c-id">&mdash;</span></div>
      <div class="row"><span class="k">Phone</span><span class="v" id="c-phone">&mdash;</span></div>
      <div class="row"><span class="k">City</span><span class="v" id="c-city">&mdash;</span></div>
      <div class="row"><span class="k">Account</span><span class="v" id="a-no">&mdash;</span></div>
      <div class="row"><span class="k">Available balance</span><span class="v big" id="a-bal">&mdash;</span></div>
    </div>
    <div class="card">
      <h2>Card</h2>
      <div class="row"><span class="k">Number</span><span class="v pan" id="k-pan">&mdash;</span></div>
      <div class="row"><span class="k">Brand</span><span class="v" id="k-brand">&mdash;</span></div>
      <div class="row"><span class="k">Status</span><span class="v" id="k-status">&mdash;</span></div>
      <div class="row"><span class="k">Daily limit</span><span class="v" id="k-limit">&mdash;</span></div>
      <div class="row"><span class="k">Channels</span><span class="v" id="k-chan">&mdash;</span></div>
    </div>
  </div>

  <div class="card">
    <h2>Checkout</h2>
    <div class="pay">
      <label>Card ending<input id="f-last4" value="4081" maxlength="4"></label>
      <label>Amount<input id="f-amount" value="250.00"></label>
      <label>Currency
        <select id="f-currency">
          <option value="USD" selected>USD</option>
          <option value="NGN">NGN</option>
        </select>
      </label>
      <label>Merchant<input id="f-merchant" value="Kyiv Electronics"></label>
      <label>Country
        <select id="f-country">
          <option value="UA" selected>UA &mdash; Ukraine</option>
          <option value="AE">AE &mdash; United Arab Emirates</option>
          <option value="NG">NG &mdash; Nigeria</option>
          <option value="US">US &mdash; United States</option>
        </select>
      </label>
      <label>Call language
        <select id="f-lang">
          <option value="en" selected>English</option>
          <option value="ar">Arabic &mdash; &#1575;&#1604;&#1593;&#1585;&#1576;&#1610;&#1577;</option>
        </select>
      </label>
      <label>Reach cardholder by
        <select id="f-contact">
          <option value="browser" selected>This browser</option>
          <option value="phone">Phone call</option>
        </select>
      </label>
      <label>Call this number<input id="f-dest" placeholder="approved numbers only"></label>
      <button id="f-send">Send payment</button>
      <button id="f-reset" class="ghost" title="Clear the demo back to a quiet account">Reset demo</button>
    </div>
    <div id="pay-out" class="payout"></div>
  </div>

  <div class="card call" id="call" hidden>
    <h2>Call from __BANK_NAME__</h2>
    <div class="ringing" id="ringing">
      <div class="ph">&#128222;</div>
      <div class="t"><b>__AGENT_NAME__, the bank's AI assistant, is calling about this payment</b>
        <span class="why" id="ring-sub">Answer to confirm it was you. Allow the microphone when asked.</span></div>
      <button class="answer" id="answer">Answer</button>
    </div>
    <div id="incall" hidden>
      <div class="k"><span class="eq" id="eq"><b></b><b></b><b></b><b></b></span><span id="call-state">Connected</span></div>
      <div class="caps" id="caps" aria-live="polite"></div>
      <div class="actions">
        <button class="ghost" id="mute">Mute</button>
        <button class="hang" id="hang">End call</button>
      </div>
    </div>
    <div class="why" style="margin-top:10px">The bank will never ask for your PIN, password or a one-time code.</div>
  </div>

  <div class="card">
    <h2>Pending authorisations</h2>
    <div class="tbl"><table>
      <thead><tr><th>Reference</th><th>Merchant</th><th>Amount</th><th>Status</th><th>Why</th></tr></thead>
      <tbody id="pend"><tr><td colspan="5" class="empty">Nothing held.</td></tr></tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>Risk signal</h2>
    <div id="sig" class="empty">No signal on this account.</div>
  </div>

  <div class="card">
    <h2>Recent transactions</h2>
    <div class="tbl"><table>
      <thead><tr><th>Reference</th><th>Narration</th><th>Channel</th><th>When</th><th class="amt">Amount</th></tr></thead>
      <tbody id="trx"><tr><td colspan="5" class="empty">Loading&hellip;</td></tr></tbody>
    </table></div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Agent activity</h2>
      <div id="live" class="empty">No call in progress.</div>
    </div>
    <div class="card">
      <h2>Fraud cases</h2>
      <ul class="log" id="cases"></ul>
      <div id="cases-empty" class="empty">No cases opened.</div>
    </div>
  </div>

  <div class="card">
    <h2>Audit trail &middot; recorded &amp; evaluated calls</h2>
    <div id="audit" class="empty">No recorded calls yet. Each call appears here once ElevenLabs has filed its recording.</div>
  </div>
</div>

<script>
// Empty when the backend serves this page; the Firebase build sets it in config.js.
const API = (window.NOBA_API_BASE || "").replace(/\/$/, "");
const SDK = "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.25.0/+esm";
// The demo cards, so typing a card number shows that cardholder's account.
const CARD_OWNER = { "4081":"CUS-100001", "9032":"CUS-100002", "6677":"CUS-100003", "2322":"CUS-100004", "8562":"CUS-100005" };
let CUSTOMER = "__CUSTOMER_ID__";
const $ = id => document.getElementById(id);
const naira = n => "₦" + Number(n || 0).toLocaleString("en-NG", {minimumFractionDigits: 2});
const txt = (id, v) => { $(id).textContent = (v === undefined || v === null) ? "—" : v; };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let lastCardStatus = null, failures = 0, busy = false, timer = null;
let sessionId = null, convo = null, muted = false, sdkPromise = null;

function cardStatus(s) {
  const cls = s === "ACTIVE" ? "ok" : (s === "BLOCKED" || s === "FROZEN") ? "bad" : "warn";
  const label = s === "BLOCKED" ? "FROZEN BY AGENT" : s;
  return '<span class="status ' + cls + '"><span class="dot"></span>' + label + '</span>';
}

async function tick() {
  let d;
  try {
    const r = await fetch(API + "/api/dashboard/" + CUSTOMER, {cache: "no-store"});
    // A tunnel 504 returns an empty body; read as text so the real cause shows.
    const body = await r.text();
    if (!r.ok || !body) throw new Error("HTTP " + r.status);
    d = JSON.parse(body);
    failures = 0;
    $("poll").textContent = "live";
    $("offline").hidden = true;
  } catch (e) {
    failures += 1;
    $("poll").textContent = failures > 2 ? "offline · retrying" : "reconnecting…";
    if (failures > 1) $("offline").hidden = false;
    schedule();
    return;
  }
  $("clock").textContent = new Date().toLocaleTimeString("en-NG");

  txt("c-name", d.customer.name); txt("c-id", d.customer.id);
  txt("c-phone", d.customer.phone); txt("c-city", d.customer.city);
  if (d.account) {
    txt("a-no", d.account.accountNumber);
    txt("a-bal", naira(d.account.availableBalance));
  }

  const k = (d.cards || [])[0];
  if (k) {
    txt("k-pan", k.maskedPan);
    txt("k-brand", (k.brand || "").toUpperCase());
    const el = $("k-status");
    el.innerHTML = cardStatus(k.status);
    // Flash the moment the agent's freeze lands, so the change is not missed.
    if (lastCardStatus !== null && lastCardStatus !== k.status) {
      el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    }
    lastCardStatus = k.status;
    txt("k-limit", naira(k.dailyLimit));
    txt("k-chan", (k.channelsEnabled || []).join(" · "));
  }

  const s = (d.signals || [])[0];
  const sig = $("sig");
  if (s) {
    sig.className = "";
    sig.innerHTML =
      '<div class="row"><span class="sev bad">' + esc(s.severity) + " · " +
      esc(s.riskType.replace(/_/g, " ")) + '</span><span class="v tag warn">' +
      esc(s.recommendedAction.replace(/_/g, " ")) + "</span></div>" +
      '<p style="margin:10px 0 0">' + esc(s.summary) + "</p>";
  } else {
    sig.className = "empty";
    sig.textContent = "No signal on this account.";
  }

  const rows = (d.transactions || []).map(function (t) {
    const foreign = t.countryCode && t.countryCode !== "NG";
    const flag = foreign ? "🌐 " : "🇳🇬 ";
    return '<tr class="' + (t.flagged ? "flagged" : "") + '"><td>' + esc(t.referenceId) +
      "</td><td>" + flag + esc(t.narration) + "</td><td>" + esc(t.channel) + "</td><td>" +
      new Date(t.bookDate).toLocaleTimeString("en-NG") + '</td><td class="amt">' +
      (t.debitOrCredit === "DEBIT" ? "-" : "+") + naira(t.amount) + "</td></tr>";
  });
  $("trx").innerHTML = rows.length ? rows.join("") : '<tr><td colspan="5" class="empty">No transactions.</td></tr>';

  // The banner is rendered from polled state, never from what the click
  // returned: a payment since declined or approved must stop claiming it is held.
  const held = (d.pending || []).filter(p => p.pending);
  const live = (d.live_calls || [])[0];
  const out = $("pay-out");
  if (held.length) {
    const h = held[0];
    const calling = !live ? "Waiting for the bank to reach the cardholder."
      : live.call_state === "awaiting_browser" ? "The bank is calling — answer below."
      : live.call_state === "ringing" ? "Ringing the cardholder's phone…"
      : live.call_state === "failed" ? "The call could not be placed."
      : "The agent is on the call with the cardholder now.";
    out.className = "payout held";
    out.innerHTML = "<strong>Held for verification</strong> · " + esc(h.referenceId) +
      " · " + esc(h.severity || "") + "<br>" + calling +
      '<div class="why">' + esc((h.reasons || []).join("; ")) + "</div>";
  } else if (out.className === "payout held") {
    // Resolved: say how, once, from the latest authorisation.
    const last = (d.pending || [])[0];
    if (last && last.status === "DECLINED") {
      out.className = "payout err";
      out.textContent = "Declined · " + last.referenceId + " — the card was frozen and the payment stopped.";
    } else if (last && last.status === "APPROVED") {
      out.className = "payout okd";
      out.textContent = "Approved · " + last.referenceId + " — the cardholder confirmed it.";
    } else { out.className = "payout"; out.textContent = ""; }
    if (convo) setTimeout(endCall, 6000);  // let the agent finish its sentence
  }

  const pend = (d.pending || []).map(function (p) {
    const cls = p.status === "DECLINED" ? "bad" : p.status === "APPROVED" ? "ok" : "warn";
    const money = p.currency === "NGN" ? naira(p.amount)
      : esc(p.amount) + " " + esc(p.currency) + " (" + naira(p.naira_amount) + ")";
    return '<tr class="' + (p.pending ? "flagged" : "") + '"><td>' + esc(p.referenceId) +
      "</td><td>" + esc(p.narration) + '</td><td class="amt">' + money +
      '</td><td><span class="status ' + cls + '"><span class="dot"></span>' +
      esc(p.stage || p.status) + '</span></td><td class="why">' +
      esc((p.reasons || []).join("; ") || p.resolution || "") + "</td></tr>";
  });
  $("pend").innerHTML = pend.length ? pend.join("") : '<tr><td colspan="5" class="empty">Nothing held.</td></tr>';

  const lv = $("live");
  if (live) {
    lv.className = "";
    const acts = (live.actions_taken || []).map(a => "<li>" + esc(a) + "</li>").join("")
      || '<li class="empty">No actions yet.</li>';
    lv.innerHTML =
      '<div class="row"><span class="k">Call</span><span class="v">' +
      esc(live.call_state.replace(/_/g, " ")) + " · " + esc(live.channel) + " · " +
      (live.language === "ar" ? "Arabic" : "English") + "</span></div>" +
      '<div class="row"><span class="k">Identity verified</span><span class="v">' +
      (live.verified ? '<span class="status ok"><span class="dot"></span>YES</span>'
                     : '<span class="status warn"><span class="dot"></span>NOT YET</span>') + "</span></div>" +
      '<div class="row"><span class="k">Attempts remaining</span><span class="v">' + live.attempts_remaining + "</span></div>" +
      '<div class="row"><span class="k">Handed to human</span><span class="v">' + (live.handed_to_human ? "YES" : "no") + "</span></div>" +
      '<ul class="log">' + acts + "</ul>";
    // The browser is the cardholder's phone: show the ringing call here.
    if (live.call_state === "awaiting_browser" && live.session_id) {
      sessionId = live.session_id;
      if (!convo) { $("call").hidden = false; $("ringing").hidden = false; $("incall").hidden = true; loadSdk().catch(() => {}); }
    }
  } else {
    lv.className = "empty";
    lv.textContent = "No call in progress.";
    if (!convo) $("call").hidden = true;
  }

  const cs = d.cases || [];
  $("cases").innerHTML = cs.map(c => "<li><strong>" + esc(c.caseId) + "</strong> &middot; " +
    esc(c.status.replace(/_/g, " ")) + '<br><span class="k">' + esc(c.summary) + "</span></li>").join("");
  $("cases-empty").style.display = cs.length ? "none" : "block";

  busy = held.length > 0 || !!live;
  schedule();
}

// ---- Checkout ----------------------------------------------------------

$("f-last4").addEventListener("change", e => {
  const owner = CARD_OWNER[e.target.value.trim()];
  if (owner && owner !== CUSTOMER) { CUSTOMER = owner; lastCardStatus = null; tick(); }
});

$("f-send").addEventListener("click", async function () {
  const btn = this, out = $("pay-out");
  const owner = CARD_OWNER[$("f-last4").value.trim()];
  if (owner) CUSTOMER = owner;
  btn.disabled = true; btn.textContent = "Authorising…";
  out.className = "payout"; out.textContent = "Asking the bank to authorise…";
  try {
    const r = await fetch(API + "/api/payments/attempt", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        card_last4: $("f-last4").value.trim(),
        amount: $("f-amount").value.trim(),
        currency: $("f-currency").value,
        merchant: $("f-merchant").value.trim(),
        country_code: $("f-country").value,
        language: $("f-lang").value,
        contact: $("f-contact").value,
        destination: $("f-dest").value.trim() || null
      })
    });
    const raw = await r.text();
    const j = raw ? JSON.parse(raw) : {};
    if (!r.ok) {
      out.className = "payout err";
      out.textContent = "Rejected: " + (j.detail || ("HTTP " + r.status));
    } else if (j.status === "APPROVED") {
      out.className = "payout okd";
      out.textContent = "Approved · " + j.reference + " — nothing suspicious.";
    } else if (j.status === "DECLINED") {
      out.className = "payout err";
      out.textContent = "Declined · " + (j.reason || "");
    } else {
      // Held: tick() renders the banner and the call from server state.
      sessionId = j.session_id;
      out.className = "payout held"; out.textContent = "Held for verification · " + j.reference;
    }
  } catch (e) {
    out.className = "payout err";
    out.textContent = "Could not reach the bank: " + e.message;
  }
  btn.disabled = false; btn.textContent = "Send payment";
  tick();
});

$("f-reset").addEventListener("click", async function () {
  await endCall();
  await fetch(API + "/api/demo/reset", {method: "POST"});
  $("pay-out").textContent = ""; $("pay-out").className = "payout";
  $("call").hidden = true; lastCardStatus = null;
  tick(); loadAudit();
});

// ---- The call, answered in the browser ---------------------------------

const loadSdk = () => (sdkPromise ||= import(SDK));

$("answer").addEventListener("click", async () => {
  $("answer").disabled = true; $("ring-sub").textContent = "Connecting…";
  try {
    await navigator.mediaDevices.getUserMedia({ audio: true });
    const { Conversation } = await loadSdk();
    const w = await (await fetch(API + "/api/web-session/" + sessionId)).json();
    if (!w.signed_url) throw new Error(w.detail || "the bank could not open the call");
    $("caps").innerHTML = "";
    convo = await Conversation.startSession({
      signedUrl: w.signed_url,
      dynamicVariables: w.dynamic_variables,
      overrides: { agent: { language: w.language } },
      onConnect: () => {
        $("ringing").hidden = true; $("incall").hidden = false;
        $("call-state").textContent = "Connected to __AGENT_NAME__";
        const id = convo && convo.getId && convo.getId();
        fetch(API + "/api/web-session/" + sessionId + "/started", { method: "POST",
          headers: {"Content-Type": "application/json"}, body: JSON.stringify({ conversation_id: id }) });
      },
      onMessage: m => caption(m.source || m.role, m.message),
      onModeChange: m => $("eq").classList.toggle("talk", (m.mode || m) === "speaking"),
      onDisconnect: () => { $("call-state").textContent = "Call ended"; $("eq").classList.remove("talk"); convo = null; setTimeout(loadAudit, 20000); },
      onError: e => { $("call-state").textContent = "Call problem: " + (e.message || e); },
    });
  } catch (e) {
    $("ring-sub").textContent = "Couldn't connect: " + e.message + ". Allow the microphone and try again.";
  }
  $("answer").disabled = false;
});

function caption(who, text) {
  if (!text) return;
  const div = document.createElement("div");
  div.className = "cap " + (who === "user" ? "user" : "ai");
  div.dir = "auto"; div.textContent = text;
  $("caps").appendChild(div); $("caps").scrollTop = $("caps").scrollHeight;
}
async function endCall() { if (convo) { try { await convo.endSession(); } catch {} convo = null; } }
$("hang").addEventListener("click", endCall);
$("mute").addEventListener("click", () => {
  if (!convo) return; muted = !muted; convo.setMicMuted(muted); $("mute").textContent = muted ? "Unmute" : "Mute";
});

// ---- Audit trail -------------------------------------------------------

async function loadAudit() {
  let rows;
  try { rows = await (await fetch(API + "/api/audit", {cache: "no-store"})).json(); } catch { return; }
  if (!rows.length) return;
  $("audit").className = "";
  $("audit").innerHTML = rows.map(a => {
    const ev = Object.entries(a.evaluation || {}).map(([k, v]) =>
      '<span class="chip ' + (v === "success" ? "ok" : v === "failure" ? "bad" : "") + '">' + esc(k.replace(/_/g, " ")) + "</span>").join("");
    const dc = Object.entries(a.data || {}).filter(([, v]) => v !== null && v !== "").map(([k, v]) =>
      '<span class="chip">' + esc(k.replace(/_/g, " ")) + ": " + esc(v) + "</span>").join("");
    return '<div class="audit"><div class="h"><b>' + esc(a.conversation_id) + "</b> " +
      '<span class="k">' + esc(a.customer || "") + " · " + new Date(a.received_at).toLocaleString() +
      (a.case_id ? " · " + esc(a.case_id) : "") + "</span></div>" +
      (a.summary ? '<div style="margin-top:4px">' + esc(a.summary) + "</div>" : "") +
      '<div class="chips">' + ev + '</div><div class="chips">' + dc + "</div>" +
      (a.has_audio ? '<audio controls preload="none" src="' + API + "/api/audit/" + encodeURIComponent(a.conversation_id) + '/audio"></audio>' : "") +
      '<button data-id="' + esc(a.conversation_id) + '">Show transcript (' + a.turns + ' turns)</button><div class="tx"></div></div>';
  }).join("");
}
$("audit").addEventListener("click", async e => {
  const id = e.target.dataset && e.target.dataset.id; if (!id) return;
  const box = e.target.nextElementSibling;
  if (box.childElementCount) { box.innerHTML = ""; return; }
  const rec = await (await fetch(API + "/api/audit/" + encodeURIComponent(id))).json();
  box.innerHTML = (rec.transcript || []).filter(t => t.message).map(t =>
    '<div dir="auto" class="' + (t.role === "agent" ? "agent" : "user") + '">' + esc(t.message) + "</div>").join("");
});

// Adaptive polling: brisk only while something is in flight, back off on failure.
function schedule() {
  if (timer) clearTimeout(timer);
  const delay = failures ? Math.min(3000 * Math.pow(2, failures - 1), 30000) : (busy ? 2000 : 8000);
  timer = setTimeout(tick, delay);
}

tick(); loadAudit(); setInterval(loadAudit, 20000);
</script>
</body>
</html>
"""

# One screen for both routes, kept under the names el_tools and build_web import.
CHECKOUT_HTML = DASHBOARD_HTML
CONSOLE_HTML = DASHBOARD_HTML
