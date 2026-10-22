// Tab "Ma feuille de temps" — the tsc-timesheet review page (its review.html,
// ported). Loaded once with the shell; it wakes up whenever the tab's root
// (#feuille) is on the page, at first load or after an htmx tab switch, and
// its listeners only act on elements inside that root.
//
// The API (dev_feuille.py) wants the per-process token from data-token in an
// X-TS-Token header. Descriptions stay in English: they go to Odoo as typed.
(function () {
  var root = null;        // #feuille while the tab is shown
  var day = null;         // the day file, as /api/day returns it
  var saveTimer = null;
  var pending = new Map();  // line id -> pending edit
  var editing = new Set();  // lines whose pickers are open although they have a task
  var searchTimer = null;
  var PENCIL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/>' +
    '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';

  function q(sel) { return root.querySelector(sel); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(h) { return (Math.round(h * 100) / 100).toLocaleString('fr-BE'); }
  function plural(n, one, many) { return n + ' ' + (n > 1 ? many : one); }

  async function api(path, body) {
    var opts = { headers: { 'X-TS-Token': root.dataset.token } };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    var res = await fetch('/feuille/api' + path, opts);
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  function toast(msg) {
    var t = q('.fs-toast');
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.hidden = true; }, 3500);
  }

  function currentDate() { return q('.fs-date').value; }

  function setDate(d) {
    q('.fs-date').value = d;
    var url = new URL(location.href);
    url.searchParams.set('date', d);
    history.replaceState(history.state, '', url);
    load();
  }

  function shift(n) {
    var d = new Date(currentDate() + 'T12:00:00');
    d.setDate(d.getDate() + n);
    setDate(d.toLocaleDateString('sv-SE'));
  }

  function message(text) {
    q('.fs-tickets').innerHTML = '<tr><td colspan="6" class="muted">' + esc(text) + '</td></tr>';
  }

  async function load() {
    await flush();
    message(day ? 'Chargement…' : 'Lecture des traces et d\'Odoo…');
    try { day = await api('/day?date=' + currentDate()); render(); }
    catch (e) { day = null; message(e.message); }
  }

  async function rebuild(btn) {
    await flush();
    btn.disabled = true;
    try { day = await api('/build', { date: currentDate() }); render(); toast('Reconstruit depuis les traces'); }
    catch (e) { toast(e.message); }
    finally { btn.disabled = false; }
  }

  function queueEdit(id, field, value) {
    var line = day.lines.find(function (l) { return l.id === id; });
    line[field] = value;
    var e = pending.get(id) || { id: id };
    e[field] = value;
    pending.set(id, e);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, 600);
    totals();
  }

  async function flush() {
    clearTimeout(saveTimer);
    if (!pending.size || !day || !root) return;
    var edits = Array.from(pending.values());
    pending.clear();
    try { return await api('/save', { date: day.date, edits: edits }); }
    catch (e) { toast('Enregistrement impossible : ' + e.message); }
  }

  function pushable(l) {
    return l.selected && !l.pushed && l.key_type !== 'bucket' && l.task_id && l.project_id && l.hours > 0;
  }

  function sum(lines, f) { return lines.reduce(function (a, l) { return a + f(l); }, 0); }

  function totals() {
    var lines = day.lines.filter(function (l) { return l.key_type !== 'bucket'; });
    var sel = lines.filter(pushable);
    q('.fs-sel-hours').textContent = fmt(sum(sel, function (l) { return l.hours; }));
    q('.fs-sel-count').textContent = sel.length;
    q('.fs-all-hours').textContent = fmt(sum(lines, function (l) { return l.hours; }));
    // Tickets plus the book-it-yourself buckets: the whole day the sources saw.
    q('.fs-tracked-hours').textContent = fmt(sum(day.lines, function (l) {
      return l.tracked_hours != null ? l.tracked_hours : l.hours; }));
    q('.fs-logged-hours').textContent = fmt(sum(day.logged, function (l) { return l.hours; }));
    q('[data-fs="push"]').disabled = sel.length === 0;
  }

  function sourcesText(l) {
    return Object.entries(l.sources).map(function (kv) { return kv[0] + ' ' + kv[1]; }).join(' · ');
  }

  function evidenceHtml(l) {
    if (!l.evidence.length) return '<span class="muted">' + esc(sourcesText(l)) + '</span>';
    var items = l.evidence.map(function (e) {
      return '<li><span class="t">' + esc(e.time) + '</span> ' + esc(e.source) + ' · ' + esc(e.text) + '</li>';
    }).join('');
    return '<details><summary>' + plural(l.evidence.length, 'trace', 'traces') + ' · ' +
      esc(sourcesText(l)) + '</summary><ul class="fs-ev">' + items + '</ul></details>';
  }

  function taskLink(id) {
    return esc(root.dataset.odoo) + '/odoo/project.task/' + encodeURIComponent(id);
  }

  function taskCell(l) {
    if (l.task_id && !editing.has(l.id)) {
      var change = l.pushed ? '' : '<a href="#" class="fs-edit" data-change="' + esc(l.id) +
        '" title="Changer de projet ou de tâche" aria-label="Changer de projet ou de tâche">' + PENCIL + '</a>';
      return '<div class="fs-task"><a href="' + taskLink(l.task_id) + '" target="_blank" rel="noopener">#' +
        esc(l.task_id) + ' ' + esc(l.task_name || '') + '</a>' + change + '</div>' +
        '<div class="muted fs-small">' + esc(l.project_name || '') + '</div>';
    }
    // Project first, then a task in it; a task id finds the task in any project.
    var cancel = l.task_id ? ' <a href="#" class="fs-small" data-cancel="' + esc(l.id) + '">annuler</a>' : '';
    var where = l.project_id ? 'dans ' + (l.project_name || 'ce projet') : '';
    return '<div class="fs-picker"><input type="text" placeholder="Projet : ' + esc(l.project_name || 'choisir…') +
      '" data-pickproj="' + esc(l.id) + '"><div class="fs-results" hidden></div></div>' +
      '<div class="fs-picker"><input type="text" placeholder="Tâche ' + esc(where) + '… (ou son n°)" data-pick="' +
      esc(l.id) + '" data-project="' + esc(l.project_id || '') + '"><div class="fs-results" hidden></div></div>' +
      (l.task_id ? '<div class="muted fs-small">actuellement #' + esc(l.task_id) + ' ' + esc(l.task_name || '') +
        cancel + '</div>' : '');
  }

  function render() {
    q('.fs-date').value = day.date;
    q('.fs-built').textContent = 'construit à ' + day.built_at.slice(11, 16);
    q('.fs-sources').innerHTML = Object.entries(day.sources).map(function (kv) {
      return '<span class="fs-chip ' + (kv[1].ok ? 'ok' : 'bad') + '" title="' + esc(kv[1].ok ? '' : kv[1].error) +
        '">' + esc(kv[0]) + '</span>';
    }).join('');
    q('.fs-notices').innerHTML =
      Object.entries(day.sources).filter(function (kv) { return !kv[1].ok; }).map(function (kv) {
        return '<div class="fs-notice">' + esc(kv[0]) + ' indisponible — ' + esc(kv[1].error) + '</div>';
      }).join('') +
      day.lines.filter(function (l) { return l.push_error; }).map(function (l) {
        return '<div class="fs-notice err">#' + esc(l.task_id) + ' non poussé — ' + esc(l.push_error) + '</div>';
      }).join('');

    var tickets = day.lines.filter(function (l) { return l.key_type !== 'bucket'; });
    q('.fs-tickets').innerHTML = tickets.length ? tickets.map(function (l) {
      var dis = l.pushed ? 'disabled' : '';
      var badges = (l.pushed ? '<span class="fs-badge ok">poussé</span>' : '') +
        (l.logged_hours ? '<span class="fs-badge warn">' + fmt(l.logged_hours) + ' h déjà encodées' +
          (l.tracked_hours != null && !l.pushed ? ' sur ' + fmt(l.tracked_hours) + ' h tracées' : '') + '</span>' : '');
      return '<tr class="' + (l.pushed ? 'pushed' : '') + '">' +
        '<td><input type="checkbox" data-sel="' + esc(l.id) + '" ' + (l.selected ? 'checked' : '') + ' ' +
          (dis || (!l.task_id ? 'disabled' : '')) + ' aria-label="Sélectionner"></td>' +
        '<td class="fs-hours"><input type="number" min="0" step="0.25" value="' + esc(l.hours) + '" data-hours="' +
          esc(l.id) + '" ' + dis + '><div class="muted fs-small num">' + Math.round(l.minutes) + ' min réelles</div></td>' +
        '<td>' + taskCell(l) + badges + '</td>' +
        '<td class="fs-desc"><input type="text" value="' + esc(l.description) + '" data-desc="' + esc(l.id) + '" ' +
          dis + '></td>' +
        '<td class="num">' + esc(l.first) + '–' + esc(l.last) + '</td>' +
        '<td>' + evidenceHtml(l) + '</td></tr>';
    }).join('') : '<tr><td colspan="6" class="muted">Aucune activité sur un ticket ce jour-là.</td></tr>';

    var names = { meeting: 'Réunions', support: 'Support', internal: 'Interne', admin: 'Admin' };
    q('.fs-buckets').innerHTML = ['meeting', 'support', 'internal', 'admin'].map(function (b) {
      var l = day.lines.find(function (x) { return x.key_type === 'bucket' && x.bucket === b; });
      return '<div class="card fs-bucket"><div class="fs-bucket-h"><span>' + names[b] + '</span><span class="num">' +
        (l ? fmt(l.hours) + ' h' : '—') + '</span></div>' +
        (l ? '<div class="muted fs-small">' + esc(l.first) + '–' + esc(l.last) + ' · ' + Math.round(l.minutes) +
          ' min réelles</div>' + evidenceHtml(l) : '<div class="muted fs-small">rien vu</div>') + '</div>';
    }).join('');

    q('.fs-logged').innerHTML = day.logged.length ? day.logged.map(function (l) {
      return '<tr><td class="num">' + fmt(l.hours) + '</td><td>' +
        (l.task_id ? '<a href="' + taskLink(l.task_id) + '" target="_blank" rel="noopener">#' + esc(l.task_id) +
          '</a> ' + esc(l.task_name) : '—') + '</td><td>' + esc(l.project_name) + '</td><td>' +
        esc(l.description) + '</td></tr>';
    }).join('') : '<tr><td colspan="4" class="muted">Rien d\'encodé pour l\'instant.</td></tr>';
    totals();
  }

  function showResults(input, url, row, empty) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async function () {
      var box = input.nextElementSibling;
      try {
        var rows = await api(url);
        box.innerHTML = rows.map(row).join('') || '<div class="muted fs-small">' + empty + '</div>';
        var r = input.getBoundingClientRect();
        box.style.top = (r.bottom + 2) + 'px';
        box.style.left = r.left + 'px';
        box.style.width = Math.max(r.width, 280) + 'px';
        box.hidden = false;
      } catch (e) { toast(e.message); }
    }, 250);
  }
  function searchTasks(input) {
    var line = esc(input.dataset.pick);
    showResults(input, '/tasks?q=' + encodeURIComponent(input.value) + '&project_id=' +
      encodeURIComponent(input.dataset.project), function (r) {
        return '<div data-task="' + esc(r.id) + '" data-line="' + line + '">#' + esc(r.id) + ' ' + esc(r.name) +
          ' <span class="muted fs-small">' + esc(r.project_name) + ' · ' + esc(r.stage) + '</span></div>';
      }, 'aucune tâche trouvée');
  }
  function searchProjects(input) {
    var line = esc(input.dataset.pickproj);
    showResults(input, '/projects?q=' + encodeURIComponent(input.value), function (r) {
      return '<div data-proj="' + esc(r.id) + '" data-line="' + line + '">' + esc(r.name) +
        ' <span class="muted fs-small">' + esc(r.partner) + '</span></div>';
    }, 'aucun projet trouvé');
  }
  function hideResults() {
    if (root) root.querySelectorAll('.fs-results').forEach(function (r) { r.hidden = true; });
  }

  function inside(t) { return root && root.contains(t); }

  document.addEventListener('change', function (ev) {
    var t = ev.target;
    if (!inside(t)) return;
    if (t.classList.contains('fs-date')) return setDate(t.value);
    if (!day) return;
    if (t.dataset.sel) queueEdit(t.dataset.sel, 'selected', t.checked);
    if (t.dataset.hours) {
      var v = Math.max(0, Math.round(parseFloat(t.value || '0') * 4) / 4);
      t.value = v;
      queueEdit(t.dataset.hours, 'hours', v);
    }
  });

  document.addEventListener('input', function (ev) {
    var t = ev.target;
    if (!inside(t) || !day) return;
    if (t.dataset.desc) queueEdit(t.dataset.desc, 'description', t.value);
    if (t.dataset.pick !== undefined) searchTasks(t);
    if (t.dataset.pickproj !== undefined) searchProjects(t);
  });

  document.addEventListener('focusin', function (ev) {
    var t = ev.target;
    if (!inside(t)) return;
    if (t.dataset.pick !== undefined) searchTasks(t);
    if (t.dataset.pickproj !== undefined) searchProjects(t);
  });

  document.addEventListener('click', async function (ev) {
    if (!inside(ev.target)) return;
    // A click on the grey text inside a result row still picks that row.
    var t = ev.target.closest('[data-task],[data-proj],[data-change],[data-cancel],[data-fs]') || ev.target;
    var action = t.dataset.fs;
    if (action === 'prev') shift(-1);
    else if (action === 'next') shift(1);
    else if (action === 'today') setDate(new Date().toLocaleDateString('sv-SE'));
    else if (action === 'rebuild') rebuild(t);
    else if (action === 'push' && day) {
      q('.fs-confirm-count').textContent = day.lines.filter(pushable).length;
      q('.fs-confirm').hidden = false;
      t.hidden = true;
    } else if (action === 'confirm-no') {
      q('.fs-confirm').hidden = true;
      q('[data-fs="push"]').hidden = false;
    } else if (action === 'confirm-yes' && day) {
      t.disabled = true;
      try {
        clearTimeout(saveTimer);
        var edits = Array.from(pending.values());
        pending.clear();
        var res = await api('/push', { date: day.date, edits: edits });
        day = res.day;
        render();
        var ok = res.results.filter(function (r) { return r.odoo_id; }).length;
        var ko = res.results.length - ok;
        toast(plural(ok, 'ligne créée', 'lignes créées') + ' dans Odoo' + (ko ? ' · ' + ko + ' refusée(s)' : ''));
      } catch (e) { toast('Envoi impossible : ' + e.message); }
      finally {
        t.disabled = false;
        q('.fs-confirm').hidden = true;
        q('[data-fs="push"]').hidden = false;
      }
    } else if (t.dataset.change || t.dataset.cancel) {
      ev.preventDefault();
      if (t.dataset.change) editing.add(t.dataset.change); else editing.delete(t.dataset.cancel);
      render();
      if (t.dataset.change) {
        var input = root.querySelector('[data-pickproj="' + CSS.escape(t.dataset.change) + '"]');
        if (input) input.focus();
      }
    } else if (t.dataset.proj) {
      // A new project drops the task: it belongs to the old project.
      var id = t.dataset.line;
      editing.add(id);
      queueEdit(id, 'project_id', +t.dataset.proj);
      queueEdit(id, 'task_id', null);
      queueEdit(id, 'selected', false);
      var fresh = await flush();
      if (fresh) day = fresh;
      render();
      var pick = root.querySelector('[data-pick="' + CSS.escape(id) + '"]');
      if (pick) pick.focus();
    } else if (t.dataset.task) {
      var lid = t.dataset.line;
      editing.delete(lid);
      queueEdit(lid, 'task_id', +t.dataset.task);
      queueEdit(lid, 'selected', true);
      var fresh2 = await flush();
      if (fresh2) day = fresh2;
      render();
    }
    if (!t.closest('.fs-picker')) hideResults();
  });

  // The result lists are position: fixed (a scroll container would clip
  // them), so any scroll leaves them floating: close them.
  window.addEventListener('scroll', hideResults, true);

  // Leaving the tab (htmx swaps the pane) or the page: save what is pending.
  document.addEventListener('htmx:beforeSwap', function (e) {
    if (root && e.detail.target && e.detail.target.id === 'pane') flush();
  });
  window.addEventListener('beforeunload', function () { if (pending.size) flush(); });

  function init() {
    var el = document.getElementById('feuille');
    if (!el || el === root) return;
    root = el;
    day = null;
    pending.clear();
    editing.clear();
    var wanted = new URL(location.href).searchParams.get('date');
    q('.fs-date').value = /^\d{4}-\d{2}-\d{2}$/.test(wanted || '') ? wanted : root.dataset.today;
    load();
  }
  document.addEventListener('DOMContentLoaded', init);
  document.addEventListener('htmx:afterSettle', function () {
    if (root && !document.body.contains(root)) root = null;
    init();
  });
})();
