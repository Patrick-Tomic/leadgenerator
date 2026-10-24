const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let current = null;
let status = null;
let waitTimer = null;
let customer = null;
try { customer = localStorage.getItem("customer"); } catch {}

// Every lead request is scoped to the selected customer.
async function api(path, opts = {}) {
  if (customer) path += (path.includes("?") ? "&" : "?") + "customer=" + encodeURIComponent(customer);
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

async function loadCustomers() {
  const list = await fetch("/api/customers").then((r) => r.json());
  if (!list.some((c) => c.slug === customer)) customer = null;
  const sel = $("#customer");
  sel.innerHTML = list.map((c) => `<option value="${esc(c.slug)}">${esc(c.name)}</option>`).join("");
  if (customer) sel.value = customer;
  customer = sel.value || null;
  sel.hidden = list.length < 2;
}

async function refreshStatus() {
  status = await api("/api/status");
  $("#brave-credit").hidden = !status.brave_attribution;
  const p = status.pipeline;
  $("#status").innerHTML =
    `<span class="dot ${p.running ? "busy" : ""}"></span>${status.ready} leads ready · ${status.analyzed}/${status.discovered} analyzed` +
    (p.running ? ` · ${esc(p.step)}` : "") + (p.error ? ` · <span class="sev-high">${esc(p.error)}</span>` : "");
  return status;
}

// Minimal Markdown → HTML for the AI brief (headings, bullets, bold, paragraphs).
function md(text) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\*(.+?)\*/g, "<em>$1</em>");
  let html = "", list = false;
  for (const line of text.split("\n")) {
    const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (li) { if (!list) { html += "<ul>"; list = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    if (list) { html += "</ul>"; list = false; }
    const h = line.match(/^#{1,6}\s+(.*)/);
    if (h) html += `<h4>${inline(h[1])}</h4>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  return html + (list ? "</ul>" : "");
}

const KIND_LABEL = { confirmed: "confirmed issue", potential: "potential", unavailable: "couldn't check", info: "info" };
const confChip = (c) => c ? `<span class="chip conf-${esc(c)}">${esc(c)}</span>` : "";
const link = (u, text) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(text || u)}</a>`;

function renderEmpty() {
  const p = status?.pipeline || {};
  $("#lead").innerHTML = `<div class="card empty">
    <h2>${p.running ? "Searching for strong leads…" : "No strong leads ready"}</h2>
    <p>${p.running ? esc(p.step) : "No unreviewed leads meet this customer's minimum score yet. Weaker leads are in the Pipeline tab."}</p>
    <p class="muted">Only grade A–B leads (score ${status?.min_score ?? 40}+) are shown here. Most businesses have no serious website problems, so finding them takes many checks.</p>
    ${p.running ? "" : `<button id="empty-discover">Find more leads</button>`}
  </div>`;
  $("#empty-discover")?.addEventListener("click", async () => { await api("/api/discover", { method: "POST" }); waitForLead(); });
}

function renderLead(lead) {
  current = lead;
  const node = $("#lead-tpl").content.cloneNode(true);
  $(".name", node).textContent = lead.name;
  $(".meta", node).textContent = [lead.industry_label, lead.city || lead.address].filter(Boolean).join(" · ");
  $(".num", node).textContent = lead.score;
  $(".grade", node).textContent = `Grade ${lead.grade}` + (lead.status === "call_later"
    ? ` · emailed ${fmtDate(lead.emailed_at)}, call by ${fmtDate(lead.follow_up_at)}`
    : lead.status !== "new" ? ` · ${lead.status.replace("_", " ")}` : "");
  $(".summary", node).textContent = lead.summary;

  const site = lead.final_url || lead.website;
  const mapUrl = lead.lat ? `https://www.openstreetmap.org/?mlat=${lead.lat}&mlon=${lead.lon}#map=18/${lead.lat}/${lead.lon}` : null;
  const googleUrl = `https://www.google.com/search?q=${encodeURIComponent(`${lead.name} ${lead.city || ""} FL`.replace(/\s+/g, " "))}`;
  $(".contact ul", node).innerHTML = [
    `<li><span class="label">Phone <button class="link-btn" data-act="phone">${lead.phone ? "Edit" : "Add"}</button></span>${lead.phone ? `<a href="tel:${esc(lead.phone)}">${esc(lead.phone)}</a>` : `<span class="muted">No phone from any source</span>`}</li>`,
    lead.email && `<li><span class="label">Email</span><a href="mailto:${esc(lead.email)}">${esc(lead.email)}</a></li>`,
    `<li><span class="label">Website <button class="link-btn" data-act="website">${site ? "Edit" : "Add"}</button></span>${site ? `<a href="${esc(/^https?:/.test(site) ? site : "http://" + site)}" target="_blank" rel="noopener">${esc(site)}</a><br>` : ""}<span class="chip ws-${esc(lead.website_state || "unknown")}">${esc(lead.website_state_label || "Website status unknown")}</span></li>`,
    lead.address && `<li><span class="label">Address</span>${mapUrl ? `<a href="${mapUrl}" target="_blank" rel="noopener">${esc(lead.address)}</a>` : esc(lead.address)}</li>`,
    ownersItem(lead),
    visibilityItem(lead),
    reviewsItem(lead),
    `<li><span class="label">Verify</span><a href="${googleUrl}" target="_blank" rel="noopener">Search Google</a>${lead.source_url ? ` · <a href="${esc(lead.source_url)}" target="_blank" rel="noopener">OSM listing</a>` : ""}</li>`,
  ].filter(Boolean).join("");

  const ex = lead.explanation || {};
  const st = ex.strongest;
  if (st) {
    $(".evidence", node).hidden = false;
    $(".ev-title", node).innerHTML = `<strong>${esc(st.title)}</strong>${confChip(st.confidence_level)}<span class="chip tier-${st.tier}">${esc(st.tier_label)}</span>`;
    $(".ev-fields", node).innerHTML = [
      ["Evidence", esc(st.evidence)],
      st.page && ["Affected page", link(st.page)],
      st.impact && ["Why it may matter", esc(st.impact)],
      ["Suggested service", esc(st.service)],
      st.talking_point && ["Talking point", `<q>${esc(st.talking_point)}</q>`],
    ].filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  }
  $(".ev-explain", node).textContent = ex.text || "";
  if (!st && ex.text) { $(".evidence", node).hidden = false; $(".evidence h3", node).textContent = "Score"; }
  if ((ex.verify || []).length) {
    $(".verify", node).hidden = false;
    $(".verify ul", node).innerHTML = ex.verify.map((v) => `<li>${esc(v)}</li>`).join("");
  }

  const others = (lead.findings || []).filter((f) => !st || f.code !== st.code);
  const group = (fs) => fs.map((f) => `
    <div class="obs">
      <div><strong>${esc(f.title)}</strong>${confChip(f.confidence_level)}</div>
      <div class="obs-ev">${esc(f.evidence)}${f.page ? ` · ${link(f.page, "page")}` : ""}</div>
      ${f.impact ? `<div class="obs-impact">${esc(f.impact)}</div>` : ""}
    </div>`).join("");
  const by = (tiers) => others.filter((f) => tiers.includes(f.tier));
  const main = by(["strong", "moderate", "weak"]), optional = by(["optional"]), unchecked = by(["none"]);
  $(".obs-list", node).innerHTML =
    (main.length ? group(main) : `<p class="muted">No other problems observed.</p>`) +
    (unchecked.length ? `<details open><summary>Couldn't check / not tested (${unchecked.length})</summary>${group(unchecked)}</details>` : "") +
    (optional.length ? `<details><summary>Optional features, low priority (${optional.length})</summary><p class="muted">Absent nice-to-haves. Not evidence of a problem on their own.</p>${group(optional)}</details>` : "");
  renderDetails(node, lead);
  setupEmail(node, lead);
  $(".notes", node).value = lead.notes || "";

  if (lead.ai_brief) { $(".brief", node).hidden = false; $(".brief-body", node).innerHTML = md(lead.ai_brief); }
  const aiBtn = $('[data-act="brief"]', node);
  if (!status?.ai_configured) { aiBtn.disabled = true; aiBtn.title = "Set ANTHROPIC_API_KEY to enable AI briefs"; }
  if (lead.ai_brief) aiBtn.textContent = "✨ Rewrite AI brief";

  $("#lead").replaceChildren(node);
  $("#lead .notes").addEventListener("change", (e) => api(`/api/leads/${lead.id}`, { method: "POST", body: JSON.stringify({ notes: e.target.value }) }));
  $("#lead .actions").addEventListener("click", onAction);
  $("#lead .contact").addEventListener("click", onAction);
  $("#lead .email-panel").addEventListener("click", (e) => { if (e.target.closest("button[data-act]")) onAction(e); });
  window.scrollTo(0, 0);
}

function ownersItem(lead) {
  const o = lead.owners || [];
  if (!o.length) return "";
  return `<li><span class="label">Owner / officers <span class="muted">(Florida Sunbiz)</span></span>${o.slice(0, 3).map((p) =>
    `<div>${esc(p.name)} <span class="muted">${esc(p.title)}${{ medium: " · matched by name + ZIP", low: " · matched by name + city: verify" }[p.match_confidence] || ""}</span></div>`).join("")}</li>`;
}

function visibilityItem(lead) {
  const v = (lead.explanation || {}).visibility;
  if (!v) return "";
  return `<li><span class="label">How well known <span class="muted">(estimate)</span></span>
    <span class="chip vis-${esc(v.level)}">${esc(v.label)}</span>
    ${v.reasons.length ? `<div class="muted">${esc(v.reasons.join("; "))}</div>` : ""}</li>`;
}

function reviewsItem(lead) {
  const r = lead.google_reviews;
  if (r && r.found) return `<li><span class="label">Google reviews</span>${r.count} review${r.count === 1 ? "" : "s"}${r.rating ? `, ${r.rating}★` : ""}
    · <a href="${esc(r.maps_url)}" target="_blank" rel="noopener">view on Google Maps</a><div class="muted">Data from Google</div></li>`;
  if (r) return `<li><span class="label">Google reviews</span><span class="muted">${esc(r.note)}</span></li>`;
  if (lead.google_reviews_enabled) return `<li><span class="label">Google reviews</span><button class="link-btn" data-act="reviews">Check Google reviews</button></li>`;
  return `<li><span class="label">Google reviews</span><span class="muted">Add a Google Places key to check</span></li>`;
}

const SRC_CHIP = (s) => `<span class="chip src">${esc(s)}</span>`;

function renderDetails(node, lead) {
  const d = lead.details;
  if (!d) { $(".sources-panel", node).hidden = true; return; }
  const ws = d.website;
  $(".ws-block", node).innerHTML = `
    <div><strong>Website:</strong> <span class="chip ws-${esc(ws.state)}">${esc(ws.label)}</span>
      ${ws.conflict ? `<span class="chip conf-unverified">sources disagree</span>` : ""}</div>
    ${ws.candidates.length ? `<ul class="cand-list">${ws.candidates.map((c) => `
      <li${c.selected ? ' class="selected"' : ""}>${link(c.url)} ${SRC_CHIP(c.source)}
        <span class="chip">identity: ${esc(c.identity || "?")}</span><span class="chip">${esc(c.check_status || "unchecked")}</span>
        ${c.selected ? '<span class="chip conf-verified">in use</span>' : ""}
        <div class="muted">${esc(c.detail || "")}${c.final_url && c.final_url !== c.url ? ` → ${esc(c.final_url)}` : ""}
        ${c.evidence.length ? ` · ${esc(c.evidence.join("; "))}` : ""}</div></li>`).join("")}</ul>`
      : `<p class="muted">No website candidates from any source.</p>`}`;
  $(".detail-table tbody", node).innerHTML = d.fields.map((f) => `
    <tr${f.conflict ? ' class="conflict"' : ""}><th>${esc(f.label)}${f.conflict ? ' <span class="chip conf-unverified">conflict</span>' : ""}</th>
      <td>${f.values.map((v) => `<div>${esc(v.value)} ${v.sources.map(SRC_CHIP).join("")}</div>`).join("")}</td></tr>`).join("");
  $(".source-list", node).innerHTML = d.sources.map((s) => `
    <li>${SRC_CHIP(s.label)} ${s.url ? link(s.url, s.record_id) : esc(s.record_id)}
      <div class="muted">match: ${esc(s.match_confidence || "?")}, ${esc(s.match_explanation || "")} · fields: ${esc(s.fields.join(", ") || "none")}
      · last checked ${esc((s.last_checked_at || "").slice(0, 10))}${s.note ? ` · ${esc(s.note)}` : ""}</div></li>`).join("");
  const tags = lead.source_tags || {};
  if (Object.keys(tags).length) $(".raw-tags pre", node).textContent = Object.entries(tags).map(([k, v]) => `${k} = ${v}`).join("\n");
  else $(".raw-tags", node).hidden = true;
}

const SENDER_LABELS = { name: "Your name", company: "Company" };

function setupEmail(node, lead) {
  const panel = $(".email-panel", node);
  const state = { variant: "short", draft: null };
  const load = async () => {
    const d = await api(`/api/leads/${lead.id}/email?variant=${state.variant}`);
    state.draft = d;
    $(".email-variant", panel).innerHTML = Object.entries(d.variants).map(([k, v]) =>
      `<option value="${k}"${k === d.variant ? " selected" : ""}>${esc(v)}</option>`).join("");
    $(".email-subject", panel).innerHTML = d.subjects.map((s) => `<option>${esc(s)}</option>`).join("");
    $(".email-body", panel).value = d.body;
    $(".email-warnings", panel).innerHTML = [...d.warnings.map((w) => `<p class="warn">⚠ ${esc(w)}</p>`),
      ...d.verify.map((v) => `<p class="muted">${esc(v)}</p>`),
      d.based_on ? `<p class="muted">Based on: ${esc(d.based_on)}</p>` : ""].join("");
    updateMailto();
  };
  const updateMailto = () => {
    const a = $(".email-open", panel);
    if (!lead.email) { a.hidden = true; return; }
    a.hidden = false;
    a.href = `mailto:${lead.email}?subject=${encodeURIComponent($(".email-subject", panel).value)}&body=${encodeURIComponent($(".email-body", panel).value)}`;
  };
  const copy = async (text, what) => {
    try { await navigator.clipboard.writeText(text); }
    catch { const t = document.createElement("textarea"); t.value = text; document.body.append(t); t.select(); document.execCommand("copy"); t.remove(); }
    $(".email-status", panel).textContent = `${what} copied`;
    setTimeout(() => ($(".email-status", panel).textContent = ""), 2000);
  };
  panel.addEventListener("change", (e) => {
    if (e.target.matches(".email-variant")) { state.variant = e.target.value; load(); }
    if (e.target.matches(".email-subject")) updateMailto();
  });
  panel.addEventListener("input", (e) => { if (e.target.matches(".email-body")) updateMailto(); });
  panel.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-copy]");
    if (b) copy(b.dataset.copy === "subject" ? $(".email-subject", panel).value
                : `Subject: ${$(".email-subject", panel).value}\n\n${$(".email-body", panel).value}`, b.dataset.copy === "subject" ? "Subject" : "Email");
    if (e.target.matches(".sender-save")) {
      const body = Object.fromEntries([...panel.querySelectorAll(".sender-grid input")].map((i) => [i.name, i.value]));
      await api("/api/settings/sender", { method: "POST", body: JSON.stringify(body) });
      $(".sender-form", panel).open = false;
      load();
    }
  });
  $(".sender-form", panel).addEventListener("toggle", async (e) => {
    if (!e.target.open || $(".sender-grid", panel).children.length) return;
    const s = await api("/api/settings/sender");
    $(".sender-grid", panel).innerHTML = Object.entries(SENDER_LABELS).map(([k, label]) =>
      `<label>${esc(label)}<input name="${k}" value="${esc(s[k] || "")}"></label>`).join("");
  });
  load().catch((err) => ($(".email-warnings", panel).textContent = err.message));
}

const fmtDate = (d) => d ? new Date(d.length === 10 ? d + "T12:00:00" : d).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";

async function loadCallLater() {
  const d = await api("/api/call-later");
  setDueBadge(d.due_count);
  $("#cl-table tbody").innerHTML = d.rows.map((r) => `
    <tr class="${r.overdue ? "overdue" : r.due ? "due" : ""}">
      <td><strong>${esc(fmtDate(r.follow_up_at))}</strong>${r.overdue ? ` <span class="chip conf-unverified">overdue</span>` : r.due ? ` <span class="chip conf-probable">today</span>` : ""}</td>
      <td>${esc(r.name)}<div class="muted">${esc(r.industry_label)}${r.city ? " · " + esc(r.city) : ""}</div></td>
      <td>${r.owners.map((o) => `<div>${esc(o.name)} <span class="muted">${esc(o.title)}</span></div>`).join("") || `<span class="muted">—</span>`}</td>
      <td>${r.phone ? `<a href="tel:${esc(r.phone)}">${esc(r.phone)}</a>` : `<span class="muted">—</span>`}</td>
      <td>${r.email ? `<a href="mailto:${esc(r.email)}">${esc(r.email)}</a>` : `<span class="muted">—</span>`}</td>
      <td>${esc(fmtDate(r.emailed_at))}</td>
      <td class="muted">${esc((r.notes || "").slice(0, 80))}</td>
      <td><button class="link-btn" data-open-cl="${r.id}">Open</button></td></tr>`).join("")
    || `<tr><td colspan="8">Nothing here yet. After you email a business, click "Emailed, call later" on its page.</td></tr>`;
}

function setDueBadge(n) { $("#due-badge").hidden = !n; $("#due-badge").textContent = n || ""; }
async function refreshDueBadge() { try { setDueBadge((await api("/api/call-later")).due_count); } catch {} }

let nwOffset = 0;
async function loadNoWeb() {
  const q = new URLSearchParams({ industry: $("#nw-industry").value, area: $("#nw-area").value, google: $("#nw-google").value,
    offset: nwOffset, has_owner: $("#nw-owner").checked, has_email: $("#nw-email").checked });
  const d = await api(`/api/no-web?${q}`);
  if ($("#nw-industry").options.length === 1) {
    $("#nw-industry").innerHTML += Object.entries(d.industries).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    $("#nw-area").innerHTML += Object.entries(d.areas).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  }
  $("#nw-help").textContent = "Businesses where no source lists a website. Unless a website search ran, that's from map listings only, so check before assuming they have none. "
    + (d.search_enabled ? "Find website runs a capped web search (Brave Search). " : "")
    + (d.google_enabled ? "Check Google uses one Google Places lookup (free up to the monthly cap)." : "Add a Google Places key to check whether they have a Google listing.")
    + " Businesses with a phone number are listed first.";
  $("#nw-table tbody").innerHTML = d.rows.map((r) => `
    <tr><td>${esc(r.name)}${Object.keys(r.social).length ? `<div class="muted">${Object.entries(r.social).map(([k, u]) => `<a href="${esc(u.startsWith("http") ? u : "https://" + u)}" target="_blank" rel="noopener">${esc(k)}</a>`).join(" · ")}</div>` : ""}</td>
      <td>${r.owners.length ? r.owners.map((o) => `<div>${esc(o.name)} <span class="muted">${esc(o.title)}${o.confidence === "low" ? " · verify" : ""}</span></div>`).join("") : `<span class="muted">—</span>`}</td>
      <td>${esc(r.industry_label)}</td><td>${esc(r.city || "")}</td>
      <td>${r.phone ? `<a href="tel:${esc(r.phone)}">${esc(r.phone)}</a>` : `<span class="muted">—</span>`}</td>
      <td class="nw-email-cell">${r.emails.length ? r.emails.map((e) => `<div><a href="mailto:${esc(e.email)}">${esc(e.email)}</a>
          <div class="muted">${esc(e.evidence)}${e.confidence !== "high" ? " · verify" : ""}</div></div>`).join("")
        : (d.email_search_enabled ? `<button class="link-btn" data-find-email="${r.id}">Find email</button>` : `<span class="muted">—</span>`)}</td>
      <td><span class="chip">${esc(r.website_label)}</span>
        ${d.search_enabled && r.website_state !== "none_found" ? `<button class="link-btn" data-search="${r.id}">Find website</button>` : ""}</td>
      <td><span class="chip ${r.google_listing === 0 ? "conf-verified" : ""}">${esc(r.google_label)}</span>
        ${d.google_enabled && r.google_listing === null ? `<button class="link-btn" data-google="${r.id}">Check Google</button>` : ""}</td>
      <td>${esc((r.status || "not opened").replace("_", " "))}</td>
      <td><button class="link-btn" data-open-nw="${r.id}">Open</button></td></tr>`).join("")
    || `<tr><td colspan="10">No businesses match these filters yet.</td></tr>`;
  $("#nw-count").textContent = d.total ? `${d.offset + 1}–${d.offset + d.rows.length} of ${d.total.toLocaleString()}` : "";
  $("#nw-prev").disabled = d.offset === 0;
  $("#nw-next").disabled = d.offset + d.rows.length >= d.total;
}

async function loadRegistrations() {
  const data = await api(`/api/registrations${$("#reg-all").checked ? "?all=true" : ""}`);
  $("#reg-help").textContent = `Download Sunbiz daily files (doc/cor/yyyymmddc.txt, doc/fic/yyyymmddf.txt) from the official public SFTP into ${data.import_dir}, then import. Registrations are legal entities, not confirmed operating businesses; addresses are as filed.`;
  $("#reg-imports").innerHTML = data.imports.length ? `<p class="muted">Recent imports: ${data.imports.map((i) =>
    `${esc(i.file_name)} (${esc(i.status)}: ${i.imported ?? 0} new, ${i.updated ?? 0} updated, ${i.skipped ?? 0} skipped, ${i.rejected ?? 0} rejected)`).join("; ")}</p>` : "";
  $("#reg-table tbody").innerHTML = data.registrations.map((r) => `
    <tr><td>${esc(r.filing_date || "")}</td><td>${esc(r.name)}<br><small class="muted">${esc(r.doc_number)}</small></td>
      <td>${esc(r.entity_type || "")}</td><td>${esc(r.principal_city || "")}</td><td>${esc(r.industry || "—")}</td>
      <td>${(r.flags || []).map((f) => `<span class="chip${f === "state_license" ? " conf-verified" : ""}">${esc(f.replace(/_/g, " "))}</span>`).join("")}
        ${r.license_occupation ? `<div class="muted">${esc(r.license_occupation)} ${esc(r.license_number || "")} · since ${esc(r.licensed_since || "?")} · ${esc(r.license_match_confidence)} match</div>` : ""}</td>
      <td>${r.match_lead_id ? `#${r.match_lead_id} <span class="chip">${esc(r.match_confidence)}</span>` : "—"}</td>
      <td>${r.promoted_lead_id ? `<button class="link-btn" data-open="${r.promoted_lead_id}">Open lead</button>`
        : `<button class="link-btn" data-promote="${r.id}">Promote to lead</button>`}</td></tr>`).join("")
    || `<tr><td colspan="8">No registrations imported${$("#reg-all").checked ? "" : " for this customer's areas and industries"} yet.</td></tr>`;
}

async function onAction(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn || !current) return;
  const act = btn.dataset.act;
  const label = btn.textContent;
  btn.disabled = true;
  try {
    if (act === "brief") {
      btn.textContent = "Writing…";
      renderLead(await api(`/api/leads/${current.id}/brief`, { method: "POST" }));
    } else if (act === "call_later") {
      const days = prompt("Emailed. Call them back in how many days?", "3");
      if (days === null) { btn.disabled = false; return; }
      await api(`/api/leads/${current.id}`, { method: "POST",
        body: JSON.stringify({ status: "call_later", follow_up_days: parseInt(days, 10) || 0, notes: $("#lead .notes").value }) });
      refreshDueBadge();
      await loadNext();
    } else if (act === "reviews") {
      btn.textContent = "Checking…";
      renderLead(await api(`/api/leads/${current.id}/reviews`, { method: "POST" }));
    } else if (act === "phone") {
      const phone = prompt("Business phone number (10 digits):", current.phone || "");
      if (!phone || !phone.trim()) { btn.disabled = false; return; }
      renderLead(await api(`/api/leads/${current.id}/phone`, { method: "POST", body: JSON.stringify({ phone: phone.trim() }) }));
    } else if (act === "website") {
      const url = prompt("Business website (it will be re-checked now):", current.final_url || current.website || "");
      if (!url || !url.trim()) { btn.disabled = false; return; }
      btn.textContent = "Checking…";
      renderLead(await api(`/api/leads/${current.id}/website`, { method: "POST", body: JSON.stringify({ website: url.trim() }) }));
    } else if (act === "reanalyze") {
      btn.textContent = "Checking…";
      renderLead(await api(`/api/leads/${current.id}/reanalyze`, { method: "POST" }));
    } else {
      const notes = $("#lead .notes").value;
      await api(`/api/leads/${current.id}`, { method: "POST", body: JSON.stringify({ status: act, notes }) });
      await loadNext();
    }
  } catch (err) {
    alert(err.message);
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function loadNext() {
  const { lead } = await api("/api/next");
  await refreshStatus();
  if (lead) renderLead(lead);
  else { current = null; renderEmpty(); waitForLead(); }
}

function waitForLead() {
  clearTimeout(waitTimer);
  waitTimer = setTimeout(async () => {
    await refreshStatus();
    if (!$("#view-next").hidden && !current) {
      if (status.ready > 0) return loadNext();
      renderEmpty();
      if (status.pipeline.running) waitForLead();
    }
  }, 3000);
}

async function loadPipeline() {
  const filter = $("#filter").value ? `?status=${$("#filter").value}` : "";
  $("#export").href = `/api/export.csv${filter}${filter ? "&" : "?"}customer=${encodeURIComponent(customer || "")}`;
  const g = $("#grade-filter").value;
  const leads = (await api(`/api/leads${filter}`)).filter((l) => !g || g.includes(l.grade));
  $("#table tbody").innerHTML = leads.map((l) => `
    <tr data-id="${l.id}"><td class="num">${l.score}</td><td>${esc(l.name)}<br><small class="sev-low">${esc(l.city || "")}</small></td>
    <td>${esc(l.industry_label)}</td><td>${esc(l.opportunities?.[0]?.label || "—")}</td><td>${esc(l.status.replace("_", " "))}</td></tr>`).join("")
    || `<tr><td colspan="5">Nothing here yet.</td></tr>`;
}

function show(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("#view-next").hidden = view !== "next";
  $("#view-pipeline").hidden = view !== "pipeline";
  $("#view-registrations").hidden = view !== "registrations";
  $("#view-noweb").hidden = view !== "noweb";
  $("#view-calllater").hidden = view !== "calllater";
  if (view === "calllater") loadCallLater().catch((e) => alert(e.message));
  if (view === "noweb") { nwOffset = 0; loadNoWeb().catch((e) => alert(e.message)); }
  if (view === "pipeline") loadPipeline();
  if (view === "registrations") loadRegistrations().catch((e) => alert(e.message));
}

$("#reg-all").addEventListener("change", () => loadRegistrations());
$("#cl-table tbody").addEventListener("click", async (e) => {
  const o = e.target.closest("[data-open-cl]");
  if (!o) return;
  show("next"); renderLead(await api(`/api/leads/${o.dataset.openCl}`));
});
["#nw-industry", "#nw-area", "#nw-google", "#nw-owner", "#nw-email"].forEach((id) => $(id).addEventListener("change", () => { nwOffset = 0; loadNoWeb(); }));
$("#nw-prev").addEventListener("click", () => { nwOffset = Math.max(0, nwOffset - 100); loadNoWeb(); });
$("#nw-next").addEventListener("click", () => { nwOffset += 100; loadNoWeb(); });
$("#nw-table tbody").addEventListener("click", async (e) => {
  const g = e.target.closest("[data-google]"), o = e.target.closest("[data-open-nw]"), f = e.target.closest("[data-search]");
  const fe = e.target.closest("[data-find-email]");
  try {
    if (fe) {
      fe.disabled = true; fe.textContent = "Searching…";
      const r = await api(`/api/no-web/${fe.dataset.findEmail}/email`, { method: "POST" });
      fe.closest("td").innerHTML = r.emails.length ? r.emails.map((m) => `<div><a href="mailto:${esc(m.email)}">${esc(m.email)}</a>
          <div class="muted">${esc(m.evidence)}${m.confidence !== "high" ? " · verify" : ""}</div></div>`).join("")
        : `<span class="muted">${esc(r.message)}</span>`;
    }
    if (f) {
      f.disabled = true; f.textContent = "Searching…";
      const r = await api(`/api/no-web/${f.dataset.search}/search`, { method: "POST" });
      f.closest("td").innerHTML = `<span class="chip">${esc(r.label)}</span>`
        + (r.website ? ` <a href="${esc(r.website)}" target="_blank" rel="noopener">${esc(r.website)}</a>` : "")
        + (r.detail ? `<div class="muted">${esc(r.detail)}</div>` : "");
    }
    if (g) {
      g.disabled = true; g.textContent = "Checking…";
      const r = await api(`/api/no-web/${g.dataset.google}/google`, { method: "POST" });
      g.closest("td").innerHTML = `<span class="chip ${r.found ? "" : "conf-verified"}">${esc(r.google_label)}</span>`
        + (r.found && r.maps_url ? ` <a href="${esc(r.maps_url)}" target="_blank" rel="noopener">view</a>` : "");
    }
    if (o) {
      o.disabled = true; o.textContent = "Opening…";
      const lead = await api(`/api/no-web/${o.dataset.openNw}/open`, { method: "POST" });
      show("next"); renderLead(lead);
    }
  } catch (err) { alert(err.message); loadNoWeb(); }
});
$("#lic-update").addEventListener("click", async (e) => {
  e.target.disabled = true; e.target.textContent = "Updating (about 1 minute)…";
  try {
    const r = await api("/api/licenses/update", { method: "POST" });
    const i = r.import;
    alert(i.status === "skipped" ? "Licenses are already up to date." :
      `${i.kept} licenses imported; ${i.registrations_matched} registrations and ${i.businesses_matched} businesses matched.`);
    loadRegistrations();
  } catch (err) { alert(err.message); } finally { e.target.disabled = false; e.target.textContent = "Update state contractor licenses"; }
});
$("#reg-import").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    const { results } = await api("/api/registrations/import", { method: "POST" });
    alert(results.length ? results.map((r) => `${r.file}: ${r.status}${r.reason ? ` (${r.reason})` : ""}`).join("\n") : "No data files found.");
    loadRegistrations();
  } catch (err) { alert(err.message); } finally { e.target.disabled = false; }
});
$("#reg-table tbody").addEventListener("click", async (e) => {
  const open = e.target.closest("[data-open]"), prom = e.target.closest("[data-promote]");
  if (!open && !prom) return;
  try {
    let lead;
    if (open) lead = await api(`/api/leads/${open.dataset.open}`);
    else {
      prom.disabled = true; prom.textContent = "Checking…";
      let res = await fetch(`/api/registrations/${prom.dataset.promote}/promote${customer ? `?customer=${encodeURIComponent(customer)}` : ""}`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (res.status === 400) {
        const industry = prompt(`Category for this business? One of: ${Object.keys(status.industries).join(", ")}`);
        if (!industry) { loadRegistrations(); return; }
        res = await fetch(`/api/registrations/${prom.dataset.promote}/promote${customer ? `?customer=${encodeURIComponent(customer)}` : ""}`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ industry }) });
      }
      lead = await res.json();
      if (!res.ok) throw new Error(lead.detail || `HTTP ${res.status}`);
    }
    show("next"); renderLead(lead);
  } catch (err) { alert(err.message); loadRegistrations(); }
});

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  show(t.dataset.view);
  if (t.dataset.view === "next" && (!current || current.status !== "new")) loadNext();
}));
$("#filter").addEventListener("change", loadPipeline);
$("#grade-filter").addEventListener("change", loadPipeline);
$("#customer").addEventListener("change", (e) => {
  customer = e.target.value;
  try { localStorage.setItem("customer", customer); } catch {}
  current = null;
  refreshStatus().then(() => ($("#view-pipeline").hidden ? loadNext() : loadPipeline()));
});
$("#discover").addEventListener("click", async () => { await api("/api/discover", { method: "POST" }); refreshStatus(); });
$("#table tbody").addEventListener("click", async (e) => {
  const row = e.target.closest("tr[data-id]");
  if (!row) return;
  show("next");
  renderLead(await api(`/api/leads/${row.dataset.id}`));
});
setInterval(() => refreshStatus().catch(() => {}), 10000);
// #lead=<id> opens a specific lead (shareable link); otherwise show the next best lead.
const deepLink = location.hash.match(/lead=(\d+)/);
loadCustomers().then(refreshStatus).then(refreshDueBadge).then(() => deepLink ? api(`/api/leads/${deepLink[1]}`).then(renderLead).catch(loadNext) : loadNext());
