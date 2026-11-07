(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const els = Object.fromEntries([
    'landing', 'workspace', 'setup-warning', 'join-screen', 'join-room-link', 'join-form',
    'join-room-id', 'join-password', 'join-button', 'join-error',
    'create-form', 'create-button', 'room-title', 'room-password', 'room-id-display',
    'test-title', 'test-subtitle', 'connection', 'connection-text', 'presence', 'invite-button', 'copy-code-button',
    'upload-banner', 'pdf-input-banner', 'pdf-input-sidebar', 'upload-action-label',
    'question-count', 'score-total', 'score-possible', 'score-progress-fill', 'question-list',
    'add-question-button', 'empty-add-button', 'empty-question', 'question-detail',
    'active-number', 'active-heading', 'active-page', 'active-points', 'active-prompt',
    'active-cipher', 'cipher-workspace', 'cipher-empty', 'cell-status', 'answer-section',
    'answer-input', 'answer-status', 'answer-count',
    'claim-input', 'claim-points', 'previous-question', 'next-question', 'question-position',
    'edit-question-button', 'page-prev', 'page-next', 'page-indicator', 'zoom-out',
    'zoom-in', 'zoom-indicator', 'pdf-viewport', 'pdf-image', 'pdf-empty', 'toast', 'overlay',
    'overlay-title', 'overlay-subtitle', 'question-dialog', 'question-form', 'dialog-title',
    'dialog-close', 'dialog-cancel', 'delete-question', 'save-question', 'edit-prompt',
    'edit-cipher', 'edit-points', 'edit-page'
  ].map(id => [id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), $(id)]));

  const state = {
    roomId: null, token: null, room: null, activeId: null, page: 1, zoom: 100,
    socket: null, socketAuthenticated: false, reconnectTimer: null, reconnectDelay: 900, pending: [],
    pendingCells: new Map(), renderedCipherSignature: null, cellSyncError: false,
    actor: actorId(), counter: 0, editingId: null, loadingPdf: false,
    toastTimer: null, pdfVersion: Date.now(), isComposing: false, deferredOps: [],
    renderedAnswerQuestionId: null, renderedAnswerNodes: [], syncError: false,
    pdfObjectUrl: null, pdfPageKey: null, pdfAbort: null
  };

  const configuredBase = String(window.DECIPHER_API_BASE || '').trim();
  let backendOrigin = location.origin;
  let setupError = '';
  try {
    if (configuredBase) {
      const backend = new URL(configuredBase, location.href);
      if (!/^https?:$/.test(backend.protocol)) throw new Error('Invalid backend protocol.');
      backendOrigin = backend.origin;
    } else if (location.hostname.endsWith('.github.io')) {
      setupError = 'This GitHub Pages site needs its backend URL. Set DECIPHER_API_BASE in config.js to your HTTPS backend origin.';
    }
    if (location.protocol === 'https:' && backendOrigin.startsWith('http:')) {
      setupError = 'This site uses HTTPS. The backend in config.js must also have an HTTPS address for live collaboration to work.';
    }
  } catch (_) {
    setupError = 'The backend URL in config.js is invalid. Enter a complete HTTPS origin, such as https://example.com.';
  }
  if (setupError) {
    els.setupWarning.textContent = setupError;
    els.setupWarning.hidden = false;
    els.createButton.disabled = true;
    els.joinButton.disabled = true;
  }

  function actorId() {
    try {
      const bytes = crypto.getRandomValues(new Uint8Array(12));
      return Array.from(bytes, x => x.toString(16).padStart(2, '0')).join('');
    } catch (_) {
      return Math.floor(Math.random() * 2 ** 48).toString(16).padStart(12, '0') +
        Date.now().toString(16);
    }
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[char]);
  }

  function showToast(message, error = false) {
    clearTimeout(state.toastTimer);
    els.toast.textContent = message;
    els.toast.classList.toggle('error', error);
    els.toast.hidden = false;
    state.toastTimer = setTimeout(() => { els.toast.hidden = true; }, 4200);
  }

  function busy(on, title = 'Preparing your test…', subtitle = 'Finding your questions and ciphers') {
    els.overlay.hidden = !on;
    els.overlayTitle.textContent = title;
    els.overlaySubtitle.textContent = subtitle;
  }

  function tokenKey(id) { return `decipher-token:${id}`; }
  function storedToken(id) {
    try { return sessionStorage.getItem(tokenKey(id)); } catch (_) { return null; }
  }
  function setToken(id, token) {
    if (typeof token !== 'string' || !token) throw new Error('The server did not return room access. Please try again.');
    state.token = token;
    try { sessionStorage.setItem(tokenKey(id), token); } catch (_) { /* tab storage may be disabled */ }
  }
  function clearToken() {
    try { if (state.roomId) sessionStorage.removeItem(tokenKey(state.roomId)); } catch (_) { /* ignored */ }
    state.token = null;
  }
  function authorizedHeaders(headers = {}) {
    if (!state.token) throw new Error('Enter the room password to continue.');
    const merged = new Headers(headers);
    merged.set('Authorization', `Bearer ${state.token}`);
    return merged;
  }
  function unauthorized() {
    clearToken();
    showJoin(state.roomId, 'Your room access has expired. Enter the password again.');
  }
  async function request(path, options = {}, auth = true) {
    if (setupError) throw new Error(setupError);
    let response;
    const headers = auth ? authorizedHeaders(options.headers) : new Headers(options.headers || {});
    try { response = await fetch(`${backendOrigin}${path}`, { ...options, headers }); }
    catch (_) { throw new Error(backendOrigin !== location.origin
      ? 'Could not reach the backend. Check its HTTPS URL and allowed site origins (CORS).'
      : 'Could not reach the workspace. Check your connection and try again.'); }
    const type = response.headers.get('content-type') || '';
    const body = type.includes('json') ? await response.json() : await response.text();
    if (!response.ok) {
      const message = typeof body === 'object' ? body.error || body.detail || body.message : body;
      const error = new Error(typeof message === 'string' && message.length < 280 ? message : `Request failed (${response.status})`);
      error.status = response.status;
      if (auth && [401, 403].includes(response.status)) unauthorized();
      throw error;
    }
    return body;
  }

  function roomPath(suffix = '') { return `/api/rooms/${encodeURIComponent(state.roomId)}${suffix}`; }
  function jsonOptions(method, body) { return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }; }
  function roomFrom(response) { return response?.room || response; }
  function questions() { return (state.room?.questions || []).slice().sort((a, b) => Number(a.number ?? 0) - Number(b.number ?? 0)); }
  function question(id = state.activeId) { return state.room?.questions?.find(q => String(q.id) === String(id)); }
  function pageCount() { return Number(state.room?.page_count || 0); }
  function isUploaded() { return Boolean(state.room?.has_pdf || state.room?.pdf_name); }

  function normalizeQuestion(q, previous) {
    const nodes = Array.isArray(q.answer_nodes) ? q.answer_nodes.map(n => ({
      id: String(n.id), pos: Array.isArray(n.pos) ? n.pos.map(Number) : [],
      c: String(n.c ?? ''), deleted: Boolean(n.deleted)
    })) : previous?.answer_nodes ? previous.answer_nodes.map(n => ({ ...n, pos: [...n.pos] })) : [];
    if (!nodes.length && q.answer && !Array.isArray(q.answer_nodes)) {
      Array.from(String(q.answer)).forEach((c, i) => nodes.push({ id: `initial-${q.id}-${i}`, pos: [i + 1], c, deleted: false }));
    }
    const workCells = q.work_cells && typeof q.work_cells === 'object' && !Array.isArray(q.work_cells)
      ? q.work_cells : previous?.work_cells || {};
    return {
      ...previous, ...q, id: String(q.id), answer_nodes: nodes, checked: Boolean(q.checked),
      work_cells: { ...workCells }
    };
  }

  // Fractional positions are compared digit by digit. Each new position also gets
  // a private actor suffix and a monotonically increasing counter; concurrent
  // insertions into the same gap therefore have distinct, stable positions.
  const BASE = 65535;
  const actorDigits = Array.from({ length: Math.ceil(state.actor.length / 4) }, (_, i) =>
    (parseInt(state.actor.slice(i * 4, i * 4 + 4), 16) || 0) % 65534 + 1
  );
  function comparePositions(a, b) {
    const len = Math.min(a.length, b.length);
    for (let i = 0; i < len; i++) if (a[i] !== b[i]) return a[i] - b[i];
    return a.length - b.length;
  }
  function visibleNodes(q) {
    return (q.answer_nodes || []).filter(n => !n.deleted)
      .sort((a, b) => comparePositions(a.pos, b.pos) || a.id.localeCompare(b.id));
  }
  function answerText(q) { return visibleNodes(q).map(n => n.c).join(''); }
  function nextNode(left, right, char) {
    const pos = [];
    let leftExact = Boolean(left), rightExact = Boolean(right);
    for (let depth = 0; depth < 245; depth++) {
      const lo = leftExact ? left[depth] ?? 0 : 0;
      const hi = rightExact ? right[depth] ?? BASE : BASE;
      if (hi - lo > 1) {
        pos.push(!rightExact && leftExact ? lo + 1 : !leftExact && rightExact ? hi - 1 : Math.floor((lo + hi) / 2));
        break;
      }
      pos.push(lo);
      if (leftExact && depth >= left.length) leftExact = false;
      if (rightExact && (depth >= right.length || lo < right[depth])) rightExact = false;
    }
    const count = ++state.counter;
    pos.push(...actorDigits, Math.floor(count / 65534) % 65534 + 1, count % 65534 + 1);
    return { id: `${state.actor}-${count}`, pos, c: char, deleted: false };
  }
  function applyOps(q, op) {
    if (!q) return false;
    const byId = new Map(q.answer_nodes.map(n => [String(n.id), n]));
    let changed = false;
    const gone = new Set((op.deletes || []).map(String));
    for (const id of gone) {
      const existing = byId.get(id);
      if (existing && !existing.deleted) { existing.deleted = true; changed = true; }
      else if (!existing) {
        const placeholder = { id, pos: [], c: '', deleted: true };
        q.answer_nodes.push(placeholder);
        byId.set(id, placeholder);
      }
    }
    for (const incoming of op.inserts || []) {
      const id = String(incoming.id);
      const existing = byId.get(id);
      if (existing) {
        if (!existing.pos.length) {
          existing.pos = incoming.pos.map(Number);
          existing.c = String(incoming.c);
        }
        continue;
      }
      const node = { id, pos: incoming.pos.map(Number), c: String(incoming.c), deleted: gone.has(id) };
      q.answer_nodes.push(node);
      byId.set(id, node);
      changed = true;
    }
    if (changed) q.answer = answerText(q);
    return changed;
  }

  function pendingKey() { return `decipher-pending:${state.roomId}`; }
  function pendingCellsKey() { return `decipher-pending-cells:${state.roomId}`; }
  function cellId(questionId, key) { return JSON.stringify([String(questionId), String(key)]); }
  function savePendingCells() {
    try { localStorage.setItem(pendingCellsKey(), JSON.stringify(Array.from(state.pendingCells.values()))); }
    catch (_) { /* storage may be unavailable */ }
    setCellStatus();
  }
  function loadPendingCells() {
    try {
      const saved = JSON.parse(localStorage.getItem(pendingCellsKey()) || '[]');
      state.pendingCells = new Map(saved.filter(cell => cell && cell.question_id && cell.key &&
        typeof cell.value === 'string').map(cell => [cellId(cell.question_id, cell.key), cell]));
    } catch (_) { state.pendingCells = new Map(); }
    setCellStatus();
  }
  function replayPendingCells() {
    for (const cell of state.pendingCells.values()) {
      const q = question(cell.question_id);
      if (q) q.work_cells[cell.key] = cell.value;
    }
  }
  function sendPendingCells() {
    if (!state.socketAuthenticated || state.socket?.readyState !== WebSocket.OPEN) return;
    for (const cell of state.pendingCells.values()) state.socket.send(JSON.stringify(cell));
  }
  function clearPendingCells() { state.pendingCells.clear(); savePendingCells(); }
  function setCellStatus() {
    const waiting = state.pendingCells.size > 0;
    els.cellStatus.textContent = state.cellSyncError ? 'Sync issue' : waiting ?
      (state.socketAuthenticated ? 'Saving…' : 'Waiting to sync…') : 'Saved';
    els.cellStatus.classList.toggle('pending', waiting || state.cellSyncError);
  }
  function savePending() {
    try { localStorage.setItem(pendingKey(), JSON.stringify(state.pending)); } catch (_) { /* storage may be unavailable */ }
    setAnswerStatus();
  }
  function loadPending() {
    try { state.pending = JSON.parse(localStorage.getItem(pendingKey()) || '[]').filter(op => op && op.op_id); }
    catch (_) { state.pending = []; }
  }
  function replayPending() {
    for (const op of state.pending) applyOps(question(op.question_id), op);
  }
  function sendPending() {
    if (!state.socketAuthenticated || state.socket?.readyState !== WebSocket.OPEN) return;
    for (const op of state.pending) state.socket.send(JSON.stringify(op));
  }
  function acknowledge(opId) {
    if (!opId) return;
    const before = state.pending.length;
    state.pending = state.pending.filter(op => op.op_id !== opId);
    if (state.pending.length !== before) {
      state.syncError = false;
      savePending();
    }
  }
  function setAnswerStatus() {
    els.answerStatus.textContent = state.syncError ? 'Sync issue' : state.pending.length
      ? (state.socketAuthenticated ? 'Saving…' : 'Waiting to sync…') : 'Saved';
    els.answerStatus.classList.toggle('pending', state.pending.length > 0 || state.syncError);
  }

  function setConnection(connected) {
    els.connection.hidden = false;
    els.connection.classList.toggle('offline', !connected);
    els.connectionText.textContent = connected ? 'Live · in sync' : 'Reconnecting';
    setAnswerStatus();
    setCellStatus();
  }
  function connect() {
    if (!state.roomId || !state.token || setupError) return;
    clearTimeout(state.reconnectTimer);
    if (state.socket) { const old = state.socket; state.socket = null; old.close(); }
    const wsOrigin = backendOrigin.replace(/^http/, 'ws');
    const socket = new WebSocket(`${wsOrigin}/ws/${encodeURIComponent(state.roomId)}`);
    state.socket = socket;
    state.socketAuthenticated = false;
    setConnection(false);
    socket.addEventListener('open', () => {
      if (socket !== state.socket) return;
      state.reconnectDelay = 900;
      socket.send(JSON.stringify({ type: 'auth', token: state.token }));
    });
    socket.addEventListener('message', event => {
      if (socket !== state.socket) return;
      let message;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      onMessage(message);
    });
    socket.addEventListener('close', event => {
      if (socket !== state.socket) return;
      state.socketAuthenticated = false;
      if (event.code === 4401) {
        unauthorized();
        return;
      }
      if (event.code === 4403) {
        els.setupWarning.textContent = 'The backend has not allowed this website’s origin. Configure the backend’s allowed origins to include this site.';
        els.setupWarning.hidden = false;
        setConnection(false);
        els.connectionText.textContent = 'Live connection blocked';
        return;
      }
      setConnection(false);
      state.reconnectTimer = setTimeout(connect, state.reconnectDelay);
      state.reconnectDelay = Math.min(Math.round(state.reconnectDelay * 1.7), 12000);
    });
    socket.addEventListener('error', () => socket.close());
  }

  function onMessage(message) {
    if (message.type === 'snapshot' || message.type === 'room') {
      const incoming = roomFrom(message);
      if (incoming && Array.isArray(incoming.questions)) {
        if (message.type === 'room') {
          state.pending = [];
          state.syncError = false;
          state.cellSyncError = false;
          savePending();
          clearPendingCells();
          state.pdfVersion++;
          applyRoom(incoming, true);
          if (!state.loadingPdf) showToast('The PDF was updated. Review the new questions.');
        } else {
          const validIds = new Set(incoming.questions.map(q => String(q.id)));
          state.pending = state.pending.filter(op => validIds.has(String(op.question_id)));
          for (const [id, cell] of state.pendingCells) {
            if (!validIds.has(String(cell.question_id))) state.pendingCells.delete(id);
          }
          savePendingCells();
          savePending();
          state.pdfVersion++;
          applyRoom(incoming);
          if (!state.socketAuthenticated && state.socket?.readyState === WebSocket.OPEN) {
            state.socketAuthenticated = true;
            setConnection(true);
            state.socket.send(JSON.stringify({ type: 'presence', name: 'Codebreaker', client_id: state.actor }));
          }
        }
        sendPending();
        sendPendingCells();
      }
    } else if (message.type === 'presence') {
      renderPresence(message);
    } else if (message.type === 'question_created' || message.type === 'question_updated') {
      const incoming = message.question || message.data;
      if (incoming?.id) upsertQuestion(incoming);
    } else if (message.type === 'question_deleted') {
      const id = String(message.question_id ?? message.id ?? message.question?.id);
      if (state.room?.questions) {
        state.room.questions = state.room.questions.filter(q => q.id !== id);
        if (state.activeId === id) state.activeId = questions()[0]?.id || null;
        renderWorkspace();
      }
    } else if (message.type === 'answer_ops') {
      if (state.isComposing && String(message.question_id) === state.activeId) {
        state.deferredOps.push(message);
        return;
      }
      applyIncomingAnswer(message);
    } else if (message.type === 'cell_update') {
      applyIncomingCell(message);
    } else if (message.type === 'ack') {
      acknowledge(message.op_id);
    } else if (message.type === 'error') {
      // The failed operation is identified by the server. Other offline or
      // pending changes remain queued and are replayed against fresh state.
      if (message.key && message.question_id) {
        state.pendingCells.delete(cellId(message.question_id, message.key));
        state.cellSyncError = true;
        savePendingCells();
      } else if (message.op_id) state.pending = state.pending.filter(op => op.op_id !== message.op_id);
      else state.syncError = true;
      savePending();
      showToast(`${message.message || 'An answer update was rejected.'} Please try that edit again.`, true);
      request(roomPath()).then(response => applyRoom(roomFrom(response))).catch(() => {});
    }
  }

  function applyIncomingAnswer(message) {
    const op = message.op || message;
    const q = question(op.question_id || message.question_id);
    if (q) {
      const before = String(q.id) === state.activeId ? visibleNodes(q) : null;
      const changed = applyOps(q, op);
      if (changed && before) renderAnswer(before);
    }
    acknowledge(op.op_id || message.op_id);
  }

  function applyIncomingCell(message) {
    const q = question(message.question_id);
    if (!q || typeof message.key !== 'string' || typeof message.value !== 'string') return;
    const id = cellId(q.id, message.key);
    const pending = state.pendingCells.get(id);
    // An older broadcast cannot undo a locally queued edit while reconnecting.
    if (pending && pending.value !== message.value) return;
    if (pending) {
      state.pendingCells.delete(id);
      state.cellSyncError = false;
      savePendingCells();
    }
    q.work_cells[message.key] = message.value;
    if (q.id !== state.activeId) return;
    const field = Array.from(els.cipherWorkspace.querySelectorAll('[data-cell-key]'))
      .find(input => input.dataset.cellKey === message.key ||
        (input.dataset.fallbackKey === message.key && q.work_cells[input.dataset.cellKey] == null));
    // Wait until blur before applying a partner's edit to the field being typed in.
    if (field && field !== document.activeElement && field.value !== message.value) field.value = message.value;
  }

  function upsertQuestion(incoming) {
    if (!state.room) return;
    const old = question(incoming.id);
    const value = normalizeQuestion(incoming, old);
    const idx = state.room.questions.findIndex(q => q.id === value.id);
    if (idx < 0) state.room.questions.push(value);
    else state.room.questions[idx] = value;
    for (const op of state.pending.filter(op => String(op.question_id) === value.id)) applyOps(value, op);
    replayPendingCells();
    if (!state.activeId) state.activeId = value.id;
    renderWorkspace();
  }

  function applyRoom(incoming, resetSelection = false) {
    const previous = new Map((state.room?.questions || []).map(q => [q.id, q]));
    state.room = {
      ...incoming,
      questions: (incoming.questions || []).map(q => normalizeQuestion(q, previous.get(String(q.id))))
    };
    replayPending();
    replayPendingCells();
    if (resetSelection || !question()) state.activeId = questions()[0]?.id || null;
    if (resetSelection) state.page = Math.max(1, Number(question()?.page) || 1);
    renderWorkspace();
  }

  function renderPresence(data = {}) {
    const users = data.users || data.members || data.people || [];
    const count = Math.max(1, Number(data.count ?? users.length ?? 1));
    const shown = Math.min(count, 3);
    const avatars = Array.from({ length: shown }, (_, i) => {
      const entry = users[i];
      const name = typeof entry === 'string' ? entry : entry?.name || (i ? 'Partner' : 'You');
      return `<span class="avatar" title="${escapeHtml(name)}">${escapeHtml(name[0]?.toUpperCase() || '?')}</span>`;
    }).join('');
    els.presence.innerHTML = `<span class="presence-avatars" aria-hidden="true">${avatars}</span><span>${count > 1 ? `${count} here together` : 'Just you for now'}</span>`;
  }

  function renderWorkspace() {
    if (!state.room) return;
    els.landing.hidden = true;
    els.joinScreen.hidden = true;
    els.workspace.hidden = false;
    els.connection.hidden = false;
    const items = questions();
    els.roomIdDisplay.textContent = `ROOM ${String(state.room.id || state.roomId).slice(0, 8).toUpperCase()}`;
    els.testTitle.textContent = state.room.title || 'Untitled practice test';
    els.testSubtitle.textContent = isUploaded() ? `${state.room.pdf_name || 'Test PDF'} · ${pageCount()} ${pageCount() === 1 ? 'page' : 'pages'}` : 'Upload a PDF to get started.';
    els.uploadBanner.hidden = isUploaded();
    els.uploadActionLabel.textContent = isUploaded() ? 'Replace PDF' : 'Upload a PDF';
    els.questionCount.textContent = String(items.length);
    const earned = items.reduce((sum, q) => sum + (q.checked ? Math.max(0, Number(q.points) || 0) : 0), 0);
    const possible = items.reduce((sum, q) => sum + Math.max(0, Number(q.points) || 0), 0);
    els.scoreTotal.textContent = earned.toLocaleString();
    els.scorePossible.textContent = `/ ${possible.toLocaleString()} pts`;
    els.scoreProgressFill.style.width = `${possible ? Math.min(100, earned / possible * 100) : 0}%`;
    renderList(items);
    renderDetail(items);
    renderPdf();
    setAnswerStatus();
  }

  function renderList(items) {
    els.questionList.innerHTML = items.map((q, i) => {
      const synopsis = (q.prompt || q.cipher || `Page ${q.page || 1}`).trim().replace(/\s+/g, ' ').slice(0, 75);
      return `<button type="button" class="list-item${q.id === state.activeId ? ' selected' : ''}${q.checked ? ' complete' : ''}" data-question-id="${escapeHtml(q.id)}" role="listitem" aria-current="${q.id === state.activeId ? 'true' : 'false'}"><span class="list-number">${q.checked ? '✓' : String(q.number ?? i + 1).padStart(2, '0')}</span><span class="list-copy"><strong>Question ${escapeHtml(q.number ?? i + 1)}</strong><span>${escapeHtml(synopsis)}</span></span><span class="list-points">${Number(q.points || 0).toLocaleString()}p</span></button>`;
    }).join('');
  }

  function renderDetail(items) {
    const q = question();
    els.emptyQuestion.hidden = Boolean(q);
    els.questionDetail.hidden = !q;
    if (!q) return;
    const index = items.findIndex(item => item.id === q.id);
    const number = q.number ?? index + 1;
    els.activeNumber.textContent = String(number).padStart(2, '0');
    els.activeHeading.textContent = `Question ${number}`;
    els.activePage.textContent = `PAGE ${q.page || 1}`;
    els.activePoints.textContent = `${Number(q.points || 0).toLocaleString()} POINTS`;
    els.activePrompt.textContent = q.prompt || 'No instructions extracted. Edit this question or view the original page.';
    renderCipher(q);
    els.answerSection.hidden = false;
    els.claimInput.checked = Boolean(q.checked);
    els.claimPoints.textContent = `+${Number(q.points || 0).toLocaleString()} pts`;
    els.questionPosition.textContent = `${index + 1} of ${items.length}`;
    els.previousQuestion.disabled = index <= 0;
    els.nextQuestion.disabled = index < 0 || index >= items.length - 1;
    renderAnswer();
  }

  const ALPHABET = Array.from('ABCDEFGHIJKLMNOPQRSTUVWXYZ');
  const SPANISH_ALPHABET = Array.from('ABCDEFGHIJKLMNÑOPQRSTUVWXYZ');
  const MORSE_SYMBOLS = ['•', '−', '×'];
  const DEFAULT_MORSE = Array.from({ length: 26 }, (_, index) =>
    [Math.floor(index / 9), Math.floor(index / 3) % 3, index % 3]
      .map(position => MORSE_SYMBOLS[position]).join(''));

  function cellInput(q, key, label, options = {}) {
    const size = options.size || 'box';
    const maxLength = size === 'line' ? 1000 : 1;
    const value = String(q.work_cells?.[key] ?? q.work_cells?.[options.fallbackKey] ?? '');
    return `<input type="text" class="work-cell work-cell-${size}" data-cell-key="${escapeHtml(key)}" ` +
      (options.fallbackKey ? `data-fallback-key="${escapeHtml(options.fallbackKey)}" ` : '') +
      `value="${escapeHtml(value)}" maxlength="${maxLength}" spellcheck="false" autocomplete="off" ` +
      `aria-label="${escapeHtml(label)}"${options.placeholder != null ?
        ` placeholder="${escapeHtml(options.placeholder)}"` :
        size === 'line' ? ' placeholder="Write your solution beneath this line"' : ''}>`;
  }

  function renderCipherLines(q, lines, sourceIndexes = []) {
    if (!lines.length) return '';
    return `<div class="cipher-lines" aria-label="Ciphertext and shared working lines">${lines.map((line, index) => {
      const letters = Array.from(String(line));
      if (letters.length <= 180) {
        return `<div class="cipher-line-scroll"><div class="cipher-character-row" aria-label="Cipher line ${index + 1}: ${escapeHtml(line)}">` +
          letters.map((char, position) => /^\s$/.test(char)
            ? '<span class="cipher-char-space" aria-hidden="true"></span>'
            : `<span class="cipher-char-unit"><span class="cipher-char-symbol" aria-hidden="true">${escapeHtml(char)}</span>` +
              cellInput(q, `line:${index}:char:${position}`,
                `Question ${q.number || ''}, line ${index + 1}, solve character ${position + 1} (${char})`,
                { size: 'letter', fallbackKey: sourceIndexes[index] !== undefined && sourceIndexes[index] !== index ?
                  `line:${sourceIndexes[index]}:char:${position}` : undefined }) + '</span>').join('') + '</div></div>';
      }
      const len = Math.min(250, letters.length + 1);
      return `<div class="cipher-line-scroll"><div class="cipher-line-unit" style="--line-ch:${len}">` +
        `<div class="cipher-message">${escapeHtml(line)}</div>` +
        cellInput(q, `line:${index}`, `Question ${q.number || ''}, work below cipher line ${index + 1}`, {
          size: 'line', fallbackKey: sourceIndexes[index] !== undefined && sourceIndexes[index] !== index ?
            `line:${sourceIndexes[index]}` : undefined
        }) +
        `</div></div>`;
    }).join('')}</div>`;
  }

  function recognizedKind(q) {
    const declared = String(q.cipher_kind || '').toLowerCase().replace(/[-\s]+/g, '_');
    if (declared && declared !== 'generic') return declared;
    const prompt = String(q.prompt || '');
    if (/\bcryptarithm\b/i.test(prompt)) return 'cryptarithm';
    if (/\bfractionated\s+morse\b/i.test(prompt)) return 'fractionated_morse';
    if (/\bcheckerboard\b/i.test(prompt)) return 'checkerboard';
    if (/\bxenocrypt\b/i.test(prompt)) return 'xenocrypt';
    for (const kind of ['aristocrat', 'patristocrat', 'baconian', 'nihilist']) {
      if (new RegExp(`\\b${kind}\\b`, 'i').test(prompt)) return kind;
    }
    return 'generic';
  }

  function cipherLinesForFallback(q, layout) {
    const structured = Array.isArray(layout.lines) ? layout.lines.filter(line => String(line).trim()) : [];
    if (structured.length) return structured;
    return String(q.cipher || '').split(/\r?\n/).filter(line => {
      const text = line.trim();
      if (!text || /^(?:frequency|replacement|alphabet|K\s*[123]|morse\s+fraction|cryptarithm\s+formula|values\s+to\s+decode|team\s+(?:name|number)|page\s+\d+)\b/i.test(text)) return false;
      const letters = text.toUpperCase().replace(/[^A-ZÑ]/g, '');
      if (letters === ALPHABET.join('') || letters === SPANISH_ALPHABET.join('')) return false;
      if (/^\d{1,3}(?:\s+\d{1,3}){20,}$/.test(text)) return false;
      return true;
    });
  }

  function frequencyCounts(lines, headers) {
    const letters = lines.join('').toUpperCase().replace(/[^A-ZÑ]/g, '');
    return headers.map(header => String(Array.from(letters).filter(char => char === header).length));
  }

  function fallbackTable(q, kind, lines) {
    if (['aristocrat', 'patristocrat', 'xenocrypt', 'baconian'].includes(kind)) {
      const headers = kind === 'xenocrypt' ? SPANISH_ALPHABET : ALPHABET;
      const k2 = /\bK\s*2\b/i.test(String(q.prompt || ''));
      const label = k2 ? 'K2' : String(q.prompt || '').match(/\bK\s*[13]\b/i)?.[0].replace(/\s/g, '').toUpperCase() || 'Alphabet';
      return { type: kind === 'baconian' ? 'baconian' : 'frequency', headers,
        header_label: label, rows: [{ label: label === 'Alphabet' ? 'Alphabet' : label, values: headers },
          { label: kind === 'baconian' ? 'Baconian' : 'Frequency',
            values: kind === 'baconian' ? headers.map((_, index) =>
              Array.from({ length: 5 }, (_, bit) => index & (1 << (4 - bit)) ? 'B' : 'A').join('')) :
              frequencyCounts(lines, headers) },
          { label: 'Replacement', values: [] }] };
    }
    if (kind === 'nihilist') return { type: 'keyed_square',
      column_headers: ['1', '2', '3', '4', '5'], row_headers: ['1', '2', '3', '4', '5'],
      values: Array.from({ length: 5 }, () => Array(5).fill('')) };
    if (kind === 'checkerboard') return { type: 'checkerboard',
      values: Array.from({ length: 6 }, () => Array(6).fill('')) };
    if (kind === 'fractionated_morse') return { type: 'morse_fraction',
      fractions: DEFAULT_MORSE, replacement: Array(26).fill('') };
    if (kind === 'cryptarithm') return { type: 'cryptarithm',
      decode_digits: String(q.prompt || '').match(/\b(?:values?|digits?)\s*(\d{5,12})\b/i)?.[1] || '',
      formula_rows: [], digits: Array.from({ length: 10 }, (_, i) => String(i)),
      letters: ALPHABET.slice(0, 10) };
    return null;
  }

  function effectiveTables(q, kind, lines, layout) {
    const tables = Array.isArray(layout.tables) ? layout.tables.filter(table => table && typeof table === 'object') : [];
    const expected = ({ aristocrat: 'frequency', patristocrat: 'frequency', xenocrypt: 'frequency',
      baconian: 'baconian', nihilist: 'keyed_square', checkerboard: 'checkerboard',
      cryptarithm: 'cryptarithm', fractionated_morse: 'morse_fraction' })[kind];
    if (expected && !tables.some(table => table.type === expected)) {
      const fallback = fallbackTable(q, kind, lines);
      if (fallback) tables.push(fallback);
    }
    return tables;
  }

  function renderLetterTable(q, table, tableIndex, kind, lines) {
    const spanish = kind === 'xenocrypt';
    const sourceHeaders = Array.isArray(table.headers) && table.headers.length ? table.headers :
      (spanish ? SPANISH_ALPHABET : ALPHABET);
    const headers = spanish && sourceHeaders.length === 26 ? SPANISH_ALPHABET : sourceHeaders;
    if (headers.length > 36) return '';
    const source = Array.isArray(table.rows) ? table.rows : [];
    const sourceValues = (row) => headers.map(letter => {
      const index = sourceHeaders.indexOf(letter);
      return index < 0 ? '' : row?.values?.[index] ?? '';
    });
    const alphabetLabel = String(table.header_label || source.find(row => /^(?:alphabet|K\s*[123]|letter)$/i.test(row.label || ''))?.label || 'Alphabet');
    const k2 = /\bK\s*2\b/i.test(`${q.prompt || ''} ${alphabetLabel}`);
    const codeRow = source.find(row => /\b(?:baconian|pattern|code)\b/i.test(row.label || ''));
    const frequencyRow = source.find(row => /\bfrequency\b/i.test(row.label || ''));
    const replacementRow = source.find(row => /\b(?:replacement|solution|plaintext|decode)\b/i.test(row.label || ''));
    const alphabetRow = { label: k2 ? 'K2' : alphabetLabel, values: headers, alphabet: true };
    const fixedValues = table.type === 'baconian' ? sourceValues(codeRow) : sourceValues(frequencyRow);
    const counted = frequencyCounts(lines, headers);
    const frequencyValues = table.type === 'frequency' && (lines.length || !fixedValues.some(value => value !== '')) ?
      counted : spanish && !sourceHeaders.includes('Ñ') ?
        fixedValues.map((value, index) => headers[index] === 'Ñ' ? counted[index] : value) : fixedValues;
    const givenRow = { label: table.type === 'baconian' ? (codeRow?.label || 'Baconian') : 'Frequency',
      values: table.type === 'baconian' ? fixedValues : frequencyValues };
    const replacement = { label: replacementRow?.label || 'Replacement', values: sourceValues(replacementRow) };
    const rows = k2 && table.type === 'frequency' ? [replacement, alphabetRow, givenRow] :
      [alphabetRow, givenRow, replacement];
    const caption = table.type === 'baconian' ? 'Baconian cipher table' :
      spanish ? 'Xenocrypt alphabet frequency table' : 'Alphabet frequency table';
    return `<div class="cipher-table-scroll"><table class="cipher-table letter-table"><caption class="visually-hidden">${caption}</caption><tbody>` +
      rows.map((row, rowIndex) => `<tr class="${row.alphabet ? 'letter-alphabet-row' : row === replacement ? 'letter-replacement-row' : 'letter-frequency-row'}"><th scope="row">${escapeHtml(row.label)}</th>` +
        headers.map((letter, columnIndex) => {
          const raw = row.values?.[columnIndex];
          const label = `${caption}, ${row.label}, ${letter}`;
          if (row.alphabet) return `<th scope="col">${escapeHtml(letter)}</th>`;
          const key = `table:${tableIndex}:row:${rowIndex}:col:${columnIndex}`;
          const oldKey = row === replacement && rowIndex !== 1 ?
            `table:${tableIndex}:row:1:col:${columnIndex}` : undefined;
          return `<td>${raw === '' || raw == null ? cellInput(q, key, label, { fallbackKey: oldKey }) :
            `<span class="table-fixed">${escapeHtml(raw)}</span>`}</td>`;
        }).join('') + '</tr>').join('') + '</tbody></table></div>';
  }

  function renderKeyedSquare(q, table, tableIndex) {
    const columns = Array.isArray(table.column_headers) && table.column_headers.length
      ? table.column_headers : ['1', '2', '3', '4', '5'];
    const rows = Array.isArray(table.row_headers) && table.row_headers.length
      ? table.row_headers : ['1', '2', '3', '4', '5'];
    if (columns.length > 10 || rows.length > 10) return '';
    return `<div class="cipher-table-scroll square-wrap"><table class="cipher-table square-table"><caption class="visually-hidden">Nihilist keyed square</caption>` +
      `<thead><tr><th scope="col" class="table-corner" aria-label="Row and column"></th>${columns.map(col =>
        `<th scope="col">${escapeHtml(col)}</th>`).join('')}</tr></thead><tbody>` +
      rows.map((row, i) => `<tr><th scope="row">${escapeHtml(row)}</th>${columns.map((col, j) => {
        const given = table.values?.[i]?.[j];
        return `<td>${given === '' || given == null
          ? cellInput(q, `table:${tableIndex}:row:${i}:col:${j}`, `Nihilist square row ${row}, column ${col}`)
          : `<span class="table-fixed">${escapeHtml(given)}</span>`}</td>`;
      }).join('')}</tr>`).join('') + '</tbody></table></div>';
  }

  function renderCheckerboard(q, table, tableIndex) {
    return `<div class="cipher-table-scroll square-wrap"><table class="cipher-table checkerboard-grid" aria-label="Checkerboard, six rows and six columns of editable cells">` +
      `<tbody>${Array.from({ length: 6 }, (_, row) => `<tr>${Array.from({ length: 6 }, (_, column) =>
        `<td>${cellInput(q, `table:${tableIndex}:row:${row}:col:${column}`,
          `Checkerboard row ${row + 1}, column ${column + 1}`)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }

  function cryptFormulaRows(table, lines) {
    const supplied = Array.isArray(table.formula_rows) ? table.formula_rows.filter(row => String(row).trim()) : [];
    const possible = lines.map(line => String(line).replace(/\s/g, '')).filter(line =>
      /^[+A-Z]{3,20}$/.test(line) && /[A-Z]{3}/.test(line));
    const extracted = supplied.length ? supplied.slice(0, 24) :
      possible.length >= 3 && (possible.some(line => line.startsWith('+')) ||
        lines.some(line => String(line).trim() === '+')) ? possible.slice(0, 24) : [];
    return extracted.map((row, index) => {
      const text = String(row).trim();
      return index > 0 && index < extracted.length - 1 && !/^[+−-]/.test(text) ? `+${text}` : text;
    });
  }

  function renderCryptarithm(q, table, lines) {
    const decode = Array.from(String(table.decode_digits || '').replace(/\s/g, '')).slice(0, 60);
    const formula = cryptFormulaRows(table, lines);
    const digits = Array.from({ length: 10 }, (_, index) => String(index));
    const letters = ALPHABET.slice(0, 10);
    const usedFormulaLines = new Set();
    const formulaSources = formula.map(row => {
      const compact = String(row).replace(/[\s+]/g, '').toUpperCase();
      const index = lines.findIndex((line, lineIndex) => !usedFormulaLines.has(lineIndex) &&
        String(line).replace(/[\s+]/g, '').toUpperCase() === compact);
      if (index >= 0) usedFormulaLines.add(index);
      return index;
    });
    const decodeHtml = decode.length ? `<section class="crypt-decode"><h3>Values to decode for solution</h3>` +
      `<div class="cipher-table-scroll"><table class="crypt-decode-table" aria-label="Values to decode"><thead><tr>${decode.map(d =>
        `<th scope="col">${escapeHtml(d)}</th>`).join('')}</tr></thead><tbody><tr>${decode.map((d, index) =>
        `<td>${cellInput(q, `crypt:decode:${index}`, `Value decoded from ${d}, position ${index + 1}`)}</td>`).join('')}</tr></tbody></table></div></section>` : '';
    const formulaHtml = formula.length ? `<section class="crypt-formula"><h3>Cryptarithm formula</h3>` +
      `<div class="crypt-formula-equation" aria-label="${escapeHtml(formula.join(', then '))}">${formula.map((line, row) =>
        `<div class="crypt-formula-row${row === formula.length - 2 ? ' crypt-last-summand' : ''}${row === formula.length - 1 ? ' crypt-result' : ''}">` +
        Array.from(String(line)).map((char, column) => /^\s$/.test(char)
          ? `<span class="crypt-formula-space" aria-hidden="true"></span>` :
          `<span class="crypt-formula-unit"><span class="cipher-char-symbol" aria-hidden="true">${escapeHtml(char)}</span>` +
          (char === '+' || char === '=' ? '<span class="crypt-formula-space" aria-hidden="true"></span>' :
            cellInput(q, `crypt:formula:${row}:${column}`,
              `Cryptarithm formula line ${row + 1}, solve character ${column + 1} (${char})`, {
                size: 'letter', fallbackKey: (() => {
                  const lineIndex = formulaSources[row];
                  if (lineIndex < 0) return undefined;
                  const ordinal = Array.from(String(line)).slice(0, column + 1)
                    .filter(letter => /[A-ZÑ]/i.test(letter)).length - 1;
                  const originalPosition = Array.from(String(lines[lineIndex])).map((letter, index) =>
                    /[A-ZÑ]/i.test(letter) ? index : null).filter(index => index !== null)[ordinal];
                  return originalPosition == null ? undefined : `line:${lineIndex}:char:${originalPosition}`;
                })()
              })) +
          '</span>').join('') + '</div>'
      ).join('')}</div></section>` : '';
    const gridHtml = `<div class="cipher-table-scroll crypt-grid-scroll"><table class="cipher-table crypt-grid"><caption class="visually-hidden">Letter and digit grid</caption>` +
      `<thead><tr><th scope="col" class="table-corner" aria-label="Letter"></th>${digits.map(d =>
        `<th scope="col">${escapeHtml(d)}</th>`).join('')}</tr></thead><tbody>${letters.map((letter, row) =>
        `<tr><th scope="row">${cellInput(q, `crypt:label:${row}`,
          `Cryptarithm grid row ${row + 1}, replace placeholder ${letter}`, { size: 'label', placeholder: letter })}</th>${digits.map((digit, column) =>
          `<td>${cellInput(q, `crypt:grid:${row}:${column}`, `Cryptarithm grid row ${row + 1}, digit ${digit}`)}</td>`).join('')}</tr>`
      ).join('')}</tbody></table></div>`;
    return `<div class="crypt-workspace"><div class="crypt-left">${decodeHtml}${formulaHtml}</div>${gridHtml}</div>`;
  }

  function renderMorseTable(q, table, tableIndex) {
    const fractions = Array.isArray(table.fractions) ? table.fractions : [];
    const replacement = Array.isArray(table.replacement) ? table.replacement : [];
    return `<div class="cipher-table-scroll morse-table-scroll"><table class="cipher-table morse-table" aria-label="Fractionated Morse replacement and Morse fraction"><tbody>` +
      `<tr><th scope="row">Replacement</th>${DEFAULT_MORSE.map((_, index) => {
        const given = replacement[index];
        return `<td>${given === '' || given == null
          ? cellInput(q, `table:${tableIndex}:row:0:col:${index}`, `Morse replacement ${index + 1}`)
          : `<span class="table-fixed">${escapeHtml(given)}</span>`}</td>`;
      }).join('')}</tr><tr><th scope="row">Morse<br>fraction</th>${DEFAULT_MORSE.map((fallback, index) => {
        const symbols = String(fractions[index] ?? '').replace(/[.·●∙]/g, '•').replace(/[—–−-]/g, '−')
          .replace(/[Xx]/g, '×').replace(/[^•−×]/g, '');
        const triple = symbols.length === 3 ? symbols : fallback;
        return `<td><span class="morse-triple" aria-label="${escapeHtml(triple)}">${Array.from(triple).map(symbol =>
          `<span>${escapeHtml(symbol)}</span>`).join('')}</span></td>`;
      }).join('')}</tr></tbody></table></div>`;
  }

  function renderCipherTable(q, table, index, kind, lines) {
    if (!table || typeof table !== 'object') return '';
    if (table.type === 'frequency' || table.type === 'baconian') return renderLetterTable(q, table, index, kind, lines);
    if (table.type === 'keyed_square') return renderKeyedSquare(q, table, index);
    if (table.type === 'checkerboard') return renderCheckerboard(q, table, index);
    if (table.type === 'morse_fraction') return renderMorseTable(q, table, index);
    if (table.type === 'cryptarithm') return renderCryptarithm(q, table, lines);
    return '';
  }

  function renderCipher(q) {
    const layout = q.cipher_layout && typeof q.cipher_layout === 'object' ? q.cipher_layout : {};
    const kind = recognizedKind(q);
    const lines = cipherLinesForFallback(q, layout);
    const tables = effectiveTables(q, kind, lines, layout);
    const crypt = tables.find(table => table.type === 'cryptarithm');
    const formula = crypt ? cryptFormulaRows(crypt, lines).map(line => String(line).replace(/[\s+]/g, '').toUpperCase()) : [];
    const worksheetEntries = lines.map((line, index) => ({ line, index })).filter(({ line }) => {
      if (!crypt) return true;
      const compact = String(line).replace(/[\s+]/g, '').toUpperCase();
      const letters = String(line).toUpperCase().replace(/[^A-Z]/g, '');
      return compact ? !formula.includes(compact) &&
        !(formula.length && letters === formula.join('').replace(/[^A-Z]/g, '')) : !formula.length;
    });
    const worksheetLines = worksheetEntries.map(({ line }) => line);
    const signature = JSON.stringify([q.id, kind, q.prompt, q.cipher, worksheetLines, tables]);
    if (signature !== state.renderedCipherSignature) {
      const focused = document.activeElement;
      const key = els.cipherWorkspace.contains(focused) ? focused.dataset.cellKey : null;
      const selection = key && typeof focused.selectionStart === 'number' ?
        [focused.selectionStart, focused.selectionEnd] : null;
      const content = `${renderCipherLines(q, worksheetLines, worksheetEntries.map(entry => entry.index))}${tables.map((table, i) => renderCipherTable(q, table, i, kind, lines)).join('')}`;
      els.cipherWorkspace.innerHTML = content;
      els.cipherWorkspace.hidden = !content;
      els.cipherEmpty.hidden = Boolean(content);
      state.renderedCipherSignature = signature;
      if (key) {
        const restored = Array.from(els.cipherWorkspace.querySelectorAll('[data-cell-key]'))
          .find(field => field.dataset.cellKey === key);
        if (restored) {
          restored.focus({ preventScroll: true });
          if (selection) restored.setSelectionRange(...selection);
        }
      }
    } else {
      for (const field of els.cipherWorkspace.querySelectorAll('[data-cell-key]')) {
        const value = String(q.work_cells[field.dataset.cellKey] ?? q.work_cells[field.dataset.fallbackKey] ?? '');
        if (field !== document.activeElement && field.value !== value) field.value = value;
      }
    }
    els.activeCipher.hidden = true;
    setCellStatus();
  }

  function handleCellInput(event) {
    const field = event.target.closest('[data-cell-key]');
    const q = question();
    if (!q || !field || !els.cipherWorkspace.contains(field)) return;
    const key = field.dataset.cellKey;
    const value = field.value.slice(0, 1000);
    if (String(q.work_cells[key] ?? '') === value && !state.pendingCells.has(cellId(q.id, key))) return;
    q.work_cells[key] = value;
    state.cellSyncError = false;
    const cell = { type: 'cell_update', question_id: q.id, key, value };
    state.pendingCells.set(cellId(q.id, key), cell);
    savePendingCells();
    if (state.socketAuthenticated && state.socket?.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify(cell));
    }
    if (field.classList.contains('work-cell-letter') && field.value && !event.isComposing) {
      const fields = Array.from(els.cipherWorkspace.querySelectorAll('.work-cell-letter'));
      fields[fields.indexOf(field) + 1]?.focus();
    }
  }

  function handleCellKeydown(event) {
    const field = event.target;
    if (!field.classList?.contains('work-cell-letter')) return;
    const fields = Array.from(els.cipherWorkspace.querySelectorAll('.work-cell-letter'));
    const index = fields.indexOf(field);
    if (index < 0) return;
    const previous = fields[index - 1];
    const next = fields[index + 1];
    if (event.key === 'Backspace' && !field.value && previous) {
      event.preventDefault();
      previous.focus();
      previous.select();
    } else if (event.key === 'ArrowLeft' && previous) {
      event.preventDefault();
      previous.focus();
    } else if (event.key === 'ArrowRight' && next) {
      event.preventDefault();
      next.focus();
    }
  }

  function handleCellPaste(event) {
    const field = event.target;
    if (!field.classList?.contains('work-cell-letter')) return;
    const pasted = Array.from(event.clipboardData?.getData('text/plain') || '')
      .filter(char => !/\s/.test(char));
    if (!pasted.length) return;
    event.preventDefault();
    const fields = Array.from(els.cipherWorkspace.querySelectorAll('.work-cell-letter'));
    const start = fields.indexOf(field);
    for (let index = 0; index < pasted.length && start + index < fields.length; index++) {
      const target = fields[start + index];
      target.value = pasted[index];
      // Each character has its own key, so two teammates can solve the same
      // ciphertext row without replacing each other's other characters.
      handleCellInput({ target, isComposing: true });
    }
    fields[Math.min(start + pasted.length, fields.length - 1)]?.focus();
  }

  function handleCellBlur(event) {
    const field = event.target;
    const q = question();
    if (!q || !field?.dataset?.cellKey || !els.cipherWorkspace.contains(field)) return;
    const key = field.dataset.cellKey;
    const pending = state.pendingCells.get(cellId(q.id, key));
    if (pending) return;
    const current = String(q.work_cells[key] ?? q.work_cells[field.dataset.fallbackKey] ?? '');
    if (field.value !== current) field.value = current;
  }

  // Restore a caret against character IDs so a partner's insert before the
  // cursor does not move the editor to the wrong place.
  function caretAfter(previous, offset, current) {
    const beforeText = previous.map(n => n.c).join('');
    const prefix = Array.from(beforeText.slice(0, offset)).length;
    if (prefix === 0) return 0;
    const anchor = previous[prefix - 1];
    const now = current.findIndex(n => n.id === anchor?.id);
    if (now < 0) return Math.min(offset, current.map(n => n.c).join('').length);
    return current.slice(0, now + 1).map(n => n.c).join('').length;
  }
  function renderAnswer(previousNodes) {
    const q = question();
    if (!q) return;
    const current = visibleNodes(q);
    const value = current.map(n => n.c).join('');
    if (els.answerInput.value !== value && !state.isComposing) {
      const focused = document.activeElement === els.answerInput;
      const previous = previousNodes || (state.renderedAnswerQuestionId === q.id ? state.renderedAnswerNodes : null);
      const start = focused ? els.answerInput.selectionStart : 0;
      const end = focused ? els.answerInput.selectionEnd : 0;
      const direction = focused ? els.answerInput.selectionDirection : 'none';
      els.answerInput.value = value;
      if (focused && previous) els.answerInput.setSelectionRange(
        caretAfter(previous, start, current), caretAfter(previous, end, current), direction
      );
    }
    state.renderedAnswerQuestionId = q.id;
    state.renderedAnswerNodes = current.map(n => ({ id: n.id, c: n.c }));
    els.answerCount.textContent = `${current.length.toLocaleString()} ${current.length === 1 ? 'character' : 'characters'}`;
  }

  function handleAnswerInput() {
    if (state.isComposing) return;
    const q = question();
    if (!q) return;
    const oldNodes = visibleNodes(q);
    const oldChars = oldNodes.map(n => n.c);
    const newChars = Array.from(els.answerInput.value);
    let prefix = 0;
    while (prefix < oldChars.length && prefix < newChars.length && oldChars[prefix] === newChars[prefix]) prefix++;
    let suffix = 0;
    while (suffix < oldChars.length - prefix && suffix < newChars.length - prefix &&
      oldChars[oldChars.length - suffix - 1] === newChars[newChars.length - suffix - 1]) suffix++;
    if (prefix === oldChars.length && prefix === newChars.length) return;
    const deleted = oldNodes.slice(prefix, oldNodes.length - suffix).map(n => n.id);
    const right = oldNodes[oldNodes.length - suffix]?.pos || null;
    let left = oldNodes[prefix - 1]?.pos || null;
    const inserted = new Array(newChars.length - suffix - prefix);
    // Split a paste through the whole gap, so even a long insertion uses
    // short position paths rather than repeatedly subdividing one side.
    function assign(from, to, a, b) {
      if (from >= to) return;
      const mid = (from + to) >>> 1;
      const node = nextNode(a, b, newChars[prefix + mid]);
      inserted[mid] = node;
      assign(from, mid, a, node.pos);
      assign(mid + 1, to, node.pos, b);
    }
    assign(0, inserted.length, left, right);
    const added = inserted.map(({ id, pos, c }) => ({ id, pos, c }));
    // The server caps a single message at 1,000 changes. Chunk large pastes
    // and replacements while keeping every operation independently retryable.
    for (let i = 0; i < Math.max(added.length, deleted.length); i += 500) {
      const op = {
        type: 'answer_ops', question_id: q.id,
        inserts: added.slice(i, i + 500), deletes: deleted.slice(i, i + 500),
        op_id: `${state.actor}-${++state.counter}-op`
      };
      applyOps(q, op);
      state.pending.push(op);
      if (state.socketAuthenticated && state.socket?.readyState === WebSocket.OPEN) state.socket.send(JSON.stringify(op));
    }
    savePending();
    state.renderedAnswerQuestionId = q.id;
    state.renderedAnswerNodes = visibleNodes(q).map(n => ({ id: n.id, c: n.c }));
    els.answerCount.textContent = `${newChars.length.toLocaleString()} ${newChars.length === 1 ? 'character' : 'characters'}`;
  }

  function invalidatePdf() {
    state.pdfAbort?.abort();
    state.pdfAbort = null;
    if (state.pdfObjectUrl) URL.revokeObjectURL(state.pdfObjectUrl);
    state.pdfObjectUrl = null;
    state.pdfPageKey = null;
    els.pdfImage.removeAttribute('src');
    els.pdfImage.hidden = true;
  }
  function pdfMessage(title, description) {
    els.pdfEmpty.querySelector('strong').textContent = title;
    els.pdfEmpty.querySelector('p').textContent = description;
    els.pdfEmpty.hidden = false;
  }
  async function loadPdfPage(key, page) {
    const controller = new AbortController();
    state.pdfAbort = controller;
    try {
      const response = await fetch(`${backendOrigin}${roomPath(`/pages/${page}`)}?v=${state.pdfVersion}`, {
        headers: authorizedHeaders(), signal: controller.signal
      });
      if ([401, 403].includes(response.status)) { unauthorized(); return; }
      if (!response.ok) throw new Error(`PDF page failed to load (${response.status}).`);
      const blob = await response.blob();
      if (state.pdfPageKey !== key || controller.signal.aborted) return;
      state.pdfObjectUrl = URL.createObjectURL(blob);
      els.pdfImage.src = state.pdfObjectUrl;
    } catch (error) {
      if (controller.signal.aborted || state.pdfPageKey !== key) return;
      pdfMessage('Could not load this page', 'Check your connection, then try another page or refresh.');
      showToast(error.message, true);
    }
  }
  function renderPdf() {
    const available = isUploaded() && pageCount() > 0;
    state.page = available ? Math.max(1, Math.min(pageCount(), state.page)) : 1;
    els.pageIndicator.textContent = available ? `Page ${state.page} / ${pageCount()}` : 'No PDF';
    els.pagePrev.disabled = !available || state.page <= 1;
    els.pageNext.disabled = !available || state.page >= pageCount();
    els.zoomOut.disabled = !available || state.zoom <= 65;
    els.zoomIn.disabled = !available || state.zoom >= 175;
    els.zoomIndicator.textContent = `${state.zoom}%`;
    if (!available) {
      invalidatePdf();
      pdfMessage('Pages live here', 'Upload a PDF to see the original test beside your answers.');
      return;
    }
    const key = `${state.roomId}:${state.page}:${state.pdfVersion}`;
    if (state.pdfPageKey !== key) {
      invalidatePdf();
      state.pdfPageKey = key;
      pdfMessage('Loading page…', 'Getting the original test page.');
      els.pdfViewport.scrollTop = 0;
      els.pdfViewport.scrollLeft = 0;
      loadPdfPage(key, state.page);
    }
    sizePdf();
  }
  function sizePdf() {
    const base = Math.max(220, els.pdfViewport.clientWidth - 36);
    els.pdfImage.style.width = `${Math.round(base * state.zoom / 100)}px`;
  }

  function switchQuestion(id) {
    if (!question(id)) return;
    state.activeId = String(id);
    state.page = Math.max(1, Number(question()?.page) || 1);
    renderWorkspace();
    const selected = els.questionList.querySelector('.selected');
    selected?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
  }

  function showJoin(id = null, message = '') {
    clearTimeout(state.reconnectTimer);
    if (state.socket) { const old = state.socket; state.socket = null; old.close(); }
    state.socketAuthenticated = false;
    invalidatePdf();
    state.room = null;
    state.activeId = null;
    state.roomId = id || null;
    state.pending = [];
    state.pendingCells.clear();
    state.cellSyncError = false;
    state.renderedCipherSignature = null;
    els.questionList.replaceChildren();
    els.answerInput.value = '';
    els.activePrompt.textContent = '';
    els.activeCipher.textContent = '';
    els.cipherWorkspace.replaceChildren();
    els.testTitle.textContent = '';
    els.connection.hidden = true;
    els.workspace.hidden = true;
    els.landing.hidden = true;
    els.joinScreen.hidden = false;
    els.joinRoomId.value = id || '';
    els.joinPassword.value = '';
    els.joinError.textContent = message;
    els.joinError.hidden = !message;
    if (!setupError) (id ? els.joinPassword : els.joinRoomId).focus();
  }

  function parseRoomCode(value) {
    const input = value.trim();
    if (!input) return '';
    try {
      const url = new URL(input);
      return url.searchParams.get('room') || url.pathname.match(/\/r\/([^/]+)/)?.[1] || input;
    } catch (_) { return input; }
  }

  async function joinRoom(event) {
    event.preventDefault();
    if (setupError) return;
    const id = parseRoomCode(els.joinRoomId.value);
    if (!id) return;
    els.joinButton.disabled = true;
    els.joinError.hidden = true;
    try {
      const response = await request(`/api/rooms/${encodeURIComponent(id)}/join`,
        jsonOptions('POST', { password: els.joinPassword.value }), false);
      const room = roomFrom(response);
      state.roomId = String(room.id);
      setToken(state.roomId, response.token);
      els.joinPassword.value = '';
      history.replaceState(null, '', `${location.pathname}?room=${encodeURIComponent(state.roomId)}`);
      loadPending();
      loadPendingCells();
      applyRoom(room, true);
      renderPresence({ count: 1 });
      connect();
    } catch (error) {
      els.joinError.textContent = [401, 403].includes(error.status) ? 'The room code or password is incorrect. Try again.'
        : error.status === 404 ? 'Room not found. Check the room code or invite link.' : error.message;
      els.joinError.hidden = false;
      els.joinPassword.focus();
    } finally { els.joinButton.disabled = false; }
  }

  async function createRoom(event) {
    event.preventDefault();
    if (setupError) return;
    els.createButton.disabled = true;
    try {
      const response = await request('/api/rooms', jsonOptions('POST', {
        title: els.roomTitle.value.trim() || 'Codebusters practice', password: els.roomPassword.value
      }), false);
      const room = roomFrom(response);
      state.roomId = String(room.id);
      setToken(state.roomId, response.token);
      els.roomPassword.value = '';
      history.replaceState(null, '', `${location.pathname}?room=${encodeURIComponent(state.roomId)}`);
      loadPending();
      loadPendingCells();
      applyRoom(room, true);
      renderPresence({ count: 1 });
      connect();
    } catch (error) { showToast(error.message, true); }
    finally { els.createButton.disabled = false; }
  }

  async function loadRoom(id) {
    state.roomId = id;
    state.token = storedToken(id);
    if (!state.token) { showJoin(id); return; }
    loadPending();
    loadPendingCells();
    try {
      const response = await request(roomPath());
      applyRoom(roomFrom(response), true);
      renderPresence({ count: 1 });
      connect();
    } catch (error) {
      if (![401, 403].includes(error.status)) showJoin(id,
        error.status === 404 ? 'Room not found. Check the invite link.' : `Could not open this room. ${error.message}`);
    }
  }

  async function uploadPdf(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') {
      showToast('Please choose a PDF file.', true); return;
    }
    if (!state.roomId || state.loadingPdf) return;
    if (isUploaded() && !window.confirm('Replacing this PDF will erase the current questions and shared answers. Continue?')) return;
    state.loadingPdf = true;
    busy(true, 'Reading your test…', 'Finding the questions, cipher text and points');
    const form = new FormData(); form.append('file', file);
    try {
      const response = await request(roomPath('/upload'), { method: 'POST', body: form });
      state.pending = [];
      savePending();
      clearPendingCells();
      state.pdfVersion++;
      applyRoom(roomFrom(response), true);
      showToast(`${questions().length} ${questions().length === 1 ? 'question' : 'questions'} ready. Review them against the PDF.`);
    } catch (error) { showToast(error.message, true); }
    finally { state.loadingPdf = false; busy(false); }
  }

  async function copyText(value, message) {
    try { await navigator.clipboard.writeText(value); showToast(message); }
    catch (_) {
      const field = document.createElement('textarea');
      field.value = value; field.style.position = 'fixed'; field.style.opacity = '0';
      document.body.append(field); field.select();
      const copied = document.execCommand('copy'); field.remove();
      showToast(copied ? message : `Copy this: ${value}`, !copied);
    }
  }
  async function copyInvite() {
    const url = `${location.origin}${location.pathname}?room=${encodeURIComponent(state.roomId)}`;
    const local = ['localhost', '127.0.0.1', '::1'].includes(location.hostname);
    const message = local ? 'Link copied. For a partner on another device, replace localhost with this computer’s LAN IP. Share the password separately.' : 'Invite link copied. Share the password separately.';
    await copyText(url, message);
  }

  function openQuestionDialog(id = null) {
    state.editingId = id;
    const q = id ? question(id) : null;
    els.dialogTitle.textContent = q ? `Edit question ${q.number || ''}` : 'Add question';
    els.editPrompt.value = q?.prompt || '';
    els.editCipher.value = q?.cipher || '';
    els.editPoints.value = String(q?.points ?? 0);
    els.editPage.value = String(q?.page || state.page || 1);
    els.deleteQuestion.hidden = !q;
    els.questionDialog.showModal();
    els.editPrompt.focus();
  }

  async function saveQuestion(event) {
    event.preventDefault();
    const value = {
      prompt: els.editPrompt.value.trim(), cipher: els.editCipher.value.trim(),
      points: Math.max(0, Number.parseInt(els.editPoints.value, 10) || 0),
      page: Math.max(1, Number.parseInt(els.editPage.value, 10) || 1)
    };
    if (!value.prompt && !value.cipher) { showToast('Add a question or some cipher text first.', true); return; }
    els.saveQuestion.disabled = true;
    try {
      const id = state.editingId;
      const response = id ? await request(roomPath(`/questions/${encodeURIComponent(id)}`), jsonOptions('PATCH', value))
        : await request(roomPath('/questions'), jsonOptions('POST', value));
      els.questionDialog.close();
      const q = response.question || response;
      if (q?.id) {
        upsertQuestion(q);
        if (!id) switchQuestion(q.id);
      }
      showToast(id ? 'Question updated.' : 'Question added.');
    } catch (error) { showToast(error.message, true); }
    finally { els.saveQuestion.disabled = false; }
  }

  async function deleteQuestion() {
    const id = state.editingId;
    if (!id || !window.confirm('Delete this question and its shared answer?')) return;
    els.deleteQuestion.disabled = true;
    try {
      await request(roomPath(`/questions/${encodeURIComponent(id)}`), { method: 'DELETE' });
      state.room.questions = state.room.questions.filter(q => q.id !== id);
      state.pending = state.pending.filter(op => String(op.question_id) !== id);
      savePending();
      for (const [cellIdValue, cell] of state.pendingCells) {
        if (String(cell.question_id) === id) state.pendingCells.delete(cellIdValue);
      }
      savePendingCells();
      if (state.activeId === id) state.activeId = questions()[0]?.id || null;
      els.questionDialog.close();
      renderWorkspace();
      showToast('Question deleted.');
    } catch (error) { showToast(error.message, true); }
    finally { els.deleteQuestion.disabled = false; }
  }

  async function toggleClaim() {
    const q = question();
    if (!q) return;
    const wanted = els.claimInput.checked;
    q.checked = wanted;
    renderWorkspace();
    try {
      const response = await request(roomPath(`/questions/${encodeURIComponent(q.id)}`), jsonOptions('PATCH', { checked: wanted }));
      if (response.question) upsertQuestion(response.question);
    } catch (error) {
      q.checked = !wanted;
      renderWorkspace();
      showToast(error.message, true);
    }
  }

  function bind() {
    els.createForm.addEventListener('submit', createRoom);
    els.joinForm.addEventListener('submit', joinRoom);
    els.joinRoomLink.addEventListener('click', () => showJoin());
    els.inviteButton.addEventListener('click', copyInvite);
    els.copyCodeButton.addEventListener('click', () => copyText(state.roomId, 'Room code copied. Share the password separately.'));
    [els.pdfInputBanner, els.pdfInputSidebar].forEach(input => input.addEventListener('change', event => {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (file) uploadPdf(file);
    }));
    els.questionList.addEventListener('click', event => {
      const item = event.target.closest('[data-question-id]');
      if (item) switchQuestion(item.dataset.questionId);
    });
    els.addQuestionButton.addEventListener('click', () => openQuestionDialog());
    els.emptyAddButton.addEventListener('click', () => openQuestionDialog());
    els.editQuestionButton.addEventListener('click', () => openQuestionDialog(state.activeId));
    els.questionForm.addEventListener('submit', saveQuestion);
    els.dialogClose.addEventListener('click', () => els.questionDialog.close());
    els.dialogCancel.addEventListener('click', () => els.questionDialog.close());
    els.deleteQuestion.addEventListener('click', deleteQuestion);
    els.claimInput.addEventListener('change', toggleClaim);
    els.answerInput.addEventListener('input', handleAnswerInput);
    els.answerInput.addEventListener('compositionstart', () => { state.isComposing = true; });
    els.answerInput.addEventListener('compositionend', () => {
      state.isComposing = false;
      handleAnswerInput();
      for (const op of state.deferredOps.splice(0)) applyIncomingAnswer(op);
    });
    els.cipherWorkspace.addEventListener('input', handleCellInput);
    els.cipherWorkspace.addEventListener('keydown', handleCellKeydown);
    els.cipherWorkspace.addEventListener('paste', handleCellPaste);
    els.cipherWorkspace.addEventListener('focusout', handleCellBlur);
    els.previousQuestion.addEventListener('click', () => {
      const items = questions(), index = items.findIndex(q => q.id === state.activeId);
      if (index > 0) switchQuestion(items[index - 1].id);
    });
    els.nextQuestion.addEventListener('click', () => {
      const items = questions(), index = items.findIndex(q => q.id === state.activeId);
      if (index >= 0 && index < items.length - 1) switchQuestion(items[index + 1].id);
    });
    els.pagePrev.addEventListener('click', () => { state.page--; renderPdf(); });
    els.pageNext.addEventListener('click', () => { state.page++; renderPdf(); });
    els.zoomOut.addEventListener('click', () => { state.zoom = Math.max(65, state.zoom - 15); renderPdf(); });
    els.zoomIn.addEventListener('click', () => { state.zoom = Math.min(175, state.zoom + 15); renderPdf(); });
    els.pdfImage.addEventListener('error', () => {
      els.pdfImage.hidden = true;
      els.pdfEmpty.hidden = false;
      els.pdfEmpty.querySelector('strong').textContent = 'Could not load this page';
      els.pdfEmpty.querySelector('p').textContent = 'Try another page or refresh this workspace.';
    });
    els.pdfImage.addEventListener('load', () => {
      els.pdfImage.hidden = false;
      els.pdfEmpty.hidden = true;
    });
    window.addEventListener('resize', sizePdf);
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' || els.questionDialog.open) return;
      if (document.activeElement?.matches('input,textarea')) return;
      const items = questions(), index = items.findIndex(q => q.id === state.activeId);
      if (event.key === 'ArrowLeft' && index > 0) switchQuestion(items[index - 1].id);
      if (event.key === 'ArrowRight' && index >= 0 && index < items.length - 1) switchQuestion(items[index + 1].id);
    });
    window.addEventListener('online', () => { if (state.roomId && state.socket?.readyState !== WebSocket.OPEN) connect(); });
  }

  bind();
  const fromUrl = new URLSearchParams(location.search).get('room') || location.pathname.match(/^\/r\/([^/]+)/)?.[1];
  if (fromUrl) loadRoom(fromUrl);
})();
