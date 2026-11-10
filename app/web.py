"""Batch PDF page: one tab per balance (?b=left / ?b=centre) listing the batches in its
press log, makes the weight-of-tablets PDF.

Started by run.py (live-balance-start.bat); on its own: python app/web.py, then open http://localhost:8086/
Its own process (standard library web server), independent of the recorder. Other PCs and
phones on the network can open http://<this-pc>:8086/ too. Shows the recorder's live
picture and status (data/live/<balance>/) in its Live panel.

Every PDF made is remembered in data/reports/prints.json (balance, cycles, start position,
where the sheet ended, product), so the page can pre-tick the cycles not yet in a PDF and
suggest the position to continue a part-printed sheet at.
"""
from __future__ import annotations

import html
import importlib
import json
import logging
import re
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from dotenv import dotenv_values

import balance
import report

ROOT = Path(__file__).resolve().parents[1]  # project folder (this file is in app/)
HOST, PORT = "0.0.0.0", 8086   # 127.0.0.1 = this PC only
REPORT_DIR = ROOT / "data" / "reports"
PRINTS_JSON = "prints.json"    # in REPORT_DIR
IN_PROGRESS_SEC = 600          # an unfinished cycle pressed this recently is still being weighed
PRODUCT_LIST = Path("//Home-pc/f/BATCHDTL/BATCHDTL.csv")  # batch number -> product name, on another PC
PRODUCT_EVERY_SEC = 60

log = logging.getLogger("web")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Batch Reports</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --ink:#1d2433; --muted:#5f6b7a; --line:#d8dde5; --soft:#eef1f5;
          --accent:#1f5fbf; --accent-soft:#dbe7fb; --ok:#1b7f45; --ok-soft:#dcf2e4; --warn:#9a5800;
          --err:#b3261e; --err-soft:#fbe1df; --focus:#1f5fbf; }
  @media (prefers-color-scheme: dark) { :root { --bg:#14171c; --card:#1d2128; --ink:#e7ebf1;
          --muted:#9aa4b2; --line:#323946; --soft:#262b34; --accent:#6ea2ff; --accent-soft:#22324d;
          --ok:#4cc38a; --ok-soft:#1d3a2b; --warn:#f0a841; --err:#ff7b72; --err-soft:#45201f; --focus:#6ea2ff; } }
  * { box-sizing:border-box }
  body { margin:0; font:15px/1.45 system-ui, "Segoe UI", sans-serif; background:var(--bg); color:var(--ink) }
  :focus-visible { outline:2px solid var(--focus); outline-offset:2px }
  .wrap { max-width:1440px; margin:auto; padding:16px }
  /* wide screens: camera + status on the left, batch list on the right scrolling on its own */
  main { display:grid; grid-template-columns:320px minmax(0,1fr); gap:16px; margin-top:16px; align-items:start }
  .side { display:grid; gap:14px }
  .batches { display:flex; flex-direction:column; gap:14px; container-type:inline-size }
  .batches > * { flex:none }  /* never squash a card to fit; the list scrolls instead */
  @media (min-width:900px) {
    .wrap { height:100vh; display:flex; flex-direction:column }
    main { flex:1; min-height:0; grid-template-rows:minmax(0, 1fr) }  /* row = the window height left */
    .side, .batches { height:100%; overflow-y:auto }
    .batches { padding-right:4px }
  }
  @media (max-width:899px) { main { grid-template-columns:1fr } }
  header.top { display:flex; flex-wrap:wrap; gap:12px; align-items:end; justify-content:space-between }
  h1 { font-size:22px; margin:0 } .sub { margin:2px 0 0; color:var(--muted); font-size:14px }
  .tabs { display:flex; gap:4px; margin:8px 0 0; padding:3px; border:1px solid var(--line); border-radius:10px;
          background:var(--card); width:max-content; max-width:100% }
  .tabs a { padding:6px 16px; border-radius:7px; color:var(--ink); text-decoration:none; font-weight:600; white-space:nowrap }
  .tabs a small { font-weight:400; color:var(--muted); margin-left:6px; font-variant-numeric:tabular-nums }
  .tabs a:hover { background:var(--soft) }
  .tabs a[aria-current] { background:var(--accent); color:#fff } .tabs a[aria-current] small { color:inherit; opacity:.85 }
  @media (max-width:520px) { .tabs { width:100% } .tabs a { flex:1; padding:5px 4px; text-align:center; line-height:1.2 }
                             .tabs a small { display:block; margin:0 } }
  .lan { font-size:13px } .lan a { color:var(--accent); font-variant-numeric:tabular-nums }
  .search { flex:0 1 280px; padding:9px 12px; font-size:15px; border:1px solid var(--line); border-radius:8px;
            background:var(--card); color:var(--ink) }
  .banner { margin:12px 0 0; padding:10px 14px; border-radius:8px; background:var(--err-soft); color:var(--err); font-weight:600 }
  .banner.ok { background:var(--ok-soft); color:var(--ok) }
  .cact { margin:6px 0 0 32px }
  .del { background:none; border:0; padding:2px 0; color:var(--muted); font:inherit; font-size:12.5px; cursor:pointer;
         text-decoration:underline }
  .del:hover, .del:focus-visible { color:var(--err) }
  .deldlg { width:min(92vw, 440px); border:1px solid var(--line); border-radius:12px; padding:18px 20px;
            background:var(--card); color:var(--ink) }
  .deldlg::backdrop { background:rgba(0,0,0,.55) }
  .deldlg h2 { margin:0 0 8px; font-size:18px; color:var(--err) }
  .deldlg p { margin:0 0 10px; font-size:14px } .del-info { color:var(--muted) }
  .del-what { font-weight:600; font-variant-numeric:tabular-nums }
  .deldlg label { display:block; font-size:13px; color:var(--muted); margin:12px 0 4px }
  .deldlg input[name=confirm] { width:100%; padding:9px 10px; font-size:15px; border:1px solid var(--line); border-radius:8px;
                                background:var(--bg); color:var(--ink) }
  .del-btns { display:flex; gap:10px; justify-content:flex-end; margin-top:16px }
  .del-btns button { padding:9px 16px; border-radius:8px; font:inherit; font-weight:600; cursor:pointer;
                     border:1px solid var(--line); background:var(--card); color:var(--ink) }
  .del-btns .del-go { background:var(--err); border-color:var(--err); color:#fff }
  .del-btns .del-go:disabled { background:var(--line); border-color:var(--line); color:var(--muted); cursor:not-allowed }
  details.batch { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:clip }  /* clip, not hidden: keeps the sticky panel working */
  summary { list-style:none; cursor:pointer; padding:14px 16px; display:flex; flex-wrap:wrap; gap:6px 14px; align-items:center }
  summary::-webkit-details-marker { display:none }
  summary::before { content:""; width:8px; height:8px; border-right:2px solid var(--muted); border-bottom:2px solid var(--muted);
                    transform:rotate(-45deg); transition:transform .15s; margin-right:2px }
  details[open] > summary::before { transform:rotate(45deg) }
  .bno { font:700 22px/1.2 ui-monospace, Consolas, monospace; letter-spacing:1px }
  .meta { color:var(--muted); font-size:13px }
  .state { margin-left:auto }
  .chip { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12.5px; font-weight:600; white-space:nowrap;
          font-variant-numeric:tabular-nums }
  .chip.ok { background:var(--ok-soft); color:var(--ok) } .chip.bad { background:var(--err-soft); color:var(--err) }
  .chip.live { background:var(--accent-soft); color:var(--accent) } .chip.done { background:var(--soft); color:var(--muted) }
  .body { display:grid; grid-template-columns:minmax(0,1fr) 300px; border-top:1px solid var(--line) }
  @container (min-width:641px) { .panel { position:sticky; top:0; align-self:start } }  /* Make PDF stays in view */
  @container (max-width:640px) { .body { grid-template-columns:1fr } .panel { border-left:0 !important; border-top:1px solid var(--line) } }
  .cycles { padding:4px 0 }
  .picks { padding:8px 16px 6px; font-size:13px; color:var(--muted) }
  .picks button { background:none; border:0; padding:2px 4px; color:var(--accent); font:inherit; cursor:pointer; text-decoration:underline }
  .cycle { padding:10px 16px; border-top:1px solid var(--soft) }
  .cycle:hover { background:var(--soft) } .cycle.locked:hover { background:none }
  .crow { display:grid; grid-template-columns:22px 1fr auto; gap:2px 10px; align-items:start; cursor:pointer }
  .cycle.locked .crow { cursor:default }
  .crow input { width:18px; height:18px; margin:2px 0 0; accent-color:var(--accent) }
  .cname { font-weight:600 } .ctime { color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums }
  .cside { text-align:right; display:grid; gap:3px; justify-items:end }
  .inpdf { font-size:12px; color:var(--muted) }
  .notes { margin:4px 0 0 32px; padding:0; list-style:none; font-size:13px; color:var(--warn) }
  .notes li::before { content:"\\26A0\\FE0E  " }
  .anyway { margin:4px 0 0 32px; font-size:13px; color:var(--warn); display:flex; gap:6px; align-items:center; cursor:pointer; width:max-content }
  .anyway input { width:15px; height:15px; margin:0 }
  .panel { padding:14px 16px; border-left:1px solid var(--line); background:var(--soft); display:grid; gap:12px; align-content:start }
  .panel label.f { display:block; font-size:13px; color:var(--muted); margin-bottom:4px }
  .panel input[type=text] { width:100%; padding:9px 10px; font-size:15px; border:1px solid var(--line); border-radius:8px;
                            background:var(--card); color:var(--ink) }
  .panel input[readonly] { background:var(--soft); color:var(--muted); cursor:default }
  .seg { display:flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; background:var(--card) }
  .seg label { position:relative; flex:1; text-align:center; padding:7px 0; cursor:pointer; font-weight:600; font-variant-numeric:tabular-nums;
               border-left:1px solid var(--line) }
  .seg label:first-child { border-left:0 }
  .seg input { position:absolute; opacity:0; pointer-events:none }
  .seg label:has(input:checked) { background:var(--accent); color:#fff }
  .seg label:has(input:focus-visible) { outline:2px solid var(--focus); outline-offset:-3px }
  .hint { font-size:12.5px; color:var(--muted); margin:4px 0 0 }
  .prev { display:flex; gap:12px; align-items:flex-start }
  .sheet { position:relative; width:96px; height:136px; background:#fff; border:1px solid var(--line); border-radius:3px;
           box-shadow:0 1px 3px rgba(0,0,0,.12); flex:none }
  .sheet .area { position:absolute; left:14%; right:14%; top:10.1%; bottom:6.7% }
  .sheet .blk { position:absolute; left:0; right:0; border-radius:2px; font:600 9px/1 system-ui, sans-serif;
                display:flex; align-items:center; justify-content:center; overflow:hidden }
  .sheet .old { background:repeating-linear-gradient(-45deg,#cfd5de 0 3px,#e6eaf0 3px 6px); color:#5f6b7a }
  .sheet .new { background:#1f5fbf; color:#fff }
  .plegend { font-size:12.5px; color:var(--muted); display:grid; gap:4px }
  .plegend b { color:var(--ink) }
  .go { width:100%; padding:11px; font-size:15px; font-weight:600; border-radius:8px; border:0; background:var(--accent);
        color:#fff; cursor:pointer }
  .go:disabled { background:var(--line); color:var(--muted); cursor:not-allowed }
  .tip { font-size:12px; color:var(--muted); margin:0 }
  .live { display:grid; gap:12px; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px }
  .cam { display:block; width:100%; height:auto; max-height:52vh; object-fit:contain; border-radius:8px; background:var(--soft) }
  .camopen { position:relative; display:block; width:100%; padding:0; border:0; background:none; cursor:zoom-in; border-radius:8px }
  .enlarge { position:absolute; right:8px; bottom:8px; padding:3px 9px; border-radius:999px; font-size:12px; font-weight:600;
             background:rgba(0,0,0,.6); color:#fff; opacity:.85 }
  .camopen:hover .enlarge, .camopen:focus-visible .enlarge { opacity:1 }
  /* large live view: camera fills the screen, details laid over it (camera colours, so fixed dark overlays) */
  .camdlg { padding:0; border:0; background:#000; width:min(96vw, 1200px); height:94vh; max-height:94vh; border-radius:12px; overflow:hidden }
  .camdlg::backdrop { background:rgba(0,0,0,.75) }
  .stage { position:relative; width:100%; height:100%; display:flex; align-items:center; justify-content:center }
  .stage .big { max-width:100%; max-height:100%; object-fit:contain }
  .ov { position:absolute; display:grid; gap:2px; padding:10px 14px; border-radius:10px; background:rgba(0,0,0,.62); color:#fff;
        font-variant-numeric:tabular-nums; max-width:calc(50% - 24px) }
  .ov.tl { top:12px; left:12px } .ov.tr { top:12px; right:56px } .ov.bl { bottom:12px; left:12px } .ov.br { bottom:12px; right:12px; text-align:right }
  .ov .k { font-size:12px; text-transform:uppercase; letter-spacing:.06em; opacity:.75 }
  .ov .v { font:700 clamp(20px, 3vw, 30px)/1.15 ui-monospace, Consolas, monospace; letter-spacing:1px }
  .ov .big-w { font-size:clamp(28px, 5vw, 52px) }
  .ov-press, .ov-switch, .ov-display { font-size:14px }
  .ov-tab { font-size:clamp(16px, 2vw, 22px); margin-bottom:6px } .ov-tab b { font-size:1.35em; font-variant-numeric:tabular-nums }
  .ov .warn { color:#f0a841 } .ov .bad { color:#ff7b72 } .ov .good { color:#4cc38a }
  .ov-live::before { content:""; display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:7px; background:currentColor }
  .close { position:absolute; top:10px; right:10px; width:36px; height:36px; border-radius:50%; border:0; font-size:24px; line-height:1;
           background:rgba(0,0,0,.62); color:#fff; cursor:pointer }
  @media (max-width:600px) { .ov { padding:6px 9px } .ov.tr { top:auto; bottom:84px; right:12px } }
  @media (max-width:899px) { .live { grid-template-columns:minmax(0,248px) 1fr } }
  @media (max-width:520px) { .live { grid-template-columns:1fr } .cam { max-width:320px } }
  .health { min-width:0 }
  .health h2 { font-size:16px; margin:2px 0 8px } .upd { font-weight:400; font-size:12px; color:var(--muted) }
  .checks { list-style:none; margin:0; padding:0; display:grid; gap:6px; font-size:14px }
  .checks li { display:flex; gap:8px; align-items:baseline }
  .checks li::before { content:""; flex:none; width:9px; height:9px; border-radius:50%; background:var(--ok);
                       transform:translateY(1px) }
  .checks li.warn::before { background:var(--warn) } .checks li.bad::before { background:var(--err) }
  .checks li.bad { color:var(--err); font-weight:600 }
  .checks b { font-variant-numeric:tabular-nums }
  .legend { font-size:12px; color:var(--muted); margin:10px 0 0 }
  .uncounted { color:var(--muted); font-size:13px; border:1px dashed var(--line); border-radius:12px; padding:12px 16px }
  .uncounted h2 { font-size:14px; margin:0 0 4px; color:var(--ink) }
  .uncounted p { margin:0 0 6px } .uncounted ul { margin:0; padding-left:18px; font-variant-numeric:tabular-nums }
  .empty { color:var(--muted); background:var(--card); border:1px dashed var(--line); border-radius:12px; padding:28px; text-align:center }
  [hidden] { display:none !important }
</style></head>
<body data-slot="%SLOT%" data-usable="%USABLE%" data-gap="%GAP%" data-bal="%BAL%">
<div class="wrap">
<header class="top">
  <div><h1>Batch reports</h1><p class="sub">%NAME% balance %ID% &middot; weight of tablets, one PDF per print</p>
  %TABS%%LAN%</div>
  <input class="search" type="search" placeholder="Find batch number" aria-label="Find batch number" autocomplete="off">
</header>
%MESSAGE%
<main>%CARDS%</main>
</div>
<script>
const B = document.body.dataset, SLOT = +B.slot, USABLE = +B.usable, GAP = +B.gap, BAL = B.bal;
// the batch list scrolls on its own: keep its place across this page's automatic reloads
const list = document.querySelector('.batches');
try { list.scrollTop = +sessionStorage.getItem('lb_scroll_' + BAL) || 0; } catch {}
addEventListener('beforeunload', () => { try { sessionStorage.setItem('lb_scroll_' + BAL, list.scrollTop); } catch {} });
// same rule as report.place(): a cycle that does not fit goes to the top of the next page
function place(hs, start) {
  let off = (start - 1) * SLOT, page = 1; const spots = [];
  for (const h of hs) { if (off + h > USABLE + 1e-9 && off > 0) { page++; off = 0; } spots.push([page, off]); off += h + GAP; }
  return spots;
}
function update(form) {
  const picked = [...form.querySelectorAll('input[name=cycle]:checked')];
  const start = +form.querySelector('input[name=start]:checked').value;
  const go = form.querySelector('.go');
  go.disabled = !picked.length;
  go.textContent = picked.length ? `Make PDF \\u00b7 ${picked.length} cycle${picked.length > 1 ? 's' : ''}` : 'Tick a cycle to print';
  const area = form.querySelector('.sheet .area'); area.innerHTML = '';
  const k = 100 / USABLE, add = (cls, top, h, text) => {
    const d = document.createElement('div'); d.className = 'blk ' + cls;
    d.style.top = top * k + '%'; d.style.height = Math.max(h * k, 3) + '%'; d.textContent = text; area.append(d); };
  if (start > 1) add('old', 0, (start - 1) * SLOT - GAP, 'printed');
  const spots = place(picked.map(c => +c.dataset.h), start);
  spots.forEach(([p, off], i) => { if (p === 1) add('new', off, +picked[i].dataset.h, 'C' + picked[i].value); });
  const pages = spots.length ? spots[spots.length - 1][0] : 1;
  form.querySelector('.pinfo').innerHTML = !picked.length ? 'Nothing selected'
    : `<b>${pages} page${pages > 1 ? 's' : ''}</b>` + (pages > 1 ? ` &middot; ${spots.filter(s => s[0] > 1).length} cycle(s) continue on a new sheet` : '');
}
document.querySelectorAll('form.batchform').forEach(form => {
  form.addEventListener('change', e => {
    if (e.target.dataset.unlocks) {                      // wrong count: "include anyway" unlocks the cycle
      const c = document.getElementById(e.target.dataset.unlocks);
      c.disabled = !e.target.checked; c.checked = e.target.checked;
      c.closest('.cycle').classList.toggle('locked', !e.target.checked);
    }
    form.dataset.dirty = 1; update(form);
  });
  form.addEventListener('input', () => form.dataset.dirty = 1);
  form.querySelectorAll('[data-pick]').forEach(b => b.addEventListener('click', () => {
    form.querySelectorAll('input[name=cycle]').forEach(c => {
      if (c.disabled) return;
      c.checked = b.dataset.pick === 'all' || (b.dataset.pick === 'new' && c.dataset.new === '1');
    });
    form.dataset.dirty = 1; update(form);
  }));
  form.addEventListener('submit', () => setTimeout(() => location.reload(), 1500)); // show "In PDF" + next position
  update(form);
});
const search = document.querySelector('.search');
search.addEventListener('input', () => {
  const q = search.value.trim().toUpperCase();
  document.querySelectorAll('details.batch').forEach(d => {
    d.hidden = q && !d.dataset.key.toUpperCase().includes(q); if (q && !d.hidden) d.open = true; });
});
search.addEventListener('keydown', e => { if (e.key === 'Escape') { search.value = ''; search.dispatchEvent(new Event('input')); } });
// Live panel: picture + health checks from the recorder, once a second
const cam = document.querySelector('.cam'), checks = document.querySelector('.checks'), upd = document.querySelector('.upd');
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const hhmm = iso => iso ? iso.slice(11, 19) : '';
async function live() {
  let s;
  try { s = await (await fetch('/status.json?b=' + BAL, {cache: 'no-store'})).json(); } catch { s = {age_sec: null}; }
  const items = [];
  const running = s.age_sec !== null && s.age_sec < 10;
  if (!running) {
    items.push(['bad', s.updated ? `Recorder stopped &mdash; last seen ${hhmm(s.updated)}. Presses are NOT being recorded.`
                                 : 'Recorder not running &mdash; presses are NOT being recorded.']);
  } else {
    items.push(['', `Recorder running since ${hhmm(s.started)}`]);
    if (s.frame_age_sec === null || s.frame_age_sec > 10)
      items.push(['bad', s.frame_age_sec === null ? 'Camera: no picture yet' : `Camera: no picture for ${Math.round(s.frame_age_sec)} s`]);
    else items.push(['', 'Camera OK']);
    items.push(s.display_found ? ['', 'Balance display found']
                               : ['warn', 'Balance display not found &mdash; view blocked or balance moved']);
    items.push(s.switch_lit ? ['warn', 'Switch pressed'] : ['', 'Switch ready']);
    items.push(s.batch_error ? ['bad', `Batch reading OFF &mdash; ${esc(s.batch_error)}`]
               : s.batch ? ['', `Batch sheet: <b>${esc(s.batch)}</b>`] : ['warn', 'Batch sheet: not read right now']);
  }
  const p = s.last_press;
  if (p) items.push(['', `Last press ${hhmm(p.time)}: <b>${p.weight ? esc(p.weight) + ' ' + esc(p.unit) : 'no weight'}</b>` +
                         (p.status && !p.status.startsWith('ok') ? ` (${esc(p.status)})` : '') + (p.batch ? ` &middot; ${esc(p.batch)}` : '')]);
  checks.innerHTML = items.map(([c, t]) => `<li class="${c}">${t}</li>`).join('');
  upd.textContent = running ? '' : '(not updating)';
  const src = `/live.jpg?b=${BAL}&t=${Date.now()}`;
  if (running) cam.src = src;
  if (dlg.open) overlay(s, running, src);
}
// large live view: same picture and status, details laid over the camera
const dlg = document.querySelector('.camdlg'), big = dlg.querySelector('.big'), q = c => dlg.querySelector(c);
function overlay(s, running, src) {
  if (running) big.src = src;
  const set = (c, html, cls = '') => { const e = q(c); e.innerHTML = html; e.className = c.slice(1) + (cls ? ' ' + cls : ''); };
  set('.ov-live', running ? `LIVE ${hhmm(s.updated)}` : `STOPPED${s.updated ? ' since ' + hhmm(s.updated) : ''}`, running ? 'good' : 'bad');
  set('.ov-batch', s.batch_error ? 'OFF' : s.batch ? esc(s.batch) : 'not read', s.batch_error ? 'v bad' : s.batch ? 'v' : 'v warn');
  const p = s.last_press;
  set('.ov-tab', `Tablet <b>${s.tablet_no ?? '&ndash;'}</b>`);
  set('.ov-weight', p ? (p.weight ? `${esc(p.weight)} ${esc(p.unit)}` : 'no weight') : '&ndash;', 'v big-w' + (p && !p.weight ? ' warn' : ''));
  set('.ov-press', p ? `${hhmm(p.time)}${p.batch ? ' &middot; ' + esc(p.batch) : ''}` +
                       (p.status && !p.status.startsWith('ok') ? ` &middot; <span class="warn">${esc(p.status)}</span>` : '') : 'no press yet');
  set('.ov-switch', running && s.switch_lit ? 'Switch PRESSED' : 'Switch ready', running && s.switch_lit ? 'bad' : '');
  set('.ov-display', !running ? '' : s.frame_age_sec === null || s.frame_age_sec > 10 ? 'Camera: no picture'
                     : s.display_found ? 'Display found' : 'Display not found', !running ? '' :
                     s.frame_age_sec === null || s.frame_age_sec > 10 ? 'bad' : s.display_found ? 'good' : 'warn');
}
document.querySelector('.camopen').addEventListener('click', () => { dlg.showModal(); live(); });
q('.close').addEventListener('click', () => dlg.close());
dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });   // click outside the picture closes
live(); setInterval(live, 1000);
// delete a cycle: type "delete" to unlock the button (checked again on the server)
const deldlg = document.querySelector('.deldlg'), delIn = deldlg.querySelector('[name=confirm]'), delGo = deldlg.querySelector('.del-go');
document.querySelectorAll('.del').forEach(b => b.addEventListener('click', () => {
  deldlg.querySelector('.del-what').textContent = b.dataset.label;
  deldlg.querySelector('[name=batch]').value = b.dataset.batch;
  deldlg.querySelector('[name=cid]').value = b.dataset.cid;
  deldlg.querySelector('[name=b]').value = BAL;
  delIn.value = ''; delGo.disabled = true; deldlg.showModal(); delIn.focus();
}));
delIn.addEventListener('input', () => delGo.disabled = delIn.value.trim().toLowerCase() !== 'delete');
deldlg.querySelector('.del-cancel').addEventListener('click', () => deldlg.close());
// a cycle is being weighed: refresh to follow it, unless someone is filling in a form or has a dialog open
if (document.querySelector('.chip.live')) setInterval(() => {
  if (!document.querySelector('form[data-dirty]') && !search.value && !dlg.open && !deldlg.open) location.reload(); }, 20000);
</script>
</body></html>"""


def lan_urls() -> list[str]:
    """Addresses other devices on the network can open: by IP and by PC name."""
    if HOST in ("127.0.0.1", "localhost"):
        return []
    # The plant LAN has no internet gateway and this PC has virtual adapters too, so the
    # LAN interface is the one that reaches the camera/NVR; the internet route is a fallback.
    camera = urlparse(dotenv_values(ROOT / ".env")
                      .get("LIVE_BALANCE_RTSP_URL") or "").hostname
    ip = ""
    for target in filter(None, (camera, "8.8.8.8")):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 1))  # no packet is sent: only picks the interface
                ip = s.getsockname()[0]
                break
        except OSError:
            continue
    urls = [f"http://{ip}:{PORT}/"] if ip and not ip.startswith("127.") else []
    return urls + [f"http://{socket.gethostname()}:{PORT}/"]


def load_prints() -> list[dict]:
    try:
        return json.loads((REPORT_DIR / PRINTS_JSON).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []


def save_print(entry: dict) -> None:
    prints = load_prints() + [entry]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_DIR / (PRINTS_JSON + ".tmp")
    tmp.write_text(json.dumps(prints, indent=1), encoding="utf-8")
    tmp.replace(REPORT_DIR / PRINTS_JSON)  # atomic: never a half-written history


def esc(s) -> str:
    return html.escape(str(s))


_products: dict[str, str] = {}  # last good read of PRODUCT_LIST; empty until one worked


def watch_products() -> None:
    """Off the request threads: opening a share whose PC is off can block for many seconds,
    and the page must still answer. A failed read keeps the last good list."""
    global _products
    ok = None
    while True:
        try:
            _products = report.read_product_names(PRODUCT_LIST)
            if ok is not True:
                log.info("Product names: %d batches from %s", len(_products), PRODUCT_LIST)
            ok = True
        except Exception as e:  # share off, file replaced or half written: names are typed by hand instead
            if ok is not False:
                log.warning("Product names not read from %s (%s): using %d names from the last good read",
                            PRODUCT_LIST, e, len(_products))
            ok = False
        time.sleep(PRODUCT_EVERY_SEC)


def cycle_row(i: int, key: str, rep: report.BatchReport, reps: list, live: bool, in_pdf: str | None) -> str:
    n, cid = len(rep.tablets), f"c-{i}-{rep.cycle}"
    first, last = rep.tablets[0].time, rep.tablets[-1].time
    notes = []
    if live:
        chip = f'<span class="chip live">weighing &middot; {n} so far</span>'
    elif rep.complete:
        chip = f'<span class="chip ok">{n}/{rep.expected} &#10003;</span>'
    else:
        chip = f'<span class="chip bad">{n}/{rep.expected} &#10007;</span>'
        notes += rep.hints()
        j = reps.index(rep)  # accidental tare: two short cycles in a row that add up to a full one
        for k in (j - 1, j + 1):
            if 0 <= k < len(reps) and not reps[k].complete and n + len(reps[k].tablets) in report.CYCLE_SIZES:
                lo, hi = sorted((rep.cycle, reps[k].cycle))
                notes.append(f"cycles {lo} and {hi} add up to {n + len(reps[k].tablets)}: balance tared mid-cycle?")
    if not live:
        if blanks := sum(t.weight_mg is None for t in rep.tablets):
            notes.append(f"{blanks} weight(s) to fill in by hand")
        if rep.repeats:
            notes.append(f"{rep.repeats} repeat press(es) not counted")
        for s in rep.nothing_added:
            if s.empty_pan:
                notes.append(f"Press at {s.time:%H:%M:%S} not counted: empty pan ({s.reading_g:.3f} g)")
            else:
                gap = ("" if s.gap_sec is None else
                       f", {int(s.gap_sec // 60)} min {int(s.gap_sec % 60)} s after the previous press"
                       if s.gap_sec >= 60 else f", {s.gap_sec:.0f} s after the previous press")
                notes.append(f"Press at {s.time:%H:%M:%S} not counted: reading "
                             f"{'rose' if s.change_mg >= 0 else 'fell'} {abs(s.change_mg)} mg with no tablet added{gap}")
        if out := len(rep.outside()):
            notes.append(f"{out} tablet(s) outside limits")
    usable = rep.complete and not live
    checked = " checked" if usable and not in_pdf else ""
    disabled = "" if usable else " disabled"
    avg = f"avg {rep.average:.2f} mg" if rep.average is not None else "no reading"
    anyway = ("" if rep.complete or live else
              f'<label class="anyway"><input type="checkbox" data-unlocks="{cid}"> include anyway</label>')
    return f"""<div class="cycle{'' if usable else ' locked'}">
<label class="crow" for="{cid}">
  <input type="checkbox" name="cycle" value="{rep.cycle}" id="{cid}" data-h="{report.block_mm(rep)}"
         data-new="{0 if in_pdf else 1}"{checked}{disabled}>
  <span><span class="cname">Cycle {rep.cycle}</span><br>
    <span class="ctime">{first:%d-%m-%Y} &middot; {first:%H:%M}&ndash;{last:%H:%M} &middot; {avg}</span></span>
  <span class="cside">{chip}{f'<span class="inpdf">In PDF {esc(in_pdf)}</span>' if in_pdf else ''}</span>
</label>
{('<ul class="notes">' + ''.join(f'<li>{esc(x)}</li>' for x in notes) + '</ul>') if notes else ''}
{anyway}
{'' if live else f'''<div class="cact"><button type="button" class="del" data-batch="{esc(key)}" data-cid="{report.cycle_id(rep)}"
  data-label="{esc(f"{key} - Cycle {rep.cycle} - {first:%d-%m-%Y %H:%M}-{last:%H:%M} - {n} tablets")}">Delete cycle</button></div>'''}
</div>"""


def card(i: int, key: str, rows: list[dict], prints: list[dict], now: datetime, scale: balance.Scale) -> str:
    reps = report.build_cycles(key, rows)
    if not reps:
        return ""
    # prints from before there was more than one balance have no "balance": all centre
    mine = [p for p in prints if p.get("batch") == key and p.get("balance", "centre") == scale.key]
    made = {}  # cycle id -> when it was last put in a PDF
    for p in mine:
        for c in p.get("cycles", []):
            made[c] = p["time"]
    last_print = mine[-1] if mine else None
    lastrep = reps[-1]
    live_last = (not lastrep.complete and (now - lastrep.tablets[-1].time).total_seconds() < IN_PROGRESS_SEC)
    rows_html, pending = [], 0
    for rep in reps:
        live = rep is lastrep and live_last
        when = made.get(report.cycle_id(rep))
        when = datetime.fromisoformat(when).strftime("%d-%m %H:%M") if when else None
        pending += rep.complete and not live and not when
        rows_html.append(cycle_row(i, key, rep, reps, live, when))

    nxt = last_print["next"] if last_print else 1
    nxt = nxt if 1 <= nxt <= report.START_POSITIONS else 1  # PDFs made before a layout change
    if last_print:
        nums = last_print.get("numbers", [])
        hint = (f"Last PDF ({datetime.fromisoformat(last_print['time']):%d-%m %H:%M}): "
                f"cycle{'s' if len(nums) > 1 else ''} {', '.join(map(str, nums))} from position "
                f"{last_print['start']}. "
                + ("That sheet is full: start a new one." if nxt == 1 else
                   f"Continue on that sheet at position {nxt}, or choose 1 for a new sheet."))
    else:
        hint = (f"1 = top of a new sheet. 2-{report.START_POSITIONS} continue a part-printed sheet "
                "below the cycles on it.")
    seg = "".join(f'<label><input type="radio" name="start" value="{n}"{" checked" if n == nxt else ""}>{n}</label>'
                  for n in range(1, report.START_POSITIONS + 1))
    product = esc(last_print.get("product", "")) if last_print else ""
    listed = _products.get(key)
    product_field = (
        f'<input type="text" id="p-{i}" value="{esc(listed)}" readonly>'
        '<p class="hint">From the batch list.</p>' if listed else
        f'<input type="text" id="p-{i}" name="product" maxlength="60" autocomplete="off" value="{product}" '
        'placeholder="optional"><p class="hint">Not in the batch list: type it here.</p>')

    title = f'<span class="bno">{esc(key)}</span>'
    d0, d1 = reps[0].tablets[0].time, lastrep.tablets[-1].time
    days = f"{d0:%d-%m-%Y}" if d0.date() == d1.date() else f"{d0:%d-%m}&ndash;{d1:%d-%m-%Y}"
    if live_last:
        state = '<span class="chip live">weighing now</span>'
    elif pending:
        state = f'<span class="chip ok">{pending} ready to print</span>'
    else:
        state = '<span class="chip done">all cycles in a PDF</span>'
    is_open = " open" if (i == 0 or pending or live_last) else ""
    return f"""<details class="batch" data-key="{esc(key)}"{is_open}>
<summary>{title}<span class="meta">{len(reps)} cycle{'s' if len(reps) > 1 else ''} &middot; {days} &middot; last press {d1:%H:%M}</span>
  <span class="state">{state}</span></summary>
<form class="batchform" method="get" action="/pdf" target="_blank">
<input type="hidden" name="batch" value="{esc(key)}"><input type="hidden" name="b" value="{scale.key}">
<div class="body">
  <div class="cycles">
    <div class="picks">Select: <button type="button" data-pick="new">not in a PDF</button>
      &middot; <button type="button" data-pick="all">all complete</button>
      &middot; <button type="button" data-pick="none">none</button></div>
    {''.join(rows_html)}
  </div>
  <div class="panel">
    <div><label class="f" for="p-{i}">Product name</label>
      {product_field}</div>
    <div><span class="f" style="font-size:13px;color:var(--muted)" id="s-{i}">Start at position on the sheet</span>
      <div class="seg" role="radiogroup" aria-labelledby="s-{i}">{seg}</div>
      <p class="hint">{esc(hint)}</p></div>
    <div class="prev"><div class="sheet" aria-hidden="true"><div class="area"></div></div>
      <div class="plegend"><span class="pinfo"></span>
        <span>Striped: already on the sheet</span><span>Blue: this print</span></div></div>
    <button class="go" type="submit">Make PDF</button>
    <p class="tip">Print at 100% (Actual size) so positions line up on a part-printed sheet.</p>
  </div>
</div></form></details>"""


def read_live(path: Path) -> bytes:
    """A live file the recorder swaps in every second; on Windows a read at the instant of
    the swap is refused, so retry briefly rather than show 'recorder not running'."""
    for _ in range(5):
        try:
            return path.read_bytes()
        except PermissionError:
            time.sleep(0.02)
    return path.read_bytes()


def live_status(scale: balance.Scale) -> dict:
    """Recorder status for the Live panel; age_sec is measured on this PC, so a viewing
    device with a wrong clock cannot make a running recorder look stopped."""
    path = scale.live_dir / "status.json"
    try:
        status = json.loads(read_live(path))
        status["age_sec"] = round(datetime.now().timestamp() - path.stat().st_mtime, 1)
    except (OSError, ValueError):
        status = {"age_sec": None}
    try:  # tared / empty pan: the next press starts a new cycle, so none weighed yet
        empty = float(status.get("display_weight")) <= 0
    except (TypeError, ValueError):
        empty = False
    status["tablet_no"] = 0 if empty else tablet_count(scale)
    return status


_tablets: dict[str, tuple[int, int | None]] = {}  # balance -> (press log mtime, count)


def tablet_count(scale: balance.Scale) -> int | None:
    """Tablets in the cycle being weighed, recounted only when the press log changes
    (the Live panel asks every second, per viewer)."""
    try:
        mtime = scale.press_csv.stat().st_mtime_ns
    except OSError:
        return None
    if _tablets.get(scale.key, (None,))[0] != mtime:
        _tablets[scale.key] = (mtime, report.latest_tablet_count(scale.press_csv))
    return _tablets[scale.key][1]


LIVE_PANEL = """<section class="live" aria-label="Live view">
  <button type="button" class="camopen" aria-haspopup="dialog" title="Open the large live view">
    <img class="cam" src="/live.jpg?b=%BAL%" alt="Live camera view of the %NAME% balance" width="248" height="352">
    <span class="enlarge">&#x2922; Enlarge</span>
  </button>
  <div class="health">
    <h2>Live <span class="upd"></span></h2>
    <ul class="checks"><li>Loading&hellip;</li></ul>
    <p class="legend">Green box: balance display found &middot; grey box: switch LED (red while pressed) &middot; blue box: batch number read (orange: not accepted yet)</p>
  </div>
</section>
<dialog class="camdlg" aria-label="Large live view of the %NAME% balance">
  <div class="stage">
    <img class="big" alt="Live camera view of the %NAME% balance">
    <div class="ov tl"><span class="k">Batch</span><span class="v ov-batch">&ndash;</span></div>
    <div class="ov tr"><span class="ov-live">&hellip;</span></div>
    <div class="ov bl"><span class="ov-tab">Tablet <b>&ndash;</b></span>
      <span class="k">Last press</span><span class="v big-w ov-weight">&ndash;</span><span class="ov-press"></span></div>
    <div class="ov br"><span class="ov-switch"></span><span class="ov-display"></span></div>
    <button type="button" class="close" aria-label="Close live view">&times;</button>
  </div>
</dialog>"""


def uncounted_note(cycles: list[list[dict]], now: datetime) -> str:
    """Cycles left out because no batch sheet was read during them (page only, never PDF)."""
    items = []
    for cyc in sorted(cycles, key=lambda c: c[0]["press_time"], reverse=True):
        t0, t1 = (datetime.fromisoformat(cyc[i]["press_time"]) for i in (0, -1))
        presses = sum(r["status"] not in report.NOT_TABLETS for r in cyc)
        why = ("being weighed, batch sheet not read yet" if (now - t1).total_seconds() < IN_PROGRESS_SEC
               else "no batch sheet seen")
        items.append(f"<li>{t0:%d-%m-%Y %H:%M}&ndash;{t1:%H:%M} &middot; {presses} press"
                     f"{'es' if presses != 1 else ''} &middot; {why}</li>")
    if not items:
        return ""
    return (f'<section class="uncounted"><h2>Not counted ({len(items)} cycle{"s" if len(items) > 1 else ""})</h2>'
            "<p>No batch number was read from the sheet during these cycles, so they belong to no "
            "batch and are left out of the reports.</p><ul>" + "".join(items) + "</ul></section>")


DELETE_DIALOG = """<dialog class="deldlg" aria-labelledby="del-title">
<form method="post" action="/delete">
  <h2 id="del-title">Delete this cycle?</h2>
  <p class="del-what"></p>
  <p class="del-info">Its presses are moved out of the press log into data/%DELETED%, so it leaves
  every report and PDF. The cycles after it are renumbered.</p>
  <input type="hidden" name="batch"><input type="hidden" name="cid"><input type="hidden" name="b">
  <label for="del-confirm">Type <b>delete</b> to confirm</label>
  <input id="del-confirm" name="confirm" autocomplete="off" spellcheck="false" placeholder="delete">
  <div class="del-btns"><button type="button" class="del-cancel">Cancel</button>
    <button type="submit" class="del-go" disabled>Delete cycle</button></div>
</form>
</dialog>"""


def render(scale: balance.Scale, message: str = "", ok: bool = False) -> str:
    groups, uncounted = report.split_log(scale.press_csv)
    prints, now = load_prints(), datetime.now()
    order = sorted(groups.items(), key=lambda kv: kv[1][-1]["press_time"], reverse=True)
    cards = "".join(card(i, k, rows, prints, now, scale) for i, (k, rows) in enumerate(order))
    cards = (f'<aside class="side">{LIVE_PANEL}</aside><section class="batches" aria-label="Batches">'
             + (cards or '<p class="empty">No batches yet. Weigh with the batch sheet '
                         'beside the balance; batches show up here.</p>')
             + uncounted_note(uncounted, now) + "</section>"
             + DELETE_DIALOG.replace("%DELETED%", scale.deleted_csv.name))
    current = ' aria-current="page"'
    tabs = '<nav class="tabs" aria-label="Balance">' + "".join(
        f'<a href="/?b={s.key}"{current if s is scale else ""}>{s.name}'
        f'<small>{esc("/".join(s.balance_id.split("/")[-2:]))}</small></a>'  # ADR/QC/AB/001 -> AB/001
        for s in balance.SCALES.values()) + "</nav>"
    return (PAGE.replace("%CARDS%", cards).replace("%TABS%", tabs)
            .replace("%BAL%", scale.key).replace("%NAME%", scale.name).replace("%ID%", esc(scale.balance_id))
            .replace("%SLOT%", str(report.SLOT_MM)).replace("%USABLE%", str(report.USABLE_MM))
            .replace("%GAP%", str(report.CYCLE_GAP))
            .replace("%LAN%", '<p class="sub lan">On other devices: ' + " &middot; ".join(
                f'<a href="{esc(u)}">{esc(u)}</a>' for u in lan_urls()) + "</p>" if lan_urls() else "")
            .replace("%MESSAGE%", f'<p class="banner{" ok" if ok else ""}" role="alert">{esc(message)}</p>'
                     if message else ""))


def scale_of(q: dict) -> balance.Scale:
    """The balance a request is about (?b=left); the default one if missing or unknown."""
    b = q.get("b", [balance.DEFAULT])
    return balance.SCALES.get(b[0] if isinstance(b, list) else b, balance.SCALES[balance.DEFAULT])


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, status: int = 200, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        importlib.reload(report)
        url = urlparse(self.path)
        q = parse_qs(url.query)
        scale = scale_of(q)
        if url.path == "/":
            msg, ok = (q["done"][0], True) if "done" in q else (q.get("err", [""])[0], False)
            return self._send(render(scale, msg[:300], ok).encode(), "text/html; charset=utf-8")
        if url.path == "/live.jpg":
            try:
                return self._send(read_live(scale.live_dir / "live.jpg"), "image/jpeg")
            except OSError:
                return self._send(b"no live picture", "text/plain", 404)
        if url.path == "/status.json":
            return self._send(json.dumps(live_status(scale)).encode(), "application/json")
        if url.path != "/pdf":
            return self._send(b"not found", "text/plain", 404)
        key = q.get("batch", [""])[0]
        product = _products.get(key) or q.get("product", [""])[0].strip()[:60]
        rows = report.batches(scale.press_csv).get(key)
        if not rows:
            return self._send(b"unknown batch", "text/plain", 404)
        reps = report.build_cycles(key, rows)
        wanted = {c for c in q.get("cycle", []) if c.isdigit()}
        reps = [r for r in reps if str(r.cycle) in wanted]
        if not reps:
            return self._send(render(scale, "Tick at least one cycle, then Make PDF.").encode(),
                              "text/html; charset=utf-8", 400)
        start = q.get("start", ["1"])[0]
        start = int(start) if start.isdigit() and 1 <= int(start) <= report.START_POSITIONS else 1
        name = re.sub(r"[^A-Za-z0-9-]+", "_", key)  # also keeps the path inside REPORT_DIR
        cycles = "-".join(str(r.cycle) for r in reps)
        out = report.write_pdf(reps, REPORT_DIR / f"batch_{name}{scale.suffix}_cycle_{cycles}.pdf",
                               scale.balance_id, product, start)
        _, nxt = report.place([report.block_mm(r) for r in reps], start)
        save_print({"balance": scale.key, "batch": key, "cycles": [report.cycle_id(r) for r in reps], "numbers": [r.cycle for r in reps],
                    "start": start, "next": nxt, "product": product,
                    "time": datetime.now().isoformat(timespec="seconds"), "file": out.name})
        log.info("PDF %s (%s) cycles %s at position %d -> %s", key, scale.name, cycles, start, out)
        self._send(out.read_bytes(), "application/pdf",
                   extra={"Content-Disposition": f'inline; filename="{out.name}"'})

    def do_POST(self):
        importlib.reload(report)
        if urlparse(self.path).path != "/delete":
            return self._send(b"not found", "text/plain", 404)
        length = min(int(self.headers.get("Content-Length") or 0), 10_000)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
        batch, cid, scale = form.get("batch", ""), form.get("cid", ""), scale_of(form)
        result = {"err": "Not deleted: type delete to confirm."}
        if form.get("confirm", "").strip().lower() == "delete":  # checked here too, not only in the browser
            reps = report.build_cycles(batch, report.batches(scale.press_csv).get(batch, []))
            rep = next((r for r in reps if report.cycle_id(r) == cid), None)
            if rep is None:
                result = {"err": "Not deleted: that cycle is no longer in the log (already deleted?)."}
            elif (not rep.complete and rep is reps[-1]
                  and (datetime.now() - rep.tablets[-1].time).total_seconds() < IN_PROGRESS_SEC):
                result = {"err": f"Not deleted: cycle {rep.cycle} of {batch} is still being weighed."}
            else:
                try:
                    moved = report.delete_cycle(scale.press_csv, batch, cid, scale.deleted_csv,
                                                self.client_address[0])
                except RuntimeError as e:
                    moved, result = 0, {"err": f"Not deleted: {e}."}
                if moved:
                    log.info("DELETED %s (%s) cycle %d (%s, %d presses) from %s", batch, scale.name, rep.cycle,
                             cid, moved, self.client_address[0])
                    result = {"done": f"Deleted cycle {rep.cycle} of {batch} ({moved} presses moved to "
                                      f"data/{scale.deleted_csv.name}). The cycles after it are renumbered."}
        # back to the page: a refresh there cannot repeat the delete
        self.send_response(303)
        self.send_header("Location", "/?" + urlencode({"b": scale.key, **result}))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):  # keep HTTP noise out of the console
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=watch_products, daemon=True, name="product-names").start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("Batch reports: http://localhost:%d/  (Ctrl+C to stop)", PORT)
    for u in lan_urls():
        log.info("  on the network: %s", u)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
