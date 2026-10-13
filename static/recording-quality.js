/* Source-specific monitoring and explicit, persistent processing policy. */
window.MeetingQuality = (() => {
  let context=null, timer=null, interrupted=false, recordedMode=null;
  function mode(recording=false) { return (recording && recordedMode) || document.getElementById(recording?'rec-processing-mode':'upload-processing-mode')?.value || 'cloud'; }
  function controls() {
    for(const [inputId,id] of [['custom-vocabulary','upload-processing-mode'],['rec-vocabulary','rec-processing-mode']]) {
      const anchor=document.getElementById(inputId)?.closest('.form-group'); if(!anchor)continue;
      const group=document.createElement('div');group.className='form-group';
      const label=document.createElement('label');label.className='form-label';label.htmlFor=id;label.textContent='資料處理方式';
      const select=document.createElement('select');select.className='form-input';select.id=id;
      for(const [value,caption] of [['cloud','一般：雲端轉錄與摘要'],['local','機密：本地逐字稿，摘要人工整理']]) {
        const option=document.createElement('option');option.value=value;option.textContent=caption;select.append(option);
      }
      group.append(label,select);anchor.after(group);
    }
  }
  async function configurePolicy() {
    try {
      const response=await fetch('/knowledge/capabilities');if(!response.ok)return;
      const caps=await response.json();
      for(const id of ['upload-processing-mode','rec-processing-mode']) {
        const select=document.getElementById(id);if(!select)continue;
        select.options[0].disabled=!caps.cloud_enabled;
        select.options[1].disabled=!caps.local_transcription;
        if(!caps.cloud_enabled&&!select.disabled)select.value='local';
        const note=document.createElement('p');note.className='form-hint';
        note.textContent=!caps.local_transcription?`${caps.cloud_enabled?'':'此服務禁止外送；'}本地模型尚未就緒，機密錄音請先保留，待管理員安裝後上傳。`:!caps.cloud_enabled?'此服務禁止外送，只使用本地轉錄。':'機密模式不外送；本地逐字稿仍需人工整理摘要。';
        select.after(note);
      }
    }catch{ /* Server validates again on submit; preserve recoverable recordings. */ }
  }
  async function stop() { if(timer)clearInterval(timer);timer=null; if(context){await context.close().catch(()=>{});context=null;} }
  async function monitor(stream) {
    await stop(); interrupted=false;
    let indicator=document.getElementById('system-audio-status');
    if(!indicator){indicator=document.createElement('p');indicator.id='system-audio-status';indicator.setAttribute('role','status');document.getElementById('rec-status').after(indicator);}
    context=new (window.AudioContext||window.webkitAudioContext)();
    const analyser=context.createAnalyser();analyser.fftSize=2048;
    context.createMediaStreamSource(new MediaStream(stream.getAudioTracks())).connect(analyser);
    await context.resume();const samples=new Float32Array(analyser.fftSize);let lastSignal=Date.now(),heard=false;
    for(const track of stream.getAudioTracks())track.addEventListener('ended',()=>{interrupted=true;},{once:true});
    timer=setInterval(()=>{
      analyser.getFloatTimeDomainData(samples); const peak=samples.reduce((value,sample)=>Math.max(value,Math.abs(sample)),0);
      if(peak>.005){lastSignal=Date.now();heard=true;}
      indicator.textContent=interrupted?'系統音訊已中斷，請停止並重新分享音訊。':
        !heard?'等待同仁發言：請確認系統音訊有聲音。':Date.now()-lastSignal>30000?'系統音訊已安靜 30 秒；若同仁正在說話，請檢查分享來源。':`系統音訊：已收到聲音（音量 ${Math.round(peak*100)}%）`;
    },500);
  }
  controls();
  configurePolicy();
  return {mode,monitor,stop,warning:()=>interrupted?'音訊品質警示：系統音訊曾中斷，請檢查錄製完整性。':'',
    lock:locked=>{const select=document.getElementById('rec-processing-mode');if(select){if(locked){recordedMode=select.value;interrupted=false;}select.disabled=locked;}},
    openCitation:async()=>{
      const params=new URLSearchParams(location.search), id=Number(params.get('meeting')),seconds=Number(params.get('t'));
      if(Number.isInteger(id)&&id>0){await openDetail(id);if(params.has('t')&&Number.isFinite(seconds)&&seconds>=0)cueSourceMedia(seconds);}
    }};
})();
