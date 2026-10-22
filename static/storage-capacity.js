/* Capacity scopes are separate: original media is not total system storage. */
(function () {
  'use strict';
  const labels = { snapshots: '日常快照 ZIP', shared_media: '共用影音副本', release: '發版／修復專用備份', database: 'SQLite 備份', other: '其他備份與收據' };
  let last = null;
  function render(storage) {
    const value = document.getElementById('ops-backup-storage');
    const tile = document.getElementById('ops-backup-storage-tile');
    if (!value || !tile) return;
    last = storage?.backup_storage || null;
    const valid = last?.complete === true && Number.isFinite(last.total_bytes);
    value.textContent = valid ? formatBytes(last.total_bytes) : '未取得';
    tile.title = valid ? '本機備份容量明細（不含原始影音、異地備份及資源回收筒）' : '容量未完整讀取；請重新整理後再查看';
    value.classList.toggle('attention', !valid);
  }
  function open() {
    let dialog = document.getElementById('backup-capacity-dialog');
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.id = 'backup-capacity-dialog';
      dialog.setAttribute('aria-labelledby', 'backup-capacity-title');
      dialog.className = 'backup-capacity-dialog';
      document.body.append(dialog);
    }
    dialog.replaceChildren();
    const heading = document.createElement('h2');
    heading.id = 'backup-capacity-title'; heading.textContent = '本機備份容量';
    const note = document.createElement('p');
    note.textContent = '不含原始影音、異地備份與資源回收筒。';
    dialog.append(heading, note);
    if (last?.complete === true && Number.isFinite(last.total_bytes)) {
      const list = document.createElement('dl');
      Object.entries(labels).forEach(([key, label]) => {
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = label; dd.textContent = formatBytes(last.categories?.[key] || 0);
        list.append(dt, dd);
      });
      dialog.append(list);
      const total = document.createElement('p');
      total.textContent = `合計：${formatBytes(last.total_bytes)}（${Number(last.total_bytes).toLocaleString()} bytes）`;
      dialog.append(total);
    } else {
      const error = document.createElement('p');
      error.textContent = '請先關閉並重新整理頁面，再查看容量；目前未取得完整備份統計，不以 0 代替。';
      dialog.append(error);
    }
    const unit = document.createElement('p');
    unit.textContent = '1 GiB = 1,024 MiB = 1,073,741,824 bytes。新快照與同目錄的 media_objects 資料夾是一組備份，請勿分開搬移或刪除。';
    const close = document.createElement('button');
    close.type = 'button'; close.textContent = '關閉'; close.className = 'btn';
    close.addEventListener('click', () => dialog.close());
    dialog.append(unit, close);
    dialog.showModal();
  }
  window.StorageCapacity = { render, open };
}());
