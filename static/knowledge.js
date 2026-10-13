(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let caps = {};
  const statusLabels = {approved:'已核准', reviewed:'已複核', needs_review:'待複核', generated:'AI 草稿', proposed:'待核准', disabled:'已停用'};
  const text = (tag, value, className) => { const el = document.createElement(tag); el.textContent = value; if(className) el.className=className; return el; };
  async function request(url, options={}) {
    const response = await fetch(url, {credentials:'same-origin', ...options});
    const data = await response.json();
    if(!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作未完成，請重新整理後重試。');
    return data;
  }
  function error(message) { $('page-error').textContent=message; $('page-error').hidden=!message; }
  function show(page) {
    for(const id of ['ask','vocabulary','recording']) $(id+'-page').hidden=id!==page;
    document.querySelectorAll('[data-page]').forEach(button => button.setAttribute('aria-current', button.dataset.page===page?'page':'false'));
    history.replaceState(null,'','#'+page); error('');
  }
  document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click',()=>show(button.dataset.page)));
  show(['ask','vocabulary','recording'].includes(location.hash.slice(1))?location.hash.slice(1):'ask');
  function renderEvidence(data) {
    $('evidence').replaceChildren(); $('statements').replaceChildren();
    for(const statement of data.statements || []) {
      const p=text('blockquote', `${statement.quote} [${statement.evidence_id}]`); $('statements').append(p);
    }
    for(const item of data.evidence) {
      const card=text('article','', 'evidence-card'); const link=text('a',`${item.id} · ${item.title}`);
      link.href='/static/index.html?meeting='+encodeURIComponent(item.meeting_id)+(item.seconds!==null?'&t='+encodeURIComponent(item.seconds):'');
      card.append(text('h3','')); card.firstChild.append(link);
      const timestamp=item.seconds===null?'':` · ${Math.floor(item.seconds/60)}:${String(item.seconds%60).padStart(2,'0')}`;
      card.append(text('div',`${item.date} · ${statusLabels[item.review_status]||'未確認'}${timestamp}${item.local_only?' · 本地':''}`,'meta'));
      card.append(text('blockquote',item.quote));
      const details=text('details',''); details.append(text('summary','來源版本'),text('div',item.source_sha256,'meta')); card.append(details);
      $('evidence').append(card);
    }
  }
  $('question-form').addEventListener('submit', async event => {
    event.preventDefault(); error(''); $('ask-button').disabled=true; $('answer-status').textContent='正在查找來源…';
    $('evidence').replaceChildren(); $('statements').replaceChildren();
    try {
      const data=await request('/knowledge/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        question:$('question').value.trim(),approved_only:$('approved-only').checked,use_cloud:$('use-cloud').checked,
        meeting_ids:Array.from(document.querySelectorAll('#meeting-scope input:checked'),input=>Number(input.value))})});
      $('answer-status').textContent=data.answer; renderEvidence(data);
    } catch(exc) { error(exc.message); $('answer-status').textContent='查詢未完成，請重試。'; }
    finally { $('ask-button').disabled=false; }
  });
  async function refreshTerms() {
    $('refresh-terms').disabled=true;
    try {
      const data=await request('/knowledge/vocabulary'); $('terms').replaceChildren();
      $('term-status').textContent=data.items.length?`${data.items.length} 個詞彙；${data.items.filter(item=>item.status==='approved').length} 個已核准。`:'尚無共用詞彙，可先提出第一筆修正。';
      for(const item of data.items) {
        const card=text('article','', 'term-card'), body=text('div','');
        body.append(text('strong',item.canonical),text('div',`${statusLabels[item.status]} · v${item.version}${item.local_only?' · 僅本地使用':''}`,'meta'));
        if(item.aliases.length) body.append(text('div','誤聽：'+item.aliases.join('、')));
        if(item.note) body.append(text('div',item.note,'subtle'));
        card.append(body);
        if(caps.can_approve) {
          const action=item.status==='approved'?'disabled':'approved';
          const button=text('button',action==='approved'?'核准':'停用'); button.type='button';
          button.addEventListener('click',()=>review(item,action,button)); card.append(button);
        }
        $('terms').append(card);
      }
    } catch(exc) { error(exc.message); $('term-status').textContent='詞彙載入未完成，請重新整理。'; }
    finally { $('refresh-terms').disabled=false; }
  }
  async function review(item, status, button) {
    $('review-title').textContent=status==='approved'?'核准共用詞彙':'停用共用詞彙';
    $('review-impact').textContent=`${item.canonical}：${status==='approved'?'新任務將使用此詞彙，並替換完全匹配的誤聽詞。請避免需依語境判斷的對照。':'後續新任務將停止使用這筆詞彙。'}已排入的任務保留原詞庫版本。`;
    const dialog=$('review-dialog'); dialog.returnValue='cancel'; dialog.showModal();
    const confirmed=await new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true}));
    if(!confirmed)return;
    button.disabled=true; error('');
    try { await request('/knowledge/vocabulary/'+item.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,version:item.version})}); await refreshTerms(); }
    catch(exc) { error(exc.message); button.disabled=false; }
  }
  $('term-form').addEventListener('submit',async event=>{
    event.preventDefault(); error(''); $('propose-button').disabled=true;
    try {
      await request('/knowledge/vocabulary',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        canonical:$('canonical').value,aliases:$('aliases').value.split(/[,，\n]/).map(s=>s.trim()).filter(Boolean),note:$('term-note').value,meeting_id:Number($('term-meeting').value)||null})});
      $('term-form').reset(); await refreshTerms(); $('term-status').textContent='修正已提出，管理員核准後供新任務使用。';
    } catch(exc) { error(exc.message); }
    finally { $('propose-button').disabled=!caps.can_propose; }
  });
  $('refresh-terms').addEventListener('click',()=>{error('');refreshTerms();});
  async function initialize() {
    try {
      caps=await request('/knowledge/capabilities'); $('local-capability').textContent=caps.message;
      $('use-cloud').disabled=!caps.cloud_enabled; $('propose-button').disabled=!caps.can_propose;
      $('cloud-note').hidden=caps.cloud_enabled; $('cloud-note').textContent='此服務已設定禁止外送，查詢會直接提供本地來源原文。';
      if(!caps.can_propose){$('term-form').hidden=true;}
      const meetingData=await request('/meetings?limit=100'),meetings=meetingData.records; $('meeting-scope').replaceChildren();
      if(meetingData.total>meetings.length)$('meeting-scope').append(text('p',`可選最近 ${meetings.length} 筆；不勾選時搜尋全部 ${meetingData.total} 筆。`,'subtle'));
      for(const item of meetings) {
        const label=text('label',''), input=document.createElement('input');input.type='checkbox';input.value=item.id;
        label.append(input,document.createTextNode(' '+item.title));$('meeting-scope').append(label);
        const option=text('option',item.title);option.value=item.id;$('term-meeting').append(option);
      }
      if(!meetings.length)$('meeting-scope').textContent='尚無會議，請先回會議紀錄上傳。';
      await refreshTerms();
    } catch(exc) { error(exc.message); $('meeting-scope').textContent='請重新整理頁面以載入會議。';$('local-capability').textContent='狀態讀取失敗，請重新整理。'; }
  }
  initialize();
})();
