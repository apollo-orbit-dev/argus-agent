"use strict";
(function(){
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var $ = function(id){ return document.getElementById(id); };
  var SESSION = (function(){ try { return localStorage.getItem('argus.session') || "dashboard"; } catch(e){ return "dashboard"; } })();

  // ---- escaping / formatting helpers ----
  function esc(s){
    return String(s).replace(/[&<>"']/g, function(c){
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
    });
  }
  function pretty(v){ try { return JSON.stringify(v, null, 2); } catch(e){ return String(v); } }
  function nfmt(x){ return (x||0).toLocaleString(); }
  function truncate(s, n){ s = String(s); return s.length > n ? s.slice(0, n-1) + '…' : s; }
  function fmtTs(ts){
    if (typeof ts !== 'number') return '';
    var d = new Date(ts*1000);
    if (isNaN(d)) return '';
    return d.toLocaleTimeString([], {hour12:false}) + '.' + String(d.getMilliseconds()).padStart(3,'0');
  }
  function relTime(ts){
    var diff = Math.max(0, Date.now() - ts);
    var m = Math.floor(diff/60000);
    if (m < 1) return 'now';
    if (m < 60) return m + 'm';
    var h = Math.floor(m/60);
    if (h < 24) return h + 'h';
    return Math.floor(h/24) + 'd';
  }
  function elapsedLabel(startedAt){
    var s = Math.floor((Date.now() - startedAt)/1000);
    if (s < 60) return s + 's';
    var m = Math.floor(s/60); s = s%60;
    return m + 'm ' + String(s).padStart(2,'0') + 's';
  }
  function fmtBytes(n){ return n < 1024 ? n + ' B' : (n/1024).toFixed(1) + ' KB'; }
  function fmtWhen(v){
    if (!v) return '—';
    var d = new Date(v);
    return isNaN(d) ? String(v) : d.toLocaleString([], {hour12:false});
  }

  // ---- toasts ----
  var toastStack = $('toastStack');
  function toast(msg, variant){
    var el = document.createElement('div');
    el.className = 'toast' + (variant ? ' ' + variant : '');
    el.textContent = msg;
    toastStack.appendChild(el);
    setTimeout(function(){
      el.classList.add('out');
      setTimeout(function(){ el.remove(); }, 220);
    }, 3200);
  }
  function flashOk(id){
    var el = $(id); if (!el) return;
    el.classList.add('show');
    clearTimeout(el._t); el._t = setTimeout(function(){ el.classList.remove('show'); }, 1800);
  }

  // ---- Admin auth: monkey-patch fetch to inject X-Admin-Token, prompt on 401 ----
  var ADMIN_KEY = "argus_admin_token";
  var _fetch = window.fetch.bind(window);
  window.fetch = function(url, opts){
    opts = opts || {};
    var t = localStorage.getItem(ADMIN_KEY);
    if (t) opts.headers = Object.assign({}, opts.headers, { "X-Admin-Token": t });
    return _fetch(url, opts).then(function(r){
      if (r.status === 401){
        var nt = prompt("This endpoint requires the admin token (ADMIN_TOKEN):");
        if (nt) { localStorage.setItem(ADMIN_KEY, nt); location.reload(); }
      }
      return r;
    });
  };

  /* ---------------- Modals ---------------- */
  function openModal(id){ var m = $(id); if (m) m.classList.add('open'); }
  function closeAllModals(){ document.querySelectorAll('.modal-overlay.open').forEach(function(m){ m.classList.remove('open'); }); }
  document.addEventListener('click', function(e){
    var opener = e.target.closest('[data-open-modal]');
    if (opener) { openModal(opener.getAttribute('data-open-modal')); return; }
    if (e.target.closest('[data-close-modal]')) { closeAllModals(); return; }
    if (e.target.classList.contains('modal-overlay')) closeAllModals();
  });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') closeAllModals(); });
  document.addEventListener('keydown', function(e){
    if (e.key !== 'Tab') return;
    var openModalEl = document.querySelector('.modal-overlay.open');
    if (!openModalEl) return;
    var focusables = Array.prototype.filter.call(
      openModalEl.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),
      function(el){ return !el.disabled && el.offsetParent !== null; }
    );
    if (!focusables.length) return;
    var first = focusables[0], last = focusables[focusables.length-1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  /* ---------------- Shared confirm-delete dialog ---------------- */
  var confirmModal = $('confirmModal');
  var confirmTitleEl = $('confirmTitle');
  var confirmMessageEl = $('confirmMessage');
  var confirmTextField = $('confirmTextField');
  var confirmTextLabel = $('confirmTextLabel');
  var confirmTextInput = $('confirmTextInput');
  var confirmCancelBtn = $('confirmCancelBtn');
  var confirmDeleteBtn = $('confirmDeleteBtn');
  var confirmPending = null;
  function confirmDelete(opts){
    opts = opts || {};
    confirmPending = opts;
    confirmTitleEl.textContent = opts.title || 'Delete';
    confirmMessageEl.textContent = opts.message || "Are you sure? This can't be undone.";
    var danger = opts.danger !== false; // non-destructive callers (e.g. "New session") pass danger:false
    confirmDeleteBtn.classList.toggle('btn-danger', danger);
    confirmDeleteBtn.textContent = opts.confirmLabel || (danger ? 'Delete' : 'Confirm');
    if (opts.requireText){
      confirmTextField.style.display = 'block';
      confirmTextLabel.textContent = 'Type "' + opts.requireText + '" to confirm';
      confirmTextInput.value = '';
      confirmDeleteBtn.disabled = true;
    } else {
      confirmTextField.style.display = 'none';
      confirmDeleteBtn.disabled = false;
    }
    confirmModal.classList.add('open');
    setTimeout(function(){ confirmCancelBtn.focus(); }, 0);
  }
  window.confirmDelete = confirmDelete;
  confirmTextInput.addEventListener('input', function(){
    if (confirmPending && confirmPending.requireText)
      confirmDeleteBtn.disabled = (confirmTextInput.value !== confirmPending.requireText);
  });
  function runConfirmedDelete(){
    if (confirmDeleteBtn.disabled) return;
    var pending = confirmPending;
    confirmModal.classList.remove('open');
    confirmPending = null;
    if (pending && typeof pending.onConfirm === 'function') pending.onConfirm();
  }
  confirmDeleteBtn.addEventListener('click', runConfirmedDelete);
  confirmTextInput.addEventListener('keydown', function(e){
    if (e.key === 'Enter' && !confirmDeleteBtn.disabled) { e.preventDefault(); runConfirmedDelete(); }
  });
  confirmCancelBtn.addEventListener('click', function(){ confirmModal.classList.remove('open'); confirmPending = null; });

  /* ---------------- Nav / routing ---------------- */
  var railItems = document.querySelectorAll('.rail-item[data-page]');
  var pageLoaders = {}; // page -> fn, called first time a page is shown
  var pageEnter = {};   // page -> fn, called every time the page is shown (e.g. open a stream)
  var pageLeave = {};   // page -> fn, called every time the page is left (e.g. tear down a stream)
  var currentPage = null;
  function switchPage(page){
    if (currentPage && currentPage !== page && pageLeave[currentPage]) pageLeave[currentPage]();
    railItems.forEach(function(b){ b.classList.toggle('active', b.dataset.page === page); });
    document.querySelectorAll('.page').forEach(function(p){ p.classList.remove('active'); });
    var el = $('page-' + page); if (el) el.classList.add('active');
    document.body.classList.remove('rail-open');
    $('rail').classList.remove('open');
    try { localStorage.setItem('argus_page', page); } catch(e){}
    if (pageLoaders[page] && !pageLoaders[page]._done) { pageLoaders[page]._done = true; pageLoaders[page](); }
    if (pageEnter[page]) pageEnter[page]();
    currentPage = page;
  }
  railItems.forEach(function(btn){ btn.addEventListener('click', function(){ switchPage(btn.dataset.page); }); });
  var hamburgerBtn = $('hamburgerBtn');
  var railBackdrop = $('railBackdrop');
  hamburgerBtn.addEventListener('click', function(){
    document.body.classList.toggle('rail-open');
    $('rail').classList.toggle('open');
  });
  railBackdrop.addEventListener('click', function(){
    document.body.classList.remove('rail-open');
    $('rail').classList.remove('open');
  });

  /* ---------------- Segmented controls (generic click->active, config ones PATCH) ---------------- */
  document.querySelectorAll('.segmented').forEach(function(seg){
    seg.addEventListener('click', function(e){
      var btn = e.target.closest('button');
      if (!btn || btn.disabled) return;
      if (seg.dataset.knob){ patchKnob(seg, btn.dataset.val); return; }
      seg.querySelectorAll('button').forEach(function(b){ b.classList.remove('active'); });
      btn.classList.add('active');
    });
  });

  /* ---------------- Heartbeat sparkline (decorative, pulses on real trace events) ---------------- */
  var hbCanvas = $('hb');
  var hbCtx = hbCanvas.getContext('2d');
  var HBW = hbCanvas.width, HBH = hbCanvas.height;
  var samples = new Array(HBW).fill(0);
  var spike = null;
  function ekgShape(t){
    if (t<2) return t*0.12;
    if (t<4) return 0.24 - (t-2)*0.05;
    if (t<6) return (t-4)*1.35;
    if (t<8) return 2.7 - (t-6)*2.5;
    if (t<11) return -0.3 + (t-8)*0.22;
    if (t<15) return 0.36 - (t-11)*0.09;
    return 0;
  }
  function hbTick(){
    samples.shift();
    var v = (Math.random()-0.5)*0.12;
    if (spike){ v = ekgShape(spike.t); spike.t++; if (spike.t > 16) spike = null; }
    samples.push(v);
    hbRender();
  }
  function hbRender(){
    hbCtx.clearRect(0,0,HBW,HBH);
    var mid = HBH*0.6, scale = 8.4;
    hbCtx.beginPath();
    for (var x=0;x<HBW;x++){
      var y = mid - samples[x]*scale;
      if (x===0) hbCtx.moveTo(x,y); else hbCtx.lineTo(x,y);
    }
    hbCtx.strokeStyle = '#38D6E0';
    hbCtx.lineWidth = 1.4;
    hbCtx.shadowColor = 'rgba(56,214,224,.6)';
    hbCtx.shadowBlur = reduceMotion ? 0 : 3;
    hbCtx.stroke();
  }
  function hbPulse(){ spike = { t: 0 }; if (reduceMotion) hbRender(); }
  hbRender();
  if (!reduceMotion) setInterval(hbTick, 30);

  var setDot = function(el, reachable){
    if (!el) return;
    el.classList.remove('led-ok','led-danger','led-amber');
    if (reachable === true) el.classList.add('led-ok');
    else if (reachable === false) el.classList.add('led-danger');
    // null/undefined -> plain grey (unknown), no class
  };
  /* ================= LIVE TRACE (SSE) + run list + single-run viewer ================= */
  var KIND_COLOR = {
    info: 'var(--muted)',
    skill: 'var(--magenta)',
    model_request: 'var(--cyan)',
    model_response: 'var(--violet)',
    tool_call: 'var(--amber)',
    validation: 'var(--teal)',
    tool_result: 'var(--ok)',
    reprompt: 'var(--amber)',
    observer: 'var(--amber)',
    final: 'var(--ok)',
    error: 'var(--danger)',
    approval_request: 'var(--amber)',
    approval_resolved: 'var(--amber)',
    paused: 'var(--amber)'
  };
  function kindColor(k){ return KIND_COLOR[k] || 'var(--muted)'; }

  var runsLiveEl = $('runsLive');
  var runsPastEl = $('runsPast');
  var viewerHead = $('viewerHead');
  var viewerBody = $('viewerBody');

  var runs = new Map();       // run_id -> run object, insertion order preserved
  var seen = new Set();       // dedupe key set: run_id|step|kind|ts
  var liveRunId = null;       // run_id of the current in-flight run, or null
  var selectedRunId = null;   // whichever run is shown in the viewer
  var followingLive = true;
  var currentView = 'transcript';   // 'transcript' | run_id — what the viewer is showing
  var userScrolledUp = false;

  function newRun(id, sessionId, firstTs){
    return { id: id, session: sessionId || SESSION, task: null, status: 'live',
             steps: [], startedAt: (firstTs != null ? firstTs*1000 : Date.now()) };
  }
  function liveRun(){ return liveRunId ? runs.get(liveRunId) : null; }
  function selectedRun(){ return selectedRunId ? runs.get(selectedRunId) : null; }
  function pastRunsOrdered(){
    var out = [];
    runs.forEach(function(r){ if (r.id !== liveRunId) out.push(r); });
    out.sort(function(a,b){ return b.startedAt - a.startedAt; });
    return out;
  }

  // Build req_id -> {approved, actor} for every approval_resolved event already present in a
  // run's step list. Used so a FULL rebuild (renderViewerFull — page refresh replay, or clicking
  // back onto a past run) renders an already-resolved approval_request as collapsed, instead of
  // as a fresh live card with active buttons (the incremental append path handles this live via
  // markApprovalResolved, but a full rebuild re-derives every step from scratch and needs to know
  // up front which requests are already decided).
  function resolvedMapFor(steps){
    var m = {};
    (steps || []).forEach(function(ev){
      if (ev.kind === 'approval_resolved' && ev.data && ev.data.req_id){
        m[ev.data.req_id] = { approved: ev.data.outcome === 'approved', actor: ev.data.actor };
      }
    });
    return m;
  }

  // Generic field renderer keyed on well-known `data` fields (not on kind) — mirrors the
  // proven index.html `affordances()` logic, restyled to Observatory's field-row/callout CSS.
  function renderFields(kind, data, resolvedMap){
    if (!data || typeof data !== 'object'){
      if (data !== undefined && data !== null && data !== '')
        return '<div class="field-row"><div class="field-value">' + esc(pretty(data)) + '</div></div>';
      return '';
    }
    // Interactive approvals: an inline decision card (buttons + standing-policy toggle) rather than
    // the generic field-row rendering below — this is the one kind that needs live controls, not text.
    if (kind === 'approval_request'){
      var apResolved = resolvedMap && resolvedMap[data.req_id];
      var apStates = data.states || [];
      var apOpts = apStates.map(function(s){ return '<option value="' + esc(s) + '">' + esc(s) + '</option>'; }).join('');
      var apDisabled = apResolved ? ' disabled' : '';
      var apOutcomeHtml = apResolved
        ? '<span class="ap-outcome tag ' + (apResolved.approved ? 'tag-ok' : 'tag-danger') + '">' +
          (apResolved.approved ? '✓ approved' : '✕ denied') + (apResolved.actor ? ' · ' + esc(apResolved.actor) : '') + '</span>'
        : '<span class="ap-outcome tag" style="display:none;"></span>';
      return '<div class="approval-card' + (apResolved ? ' resolved' : '') + '" data-req="' + esc(data.req_id) + '">' +
        '<div class="ap-title">⏸ Approval needed — ' + esc(data.prompt || data.kind || 'action') + '</div>' +
        (data.target ? '<div class="ap-target">' + esc(data.target) + '</div>' : '') +
        '<div class="ap-actions">' +
          '<button class="btn btn-primary btn-sm" data-apv="approve_once"' + apDisabled + '>Approve once</button>' +
          '<button class="btn btn-danger btn-sm" data-apv="deny_once"' + apDisabled + '>Deny once</button>' +
          (apOpts ? '<label class="ap-policy">Standing: <select data-apv-policy' + apDisabled + '>' +
            '<option value="" selected disabled>set…</option>' + apOpts + '</select></label>' : '') +
          apOutcomeHtml +
        '</div></div>';
    }
    if (kind === 'approval_resolved'){
      var apOk = data.outcome === 'approved';
      return '<div class="field-row"><span class="field-label">decision</span><div class="field-value">' +
        '<span class="tag ' + (apOk ? 'tag-ok' : 'tag-danger') + '">' + (apOk ? '✓ approved' : '✕ denied') + '</span>' +
        (data.actor ? ' · ' + esc(data.actor) : '') + '</div></div>';
    }
    if (kind === 'paused'){
      return '<div class="field-row"><span class="field-label">status</span><div class="field-value">' +
        '<span class="tag tag-amber">⏸ paused</span> turn ended awaiting approval (request ' + esc(data.req_id || '?') + ')</div></div>';
    }
    var parts = [];
    function field(label, html){
      return '<div class="field-row"><span class="field-label">' + esc(label) + '</span><div class="field-value">' + html + '</div></div>';
    }
    if (typeof data.answer === 'string')
      parts.push(field('answer', '<div class="callout callout-answer">' + esc(data.answer) + '</div>'));
    if (Array.isArray(data.options) && data.options.length)
      parts.push(field('choose', '<div class="clarify-opts">' + data.options.map(function(o){
        return '<button class="btn btn-sm clarify-opt" data-clarify-opt="' + esc(o) + '">' + esc(o) + '</button>';
      }).join('') + '</div>'));
    if (typeof data.text === 'string' && data.text !== '')
      parts.push(field('text', esc(data.text)));
    if (typeof data.message === 'string' && data.message !== '')
      parts.push(field('message', esc(data.message)));
    if (typeof data.reasoning === 'string' && data.reasoning.trim() !== ''){
      var rz = data.reasoning.trim();
      var preview = rz.length > 90 ? rz.slice(0, 90) + '…' : rz;
      parts.push(field('thinking',
        '<details class="reasoning-block"><summary>' + esc(preview) + '</summary>' +
        '<div class="callout callout-reasoning">' + esc(rz) + '</div></details>'));
    }
    if (data.tool !== undefined){
      var args = data.args !== undefined ? '(' + esc(pretty(data.args)) + ')' : '()';
      parts.push(field('tool call', '<strong style="color:' + kindColor(kind) + ';">' + esc(data.tool) + '</strong>' + args));
    }
    if (data.result !== undefined && typeof data.result !== 'object')
      parts.push(field('result', esc(String(data.result))));
    else if (data.result !== undefined)
      parts.push(field('result', '<pre class="trace-json">' + esc(pretty(data.result)) + '</pre>'));
    if (typeof data.raw === 'string' && data.raw !== '')
      parts.push(field('raw', '<pre class="trace-json">' + esc(data.raw) + '</pre>'));
    if (typeof data.error === 'string' && data.error !== '')
      parts.push(field('error', '<div class="callout callout-error">' + esc(data.error) + '</div>'));
    var okv = data.valid !== undefined ? data.valid : data.ok;
    if (okv !== undefined)
      parts.push(field('validation', '<span class="tag ' + (okv ? 'tag-ok' : 'tag-danger') + '">' + (okv ? 'valid' : 'invalid') + '</span>'));
    if (kind === 'skill' && data.active_skill)
      parts.push(field('skill', esc(data.active_skill) + (data.steps != null ? ' · ' + data.steps + ' steps' : '') + (data.execution ? ' · ' + esc(data.execution) : '')));
    if (kind === 'observer' && data.issue)
      parts.push(field('observer', esc(data.issue) + (data.tool ? ' · ' + esc(data.tool) : '') + (data.name ? ' · ' + esc(data.name) : '') + (data.count != null ? ' · count ' + data.count : '')));
    if (kind === 'reprompt' && data.reason)
      parts.push(field('reason', esc(data.reason)));
    parts.push('<details class="raw-data"><summary></summary><pre class="trace-json">' + esc(pretty(data)) + '</pre></details>');
    return parts.join('');
  }

  function stepNodeHtml(ev, resolvedMap){
    var c = kindColor(ev.kind);
    var stepText = (ev.step !== undefined && ev.step !== null) ? ('step ' + ev.step) : '—';
    var fieldsHtml = renderFields(ev.kind, ev.data, resolvedMap);
    return '<div class="step-node' + (reduceMotion ? '' : ' row-in') + '">' +
        '<div class="step-gutter"><span class="step-dot" style="border-color:' + c + ';"></span><span class="step-connector"></span></div>' +
        '<div class="step-body">' +
          '<div class="step-head">' +
            '<span class="tag-pill" style="color:' + c + '; background:color-mix(in srgb, ' + c + ' 15%, transparent); border-color:color-mix(in srgb, ' + c + ' 32%, transparent);">' + esc(ev.kind || '?') + '</span>' +
            '<span class="step-num num">' + stepText + '</span>' +
            '<span class="step-time num">' + esc(fmtTs(ev.ts)) + '</span>' +
          '</div>' +
          fieldsHtml +
        '</div>' +
      '</div>';
  }

  function runRowHtml(run){
    var isLive = (run.id === liveRunId);
    var dotClass = isLive ? 'dot-live' : (run.status === 'ok' ? 'dot-ok' : 'dot-error');
    var timeLabel = isLive ? elapsedLabel(run.startedAt) : relTime(run.startedAt);
    var sel = (selectedRunId === run.id) ? ' selected' : '';
    var task = run.task || ('run ' + run.id.slice(0,8));
    return '<div class="run-row' + sel + '" data-id="' + esc(run.id) + '">' +
        '<div class="run-row-top">' +
          '<span class="run-row-dot ' + dotClass + '"></span>' +
          '<span class="run-row-id num">' + esc(run.id.slice(0,8)) + '</span>' +
          (isLive ? '<span class="run-row-live-label">live</span>' : '') +
          '<span class="run-row-time num">' + timeLabel + '</span>' +
          '<span class="run-row-steps num">' + run.steps.length + ' steps</span>' +
        '</div>' +
        '<div class="run-row-task" title="' + esc(task) + '">' + esc(task) + '</div>' +
      '</div>';
  }

  var EMPTY_RUNS_HTML = '<div class="empty"><span class="empty-title">No runs yet</span>run a task above and watch it stream here.</div>';
  var tracePersist = false; // set from /status ('trace_persistence'); flips the empty-runs copy below
  function emptyRunsHtml(){
    return tracePersist
      ? '<div class="empty"><span class="empty-title">No runs yet</span>run a task above and watch it stream here.</div>'
      : '<div class="empty"><span class="empty-title">No runs to show</span>the transcript above is the record; runs aren\'t kept across a restart.</div>';
  }

  function renderRunsList(){
    var live = liveRun();
    runsLiveEl.innerHTML = live ? runRowHtml(live) : '';
    var past = pastRunsOrdered();
    runsPastEl.innerHTML = past.length ? past.map(runRowHtml).join('') : (live ? '' : emptyRunsHtml());
  }

  function renderViewerHeader(run){
    var isLive = (run.id === liveRunId);
    var statusHtml = isLive
      ? '<span class="live-badge"><span class="live-dot"></span>LIVE</span>'
      : '<span class="tag ' + (run.status === 'ok' ? 'tag-ok' : 'tag-danger') + '">' + esc(run.status) + '</span>';
    var returnHtml = (!followingLive && liveRunId) ? '<button class="return-live-btn" id="returnLiveBtn"><span class="live-dot"></span>return to live</button>' : '';
    var task = run.task || ('run ' + run.id.slice(0,8));
    viewerHead.innerHTML =
      '<span class="vh-id num">run ' + esc(run.id.slice(0,8)) + '</span>' +
      '<span class="vh-sep">·</span>' +
      '<span class="vh-session">' + esc(run.session) + '</span>' +
      '<span class="vh-sep">·</span>' +
      '<span class="vh-task" title="' + esc(task) + '">' + esc(task) + '</span>' +
      '<span class="vh-sep">·</span>' +
      '<span class="vh-steps num">' + run.steps.length + ' steps</span>' +
      statusHtml + returnHtml;
  }

  function renderViewerFull(run){
    if (!run){
      viewerHead.innerHTML = '';
      viewerBody.innerHTML = EMPTY_RUNS_HTML;
      return;
    }
    renderViewerHeader(run);
    var resolvedMap = resolvedMapFor(run.steps);
    viewerBody.innerHTML = run.steps.length
      ? run.steps.map(function(ev){ return stepNodeHtml(ev, resolvedMap); }).join('')
      : '<div class="empty">no events yet</div>';
    jumpBtn.classList.remove('show');
    userScrolledUp = false;
    if (run.id === liveRunId) viewerBody.scrollTop = viewerBody.scrollHeight;
    else viewerBody.scrollTop = 0;
  }

  function selectRun(id){
    if (!id || !runs.has(id)) return;
    currentView = id;
    selectedRunId = id;
    followingLive = (id === liveRunId);
    setTranscriptActive(false);
    renderViewerFull(runs.get(id));
    renderRunsList();
  }
  function returnToLive(){ if (liveRunId) selectRun(liveRunId); }

  viewerHead.addEventListener('click', function(e){ if (e.target.closest('#returnLiveBtn')) returnToLive(); });
  [runsLiveEl, runsPastEl].forEach(function(container){
    container.addEventListener('click', function(e){
      var row = e.target.closest('.run-row');
      if (row) selectRun(row.getAttribute('data-id'));
    });
  });
  (function(){ var t = $('transcriptCard'); if (t) t.addEventListener('click', showTranscript); })();

  /* ---- interactive approvals: the inline trace card (delegated on viewerBody, same pattern as
     runsLiveEl/runsPastEl above — the trace body itself has no prior delegated listener) ---- */
  function markApprovalResolved(card, text, ok){
    card.classList.add('resolved');
    card.querySelectorAll('button, select').forEach(function(el){ el.disabled = true; });
    var out = card.querySelector('.ap-outcome');
    if (!out) return;
    out.textContent = text;
    out.classList.remove('tag-ok', 'tag-danger', 'tag-muted');
    out.classList.add(ok === true ? 'tag-ok' : ok === false ? 'tag-danger' : 'tag-muted');
    out.style.display = 'inline-flex';
  }
  function findApprovalCards(reqId){
    return Array.prototype.filter.call(document.querySelectorAll('.approval-card'), function(c){
      return c.getAttribute('data-req') === reqId;
    });
  }
  async function decideApproval(card, reqId, action){
    card.querySelectorAll('button, select').forEach(function(el){ el.disabled = true; });
    try {
      var res = await (await fetch('/approvals/decide', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ req_id: reqId, action: action })
      })).json();
      var approved = (action === 'approve_once' || action === 'always_allow');
      if (res.result === 'unknown') markApprovalResolved(card, '— already resolved', null);
      else markApprovalResolved(card, approved ? '✓ approved' : '✕ denied', approved);
    } catch(e){
      toast('Approval decision failed: ' + e.message, 'err');
      card.querySelectorAll('button, select').forEach(function(el){ el.disabled = false; });
    }
  }
  viewerBody.addEventListener('click', function(e){
    var btn = e.target.closest('[data-apv]');
    if (!btn) return;
    var card = btn.closest('.approval-card');
    if (!card || card.classList.contains('resolved')) return;
    decideApproval(card, card.getAttribute('data-req'), btn.getAttribute('data-apv'));
  });
  viewerBody.addEventListener('change', function(e){
    var sel = e.target.closest('[data-apv-policy]');
    if (!sel) return;
    var card = sel.closest('.approval-card');
    if (!card || card.classList.contains('resolved')) return;
    var val = sel.value;
    if (!val || val === 'ask') return;   // standing "ask" is the default — nothing to change
    decideApproval(card, card.getAttribute('data-req'), val === 'allow' ? 'always_allow' : 'always_deny');
  });

  var jumpBtn = document.createElement('button');
  jumpBtn.className = 'jump-latest';
  jumpBtn.innerHTML = '↓ jump to latest';
  jumpBtn.addEventListener('click', function(){
    viewerBody.scrollTop = viewerBody.scrollHeight;
    userScrolledUp = false;
    jumpBtn.classList.remove('show');
  });
  document.querySelector('.trace-viewer').appendChild(jumpBtn);
  viewerBody.addEventListener('scroll', function(){
    if (!followingLive) return;
    var atBottom = (viewerBody.scrollHeight - viewerBody.scrollTop - viewerBody.clientHeight) < 24;
    userScrolledUp = !atBottom;
    if (atBottom) jumpBtn.classList.remove('show');
  });

  function updateTaskLabel(run, ev){
    if (run.task) return;
    if (ev.kind === 'info' && ev.data && typeof ev.data.text === 'string' && ev.data.text)
      run.task = ev.data.text;
    else if (ev.kind === 'skill' && ev.data && ev.data.active_skill)
      run.task = ev.data.active_skill + ' (skill)';
  }

  function processEvent(ev){
    var key = [ev.run_id, ev.step, ev.kind, ev.ts].join('|');
    if (seen.has(key)) return;
    seen.add(key);

    var isNewRun = !runs.has(ev.run_id);
    if (isNewRun){
      runs.set(ev.run_id, newRun(ev.run_id, ev.session_id, ev.ts));
      liveRunId = ev.run_id; // the newest run seen becomes the in-flight one
      // Only auto-follow a new run into the viewer when we're ALREADY viewing a run. When the
      // transcript is showing, a new turn must NOT clobber it — it just appears in the Runs list.
      if (currentView !== 'transcript' && followingLive){ currentView = ev.run_id; selectedRunId = ev.run_id; }
    }
    var run = runs.get(ev.run_id);
    updateTaskLabel(run, ev);
    run.steps.push(ev);

    if (ev.kind === 'final'){
      run.status = 'ok'; if (liveRunId === run.id) liveRunId = null;
      // Stay-put refresh: if the transcript is the active view, reload it when this session's turn
      // completes so the new turn appears — without ever covering the conversation.
      if (currentView === 'transcript' && ev.session_id === SESSION) loadTranscript(SESSION);
    }
    else if (ev.kind === 'error'){ run.status = 'error'; if (liveRunId === run.id) liveRunId = null; }
    else if (ev.kind === 'approval_resolved' && ev.data && ev.data.req_id){
      // The engine emits approval_resolved (ApprovalBroker._emit_resolved) whenever a request is
      // decided, whether via this dashboard's own decideApproval() POST or another channel (another
      // open dashboard tab, Telegram, the Developer page). This branch reacts to that event so any
      // matching card here collapses too, even when the decision didn't originate from this card's
      // own POST response.
      var apApproved = ev.data.outcome === 'approved' ? true : (ev.data.outcome === 'denied' ? false : null);
      findApprovalCards(ev.data.req_id).forEach(function(c){
        markApprovalResolved(c, apApproved === true ? '✓ approved' : apApproved === false ? '✕ denied' : '— resolved', apApproved);
      });
    }

    // Only paint run steps into the viewer when the viewer is actually showing THIS run.
    if (currentView === run.id){
      if (isNewRun){
        renderViewerFull(run);
      } else {
        viewerBody.insertAdjacentHTML('beforeend', stepNodeHtml(ev));
        renderViewerHeader(run);
        if (!userScrolledUp) viewerBody.scrollTop = viewerBody.scrollHeight;
        else jumpBtn.classList.add('show');
      }
    }
    renderRunsList();
    hbPulse();
  }

  setInterval(renderRunsList, 5000); // keep relative/elapsed time labels fresh

  var es = null;
  function wireEventHandlers(source){
    source.onmessage = function(m){
      var ev; try { ev = JSON.parse(m.data); } catch(e){ return; }
      processEvent(ev);
    };
    source.onerror = function(){ /* EventSource auto-reconnects; `seen` dedupes the ring-buffer replay */ };
  }
  function reopenEvents(){
    if (es) { try { es.close(); } catch(e){} }
    es = new EventSource("/events?session_id=" + encodeURIComponent(SESSION));
    wireEventHandlers(es);
  }
  renderRunsList();

  /* ---- session switching: create/rename/delete/list durable sessions (Task 4's /sessions
     endpoints), and re-scope the console (runs list + live trace + persisted transcript) to
     whichever one is active. Mutating calls (/sessions POST|PATCH|DELETE) are admin-gated
     server-side; they go through plain fetch() because window.fetch is monkey-patched above
     to inject X-Admin-Token on every request (and prompt-and-retry on 401) — that IS this
     dashboard's admin-fetch helper, the same one Rules/Reliability/Routines POSTs rely on. ---- */
  // Render Argus's (assistant) message markdown safely: marked parses, DOMPurify sanitizes the HTML.
  // Falls back to escaped literal text if the vendored libs didn't load.
  var _mdInit = false;
  function renderAssistantMd(text){
    var raw = (text == null) ? '' : String(text);
    if (window.marked && window.DOMPurify){
      try {
        if (!_mdInit){ marked.setOptions({ gfm: true, breaks: true }); _mdInit = true; }
        return DOMPurify.sanitize(marked.parse(raw));
      } catch(e){ /* fall through to literal */ }
    }
    return esc(raw);
  }

  async function loadTranscript(id){
    try {
      var data = await (await fetch('/sessions/' + encodeURIComponent(id) + '/messages?limit=1000')).json();
      // Drop empty / json-null turns: an assistant tool-call turn is stored with null content and would
      // otherwise render as the literal "null". The raw log keeps them; the chat view hides them.
      var msgs = (data.messages || []).filter(function(m){
        var c = (m.content == null ? '' : String(m.content)).trim();
        return c && c !== 'null';
      });
      updateTranscriptMeta(msgs.length);
      if (!msgs.length){
        viewerHead.innerHTML =
          '<span class="vh-id num">transcript</span><span class="vh-sep">·</span><span class="vh-session">' + esc(id) + '</span>';
        viewerBody.innerHTML = '<div class="empty"><span class="empty-title">No messages yet</span>send a turn to start this conversation.</div>';
        return;
      }
      viewerHead.innerHTML =
        '<span class="vh-id num">transcript</span><span class="vh-sep">·</span>' +
        '<span class="vh-session">' + esc(id) + '</span><span class="vh-sep">·</span>' +
        '<span class="vh-steps num">' + msgs.length + ' messages</span>';
      // Chat/messaging view: user right, assistant left, tool output as a muted note.
      viewerBody.innerHTML = '<div class="chat">' + msgs.map(function(m){
        var role = m.role || '?';
        var raw = m.content == null ? '' : String(m.content);
        if (role === 'tool'){
          return '<div class="chat-tool"><span class="chat-tool-label">tool</span>' +
                   '<div class="chat-tool-body">' + esc(raw) + '</div></div>';
        }
        // The harness injects steering notes mid-turn (the observer's repeat nudge, the
        // create-without-verify nudge, the output-truncated reprompt). The model has no "system"
        // slot mid-conversation, so they go in with role:"user" and read in the transcript as if
        // the owner typed them. They all carry the "[note] " prefix by convention — render those
        // as a centred system note, not a user bubble.
        if (role === 'user' && raw.indexOf('[note] ') === 0){
          return '<div class="chat-note"><span class="chat-note-label">Argus nudge</span>' +
                   '<div class="chat-note-body">' + esc(raw.slice(7).trim()) + '</div></div>';
        }
        var side = (role === 'user') ? 'me' : 'them';
        // user text stays literal; Argus's replies render markdown (bold, lists, tables, code).
        var body = (role === 'user') ? esc(raw) : ('<div class="md">' + renderAssistantMd(raw) + '</div>');
        return '<div class="chat-row ' + side + '"><div class="chat-bubble ' + side + '">' + body + '</div></div>';
      }).join('') + '</div>';
      viewerBody.scrollTop = viewerBody.scrollHeight;
    } catch(e){ toast('Failed to load transcript: ' + e.message, 'err'); }
  }

  function setTranscriptActive(on){
    var el = $('transcriptCard');
    if (el) el.classList.toggle('active', !!on);
  }
  function updateTranscriptMeta(n){
    var el = $('transcriptMeta');
    if (el) el.textContent = n ? (nfmt(n) + ' message' + (n === 1 ? '' : 's')) : 'no messages yet';
  }
  // Show the conversation transcript (the default view). Selecting a run drills into its trace instead.
  function showTranscript(){
    currentView = 'transcript';
    selectedRunId = null; followingLive = false;
    setTranscriptActive(true);
    loadTranscript(SESSION);
    renderRunsList();
  }

  // The sidebar shows a session's NAME, so once it's renamed the id has nowhere else to surface.
  // Pin it to the Runs header, click-to-copy (ephemeral `__`-prefixed sessions have no stored row
  // but still have an id worth showing).
  function updateRunsSessionId(){
    var el = $('runsSessionId');
    if (!el) return;
    el.textContent = SESSION || '';
    el.title = 'Session id: ' + (SESSION || '(none)') + ' — click to copy';
  }
  document.addEventListener('click', function(e){
    var el = e.target.closest ? e.target.closest('#runsSessionId') : null;
    if (!el || !SESSION) return;
    // Clipboard needs a secure context; over plain http on the LAN it's undefined, so fall back to
    // selecting the text rather than throwing and looking broken.
    if (navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(SESSION)
        .then(function(){ toast('Copied ' + SESSION, 'ok'); })
        .catch(function(){ toast('Copy failed — select it manually', 'err'); });
    } else {
      var r = document.createRange(); r.selectNodeContents(el);
      var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
      toast('Press Ctrl+C to copy', 'ok');
    }
  });

  async function renderSessionList(){
    updateRunsSessionId();
    var list;
    try { list = await (await fetch('/sessions')).json(); } catch(e){ return; }
    var ul = $('sessionList');
    if (!ul) return;
    if (!Array.isArray(list) || !list.length){
      ul.innerHTML = '<li class="empty">No sessions yet</li>';
      return;
    }
    ul.innerHTML = list.map(function(s){
      var active = s.id === SESSION;
      var name = s.name || s.id;
      return '<li class="session-row' + (active ? ' active' : '') + '" data-id="' + esc(s.id) + '">' +
          '<span class="session-row-name" title="' + esc(name) + '">' + esc(name) + '</span>' +
          '<span class="session-row-count num">' + nfmt(s.message_count || 0) + '</span>' +
          '<span class="session-row-actions">' +
            '<button class="act-btn" data-session-rename title="Rename">✎</button>' +
            '<button class="act-btn danger" data-session-delete title="Delete">✕</button>' +
          '</span>' +
        '</li>';
    }).join('');
  }

  function setSession(id){
    SESSION = id;
    try { localStorage.setItem('argus.session', id); } catch(e){}
    // runs/seen/live-tracking are all per-session — drop the old session's before re-subscribing
    // (also resets followingLive so a live run on the new session is auto-tracked even if the
    // user had scrolled back into a past run on the previous session).
    runs.clear(); seen.clear(); liveRunId = null; selectedRunId = null; followingLive = true;
    currentView = 'transcript';   // the conversation is the default view for a session
    reopenEvents();          // re-subscribe /events at the new session — replays its recent ring buffer
    loadTranscript(id);      // show the persisted conversation
    setTranscriptActive(true);
    renderRunsList();
    renderSessionList();
  }

  async function renameSession(id){
    var name = prompt('Rename session:');
    if (!name) return;
    try {
      // The fetch shim resolves (not rejects) a 401 when the admin token is missing/wrong, so a
      // failed mutation must be caught on res.ok — otherwise we'd toast success on an auth failure.
      var res = await fetch('/sessions/' + encodeURIComponent(id), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name })
      });
      if (!res.ok) { toast('Rename failed (' + res.status + ')', 'err'); return; }
      renderSessionList();
      toast('Renamed session', 'ok');
    } catch(e){ toast('Rename failed: ' + e.message, 'err'); }
  }

  function deleteSessionPrompt(id){
    confirmDelete({
      title: 'Delete session',
      message: "Delete this session? This can't be undone.",
      onConfirm: async function(){
        var res;
        try { res = await fetch('/sessions/' + encodeURIComponent(id), { method: 'DELETE' }); }
        catch(e){ toast('Delete failed: ' + e.message, 'err'); return; }
        // Guard on res.ok so a 401 (no/wrong admin token) doesn't falsely report success and,
        // worse, fall back to setSession('dashboard') when nothing was actually deleted.
        if (!res.ok) { toast('Delete failed (' + res.status + ')', 'err'); return; }
        toast('Session deleted', 'ok');
        if (id === SESSION) setSession('dashboard');   // fall back to the default session
        else renderSessionList();
      }
    });
  }

  var sessionListEl = $('sessionList');
  if (sessionListEl) sessionListEl.addEventListener('click', function(e){
    var row = e.target.closest('.session-row');
    if (!row) return;
    var id = row.getAttribute('data-id');
    if (e.target.closest('[data-session-rename]')) { renameSession(id); return; }
    if (e.target.closest('[data-session-delete]')) { deleteSessionPrompt(id); return; }
    if (id && id !== SESSION) setSession(id);
  });

  var sessionNewBtn = $('sessionNewBtn');
  if (sessionNewBtn) sessionNewBtn.addEventListener('click', async function(){
    sessionNewBtn.disabled = true;
    try {
      // Check res.ok BEFORE parsing/using the body: a 401 from the fetch shim has no id, and
      // setSession(undefined) would persist SESSION="undefined" to localStorage and re-subscribe
      // /events?session_id=undefined — real corruption. Only switch on a real id.
      var res = await fetch('/sessions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({})
      });
      if (!res.ok) { toast('Failed to create session (' + res.status + ')', 'err'); return; }
      var r = await res.json();
      if (!r || !r.id) { toast('Failed to create session (no id returned)', 'err'); return; }
      setSession(r.id);
      toast('New session created', 'ok');
    } catch(e){ toast('Failed to create session: ' + e.message, 'err'); }
    finally { sessionNewBtn.disabled = false; }
  });
  /* ================= CONSOLE: run / slash-commands / usage / config / skills / status ================= */
  var runnerInput = $('runnerInput');
  var runBtn = $('runBtn');
  var runStatus = $('runStatus');

  /* ---- auto-growing prompt textarea: 1 line when empty (no scrollbar) up to ~20 lines,
     then internal scroll. Height is driven by content, not a fixed focus-state jump. ---- */
  var RUNNER_INPUT_MAX_H = 312; // ~20 lines
  function autosizeRunnerInput(){
    runnerInput.style.height = 'auto';
    var full = runnerInput.scrollHeight;
    runnerInput.style.height = Math.min(full, RUNNER_INPUT_MAX_H) + 'px';
    runnerInput.style.overflowY = (full > RUNNER_INPUT_MAX_H) ? 'auto' : 'hidden';
  }
  runnerInput.addEventListener('input', autosizeRunnerInput);
  autosizeRunnerInput();

  async function runTask(){
    var text = runnerInput.value.trim();
    if (!text) { runnerInput.focus(); return; }
    if (text.startsWith('/')) { handleSlash(text); runnerInput.value = ''; autosizeRunnerInput(); return; }
    var skillSel = $('skillPicker');
    var skill = (skillSel && !skillSel.disabled && skillSel.value) ? skillSel.value : null;

    runBtn.disabled = true;
    // any clarify-option buttons from a prior turn are now stale — retire them so a scrolled-back
    // old choice can't be sent as this turn's answer.
    document.querySelectorAll('.clarify-opt').forEach(function(b){ b.disabled = true; });
    runStatus.textContent = 'running…';
    runStatus.style.color = 'var(--cyan)';
    var started = performance.now();
    try {
      var res = await fetch('/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: SESSION, text: text, skill: skill })
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      await res.json();
      var secs = ((performance.now() - started) / 1000).toFixed(1);
      runStatus.textContent = 'completed in ' + secs + 's';
      runStatus.style.color = '';
      runnerInput.value = '';
      autosizeRunnerInput();
      loadUsage();
    } catch (e) {
      runStatus.textContent = 'run failed';
      runStatus.style.color = 'var(--danger)';
      toast('Run failed: ' + e.message, 'err');
    } finally {
      runBtn.disabled = false;
    }
  }
  runBtn.addEventListener('click', runTask);
  runnerInput.addEventListener('keydown', function(e){
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!runBtn.disabled) runTask(); }
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); if (!runBtn.disabled) runTask(); }
  });
  // Clarification choice buttons: clicking an option sends it as the user's next message.
  document.addEventListener('click', function(e){
    var opt = e.target.closest('[data-clarify-opt]');
    if (!opt || runBtn.disabled) return;
    runnerInput.value = opt.getAttribute('data-clarify-opt');
    autosizeRunnerInput();
    runTask();
  });

  /* ---- new session: reset the engine-side session and the local trace state ---- */
  var newSessionBtn = $('newSessionBtn');
  if (newSessionBtn) newSessionBtn.addEventListener('click', function(){
    confirmDelete({
      title: 'New session',
      message: 'Start a new session? This clears the current conversation and trace.',
      confirmLabel: 'New session',
      danger: false,
      onConfirm: async function(){
        try {
          var res = await fetch('/session/reset', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: SESSION })
          });
          if (!res.ok) throw new Error('HTTP ' + res.status);
        } catch(e){ toast('Failed to start new session: ' + e.message, 'err'); return; }
        runs.clear(); seen.clear(); liveRunId = null; selectedRunId = null; followingLive = true;
        renderRunsList();
        showTranscript();        // back to the (now context-cleared) conversation view
        loadUsage();
        toast('New session started', 'ok');
      }
    });
  });

  function showCmdOut(title, html){
    $('cmdOutTitle').textContent = title;
    $('cmdOutBody').innerHTML = html;
    $('cmdOut').style.display = 'block';
  }
  $('cmdOutClose').addEventListener('click', function(){ $('cmdOut').style.display = 'none'; });

  async function handleSlash(raw){
    var cmd = raw.trim().slice(1).split(/\s+/)[0].toLowerCase();
    switch (cmd){
      case 'usage': {
        loadUsage();
        try {
          var u = await (await fetch('/usage?session_id=' + SESSION)).json();
          showCmdOut('/usage',
            '<b>' + nfmt(u.context_tokens) + '</b> tokens' +
            (u.context_window ? ' / ' + nfmt(u.context_window) : '') +
            ' · ' + u.messages + ' msgs · ' + u.tools_available + ' tools · ' + u.skills_available + ' skills' +
            (u.percent_used != null ? ' · ' + u.percent_used + '% used' : ''));
        } catch(e){ toast('usage unavailable', 'err'); }
        break;
      }
      case 'compact': {
        showCmdOut('/compact', 'compacting context…');
        await compactCtx();
        break;
      }
      case 'reset': {
        runs.clear(); seen.clear(); liveRunId = null; selectedRunId = null; followingLive = true;
        renderRunsList();
        showTranscript();        // back to the conversation view
        toast('Trace view cleared — engine history reset is Telegram-only; the dashboard session persists', 'info');
        showCmdOut('/reset', 'Cleared the local trace view. History reset is Telegram-only; the dashboard session persists on the server.');
        break;
      }
      case 'skills': {
        try {
          var lib1 = await (await fetch('/library')).json();
          var sk = lib1.skills || {};
          var all1 = [].concat(sk.builtin || [], sk.created || []);
          var items1 = all1.length
            ? '<ul>' + all1.map(function(s){ return '<li><code>' + esc(s.name) + '</code>' + (s.description ? ' — ' + esc(s.description) : '') + '</li>'; }).join('') + '</ul>'
            : '(no skills)';
          showCmdOut('/skills', items1);
        } catch(e){ toast('skills unavailable', 'err'); }
        break;
      }
      case 'tools': {
        try {
          var lib2 = await (await fetch('/library')).json();
          var t2 = lib2.tools || {};
          var all2 = [].concat(t2.builtin || [], t2.created || []);
          var items2 = all2.length
            ? '<ul>' + all2.map(function(o){ return '<li><code>' + esc(o.name) + '</code>' + (o.description ? ' — ' + esc(truncate(o.description, 100)) : '') + '</li>'; }).join('') + '</ul>'
            : '(no tools)';
          showCmdOut('/tools', items2);
        } catch(e){ toast('tools unavailable', 'err'); }
        break;
      }
      case 'status': {
        try {
          var s = await (await fetch('/run-status?session_id=' + SESSION)).json();
          var head = s.running
            ? '🟢 <b>Working</b> — step ' + (s.current_step||0) + '/' + (s.max_steps||'?') + (s.last_tool ? ' (last tool: <code>' + esc(s.last_tool) + '</code>)' : '')
            : '⚪ <b>Idle</b> — not working on anything right now';
          showCmdOut('/status', head + '<ul><li>turns this session: ' + (s.turns||0) + '</li><li>messages in history: ' + (s.messages||0) + '</li></ul>');
        } catch(e){ toast('status unavailable', 'err'); }
        break;
      }
      case 'help': {
        showCmdOut('/help',
          'Dashboard slash-commands (handled here, not sent to the agent):<ul>' +
          '<li><code>/status</code> — is the agent working, and which step</li>' +
          '<li><code>/usage</code> — refresh &amp; show context usage</li>' +
          '<li><code>/compact</code> — compact the context window</li>' +
          '<li><code>/reset</code> — clear the trace view (history reset is Telegram-only)</li>' +
          '<li><code>/tools</code> — list available tools</li>' +
          '<li><code>/skills</code> — list available skills</li>' +
          '<li><code>/help</code> — show this list</li></ul>');
        break;
      }
      default:
        toast('unknown command: /' + cmd);
    }
  }

  /* ---- context gauge + compact ---- */
  var lastUsage = null; // last /usage payload, used to render the gauge-chip hover popover
  function renderGaugePopover(u, pct, win){
    var b = (u && u.breakdown) || {};
    function row(k, v){ return '<div class="gp-row"><span class="gp-k">' + esc(k) + '</span><span class="gp-v num">' + esc(v) + '</span></div>'; }
    $('gaugePopover').innerHTML =
      '<div class="gp-title">Context breakdown</div>' +
      row('System prompt', nfmt(b.system_prompt || 0)) +
      row('Tool schemas', nfmt(b.tool_schemas || 0)) +
      row('Conversation', nfmt(b.conversation || 0)) +
      '<div class="gp-sep"></div>' +
      row('Messages', nfmt(u.messages || 0)) +
      row('Tools available', nfmt(u.tools_available || 0)) +
      row('Skills available', nfmt(u.skills_available || 0)) +
      '<div class="gp-sep"></div>' +
      row('Total', nfmt(u.context_tokens || 0) + (win ? ' / ' + nfmt(win) : '')) +
      row('Percent used', (pct != null ? pct + '%' : '–'));
  }
  async function loadUsage(){
    try {
      var u = await (await fetch('/usage?session_id=' + SESSION)).json();
      lastUsage = u;
      var win = u.context_window;
      var pct = u.percent_used != null ? u.percent_used : (win ? Math.round(100*u.context_tokens/win) : 0);
      $('gaugeFill').style.width = Math.min(100, pct) + '%';
      var humK = function(x){ return x >= 1000 ? (x/1000).toFixed(1).replace(/\.0$/,'') + 'k' : String(x); };
      $('gaugeLabel').textContent = pct + '% · ' + humK(u.context_tokens||0) + (win ? '/' + humK(win) : '');
      renderGaugePopover(u, pct, win);
    } catch(e){ $('gaugeLabel').textContent = '–'; }
  }
  async function compactCtx(){
    try {
      var r = await (await fetch('/compact', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: SESSION })
      })).json();
      var msg = r.compacted
        ? ('✓ ' + r.messages_before + '→' + r.messages_after + ' msgs, ~' + nfmt(r.tokens_before) + '→~' + nfmt(r.estimated_tokens_after) + ' tok')
        : (r.reason || 'nothing to compact');
      toast(msg, r.compacted ? 'ok' : 'info');
      if ($('cmdOut').style.display !== 'none' && $('cmdOutTitle').textContent === '/compact') showCmdOut('/compact', esc(msg));
      loadUsage();
    } catch(e){ toast('compact failed: ' + e.message, 'err'); }
  }
  $('compactBtn').addEventListener('click', compactCtx);

  /* ---- config (console popover + settings runtime limits) ---- */
  function setInputVal(id, val){
    var el = $(id); if (!el) return;
    if (document.activeElement === el) return;
    el.value = (val === undefined || val === null) ? '' : String(val);
  }
  function reflectSeg(container, val){
    if (!container) return;
    container.querySelectorAll('button').forEach(function(b){ b.classList.toggle('active', b.dataset.val === String(val)); });
  }
  function reflectBoolSwitch(id, val){ var el = $(id); if (el && document.activeElement !== el) el.checked = !!val; }

  var lastConfig = {};
  function applyConfig(cfg){
    lastConfig = cfg || {};
    reflectSeg(document.querySelector('[data-knob="tool_calling_mode"]'), cfg.tool_calling_mode);
    reflectSeg(document.querySelector('[data-knob="skill_selection_mode"]'), cfg.skill_selection_mode);
    $('chipTcm').textContent = cfg.tool_calling_mode || '–';
    $('chipSsm').textContent = cfg.skill_selection_mode || '–';
    var skillSel = $('skillPicker');
    var explicit = cfg.skill_selection_mode === 'explicit';
    skillSel.disabled = !explicit;

    reflectBoolSwitch('observerToggle', cfg.enable_observer);
    setInputVal('observerThreshold', cfg.observer_repeat_threshold);

    reflectSeg($('segReason'), cfg.model_reasoning);
    reflectSeg($('segSemantic'), cfg.semantic_recall);
    reflectSeg($('segMemScope'), cfg.memory_scope);

    setInputVal('inMaxTokens', cfg.model_max_tokens);
    setInputVal('inMaxSteps', cfg.max_steps);
    setInputVal('inAutoCompact', cfg.auto_compact_tokens);
    reflectBoolSwitch('swToolCreation', cfg.enable_tool_creation);
    reflectBoolSwitch('swToolNetwork', cfg.tool_creation_allow_network);
    reflectBoolSwitch('swCodeInterp', cfg.enable_code_interpreter);

    reflectBoolSwitch('swTracePersist', cfg.enable_trace_persistence);
    if ($('selTraceMode')) $('selTraceMode').value = cfg.trace_retention_mode;
    setInputVal('inTraceDays', cfg.trace_retention_days);
    setInputVal('inTraceKeep', cfg.trace_keep_runs_per_session);

    reflectBoolSwitch('cfgEnableSandbox', cfg.enable_sandbox);
    setInputVal('cfgSandboxRuntime', cfg.sandbox_runtime);
    setInputVal('cfgSandboxIdleMinutes', cfg.sandbox_idle_minutes);

    var tok = $('inTgToken');
    if (tok && document.activeElement !== tok)
      tok.placeholder = cfg.telegram_bot_token === '***set***' ? 'a token is set — paste a new one to change' : 'paste new token to change';
    setInputVal('inTgChats', cfg.allowed_chat_ids);
    setInputVal('inSearxngUrl', cfg.searxng_base_url);
    setInputVal('inFirecrawlUrl', cfg.firecrawl_base_url);
    $('tgStatusTag').textContent = cfg.telegram_bot_token === '***set***' ? 'connected' : 'not set';
    $('tgStatusTag').className = 'tag ' + (cfg.telegram_bot_token === '***set***' ? 'tag-ok' : 'tag-muted');

    var provider = (cfg.model_provider && cfg.model_provider !== 'auto') ? cfg.model_provider : providerOf(cfg.model_base_url);
    var host; try { host = new URL(cfg.model_base_url).host; } catch(e){ host = (cfg.model_base_url||'–').slice(0,60); }
    $('modelActiveLine').textContent = (cfg.model_name || '–') + '  ·  ' + provider + '  ·  ' + host;

    // Compact model chip — mode is in the header chips, steps/tokens in the ⚙ controls popover, so the
    // one thing worth showing in the toolbar is which model is active (kept short; full name on hover).
    var mn = cfg.model_name || '–';
    $('statsReadout').textContent = mn.indexOf('/') > -1 ? mn.split('/').pop() : mn;
    $('statsReadout').title = mn;
  }
  function providerOf(url){
    url = (url||'').toLowerCase();
    if (url.includes('openrouter.ai')) return 'openrouter';
    if (url.includes('api.openai.com')) return 'openai';
    return 'vllm';
  }
  async function loadConfig(){
    try { applyConfig(await (await fetch('/config')).json()); }
    catch(e){ toast('Config load failed', 'err'); }
  }
  async function patchConfigKey(key, value){
    var res = await fetch('/config', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify((function(){ var o = {}; o[key] = value; return o; })()) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    applyConfig(await res.json());
    // Persist to .env so a dashboard setting change (reasoning, limits, toggles) survives a restart —
    // these knobs have no separate Save button, so the change IS the intent. Best-effort.
    try { await fetch('/config/save', { method: 'POST' }); } catch (e) {}
  }
  async function patchKnob(seg, val){
    var knob = seg.dataset.knob;
    seg.querySelectorAll('button').forEach(function(b){ b.disabled = true; });
    try { await patchConfigKey(knob, val); }
    catch(e){ toast('Failed to set ' + knob, 'err'); loadConfig(); }
    finally { seg.querySelectorAll('button').forEach(function(b){ b.disabled = false; }); }
  }
  function wireNumberField(id, key){
    var el = $(id); if (!el) return;
    el.addEventListener('change', function(){
      var n = Number(el.value);
      if (!Number.isFinite(n)) { toast(key + ': not a number', 'err'); return; }
      patchConfigKey(key, Math.trunc(n)).catch(function(e){ toast('Failed to set ' + key, 'err'); });
    });
  }
  wireNumberField('inMaxTokens', 'model_max_tokens');
  wireNumberField('inMaxSteps', 'max_steps');
  wireNumberField('inAutoCompact', 'auto_compact_tokens');
  function wireBoolSwitch(id, key){
    var el = $(id); if (!el) return;
    el.addEventListener('change', function(){ patchConfigKey(key, el.checked).catch(function(){ toast('Failed to set ' + key, 'err'); loadConfig(); }); });
  }
  wireBoolSwitch('observerToggle', 'enable_observer');
  wireBoolSwitch('swToolCreation', 'enable_tool_creation');
  wireBoolSwitch('swToolNetwork', 'tool_creation_allow_network');
  wireBoolSwitch('swCodeInterp', 'enable_code_interpreter');
  wireBoolSwitch('swTracePersist', 'enable_trace_persistence');
  (function(){
    var m = $('selTraceMode'); if (m) m.addEventListener('change', function(){ patchConfigKey('trace_retention_mode', m.value).catch(function(){ toast('Failed to set trace_retention_mode', 'err'); loadConfig(); }); });
  })();
  wireNumberField('inTraceDays', 'trace_retention_days');
  wireNumberField('inTraceKeep', 'trace_keep_runs_per_session');
  $('observerThreshold').addEventListener('change', function(){
    var n = Number($('observerThreshold').value);
    if (Number.isFinite(n)) patchConfigKey('observer_repeat_threshold', Math.trunc(n)).catch(function(){ toast('Failed to set threshold', 'err'); });
  });

  /* ---- sandbox card ---- */
  wireBoolSwitch('cfgEnableSandbox', 'enable_sandbox');
  $('cfgSandboxRuntime').addEventListener('change', function(){
    patchConfigKey('sandbox_runtime', this.value).catch(function(){ toast('Failed to set sandbox_runtime', 'err'); loadConfig(); });
  });
  wireNumberField('cfgSandboxIdleMinutes', 'sandbox_idle_minutes');
  async function loadSandboxStatus(){
    var el = $('sandboxStatus');
    if (!el) return;
    try {
      var s = await (await fetch('/sandbox/status')).json();
      var cls = s.available ? 'sb-ok' : 'sb-bad';
      var rows = [
        '<span>runtime: <span class="' + cls + '">' + esc(s.runtime || '?') +
          (s.version ? ' ' + esc(s.version) : '') + '</span></span>',
        '<span>status: <span class="' + cls + '">' +
          (s.available ? 'ready' : esc(s.reason || 'unavailable')) + '</span></span>'
      ];
      if (s.image) rows.push('<span>image: ' + esc(s.image) +
        (s.image_present === false ? ' <span class="sb-bad">(not built)</span>' : '') + '</span>');
      if (s.workspaces && s.workspaces.length)
        rows.push('<span>running: ' + esc(s.workspaces.join(', ')) + '</span>');
      el.innerHTML = rows.join('');
    } catch(e){ el.innerHTML = '<span class="sb-bad">status unavailable</span>'; }
  }
  $('sandboxRecheckBtn').addEventListener('click', loadSandboxStatus);
  $('sandboxSetupBtn').addEventListener('click', async function(){
    var btn = this, out = $('sandboxOutput');
    btn.disabled = true; btn.textContent = 'Setting up…';
    out.style.display = 'block'; out.textContent = 'running scripts/setup-sandbox.sh …';
    try {
      // The fetch shim resolves a 401 rather than rejecting, so success is only visible on res.ok.
      var res = await fetch('/sandbox/setup', { method: 'POST' });
      if (!res.ok){ out.textContent = 'Setup failed (' + res.status + ')'; toast('Setup failed (' + res.status + ')', 'err'); }
      else {
        var d = await res.json();
        out.textContent = d.output || '(no output)';
        toast(d.ok ? 'Sandbox ready' : 'Setup failed — see the output', d.ok ? 'ok' : 'err');
      }
    } catch(e){ out.textContent = String(e); toast('Setup failed: ' + e.message, 'err'); }
    btn.disabled = false; btn.textContent = 'Set up sandbox';
    loadSandboxStatus();
  });

  /* ---- controls popover ---- */
  var controlsBtn = $('controlsBtn');
  var controlsPopover = $('controlsPopover');
  controlsBtn.addEventListener('click', function(e){ e.stopPropagation(); controlsPopover.classList.toggle('open'); });
  document.addEventListener('click', function(e){
    if (controlsPopover.classList.contains('open') && !e.target.closest('.toolbar-controls-wrap')) controlsPopover.classList.remove('open');
  });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') controlsPopover.classList.remove('open'); });

  /* ---- skill picker ---- */
  async function loadSkills(){
    try {
      var list = await (await fetch('/skills')).json();
      var sel = $('skillPicker');
      sel.innerHTML = '';
      var none = document.createElement('option'); none.value = ''; none.textContent = '— none (auto) —'; sel.appendChild(none);
      (list||[]).forEach(function(s){
        var o = document.createElement('option'); o.value = s.name; o.textContent = s.name;
        o.title = s.description || '';
        sel.appendChild(o);
      });
      sel.addEventListener('change', function(){
        var label = sel.value || '— none (auto) —';
        $('activeSkillLabel').textContent = label;
        sel.title = 'Skill picker — active: ' + label;
      });
    } catch(e){ /* skills optional */ }
  }

  /* ---- status polling (health LEDs, mode chips) ---- */
  async function pollStatus(){
    try {
      var s = await (await fetch('/status')).json();
      [['model', s.model], ['searxng', s.searxng], ['firecrawl', s.firecrawl]].forEach(function(pair){
        var k = pair[0], v = pair[1] || {};
        var state = v.configured === false ? null : v.reachable;
        setDot($('led-' + k), state);
        var item = $('health' + k.charAt(0).toUpperCase() + k.slice(1));
        if (item) item.title = v.configured === false ? 'not configured' : ((v.url||'') + (v.name ? ' (' + v.name + ')' : '') + ' — ' + (v.reachable ? 'reachable' : 'unreachable'));
      });
      if (s.embedding){
        $('healthEmbed').style.display = '';
        setDot($('led-embed'), s.embedding.reachable);
        $('healthEmbed').title = (s.embedding.url||'') + (s.embedding.name ? ' (' + s.embedding.name + ')' : '');
      } else {
        $('healthEmbed').style.display = 'none';
      }
      $('chipTcm').textContent = s.tool_calling_mode || $('chipTcm').textContent;
      $('chipSsm').textContent = s.skill_selection_mode || $('chipSsm').textContent;
      var wasTracePersist = tracePersist;
      tracePersist = !!s.trace_persistence;
      if (tracePersist !== wasTracePersist) renderRunsList();
    } catch(e){
      ['model','searxng','firecrawl','embed'].forEach(function(k){ setDot($('led-' + k), null); });
    }
  }
  /* ================= AUTOMATION: routines / scheduled / watches ================= */
  var ROUTINE_TEMPLATE = {
    name: "", description: "", enabled: true,
    steps: [ { type: "tool", id: "step1", tool: "", args: {} } ],
    output: "", deliver: { channel: "telegram", subject: "" },
    trigger: { on_demand: true, phrases: [], schedule: "" }
  };
  var rbModel = null;
  var rbMeta = { tools: [], skills: [], channels: ["telegram","email","push","none"] };
  var rbMetaLoaded = false;

  async function loadRoutines(){
    var body = $('routinesBody');
    try {
      var d = await (await fetch('/routines')).json();
      var rs = d.routines || [];
      $('routinesCount').textContent = rs.length ? (' ' + rs.length) : '';
      if (!rs.length){
        body.innerHTML = '<tr><td colspan="6" class="empty"><span class="empty-title">No routines yet</span>click + New routine to create one.</td></tr>';
        return;
      }
      body.innerHTML = rs.map(function(r){
        var n = (r.steps||[]).length;
        var stepsPreview = (r.steps||[]).map(function(s){ return s.type; }).join(' → ') || '—';
        var trigger = (r.trigger && r.trigger.schedule) ? esc(r.trigger.schedule) : ((r.trigger && r.trigger.on_demand) ? 'on demand' : '—');
        var deliver = (r.deliver && r.deliver.channel && r.deliver.channel !== 'none') ? esc(r.deliver.channel) : '—';
        var next = (r.next_run) ? '<span class="tag tag-ok">' + esc(fmtWhen(r.next_run)) + '</span>' : '<span class="tag tag-muted">' + (r.trigger && r.trigger.on_demand ? 'on demand' : '—') + '</span>';
        return '<tr data-name="' + esc(r.name) + '">' +
          '<td>' + esc(r.name) + (r.enabled === false ? ' <span class="card-hint">(disabled)</span>' : '') + '</td>' +
          '<td>' + n + ' · ' + esc(stepsPreview) + '</td>' +
          '<td>' + trigger + '</td>' +
          '<td>' + deliver + '</td>' +
          '<td>' + next + '</td>' +
          '<td class="actions">' +
            '<button class="act-btn" data-routine-run="' + esc(r.name) + '" title="Run now">▶</button>' +
            '<button class="act-btn" data-routine-edit="' + esc(r.name) + '" title="Edit">✎</button>' +
            '<button class="act-btn danger" data-routine-delete="' + esc(r.name) + '" title="Delete">✕</button>' +
          '</td></tr>';
      }).join('');
    } catch(e){ body.innerHTML = '<tr><td colspan="6" class="panel-error">Failed to load routines.</td></tr>'; }
  }
  $('routinesBody').addEventListener('click', function(e){
    var runBtn2 = e.target.closest('[data-routine-run]');
    var editBtn = e.target.closest('[data-routine-edit]');
    var delBtn = e.target.closest('[data-routine-delete]');
    if (runBtn2) runRoutineNow(runBtn2.getAttribute('data-routine-run'));
    else if (editBtn) editRoutine(editBtn.getAttribute('data-routine-edit'));
    else if (delBtn) {
      var name = delBtn.getAttribute('data-routine-delete');
      confirmDelete({
        title: 'Delete routine', message: 'Delete "' + name + '"? This can\'t be undone.',
        onConfirm: async function(){
          try { await fetch('/routines/' + encodeURIComponent(name), { method: 'DELETE' }); toast('Deleted ' + name, 'ok'); }
          catch(e){ toast('Delete failed: ' + e.message, 'err'); }
          loadRoutines();
        }
      });
    }
  });
  $('routinesRefreshBtn').addEventListener('click', loadRoutines);

  async function runRoutineNow(name){
    var out = $('routineRunOut');
    out.style.display = 'block';
    out.textContent = 'running ' + name + '…';
    try {
      var j = await (await fetch('/routines/' + encodeURIComponent(name) + '/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deliver: true }) })).json();
      if (!j.ok){
        out.textContent = '✗ ' + (j.error || j.detail || 'failed');
        toast('Routine failed', 'err');
        return;
      }
      var note = '';
      if (j.channel) note = j.delivered ? ('\n\n✓ delivered via ' + j.channel)
                                        : ('\n\n✗ delivery failed: ' + (j.delivery_error || 'unknown'));
      out.textContent = '▶ ' + name + ':\n\n' + (j.output || '(no output)') + note;
      if (j.channel && !j.delivered) toast('Ran, but delivery failed: ' + (j.delivery_error || 'unknown'), 'err');
      else if (j.delivered) toast(name + ' ran + delivered via ' + j.channel, 'ok');
      else toast(name + ' ran successfully', 'ok');
    } catch(e){ out.textContent = '✗ ' + e.message; }
  }

  function optTags(list, sel){
    return (list||[]).map(function(x){ return '<option' + (x === sel ? ' selected' : '') + '>' + esc(x) + '</option>'; }).join('');
  }
  // The argument contract for a tool, plus a one-click skeleton for the args box. `tool_params`
  // may be absent (older backend) — degrade to silence rather than to a broken step card.
  function rbArgsHintHtml(toolName){
    var specs = (rbMeta.tool_params || {})[toolName];
    if (!specs) return '';
    if (!specs.length) return '<span class="rb-hint-none">takes no arguments — leave this blank</span>';
    var skeleton = {};
    specs.forEach(function(p){ if (p.required) skeleton[p.name] = ''; });
    var rows = specs.map(function(p){
      return '<div class="rb-hint-row"><code>' + esc(p.name) + '</code>' +
        '<span class="rb-hint-type">' + esc(p.type) + (p.required ? ', required' : ', optional') + '</span>' +
        (p.description ? '<span class="rb-hint-desc">' + esc(p.description) + '</span>' : '') + '</div>';
    }).join('');
    var fill = Object.keys(skeleton).length ? JSON.stringify(skeleton) : '{}';
    return rows + '<button type="button" class="rb-hint-fill" data-fill=\'' + esc(fill) +
           '\'>insert template</button>';
  }

  function rbStepCard(s, i){
    var isTool = s.type === 'tool';
    var card = document.createElement('div');
    card.className = 'rb-step';
    card.innerHTML =
      '<div class="rb-step-head"><span class="rb-badge">' + (isTool ? 'TOOL' : 'MODEL') + '</span>' +
      '<input class="rb-id" placeholder="step id (snake_case)">' +
      '<span class="rb-move"><button data-up="' + i + '" title="move up">↑</button>' +
      '<button data-down="' + i + '" title="move down">↓</button>' +
      '<button data-rm="' + i + '" title="remove">✕</button></span></div>' +
      (isTool
        ? '<div class="rb-row"><select class="rb-tool">' + optTags(rbMeta.tools, s.tool) + '</select>' +
          '<label class="rb-opt"><input type="checkbox" class="rb-optional"> optional (continue on error)</label></div>' +
          '<input class="rb-args" placeholder=\'args as JSON, e.g. {"location": "Atlanta, GA"}\'>' +
          '<div class="rb-args-hint"></div>'
        : '<textarea class="rb-prompt" placeholder="prompt — reference earlier steps with {{step_id}}"></textarea>' +
          '<div class="rb-row"><span class="rb-opt">skill</span><select class="rb-skill"><option value=""></option>' + optTags(rbMeta.skills, s.skill) + '</select></div>');
    card.querySelector('.rb-id').value = s.id || '';
    if (isTool){
      card.querySelector('.rb-args').value = (s.args && Object.keys(s.args).length) ? JSON.stringify(s.args) : '';
      card.querySelector('.rb-optional').checked = !!s.optional;
      // A tool step's args are hand-typed JSON, so the step has to state the tool's contract —
      // otherwise the only way to learn a tool's arguments is to go read its source.
      var sel = card.querySelector('.rb-tool'), hint = card.querySelector('.rb-args-hint');
      var paint = function(){ hint.innerHTML = rbArgsHintHtml(sel.value); };
      sel.addEventListener('change', paint);
      hint.addEventListener('click', function(e){
        var b = e.target.closest('[data-fill]');
        if (!b) return;
        card.querySelector('.rb-args').value = b.getAttribute('data-fill');
      });
      paint();
    } else {
      card.querySelector('.rb-prompt').value = s.prompt || '';
    }
    return card;
  }
  function rbRender(){
    var c = $('rbSteps'); c.innerHTML = '';
    (rbModel.steps||[]).forEach(function(s,i){ c.appendChild(rbStepCard(s,i)); });
    c.querySelectorAll('[data-up]').forEach(function(b){ b.onclick = function(){ rbMove(+b.dataset.up, -1); }; });
    c.querySelectorAll('[data-down]').forEach(function(b){ b.onclick = function(){ rbMove(+b.dataset.down, 1); }; });
    c.querySelectorAll('[data-rm]').forEach(function(b){ b.onclick = function(){ rbRemove(+b.dataset.rm); }; });
    $('rbName').value = rbModel.name || '';
    $('rbDesc').value = rbModel.description || '';
    $('rbEnabled').checked = rbModel.enabled !== false;
    $('rbChannel').innerHTML = optTags(rbMeta.channels, (rbModel.deliver||{}).channel || 'telegram');
    $('rbSubject').value = (rbModel.deliver||{}).subject || '';
    $('rbSchedule').value = (rbModel.trigger||{}).schedule || '';
    $('rbPhrases').value = ((rbModel.trigger||{}).phrases || []).join(', ');
    var ids = (rbModel.steps||[]).map(function(s){ return s.id; }).filter(Boolean);
    $('rbOutput').innerHTML = '<option value="">(last step)</option>' + optTags(ids, rbModel.output || '');
  }
  function rbCollect(){
    rbModel.steps = Array.prototype.map.call($('rbSteps').querySelectorAll('.rb-step'), function(card){
      var id = card.querySelector('.rb-id').value.trim();
      if (card.querySelector('.rb-tool')){
        var args = {}, raw = card.querySelector('.rb-args').value.trim();
        if (raw) { try { args = JSON.parse(raw); } catch(e){ args = raw; } }
        return { type: 'tool', id: id, tool: card.querySelector('.rb-tool').value, args: args, optional: card.querySelector('.rb-optional').checked };
      }
      return { type: 'model', id: id, prompt: card.querySelector('.rb-prompt').value, skill: card.querySelector('.rb-skill').value || '' };
    });
    rbModel.name = $('rbName').value.trim();
    rbModel.description = $('rbDesc').value;
    rbModel.enabled = $('rbEnabled').checked;
    rbModel.output = $('rbOutput').value;
    rbModel.deliver = { channel: $('rbChannel').value, subject: $('rbSubject').value };
    rbModel.trigger = { on_demand: true, schedule: $('rbSchedule').value.trim(),
      phrases: $('rbPhrases').value.split(',').map(function(x){ return x.trim(); }).filter(Boolean) };
  }
  function rbMove(i,d){ rbCollect(); var j = i+d; if (j<0 || j>=rbModel.steps.length) return;
    var t = rbModel.steps[i]; rbModel.steps[i] = rbModel.steps[j]; rbModel.steps[j] = t; rbRender(); }
  function rbRemove(i){ rbCollect(); rbModel.steps.splice(i,1); rbRender(); }
  function rbAdd(type){
    rbCollect(); var n = rbModel.steps.length + 1;
    rbModel.steps.push(type === 'tool'
      ? { type: 'tool', id: 'step' + n, tool: rbMeta.tools[0] || '', args: {} }
      : { type: 'model', id: 'step' + n, prompt: '', skill: '' });
    rbRender();
  }
  $('rbAddTool').addEventListener('click', function(){ rbAdd('tool'); });
  $('rbAddModel').addEventListener('click', function(){ rbAdd('model'); });
  $('rbRawToggle').addEventListener('change', function(e){
    var showRaw = e.target.checked;
    $('rbRawField').style.display = showRaw ? 'block' : 'none';
    if (showRaw){ rbCollect(); $('rbRawJson').value = pretty(rbModel); }
    else {
      try { rbModel = JSON.parse($('rbRawJson').value); }
      catch(err){ $('routineEditMsg').textContent = '✗ invalid JSON: ' + err.message; e.target.checked = true; $('rbRawField').style.display = 'block'; return; }
      rbRender();
    }
  });
  async function openRoutineEditor(obj, title){
    if (!rbMetaLoaded){
      try { rbMeta = await (await fetch('/routine-meta')).json(); rbMetaLoaded = true; } catch(e){}
    }
    rbModel = JSON.parse(JSON.stringify(obj));
    $('routineModalTitle').textContent = title || 'Routine editor';
    $('rbRawToggle').checked = false;
    $('rbRawField').style.display = 'none';
    $('routineEditMsg').textContent = '';
    rbRender();
    openModal('routineModal');
  }
  $('newRoutineBtn').addEventListener('click', function(){ openRoutineEditor(ROUTINE_TEMPLATE, 'New routine'); });
  async function editRoutine(name){
    try {
      var r = await (await fetch('/routines/' + encodeURIComponent(name))).json();
      openRoutineEditor(r, 'Edit — ' + name);
    } catch(e){ toast('Could not load routine: ' + e.message, 'err'); }
  }
  $('rbSaveBtn').addEventListener('click', async function(){
    if ($('rbRawField').style.display !== 'none'){
      try { rbModel = JSON.parse($('rbRawJson').value); }
      catch(e){ $('routineEditMsg').textContent = '✗ invalid JSON: ' + e.message; return; }
    } else { rbCollect(); }
    try {
      var r = await fetch('/routines', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(rbModel) });
      if (r.ok) { $('routineEditMsg').textContent = '✓ saved'; toast('Saved routine ' + rbModel.name, 'ok'); closeAllModals(); loadRoutines(); }
      else { var j = await r.json().catch(function(){ return {}; }); $('routineEditMsg').textContent = '✗ ' + (j.detail || ('HTTP ' + r.status)); }
    } catch(e){ $('routineEditMsg').textContent = '✗ ' + e.message; }
  });

  /* ---- scheduled tasks ---- */
  async function loadScheduled(){
    var body = $('scheduledBody');
    try {
      var jobs = await (await fetch('/scheduled')).json();
      if (!Array.isArray(jobs) || !jobs.length){
        $('scheduledCount').textContent = '';
        body.innerHTML = '<tr><td colspan="5" class="empty"><span class="empty-title">No scheduled tasks</span>the agent creates these itself when you ask it to schedule something.</td></tr>';
        return;
      }
      $('scheduledCount').textContent = ' ' + jobs.length;
      body.innerHTML = jobs.map(function(j){
        return '<tr><td>' + esc(j.instruction || '') + '</td><td>' + esc(j.schedule || '') + '</td>' +
          '<td class="num">' + esc(fmtWhen(j.next_run)) + '</td><td class="num">' + esc(String(j.runs != null ? j.runs : 0)) + '</td>' +
          '<td class="actions"><button class="act-btn danger" data-sched-delete="' + esc(j.id) +
          '" title="Cancel this scheduled task">✕</button></td></tr>';
      }).join('');
    } catch(e){ body.innerHTML = '<tr><td colspan="5" class="panel-error">Failed to load scheduled tasks.</td></tr>'; }
  }
  $('scheduledBody').addEventListener('click', function(e){
    var b = e.target.closest('[data-sched-delete]');
    if (!b) return;
    var id = b.getAttribute('data-sched-delete');
    var task = b.closest('tr').querySelector('td').textContent;
    confirmDelete({
      title: 'Cancel scheduled task',
      message: 'Cancel "' + task + '"? It will stop running. This can\'t be undone.',
      onConfirm: async function(){
        try {
          // The fetch shim RESOLVES a 401 rather than rejecting, so a failed mutation is only
          // visible on res.ok — without this check a missing admin token toasts success and the
          // row silently reappears on the next refresh.
          var res = await fetch('/scheduled/delete', { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) });
          if (!res.ok) { toast('Cancel failed (' + res.status + ')', 'err'); }
          else { toast('Cancelled scheduled task', 'ok'); }
        } catch(e){ toast('Cancel failed: ' + e.message, 'err'); }
        loadScheduled();
      }
    });
  });

  /* ---- watches ---- */
  async function loadWatches(){
    var body = $('watchesBody');
    try {
      var d = await (await fetch('/watches')).json();
      var ws = (d.watches || []).filter(function(w){ return w.active; });
      $('watchesCount').textContent = ws.length ? (' ' + ws.length) : '';
      if (!ws.length){
        body.innerHTML = '<tr><td colspan="4" class="empty"><span class="empty-title">Not watching anything</span>ask Argus to watch a page for changes.</td></tr>';
        return;
      }
      body.innerHTML = ws.map(function(w){
        return '<tr data-id="' + esc(w.id) + '"><td><a href="' + esc(w.url) + '" target="_blank" rel="noopener">' + esc(w.description || w.url) + '</a></td>' +
          '<td class="num">every ' + esc(w.interval_minutes) + 'm</td><td><span class="tag tag-muted">active</span></td>' +
          '<td class="actions"><button class="act-btn danger" data-watch-delete="' + esc(w.id) + '" title="Stop watching">✕</button></td></tr>';
      }).join('');
    } catch(e){ body.innerHTML = '<tr><td colspan="4" class="panel-error">Failed to load watches.</td></tr>'; }
  }
  $('watchesBody').addEventListener('click', function(e){
    var b = e.target.closest('[data-watch-delete]');
    if (!b) return;
    var id = b.getAttribute('data-watch-delete');
    var url = b.closest('tr').querySelector('td').textContent;
    confirmDelete({
      title: 'Stop watching', message: 'Stop watching "' + url + '"? This can\'t be undone.',
      onConfirm: async function(){
        try {
          // Same as everywhere else: the fetch shim resolves a 401, so success must be read off
          // res.ok or a missing admin token reports "Stopped watching" and changes nothing.
          var res = await fetch('/watches/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) });
          if (!res.ok) { toast('Delete failed (' + res.status + ')', 'err'); }
          else { toast('Stopped watching', 'ok'); }
        }
        catch(e){ toast('Delete failed: ' + e.message, 'err'); }
        loadWatches();
      }
    });
  });
  /* ================= DATA: files / knowledge / tables / artifacts ================= */
  var filesData = [];
  var filesQuery = '';
  function fileExt(name){ var i = name.lastIndexOf('.'); return i < 0 ? '' : name.slice(i+1).toLowerCase(); }
  function renderFiles(){
    var body = $('filesBody');
    var q = filesQuery.trim().toLowerCase();
    var filtered = q ? filesData.filter(function(f){ return f.name.toLowerCase().indexOf(q) > -1; }) : filesData.slice();
    var mode = $('filesSort').value;
    filtered.sort(function(a,b){
      if (mode === 'name') return a.name.localeCompare(b.name);
      if (mode === 'size') return (b.size||0) - (a.size||0);
      if (mode === 'type') return fileExt(a.name).localeCompare(fileExt(b.name)) || a.name.localeCompare(b.name);
      return (b.modified||0) - (a.modified||0);
    });
    $('filesCount').textContent = filesData.length ? (q ? (filtered.length + ' of ' + filesData.length) : (filesData.length + ' files')) : '';
    if (!filesData.length){
      body.innerHTML = '<tr><td colspan="5" class="empty"><span class="empty-title">No files yet</span>Argus saves reports and charts here — upload one, or ask the agent to write a file.</td></tr>';
      return;
    }
    if (!filtered.length){
      body.innerHTML = '<tr><td colspan="5" class="empty">No files match &ldquo;' + esc(filesQuery) + '&rdquo;</td></tr>';
      return;
    }
    body.innerHTML = filtered.map(function(f){
      return '<tr data-name="' + esc(f.name) + '">' +
        '<td><a href="#" class="file-open" data-fn="' + esc(f.name) + '">' + esc(f.name) + '</a></td>' +
        '<td><span class="tag tag-muted">' + esc(fileExt(f.name) || '—') + '</span></td>' +
        '<td class="num">' + esc(fmtBytes(f.size||0)) + '</td>' +
        '<td class="num">' + (f.modified ? esc(relTime(f.modified*1000)) : '—') + '</td>' +
        '<td class="actions">' +
          '<button class="act-btn file-open" data-fn="' + esc(f.name) + '" title="Preview">◎</button>' +
          '<a class="act-btn" href="/files/' + encodeURIComponent(f.name) + '" title="Download">⬇</a>' +
          '<button class="act-btn danger" data-file-delete="' + esc(f.name) + '" title="Delete">✕</button>' +
        '</td></tr>';
    }).join('');
  }
  async function loadFiles(){
    try {
      var d = await (await fetch('/files')).json();
      filesData = d.files || [];
      renderFiles();
    } catch(e){ $('filesBody').innerHTML = '<tr><td colspan="4" class="panel-error">Failed to load files.</td></tr>'; }
  }
  $('filesSearch').addEventListener('input', function(e){ filesQuery = e.target.value; renderFiles(); });
  $('filesSort').addEventListener('change', renderFiles);
  $('filesBody').addEventListener('click', function(e){
    var open = e.target.closest('.file-open');
    var del = e.target.closest('[data-file-delete]');
    if (open) { e.preventDefault(); openFilePreview(open.getAttribute('data-fn')); }
    else if (del) {
      var name = del.getAttribute('data-file-delete');
      confirmDelete({
        title: 'Delete file', message: 'Delete "' + name + '"? This can\'t be undone.',
        onConfirm: async function(){
          try { await fetch('/files/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) }); toast('Deleted ' + name, 'ok'); }
          catch(e){ toast('Delete failed: ' + e.message, 'err'); }
          loadFiles();
        }
      });
    }
  });
  $('fileUploadInput').addEventListener('change', async function(){
    var input = this;
    if (!input.files || !input.files[0]) return;
    var fd = new FormData(); fd.append('file', input.files[0]);
    try {
      var r = await fetch('/files/upload', { method: 'POST', body: fd });
      if (!r.ok) toast('Upload failed (' + r.status + ')', 'err'); else toast('Uploaded', 'ok');
    } catch(e){ toast('Upload failed: ' + e.message, 'err'); }
    input.value = ''; loadFiles();
  });

  var PV_IMG = ['png','jpg','jpeg','gif','webp','svg','bmp','ico','avif'];
  var PV_TEXT = ['txt','csv','tsv','json','log','py','js','ts','html','htm','css','yaml','yml','xml','sh','sql','ini','toml','conf','cfg','md','markdown'];
  var currentPreviewFile = null;
  function openFilePreview(name){
    currentPreviewFile = name;
    var ext = fileExt(name);
    var url = '/files/' + encodeURIComponent(name);
    $('fileModalTitle').textContent = name;
    $('fileModalDownloadBtn').href = url;
    $('fileModalDownloadBtn').setAttribute('download', name);
    var bodyEl = $('fileModalBody');
    bodyEl.innerHTML = '<div class="empty">Loading…</div>';
    openModal('fileModal');
    if (PV_IMG.indexOf(ext) > -1){
      bodyEl.innerHTML = '';
      var img = document.createElement('img'); img.src = url + '?inline=1'; img.alt = name; img.style.maxWidth = '100%';
      bodyEl.appendChild(img);
    } else if (ext === 'pdf'){
      bodyEl.innerHTML = '';
      var fr = document.createElement('iframe'); fr.src = url + '?inline=1'; fr.title = name;
      fr.style.width = '100%'; fr.style.height = '100%'; fr.style.flex = '1'; fr.style.border = '0';
      bodyEl.appendChild(fr);
    } else if (PV_TEXT.indexOf(ext) > -1){
      fetch(url + '?inline=1').then(function(r){ return r.text(); }).then(function(t){
        var pre = document.createElement('pre');
        pre.className = 'code-block'; pre.style.whiteSpace = 'pre-wrap'; pre.style.maxHeight = '60vh';
        pre.textContent = t;
        bodyEl.innerHTML = ''; bodyEl.appendChild(pre);
      }).catch(function(){ bodyEl.innerHTML = '<div class="panel-error">Could not load this file.</div>'; });
    } else {
      bodyEl.innerHTML = '<div class="card-hint">No inline preview for .' + esc(ext || '?') + ' files — use Download.</div>';
    }
  }
  $('fileModalDeleteBtn').addEventListener('click', function(){
    if (!currentPreviewFile) return;
    var name = currentPreviewFile;
    confirmDelete({
      title: 'Delete file', message: 'Delete "' + name + '"? This can\'t be undone.',
      onConfirm: async function(){
        closeAllModals();
        try { await fetch('/files/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) }); toast('Deleted ' + name, 'ok'); }
        catch(e){ toast('Delete failed: ' + e.message, 'err'); }
        loadFiles();
      }
    });
  });

  /* ---- knowledge ---- */
  var knowledgeData = [];
  var knowledgeQuery = '';
  function renderKnowledge(){
    var el = $('knowledgeList');
    var q = knowledgeQuery.trim().toLowerCase();
    var filtered = q ? knowledgeData.filter(function(k){ return k.source.toLowerCase().indexOf(q) > -1; }) : knowledgeData.slice();
    $('knowledgeCount').textContent = knowledgeData.length ? (q ? (filtered.length + ' of ' + knowledgeData.length) : (knowledgeData.length + ' sources')) : '';
    if (!knowledgeData.length){
      el.innerHTML = '<div class="empty"><span class="empty-title">Nothing added yet</span>ask Argus to add a document or URL to its knowledge base.</div>';
      return;
    }
    if (!filtered.length){ el.innerHTML = '<div class="empty">No sources match &ldquo;' + esc(knowledgeQuery) + '&rdquo;</div>'; return; }
    el.innerHTML = filtered.map(function(k){
      return '<div class="list-item"><div class="list-main"><div class="list-title">' + esc(k.source) + '</div>' +
        '<div class="list-sub num">' + k.chunks + ' chunk' + (k.chunks === 1 ? '' : 's') + '</div></div>' +
        '<button class="act-btn danger" data-know-forget="' + esc(k.source) + '" title="Forget source">✕</button></div>';
    }).join('');
  }
  async function loadKnowledge(){
    try {
      var d = await (await fetch('/knowledge')).json();
      knowledgeData = d.sources || [];
      var s = d.stats || {};
      $('knowledgeHint').textContent = (s.semantic ? 'RAG · semantic' : 'RAG · keyword only') + ' · ' + (s.chunks||0) + ' chunks';
      renderKnowledge();
    } catch(e){ $('knowledgeList').innerHTML = '<div class="panel-error">Failed to load knowledge.</div>'; }
  }
  $('knowledgeSearch').addEventListener('input', function(e){ knowledgeQuery = e.target.value; renderKnowledge(); });
  $('knowledgeList').addEventListener('click', function(e){
    var b = e.target.closest('[data-know-forget]');
    if (!b) return;
    var src = b.getAttribute('data-know-forget');
    confirmDelete({
      title: 'Forget source', message: 'Delete "' + src + '"? This can\'t be undone.',
      onConfirm: async function(){
        try { await fetch('/knowledge/forget', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source: src }) }); toast('Forgot ' + src, 'ok'); }
        catch(e){ toast('Forget failed: ' + e.message, 'err'); }
        loadKnowledge();
      }
    });
  });

  /* ---- artifacts ---- */
  var artifactsData = [];
  var artifactsQuery = '';
  function renderArtifacts(){
    var el = $('artifactsList');
    var q = artifactsQuery.trim().toLowerCase();
    var filtered = q ? artifactsData.filter(function(a){ return (a.title||a.filename).toLowerCase().indexOf(q) > -1; }) : artifactsData.slice();
    filtered.sort(function(a,b){ return (b.modified||0) - (a.modified||0); });
    $('artifactsCount').textContent = artifactsData.length ? (q ? (filtered.length + ' of ' + artifactsData.length) : (artifactsData.length + ' artifacts')) : '';
    if (!artifactsData.length){
      el.innerHTML = '<div class="empty"><span class="empty-title">No artifacts yet</span>ask Argus to build a web page or dashboard.</div>';
      return;
    }
    if (!filtered.length){ el.innerHTML = '<div class="empty">No artifacts match &ldquo;' + esc(artifactsQuery) + '&rdquo;</div>'; return; }
    el.innerHTML = filtered.map(function(a){
      return '<div class="list-item"><div class="list-main"><div class="list-title"><a href="' + esc(a.url) + '" target="_blank" rel="noopener">' + esc(a.title || a.filename) + '</a></div>' +
        '<div class="list-sub">' + esc(a.filename) + ' · built ' + esc(a.modified ? relTime(a.modified*1000) + ' ago' : '—') + '</div></div>' +
        '<span class="tag tag-ok" style="margin-right:6px;">READY</span>' +
        '<button class="act-btn danger" data-art-delete="' + esc(a.filename) + '" title="Delete artifact">✕</button></div>';
    }).join('');
  }
  async function loadArtifacts(){
    try {
      var d = await (await fetch('/artifacts')).json();
      artifactsData = d.artifacts || [];
      renderArtifacts();
    } catch(e){ $('artifactsList').innerHTML = '<div class="panel-error">Failed to load artifacts.</div>'; }
  }
  $('artifactsSearch').addEventListener('input', function(e){ artifactsQuery = e.target.value; renderArtifacts(); });
  $('artifactsList').addEventListener('click', function(e){
    var b = e.target.closest('[data-art-delete]');
    if (!b) return;
    var fn = b.getAttribute('data-art-delete');
    confirmDelete({
      title: 'Delete artifact', message: 'Delete "' + fn + '"? This can\'t be undone.',
      onConfirm: async function(){
        try {
          var r = await (await fetch('/artifacts/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filename: fn }) })).json();
          toast(r.ok === false ? 'Delete failed' : 'Deleted ' + fn, r.ok === false ? 'err' : 'ok');
        } catch(e){ toast('Delete failed: ' + e.message, 'err'); }
        loadArtifacts();
      }
    });
  });

  /* ---- tables ---- */
  var tablesData = [];
  function renderTablesList(){
    var el = $('tablesList');
    $('tablesCount').textContent = tablesData.length ? (' ' + tablesData.length) : '';
    if (!tablesData.length){
      el.innerHTML = '<div class="empty"><span class="empty-title">No tables yet</span>ask Argus to create a table to store structured data.</div>';
      return;
    }
    el.innerHTML = tablesData.map(function(t){
      var cols = t.columns || [];
      var colNames = cols.map(function(c){ return String(c).split(' ')[0]; });
      var preview = colNames.slice(0,4).join(' · ') + (colNames.length > 4 ? ' …' : '');
      return '<div class="list-item table-row" data-table="' + esc(t.name) + '" tabindex="0" role="button" aria-label="Browse ' + esc(t.name) + '">' +
          '<div class="list-main"><div class="list-title num">' + esc(t.name) + ' <span class="card-hint">— ' + (t.rows||0) + ' rows · ' + cols.length + ' columns</span></div>' +
          '<div class="list-sub">' + esc(preview) + '</div></div>' +
          '<button class="act-btn danger" data-drop-table="' + esc(t.name) + '" title="Drop table">✕</button>' +
        '</div>';
    }).join('');
  }
  async function loadTables(){
    try {
      var d = await (await fetch('/tables')).json();
      tablesData = d.tables || [];
      renderTablesList();
    } catch(e){ $('tablesList').innerHTML = '<div class="panel-error">Failed to load tables.</div>'; }
  }
  $('tablesList').addEventListener('click', function(e){
    var dropBtn = e.target.closest('[data-drop-table]');
    if (dropBtn){
      e.stopPropagation();
      var name = dropBtn.getAttribute('data-drop-table');
      var def = tablesData.find(function(t){ return t.name === name; });
      confirmDelete({
        title: 'Drop table',
        message: 'This permanently deletes "' + name + '" and all ' + (def ? def.rows : 0) + ' rows.',
        requireText: name,
        onConfirm: async function(){
          try {
            var r = await (await fetch('/tables/drop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) })).json();
            toast(r.ok === false ? 'Drop failed' : 'Dropped ' + name, r.ok === false ? 'err' : 'ok');
          } catch(e){ toast('Drop failed: ' + e.message, 'err'); }
          loadTables();
        }
      });
      return;
    }
    var row = e.target.closest('.table-row');
    if (row) openTableViewer(row.getAttribute('data-table'));
  });
  $('tablesList').addEventListener('keydown', function(e){
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var row = e.target.closest('.table-row');
    if (!row) return;
    e.preventDefault();
    openTableViewer(row.getAttribute('data-table'));
  });

  /* ---- table viewer modal (real backend paging via /tables/{name}/rows) ---- */
  var tvState = { table: null, columns: [], rows: [], total: 0, limit: 50, offset: 0, query: '' };
  async function openTableViewer(name){
    tvState = { table: name, columns: [], rows: [], total: 0, limit: 50, offset: 0, query: '' };
    $('tvTitle').textContent = name;
    $('tvFilter').value = '';
    $('tvSchema').innerHTML = '';
    $('tvTbody').innerHTML = '<tr><td class="empty">loading…</td></tr>';
    openModal('tableViewerModal');
    await tvLoadPage();
    setTimeout(function(){ $('tvFilter').focus(); }, 0);
  }
  async function tvLoadPage(){
    try {
      var url = '/tables/' + encodeURIComponent(tvState.table) + '/rows?limit=' + tvState.limit + '&offset=' + tvState.offset;
      var d = await (await fetch(url)).json();
      tvState.columns = d.columns || [];
      tvState.rows = d.rows || [];
      tvState.total = d.total || 0;
      $('tvSchema').innerHTML = tvState.columns.map(function(c){ return '<span class="col-chip">' + esc(c) + '</span>'; }).join('');
      renderTvTable();
    } catch(e){
      $('tvTbody').innerHTML = '<tr><td class="panel-error">Failed to load rows.</td></tr>';
    }
  }
  function tvFilteredRows(){
    var q = tvState.query.trim().toLowerCase();
    if (!q) return tvState.rows;
    return tvState.rows.filter(function(r){
      return tvState.columns.some(function(c){ return String(r[c]).toLowerCase().indexOf(q) > -1; });
    });
  }
  function renderTvTable(){
    var cols = tvState.columns;
    if (!cols.length){
      $('tvThead').innerHTML = '';
      $('tvTbody').innerHTML = '<tr><td class="empty">This table has no rows yet.</td></tr>';
      $('tvRowCount').textContent = '0 rows';
      $('tvPageLabel').textContent = '';
      $('tvPrev').disabled = true; $('tvNext').disabled = true;
      return;
    }
    $('tvThead').innerHTML = '<tr>' + cols.map(function(c){ return '<th>' + esc(c) + '</th>'; }).join('') + '</tr>';
    var rows = tvFilteredRows();
    $('tvTbody').innerHTML = rows.length ? rows.map(function(r){
      return '<tr>' + cols.map(function(c){
        var v = r[c];
        var display = (v === null || v === undefined) ? '—' : v;
        var cls = (typeof v === 'number') ? ' class="num"' : '';
        return '<td' + cls + '>' + esc(display) + '</td>';
      }).join('') + '</tr>';
    }).join('') : '<tr><td colspan="' + cols.length + '" class="empty">No rows match &ldquo;' + esc(tvState.query) + '&rdquo;</td></tr>';
    $('tvRowCount').textContent = tvState.query
      ? (rows.length + ' of ' + tvState.rows.length + ' loaded rows match')
      : (tvState.rows.length + ' of ' + tvState.total + ' rows loaded');
    var totalPages = Math.max(1, Math.ceil(tvState.total / tvState.limit));
    var curPage = Math.floor(tvState.offset / tvState.limit) + 1;
    $('tvPageLabel').textContent = 'page ' + curPage + ' of ' + totalPages;
    $('tvPrev').disabled = tvState.offset <= 0;
    $('tvNext').disabled = (tvState.offset + tvState.limit) >= tvState.total;
  }
  $('tvFilter').addEventListener('input', function(e){ tvState.query = e.target.value; renderTvTable(); });
  $('tvPrev').addEventListener('click', function(){ if (tvState.offset > 0) { tvState.offset = Math.max(0, tvState.offset - tvState.limit); tvLoadPage(); } });
  $('tvNext').addEventListener('click', function(){ if (tvState.offset + tvState.limit < tvState.total) { tvState.offset += tvState.limit; tvLoadPage(); } });
  $('tvDropBtn').addEventListener('click', function(){
    var name = tvState.table;
    var def = tablesData.find(function(t){ return t.name === name; });
    confirmDelete({
      title: 'Drop table',
      message: 'This permanently deletes "' + name + '" and all ' + (def ? def.rows : tvState.total) + ' rows.',
      requireText: name,
      onConfirm: async function(){
        closeAllModals();
        try {
          var r = await (await fetch('/tables/drop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) })).json();
          toast(r.ok === false ? 'Drop failed' : 'Dropped ' + name, r.ok === false ? 'err' : 'ok');
        } catch(e){ toast('Drop failed: ' + e.message, 'err'); }
        loadTables();
      }
    });
  });
  /* ================= MEMORY ================= */
  async function loadMemoryStats(){
    try {
      var m = await (await fetch('/memory/stats?session_id=' + SESSION)).json();
      $('memCount').textContent = m.count != null ? m.count : 0;
      $('memTrust').textContent = m.avg_trust != null ? Number(m.avg_trust).toFixed(2) : '–';
      $('memSemantic').textContent = m.semantic_enabled ? 'on' : 'off';
      $('memSemantic').style.color = m.semantic_enabled ? 'var(--ok)' : 'var(--faint)';
    } catch(e){ /* stats optional */ }
    loadMemoryFacts();
  }
  async function loadMemoryFacts(){
    var el = $('memoryList');
    try {
      var d = await (await fetch('/memory/list?session_id=' + SESSION)).json();
      var facts = d.facts || [];
      $('memFactsCount').textContent = facts.length ? (' ' + facts.length) : '';
      if (!facts.length){
        el.innerHTML = '<div class="empty"><span class="empty-title">Nothing saved yet</span>Argus remembers facts you tell it, and things it infers as you use it.</div>';
        return;
      }
      el.innerHTML = facts.map(function(f){
        var trust = f.trust != null ? f.trust : 0;
        return '<div class="list-item"><div class="list-main"><div class="list-title">' + esc(f.text) + '</div>' +
          '<div class="list-sub">trust ' + Number(trust).toFixed(2) + ' · ' + esc(f.source || 'user') + '</div></div>' +
          '<div class="trust-bar" title="trust ' + Number(trust).toFixed(2) + '"><i style="width:' + Math.round(trust*100) + '%;"></i></div>' +
          '<button class="act-btn danger" data-mem-forget="' + f.id + '" style="margin-left:8px;" title="Delete">✕</button></div>';
      }).join('');
    } catch(e){ el.innerHTML = '<div class="panel-error">Failed to load memories.</div>'; }
  }
  $('memoryList').addEventListener('click', function(e){
    var b = e.target.closest('[data-mem-forget]');
    if (!b) return;
    var id = b.getAttribute('data-mem-forget');
    var text = b.closest('.list-item').querySelector('.list-title').textContent;
    confirmDelete({
      title: 'Delete memory', message: 'Delete "' + text + '"? This can\'t be undone.',
      onConfirm: async function(){
        try {
          var r = await (await fetch('/memory/forget', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: SESSION, id: Number(id) }) })).json();
          toast(r.ok === false ? 'Delete failed' : 'Deleted memory', r.ok === false ? 'err' : 'ok');
        } catch(e){ toast('Delete failed: ' + e.message, 'err'); }
        loadMemoryStats();
      }
    });
  });
  $('memRefreshBtn').addEventListener('click', loadMemoryStats);
  $('summarizeBtn').addEventListener('click', async function(){
    var btn = $('summarizeBtn'); btn.disabled = true;
    $('memSummarizeMeta').textContent = 'summarizing…';
    try {
      var r = await (await fetch('/memory/summary', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: SESSION }) })).json();
      var box = $('summaryBox');
      box.textContent = r.summary || '(no summary)';
      box.style.display = 'block';
      $('memSummarizeMeta').textContent = r.count != null ? (r.count + ' memories') : '';
    } catch(e){ $('memSummarizeMeta').textContent = 'summary failed'; }
    finally { btn.disabled = false; }
  });

  /* ================= RULES ================= */
  async function loadRules(){
    var el = $('rulesList');
    try {
      var d = await (await fetch('/rules/list')).json();
      var rules = d.rules || [];
      $('rulesCount').textContent = rules.length ? (' ' + rules.length) : '';
      if (!rules.length){
        el.innerHTML = '<div class="empty"><span class="empty-title">No standing rules yet</span>Add one below, or Argus will draft one automatically when you correct it.</div>';
        return;
      }
      el.innerHTML = rules.map(function(r){
        return '<div class="list-item"><div class="list-main"><div class="list-title' + (r.enabled ? '' : ' muted') + '">' +
          esc(r.text) + '</div></div>' +
          (r.source === 'auto' ? '<span class="tag tag-muted">auto</span>' : '') +
          '<label class="switch" style="margin-left:8px;" title="' + (r.enabled ? 'Enabled' : 'Disabled') + '">' +
          '<input type="checkbox" data-rule-toggle="' + esc(r.id) + '"' + (r.enabled ? ' checked' : '') + '><span class="track"><span class="thumb"></span></span></label>' +
          '<button class="act-btn danger" data-rule-forget="' + esc(r.id) + '" style="margin-left:8px;" title="Remove">✕</button></div>';
      }).join('');
    } catch(e){ el.innerHTML = '<div class="panel-error">Failed to load rules.</div>'; }
  }
  $('rulesList').addEventListener('change', function(e){
    var t = e.target.closest('[data-rule-toggle]');
    if (!t) return;
    var id = t.getAttribute('data-rule-toggle');
    fetch('/rules/toggle', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id, enabled: t.checked }) })
      .then(function(){ loadRules(); })
      .catch(function(){ toast('Toggle failed', 'err'); loadRules(); });
  });
  $('rulesList').addEventListener('click', function(e){
    var b = e.target.closest('[data-rule-forget]');
    if (!b) return;
    var id = b.getAttribute('data-rule-forget');
    var text = b.closest('.list-item').querySelector('.list-title').textContent;
    confirmDelete({
      title: 'Remove rule', message: 'Remove "' + text + '"? This can\'t be undone.',
      onConfirm: async function(){
        try {
          var r = await (await fetch('/rules/remove', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) })).json();
          toast(r.ok === false ? 'Remove failed' : 'Rule removed', r.ok === false ? 'err' : 'ok');
        } catch(e){ toast('Remove failed: ' + e.message, 'err'); }
        loadRules();
      }
    });
  });
  $('ruleAddBtn').addEventListener('click', async function(){
    var inp = $('ruleInput');
    var text = inp.value.trim();
    if (!text) { toast('Enter a rule', 'info'); return; }
    try {
      var r = await fetch('/rules/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text }) });
      if (!r.ok) { var e = await r.json().catch(function(){ return {}; }); throw new Error(e.detail || ('HTTP ' + r.status)); }
      inp.value = '';
      await loadRules();
      toast('Rule added', 'ok');
    } catch(e){ toast(e.message || 'Add failed', 'err'); }
  });
  $('ruleInput').addEventListener('keydown', function(e){ if (e.key === 'Enter') $('ruleAddBtn').click(); });

  /* ================= DEVELOPER: library / deps / trust ================= */
  // Per-tool Allow/Ask/Deny select. `permMap` is {key: {state, states}} from GET /permissions
  // (Task 5: one row per tool, plus the always-present "dep-install" sub-gate). Falls back to
  // ["allow","ask","deny"]/"allow" for ordinary tools if the map has no entry (or fetch failed),
  // and to ["ask","deny"]/"ask" for the binary "dep-install" gate.
  function permSelectHtml(key, permMap){
    var p = (permMap || {})[key] || {};
    var fallbackStates = key === 'dep-install' ? ['ask', 'deny'] : ['allow', 'ask', 'deny'];
    var states = p.states || fallbackStates;
    var state = p.state || states[0];
    var opts = states.map(function(s){
      return '<option value="' + esc(s) + '"' + (s === state ? ' selected' : '') + '>' + esc(s) + '</option>';
    }).join('');
    return '<select class="perm-select" data-perm-key="' + esc(key) + '" data-prev-state="' + esc(state) + '">' + opts + '</select>';
  }
  function libItemsHtml(arr, withTools, delKind, permMap){
    if (!arr || !arr.length) return '<div class="empty">(none yet)</div>';
    var isTool = !withTools;   // convention: tool call sites pass withTools=false, skill call sites pass true
    return arr.map(function(o){
      return '<div class="list-item"><div class="list-main"><div class="list-title">' + esc(o.name) +
        // Full description, not truncated: the row already wraps (white-space:normal), and a tool's
        // description IS its documentation here — clipping it at 140 chars hid the half that says
        // when to use the tool and what its arguments mean.
        (o.description ? '<div class="list-sub" style="font-family:var(--font-body); color:var(--muted); white-space:normal;">' + esc(o.description) + '</div>' : '') +
        (withTools && Array.isArray(o.tools) && o.tools.length ? '<div class="list-sub">tools: ' + esc(o.tools.join(', ')) + '</div>' : '') +
        '</div></div>' +   // close .list-title AND .list-main
        (isTool ? permSelectHtml(o.name, permMap) : '') +
        (delKind ? '<button class="act-btn danger" data-lib-delete="' + delKind + '" data-lib-name="' + esc(o.name) + '" title="Delete">✕</button>' : '') +
        '</div>';          // close .list-item — the ✕/select are now siblings of .list-main, so the flex row right-aligns them inline instead of stacking below
    }).join('');
  }
  async function loadLibrary(){
    try {
      var lib = await (await fetch('/library')).json();
      var permMap = {};
      try {
        var pd = await (await fetch('/permissions')).json();
        (pd.permissions || []).forEach(function(p){ permMap[p.key] = p; });
      } catch(e){ /* leave permMap empty; per-tool selects fall back to allow/ask/deny defaults */ }
      var t = lib.tools || {}, s = lib.skills || {};
      var cond = t.conditional_enabled || [];
      $('toolsBuiltin').innerHTML = libItemsHtml(t.builtin, false, null, permMap);
      $('toolsConditional').innerHTML = cond.length
        ? '<div class="card-hint" style="margin-bottom:6px;">available when their feature flag is on</div>' + libItemsHtml(cond, false, null, permMap)
        : '<div class="empty">(none active)</div>';
      $('toolsCreated').innerHTML = libItemsHtml(t.created, false, 'tool', permMap);
      $('skillsBuiltin').innerHTML = libItemsHtml(s.builtin, true);
      $('skillsCreated').innerHTML = libItemsHtml(s.created, true, 'skill');
      $('depInstallPerm').innerHTML = permSelectHtml('dep-install', permMap);
      var totalTools = (t.builtin||[]).length + (t.created||[]).length;
      var totalSkills = (s.builtin||[]).length + (s.created||[]).length;
      $('toolsCountBadge').textContent = totalTools + ' total';
      $('skillsCountBadge').textContent = totalSkills + ' total';
      document.querySelectorAll('[data-lib-delete]').forEach(function(b){
        b.addEventListener('click', function(){
          var kind = b.getAttribute('data-lib-delete'), name = b.getAttribute('data-lib-name');
          confirmDelete({
            title: 'Delete ' + kind, message: 'Delete the created ' + kind + ' "' + name + '"? This removes it from disk.',
            onConfirm: async function(){
              try {
                var r = await (await fetch('/library/' + kind + '/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) })).json();
                toast(r.ok === false ? 'Delete failed' : 'Deleted ' + name, r.ok === false ? 'err' : 'ok');
              } catch(e){ toast('Delete failed: ' + e.message, 'err'); }
              loadLibrary();
            }
          });
        });
      });
    } catch(e){
      ['toolsBuiltin','toolsConditional','toolsCreated','skillsBuiltin','skillsCreated'].forEach(function(id){ $(id).innerHTML = '<div class="panel-error">Failed to load.</div>'; });
    }
  }

  /* ---- interactive approvals: per-tool perm-select toggles + Pending approvals list ---- */
  // Delegated handler for every `.perm-select` on the page: per-tool rows rendered by
  // libItemsHtml() and the standalone dep-install control. POST body field is `key` (the
  // tool name, or "dep-install"), NOT `kind` — that was the old bespoke permMatrix's field.
  document.addEventListener('change', async function(e){
    var sel = e.target.closest('.perm-select');
    if (!sel) return;
    var key = sel.getAttribute('data-perm-key'), state = sel.value;
    var prev = sel.getAttribute('data-prev-state') || state;
    sel.disabled = true;
    try {
      var r = await fetch('/permissions/set', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: key, state: state }) });
      if (!r.ok) { var eb = await r.json().catch(function(){ return {}; }); throw new Error(eb.detail || ('HTTP ' + r.status)); }
      sel.setAttribute('data-prev-state', state);
      toast('Policy updated', 'ok');
    } catch(err){
      sel.value = prev;
      toast('Update failed: ' + err.message, 'err');
    }
    sel.disabled = false;
  });

  async function loadPendingApprovals(){
    var el = $('pendingApprovals');
    try {
      var pr = await (await fetch('/permissions')).json();   // gate labels + valid states, for the standing-policy select
      var gates = {};
      (pr.permissions || []).forEach(function(p){ gates[p.key] = p; });
      var d = await (await fetch('/approvals')).json();
      var pending = d.approvals || [];
      var badge = $('pendingApprovalsBadge');
      badge.textContent = pending.length;
      badge.className = 'tag ' + (pending.length ? 'tag-amber' : 'tag-muted');
      if (!pending.length){ el.innerHTML = '<div class="empty">Nothing awaiting approval.</div>'; return; }
      el.innerHTML = pending.map(function(r){
        var g = gates[r.kind] || {};
        var opts = (g.states || []).map(function(s){ return '<option value="' + esc(s) + '">' + esc(s) + '</option>'; }).join('');
        return '<div class="warn-box">' + esc(g.label || r.kind) +
          (r.target ? ': <strong>&nbsp;' + esc(r.target) + '&nbsp;</strong>' : '') +
          (r.prompt ? '<br>' + esc(r.prompt) : '') + '</div>' +
          '<div class="row-inline" style="justify-content:flex-end; margin-bottom:14px;">' +
          (opts ? '<label style="margin-right:auto; font-size:11.5px; color:var(--muted);">Standing: ' +
            '<select data-apv-set-kind="' + esc(r.kind) + '" data-apv-set-req="' + esc(r.id) + '" style="width:auto; min-width:90px;">' +
            '<option value="" selected disabled>set…</option>' + opts + '</select></label>' : '') +
          '<button class="btn btn-danger btn-sm" data-apv-dec="' + esc(r.id) + '" data-apv-act="deny_once">Deny</button>' +
          '<button class="btn btn-primary btn-sm" data-apv-dec="' + esc(r.id) + '" data-apv-act="approve_once">Approve</button></div>';
      }).join('');
    } catch(e){ el.innerHTML = '<div class="panel-error">Failed to load pending approvals.</div>'; }
  }
  $('pendingApprovals').addEventListener('click', async function(e){
    var b = e.target.closest('[data-apv-dec]');
    if (!b) return;
    var id = b.getAttribute('data-apv-dec'), action = b.getAttribute('data-apv-act');
    b.disabled = true;
    try {
      var res = await (await fetch('/approvals/decide', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ req_id: id, action: action }) })).json();
      toast(res.result === 'unknown' ? 'Already resolved' : (action === 'approve_once' ? 'Approved' : 'Denied'), res.result === 'unknown' ? 'info' : 'ok');
    } catch(err){ toast('Decision failed: ' + err.message, 'err'); }
    loadPendingApprovals();
  });
  $('pendingApprovals').addEventListener('change', async function(e){
    var sel = e.target.closest('[data-apv-set-kind]');
    if (!sel) return;
    var val = sel.value;
    if (!val || val === 'ask') { loadPendingApprovals(); return; }   // no-op standing
    var reqId = sel.getAttribute('data-apv-set-req');
    var action = val === 'allow' ? 'always_allow' : 'always_deny';
    sel.disabled = true;
    try {
      var res = await (await fetch('/approvals/decide', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ req_id: reqId, action: action }) })).json();
      toast(res.result === 'unknown' ? 'Already resolved' : 'Standing policy updated', res.result === 'unknown' ? 'info' : 'ok');
    } catch(err){ toast('Update failed: ' + err.message, 'err'); }
    loadPendingApprovals();
  });

  async function loadDeps(){
    var el = $('pendingInstalls');
    try {
      var d = await (await fetch('/deps')).json();
      var pending = d.pending || [];
      $('depsBadge').textContent = pending.length;
      $('depsBadge').className = 'tag ' + (pending.length ? 'tag-amber' : 'tag-muted');
      if (!pending.length){ el.innerHTML = '<div class="empty">Nothing awaiting approval.</div>'; return; }
      el.innerHTML = pending.map(function(r){
        return '<div class="warn-box">Created tool <strong>&nbsp;' + esc(r.tool_name||'?') + '&nbsp;</strong> requests the dependency <strong>&nbsp;' + esc(r.module) + '&nbsp;</strong>' +
          (r.last_error ? '<br><span style="color:var(--danger)">⚠ last attempt failed: ' + esc(r.last_error) + '</span>' : '') + '</div>' +
          '<div class="row-inline" style="justify-content:flex-end; margin-bottom:14px;">' +
          '<button class="btn btn-danger btn-sm" data-dep-deny="' + esc(r.id) + '">Deny</button>' +
          '<button class="btn btn-primary btn-sm" data-dep-approve="' + esc(r.id) + '">Approve &amp; install</button></div>';
      }).join('');
    } catch(e){ el.innerHTML = '<div class="panel-error">Failed to load pending installs.</div>'; }
  }
  $('pendingInstalls').addEventListener('click', async function(e){
    var ap = e.target.closest('[data-dep-approve]');
    var dn = e.target.closest('[data-dep-deny]');
    if (!ap && !dn) return;
    var btn = ap || dn;
    var id = btn.getAttribute(ap ? 'data-dep-approve' : 'data-dep-deny');
    var path = ap ? 'approve' : 'deny';
    btn.disabled = true; btn.textContent = ap ? 'installing…' : 'denying…';
    try {
      var res = await (await fetch('/deps/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) })).json();
      if (res.ok === false) toast('Failed: ' + (res.error || 'unknown').slice(0,200), 'err');
      else if (res.version) toast('Installed ' + res.module + ' v' + res.version, 'ok');
      else toast(ap ? 'Approved' : 'Denied', 'ok');
    } catch(e){ toast('Request failed: ' + e.message, 'err'); }
    loadDeps();
  });

  async function loadTrust(){
    var el = $('trustedRequests');
    try {
      var d = await (await fetch('/trust')).json();
      var pending = d.pending || [], trusted = d.trusted || [];
      $('trustBadge').textContent = pending.length;
      $('trustBadge').className = 'tag ' + (pending.length ? 'tag-danger' : 'tag-muted');
      var html = pending.map(function(r){
        return '<div class="danger-box">Tool <strong>&nbsp;' + esc(r.tool_name) + '&nbsp;</strong> wants <strong>unsandboxed</strong> execution — read the code below.</div>' +
          '<pre class="code-block" style="white-space:pre-wrap;">' + esc(r.code || '') + '</pre>' +
          '<label class="switch" style="margin:10px 0;"><input type="checkbox" class="trust-confirm" data-id="' + esc(r.id) + '"><span class="track"><span class="thumb"></span></span>I have reviewed this code</label>' +
          '<div class="row-inline" style="justify-content:flex-end; margin-bottom:14px;">' +
          '<button class="btn btn-danger btn-sm" data-trust-deny="' + esc(r.id) + '">Deny</button>' +
          '<button class="btn btn-primary btn-sm" data-trust-approve="' + esc(r.id) + '" disabled>Approve</button></div>';
      }).join('');
      if (trusted.length){
        html += '<div class="card-hint" style="margin:8px 0;">Currently trusted:</div>';
        html += trusted.map(function(t){
          return '<div class="list-item"><div class="list-main"><span class="list-title">' + esc(t.tool_name) + '</span>' +
            '<div class="list-sub">approved ' + esc((t.approved_at||'').slice(0,10)) + '</div></div>' +
            '<button class="btn btn-sm" data-trust-revoke="' + esc(t.tool_name) + '">Revoke</button></div>';
        }).join('');
      }
      el.innerHTML = html || '<div class="empty">Nothing to review.</div>';
      el.querySelectorAll('.trust-confirm').forEach(function(cb){
        cb.addEventListener('change', function(){
          var approveBtn = el.querySelector('[data-trust-approve="' + cb.dataset.id + '"]');
          if (approveBtn) approveBtn.disabled = !cb.checked;
        });
      });
    } catch(e){ el.innerHTML = '<div class="panel-error">Failed to load trust requests.</div>'; }
  }
  $('trustedRequests').addEventListener('click', async function(e){
    var ap = e.target.closest('[data-trust-approve]');
    var dn = e.target.closest('[data-trust-deny]');
    var rv = e.target.closest('[data-trust-revoke]');
    if (!ap && !dn && !rv) return;
    var btn = ap || dn || rv;
    btn.disabled = true;
    var path = ap ? 'approve' : dn ? 'deny' : 'revoke';
    var body = rv ? { tool_name: rv.getAttribute('data-trust-revoke') } : { id: (ap||dn).getAttribute(ap ? 'data-trust-approve' : 'data-trust-deny') };
    try {
      var res = await (await fetch('/trust/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
      if (res.ok === false) toast('Failed: ' + (res.error || 'unknown'), 'err');
      else toast('Done', 'ok');
    } catch(e){ toast('Request failed: ' + e.message, 'err'); }
    loadTrust();
  });
  /* ================= SETTINGS: model connections/roles, commands, telegram/notify, prompts, env ================= */
  var OPENROUTER_URL = "https://openrouter.ai/api/v1";
  var ACTIVE_CAPS = ["chat","utility","embedding"];
  var rolesCache = { connections: [], roles: {}, capabilities: [], active: {} };
  function connProvider(c){ return (c.provider && c.provider !== 'auto') ? c.provider : providerOf(c.base_url); }
  function hostOf(url){ try { return new URL(url).host; } catch(e){ return (url||'–').slice(0,60); } }

  async function loadRoles(){
    try { rolesCache = await (await fetch('/model/roles')).json(); } catch(e){ return; }
    renderCapsChecks(); renderRoles(); renderConnList();
  }
  // A capability→connection dropdown, shared by the active strip and the reserved grid.
  function roleSelect(cap){
    var roles = rolesCache.roles || {}, conns = rolesCache.connections || [];
    var sel = document.createElement('select');
    var none = document.createElement('option'); none.value = ''; none.textContent = '— unset —'; sel.appendChild(none);
    conns.forEach(function(c){ var o = document.createElement('option'); o.value = c.label; o.textContent = c.label + '  ·  ' + connProvider(c); sel.appendChild(o); });
    sel.value = roles[cap] || '';
    sel.addEventListener('change', function(){ setRole(cap, sel.value || null); });
    return sel;
  }
  function renderRoles(){
    var caps = rolesCache.capabilities || [];
    var active = caps.filter(function(c){ return ACTIVE_CAPS.indexOf(c) > -1; });
    var reserved = caps.filter(function(c){ return ACTIVE_CAPS.indexOf(c) === -1; });
    var strip = $('rolesActive');
    if (strip){
      strip.innerHTML = '';
      active.forEach(function(cap){
        var item = document.createElement('div'); item.className = 'role-item';
        var lab = document.createElement('span'); lab.className = 'role-name'; lab.textContent = cap;
        item.appendChild(lab); item.appendChild(roleSelect(cap));
        strip.appendChild(item);
      });
    }
    var grid = $('rolesReserved');
    if (grid){
      grid.innerHTML = '';
      reserved.forEach(function(cap){
        var lab = document.createElement('div'); lab.className = 'rlabel'; lab.textContent = cap;
        grid.appendChild(lab); grid.appendChild(roleSelect(cap));
      });
    }
    var count = $('rolesReservedCount'); if (count) count.textContent = '(' + reserved.length + ')';
    var toggle = $('rolesReservedToggle'); if (toggle) toggle.style.display = reserved.length ? '' : 'none';
  }
  function renderConnList(){
    var el = $('connList'); if (!el) return;
    var conns = rolesCache.connections || [], roles = rolesCache.roles || {};
    var usedBy = {};
    Object.keys(roles).forEach(function(cap){ var lab = roles[cap]; if (lab) (usedBy[lab] = usedBy[lab] || []).push(cap); });
    if (!conns.length){ el.innerHTML = '<div class="empty">No connections yet — add one to get started.</div>'; return; }
    el.innerHTML = '';
    conns.forEach(function(c){
      var card = document.createElement('div'); card.className = 'conn-card';
      // top row: identity (name + role pills) on the left, actions on the right
      var top = document.createElement('div'); top.className = 'cc-top';
      var idline = document.createElement('div'); idline.className = 'cc-idline';
      var nm = document.createElement('span'); nm.className = 'cc-name'; nm.textContent = c.label;
      idline.appendChild(nm);
      var pills = document.createElement('span'); pills.className = 'cc-pills';
      (usedBy[c.label] || []).forEach(function(r){ var p = document.createElement('span'); p.className = 'role-pill'; p.textContent = r; pills.appendChild(p); });
      idline.appendChild(pills);
      var result = document.createElement('span'); result.className = 'cc-test';
      var actions = document.createElement('div'); actions.className = 'cc-actions';
      var test = document.createElement('button'); test.className = 'btn btn-sm'; test.textContent = 'Test';
      test.addEventListener('click', function(){ testConn(c.label, test, result); });
      var edit = document.createElement('button'); edit.className = 'btn btn-sm'; edit.textContent = 'Edit';
      edit.addEventListener('click', function(){ openConnModal(c); });
      var del = document.createElement('button'); del.className = 'btn btn-sm btn-icon'; del.textContent = '✕'; del.title = 'Remove connection';
      del.addEventListener('click', function(){ removeConn(c.label); });
      actions.appendChild(test); actions.appendChild(edit); actions.appendChild(del);
      top.appendChild(idline); top.appendChild(actions);
      // bottom row: connection detail on the left, test result on the right
      var bottom = document.createElement('div'); bottom.className = 'cc-bottom';
      var meta = document.createElement('div'); meta.className = 'cc-meta';
      var hasKey = (c.api_key && c.api_key !== 'dummy');
      meta.textContent = c.model_name + ' · ' + connProvider(c) + ' · ' + (hasKey ? 'key ✓' : 'key —');
      bottom.appendChild(meta); bottom.appendChild(result);
      card.appendChild(top); card.appendChild(bottom);
      el.appendChild(card);
    });
  }
  function renderCapsChecks(){
    var el = $('cnCaps'); if (!el || el.dataset.built) return;
    (rolesCache.capabilities || ["chat","embedding","vision","tts","stt","image_gen","video_gen"]).forEach(function(cap){
      var l = document.createElement('label');
      var cb = document.createElement('input'); cb.type = 'checkbox'; cb.value = cap; cb.className = 'cn-cap';
      l.appendChild(cb); l.appendChild(document.createTextNode(cap));
      el.appendChild(l);
    });
    el.dataset.built = '1';
  }
  function updateProviderHint(){
    var hint = $('cnProviderHint'); if (!hint) return;
    var url = $('cnBase').value.trim(), prov = $('cnProvider').value;
    if (!url){ hint.textContent = 'blank = OpenRouter'; return; }
    hint.textContent = 'detected: ' + ((prov && prov !== 'auto') ? prov : providerOf(url));
  }
  function openConnModal(conn){
    renderCapsChecks();
    $('connModalTitle').textContent = conn ? ('Edit connection: ' + conn.label) : 'Add connection';
    $('cnLabel').value = conn ? (conn.label || '') : '';
    $('cnModel').value = conn ? (conn.model_name || '') : '';
    $('cnBase').value = conn ? (conn.base_url || '') : '';
    $('cnCtx').value = conn ? (conn.context_window || '') : '';
    $('cnProvider').value = conn ? (conn.provider || 'auto') : 'auto';
    $('cnKey').value = '';
    var hasKey = conn && conn.api_key && conn.api_key !== 'dummy';
    $('cnKey').placeholder = hasKey ? 'set — blank keeps the current key' : 'blank inherits provider key';
    var caps = {}; if (conn) (conn.capabilities || []).forEach(function(x){ caps[x] = true; });
    document.querySelectorAll('.cn-cap').forEach(function(cb){ cb.checked = !!caps[cb.value]; });
    $('connModalMsg').textContent = '';
    updateProviderHint();
    openModal('connModal');
    setTimeout(function(){ $('cnLabel').focus(); }, 40);
  }
  $('rolesReservedToggle').addEventListener('click', function(){
    var g = $('rolesReserved'); var willOpen = g.hidden;
    g.hidden = !willOpen;
    this.setAttribute('aria-expanded', String(willOpen));
    this.classList.toggle('open', willOpen);
  });
  $('connAddBtn').addEventListener('click', function(){ openConnModal(null); });
  $('cnBase').addEventListener('input', updateProviderHint);
  $('cnProvider').addEventListener('change', updateProviderHint);
  $('cnSaveBtn').addEventListener('click', async function(){
    var msg = $('connModalMsg'); msg.className = 'conn-modal-msg';
    var model = $('cnModel').value.trim();
    if (!model) { msg.textContent = 'Enter a model ID.'; msg.classList.add('err'); return; }
    var caps = Array.prototype.map.call(document.querySelectorAll('.cn-cap:checked'), function(cb){ return cb.value; });
    var body = { label: $('cnLabel').value.trim() || model, model_name: model,
      base_url: $('cnBase').value.trim() || OPENROUTER_URL, provider: $('cnProvider').value,
      context_window: $('cnCtx').value.trim() ? Math.trunc(Number($('cnCtx').value)) : null,
      capabilities: caps };
    var key = $('cnKey').value.trim();
    if (key && key.indexOf('/') > -1) { msg.textContent = "That looks like a model ID, not a key — leave it blank to inherit the provider's key."; msg.classList.add('err'); return; }
    if (key) body.api_key = key;
    var btn = this; btn.disabled = true;
    try {
      var r = await fetch('/model/presets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      closeAllModals(); await loadRoles(); toast('Saved ' + body.label, 'ok');
    } catch(e){ msg.textContent = 'Save failed: ' + e.message; msg.classList.add('err'); }
    finally { btn.disabled = false; }
  });
  async function testConn(label, btn, out){
    var prev = btn.textContent; btn.disabled = true; btn.textContent = '…';
    out.className = 'cc-test'; out.textContent = ''; out.title = '';
    try {
      var r = await fetch('/model/presets/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: label }) });
      var j = await r.json();
      if (j.ok){
        out.className = 'cc-test ok';
        out.textContent = '✓ ' + j.detail + (j.latency_ms != null ? ' · ' + j.latency_ms + 'ms' : '');
      } else {
        out.className = 'cc-test err';
        out.textContent = '✗ ' + (j.detail || 'failed') + (j.status ? ' (' + j.status + ')' : '');
        if (j.hint) out.title = j.hint;
      }
    } catch(e){ out.className = 'cc-test err'; out.textContent = '✗ request failed'; }
    finally { btn.disabled = false; btn.textContent = prev; }
  }
  async function removeConn(label){
    confirmDelete({
      title: 'Remove connection', message: 'Remove connection "' + label + '"?',
      onConfirm: async function(){
        try { await fetch('/model/presets/remove', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: label }) }); await loadRoles(); toast('Removed ' + label, 'info'); }
        catch(e){ toast('Remove failed', 'err'); }
      }
    });
  }
  async function setRole(cap, conn){
    try {
      var r = await fetch('/model/roles', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role: cap, connection: conn }) });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      await loadConfig(); await loadRoles();
      toast(cap + (conn ? ' → ' + conn : ' unset'), 'ok');
    } catch(e){ toast('Failed to set ' + cap, 'err'); loadRoles(); }
  }
  $('reembedBtn').addEventListener('click', async function(){
    if (!confirm('Re-embed all stored memory + knowledge vectors with the current embedding model?\n(Safe — it recomputes from stored text.)')) return;
    var btn = $('reembedBtn'), status = $('reembedStatus'), bar = $('reembedBar'), fill = $('reembedFill');
    var prev = btn.textContent;
    btn.disabled = true; btn.textContent = 're-embedding…';
    bar.style.display = ''; fill.style.width = '0%'; status.textContent = 'starting…';
    var final = null;
    try {
      var res = await fetch('/model/reembed', { method: 'POST' });
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = '';
      for (;;){
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0){
          var line = buf.slice(0, idx).trim(); buf = buf.slice(idx+2);
          if (line.slice(0,5) !== 'data:') continue;
          var ev; try { ev = JSON.parse(line.slice(5).trim()); } catch(e){ continue; }
          if (ev.type === 'progress' && ev.total > 0){
            fill.style.width = Math.round(100*ev.done/ev.total) + '%';
            status.textContent = ev.done + ' / ' + ev.total;
          } else if (ev.type === 'done' || ev.type === 'error') { final = ev; }
        }
      }
      if (final && final.ok){
        fill.style.width = '100%';
        status.textContent = '✓ ' + final.memory + ' facts + ' + final.knowledge + ' chunks';
        toast('Re-embedded ' + final.memory + ' facts + ' + final.knowledge + ' chunks', 'ok');
      } else {
        status.textContent = (final && final.error) || 'failed';
        toast((final && final.error) || 'Re-embed had failures', 'err');
      }
    } catch(e){ status.textContent = 'failed'; toast('Re-embed failed', 'err'); }
    finally {
      btn.disabled = false; btn.textContent = prev;
      setTimeout(function(){ bar.style.display = 'none'; status.textContent = ''; }, 4000);
    }
  });

  /* ---- custom commands ---- */
  async function loadCommands(){
    var el = $('cmdList'); if (!el) return;
    var items = {};
    try { items = await (await fetch('/commands')).json(); } catch(e){ return; }
    var names = Object.keys(items).sort();
    if (!names.length){ el.innerHTML = '<div class="empty">No custom commands yet — add one below.</div>'; return; }
    el.innerHTML = '';
    names.forEach(function(name){
      var row = document.createElement('div'); row.className = 'conn-row';
      var nm = document.createElement('span'); nm.className = 'cn-name'; nm.textContent = '/' + name;
      var meta = document.createElement('span'); meta.className = 'cn-meta'; meta.textContent = items[name];
      var sp = document.createElement('span'); sp.className = 'cn-sp';
      var edit = document.createElement('button'); edit.className = 'btn btn-sm'; edit.textContent = 'edit';
      edit.addEventListener('click', function(){ $('cmdName').value = name; $('cmdExp').value = items[name]; $('cmdName').focus(); });
      var del = document.createElement('button'); del.className = 'btn btn-sm'; del.textContent = '✕'; del.title = 'Remove command';
      del.addEventListener('click', function(){ removeCommand(name); });
      row.appendChild(nm); row.appendChild(meta); row.appendChild(sp); row.appendChild(edit); row.appendChild(del);
      el.appendChild(row);
    });
  }
  $('cmdSaveBtn').addEventListener('click', async function(){
    var name = $('cmdName').value.trim(), exp = $('cmdExp').value.trim();
    if (!name) { toast('Enter a command name', 'info'); return; }
    if (!exp) { toast('Enter what the command should do', 'info'); return; }
    try {
      var r = await fetch('/commands', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name, expansion: exp }) });
      if (!r.ok) { var e = await r.json().catch(function(){ return {}; }); throw new Error(e.detail || ('HTTP ' + r.status)); }
      var body = await r.json();
      $('cmdName').value = ''; $('cmdExp').value = '';
      await loadCommands(); toast('Saved /' + body.name, 'ok');
    } catch(e){ toast(e.message || 'Save failed', 'err'); }
  });
  async function removeCommand(name){
    confirmDelete({
      title: 'Remove command', message: 'Remove command "/' + name + '"?',
      onConfirm: async function(){
        try { await fetch('/commands/remove', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) }); await loadCommands(); toast('Removed /' + name, 'info'); }
        catch(e){ toast('Remove failed', 'err'); }
      }
    });
  }

  /* ---- telegram + notifications ---- */
  $('tgTokenSaveBtn').addEventListener('click', async function(){
    var v = $('inTgToken').value.trim();
    if (!v) { toast('telegram_bot_token: nothing to apply (write-only)', 'info'); return; }
    try { await patchConfigKey('telegram_bot_token', v); await fetch('/config/save', { method: 'POST' }); $('inTgToken').value = ''; toast('Token saved to .env — restart to apply', 'ok'); }
    catch(e){ toast('Save failed: ' + e.message, 'err'); }
  });
  $('tgChatsSaveBtn').addEventListener('click', async function(){
    try { await patchConfigKey('allowed_chat_ids', $('inTgChats').value.trim()); await fetch('/config/save', { method: 'POST' }); toast('Saved to .env — restart to apply', 'ok'); }
    catch(e){ toast('Save failed: ' + e.message, 'err'); }
  });
  function wireWebToolSave(btnId, inputId, key){
    $(btnId).addEventListener('click', async function(){
      try { await patchConfigKey(key, $(inputId).value.trim()); await fetch('/config/save', { method: 'POST' }); toast('Saved to .env — restart to apply', 'ok'); }
      catch(e){ toast('Save failed: ' + e.message, 'err'); }
    });
  }
  wireWebToolSave('searxngSaveBtn', 'inSearxngUrl', 'searxng_base_url');
  wireWebToolSave('firecrawlSaveBtn', 'inFirecrawlUrl', 'firecrawl_base_url');
  async function loadNotify(){
    try {
      var s = await (await fetch('/notify')).json();
      var a = s.available || {};
      $('notifyStatusTag').textContent = (a.email ? 'email✓ ' : '') + (a.ntfy ? 'ntfy✓ ' : '') + (a.telegram ? 'telegram✓' : '') || 'none configured';
      if (s.ntfy_topic && !$('inNtfyTopic').value) $('inNtfyTopic').value = s.ntfy_topic;
      if (s.email_to && !$('inNotifyEmail').value) $('inNotifyEmail').value = s.email_to;
    } catch(e){ $('notifyStatusTag').textContent = 'status unavailable'; }
    // SMTP host/port/user/from + ntfy server come from /config (not /notify)
    try {
      var c = await (await fetch('/config')).json();
      if (c.notify_email_to && !$('inNotifyEmail').value) $('inNotifyEmail').value = c.notify_email_to;
      if (c.notify_email_from) $('inSmtpFrom').value = c.notify_email_from;
      if (c.smtp_host) $('inSmtpHost').value = c.smtp_host;
      if (c.smtp_port) $('inSmtpPort').value = c.smtp_port;
      if (c.smtp_user) $('inSmtpUser').value = c.smtp_user;
      if (c.ntfy_server) $('inNtfyServer').value = c.ntfy_server;
      if (c.ntfy_topic && !$('inNtfyTopic').value) $('inNtfyTopic').value = c.ntfy_topic;
      if (c.smtp_password === '***set***') $('inSmtpPassword').placeholder = '•••••• (set — blank keeps it)';
    } catch(e){}
  }
  $('notifyEmailSaveBtn').addEventListener('click', async function(){
    try {
      var patch = { notify_email_to: $('inNotifyEmail').value.trim(), notify_email_from: $('inSmtpFrom').value.trim(),
                    smtp_host: $('inSmtpHost').value.trim(), smtp_port: parseInt($('inSmtpPort').value, 10) || 587,
                    smtp_user: $('inSmtpUser').value.trim() };
      var pw = $('inSmtpPassword').value;
      if (pw) patch.smtp_password = pw;                 // only overwrite the password if a new one was typed
      await fetch('/config', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
      await fetch('/config/save', { method: 'POST' });
      $('inSmtpPassword').value = '';
      loadNotify();
      toast('Email settings saved', 'ok');
    } catch(e){ toast('Save failed: ' + e.message, 'err'); }
  });
  $('notifyNtfySaveBtn').addEventListener('click', async function(){
    try {
      await fetch('/config', { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                               body: JSON.stringify({ ntfy_topic: $('inNtfyTopic').value.trim(), ntfy_server: $('inNtfyServer').value.trim() }) });
      await fetch('/config/save', { method: 'POST' });
      toast('Push settings saved', 'ok');
    } catch(e){ toast('Save failed: ' + e.message, 'err'); }
  });
  async function testNotify(channel){
    var out = $('notifyTestOut');
    out.textContent = 'sending…';
    try {
      var r = await fetch('/notify/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ channel: channel }) });
      var j = await r.json();
      out.textContent = (j.ok ? '✓ ' : '✗ ') + (j.detail || '');
    } catch(e){ out.textContent = '✗ ' + e.message; }
  }
  $('testNtfyBtn').addEventListener('click', function(){ testNotify('ntfy'); });
  $('testEmailBtn').addEventListener('click', function(){ testNotify('email'); });

  /* ---- system prompt / soul ---- */
  async function loadSystemPrompt(){
    try {
      var j = await (await fetch('/system-prompt')).json();
      var ta = $('sysPromptText');
      if (document.activeElement !== ta) ta.value = j.prompt || '';
    } catch(e){ $('sysPromptText').placeholder = 'failed to load'; }
  }
  $('sysPromptSaveBtn').addEventListener('click', async function(){
    var btn = $('sysPromptSaveBtn'); btn.disabled = true;
    try {
      var res = await fetch('/system-prompt', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: $('sysPromptText').value }) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashOk('sysPromptOk');
    } catch(e){ toast('Save prompt failed: ' + e.message, 'err'); }
    finally { btn.disabled = false; }
  });
  async function loadSoul(){
    try {
      var j = await (await fetch('/soul')).json();
      var ta = $('soulText');
      if (document.activeElement !== ta) ta.value = j.soul || '';
    } catch(e){ $('soulText').placeholder = 'failed to load'; }
  }
  $('soulSaveBtn').addEventListener('click', async function(){
    var btn = $('soulSaveBtn'); btn.disabled = true;
    try {
      var res = await fetch('/soul', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ soul: $('soulText').value }) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashOk('soulOk');
    } catch(e){ toast('Save soul failed: ' + e.message, 'err'); }
    finally { btn.disabled = false; }
  });
  $('soulRevertBtn').addEventListener('click', async function(){
    if (!confirm('Restore the previous persona? (Reverting again swaps back.)')) return;
    var btn = $('soulRevertBtn'); btn.disabled = true;
    try {
      var res = await fetch('/soul/revert', { method: 'POST' });
      var j = await res.json();
      if (!j.ok) { toast('Revert failed: ' + (j.error || res.status), 'err'); return; }
      $('soulText').value = j.soul || '';
      flashOk('soulOk');
    } catch(e){ toast('Revert failed: ' + e.message, 'err'); }
    finally { btn.disabled = false; }
  });

  /* ---- raw .env ---- */
  async function loadEnv(){
    try {
      var j = await (await fetch('/config/env')).json();
      var ta = $('envText');
      if (document.activeElement !== ta) ta.value = j.text || '';
      $('envPath').textContent = j.path ? ('path: ' + j.path) : 'requires restart';
    } catch(e){ $('envText').placeholder = 'failed to load'; }
  }
  $('envSaveBtn').addEventListener('click', async function(){
    var btn = $('envSaveBtn'); btn.disabled = true;
    try {
      var res = await fetch('/config/env', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: $('envText').value }) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashOk('envOk'); toast('Saved .env — restart to apply', 'ok');
    } catch(e){ toast('Save .env failed: ' + e.message, 'err'); }
    finally { btn.disabled = false; }
  });
  $('envSaveLiveBtn').addEventListener('click', async function(){
    var btn = $('envSaveLiveBtn'); btn.disabled = true;
    try {
      var res = await fetch('/config/save', { method: 'POST' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashOk('envOk'); toast('Live config written to .env', 'ok');
      loadEnv();
    } catch(e){ toast('Save live config failed: ' + e.message, 'err'); }
    finally { btn.disabled = false; }
  });
  $('restartBtn').addEventListener('click', async function(){
    if (!confirm('Restart the Argus server?\n\nThe connection will briefly drop while it re-execs and reloads .env.')) return;
    try { var res = await fetch('/admin/restart', { method: 'POST' }); }
    catch(e){ /* the process may die before responding — expected */ }
    toast('Restarting…', 'info');
    var tries = 0;
    var tick = async function(){
      tries += 1;
      try {
        var r = await fetch('/status', { cache: 'no-store' });
        if (r.ok){ reopenEvents(); loadConfig(); loadSystemPrompt(); loadEnv(); pollStatus(); toast('Server back online', 'ok'); return; }
      } catch(e){ /* still down */ }
      if (tries < 20) setTimeout(tick, 800);
      else toast('Server did not come back — try reloading', 'err');
    };
    setTimeout(tick, 3500);
  });
  /* ================= LOGS (server log viewer) ================= */
  // Consumed with fetch()+getReader() rather than EventSource because this endpoint is
  // admin-gated: EventSource can't send the X-Admin-Token header the fetch shim injects above.
  var LOG_MAX_LINES = 2000;
  var logBody = $('logBody');
  var logConnDot = $('logConnDot');
  var logConnLabel = $('logConnLabel');
  var logsJumpBtn = $('logsJumpBtn');
  var logsPauseBtn = $('logsPauseBtn');
  var logsAbortCtrl = null;
  var logsPaused = false;
  var logsUserScrolledUp = false;

  function logsSetConn(state){
    logConnDot.classList.remove('led-ok', 'led-danger', 'led-amber', 'led-pulse');
    if (state === 'live'){ logConnDot.classList.add('led-ok', 'led-pulse'); logConnLabel.textContent = 'Live'; }
    else if (state === 'paused'){ logConnDot.classList.add('led-amber'); logConnLabel.textContent = 'Paused'; }
    else if (state === 'connecting'){ logConnLabel.textContent = 'Connecting…'; }
    else if (state === 'error'){ logConnDot.classList.add('led-danger'); logConnLabel.textContent = 'Disconnected'; }
  }

  function logLevel(line){
    if (/\bCRITICAL\b/.test(line) || /\bERROR\b/.test(line) || /Traceback/.test(line)) return 'error';
    if (/\bWARNING\b/.test(line) || /\bWARN\b/.test(line)) return 'warn';
    if (/\bDEBUG\b/.test(line)) return 'debug';
    return 'info';
  }

  function logsAtBottom(){
    return (logBody.scrollHeight - logBody.scrollTop - logBody.clientHeight) < 24;
  }

  function appendLogLines(lines){
    if (!lines || !lines.length) return;
    var empty = logBody.querySelector('.empty'); if (empty) empty.remove();
    var frag = document.createDocumentFragment();
    lines.forEach(function(line){
      var div = document.createElement('div');
      div.className = 'log-line lvl-' + logLevel(line);
      div.innerHTML = esc(line);
      frag.appendChild(div);
    });
    var wasAtBottom = !logsUserScrolledUp;
    logBody.appendChild(frag);
    while (logBody.children.length > LOG_MAX_LINES) logBody.removeChild(logBody.firstChild);
    if (wasAtBottom){ logBody.scrollTop = logBody.scrollHeight; logsJumpBtn.classList.remove('show'); }
    else logsJumpBtn.classList.add('show');
  }

  logBody.addEventListener('scroll', function(){
    logsUserScrolledUp = !logsAtBottom();
    if (!logsUserScrolledUp) logsJumpBtn.classList.remove('show');
  });
  logsJumpBtn.addEventListener('click', function(){
    logBody.scrollTop = logBody.scrollHeight;
    logsUserScrolledUp = false;
    logsJumpBtn.classList.remove('show');
  });
  $('logsClearBtn').addEventListener('click', function(){
    logBody.innerHTML = '<div class="empty">Waiting for log output…</div>';
    logsJumpBtn.classList.remove('show');
    logsUserScrolledUp = false;
  });
  logsPauseBtn.addEventListener('click', function(){
    logsPaused = !logsPaused;
    logsPauseBtn.textContent = logsPaused ? 'Resume' : 'Pause';
    if (logsAbortCtrl) logsSetConn(logsPaused ? 'paused' : 'live');
  });

  async function connectLogs(){
    disconnectLogs();
    logBody.innerHTML = '<div class="empty">Waiting for log output…</div>';
    logsJumpBtn.classList.remove('show');
    logsUserScrolledUp = false;
    logsAbortCtrl = new AbortController();
    var ctrl = logsAbortCtrl;
    logsSetConn(logsPaused ? 'paused' : 'connecting');
    try {
      var r = await fetch('/logs/stream?lines=300', { signal: ctrl.signal });
      if (ctrl.signal.aborted) return;
      if (r.status === 401){
        logsSetConn('error');
        logBody.innerHTML = '<div class="panel-error">Logs require the admin token.</div>';
        return;
      }
      if (!r.ok || !r.body){
        logsSetConn('error');
        logBody.innerHTML = '<div class="panel-error">Failed to connect to the log stream.</div>';
        return;
      }
      logsSetConn(logsPaused ? 'paused' : 'live');
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      while (true){
        var res = await reader.read();
        if (res.done) break;
        buf += decoder.decode(res.value, { stream: true });
        var frames = buf.split('\n\n');
        buf = frames.pop();
        for (var i = 0; i < frames.length; i++){
          var frame = frames[i].trim();
          if (!frame) continue;
          var dataLine = frame.split('\n').filter(function(l){ return l.indexOf('data:') === 0; }).join('');
          if (!dataLine) continue;
          try {
            var payload = JSON.parse(dataLine.slice(5).trim());
            if (!logsPaused && payload && payload.lines) appendLogLines(payload.lines);
          } catch(e){ /* ignore malformed frame */ }
        }
      }
      if (!ctrl.signal.aborted) logsSetConn('error');
    } catch(e){
      if (ctrl.signal.aborted) return; // expected — we navigated away
      logsSetConn('error');
      logBody.innerHTML = '<div class="panel-error">Log stream disconnected.</div>';
    }
  }
  function disconnectLogs(){
    if (logsAbortCtrl){ logsAbortCtrl.abort(); logsAbortCtrl = null; }
  }
  pageEnter.logs = connectLogs;
  pageLeave.logs = disconnectLogs;

  /* ================= RELIABILITY: tool/routine/loop health ================= */
  function sparkline(vals, w, h){
    if (!vals || !vals.length) return '';
    var max = 100, min = 0, n = vals.length;
    var pts = vals.map(function(v,i){
      var x = n<2 ? 0 : (i/(n-1))*(w-2)+1;
      var y = h - 1 - ((v-min)/(max-min))*(h-2);
      return x.toFixed(1)+','+y.toFixed(1);
    }).join(' ');
    return '<svg class="spark" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'">'
      + '<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="'+pts+'"/></svg>';
  }
  function scoreCard(label, big, sub){
    return '<div class="card rel-score"><div class="rel-big">'+esc(String(big))+'</div>'
      + '<div class="rel-label">'+esc(label)+'</div><div class="rel-sub">'+esc(sub)+'</div></div>';
  }
  function toolRow(t){
    var pct = t.success_pct==null?'—':t.success_pct+'%';
    var cls = t.success_pct==null?'':(t.success_pct>=95?'ok':(t.success_pct>=80?'warn':'bad'));
    return '<div class="rel-tool"><span class="rt-name">'+esc(t.entity)+'</span>'
      + '<span class="rt-spark '+cls+'">'+sparkline(t.spark,64,16)+'</span>'
      + '<span class="rt-pct '+cls+'">'+pct+'</span>'
      + '<span class="rt-meta">'+t.calls+' calls'+(t.mean_ms!=null?' · '+t.mean_ms+'ms':'')+'</span>'
      + '<span class="rt-err" title="'+esc(t.last_error||'')+'">'+esc(t.last_error||'')+'</span></div>';
  }
  async function loadReliability(){
    var days = $('relRange').value || '30';
    try {
      var s = await (await fetch('/reliability/summary?days='+days)).json();
      if (!s.enabled){ $('relDisabled').style.display=''; $('relScore').innerHTML=''; return; }
      $('relDisabled').style.display='none';
      $('relScore').innerHTML =
        scoreCard('Tool success', s.tool_success_pct==null?'—':s.tool_success_pct+'%', s.tool_calls+' calls')
        + scoreCard('Routine completion', s.routine_completion_pct==null?'—':s.routine_completion_pct+'%', s.routine_runs+' runs')
        + scoreCard('Loop friction', s.friction_events, 'reprompts + parse-fails');
      var tools = await (await fetch('/reliability/tools?days='+days)).json();
      $('relTools').innerHTML = tools.length ? tools.map(toolRow).join('')
        : '<div class="empty">No tool calls recorded yet.</div>';
      var routines = await (await fetch('/reliability/routines?days='+days)).json();
      $('relRoutines').innerHTML = routines.length ? routines.map(function(r){
        return '<div class="rel-line"><span>'+esc(r.entity)+'</span><span>'+r.runs+' runs · '
          + (r.completion_pct==null?'—':r.completion_pct+'%')+'</span></div>'; }).join('')
        : '<div class="empty">No routine runs yet.</div>';
      var loop = await (await fetch('/reliability/loop?days='+days)).json();
      $('relLoop').innerHTML = ['parse_fail','reprompt','validation_fail'].map(function(k){
        var d = loop[k]||{total:0,series:[]};
        return '<div class="rel-line"><span>'+k.replace('_',' ')+'</span><span>'+d.total
          + ' '+sparkline((d.series||[]).map(function(x){return Math.max(0,100-x.n*10);}),80,18)+'</span></div>';
      }).join('');
    } catch(e){ toast('Failed to load reliability data: ' + e.message, 'err'); }
  }
  $('relRefresh').addEventListener('click', loadReliability);
  $('relRange').addEventListener('change', loadReliability);

  /* ================= WIRE-UP: page-first-open loaders + initial calls ================= */
  pageLoaders.automation = function(){ loadRoutines(); loadScheduled(); loadWatches(); };
  pageLoaders.data = function(){ loadFiles(); loadKnowledge(); loadArtifacts(); loadTables(); };
  pageLoaders.memory = function(){ loadMemoryStats(); };
  pageLoaders.rules = function(){ loadRules(); };
  pageLoaders.developer = function(){ loadLibrary(); loadDeps(); loadTrust(); loadPendingApprovals(); };
  pageLoaders.reliability = function(){ loadReliability(); };
  pageLoaders.settings = function(){
    loadRoles(); loadCommands(); loadNotify();
    loadSystemPrompt(); loadSoul(); loadEnv();
    loadSandboxStatus();
  };

  var startPage = (function(){ try { return localStorage.getItem('argus_page') || 'console'; } catch(e){ return 'console'; } })();
  switchPage(startPage);

  reopenEvents();
  loadTranscript(SESSION);   // restore the persisted transcript for whichever session was active on last visit
  renderSessionList();
  loadConfig();
  loadSkills();
  loadUsage();
  pollStatus();
  setInterval(pollStatus, 5000);
  fetch('/version').then(function(r){ return r.json(); })
    .then(function(v){
      $('railFooter').textContent = 'Argus v' + (v.version || '?');
      return fetch('/updates').then(function(r){ return r.json(); });
    })
    .then(function(u){
      if (u && u.update_available && u.latest){
        var a = document.createElement('a');
        a.className = 'update-badge';
        a.href = u.url || 'https://github.com/apollo-orbit-dev/argus-agent/releases';
        a.target = '_blank'; a.rel = 'noopener';
        a.title = 'A newer release (' + u.latest + ') is available — click for the release notes';
        a.textContent = '↑ ' + u.latest;
        $('railFooter').appendChild(a);
      }
    })
    .catch(function(){ if (!$('railFooter').textContent) $('railFooter').textContent = 'Argus'; });
})();
