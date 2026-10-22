(function () {
  'use strict';

  const DB_NAME = 'meeting-assistant-recording-recovery';
  const DB_VERSION = 1;
  const SESSION_STORE = 'sessions';
  const CHUNK_STORE = 'chunks';
  const state = {
    dbPromise: null,
    activeSessionId: null,
    activeChunkIndex: 0,
    writeQueue: Promise.resolve(),
    lastWriteError: '',
    maxUploadBytes: 500 * 1024 * 1024,
    busySessionId: null,
    initialized: false,
    uploadQueue: Promise.resolve(),
  };

  function requestResult(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('IndexedDB request failed'));
    });
  }

  function transactionDone(transaction) {
    return new Promise((resolve, reject) => {
      transaction.oncomplete = () => resolve();
      transaction.onabort = () => reject(transaction.error || new Error('IndexedDB transaction aborted'));
      transaction.onerror = () => reject(transaction.error || new Error('IndexedDB transaction failed'));
    });
  }

  function openDatabase() {
    if (!window.indexedDB) {
      return Promise.reject(new Error('此瀏覽器不支援錄音中斷保護。'));
    }
    if (state.dbPromise) return state.dbPromise;
    state.dbPromise = new Promise((resolve, reject) => {
      const request = window.indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(SESSION_STORE)) {
          db.createObjectStore(SESSION_STORE, { keyPath: 'id' });
        }
        if (!db.objectStoreNames.contains(CHUNK_STORE)) {
          const chunks = db.createObjectStore(CHUNK_STORE, { keyPath: 'key' });
          chunks.createIndex('sessionId', 'sessionId', { unique: false });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('無法開啟錄音中斷保護儲存區。'));
      request.onblocked = () => reject(new Error('錄音中斷保護儲存區正在被其他分頁使用。'));
    });
    return state.dbPromise;
  }

  function newSessionId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    return `recording-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function putSession(session) {
    const db = await openDatabase();
    const transaction = db.transaction(SESSION_STORE, 'readwrite');
    transaction.objectStore(SESSION_STORE).put(session);
    await transactionDone(transaction);
    return session;
  }

  async function getSession(sessionId) {
    const db = await openDatabase();
    const transaction = db.transaction(SESSION_STORE, 'readonly');
    const done = transactionDone(transaction);
    const session = await requestResult(transaction.objectStore(SESSION_STORE).get(sessionId));
    await done;
    return session || null;
  }

  async function listSessions() {
    const db = await openDatabase();
    const transaction = db.transaction(SESSION_STORE, 'readonly');
    const done = transactionDone(transaction);
    const sessions = await requestResult(transaction.objectStore(SESSION_STORE).getAll());
    await done;
    return (sessions || []).sort((left, right) =>
      String(right.updatedAt || right.createdAt || '').localeCompare(String(left.updatedAt || left.createdAt || ''))
    );
  }

  async function updateSession(sessionId, changes) {
    const current = await getSession(sessionId);
    if (!current) return null;
    return putSession({
      ...current,
      ...changes,
      updatedAt: new Date().toISOString(),
    });
  }

  async function storeChunk(sessionId, index, blob, seconds) {
    const db = await openDatabase();
    const transaction = db.transaction([SESSION_STORE, CHUNK_STORE], 'readwrite');
    const done = transactionDone(transaction);
    const sessions = transaction.objectStore(SESSION_STORE);
    const chunks = transaction.objectStore(CHUNK_STORE);
    const current = await requestResult(sessions.get(sessionId));
    if (!current) {
      transaction.abort();
      throw new Error('找不到目前錄音的中斷保護資料。');
    }
    chunks.put({
      key: `${sessionId}:${String(index).padStart(9, '0')}`,
      sessionId,
      index,
      blob,
      bytes: Number(blob.size || 0),
    });
    sessions.put({
      ...current,
      bytes: Number(current.bytes || 0) + Number(blob.size || 0),
      chunkCount: Math.max(Number(current.chunkCount || 0), index + 1),
      seconds: Math.max(Number(current.seconds || 0), Number(seconds || 0)),
      updatedAt: new Date().toISOString(),
    });
    await done;
  }

  async function getSessionChunks(sessionId) {
    const db = await openDatabase();
    const transaction = db.transaction(CHUNK_STORE, 'readonly');
    const done = transactionDone(transaction);
    const index = transaction.objectStore(CHUNK_STORE).index('sessionId');
    const chunks = await requestResult(index.getAll(window.IDBKeyRange.only(sessionId)));
    await done;
    return (chunks || []).sort((left, right) => Number(left.index || 0) - Number(right.index || 0));
  }

  async function deleteSession(sessionId) {
    const db = await openDatabase();
    const transaction = db.transaction([SESSION_STORE, CHUNK_STORE], 'readwrite');
    transaction.objectStore(SESSION_STORE).delete(sessionId);
    const chunkIndex = transaction.objectStore(CHUNK_STORE).index('sessionId');
    const cursorRequest = chunkIndex.openCursor(window.IDBKeyRange.only(sessionId));
    cursorRequest.onsuccess = () => {
      const cursor = cursorRequest.result;
      if (!cursor) return;
      cursor.delete();
      cursor.continue();
    };
    await transactionDone(transaction);
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value || 0));
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function formatDuration(value) {
    const seconds = Math.max(0, Number(value || 0));
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, '0')}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
  }

  function formatDate(value) {
    const parsed = new Date(value || '');
    return Number.isNaN(parsed.getTime()) ? '時間不明' : parsed.toLocaleString('zh-TW');
  }

  function statusLabel(session) {
    if (session.status === 'uploading' && state.busySessionId === session.id) return '正在背景上傳';
    if (session.status === 'upload_failed') return '上傳失敗，可重啟';
    if (session.status === 'uploading') return '上傳可能中斷，可重啟';
    if (session.status === 'ready') return '等待重新送出';
    return '錄製中斷';
  }

  function ensurePanel() {
    let panel = document.getElementById('recording-recovery-panel');
    if (panel) return panel;
    const modal = document.getElementById('record-modal');
    const result = document.getElementById('rec-result');
    if (!modal || !result) return null;
    panel = document.createElement('section');
    panel.id = 'recording-recovery-panel';
    panel.className = 'recording-recovery-panel';
    panel.setAttribute('aria-labelledby', 'recording-recovery-title');
    panel.innerHTML = `
      <div class="recording-recovery-header">
        <div class="recording-recovery-title" id="recording-recovery-title">可恢復的中斷錄製</div>
        <button type="button" class="recording-recovery-refresh" data-recovery-action="refresh">更新</button>
      </div>
      <div class="recording-recovery-help">錄製期間每秒保存在這台裝置的此瀏覽器；中斷後可重新送出，成功排入後自動移除本機副本。</div>
      <div class="recording-recovery-status" id="recording-recovery-status" role="status" aria-live="polite"></div>
      <div class="recording-recovery-list" id="recording-recovery-list" role="list"></div>`;
    panel.addEventListener('click', handlePanelClick);
    result.insertAdjacentElement('afterend', panel);
    return panel;
  }

  function setStatus(message, error) {
    const status = document.getElementById('recording-recovery-status');
    if (!status) return;
    status.textContent = String(message || '');
    status.classList.toggle('error', Boolean(error));
  }

  function updateHeaderCount(count) {
    const button = document.getElementById('btn-open-record');
    if (!button) return;
    let badge = button.querySelector('.recording-recovery-count');
    if (count <= 0) {
      badge?.remove();
      button.removeAttribute('title');
      return;
    }
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'recording-recovery-count';
      button.appendChild(badge);
    }
    badge.textContent = String(count);
    button.title = `有 ${count} 筆可恢復的中斷錄製`;
  }

  function renderSessions(sessions) {
    const list = document.getElementById('recording-recovery-list');
    if (!list) return;
    updateHeaderCount(sessions.length);
    if (!sessions.length) {
      list.innerHTML = '<div class="recording-recovery-empty">目前沒有可恢復的中斷錄製。</div>';
      return;
    }
    list.innerHTML = sessions.map(session => {
      const id = escapeHtml(session.id);
      const busy = state.busySessionId === session.id;
      const canRestart = Number(session.chunkCount || 0) > 0 && !busy;
      return `<div class="recording-recovery-item" role="listitem">
        <div class="recording-recovery-info">
          <div class="recording-recovery-name">${escapeHtml(session.title || '未命名中斷錄製')}</div>
          <div class="recording-recovery-meta">
            <span class="recording-recovery-badge">${escapeHtml(statusLabel(session))}</span>
            <span>${escapeHtml(formatDate(session.createdAt))}</span>
            <span>${escapeHtml(formatDuration(session.seconds))}</span>
            <span>${escapeHtml(formatBytes(session.bytes))}</span>
            <span>${Number(session.chunkCount || 0)} 段</span>
          </div>
        </div>
        <div class="recording-recovery-actions">
          <button type="button" class="recording-recovery-action primary" data-recovery-action="restart" data-session-id="${id}" ${canRestart ? '' : 'disabled'}>${busy ? '送出中' : '重新送出'}</button>
          <button type="button" class="recording-recovery-action danger" data-recovery-action="delete" data-session-id="${id}" ${busy ? 'disabled' : ''}>刪除</button>
        </div>
      </div>`;
    }).join('');
  }

  async function refresh() {
    ensurePanel();
    try {
      const sessions = await listSessions();
      renderSessions(sessions);
      if (state.lastWriteError) {
        setStatus(`最近一次本機保存失敗：${state.lastWriteError}`, true);
      } else {
        setStatus(sessions.length ? `找到 ${sessions.length} 筆可恢復錄製。` : '中斷保護已啟用。');
      }
      return sessions;
    } catch (error) {
      updateHeaderCount(0);
      setStatus(error.message || String(error), true);
      return [];
    }
  }

  async function begin(metadata) {
    ensurePanel();
    if (navigator.storage && typeof navigator.storage.persist === 'function') {
      navigator.storage.persist().catch(() => false);
    }
    const now = new Date().toISOString();
    const session = {
      id: newSessionId(),
      title: String(metadata.title || '').trim() || `現場錄製_${new Date().toLocaleString('zh-TW').replace(/[/:]/g, '-')}`,
      customVocabulary: String(metadata.customVocabulary || '').trim(),
      processingMode: metadata.processingMode === 'local' ? 'local' : 'cloud',
      trainingCourseId: metadata.trainingCourseId || null,
      trainingClientId: metadata.trainingClientId || null,
      mode: String(metadata.mode || 'audio'),
      profileId: String(metadata.profileId || 'audio_standard'),
      mimeType: String(metadata.mimeType || 'audio/webm'),
      warning: '',
      status: 'recording',
      seconds: 0,
      bytes: 0,
      chunkCount: 0,
      createdAt: now,
      updatedAt: now,
      error: '',
    };
    await putSession(session);
    state.activeSessionId = session.id;
    state.activeChunkIndex = 0;
    state.writeQueue = Promise.resolve();
    state.lastWriteError = '';
    await refresh();
    return session.id;
  }

  function captureChunk(blob, seconds) {
    if (!state.activeSessionId || !blob || Number(blob.size || 0) <= 0) return;
    const sessionId = state.activeSessionId;
    const index = state.activeChunkIndex++;
    state.writeQueue = state.writeQueue
      .then(() => storeChunk(sessionId, index, blob, seconds))
      .catch(error => {
        state.lastWriteError = error.message || String(error);
        setStatus(`錄音仍在進行，但中斷保護寫入失敗：${state.lastWriteError}`, true);
      });
  }

  async function finish(metadata) {
    const sessionId = state.activeSessionId;
    if (!sessionId) return null;
    await state.writeQueue;
    const current = await getSession(sessionId);
    const session = await updateSession(sessionId, {
      title: String(metadata.title || '').trim() || current?.title || '中斷錄製',
      customVocabulary: String(metadata.customVocabulary || '').trim(),
      seconds: Number(metadata.seconds || 0),
      warning: String(metadata.warning || '').trim(),
      status: 'ready',
      error: state.lastWriteError,
    });
    await refresh();
    return session;
  }

  async function markUploadFailed(message) {
    const sessionId = state.activeSessionId;
    if (!sessionId) return;
    await state.writeQueue;
    await updateSession(sessionId, { status: 'upload_failed', error: String(message || '') });
    await refresh();
  }

  async function removeActive() {
    const sessionId = state.activeSessionId;
    state.activeSessionId = null;
    state.activeChunkIndex = 0;
    if (sessionId) await deleteSession(sessionId);
    await refresh();
  }

  async function discardActive() {
    const sessionId = state.activeSessionId;
    state.activeSessionId = null;
    state.activeChunkIndex = 0;
    await state.writeQueue;
    if (sessionId) await deleteSession(sessionId);
    await refresh();
  }

  function extensionForMimeType(mimeType) {
    const normalized = String(mimeType || '').toLowerCase();
    if (normalized.includes('ogg')) return 'ogg';
    if (normalized.includes('mp4')) return 'mp4';
    return 'webm';
  }

  async function apiErrorMessage(response) {
    const copy = response.clone();
    try {
      const payload = await response.json();
      return payload.detail || payload.error || `HTTP ${response.status}`;
    } catch (_) {
      const text = await copy.text();
      return text || `HTTP ${response.status}`;
    }
  }

  async function restart(sessionId, resumed = true) {
    const session = await getSession(sessionId);
    if (!session) throw new Error('找不到這筆中斷錄製。');
    const chunks = await getSessionChunks(sessionId);
    if (!chunks.length) throw new Error('這筆中斷錄製沒有可送出的媒體片段。');
    const mimeType = session.mimeType || chunks[0].blob.type || 'audio/webm';
    const blob = new Blob(chunks.map(item => item.blob), { type: mimeType });
    if (blob.size > state.maxUploadBytes) {
      throw new Error(`錄製檔 ${formatBytes(blob.size)} 超過上傳上限 ${formatBytes(state.maxUploadBytes)}。`);
    }

    state.busySessionId = sessionId;
    await updateSession(sessionId, { status: 'uploading', error: '' });
    await refresh();
    setStatus(`正在重新送出「${session.title}」...`);

    const file = new File([blob], `recovered-recording.${extensionForMimeType(mimeType)}`, { type: mimeType });
    const formData = new FormData();
    formData.append('file', file);
    formData.append('title', session.title || '中斷錄製');
    formData.append('processing_mode', session.processingMode || 'cloud');
    if (session.customVocabulary) formData.append('custom_vocabulary', session.customVocabulary);
    if (session.profileId) formData.append('recording_profile', session.profileId);
    const warning = [
      session.warning,
      resumed ? '此錄製曾在瀏覽器端中斷，已從本機中斷保護儲存區重新送出。' : '',
    ].filter(Boolean).join('\n');
    formData.append('recording_warning', warning);

    try {
      if (session.trainingCourseId) {
        const reservation = await fetch(`/training/courses/${encodeURIComponent(session.trainingCourseId)}/parts`, {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({client_id:session.trainingClientId || session.id,title:session.title}),
        });
        if (!reservation.ok) throw new Error(await apiErrorMessage(reservation));
        const part = await reservation.json();
        formData.append('training_part_id', part.id);
      }
      const response = await fetch('/upload-media', { method: 'POST', body: formData });
      if (!response.ok) throw new Error(await apiErrorMessage(response));
      const payload = await response.json();
      await deleteSession(sessionId);
      state.busySessionId = null;
      await refresh();
      setStatus(`已重新排入處理，任務 ${String(payload.job_id || '').slice(0, 8)}。`);
      if (typeof window.loadJobs === 'function') window.loadJobs();
      if (typeof window.loadMetrics === 'function') window.loadMetrics();
      return payload;
    } catch (error) {
      state.busySessionId = null;
      await updateSession(sessionId, { status: 'upload_failed', error: error.message || String(error) });
      await refresh();
      setStatus(`重新送出失敗：${error.message || error}`, true);
      throw error;
    }
  }

  async function remove(sessionId) {
    const session = await getSession(sessionId);
    if (!session) return;
    if (!window.confirm(`刪除中斷錄製「${session.title}」？刪除後無法從此瀏覽器恢復。`)) return;
    await deleteSession(sessionId);
    await refresh();
  }

  async function download(sessionId) {
    const session = await getSession(sessionId);
    const chunks = await getSessionChunks(sessionId);
    if (!session || !chunks.length) throw new Error('這一段沒有可下載的錄製資料。');
    const blob = new Blob(chunks.map(item=>item.blob), {type:session.mimeType || chunks[0].blob.type});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href=url;link.download=`course-recording-${sessionId}.${extensionForMimeType(blob.type)}`;
    link.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }

  async function handlePanelClick(event) {
    const button = event.target.closest('[data-recovery-action]');
    if (!button) return;
    const action = button.dataset.recoveryAction;
    const sessionId = button.dataset.sessionId;
    button.disabled = true;
    try {
      if (action === 'refresh') await refresh();
      if (action === 'restart' && sessionId) await restart(sessionId);
      if (action === 'delete' && sessionId) await remove(sessionId);
    } catch (error) {
      console.error('Recording recovery action failed', error);
    } finally {
      if (action === 'refresh') button.disabled = false;
    }
  }

  async function restartServerMedia(filename) {
    const name = String(filename || '').trim();
    if (!name) return;
    if (!window.confirm(`重新處理未連結原始檔「${name}」？`)) return;
    if (typeof window.setSourceStorageStatus === 'function') {
      window.setSourceStorageStatus(`正在重新排入「${name}」...`, false, true);
    }
    try {
      const response = await fetch(`/source-media/inventory/${encodeURIComponent(name)}/restart`, { method: 'POST' });
      if (!response.ok) throw new Error(await apiErrorMessage(response));
      const payload = await response.json();
      if (typeof window.openSourceStorageInventory === 'function') await window.openSourceStorageInventory();
      if (typeof window.loadJobs === 'function') window.loadJobs();
      if (typeof window.loadMetrics === 'function') window.loadMetrics();
      window.alert(`已重新排入處理：${String(payload.job_id || '').slice(0, 8)}`);
      return payload;
    } catch (error) {
      if (typeof window.setSourceStorageStatus === 'function') {
        window.setSourceStorageStatus(`重新處理失敗：${error.message || error}`, true);
      }
      console.error('Unable to restart retained source media', error);
      return null;
    }
  }

  async function initialize(options) {
    if (options && Number(options.maxUploadBytes) > 0) {
      state.maxUploadBytes = Number(options.maxUploadBytes);
    }
    ensurePanel();
    state.initialized = true;
    return refresh();
  }

  window.RecordingRecovery = {
    begin,
    captureChunk,
    discardActive,
    finish,
    initialize,
    markUploadFailed,
    refresh,
    removeActive,
    restartServerMedia,
    listSessions,
    restart,
    download,
    detachActive: () => { const id=state.activeSessionId; state.activeSessionId=null; state.activeChunkIndex=0; return id; },
    queueUpload: sessionId => {
      const work = state.uploadQueue.catch(() => {}).then(() => restart(sessionId, false));
      state.uploadQueue = work;
      return work;
    },
  };
}());
