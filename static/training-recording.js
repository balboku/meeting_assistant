/* Reuse the existing capture UI; only course routing and save/continue differ. */
window.TrainingRecording = (() => {
  'use strict';
  const courseId = Number(new URLSearchParams(location.search).get('training_course'));
  let course = null, clientId = null;
  const $ = id => document.getElementById(id);

  function banner() {
    let node = $('training-recording-context');
    if (!node) {
      node = document.createElement('div');node.id='training-recording-context';node.className='form-group';
      const title=document.createElement('p');title.id='training-recording-title';
      const status=document.createElement('p');status.id='training-recording-status';status.setAttribute('role','status');
      const link=document.createElement('a');link.href=`/static/training.html?course=${courseId}`;link.textContent='返回課程段落與筆記';link.style.color='var(--blue)';
      const help=document.createElement('p');help.className='form-hint';help.textContent='休息時保存本段，後續可繼續錄製；上傳完成前請保持此頁開啟。';
      node.append(title,status,help,link);$('rec-title').closest('.form-group').before(node);
    }
    return node;
  }

  async function load() {
    const response=await fetch(`/training/courses/${courseId}`);
    if(!response.ok)throw new Error('課程目前無法開啟，請返回課程清單更新後再錄製。');
    course=await response.json();
    if(course.status!=='open')throw new Error('課程已結束，請返回課程按「重新開課」後再錄製。');
    return course;
  }

  async function opened() {
    if(!courseId)return;
    banner();$('btn-rec-start').disabled=true;
    try {
      await load();$('training-recording-title').textContent=`課程：${course.title}`;
      const sessions=await RecordingRecovery.listSessions().catch(()=>[]);
      const pending=sessions.filter(s=>Number(s.trainingCourseId)===courseId&&!course.parts.some(p=>p.client_id===(s.trainingClientId||s.id))).length;
      $('rec-title').value=`第 ${course.parts.length+pending+1} 段`;
      document.querySelector('label[for="rec-title"]').textContent='本段名稱（選填）';
      $('record-modal-title').textContent='教育訓練課程錄製';
      $('rec-title').placeholder='例如：上午第一節';
      $('rec-processing-mode').options[0].textContent='一般：雲端轉錄與課程筆記';
      $('rec-processing-mode').options[1].textContent='機密：本地逐字稿，筆記人工整理';
      const mode=$('rec-processing-mode');if(mode){mode.value=course.processing_mode;mode.disabled=true;}
      $('btn-rec-stop').textContent='休息並保存本段';
      $('btn-rec-start').textContent=course.parts.length?'繼續下一段錄製':'開始本段錄製';
      $('btn-rec-start').disabled=false;
      $('training-recording-status').textContent='本段保存後可立即續錄，AI 會在背景處理。';
    }catch(error){$('training-recording-status').textContent=error.message;}
  }

  function prepare() {
    if(!courseId)return;
    if(!course || course.status!=='open')throw new Error('請返回課程確認開課狀態後再錄製。');
    clientId=crypto.randomUUID();
    const mode=$('rec-processing-mode');if(mode)mode.value=course.processing_mode;
  }

  async function saved(session, blob) {
    if(!courseId)return false;
    banner();
    if(!session || session.error || Number(session.bytes)!==blob.size) {
      $('training-recording-status').textContent='本段尚未完整保存到本機。請先下載備份，再返回課程上傳。';
      let link=$('training-recording-backup');
      if(!link){link=document.createElement('a');link.id='training-recording-backup';banner().append(link);}
      if(link.href.startsWith('blob:'))URL.revokeObjectURL(link.href);
      link.href=URL.createObjectURL(blob);link.download=`course-${courseId}-${Date.now()}.webm`;link.textContent='下載本段錄製備份';
      $('rec-close-btn').disabled=false;
      return true;
    }
    if(!blob.size)throw new Error('本段沒有取得錄製資料，請確認收音後再試。');
    RecordingRecovery.detachActive();
    resetRecorder();await opened();
    $('training-recording-status').textContent='本段已保存到這台裝置，正在背景上傳；可開始下一段。';
    RecordingRecovery.queueUpload(session.id).then(()=>{
      if(!mediaRecorder || mediaRecorder.state==='inactive')$('training-recording-status').textContent='上一段已送出並排入轉錄，可繼續下一段。';
    }).catch(error=>{
      $('training-recording-status').textContent=`上一段尚未送出：${error.message}。可在下方「可恢復的中斷錄製」補送。`;
    });
    return true;
  }

  if(courseId)opened().then(()=>openRecordModal());
  return {opened,prepare,saved,metadata:()=>courseId?{trainingCourseId:courseId,trainingClientId:clientId}: {}};
})();
