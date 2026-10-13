/* Read-only topic reader. One server snapshot; no AI, review mutation or inferred edges. */
(function (root) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const list = value => Array.isArray(value) ? value : [];
  const text = value => (Array.isArray(value) ? value.join('；') : String(value ?? '')).trim() || '未提及';
  const reviewLabels = {generated:'尚未人工複核',needs_review:'待重新複核',reviewed:'已人工複核',approved:'已核准'};
  const kinds = {D:'討論摘要',R:'決議',A:'待辦'};
  const modes = {topics:'議題式整合',map:'議題總覽圖',actions:'待辦總覽',original:'原文'};

  function model(view) {
    const ds = list(view.discussions), rs = list(view.decisions), aa = list(view.actions);
    const topicIds = new Set(ds.map(d => d.id));
    return {branches:ds.map(d => ({discussion:d, decisions:rs.filter(r => list(r.related_discussions).includes(d.id)),
      actions:aa.filter(a => list(a.related_discussions).includes(d.id))})),
    unassigned:[...rs,...aa].filter(i => !list(i.related_discussions).some(id => topicIds.has(id))),
    items:new Map([...ds,...rs,...aa].map(i => [i.id,i]))};
  }
  function seconds(code) {
    if (!/^\d{1,3}:[0-5]\d(?::[0-5]\d)?$/.test(String(code))) return null;
    return String(code).split(':').reduce((total,n) => total*60+Number(n),0);
  }
  function risks(item) {
    const result = [];
    if (item.id.startsWith('R') && item.status !== 'confirmed') result.push('決議待確認');
    if (list(item.review_ranges).length) result.push('來源需複核');
    if (list(item.confirmation_required).length) result.push('欄位待確認');
    if (item.id.startsWith('A') && ['未提及','待確認','不確定'].includes(text(item.owner))) result.push('負責人待確認');
    if (item.id.startsWith('A') && ['未提及','待確認','不確定'].includes(text(item.due))) result.push('期限待確認');
    if (item.id.startsWith('A') && /^(發言者|Speaker)\s*[A-Z\d]+$/i.test(text(item.owner))) result.push('負責人姓名待確認');
    return result;
  }
  function badges(item) {
    return risks(item).map(r => `<span class="mr-risk">${esc(r)}</span>`).join('');
  }
  function status(item) {
    const source = item.id.startsWith('R') ? `決議：${item.status === 'confirmed' ? '原稿已確認' : item.status === 'pending' ? '待確認' : text(item.status)}；` : '';
    return `<p class="mr-meta">${esc(source)}人工審查：${esc(reviewLabels[item.review_status] || '尚未人工複核')}</p>`;
  }
  function evidence(item, meeting) {
    const codes = list(item.id.startsWith('A') ? item.source_timecodes : item.evidence_timecodes);
    return `<div class="mr-evidence">佐證：${codes.length ? codes.map(code => {
      const time = seconds(code);
      return time === null ? esc(code) : `<button type="button" data-mr-seek="${time}" ${meeting.source_media_available === false ? 'disabled title="原始媒體不存在，請先補回原始檔"' : `title="定位 ${esc(code)} 原始媒體"`}>${esc(code)}</button>`;
    }).join(' ') : '原稿未列明時間'}${meeting.source_media_available === false ? '<span>原始媒體不存在，請先於播放器區補回原始檔。</span>' : ''}</div>`;
  }
  function fields(item, pairs) {
    return pairs.map(([label,key]) => `<p><strong>${label}：</strong>${esc(text(item[key]))}</p>`).join('');
  }
  function itemBody(item, meeting) {
    const kind = item.id[0];
    const pairs = kind === 'D' ? [['討論摘要','summary'],['背景','context'],['重點','key_points'],['影響／風險','impact'],['待釐清','open_questions']]
      : kind === 'R' ? [['決議','decision'],['依據／條件','basis'],['關聯議題','related_discussions']]
      : [['任務','task'],['負責人','owner'],['期限（原文）','due'],['優先級','priority'],['關聯議題','related_discussions'],['關聯決議','related_decisions']];
    return badges(item)+status(item)+fields(item,pairs)+evidence(item,meeting);
  }
  function actions(items, meeting) {
    if (!items.length) return '<p class="mr-meta">原稿未列明此議題的待辦；不代表無需後續行動。</p>';
    return `<table class="mr-actions"><thead><tr><th>待辦及關聯</th><th>負責人</th><th>期限（原文）</th><th>優先級</th></tr></thead><tbody>${items.map(a => `<tr>
      <td data-label="待辦"><strong>${esc(a.id)} ${esc(a.task)}</strong>${badges(a)}${status(a)}<p class="mr-meta">議題 ${esc(text(a.related_discussions))} · 決議 ${esc(text(a.related_decisions))}</p>${evidence(a,meeting)}</td>
      <td data-label="負責人">${esc(text(a.owner))}</td><td data-label="期限">${esc(text(a.due))}</td><td data-label="優先級">${esc(text(a.priority))}</td></tr>`).join('')}</tbody></table>`;
  }
  function decisions(items, meeting) {
    return items.length ? items.map(r => `<section class="mr-decision"><h4>${esc(r.id)} 決議</h4>${itemBody(r,meeting)}</section>`).join('') : '<p class="mr-meta">原稿未列明此議題的決議。</p>';
  }
  function unassigned(items, meeting) {
    if (!items.length) return '';
    return `<section class="mr-unassigned"><h3>未明確歸屬議題</h3><p class="mr-meta">請核對原文中的關聯；以下項目保留，不推測歸屬。</p>${decisions(items.filter(i=>i.id[0]==='R'),meeting)}${actions(items.filter(i=>i.id[0]==='A'),meeting)}</section>`;
  }
  function topics(data, meeting) {
    return `<nav class="mr-jump" aria-label="議題跳轉">${data.branches.map(b=>`<button type="button" data-mr-topic="${esc(b.discussion.id)}">${esc(b.discussion.id+' '+b.discussion.topic)}</button>`).join('')}</nav>` + data.branches.map(b => {
      const d = b.discussion;
      return `<article class="mr-topic" id="minutes-topic-${esc(d.id)}" tabindex="-1"><h3>${esc(d.id)} ${esc(d.topic)}</h3>${badges(d)}<p>${esc(text(d.summary))}</p>
        <p><strong>影響／風險：</strong>${esc(text(d.impact))}</p>${evidence(d,meeting)}
        <details><summary>背景與討論重點</summary>${fields(d,[['背景','context'],['重點','key_points'],['待釐清','open_questions']])}${status(d)}</details>
        <h4>決議</h4>${decisions(b.decisions,meeting)}<h4>後續行動</h4>${actions(b.actions,meeting)}</article>`;
    }).join('') + unassigned(data.unassigned,meeting);
  }
  function node(item) {
    return `<button type="button" class="mr-node mr-kind-${item.id[0]}" data-mr-item="${esc(item.id)}" aria-controls="minutes-item-detail" aria-expanded="false"><span>${esc(item.id)} · ${kinds[item.id[0]]}</span><strong>${esc(item.topic || item.decision || item.task)}</strong>${badges(item)}</button>`;
  }
  function map(data, meeting) {
    return `<p class="mr-meta">點選節點查看全文；連線僅表示議題歸屬，不代表因果或執行順序。</p>
      <div class="mr-tools"><button type="button" data-mr-expand="true">展開全部分支</button><button type="button" data-mr-expand="false">收合全部分支</button></div>
      <div class="mr-map"><div class="mr-root">${esc(meeting.title)}</div><div class="mr-branches">${data.branches.map(b => {
        const children = [...b.decisions,...b.actions];
        const warnings = [...new Set(children.flatMap(risks))];
        return `<div class="mr-branch"><div>${node(b.discussion)}<p class="mr-meta mr-warning">${esc(warnings.join(' · '))}</p></div>${children.length ? `<details open><summary>${esc(b.discussion.id)} 分支 · ${b.decisions.length} 決議／${b.actions.length} 待辦</summary><div class="mr-leaves">${children.map(node).join('')}</div></details>` : '<p class="mr-meta">原稿未列決議或待辦，可點議題閱讀全文。</p>'}</div>`;
      }).join('')}</div></div>${data.unassigned.length ? `<section class="mr-unassigned"><h3>未明確歸屬議題</h3>${data.unassigned.map(node).join('')}</section>` : ''}`;
  }
  function render(meeting, options) {
    const view = meeting.minutes_view;
    if (!view?.available) return `<div class="minutes-reader"><p class="mr-meta">${esc(view?.reason || '請閱讀原文；這份紀錄尚無可安全整合的議題資料。')}</p><div class="md-content">${options.markdown(meeting.full_content || '')}</div></div>`;
    return `<section class="minutes-reader" aria-label="會議紀錄閱讀器"><nav class="mr-modes" aria-label="會議閱讀方式">${Object.entries(modes).map(([key,label])=>`<button type="button" data-mr-mode="${key}" aria-pressed="${key==='topics'}">${label}</button>`).join('')}</nav>
      <p class="mr-meta">${view.discussions.length} 個議題 · ${view.decisions.length} 項決議 · ${view.actions.length} 項待辦 · ${esc(reviewLabels[view.review_status] || '待人工確認')}</p>
      <details class="mr-help"><summary>閱讀與確認說明</summary><p>各模式共用同一份資料。決議「原稿已確認」不等於人工核准；跨議題項目沿用相同編號，待辦總覽每個編號只列一次，不自動合併任務。背景、佐證與審查操作仍保留。</p><p>議題內容${view.source === 'structured_summary' ? '含結構化資料中的人工欄位確認' : '由原文規則式解析'}；「原文」可查看原始文件。切換閱讀模式不會修改或核准紀錄。</p></details>
      <div class="mr-prefix md-content">${options.markdown(view.prefix_markdown.replace(/^---\s*\n[\s\S]*?\n---\s*\n?/,''))}</div>
      <div class="mr-body"></div><div class="mr-appendix md-content">${options.markdown(view.appendix_markdown)}</div>
      <aside id="minutes-item-detail" class="mr-drawer" aria-label="議題節點明細" hidden><div class="mr-drawer-head"><h3 tabindex="-1"></h3><button type="button" data-mr-close>關閉明細</button></div><div class="mr-drawer-body"></div></aside></section>`;
  }
  function mount(host, meeting, options) {
    const el = host.querySelector('.minutes-reader');
    if (!el) return;
    if (!meeting.minutes_view?.available) { options.enhance(el.querySelector('.md-content')); return; }
    const view = meeting.minutes_view, data = model(view), body = el.querySelector('.mr-body');
    const drawer = el.querySelector('.mr-drawer');
    let trigger = null;
    function close(restore = true) {
      drawer.hidden = true;
      el.querySelectorAll('[data-mr-item]').forEach(b=>b.setAttribute('aria-expanded','false'));
      if (restore && trigger?.isConnected) trigger.focus({preventScroll:true});
    }
    function select(mode) {
      close(false);
      el.querySelectorAll('[data-mr-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mrMode===mode)));
      const appendix = el.querySelector('.mr-appendix');
      if (mode==='original') {
        appendix.hidden = true;
        appendix.replaceChildren(); // Global player highlighting must see only one transcript.
      }
      el.querySelector('.mr-prefix').hidden = mode==='original';
      body.innerHTML = mode==='map' ? map(data,meeting) : mode==='actions' ? `<h3>全會議待辦總覽</h3><p class="mr-meta">每個 A 編號列一次；不自動合併或判定完成。</p>${actions(view.actions,meeting)}`
        : mode==='original' ? `<div class="md-content">${options.markdown(meeting.full_content || '')}</div>` : topics(data,meeting);
      if (mode==='original') options.enhance(body.querySelector('.md-content'));
      else if (appendix.hidden) {
        appendix.hidden = false;
        appendix.innerHTML = options.markdown(view.appendix_markdown);
        options.enhance(appendix);
      }
    }
    el.addEventListener('click', event=>{
      const button = event.target.closest('button');
      if (!button || !el.contains(button)) return;
      if (button.dataset.mrMode) select(button.dataset.mrMode);
      if (button.hasAttribute('data-mr-close')) close();
      if (button.dataset.mrSeek !== undefined) { close(false); options.seek(Number(button.dataset.mrSeek)); }
      if (button.dataset.mrExpand !== undefined) { close(false); body.querySelectorAll('.mr-branch details').forEach(d=>{d.open=button.dataset.mrExpand==='true';}); }
      if (button.dataset.mrTopic) {
        const id = button.dataset.mrTopic; select('topics');
        const topic = el.querySelector('#minutes-topic-'+id);
        topic?.focus({preventScroll:true}); topic?.scrollIntoView({block:'start'});
      }
      if (button.dataset.mrItem) {
        const item = data.items.get(button.dataset.mrItem); if (!item) return;
        close(false); trigger=button; button.setAttribute('aria-expanded','true');
        drawer.querySelector('h3').textContent = item.id+' · '+kinds[item.id[0]]+'明細';
        const refs = item.id[0]==='D' ? [item.id] : list(item.related_discussions);
        drawer.querySelector('.mr-drawer-body').innerHTML = itemBody(item,meeting)+refs.filter(id=>data.items.has(id) && id[0]==='D').map(id=>`<button type="button" data-mr-topic="${esc(id)}">前往 ${esc(id)} 議題全文</button>`).join('');
        drawer.hidden=false; drawer.querySelector('.mr-drawer-body').scrollTop=0;
        drawer.querySelector('h3').focus({preventScroll:true});
      }
    });
    el.addEventListener('keydown',event=>{if(event.key==='Escape' && !drawer.hidden){event.preventDefault();event.stopPropagation();close();}});
    options.enhance(el.querySelector('.mr-appendix'));
    select('topics');
  }
  const api = {render,mount,model,seconds,risks,actions,itemBody};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.MinutesReader = api;
})(typeof window !== 'undefined' ? window : globalThis);
