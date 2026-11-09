(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let course = null, canWrite = false, selectedPart = null, uploading = false;
  const params = new URLSearchParams(location.search);
  let courseId = Number(params.get('course')) || null;
  const labels = {done:'逐字稿完成',saved:'等待送出檔案',pending:'背景等待中',processing:'正在轉錄',failed:'轉錄失敗',cancelled:'轉錄已取消'};
  const needsReview = part => part.failure?.code === 'needs_review';
  const partStatus = part => needsReview(part) ? '待核聽' :
    (part.state === 'done' && !part.has_transcript ? '缺少逐字稿' : labels[part.state] || '逐字稿尚未就緒');
  const notesFailed = c => ['failed','cancelled'].includes(c.notes_job?.status ?? c.notes_job_status);
  function notesPhase(c, completed, total) {
    if (c.notes_current && total > 0 && completed === total) return '整合筆記已完成';
    if (c.status === 'open' || !total || completed < total) return '整合筆記尚未開始';
    if (notesFailed(c)) return '整合筆記未完成';
    const jobStatus = c.notes_job?.status ?? c.notes_job_status;
    if (jobStatus === 'processing') return '整合筆記整理中';
    if (jobStatus === 'pending') return '整合筆記已排隊';
    return '整合筆記尚未產出';
  }

  function timeLabel(seconds) {
    const pad = n => String(n).padStart(2, '0');
    const minutes = Math.floor(seconds / 60);
    return `${minutes >= 60 ? `${Math.floor(minutes / 60)}:` : ''}${pad(minutes % 60)}:${pad(seconds % 60)}`;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let message;
      try { message = (await response.json()).detail; } catch (_) { message = '服務暫時無法回應'; }
      throw new Error(String(message || '操作未完成') + '；請更新狀態後再試。');
    }
    return response.json();
  }
  const json = (method, data) => ({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  function setText(element, value) { if (element && element.textContent !== value) element.textContent = value; }
  function error(err) { $('page-error').textContent = err.message || String(err); $('page-error').hidden = false; }
  async function action(operation) { $('page-error').hidden = true; try { await operation(); } catch (err) { error(err); } }

  async function loadCourses() {
    const result = await api('/training/courses');
    canWrite = result.can_write;
    $('create-section').hidden = !canWrite;
    if (!result.cloud_enabled) { $('course-mode').value = 'local'; $('course-mode').disabled = true; }
    $('course-list-status').textContent = result.courses.length ? `共 ${result.courses.length} 門課程` : '尚無課程，請先建立第一門課。';
    $('course-list').innerHTML = result.courses.map(c => `<li><button type="button" data-course="${c.id}" aria-current="${c.id===courseId}">${escape(c.title)}<span class="course-meta" data-course-progress>${c.status==='open'?'開課中':'已結課'} · ${c.completed_parts}/${c.part_count} 段逐字稿完成</span><span class="course-meta" data-course-notes>${notesPhase(c,c.completed_parts,c.part_count)}</span></button></li>`).join('');
    if (!result.courses.length && canWrite) $('create-section').open = true;
  }

  async function localRecordings() {
    const sessions = (await RecordingRecovery.listSessions()).filter(s => Number(s.trainingCourseId) === courseId);
    $('local-pending').hidden = sessions.length === 0;
    $('local-pending').textContent = `這台裝置還有 ${sessions.length} 段未確認送出。請先補送，再結束課程。`;
    $('local-recordings').innerHTML = sessions.map(s => `<div>${escape(s.title)} <button data-local-send="${escape(s.id)}" ${canWrite?'':'disabled'}>補送這一段</button><button data-local-download="${escape(s.id)}">下載備份</button></div>`).join('');
    return sessions.length;
  }

  function notesStatus(c, completed) {
    const phase = notesPhase(c, completed, c.parts.length);
    if (phase === '整合筆記已完成') return phase;
    if (!c.parts.length) return `${phase}：請先加入第一段。`;
    const missing = c.parts.filter(p => !p.has_transcript);
    if (missing.length) return `${phase}：${missing.map(p => `第 ${p.position} 段${partStatus(p)}`).join('、')}。`;
    if (c.status === 'open') return `${phase}：結課後才會整理。`;
    if (notesFailed(c)) return `${phase}：筆記整理未成功，已完成的逐字稿仍保留。`;
    if (!['processing','pending'].includes(c.notes_job?.status)) return `${phase}：逐字稿已齊，但尚無可用筆記。`;
    return phase;
  }

  function partRow(part, index) {
    const editable = canWrite && course.status === 'open';
    const progress = part.state === 'processing' && part.progress_total ? ` · 處理至第 ${part.progress_current || 0}/${part.progress_total} 個切片` : '';
    const warning = part.warnings.length
      ? `<p class="warning">來源需複核：${escape(part.warnings.join('；'))}</p>` : '';
    const play = part.source_audio ? `<button data-play="${part.id}">${part.has_transcript ? '播放／查看逐字稿' : '從頭播放'}</button>` : '';
    const issue = part.failure;
    const hasRange = Number.isInteger(issue?.start_seconds) && Number.isInteger(issue?.end_seconds)
      && issue.start_seconds >= 0 && issue.end_seconds > issue.start_seconds;
    const start = hasRange ? timeLabel(issue.start_seconds) : '';
    const failure = issue ? `<p class="part-issue">${escape(issue.reason)}${hasRange
      ? `<br>錄音內第 ${escape(issue.segment_number)} 個切片，最近補救範圍：<span class="time-range">${start}–${timeLabel(issue.end_seconds)}</span>。${part.source_audio ? '' : ' 本段沒有可供播放的來源檔。'}`
      : needsReview(part) ? ' 系統未留下可跳播的時間點。' : ''}</p>` : '';
    const review = needsReview(part) && hasRange && part.source_audio
      ? `<button class="review-jump" data-play="${part.id}" data-seek="${issue.start_seconds}" aria-label="第 ${part.position} 段，跳到 ${start} 核聽">跳到 ${start} 核聽</button>` : '';
    const retry = canWrite && !needsReview(part) && ['failed', 'cancelled'].includes(part.state)
      ? `<button data-retry="${part.job_id}">重試本段</button>` : '';
    const missing = canWrite && part.state === 'saved'
      ? `<label>補送本段原始檔<input type="file" data-part-file="${part.id}" accept="audio/*,video/*,.mkv,.wmv" aria-label="補送第 ${part.position} 段原始檔"></label>` : '';
    const move = editable ? `
      <button data-move="${part.id}" data-direction="-1" ${index === 0 ? 'disabled' : ''} aria-label="第 ${part.position} 段上移">上移</button>
      <button data-move="${part.id}" data-direction="1" ${index === course.parts.length - 1 ? 'disabled' : ''} aria-label="第 ${part.position} 段下移">下移</button>` : '';
    return `<li>
      <div class="part-main"><span class="part-title">第 ${part.position} 段 · ${escape(part.title)}</span><span class="part-state${issue?' warning':''}">${escape(partStatus(part))}</span></div>
      <span class="course-meta">${escape(part.created_at)}${progress}</span>${failure}${warning}
      <div class="part-actions">${review}${play}${retry}${move}</div>${missing}
    </li>`;
  }

  async function loadDetail() {
    if (!courseId) return;
    course = await api(`/training/courses/${courseId}`);
    document.body.classList.add('has-course'); $('choose-course').hidden = true; $('course-detail').hidden = false;
    $('detail-title').textContent = course.title;
    const count = course.parts.filter(p=>p.has_transcript).length;
    const progress = `${course.status==='open'?'開課中':'已結課'} · ${count}/${course.parts.length} 段逐字稿完成`;
    setText($('course-status'), `${progress} · ${course.processing_mode==='local'?'機密本地處理':'一般雲端處理'}`);
    const courseCard = $('course-list').querySelector(`[data-course="${course.id}"]`);
    setText(courseCard?.querySelector('[data-course-progress]'), progress);
    setText(courseCard?.querySelector('[data-course-notes]'), notesPhase(course, count, course.parts.length));
    $('record-part').hidden = !canWrite || course.status!=='open';
    $('record-part').href = `/static/index.html?training_course=${course.id}`;
    $('record-part').textContent = course.parts.length ? '繼續下一段錄製' : '開始本段錄製';
    $('add-source').hidden = !canWrite || course.status!=='open';
    $('finish-course').hidden = !canWrite;
    const retryNotes = course.status==='closed' && course.parts.length>0 && count===course.parts.length && !course.notes_current && notesFailed(course);
    $('finish-course').textContent = course.status==='open'?'結課（收齊逐字稿後整理筆記）':retryNotes?'重試課程筆記':'重新開課，加入下一段';
    setText($('notes-status'), notesStatus(course, count));
    const pending = await localRecordings();
    $('finish-course').disabled = uploading || (course.status==='open' && (!course.parts.length || pending>0));
    const rows = course.parts.map(partRow).join('');
    if ($('part-list').renderedRows !== rows) {
      $('part-list').innerHTML = rows;
      $('part-list').renderedRows = rows;
    }
    $('stale-notes').hidden = !course.notes_markdown || course.notes_current;
    if ($('course-notes').dataset.markdown !== course.notes_markdown) {
      $('course-notes').innerHTML = DOMPurify.sanitize(marked.parse(course.notes_markdown || ''));
      $('course-notes').dataset.markdown = course.notes_markdown;
    }
    $('download-notes').hidden = !course.notes_markdown;
    $('download-notes').href = `/training/courses/${course.id}/notes.md`;
    if (!selectedPart && params.get('part')) {
      const part = course.parts.find(p=>p.id===params.get('part'));
      if (part) openPart(part, Math.max(0, Number(params.get('t')) || 0));
    }
  }

  function openPart(part, seconds=0, focus=false) {
    if (!part?.source_audio) return;
    selectedPart = part;
    $('player-panel').hidden = false; $('player-error').hidden = true; $('part-transcript').hidden = true;
    $('player-title').textContent = `第 ${part.position} 段 · ${part.title}${seconds>0 ? `（核聽 ${timeLabel(Math.floor(seconds))}）` : ''}`;
    const player = $('course-player');
    const playbackError = text => { $('player-error').textContent = text; $('player-error').hidden = false; };
    player.onloadedmetadata = () => {
      if (!seconds) return;
      if (!Number.isFinite(player.duration) || seconds >= player.duration) {
        playbackError('無法定位這個時間點：媒體長度不足或尚未取得。請確認來源媒體。');
        return;
      }
      player.currentTime = seconds;
    };
    player.onerror = () => playbackError('原始媒體目前無法播放，請更新狀態或聯絡管理員確認來源。');
    player.src = `/training/courses/${course.id}/parts/${part.id}/media`;
    if (focus) $('player-title').focus({preventScroll:true});
    $('player-panel').scrollIntoView({block:'start',behavior:'smooth'});
  }

  async function choose(id) {
    courseId=id; selectedPart=null; $('player-panel').hidden=true;
    history.replaceState(null,'',`?course=${id}`);
    await loadDetail(); await loadCourses();
  }

  $('create-course').addEventListener('submit', event => { event.preventDefault(); action(async()=>{
    const c = await api('/training/courses',json('POST',{title:$('course-title').value.trim(),processing_mode:$('course-mode').value}));
    $('course-title').value=''; await choose(c.id);
  }); });
  $('course-list').addEventListener('click', event => {const b=event.target.closest('[data-course]'); if(b) action(()=>choose(Number(b.dataset.course)));});
  $('back-courses').onclick=()=>{document.body.classList.remove('has-course'); $('course-detail').hidden=true; $('choose-course').hidden=false; courseId=null; selectedPart=null; $('course-player').pause(); history.replaceState(null,'',location.pathname);};
  $('refresh-course').onclick=()=>action(async()=>{await loadDetail();await loadCourses();});
  $('finish-course').onclick=()=>action(async()=>{
    const retry = $('finish-course').textContent==='重試課程筆記';
    const target = course.status==='open'||retry?'closed':'open';
    await api(`/training/courses/${course.id}/status`,json('PUT',{status:target,version:course.version}));
    await loadDetail();await loadCourses();
  });
  $('part-list').addEventListener('click',event=>action(async()=>{
    const play=event.target.closest('[data-play]'),retry=event.target.closest('[data-retry]'),move=event.target.closest('[data-move]');
    if(play) openPart(course.parts.find(p=>p.id===play.dataset.play), Number(play.dataset.seek) || 0, true);
    if(retry) {await api(`/jobs/${retry.dataset.retry}/retry`,{method:'POST'});await loadDetail();}
    if(move) {const ids=course.parts.map(p=>p.id),i=ids.indexOf(move.dataset.move),j=i+Number(move.dataset.direction);[ids[i],ids[j]]=[ids[j],ids[i]];await api(`/training/courses/${course.id}/order`,json('PUT',{part_ids:ids,version:course.version}));await loadDetail();}
  }));
  $('part-list').addEventListener('change', event => {
    const input = event.target.closest('[data-part-file]');
    const file = input?.files?.[0];
    if (!file) return;
    action(async () => {
      const part = course.parts.find(p => p.id === input.dataset.partFile);
      if (!part) throw new Error('段落已變更，請更新狀態後再選擇檔案。');
      input.disabled = true;
      const form = new FormData();
      form.append('file', file);
      form.append('training_part_id', part.id);
      form.append('processing_mode', course.processing_mode);
      try {
        await api('/upload-media', {method: 'POST', body: form});
        $('part-status').textContent = '本段已補送，可繼續下一段。';
      } finally {
        await loadDetail();
      }
    });
  });
  $('show-transcript').onclick=()=>action(async()=>{
    const opened = selectedPart;
    if (!opened) return;
    const data = await api(`/training/courses/${course.id}/parts/${opened.id}/transcript`);
    if (selectedPart !== opened) return;
    const part = course.parts.find(p=>p.id===opened.id) || opened;
    $('part-transcript').textContent = data.transcript || `逐字稿尚未完成：${partStatus(part)}。`;
    $('part-transcript').hidden = false;
  });
  $('attach-meeting').onsubmit=event=>{event.preventDefault();action(async()=>{if(!$('meeting-source').value)throw new Error('請先選擇一份既有紀錄。');await api(`/training/courses/${course.id}/meetings`,json('POST',{meeting_id:Number($('meeting-source').value)}));await loadDetail();await loadCourses();});};
  $('upload-parts').onsubmit=event=>{event.preventDefault();if(uploading)return;action(async()=>{
    uploading=true; const target={...course},files=Array.from($('part-files').files);$('upload-parts').querySelector('button').disabled=true;
    try {for(const file of files){$('part-status').textContent=`正在保存 ${file.name}…`;await RecordingRecovery.begin({title:file.name,trainingCourseId:target.id,trainingClientId:crypto.randomUUID(),processingMode:target.processing_mode,mimeType:file.type});RecordingRecovery.captureChunk(file,0);const saved=await RecordingRecovery.finish({title:file.name});if(!saved||saved.error||saved.bytes!==file.size)throw new Error('檔案尚未保存，請保留原始檔並重新選擇上傳。');RecordingRecovery.detachActive();RecordingRecovery.queueUpload(saved.id).then(()=>loadDetail()).catch(error); } $('part-status').textContent='檔案已保存並開始背景上傳，可繼續錄製下一段；上傳完成前請保持此頁開啟。';$('part-files').value='';}
    finally {uploading=false;$('upload-parts').querySelector('button').disabled=false;await loadDetail();await loadCourses();}
  });};
  $('local-recordings').onclick=event=>{const button=event.target.closest('[data-local-send]'),download=event.target.closest('[data-local-download]');if(button)action(async()=>{button.disabled=true;await RecordingRecovery.restart(button.dataset.localSend);await loadDetail();});if(download)action(()=>RecordingRecovery.download(download.dataset.localDownload));};
  action(async()=>{
    await loadCourses(); if(courseId) await loadDetail();
    const data=await api('/meetings?limit=200'); const records=Array.isArray(data)?data:data.records||[];
    $('meeting-source').innerHTML='<option value="">請選擇</option>'+records.map(m=>`<option value="${m.id}">#${m.id} ${escape(m.title)}</option>`).join('');
  });
  setInterval(()=>{if(courseId&&!document.hidden&&!uploading)action(loadDetail);},8000);
})();
