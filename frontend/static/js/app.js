/**
 * app.js — Document list, sidebar, navigation, and file upload orchestration.
 * Depends on auth.js and editor.js (loaded before this in app.html).
 */

const App = (() => {
  let _docs = { owned: [], shared: [] };
  let _currentDocId = null;
  let _searchQuery = '';

  // ── Boot ──────────────────────────────────────────────────────────────────
  async function init() {
    if (!Auth.isLoggedIn()) {
      window.location.href = '/';
      return;
    }

    _renderUserAvatar();
    await _loadDocuments();
    _wireEvents();

    // Auto-open first owned document if any
    const first = _docs.owned[0] || _docs.shared[0];
    if (first) openDocument(first.id);
  }

  function _renderUserAvatar() {
    const user = Auth.getUser();
    if (!user) return;
    const el = document.getElementById('user-avatar');
    if (el) {
      el.textContent = user.username[0].toUpperCase();
      el.title = _capitalize(user.username);
    }
    const nameEl = document.getElementById('user-name');
    if (nameEl) nameEl.textContent = _capitalize(user.username);
  }

  function _capitalize(s) {
    if (!s) return s;
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  // ── Load + render documents ───────────────────────────────────────────────
  async function _loadDocuments() {
    try {
      const res = await Auth.apiFetch('/api/documents');
      if (!res.ok) return;
      _docs = await res.json();
      _renderDocumentList();
    } catch (e) {
      console.error('Failed to load documents', e);
    }
  }

  function _renderDocumentList() {
    const query = _searchQuery.toLowerCase();

    const filterDocs = (list) =>
      query ? list.filter(d => d.title.toLowerCase().includes(query)) : list;

    _renderSection('owned-list',  filterDocs(_docs.owned),  false);
    _renderSection('shared-list', filterDocs(_docs.shared), true);
  }

  function _renderSection(listId, docs, isShared) {
    const ul = document.getElementById(listId);
    if (!ul) return;
    ul.innerHTML = '';

    if (docs.length === 0) {
      ul.innerHTML = `<li class="sidebar-empty-hint">${isShared ? 'No shared documents yet' : 'No documents yet'}</li>`;
      return;
    }

    docs.forEach(doc => {
      const li = document.createElement('li');
      li.className = 'doc-item' + (doc.id === _currentDocId ? ' active' : '');
      li.dataset.id = doc.id;

      const badge = isShared
        ? `<span class="badge-shared">${doc.permission}</span>`
        : `<span class="badge-owner">mine</span>`;

      li.innerHTML = `
        <span class="doc-item-icon">📄</span>
        <div class="doc-item-info">
          <div class="doc-item-title" title="${_esc(doc.title)}">${_esc(doc.title)}</div>
          <div class="doc-item-meta">${_relTime(doc.updated_at)}</div>
        </div>
        ${badge}
        <div class="doc-item-actions">
          ${!isShared ? `<button class="doc-action-btn danger" data-action="delete" data-id="${doc.id}" title="Delete">🗑</button>` : ''}
        </div>`;

      li.addEventListener('click', (e) => {
        if (e.target.closest('[data-action="delete"]')) return;
        openDocument(doc.id);
      });

      const delBtn = li.querySelector('[data-action="delete"]');
      if (delBtn) {
        delBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          _confirmDelete(doc.id, doc.title);
        });
      }

      ul.appendChild(li);
    });
  }

  // ── Open document ─────────────────────────────────────────────────────────
  async function openDocument(id) {
    _currentDocId = id;
    _renderDocumentList(); // update active state

    const editorArea = document.getElementById('editor-area');
    const emptyState = document.getElementById('editor-empty-state');
    const paperWrap  = document.getElementById('editor-paper-wrap');

    if (emptyState) emptyState.style.display = 'none';
    if (paperWrap)  paperWrap.style.display = 'flex';

    await Editor.loadDocument(id);
  }

  // ── Create document ───────────────────────────────────────────────────────
  async function createDocument() {
    try {
      const res = await Auth.apiFetch('/api/documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: 'Untitled Document' }),
      });
      if (!res.ok) throw new Error('Failed to create document');
      const doc = await res.json();
      _docs.owned.unshift(doc);
      _renderDocumentList();
      openDocument(doc.id);
      Toast.show('New document created', 'success');
    } catch (e) {
      Toast.show(e.message, 'error');
    }
  }

  // ── Delete document ───────────────────────────────────────────────────────
  function _confirmDelete(id, title) {
    if (!confirm(`Delete "${title}"? This cannot be undone.`)) return;
    _deleteDocument(id);
  }

  async function _deleteDocument(id) {
    try {
      const res = await Auth.apiFetch(`/api/documents/${id}`, { method: 'DELETE' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Delete failed');
      }
      _docs.owned = _docs.owned.filter(d => d.id !== id);
      _renderDocumentList();

      if (_currentDocId === id) {
        _currentDocId = null;
        Editor.clearEditor();
        const empty = document.getElementById('editor-empty-state');
        const paper = document.getElementById('editor-paper-wrap');
        if (empty) empty.style.display = 'flex';
        if (paper) paper.style.display = 'none';
      }
      Toast.show('Document deleted', 'success');
    } catch (e) {
      Toast.show(e.message, 'error');
    }
  }

  // ── File upload (create new doc from file) ────────────────────────────────
  async function uploadFile(file) {
    const allowed = ['.txt', '.md', '.docx'];
    const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
    if (!allowed.includes(ext)) {
      Toast.show(`Unsupported file type. Allowed: ${allowed.join(', ')}`, 'error');
      return;
    }

    Toast.show('Importing file…', 'info');
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await Auth.apiFetch('/api/documents/upload-new', { method: 'POST', body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Upload failed');
      }
      const doc = await res.json();
      _docs.owned.unshift(doc);
      _renderDocumentList();
      openDocument(doc.id);
      Toast.show(`"${doc.title}" imported`, 'success');
    } catch (e) {
      Toast.show(e.message, 'error');
    }
  }

  // ── Refresh list (called by editor after title change, etc.) ─────────────
  async function refreshList() {
    await _loadDocuments();
  }

  // ── Wire events ───────────────────────────────────────────────────────────
  function _wireEvents() {
    // New document
    document.getElementById('btn-new-doc')?.addEventListener('click', createDocument);

    // Search
    const searchInput = document.getElementById('search-input');
    searchInput?.addEventListener('input', (e) => {
      _searchQuery = e.target.value;
      _renderDocumentList();
    });

    // Sidebar toggle
    document.getElementById('sidebar-toggle')?.addEventListener('click', () => {
      document.getElementById('app-shell').classList.toggle('sidebar-collapsed');
    });

    // Comments panel toggle
    document.getElementById('btn-toggle-comments')?.addEventListener('click', () => {
      document.getElementById('app-shell').classList.toggle('comments-collapsed');
    });

    // Dark mode toggle
    document.getElementById('btn-dark-mode')?.addEventListener('click', () => {
      document.body.classList.toggle('editor-dark');
      const isDark = document.body.classList.contains('editor-dark');
      localStorage.setItem('ajaia_dark', isDark ? '1' : '0');
      const btn = document.getElementById('btn-dark-mode');
      if (btn) btn.title = isDark ? 'Light mode' : 'Dark mode';
      btn.textContent = isDark ? '☀️' : '🌙';
    });

    // Restore dark mode preference
    if (localStorage.getItem('ajaia_dark') === '1') {
      document.body.classList.add('editor-dark');
      const btn = document.getElementById('btn-dark-mode');
      if (btn) { btn.textContent = '☀️'; btn.title = 'Light mode'; }
    }

    // Upload zone
    const uploadZone = document.getElementById('upload-zone');
    const fileInput  = document.getElementById('file-input');

    uploadZone?.addEventListener('click', () => fileInput?.click());
    fileInput?.addEventListener('change', (e) => {
      if (e.target.files?.[0]) uploadFile(e.target.files[0]);
      fileInput.value = '';
    });

    uploadZone?.addEventListener('dragover', (e) => {
      e.preventDefault();
      uploadZone.classList.add('dragover');
    });
    uploadZone?.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
    uploadZone?.addEventListener('drop', (e) => {
      e.preventDefault();
      uploadZone.classList.remove('dragover');
      const file = e.dataTransfer.files?.[0];
      if (file) uploadFile(file);
    });

    // Logout
    document.getElementById('btn-logout')?.addEventListener('click', Auth.logout);

    // Refresh sidebar timestamps every 60s (fixes "just now" always showing)
    setInterval(() => _renderDocumentList(), 60_000);
  }

  // ── Utilities ─────────────────────────────────────────────────────────────
  function _esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _relTime(iso) {
    if (!iso) return '';
    const diff = Date.now() - new Date(iso).getTime();
    const s = Math.floor(diff / 1000);
    if (s < 60)   return 'just now';
    if (s < 3600) return `${Math.floor(s/60)}m ago`;
    if (s < 86400) return `${Math.floor(s/3600)}h ago`;
    return `${Math.floor(s/86400)}d ago`;
  }

  return { init, openDocument, createDocument, refreshList, uploadFile };
})();

// ── Toast notifications ────────────────────────────────────────────────────
const Toast = (() => {
  function show(msg, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const icons = { success: '✓', error: '✕', info: 'ℹ', warn: '⚠' };
    const div = document.createElement('div');
    div.className = `toast toast-${type}`;
    div.innerHTML = `<span>${icons[type] || 'ℹ'}</span><span>${msg}</span>`;
    container.appendChild(div);
    setTimeout(() => div.remove(), 3200);
  }
  return { show };
})();
