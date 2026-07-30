(() => {
  const input = document.getElementById('previous-minutes-input');
  const status = document.getElementById('previous-minutes-status');
  const hint = document.getElementById('previous-minutes-hint');
  let selectedFile = null;
  let selectionError = '';

  function maxBytes() {
    return Number(runtimeConfig.previous_minutes_max_bytes) || 20 * 1024 * 1024;
  }

  function render(message = '未選擇前次會議紀錄。', isError = false) {
    if (!status) return;
    status.textContent = message;
    status.style.color = isError ? 'var(--red)' : '';
  }

  function select(file) {
    selectedFile = null;
    selectionError = '';
    if (!file) {
      render();
      return;
    }
    if (!String(file.name || '').toLowerCase().endsWith('.docx')) {
      selectionError = '前次會議紀錄僅支援 Word .docx 格式。';
    } else if (file.size > maxBytes()) {
      selectionError = `前次會議紀錄過大，上限 ${formatBytes(maxBytes())}。`;
    }
    if (selectionError) {
      input.value = '';
      render(selectionError, true);
      return;
    }
    selectedFile = file;
    render(`📄 ${file.name} (${formatBytes(file.size)})`);
  }

  input?.addEventListener('change', event => select(event.target.files?.[0]));
  window.previousMinutesUpload = {
    appendTo(formData) {
      if (selectedFile) formData.append('previous_minutes_file', selectedFile);
    },
    validate() {
      if (!selectionError) return true;
      alert(selectionError);
      return false;
    },
    reset() {
      selectedFile = null;
      selectionError = '';
      if (input) input.value = '';
      render();
    },
    refreshConfig() {
      if (hint) {
        hint.textContent = `僅支援含可讀文字的 .docx，單檔上限 ${formatBytes(maxBytes())}；本次狀態仍以本次逐字稿為準。`;
      }
      if (selectedFile && selectedFile.size > maxBytes()) select(selectedFile);
    },
  };
  window.previousMinutesUpload.refreshConfig();
})();
