"""Live HTML dashboard for the agentic stack (Round 4.5).

GET /v1/_debug/dashboard  -> single-page HTML view with auto-refresh.

The page is fully self-contained — no external CSS/JS — and pulls fresh
JSON from /v1/_debug/agentic every 15 seconds. Behind the same Basic Auth
gate as the rest of the debug surface.

Sections:
  - Header strip with timestamp + global health badge
  - Env flag grid (each layer's on/off + which model)
  - Per-layer card: skills, reflector, goal_tracker, skill_planner,
    orchestrator, distiller, prompt_evolver, eval
  - Watchdog rolling window (avg confidence + success rate)
  - Answer cache stats + top hits
  - Model preference rankings per domain
  - Cost telemetry (per provider, recent calls)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)
router = APIRouter()


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>OpenJarvis Agentic Stack — Live</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
  :root {
    --bg: #0b0f14;
    --panel: #121821;
    --panel-2: #1a2331;
    --fg: #e6edf3;
    --muted: #8b97a8;
    --accent: #4cc9f0;
    --ok: #4ade80;
    --warn: #facc15;
    --bad: #f87171;
    --pill-bg: #1f2937;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg);
               font: 14px/1.45 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif; }
  header { padding: 18px 24px; border-bottom: 1px solid #1f2937; display: flex;
           align-items: center; gap: 14px; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: 0.2px; }
  .pulse { width: 10px; height: 10px; border-radius: 50%; background: var(--ok);
           box-shadow: 0 0 0 0 var(--ok); animation: pulse 2s infinite; }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(74,222,128,0.7); }
    70% { box-shadow: 0 0 0 12px rgba(74,222,128,0); }
    100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); }
  }
  header .ts { color: var(--muted); margin-left: auto; font-size: 12px; }
  main { padding: 24px; max-width: 1400px; margin: 0 auto; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
  .card { background: var(--panel); border: 1px solid #1f2937; border-radius: 10px;
          padding: 16px; }
  .card h2 { margin: 0 0 12px 0; font-size: 13px; text-transform: uppercase;
             letter-spacing: 1.2px; color: var(--muted); display: flex; align-items: center;
             gap: 8px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px;
           font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; }
  .badge.ok   { background: rgba(74,222,128,0.15); color: var(--ok); }
  .badge.warn { background: rgba(250,204,21,0.15); color: var(--warn); }
  .badge.bad  { background: rgba(248,113,113,0.15); color: var(--bad); }
  .badge.off  { background: var(--pill-bg); color: var(--muted); }
  .row { display: flex; justify-content: space-between; gap: 12px;
         padding: 6px 0; border-bottom: 1px dashed #1f2937; }
  .row:last-child { border-bottom: none; }
  .row .k { color: var(--muted); }
  .row .v { color: var(--fg); font-variant-numeric: tabular-nums; text-align: right;
            word-break: break-all; }
  pre { background: var(--panel-2); border-radius: 6px; padding: 10px;
        font-size: 11px; overflow-x: auto; color: var(--muted); max-height: 260px; }
  .kpi { display: grid; gap: 6px; grid-template-columns: repeat(3, 1fr); margin-bottom: 8px; }
  .kpi .b { background: var(--panel-2); border-radius: 6px; padding: 10px;
            text-align: center; }
  .kpi .b .n { font-size: 22px; font-weight: 700; color: var(--accent); }
  .kpi .b .l { font-size: 10px; color: var(--muted); text-transform: uppercase;
               letter-spacing: 0.8px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #1f2937;
           color: var(--fg); font-variant-numeric: tabular-nums; }
  th { color: var(--muted); font-weight: 500; font-size: 10px;
       text-transform: uppercase; letter-spacing: 0.8px; }
  .footer { text-align: center; padding: 24px; color: var(--muted); font-size: 11px; }
  button { background: var(--accent); color: #04101a; border: 0; padding: 6px 12px;
           border-radius: 6px; font-weight: 600; cursor: pointer; }
  .err { color: var(--bad); font-size: 11px; }
</style>
</head>
<body>
<header>
  <div class="pulse" id="pulse"></div>
  <h1>OpenJarvis · Agentic Stack</h1>
  <span class="badge ok" id="globalBadge">LIVE</span>
  <span class="ts" id="ts">—</span>
  <button onclick="refresh()">Refresh now</button>
</header>
<main>
  <div class="grid" id="grid"></div>
</main>
<div class="footer">Auto-refresh every 15s · <code>/v1/_debug/agentic</code></div>

<script>
const FLAGS = [
  "OPENJARVIS_SKILLS_AUTOLOAD_ENABLED",
  "OPENJARVIS_ORCHESTRATOR_ENABLED",
  "OPENJARVIS_ORCHESTRATOR_MODE",
  "OPENJARVIS_DISTILLER_ONLINE_ENABLED",
  "OPENJARVIS_REFLECTOR_ENABLED",
  "OPENJARVIS_GOALS_ENABLED",
  "OPENJARVIS_SKILL_PLANNER_ENABLED",
  "OPENJARVIS_PROMPT_EVOLVER_ENABLED",
  "OPENJARVIS_ANSWER_CACHE_ENABLED",
  "OPENJARVIS_WATCHDOG_ENABLED",
  "OPENJARVIS_MODEL_PREFERENCE_ENABLED",
  "OPENJARVIS_COST_TELEMETRY_ENABLED",
];

function flagBadge(v) {
  const on = ["true","1","yes","on"].includes(String(v||"").toLowerCase());
  if (on) return '<span class="badge ok">ON</span>';
  if (String(v||"") === "<unset>") return '<span class="badge off">unset</span>';
  return '<span class="badge off">OFF</span>';
}
function pill(state) {
  const s = String(state||"").toLowerCase();
  if (["ok","live","green"].includes(s)) return '<span class="badge ok">'+s.toUpperCase()+'</span>';
  if (["warn","watch","warming-up"].includes(s)) return '<span class="badge warn">'+s.toUpperCase()+'</span>';
  if (["bad","degraded","fail","error"].includes(s)) return '<span class="badge bad">'+s.toUpperCase()+'</span>';
  return '<span class="badge off">'+(s||"-").toUpperCase()+'</span>';
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderCards(data) {
  const cards = [];

  // 1. Env flags
  const envRows = FLAGS.map(f =>
    '<div class="row"><span class="k">'+escapeHtml(f.replace("OPENJARVIS_","").toLowerCase())+
    '</span><span class="v">'+flagBadge(data.env && data.env[f])+'</span></div>'
  ).join("");
  cards.push('<div class="card"><h2>Env flags</h2>'+envRows+'</div>');

  // 2. Skills
  const s = data.skills || {};
  cards.push('<div class="card"><h2>Skills '+pill(s.loaded ? "ok" : "warn")+'</h2>'+
    '<div class="kpi"><div class="b"><div class="n">'+(s.count||0)+'</div><div class="l">loaded</div></div></div>'+
    '<div class="row"><span class="k">sample</span><span class="v">'+escapeHtml((s.sample||[]).slice(0,3).join(", "))+'</span></div>'+
    (s.error ? '<div class="err">'+escapeHtml(s.error)+'</div>' : '') +
    '</div>');

  // 3. Reflector
  const r = data.reflector || {};
  const rOk = r.sync_test_ok;
  cards.push('<div class="card"><h2>Reflector '+pill(rOk ? "ok" : (r.enabled ? "warn" : "off"))+'</h2>'+
    (r.score ? ('<div class="kpi">'+
      '<div class="b"><div class="n">'+(r.score.confidence||0).toFixed(2)+'</div><div class="l">conf</div></div>'+
      '<div class="b"><div class="n">'+(r.score.success ? "Y" : "N")+'</div><div class="l">success</div></div>'+
      '<div class="b"><div class="n">'+(r.elapsed_ms||0)+'</div><div class="l">ms</div></div>'+
      '</div>') : '')+
    (r.score && r.score.learning ? '<div class="row"><span class="k">learning</span><span class="v">'+escapeHtml(r.score.learning)+'</span></div>' : '')+
    (r.error ? '<div class="err">'+escapeHtml(r.error)+'</div>' : '') +
    '</div>');

  // 4. Goal tracker
  const g = data.goal_tracker || {};
  cards.push('<div class="card"><h2>Goal tracker '+pill(g.round_trip_ok ? "ok" : (g.enabled ? "warn" : "off"))+'</h2>'+
    '<div class="kpi"><div class="b"><div class="n">'+(g.list_count||0)+'</div><div class="l">active</div></div>'+
    '<div class="b"><div class="n">'+(g.round_trip_ok ? "OK" : "FAIL")+'</div><div class="l">round-trip</div></div></div>'+
    (g.error ? '<div class="err">'+escapeHtml(g.error)+'</div>' : '') +
    '</div>');

  // 5. Skill planner
  const sp = data.skill_planner || {};
  cards.push('<div class="card"><h2>Skill planner '+pill(sp.test_match ? "ok" : (sp.enabled ? "warn" : "off"))+'</h2>'+
    (sp.test_match ? ('<div class="row"><span class="k">match</span><span class="v">'+escapeHtml(sp.test_match.skill_name)+' @ '+sp.test_match.score+'</span></div>') :
     '<div class="row"><span class="k">match</span><span class="v">none</span></div>')+
    (sp.error ? '<div class="err">'+escapeHtml(sp.error)+'</div>' : '') +
    '</div>');

  // 6. Orchestrator
  const o = data.orchestrator || {};
  cards.push('<div class="card"><h2>Orchestrator '+pill(o.clean_responses > 0 ? "ok" : (o.enabled ? "warn" : "off"))+'</h2>'+
    '<div class="kpi">'+
      '<div class="b"><div class="n">'+(o.clean_responses||0)+'/'+(o.responses||0)+'</div><div class="l">clean</div></div>'+
      '<div class="b"><div class="n">'+(o.elapsed_ms||0)+'</div><div class="l">ms</div></div>'+
      '<div class="b"><div class="n">'+escapeHtml(o.mode || "-")+'</div><div class="l">mode</div></div>'+
    '</div>'+
    (o.winner ? ('<div class="row"><span class="k">winner</span><span class="v">'+escapeHtml(o.winner.model||"?")+'</span></div>') : '')+
    (o.all_providers ? ('<div class="row"><span class="k">providers</span><span class="v">'+escapeHtml((o.all_providers||[]).slice(0,4).join(", "))+'</span></div>') : '')+
    (o.error ? '<div class="err">'+escapeHtml(o.error)+'</div>' : '') +
    '</div>');

  // 7. Distiller
  const d = data.distiller || {};
  cards.push('<div class="card"><h2>Distiller '+pill(d.import_ok ? "ok" : (d.enabled ? "warn" : "off"))+'</h2>'+
    '<div class="row"><span class="k">import_ok</span><span class="v">'+(d.import_ok ? "yes" : "no")+'</span></div>'+
    (d.error ? '<div class="err">'+escapeHtml(d.error)+'</div>' : '') +
    '</div>');

  // 8. Prompt evolver
  const e = data.prompt_evolver || {};
  cards.push('<div class="card"><h2>Prompt evolver '+pill(e.enabled ? "ok" : "off")+'</h2>'+
    (e.note ? '<div class="row"><span class="k">note</span><span class="v">'+escapeHtml(e.note)+'</span></div>' : '')+
    (e.error ? '<div class="err">'+escapeHtml(e.error)+'</div>' : '') +
    '</div>');

  // 9. Watchdog
  const w = data.watchdog || {};
  const wState = w.state || (w.enabled ? "warming-up" : "off");
  cards.push('<div class="card"><h2>Watchdog '+pill(wState)+'</h2>'+
    '<div class="kpi">'+
      '<div class="b"><div class="n">'+(w.avg_confidence||0)+'</div><div class="l">avg conf</div></div>'+
      '<div class="b"><div class="n">'+((w.success_rate||0)*100).toFixed(0)+'%</div><div class="l">success</div></div>'+
      '<div class="b"><div class="n">'+(w.samples||0)+'</div><div class="l">window</div></div>'+
    '</div>'+
    '<div class="row"><span class="k">threshold</span><span class="v">'+(w.threshold||"-")+'</span></div>'+
    '<div class="row"><span class="k">autorollback</span><span class="v">'+(w.autorollback_enabled ? "yes" : "no")+'</span></div>'+
    ((w.rollback_history||[]).length ? ('<div class="row"><span class="k">last rollback</span><span class="v">'+escapeHtml(w.rollback_history[w.rollback_history.length-1].flag)+'</span></div>') : '')+
    '</div>');

  // 10. Answer cache
  const ac = data.answer_cache || {};
  cards.push('<div class="card"><h2>Answer cache '+pill(ac.enabled ? "ok" : "off")+'</h2>'+
    '<div class="kpi">'+
      '<div class="b"><div class="n">'+(ac.size||0)+'</div><div class="l">entries</div></div>'+
      '<div class="b"><div class="n">'+(ac.total_hits||0)+'</div><div class="l">total hits</div></div>'+
      '<div class="b"><div class="n">'+Object.keys(ac.by_domain||{}).length+'</div><div class="l">domains</div></div>'+
    '</div>'+
    ((ac.top_hits||[]).length ?
      '<table><tr><th>query</th><th>hits</th></tr>'+
      ac.top_hits.map(h => '<tr><td>'+escapeHtml((h.query||"").slice(0,50))+'</td><td>'+(h.hits||0)+'</td></tr>').join("")+
      '</table>' : '') +
    '</div>');

  // 11. Model preference
  const mp = data.model_preference || {};
  let mpTable = '';
  if (mp.top_per_domain) {
    for (const dom of Object.keys(mp.top_per_domain)) {
      const rows = (mp.top_per_domain[dom] || []).slice(0,3);
      if (rows.length) {
        mpTable += '<table><tr><th colspan="3">'+escapeHtml(dom)+'</th></tr>';
        for (const r of rows) {
          mpTable += '<tr><td>'+escapeHtml((r.model||"").slice(0,28))+'</td><td>'+r.score+'</td><td>n='+r.samples+'</td></tr>';
        }
        mpTable += '</table>';
      }
    }
  }
  cards.push('<div class="card"><h2>Model preference '+pill(mp.enabled ? "ok" : "off")+'</h2>'+
    '<div class="kpi"><div class="b"><div class="n">'+(mp.total_observations||0)+'</div><div class="l">obs</div></div>'+
    '<div class="b"><div class="n">'+(mp.domains_tracked||0)+'</div><div class="l">domains</div></div></div>'+
    mpTable+
    '</div>');

  // 12. Cost telemetry
  const c = data.cost_telemetry || {};
  let costTable = '';
  if ((c.per_provider||[]).length) {
    costTable = '<table><tr><th>provider</th><th>calls</th><th>$</th><th>ms</th></tr>'+
      c.per_provider.slice(0,6).map(p =>
        '<tr><td>'+escapeHtml((p.provider||"?")+(p.model?":"+p.model.slice(0,20):""))+'</td>'+
        '<td>'+p.calls+'</td><td>'+(p.cost_usd||0).toFixed(4)+'</td><td>'+p.avg_latency_ms+'</td></tr>'
      ).join("")+'</table>';
  }
  cards.push('<div class="card"><h2>Cost telemetry '+pill(c.enabled ? "ok" : "off")+'</h2>'+
    '<div class="kpi">'+
      '<div class="b"><div class="n">'+(c.total_calls||0)+'</div><div class="l">calls</div></div>'+
      '<div class="b"><div class="n">$'+(c.total_cost_usd||0).toFixed(3)+'</div><div class="l">cost</div></div>'+
      '<div class="b"><div class="n">'+((c.total_tokens_in||0)+(c.total_tokens_out||0))+'</div><div class="l">tokens</div></div>'+
    '</div>'+
    costTable+
    '</div>');

  document.getElementById("grid").innerHTML = cards.join("");
}

async function refresh() {
  try {
    const r = await fetch('/v1/_debug/agentic', { cache: 'no-store', credentials: 'same-origin' });
    if (!r.ok) throw new Error(r.status + " " + r.statusText);
    const data = await r.json();
    document.getElementById('ts').textContent = data._timestamp + ' · ' + (data._elapsed_ms||0) + 'ms';
    renderCards(data);
    const bad = (data.reflector && data.reflector.error) || (data.orchestrator && data.orchestrator.error);
    const badge = document.getElementById('globalBadge');
    badge.className = 'badge ' + (bad ? 'bad' : 'ok');
    badge.textContent = bad ? 'DEGRADED' : 'LIVE';
  } catch (e) {
    const badge = document.getElementById('globalBadge');
    badge.className = 'badge bad';
    badge.textContent = 'OFFLINE: ' + e.message;
  }
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


@router.get("/v1/_debug/dashboard", response_class=HTMLResponse)
async def debug_dashboard() -> HTMLResponse:
    return HTMLResponse(content=_DASHBOARD_HTML, status_code=200)
