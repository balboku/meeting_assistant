(() => {
  function setStatus(message = '', level = 'info', busy = false) {
    const status = document.getElementById('previous-minutes-rerun-status');
    if (!status) return;
    status.textContent = message;
    status.classList.toggle('success', level === 'success' && Boolean(message));
    status.classList.toggle('error', level === 'error' && Boolean(message));
    status.setAttribute('aria-busy', String(Boolean(busy)));
  }

  function validationError(file) {
    if (!String(file?.name || '').toLowerCase().endsWith('.docx')) {
      return '前次會議紀錄僅支援 Word .docx 格式。';
    }
    const limit = Number(runtimeConfig.previous_minutes_max_bytes) || 20 * 1024 * 1024;
    if (Number(file?.size || 0) > limit) {
      return `前次會議紀錄過大，上限 ${formatBytes(limit)}。`;
    }
    return '';
  }

  async function start(meetingId, file) {
    if (!meetingId || !file) return;
    const error = validationError(file);
    if (error) {
      setStatus(error, 'error');
      alert(error);
      return;
    }
    if (!confirm(
      `確定要補上「${file.name}」並重產嗎？\n`
      + '系統會沿用目前逐字稿、執行第二模型查核並建立一筆新紀錄；原紀錄保持不變。'
    )) {
      setStatus('已取消補上前次會議紀錄。');
      return;
    }

    const button = document.getElementById('previous-minutes-rerun-button');
    const originalButtonText = button?.innerHTML;
    setStatus(`正在保存並建立重產任務：${file.name}`, 'info', true);
    if (button) {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.innerHTML = '<span class="loading-spinner"></span> 建立中';
    }

    const formData = new FormData();
    formData.append('previous_minutes_file', file);
    formData.append('high_quality', 'true');
    try {
      const response = await fetch(`${API}/meetings/${meetingId}/previous-minutes-rerun`, {
        method: 'POST',
        body: formData,
      });
      if (!response.ok) throw new Error(await apiErrorMessage(response));
      const result = await response.json();
      await loadJobs();
      await loadMetrics();
      setDetailStatus(result.message);
      setStatus(`已建立重產任務：${result.job_id}`, 'success');
      alert(`${result.message}\n任務：${result.job_id}\n可在「任務狀態」查看進度。`);
    } catch (requestError) {
      setStatus(`補上前次會議紀錄失敗：${requestError.message}`, 'error');
      setDetailStatus(`補上前次會議紀錄失敗：${requestError.message}`);
      alert(`補上前次會議紀錄失敗：${requestError.message}`);
    } finally {
      if (button) {
        button.disabled = false;
        button.setAttribute('aria-busy', 'false');
        button.innerHTML = originalButtonText || '📄 補前次重產';
      }
    }
  }

  function renderLineage(meeting) {
    const regeneration = meeting?.quality_report?.regeneration;
    if (regeneration?.relation !== 'regenerated_with_previous_minutes') return '';
    const sourceId = Number(regeneration.source_meeting_id);
    if (!Number.isInteger(sourceId) || sourceId <= 0) return '';
    const transcriptSha = String(regeneration.source_transcript_sha256 || '');
    const title = transcriptSha
      ? `沿用會議 #${sourceId} 的逐字稿；SHA-256：${transcriptSha}`
      : `沿用會議 #${sourceId} 的逐字稿`;
    return `<div class="meta-chip" title="${escapeHtml(title)}"><span class="icon">↻</span>由會議 #${sourceId} 補前次重產</div>`;
  }

  window.previousMinutesRerun = { start, renderLineage };
})();
