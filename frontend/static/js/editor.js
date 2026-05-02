/**
 * editor.js — Quill editor, autosave, word count, grammar, sharing,
 *              version history, comments, WebSocket, toolbar extras.
 */

const Editor = (() => {
  let quill = null;
  let _docId = null;
  let _doc   = null;
  let _saveTimer  = null;
  let _wordGoal   = null;
  let _ws         = null;
  let _grammarMarks = [];
  let _commentDraft = null;
  let _grammarTimer = null;
  let _wordWarnShown = false;
  let _savedSelection = null;   // last known non-null Quill selection

  const SAVE_DEBOUNCE_MS  = 1800;
  const GRAMMAR_DEBOUNCE_MS = 4000;

  // ── Init ──────────────────────────────────────────────────────────────────
  function init() {
    quill = new Quill('#quill-editor', {
      modules: {
        toolbar: '#quill-toolbar',
        history: { delay: 1000, maxStack: 200, userOnly: true },
      },
      theme: 'snow',
      placeholder: 'Start writing…',
    });

    // Override Quill's image handler to upload to server
    const toolbar = quill.getModule('toolbar');
    toolbar.addHandler('image', _handleImageInsert);

    // Title autosave
    document.getElementById('doc-title-input')?.addEventListener('input', _scheduleSave);

    quill.on('text-change', (delta, old, source) => {
      if (source !== 'user') return;
      _updateWordCount();
      _scheduleSave();
      _scheduleGrammarCheck();
      _broadcastContent();
    });

    quill.on('selection-change', (range) => {
      if (range) {
        _savedSelection = range;   // always keep the last real selection
        _broadcastCursor(range);
        _commentDraft = range.length > 0 ? range : null;
        _updateToolbarState();
      }
    });

    _wireToolbarTabs();
    _wireCustomToolbar();
    _wireModalEvents();
    _wireCommentEvents();
    _wireFindReplace();
    _wireWordLimitWarning();
  }

  // ── Load document ─────────────────────────────────────────────────────────
  async function loadDocument(id) {
    try {
      const res = await Auth.apiFetch(`/api/documents/${id}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        Toast.show(err.detail || 'Failed to load document', 'error');
        return;
      }
      _doc   = await res.json();
      _docId = id;
      _wordWarnShown = false;

      const titleInput = document.getElementById('doc-title-input');
      if (titleInput) titleInput.value = _doc.title;

      // Show owner in topbar
      const ownerEl = document.getElementById('topbar-owner');
      if (ownerEl) {
        ownerEl.textContent = _capitalize(_doc.owner_username);
        ownerEl.title = `Document owner: ${_doc.owner_username}`;
        ownerEl.style.display = '';
      }

      try {
        const delta = JSON.parse(_doc.content_delta);
        quill.setContents(delta, 'silent');
      } catch {
        quill.setText(_doc.content_delta, 'silent');
      }

      quill.history.clear();
      _updateWordCount();
      _setSaveStatus('All changes saved');
      _wordGoal = _doc.word_target || null;
      _renderWordGoalBar();

      const canEdit = _doc.permission === 'owner' || _doc.permission === 'editor';
      quill.enable(canEdit);
      if (titleInput) titleInput.disabled = !canEdit;

      // Show access-request button for viewers who don't own the doc
      _updateAccessRequestUI();

      await _loadComments();
      _connectWS(id);
      _updateShareBadge();

      // Load public link state
      if (_doc.public_token) {
        _showPublicLink(_doc.public_token, _doc.public_permission);
      }

    } catch (e) {
      Toast.show('Error loading document: ' + e.message, 'error');
    }
  }

  // ── Save ──────────────────────────────────────────────────────────────────
  function _scheduleSave() {
    _setSaveStatus('Unsaved changes…');
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(save, SAVE_DEBOUNCE_MS);
  }

  async function save() {
    if (!_docId) return;
    const title = document.getElementById('doc-title-input')?.value?.trim() || 'Untitled Document';
    const rawContents = quill.getContents();
    // Strip grammar mark backgrounds before saving
    const cleanDelta = _stripGrammarBackground(rawContents);
    const content_delta = JSON.stringify(cleanDelta);
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content_delta }),
      });
      if (!res.ok) throw new Error('Save failed');
      _doc = await res.json();
      _setSaveStatus('All changes saved');
      await App.refreshList();
    } catch (e) {
      _setSaveStatus('Save failed — retrying…');
      _saveTimer = setTimeout(save, 5000);
    }
  }

  function _stripGrammarBackground(delta) {
    const ops = (delta.ops || []).map(op => {
      if (!op.attributes) return op;
      const attrs = { ...op.attributes };
      // Remove grammar mark backgrounds
      if (attrs.background === '#fef9c3' || attrs.background === '#fee2e2' ||
          attrs.background === '#fff0f0' || attrs.background === '#bfdbfe') {
        delete attrs.background;
      }
      return Object.keys(attrs).length ? { ...op, attributes: attrs } : { insert: op.insert };
    });
    return { ops };
  }

  function _setSaveStatus(msg) {
    const el = document.getElementById('save-status');
    if (el) el.textContent = msg;
  }

  function clearEditor() {
    _docId = null;
    _doc   = null;
    if (quill) quill.setText('', 'silent');
    const titleInput = document.getElementById('doc-title-input');
    if (titleInput) { titleInput.value = ''; titleInput.disabled = false; }
    _setSaveStatus('');
    _updateWordCount();
    _disconnectWS();
    document.getElementById('comments-list').innerHTML = '';
    document.getElementById('word-limit-warning')?.classList.remove('open');
  }

  // ── Word count ────────────────────────────────────────────────────────────
  function _updateWordCount() {
    const text  = quill ? quill.getText().trim() : '';
    const words = text.length === 0 ? 0 : text.split(/\s+/).filter(Boolean).length;
    const chars = text.length;

    const wcEl = document.getElementById('word-count-display');
    const ccEl = document.getElementById('char-count-display');
    if (wcEl) wcEl.textContent = `${words} word${words !== 1 ? 's' : ''}`;
    if (ccEl) ccEl.textContent = `${chars} char${chars !== 1 ? 's' : ''}`;

    if (_wordGoal) {
      const pct = Math.min((words / _wordGoal) * 100, 100);
      const fill = document.getElementById('wg-fill');
      const goalText = document.getElementById('word-goal-text');
      if (fill) {
        fill.style.width = `${pct}%`;
        fill.className = 'wg-fill' + (pct >= 100 ? ' over' : pct >= 80 ? ' warn' : '');
      }
      if (goalText) goalText.textContent = `${words} / ${_wordGoal}`;

      // Show warning popup when reaching 90% of goal (once per session)
      if (pct >= 90 && pct < 100 && !_wordWarnShown) {
        _wordWarnShown = true;
        _showWordLimitWarning(words, _wordGoal);
      }
    }
  }

  function _renderWordGoalBar() {
    const wrap = document.getElementById('word-goal-bar-wrap');
    if (wrap) wrap.classList.toggle('visible', !!_wordGoal);
    _updateWordCount();
  }

  function _showWordLimitWarning(words, goal) {
    const el = document.getElementById('word-limit-warning');
    const msg = document.getElementById('wlw-message');
    if (!el || !msg) return;
    msg.textContent = `You're at ${words} / ${goal} words (${Math.round(words/goal*100)}% of your goal).`;
    el.classList.add('open');
  }

  function _wireWordLimitWarning() {
    document.getElementById('wlw-ignore')?.addEventListener('click', () => {
      document.getElementById('word-limit-warning')?.classList.remove('open');
    });
    document.getElementById('wlw-increase')?.addEventListener('click', () => {
      document.getElementById('word-limit-warning')?.classList.remove('open');
      openWordGoalModal();
    });
  }

  // ── Auto grammar check ────────────────────────────────────────────────────
  function _scheduleGrammarCheck() {
    clearTimeout(_grammarTimer);
    _grammarTimer = setTimeout(_runAutoGrammar, GRAMMAR_DEBOUNCE_MS);
  }

  async function _runAutoGrammar() {
    if (!_docId) return;
    const text = quill.getText().trim();
    if (!text || text.length < 10) return;
    try {
      const res = await Auth.apiFetch('/api/grammar-check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text.slice(0, 20000) }),
      });
      if (!res.ok) return;
      const data = await res.json();
      _applyInlineGrammarMarks(data.matches || []);
    } catch { /* silent */ }
  }

  function _applyInlineGrammarMarks(matches) {
    _clearGrammarMarks();
    matches.slice(0, 100).forEach(m => {
      const ruleType = m.rule?.issueType || 'grammar';
      const bg = ruleType === 'misspelling' ? '#fff0f0' : '#f0f6ff';
      try {
        quill.formatText(m.offset, m.length, 'background', bg, 'api');
        _grammarMarks.push({ offset: m.offset, length: m.length });
      } catch (_) {}
    });
  }

  // Grammar check modal (manual full check)
  async function checkGrammar() {
    if (!_docId) { Toast.show('Open a document first', 'warn'); return; }
    const text = quill.getText();
    if (!text.trim()) { Toast.show('Document is empty', 'warn'); return; }

    _clearGrammarMarks();
    _openModal('grammar-modal');
    const resultsEl = document.getElementById('grammar-results');
    resultsEl.innerHTML = '<div style="text-align:center;padding:24px;color:var(--text-400)"><div class="spinner spinner-dark" style="margin:0 auto 8px"></div>Checking grammar…</div>';

    try {
      const res = await Auth.apiFetch('/api/grammar-check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text.slice(0, 20000) }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Grammar check failed');
      const data = await res.json();
      _renderGrammarResults(data.matches || []);
    } catch (e) {
      resultsEl.innerHTML = `<div class="grammar-issue type-grammar"><div class="grammar-issue-msg">⚠ ${e.message}</div></div>`;
    }
  }

  function _renderGrammarResults(matches) {
    const resultsEl = document.getElementById('grammar-results');
    if (!resultsEl) return;
    if (matches.length === 0) {
      resultsEl.innerHTML = '<div style="text-align:center;padding:32px;color:var(--success)">✓ No issues found!</div>';
      return;
    }
    resultsEl.innerHTML = `<div style="font-size:12px;color:var(--text-400);margin-bottom:8px">${matches.length} issue${matches.length !== 1 ? 's' : ''} found</div>`;

    matches.slice(0, 50).forEach(m => {
      const ruleType = m.rule?.issueType || 'grammar';
      const typeClass = ruleType === 'misspelling' ? 'type-spell' : ruleType === 'style' ? 'type-style' : 'type-grammar';
      const suggestions = (m.replacements || []).slice(0, 5)
        .map(r => `<span class="suggestion-chip" data-offset="${m.offset}" data-len="${m.length}" data-value="${_esc(r.value)}">${_esc(r.value)}</span>`)
        .join('');

      const card = document.createElement('div');
      card.className = `grammar-issue ${typeClass}`;
      card.innerHTML = `
        <div class="grammar-issue-msg">${_esc(m.message)}</div>
        <div class="grammar-issue-context">"${_esc((m.context?.text || '').trim())}"</div>
        ${suggestions ? `<div class="grammar-suggestions">${suggestions}</div>` : ''}`;

      try {
        const bg = ruleType === 'misspelling' ? '#fff0f0' : '#f0f6ff';
        quill.formatText(m.offset, m.length, 'background', bg, 'api');
        _grammarMarks.push({ offset: m.offset, length: m.length });
      } catch (_) {}

      card.querySelectorAll('.suggestion-chip').forEach(chip => {
        chip.addEventListener('click', () => {
          quill.deleteText(parseInt(chip.dataset.offset), parseInt(chip.dataset.len), 'user');
          quill.insertText(parseInt(chip.dataset.offset), chip.dataset.value, 'user');
          chip.closest('.grammar-issue').remove();
          Toast.show(`Replaced with "${chip.dataset.value}"`, 'success');
        });
      });
      resultsEl.appendChild(card);
    });
  }

  function _clearGrammarMarks() {
    _grammarMarks.forEach(({ offset, length }) => {
      try { quill.formatText(offset, length, 'background', false, 'api'); } catch (_) {}
    });
    _grammarMarks = [];
  }

  // ── Two-tab toolbar ───────────────────────────────────────────────────────
  function _wireToolbarTabs() {
    document.querySelectorAll('.toolbar-tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.toolbar-tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.toolbar-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const pane = btn.dataset.tbTab;
        document.querySelector(`[data-tb-pane="${pane}"]`)?.classList.add('active');
      });
    });
  }

  // ── Custom toolbar buttons ────────────────────────────────────────────────
  function _wireCustomToolbar() {
    // Font family
    document.getElementById('tb-font-family')?.addEventListener('change', (e) => {
      quill.format('font', e.target.value || false);
    });

    // Font size
    document.getElementById('tb-font-size')?.addEventListener('change', (e) => {
      quill.format('size', e.target.value || false);
    });

    // Heading
    document.getElementById('tb-heading')?.addEventListener('change', (e) => {
      quill.format('header', e.target.value ? parseInt(e.target.value) : false);
    });

    // Simple format toggle buttons
    document.querySelectorAll('.tb-btn[data-fmt]').forEach(btn => {
      btn.addEventListener('click', () => {
        const fmt = btn.dataset.fmt;
        const val = btn.dataset.val;
        if (val !== undefined) {
          if (fmt === 'script') {
            const current = quill.getFormat().script;
            quill.format('script', current === val ? false : val);
          } else if (fmt === 'indent') {
            quill.format('indent', val);
          } else if (fmt === 'list') {
            const current = quill.getFormat().list;
            quill.format('list', current === val ? false : val);
          } else if (fmt === 'align') {
            quill.format('align', val || false);
          } else if (fmt === 'blockquote' || fmt === 'code-block') {
            const current = quill.getFormat()[fmt];
            quill.format(fmt, !current);
          } else {
            quill.format(fmt, val);
          }
        } else {
          const current = quill.getFormat()[fmt];
          quill.format(fmt, !current);
        }
        _updateToolbarState();
      });
    });

    // Text color — native color picker, applies directly via quill.format()
    const colorInput = document.getElementById('tb-color-input');
    const colorBar   = document.getElementById('tb-color-bar');
    document.getElementById('tb-color-proxy')?.addEventListener('click', (e) => {
      // Save selection before the label click moves focus
      _savedSelection = quill.getSelection() || _savedSelection;
      colorInput?.click();
    });
    colorInput?.addEventListener('input', (e) => {
      const color = e.target.value;
      if (colorBar) colorBar.style.background = color;
      const range = _savedSelection;
      if (range && range.length > 0) {
        quill.formatText(range.index, range.length, 'color', color, 'user');
      } else {
        quill.format('color', color, 'user');
      }
    });

    // Highlight color — native color picker
    const bgInput = document.getElementById('tb-bg-input');
    const bgBar   = document.getElementById('tb-bg-bar');
    document.getElementById('tb-bg-proxy')?.addEventListener('click', (e) => {
      _savedSelection = quill.getSelection() || _savedSelection;
      bgInput?.click();
    });
    bgInput?.addEventListener('input', (e) => {
      const color = e.target.value;
      if (bgBar) bgBar.style.background = color;
      const range = _savedSelection;
      if (range && range.length > 0) {
        quill.formatText(range.index, range.length, 'background', color, 'user');
      } else {
        quill.format('background', color, 'user');
      }
    });

    // Link
    document.getElementById('tb-link')?.addEventListener('click', () => {
      document.querySelector('#quill-toolbar .ql-link')?.click();
    });

    // Clean formatting
    document.getElementById('tb-clean')?.addEventListener('click', () => {
      const range = quill.getSelection();
      if (range) quill.removeFormat(range.index, range.length);
    });

    // Change case
    document.getElementById('tb-case')?.addEventListener('click', _changeCaseMenu);

    // Find/Replace
    document.getElementById('tb-find')?.addEventListener('click', () => {
      const bar = document.getElementById('find-replace-bar');
      if (bar) { bar.classList.toggle('open'); if (bar.classList.contains('open')) document.getElementById('fr-find-input')?.focus(); }
    });

    // Table picker
    _initTablePicker();

    // Image upload
    document.getElementById('tb-image')?.addEventListener('click', () => {
      document.getElementById('image-upload-input')?.click();
    });
    document.getElementById('image-upload-input')?.addEventListener('change', async (e) => {
      const file = e.target.files?.[0];
      if (file) await _uploadAndInsertImage(file);
      e.target.value = '';
    });

    // Page break
    document.getElementById('tb-page-break')?.addEventListener('click', () => {
      const range = quill.getSelection(true);
      quill.insertText(range.index, '\n', 'user');
      quill.insertEmbed(range.index + 1, 'hr', true, 'user');
      quill.setSelection(range.index + 2, 'user');
    });

    // Horizontal rule
    document.getElementById('tb-hr')?.addEventListener('click', () => {
      const range = quill.getSelection(true);
      quill.insertText(range.index, '\n', 'user');
      quill.insertEmbed(range.index + 1, 'hr', true, 'user');
      quill.setSelection(range.index + 2, 'user');
    });

    // Import into current doc
    const importInput = document.getElementById('import-into-doc-input');
    document.getElementById('btn-import-into-doc')?.addEventListener('click', () => importInput?.click());
    importInput?.addEventListener('change', (e) => {
      if (e.target.files?.[0]) uploadIntoDoc(e.target.files[0]);
      importInput.value = '';
    });
  }

  function _updateToolbarState() {
    if (!quill) return;
    const fmt = quill.getFormat();
    const map = {
      'tb-bold':      { key: 'bold' },
      'tb-italic':    { key: 'italic' },
      'tb-underline': { key: 'underline' },
      'tb-strike':    { key: 'strike' },
      'tb-sub':       { key: 'script', val: 'sub' },
      'tb-super':     { key: 'script', val: 'super' },
      'tb-ol':        { key: 'list', val: 'ordered' },
      'tb-ul':        { key: 'list', val: 'bullet' },
    };
    Object.entries(map).forEach(([id, { key, val }]) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.toggle('active', val !== undefined ? fmt[key] === val : !!fmt[key]);
    });
  }

  // ── Change case ───────────────────────────────────────────────────────────
  function _changeCaseMenu() {
    // Clicking the button loses focus, so fall back to the last saved selection
    const range = quill.getSelection() || _savedSelection;
    if (!range || range.length === 0) { Toast.show('Select text first to change case', 'warn'); return; }
    const text = quill.getText(range.index, range.length);
    const options = [
      { label: 'UPPERCASE', fn: s => s.toUpperCase() },
      { label: 'lowercase', fn: s => s.toLowerCase() },
      { label: 'Title Case', fn: s => s.replace(/\w+/g, w => w[0].toUpperCase() + w.slice(1).toLowerCase()) },
      { label: 'Sentence case', fn: s => s.replace(/(^\s*\w|[.!?]\s*\w)/g, c => c.toUpperCase()) },
    ];

    // Simple inline menu
    const existingMenu = document.getElementById('case-menu');
    if (existingMenu) existingMenu.remove();

    const btn = document.getElementById('tb-case');
    const rect = btn.getBoundingClientRect();
    const menu = document.createElement('div');
    menu.id = 'case-menu';
    menu.style.cssText = `position:fixed;top:${rect.bottom+4}px;left:${rect.left}px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);z-index:300;min-width:140px;padding:4px`;
    options.forEach(opt => {
      const item = document.createElement('div');
      item.textContent = opt.label;
      item.style.cssText = 'padding:7px 12px;cursor:pointer;font-size:12px;color:var(--text-700);border-radius:4px';
      item.addEventListener('mouseenter', () => item.style.background = 'var(--border-light)');
      item.addEventListener('mouseleave', () => item.style.background = '');
      item.addEventListener('mousedown', (e) => {
        // mousedown fires before blur; prevents Quill from losing the selection
        e.preventDefault();
        const replaced = opt.fn(text);
        quill.deleteText(range.index, range.length, 'user');
        quill.insertText(range.index, replaced, 'user');
        quill.setSelection(range.index, replaced.length, 'silent');
        menu.remove();
      });
      menu.appendChild(item);
    });
    document.body.appendChild(menu);
    document.addEventListener('click', () => menu.remove(), { once: true });
  }

  // ── Table picker ──────────────────────────────────────────────────────────
  function _initTablePicker() {
    const popup = document.getElementById('table-picker-popup');
    const grid  = document.getElementById('table-picker-grid');
    const label = document.getElementById('table-picker-label');
    const btn   = document.getElementById('tb-table');
    if (!popup || !grid || !btn) return;

    // Build 8x8 grid
    for (let r = 0; r < 8; r++) {
      for (let c = 0; c < 8; c++) {
        const cell = document.createElement('div');
        cell.className = 'table-picker-cell';
        cell.dataset.row = r + 1;
        cell.dataset.col = c + 1;
        cell.addEventListener('mouseover', () => {
          const rows = r + 1, cols = c + 1;
          grid.querySelectorAll('.table-picker-cell').forEach(ce => {
            ce.classList.toggle('highlighted', parseInt(ce.dataset.row) <= rows && parseInt(ce.dataset.col) <= cols);
          });
          label.textContent = `${rows} × ${cols}`;
        });
        cell.addEventListener('click', () => {
          _insertTable(r + 1, c + 1);
          popup.classList.remove('open');
        });
        grid.appendChild(cell);
      }
    }

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      popup.classList.toggle('open');
      // Position below button
      const rect = btn.getBoundingClientRect();
      popup.style.top  = `${rect.bottom + 4}px`;
      popup.style.left = `${rect.left}px`;
      popup.style.position = 'fixed';
    });

    document.addEventListener('click', (e) => {
      if (!popup.contains(e.target) && e.target !== btn) popup.classList.remove('open');
    });
  }

  function _insertTable(rows, cols) {
    const range = quill.getSelection(true);
    let html = '<table>';
    for (let r = 0; r < rows; r++) {
      html += '<tr>';
      for (let c = 0; c < cols; c++) {
        html += r === 0 ? '<th>&nbsp;</th>' : '<td>&nbsp;</td>';
      }
      html += '</tr>';
    }
    html += '</table>';
    quill.clipboard.dangerouslyPasteHTML(range.index, html, 'user');
    Toast.show(`${rows}×${cols} table inserted`, 'success');
  }

  // ── Image upload ──────────────────────────────────────────────────────────
  function _handleImageInsert() {
    document.getElementById('image-upload-input')?.click();
  }

  async function _uploadAndInsertImage(file) {
    if (!_docId) { Toast.show('Open a document first', 'warn'); return; }
    Toast.show('Uploading image…', 'info');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await Auth.apiFetch(`/api/documents/${_docId}/images`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Upload failed');
      const data = await res.json();
      const range = quill.getSelection(true);
      quill.insertEmbed(range.index, 'image', data.url, 'user');
      quill.setSelection(range.index + 1, 'user');
      Toast.show('Image inserted', 'success');
    } catch (e) {
      Toast.show(e.message, 'error');
    }
  }

  // ── Find & Replace ────────────────────────────────────────────────────────
  let _frMatches = [];
  let _frCurrent = -1;

  function _wireFindReplace() {
    const findInput    = document.getElementById('fr-find-input');
    const replInput    = document.getElementById('fr-replace-input');
    const countEl      = document.getElementById('fr-count');
    const prevBtn      = document.getElementById('fr-prev');
    const nextBtn      = document.getElementById('fr-next');
    const replOne      = document.getElementById('fr-replace-one');
    const replAll      = document.getElementById('fr-replace-all');
    const closeBtn     = document.getElementById('fr-close');

    if (!findInput) return;

    findInput.addEventListener('input', _updateFind);
    findInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.shiftKey ? _frGo(-1) : _frGo(1); }
      if (e.key === 'Escape') document.getElementById('find-replace-bar')?.classList.remove('open');
    });

    prevBtn?.addEventListener('click', () => _frGo(-1));
    nextBtn?.addEventListener('click', () => _frGo(1));
    closeBtn?.addEventListener('click', () => {
      document.getElementById('find-replace-bar')?.classList.remove('open');
      _clearFrHighlights();
    });

    replOne?.addEventListener('click', () => {
      if (_frCurrent < 0 || _frCurrent >= _frMatches.length) return;
      const m = _frMatches[_frCurrent];
      const val = replInput?.value || '';
      quill.deleteText(m.index, m.length, 'user');
      quill.insertText(m.index, val, 'user');
      _updateFind();
      setTimeout(() => findInput?.focus(), 0);
    });

    replAll?.addEventListener('click', () => {
      const query = findInput.value;
      const repl  = replInput?.value || '';
      if (!query) return;
      const text = quill.getText();
      let offset = 0;
      let idx = text.indexOf(query);
      let count = 0;
      while (idx !== -1) {
        quill.deleteText(idx + offset, query.length, 'user');
        quill.insertText(idx + offset, repl, 'user');
        offset += repl.length - query.length;
        idx = text.indexOf(query, idx + 1);
        count++;
      }
      Toast.show(`Replaced ${count} occurrence${count !== 1 ? 's' : ''}`, 'success');
      _updateFind();
    });

    // Ctrl+H to open
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'h') {
        e.preventDefault();
        const bar = document.getElementById('find-replace-bar');
        bar?.classList.toggle('open');
        if (bar?.classList.contains('open')) findInput.focus();
      }
    });
  }

  function _updateFind() {
    const findInput = document.getElementById('fr-find-input');
    const query     = findInput?.value || '';
    const countEl   = document.getElementById('fr-count');
    _clearFrHighlights();
    _frMatches = [];
    _frCurrent = -1;
    if (!query || !quill) { if (countEl) countEl.textContent = ''; return; }

    const text = quill.getText();
    let idx = text.indexOf(query);
    while (idx !== -1) {
      _frMatches.push({ index: idx, length: query.length });
      idx = text.indexOf(query, idx + 1);
    }
    // Apply highlights using 'api' source so text-change handler ignores them
    _frMatches.forEach(m => {
      try { quill.formatText(m.index, m.length, 'background', '#fde68a', 'api'); } catch (_) {}
    });
    if (countEl) countEl.textContent = _frMatches.length > 0 ? `${_frMatches.length}` : '0';
    if (_frMatches.length > 0) {
      _frCurrent = 0;
      const m = _frMatches[0];
      // Use scrollIntoView without stealing focus
      try {
        const bounds = quill.getBounds(m.index, m.length);
        const editorEl = document.getElementById('quill-editor');
        if (editorEl && bounds) editorEl.scrollTop = bounds.top - 100;
      } catch (_) {}
      if (countEl) countEl.textContent = `1/${_frMatches.length}`;
    }
    // Always return focus to the find input — Quill formatText can steal it
    setTimeout(() => findInput?.focus(), 0);
  }

  function _frGo(dir) {
    if (_frMatches.length === 0) return;
    _frCurrent = (_frCurrent + dir + _frMatches.length) % _frMatches.length;
    const m = _frMatches[_frCurrent];
    const countEl = document.getElementById('fr-count');
    if (countEl) countEl.textContent = `${_frCurrent + 1}/${_frMatches.length}`;
    // Scroll to match without calling quill.setSelection (which steals focus)
    try {
      const bounds = quill.getBounds(m.index, m.length);
      const editorEl = document.getElementById('quill-editor');
      if (editorEl && bounds) editorEl.scrollTop = bounds.top - 100;
    } catch (_) {}
    // Restore focus to find input after any Quill operation
    setTimeout(() => document.getElementById('fr-find-input')?.focus(), 0);
  }

  function _clearFrHighlights() {
    _frMatches.forEach(m => {
      try { quill.formatText(m.index, m.length, 'background', false, 'api'); } catch (_) {}
    });
  }

  // ── Share modal ───────────────────────────────────────────────────────────
  async function openShareModal() {
    if (!_docId) { Toast.show('Open a document first', 'warn'); return; }
    _openModal('share-modal');
    await _loadShareInfo();
    await _loadUsers();
    if (_doc.is_owner) await _loadAccessRequests();
    _refreshPublicLinkUI();
  }

  async function _loadShareInfo() {
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/shares`);
      const shares = res.ok ? await res.json() : [];
      const listEl = document.getElementById('current-shares-list');
      if (!listEl) return;

      const ownerRow = `<div class="share-row">
        <div class="share-name">👑 ${_esc(_doc.owner_username)}</div>
        <span class="share-perm perm-owner">owner</span>
      </div>`;

      const shareRows = shares.map(s => `
        <div class="share-row" data-share-id="${s.id}">
          <div class="share-name">👤 ${_esc(s.username)}</div>
          <span class="share-perm perm-${s.permission}">${s.permission}</span>
          ${_doc.is_owner ? `<button class="btn btn-ghost btn-sm" onclick="Editor._revokeShare(${s.id})" style="margin-left:auto">✕</button>` : ''}
        </div>`).join('');

      listEl.innerHTML = ownerRow + (shareRows || '<p style="font-size:12px;color:var(--text-400);padding:6px 0">Not shared with anyone yet.</p>');
    } catch { /* silent */ }
  }

  async function _loadUsers() {
    try {
      const res = await Auth.apiFetch('/api/users');
      const users = res.ok ? await res.json() : [];
      const sel = document.getElementById('share-user-select');
      if (sel) {
        sel.innerHTML = '<option value="">Select a user…</option>' +
          users.map(u => `<option value="${u.username}">${_capitalize(u.username)} (${u.email})</option>`).join('');
      }
      // Copy to section share modal
      const dst = document.getElementById('section-share-user-select');
      if (dst) dst.innerHTML = sel.innerHTML;
    } catch { /* silent */ }
  }

  async function _loadAccessRequests() {
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/access-requests`);
      if (!res.ok) return;
      const reqs = await res.json();
      const listEl = document.getElementById('access-requests-list');
      if (!listEl) return;
      if (reqs.length === 0) {
        listEl.innerHTML = '<p style="font-size:12px;color:var(--text-400);padding:8px">No pending access requests.</p>';
        return;
      }
      listEl.innerHTML = reqs.map(r => `
        <div class="access-req-item">
          <div class="access-req-info">
            <div class="access-req-name">👤 ${_esc(_capitalize(r.requester_username))} <span style="font-weight:400;color:var(--text-400);font-size:11px">${_esc(r.requester_email)}</span></div>
            <div class="access-req-msg">Requesting <strong>${r.requested_permission}</strong> access${r.message ? ` — "${_esc(r.message)}"` : ''}</div>
          </div>
          <div class="access-req-actions">
            <button class="btn btn-primary btn-sm" onclick="Editor._decideRequest(${r.id}, true)">Approve</button>
            <button class="btn btn-secondary btn-sm" onclick="Editor._decideRequest(${r.id}, false)">Deny</button>
          </div>
        </div>`).join('');
    } catch { /* silent */ }
  }

  async function _decideRequest(requestId, approved) {
    try {
      const res = await Auth.apiFetch(`/api/access-requests/${requestId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved }),
      });
      if (!res.ok) throw new Error();
      Toast.show(approved ? 'Access approved' : 'Request denied', 'success');
      await _loadAccessRequests();
      if (approved) await _loadShareInfo();
    } catch { Toast.show('Failed to update request', 'error'); }
  }

  function _refreshPublicLinkUI() {
    if (!_doc) return;
    const displayEl = document.getElementById('public-link-display');
    const enableBtn = document.getElementById('btn-enable-public-link');
    const disableBtn = document.getElementById('btn-disable-public-link');

    if (_doc.public_token) {
      _showPublicLink(_doc.public_token, _doc.public_permission);
      if (displayEl) displayEl.style.display = 'block';
      if (enableBtn) enableBtn.textContent = 'Regenerate Link';
      if (disableBtn) disableBtn.style.display = '';
    } else {
      if (displayEl) displayEl.style.display = 'none';
      if (enableBtn) enableBtn.textContent = 'Generate Link';
      if (disableBtn) disableBtn.style.display = 'none';
    }
  }

  function _showPublicLink(token, permission) {
    const urlEl  = document.getElementById('public-link-url');
    const dispEl = document.getElementById('public-link-display');
    if (urlEl) urlEl.textContent = `${location.origin}/p/${token}`;
    if (dispEl) dispEl.style.display = 'block';
    const disableBtn = document.getElementById('btn-disable-public-link');
    if (disableBtn) disableBtn.style.display = '';
  }

  async function _revokeShare(shareId) {
    if (!confirm('Revoke access?')) return;
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/shares/${shareId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail);
      Toast.show('Access revoked', 'success');
      await _loadShareInfo();
    } catch (e) { Toast.show(e.message, 'error'); }
  }

  async function _submitShare() {
    const activePane = document.querySelector('#share-modal [data-pane].active');
    const pane = activePane?.dataset.pane;

    if (pane === 'share-link') {
      // Handle public link
      const permission = document.getElementById('public-link-permission')?.value || 'viewer';
      try {
        const res = await Auth.apiFetch(`/api/documents/${_docId}/public-link`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ permission }),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail);
        const data = await res.json();
        if (_doc) { _doc.public_token = data.public_token; _doc.public_permission = data.public_permission; }
        _showPublicLink(data.public_token, data.public_permission);
        document.getElementById('btn-enable-public-link').textContent = 'Regenerate Link';
        document.getElementById('btn-disable-public-link').style.display = '';
        Toast.show('Public link generated', 'success');
      } catch (e) { Toast.show(e.message, 'error'); }
      return;
    }

    // Check if invite email or username share
    const email = document.getElementById('invite-email-input')?.value?.trim();
    if (email) {
      const permission = document.getElementById('invite-permission-select')?.value || 'viewer';
      try {
        const res = await Auth.apiFetch(`/api/documents/${_docId}/invite`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, permission }),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail);
        const data = await res.json();
        document.getElementById('invite-email-input').value = '';

        const resultEl = document.getElementById('invite-link-result');
        if (resultEl) {
          if (data.invite_link && !data.email_sent) {
            resultEl.style.display = 'block';
            resultEl.innerHTML = `<div class="public-link-box"><span class="public-link-url">${_esc(data.invite_link)}</span><button class="btn btn-secondary btn-sm" onclick="navigator.clipboard.writeText('${_esc(data.invite_link)}');Toast.show('Copied','success')">Copy</button></div><p style="font-size:11px;color:var(--text-400);margin-top:4px">SMTP not configured — share this link manually.</p>`;
          } else {
            resultEl.style.display = 'none';
          }
        }
        Toast.show(data.email_sent ? `Invite email sent to ${email}` : `Invite link created for ${email}`, 'success');
        await _loadShareInfo();
      } catch (e) { Toast.show(e.message, 'error'); }
      return;
    }

    // Username share
    const username   = document.getElementById('share-user-select')?.value;
    const permission = document.getElementById('share-permission-select')?.value || 'viewer';
    if (!username) { Toast.show('Select a user or enter an email', 'warn'); return; }
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, permission }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail);
      Toast.show(`Shared with ${username}`, 'success');
      await _loadShareInfo();
    } catch (e) { Toast.show(e.message, 'error'); }
  }

  function _updateShareBadge() {
    const btn = document.getElementById('btn-share');
    if (!btn || !_doc) return;
    btn.title = _doc.is_owner ? 'Share document' : `Shared as ${_doc.permission}`;
  }

  // ── Access request (viewer → request editor) ──────────────────────────────
  function _updateAccessRequestUI() {
    const existingBtn = document.getElementById('btn-request-access');
    if (existingBtn) existingBtn.remove();
    if (!_doc || _doc.is_owner || _doc.permission !== 'viewer') return;

    const btn = document.createElement('button');
    btn.id = 'btn-request-access';
    btn.className = 'btn btn-ghost btn-sm';
    btn.style.cssText = 'font-size:11px;';
    btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Request Edit';
    btn.addEventListener('click', _requestAccess);

    const topbarActions = document.querySelector('.topbar-actions');
    if (topbarActions) topbarActions.insertBefore(btn, topbarActions.firstChild);
  }

  async function _requestAccess() {
    const message = prompt('Optional message to the document owner (or press OK to skip):') || '';
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/access-requests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requested_permission: 'editor', message }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail);
      Toast.show('Access request sent to the document owner', 'success');
      document.getElementById('btn-request-access')?.remove();
    } catch (e) { Toast.show(e.message, 'error'); }
  }

  // ── Section share modal ───────────────────────────────────────────────────
  async function openSectionShareModal() {
    if (!_docId) { Toast.show('Open a document first', 'warn'); return; }
    if (!_doc?.is_owner) { Toast.show('Only the owner can share sections', 'warn'); return; }
    _openModal('section-share-modal');
    _renderSectionList();
    await _loadUsers();
  }

  function _extractSections() {
    const delta = quill.getContents();
    const sections = [];
    let buf = '';
    let opIdx = 0;
    for (const op of delta.ops) {
      if (typeof op.insert !== 'string') { opIdx++; continue; }
      if (op.insert === '\n' && op.attributes?.header) {
        sections.push({ heading: buf.trim(), level: op.attributes.header, opIndex: opIdx });
        buf = '';
      } else if (op.insert.endsWith('\n')) {
        buf = '';
      } else {
        buf += op.insert;
      }
      opIdx++;
    }
    return sections;
  }

  let _selectedSection = null;

  function _renderSectionList() {
    const listEl = document.getElementById('section-list');
    if (!listEl) return;
    const sections = _extractSections();
    if (sections.length === 0) {
      listEl.innerHTML = '<p style="font-size:12px;color:var(--text-400);padding:8px">No headings found. Add H1/H2/H3 headings to enable section sharing.</p>';
      return;
    }
    listEl.innerHTML = sections.map((s, i) => `
      <div class="section-item" data-i="${i}" data-heading="${_esc(s.heading)}" data-op-index="${s.opIndex}">
        <span class="section-level">H${s.level}</span>
        <span class="section-name">${_esc(s.heading)}</span>
      </div>`).join('');

    listEl.querySelectorAll('.section-item').forEach(el => {
      el.addEventListener('click', () => {
        listEl.querySelectorAll('.section-item').forEach(x => x.classList.remove('selected'));
        el.classList.add('selected');
        _selectedSection = { heading: el.dataset.heading, section_index: parseInt(el.dataset.opIndex) };
      });
    });
  }

  async function _submitSectionShare() {
    if (!_selectedSection) { Toast.show('Select a section first', 'warn'); return; }
    const username   = document.getElementById('section-share-user-select')?.value;
    const permission = document.getElementById('section-share-permission')?.value || 'viewer';
    if (!username) { Toast.show('Select a user', 'warn'); return; }
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/section-shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, section_heading: _selectedSection.heading, section_index: _selectedSection.section_index, permission }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail);
      Toast.show(`Section shared with ${username}`, 'success');
      _closeModal('section-share-modal');
    } catch (e) { Toast.show(e.message, 'error'); }
  }

  // ── Version history ───────────────────────────────────────────────────────
  async function openVersionHistory() {
    if (!_docId) { Toast.show('Open a document first', 'warn'); return; }
    _openModal('version-modal');
    const listEl = document.getElementById('version-list');
    listEl.innerHTML = '<div style="text-align:center;padding:24px;color:var(--text-400)"><div class="spinner spinner-dark" style="margin:0 auto 8px"></div>Loading history…</div>';

    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/versions`);
      const versions = res.ok ? await res.json() : [];
      if (versions.length === 0) {
        listEl.innerHTML = '<p style="font-size:12px;color:var(--text-400);padding:8px">No version history yet.</p>';
        return;
      }
      listEl.innerHTML = versions.map(v => `
        <div class="version-item">
          <div class="version-dot"></div>
          <div class="version-info">
            <div class="version-time">${_esc(v.title)} — ${_fmtTime(v.created_at)}</div>
            <div class="version-by">Saved by ${_esc(_capitalize(v.created_by_username))}</div>
          </div>
          <button class="btn btn-secondary btn-sm" onclick="Editor._restoreVersion(${v.id})">Restore</button>
        </div>`).join('');
    } catch (e) {
      listEl.innerHTML = `<p style="color:var(--danger);font-size:12px">${e.message}</p>`;
    }
  }

  async function _restoreVersion(versionId) {
    if (!confirm('Restore this version? Comments after this snapshot will be removed.')) return;
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/versions/${versionId}/restore`, { method: 'POST' });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Restore failed');
      const doc = await res.json();
      quill.setContents(JSON.parse(doc.content_delta), 'silent');
      const titleInput = document.getElementById('doc-title-input');
      if (titleInput) titleInput.value = doc.title;
      _setSaveStatus('All changes saved');
      await _loadComments();
      _closeModal('version-modal');
      Toast.show('Version restored', 'success');
    } catch (e) { Toast.show(e.message, 'error'); }
  }

  // ── Word goal modal ───────────────────────────────────────────────────────
  function openWordGoalModal() {
    _openModal('word-goal-modal');
    const input = document.getElementById('word-goal-input');
    if (input) input.value = _wordGoal || '';
    const wgCurrent = document.getElementById('wg-current-count');
    if (wgCurrent) {
      const words = quill ? quill.getText().trim().split(/\s+/).filter(Boolean).length : 0;
      wgCurrent.textContent = words;
    }
  }

  async function _saveWordGoal() {
    const val = parseInt(document.getElementById('word-goal-input')?.value);
    _wordGoal = isNaN(val) || val <= 0 ? null : val;
    _wordWarnShown = false;
    if (_docId) {
      await Auth.apiFetch(`/api/documents/${_docId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word_target: _wordGoal || 0 }),
      }).catch(() => {});
    }
    _renderWordGoalBar();
    _closeModal('word-goal-modal');
    Toast.show(_wordGoal ? `Word goal set to ${_wordGoal}` : 'Word goal cleared', 'success');
  }

  // ── Export ────────────────────────────────────────────────────────────────
  async function exportMarkdown() {
    if (!_docId) { Toast.show('Open a document first', 'warn'); return; }
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/export/markdown`);
      if (!res.ok) throw new Error('Export failed');
      const data = await res.json();
      const blob = new Blob([data.content], { type: 'text/markdown' });
      const url  = URL.createObjectURL(blob);
      const a    = Object.assign(document.createElement('a'), { href: url, download: data.filename });
      a.click();
      URL.revokeObjectURL(url);
      Toast.show('Exported as Markdown', 'success');
    } catch (e) { Toast.show(e.message, 'error'); }
  }

  // ── Upload into existing document ─────────────────────────────────────────
  async function uploadIntoDoc(file) {
    if (!_docId) { Toast.show('Open a document first', 'warn'); return; }
    Toast.show('Importing content…', 'info');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await Auth.apiFetch(`/api/documents/${_docId}/upload`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Upload failed');
      const data = await res.json();
      quill.setContents(JSON.parse(data.delta), 'user');
      Toast.show(`Content from "${data.filename}" imported`, 'success');
    } catch (e) { Toast.show(e.message, 'error'); }
  }

  // ── Comments ──────────────────────────────────────────────────────────────
  async function _loadComments() {
    if (!_docId) return;
    try {
      const res = await Auth.apiFetch(`/api/documents/${_docId}/comments`);
      const comments = res.ok ? await res.json() : [];
      _renderComments(comments);
    } catch { /* silent */ }
  }

  function _renderComments(comments) {
    const listEl = document.getElementById('comments-list');
    if (!listEl) return;
    if (comments.length === 0) {
      listEl.innerHTML = `<div class="comments-empty"><div>💬</div>No comments yet.<br><small>Select text and click "Add comment".</small></div>`;
      return;
    }
    listEl.innerHTML = comments.map(c => `
      <div class="comment-card ${c.resolved ? 'resolved' : ''}" data-id="${c.id}">
        <div class="comment-header">
          <div class="comment-avatar">${c.author_username[0].toUpperCase()}</div>
          <div class="comment-author">${_esc(_capitalize(c.author_username))}</div>
          <div class="comment-time">${_fmtTime(c.created_at)}</div>
        </div>
        <div class="comment-body">${_esc(c.content)}</div>
        <div class="comment-footer">
          <button class="comment-btn resolve" onclick="Editor._toggleResolve(${c.id}, this)">${c.resolved ? '↩ Unresolve' : '✓ Resolve'}</button>
          <button class="comment-btn delete" onclick="Editor._deleteComment(${c.id}, this)">Delete</button>
          ${c.selection_index != null ? `<button class="comment-btn" onclick="Editor._goToComment(${c.selection_index}, ${c.selection_length || 0})">Go to</button>` : ''}
        </div>
      </div>`).join('');
  }

  async function _submitComment() {
    const input = document.getElementById('comment-input');
    const content = input?.value?.trim();
    if (!content) { Toast.show('Write something first', 'warn'); return; }
    try {
      const body = { content };
      if (_commentDraft) {
        body.selection_index  = _commentDraft.index;
        body.selection_length = _commentDraft.length;
        quill.formatText(_commentDraft.index, _commentDraft.length, 'background', '#bfdbfe', 'api');
      }
      const res = await Auth.apiFetch(`/api/documents/${_docId}/comments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail);
      if (input) input.value = '';
      document.getElementById('comment-form')?.classList.remove('visible');
      await _loadComments();
      Toast.show('Comment added', 'success');
    } catch (e) { Toast.show(e.message, 'error'); }
  }

  async function _toggleResolve(id, btn) {
    try {
      const res = await Auth.apiFetch(`/api/comments/${id}/resolve`, { method: 'PUT' });
      if (!res.ok) throw new Error();
      const data = await res.json();
      const card = btn.closest('.comment-card');
      if (card) card.classList.toggle('resolved', data.resolved);
      btn.textContent = data.resolved ? '↩ Unresolve' : '✓ Resolve';
    } catch { Toast.show('Failed to update comment', 'error'); }
  }

  async function _deleteComment(id, btn) {
    if (!confirm('Delete this comment?')) return;
    try {
      const res = await Auth.apiFetch(`/api/comments/${id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error();
      btn.closest('.comment-card')?.remove();
      Toast.show('Comment deleted', 'success');
    } catch { Toast.show('Could not delete comment', 'error'); }
  }

  function _goToComment(index, length) {
    if (!quill) return;
    quill.setSelection(index, length);
  }

  // ── WebSocket presence ────────────────────────────────────────────────────
  const CURSOR_COLORS = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899'];

  function _connectWS(docId) {
    _disconnectWS();
    const token = Auth.getToken();
    if (!token) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    try {
      _ws = new WebSocket(`${proto}//${location.host}/ws/documents/${docId}?token=${encodeURIComponent(token)}`);
      _ws.onmessage = (e) => _handleWSMessage(JSON.parse(e.data));
      _ws.onerror   = () => { _ws = null; };
      _ws.onclose   = () => { _ws = null; _renderPresence([]); };
    } catch { _ws = null; }
  }

  function _disconnectWS() {
    if (_ws) { _ws.close(); _ws = null; }
    _renderPresence([]);
  }

  function _handleWSMessage(msg) {
    if (msg.type === 'presence') {
      _renderPresence(msg.users);
    } else if (msg.type === 'content_update') {
      if (!_saveTimer && msg.full_content) {
        try { quill.setContents(JSON.parse(msg.full_content), 'silent'); } catch { /* ignore */ }
      }
    }
  }

  function _broadcastContent() {
    if (_ws?.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'content_update', full_content: JSON.stringify(quill.getContents()) }));
    }
  }

  function _broadcastCursor(range) {
    if (_ws?.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'cursor', index: range.index, length: range.length }));
    }
  }

  function _renderPresence(users) {
    const el = document.getElementById('presence-indicator');
    if (!el) return;
    const me = Auth.getUser();
    const others = users.filter(u => u.user_id !== me?.id);
    if (others.length === 0) { el.innerHTML = ''; return; }
    el.innerHTML = others.slice(0, 5).map((u, i) =>
      `<div class="presence-avatar" style="background:${CURSOR_COLORS[i % CURSOR_COLORS.length]}" title="${_esc(_capitalize(u.username))}">${u.username[0].toUpperCase()}</div>`
    ).join('') + (others.length > 5 ? `<span style="font-size:11px;color:var(--text-400)">+${others.length - 5}</span>` : '');
  }

  // ── Wire modal events ─────────────────────────────────────────────────────
  function _wireModalEvents() {
    document.getElementById('btn-share')?.addEventListener('click', openShareModal);
    document.getElementById('btn-submit-share')?.addEventListener('click', _submitShare);

    document.getElementById('btn-enable-public-link')?.addEventListener('click', async () => {
      const permission = document.getElementById('public-link-permission')?.value || 'viewer';
      try {
        const res = await Auth.apiFetch(`/api/documents/${_docId}/public-link`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ permission }),
        });
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (_doc) { _doc.public_token = data.public_token; _doc.public_permission = data.public_permission; }
        _showPublicLink(data.public_token, data.public_permission);
        document.getElementById('btn-enable-public-link').textContent = 'Regenerate Link';
        document.getElementById('btn-disable-public-link').style.display = '';
        Toast.show('Public link generated', 'success');
      } catch { Toast.show('Failed to generate link', 'error'); }
    });

    document.getElementById('btn-disable-public-link')?.addEventListener('click', async () => {
      try {
        await Auth.apiFetch(`/api/documents/${_docId}/public-link`, { method: 'DELETE' });
        if (_doc) { _doc.public_token = null; _doc.public_permission = null; }
        document.getElementById('public-link-display').style.display = 'none';
        document.getElementById('btn-enable-public-link').textContent = 'Generate Link';
        document.getElementById('btn-disable-public-link').style.display = 'none';
        Toast.show('Public link disabled', 'success');
      } catch { Toast.show('Failed to disable link', 'error'); }
    });

    document.getElementById('btn-copy-public-link')?.addEventListener('click', () => {
      const url = document.getElementById('public-link-url')?.textContent;
      if (url) navigator.clipboard.writeText(url).then(() => Toast.show('Link copied', 'success'));
    });

    document.getElementById('btn-section-share')?.addEventListener('click', openSectionShareModal);
    document.getElementById('btn-submit-section-share')?.addEventListener('click', _submitSectionShare);

    document.getElementById('btn-history')?.addEventListener('click', openVersionHistory);
    document.getElementById('btn-word-goal')?.addEventListener('click', openWordGoalModal);
    document.getElementById('btn-save-word-goal')?.addEventListener('click', _saveWordGoal);
    document.getElementById('btn-export-md')?.addEventListener('click', exportMarkdown);

    // Close modals
    document.querySelectorAll('[data-modal-close]').forEach(btn => {
      btn.addEventListener('click', () => _closeModal(btn.dataset.modalClose));
    });
    document.querySelectorAll('.modal-overlay').forEach(overlay => {
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) _closeModal(overlay.id);
      });
    });

    // Tab switching within modals
    document.querySelectorAll('.modal-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        const parent = tab.closest('.modal');
        parent.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
        parent.querySelectorAll('.modal-tab-pane').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        parent.querySelector(`[data-pane="${tab.dataset.tab}"]`)?.classList.add('active');
      });
    });
  }

  function _wireCommentEvents() {
    document.getElementById('btn-add-comment')?.addEventListener('click', () => {
      document.getElementById('comment-form')?.classList.toggle('visible');
    });
    document.getElementById('btn-submit-comment')?.addEventListener('click', _submitComment);
    document.getElementById('btn-cancel-comment')?.addEventListener('click', () => {
      document.getElementById('comment-form')?.classList.remove('visible');
    });
  }

  // ── Modal helpers ─────────────────────────────────────────────────────────
  function _openModal(id) { document.getElementById(id)?.classList.add('open'); }
  function _closeModal(id) { document.getElementById(id)?.classList.remove('open'); _clearGrammarMarks(); }

  // ── Utilities ─────────────────────────────────────────────────────────────
  function _esc(s) {
    return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _capitalize(s) {
    if (!s) return s;
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function _fmtTime(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleString(undefined, {
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  return {
    init, loadDocument, save, clearEditor,
    checkGrammar,
    openShareModal, openSectionShareModal, openVersionHistory, openWordGoalModal,
    exportMarkdown, uploadIntoDoc,
    _revokeShare, _restoreVersion, _toggleResolve, _deleteComment, _goToComment,
    _decideRequest,
  };
})();
