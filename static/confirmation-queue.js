(() => {
  'use strict';

  const API_PATH = '/meetings/confirmation-tasks';
  const FIELD_LABELS = {
    owner: '負責人',
    due: '期限',
    evidence_timecodes: '討論／決議時間碼',
    source_timecodes: '待辦時間碼',
  };
  let pendingTasks = [];

  function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function installStyles() {
    const style = document.createElement('style');
    style.textContent = `
      .confirmation-overlay{position:fixed;inset:0;z-index:1200;background:rgba(8,10,20,.72);display:flex;align-items:center;justify-content:center;padding:24px}
      .confirmation-panel{width:min(900px,96vw);max-height:88vh;overflow:auto;background:var(--bg-card,#171927);border:1px solid var(--border,#303348);border-radius:16px;padding:20px;box-shadow:0 24px 80px rgba(0,0,0,.45)}
      .confirmation-header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px}
      .confirmation-list{display:grid;gap:10px}
      .confirmation-item{border:1px solid var(--border,#303348);border-radius:12px;padding:12px;background:rgba(255,255,255,.025)}
      .confirmation-meta{color:var(--text-dim,#9a9db2);font-size:12px;margin-bottom:6px}
      .confirmation-source{font-size:13px;margin-bottom:9px;word-break:break-word}
      .confirmation-actions{display:flex;gap:8px;flex-wrap:wrap}
      .confirmation-input{flex:1;min-width:220px;border:1px solid var(--border,#303348);border-radius:8px;padding:8px 10px;background:var(--bg,#0f111b);color:var(--text,#eef0f8)}
      .confirmation-status{font-size:13px;color:var(--text-dim,#9a9db2)}
    `;
    document.head.appendChild(style);
  }

  async function fetchPendingTasks() {
    const response = await fetch(`${API_PATH}?status=pending&limit=500`);
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    pendingTasks = Array.isArray(payload.items) ? payload.items : [];
    return pendingTasks;
  }

  function updateTile(tasks, error) {
    const value = document.getElementById('ops-confirmation-count');
    const detail = document.getElementById('ops-confirmation-detail');
    if (!value || !detail) return;
    if (error) {
      value.textContent = '!';
      detail.textContent = '讀取失敗';
      return;
    }
    value.textContent = String(tasks.length);
    detail.textContent = tasks.length ? '待人工確認' : '已清空';
  }

  async function patchTask(task, status, resolutionValue) {
    const response = await fetch(`${API_PATH}/${task.id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        status,
        resolution_value: resolutionValue || null,
        resolution_note: status === 'waived' ? '由管理介面略過' : '由管理介面確認',
      }),
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  function renderTask(task, list, statusElement) {
    const item = createElement('article', 'confirmation-item');
    const label = FIELD_LABELS[task.field_name] || task.field_name;
    item.appendChild(createElement(
      'div',
      'confirmation-meta',
      `會議 #${task.meeting_id} · ${task.meeting_title} · ${task.item_key} · ${label}`,
    ));
    item.appendChild(createElement(
      'div',
      'confirmation-source',
      `目前值：${task.source_value || '未提及'}`,
    ));
    const actions = createElement('div', 'confirmation-actions');
    const input = createElement('input', 'confirmation-input');
    input.type = 'text';
    input.placeholder = task.field_name.includes('timecodes')
      ? '例如 12:30、18:20'
      : `輸入${label}`;
    input.setAttribute('aria-label', `${task.item_key} ${label}`);
    const resolve = createElement('button', 'btn-primary', '確認');
    const waive = createElement('button', 'btn-secondary', '略過');
    resolve.addEventListener('click', async () => {
      const value = input.value.trim();
      if (!value) {
        statusElement.textContent = `${task.item_key} 的${label}尚未輸入。`;
        input.focus();
        return;
      }
      resolve.disabled = true;
      waive.disabled = true;
      try {
        await patchTask(task, 'resolved', value);
        item.remove();
        pendingTasks = pendingTasks.filter(candidate => candidate.id !== task.id);
        updateTile(pendingTasks);
        statusElement.textContent = `${task.item_key} 的${label}已確認。`;
      } catch (error) {
        statusElement.textContent = `更新失敗：${error.message}`;
        resolve.disabled = false;
        waive.disabled = false;
      }
    });
    waive.addEventListener('click', async () => {
      waive.disabled = true;
      resolve.disabled = true;
      try {
        await patchTask(task, 'waived', '');
        item.remove();
        pendingTasks = pendingTasks.filter(candidate => candidate.id !== task.id);
        updateTile(pendingTasks);
        statusElement.textContent = `${task.item_key} 的${label}已略過。`;
      } catch (error) {
        statusElement.textContent = `更新失敗：${error.message}`;
        waive.disabled = false;
        resolve.disabled = false;
      }
    });
    actions.append(input, resolve, waive);
    item.appendChild(actions);
    list.appendChild(item);
  }

  async function openQueue() {
    const overlay = createElement('div', 'confirmation-overlay');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    const panel = createElement('section', 'confirmation-panel');
    const header = createElement('div', 'confirmation-header');
    header.appendChild(createElement('h2', '', '結構化會議人工確認'));
    const close = createElement('button', 'btn-secondary', '關閉');
    close.addEventListener('click', () => overlay.remove());
    header.appendChild(close);
    const status = createElement('div', 'confirmation-status', '載入中…');
    const list = createElement('div', 'confirmation-list');
    panel.append(header, status, list);
    overlay.appendChild(panel);
    overlay.addEventListener('click', event => {
      if (event.target === overlay) overlay.remove();
    });
    document.body.appendChild(overlay);
    close.focus();
    try {
      const tasks = await fetchPendingTasks();
      updateTile(tasks);
      status.textContent = tasks.length
        ? `共 ${tasks.length} 個待確認欄位。`
        : '目前沒有待確認欄位。';
      tasks.forEach(task => renderTask(task, list, status));
    } catch (error) {
      status.textContent = `載入失敗：${error.message}`;
      updateTile([], error);
    }
  }

  function installTile() {
    const dashboard = document.getElementById('ops-dashboard');
    if (!dashboard || document.getElementById('ops-confirmation-tile')) return;
    const tile = createElement('div', 'ops-tile actionable');
    tile.id = 'ops-confirmation-tile';
    tile.role = 'button';
    tile.tabIndex = 0;
    tile.title = '開啟結構化會議人工確認佇列';
    tile.setAttribute('aria-label', tile.title);
    tile.append(
      createElement('div', 'ops-label', '待確認欄位'),
      createElement('div', 'ops-value', '–'),
      createElement('div', 'ops-detail', '載入中'),
    );
    tile.children[1].id = 'ops-confirmation-count';
    tile.children[2].id = 'ops-confirmation-detail';
    tile.addEventListener('click', openQueue);
    tile.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openQueue();
      }
    });
    dashboard.appendChild(tile);
    fetchPendingTasks()
      .then(tasks => updateTile(tasks))
      .catch(error => updateTile([], error));
  }

  window.addEventListener('DOMContentLoaded', () => {
    installStyles();
    installTile();
  });
})();
