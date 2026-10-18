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

function renderEmpty() {
  const p = status?.pipeline || {};
  $("#lead").innerHTML = `<div class="card empty">
    <h2>${p.running ? "Researching leads…" : "No leads ready"}</h2>
    <p>${p.running ? esc(p.step) : "All discovered leads have been reviewed. Find more to keep going."}</p>
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
  $(".grade", node).textContent = `Grade ${lead.grade}` + (lead.status !== "new" ? ` · ${lead.status.replace("_", " ")}` : "");
  $(".summary", node).textContent = lead.summary;

  const site = lead.final_url || lead.website;
  const mapUrl = lead.lat ? `https://www.openstreetmap.org/?mlat=${lead.lat}&mlon=${lead.lon}#map=18/${lead.lat}/${lead.lon}` : null;
  const googleUrl = `https://www.google.com/search?q=${encodeURIComponent(`${lead.name} ${lead.city || "Tampa"} FL`)}`;
  $(".contact ul", node).innerHTML = [
    lead.phone && `<li><span class="label">Phone</span><a href="tel:${esc(lead.phone)}">${esc(lead.phone)}</a></li>`,
    lead.email && `<li><span class="label">Email</span><a href="mailto:${esc(lead.email)}">${esc(lead.email)}</a></li>`,
    site && `<li><span class="label">Website</span><a href="${esc(/^https?:/.test(site) ? site : "http://" + site)}" target="_blank" rel="noopener">${esc(site)}</a></li>`,
    lead.address && `<li><span class="label">Address</span>${mapUrl ? `<a href="${mapUrl}" target="_blank" rel="noopener">${esc(lead.address)}</a>` : esc(lead.address)}</li>`,
    `<li><span class="label">Verify</span><a href="${googleUrl}" target="_blank" rel="noopener">Search Google</a>${lead.source_url ? ` · <a href="${esc(lead.source_url)}" target="_blank" rel="noopener">OSM listing</a>` : ""}</li>`,
  ].filter(Boolean).join("");

  const opps = lead.opportunities || [];
  $(".opp-list", node).innerHTML = opps.length ? opps.map((o) => `
    <div class="opp">
      <div class="opp-title"><span>${esc(o.label)}${o.basis ? `<span class="chip kind-${esc(o.basis)}">${esc(o.basis)}</span>` : ""}<span class="chip">${esc(o.confidence)} confidence</span></span><span class="opp-pts">+${o.points}</span></div>
      <p>${esc(o.pitch)}</p>
      <ul>${o.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
    </div>`).join("") : `<p>No evidence-backed opportunities found.</p>`;

  $(".all-findings ul", node).innerHTML = (lead.findings || []).map((f) =>
    `<li><strong class="sev-${f.severity}">${esc(f.title)}</strong> <span class="chip kind-${f.kind}">${KIND_LABEL[f.kind] || f.kind}</span><span class="chip">${f.severity}</span><span class="chip">${f.confidence} confidence</span><br>${esc(f.evidence)}</li>`).join("");
  $(".notes", node).value = lead.notes || "";

  if (lead.ai_brief) { $(".brief", node).hidden = false; $(".brief-body", node).innerHTML = md(lead.ai_brief); }
  const aiBtn = $('[data-act="brief"]', node);
  if (!status?.ai_configured) { aiBtn.disabled = true; aiBtn.title = "Set ANTHROPIC_API_KEY to enable AI briefs"; }
  if (lead.ai_brief) aiBtn.textContent = "✨ Rewrite AI brief";

  $("#lead").replaceChildren(node);
  $("#lead .notes").addEventListener("change", (e) => api(`/api/leads/${lead.id}`, { method: "POST", body: JSON.stringify({ notes: e.target.value }) }));
  $("#lead .actions").addEventListener("click", onAction);
  window.scrollTo(0, 0);
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
  const leads = await api(`/api/leads${filter}`);
  $("#table tbody").innerHTML = leads.map((l) => `
    <tr data-id="${l.id}"><td class="num">${l.score}</td><td>${esc(l.name)}<br><small class="sev-low">${esc(l.city || "")}</small></td>
    <td>${esc(l.industry_label)}</td><td>${esc(l.opportunities?.[0]?.label || "—")}</td><td>${esc(l.status.replace("_", " "))}</td></tr>`).join("")
    || `<tr><td colspan="5">Nothing here yet.</td></tr>`;
}

function show(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("#view-next").hidden = view !== "next";
  $("#view-pipeline").hidden = view !== "pipeline";
  if (view === "pipeline") loadPipeline();
}

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  show(t.dataset.view);
  if (t.dataset.view === "next" && (!current || current.status !== "new")) loadNext();
}));
$("#filter").addEventListener("change", loadPipeline);
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
loadCustomers().then(refreshStatus).then(loadNext);
