'use strict';
const assert = require('node:assert/strict');
const reader = require('../static/minutes-reader.js');
const d = {id:'D1',topic:'安全測試',summary:'摘要',review_status:'generated'};
const r = {id:'R1',decision:'<img src=x onerror=alert(1)>',related_discussions:['D1','D2'],status:'pending',basis:'測試通過才執行'};
const a = {id:'A1',task:'A & B <script>bad</script>',owner:'發言者 A',due:'未提及',priority:'高',related_discussions:[],related_decisions:['R1'],source_timecodes:['01:02:03'],confirmation_required:['due']};
const view = {available:true,discussions:[d,{...d,id:'D2'}],decisions:[r],actions:[a],unassigned:['A1'],prefix_markdown:'',appendix_markdown:'',review_status:'generated'};
const data = reader.model(view);
assert.equal(data.items.size,4);
assert.equal(data.branches[0].decisions.length,1);
assert.equal(data.branches[1].decisions.length,1);
assert.equal(data.branches[0].actions.length,0); // R1 membership must not imply A1 -> D1.
assert.deepEqual(data.unassigned.map(i=>i.id),['A1']);
assert.equal(reader.seconds('01:02:03'),3723);
assert.equal(reader.seconds('13:20'),800);
assert.equal(reader.seconds('1:99'),null);
assert(reader.risks(r).includes('決議待確認'));
assert(reader.risks(a).includes('負責人姓名待確認'));
assert(reader.risks(a).includes('期限待確認'));
assert(reader.risks({...a,owner:''}).includes('負責人待確認'));
assert(reader.risks({...d,review_ranges:[{}]}).includes('來源需複核'));
assert(!reader.risks({...r,status:'confirmed'}).includes('決議待確認'));
const html=reader.actions([a],{source_media_available:false});
assert(!html.includes('<script>')); assert(html.includes('&lt;script&gt;'));
assert(html.includes('data-mr-seek="3723" disabled')); assert(html.includes('未提及'));
const decision=reader.itemBody(r,{});
assert(decision.includes('&lt;img')); assert(!decision.includes('<img'));
assert(decision.includes('測試通過才執行')); assert(decision.includes('尚未人工複核'));
const shell=reader.render({minutes_view:view},{markdown:s=>s});
assert(shell.includes('data-mr-mode="topics" aria-pressed="true"'));
assert(shell.includes('data-mr-mode="original"'));
assert(shell.includes('每個編號只列一次'));
const fallback=reader.render({full_content:'保留原文',minutes_view:{available:false,reason:'請閱讀原文'}},{markdown:s=>s});
assert(fallback.includes('保留原文')); assert(!fallback.includes('data-mr-mode'));
// Minimal mount adapter: exercise real mode handlers without adding a DOM dependency.
const appendix = {innerHTML:'transcript',hidden:false,replaceChildren(){this.innerHTML='';}};
const prefix = {hidden:false}, body = {innerHTML:'',querySelector(){return this;}};
const drawer = {hidden:true};
const buttons = ['topics','map','actions','original'].map(mode=>({
  dataset:{mrMode:mode},setAttribute(){},hasAttribute(){return false;},closest(){return this;}
}));
const handlers = {}, enhanced = [];
const elements = {'.mr-body':body,'.mr-drawer':drawer,'.mr-appendix':appendix,'.mr-prefix':prefix};
const root = {querySelector:selector=>elements[selector],
  querySelectorAll:selector=>selector==='[data-mr-mode]' ? buttons : [],
  addEventListener:(name,fn)=>{handlers[name]=fn;},contains:()=>true};
reader.mount({querySelector:()=>root},{minutes_view:{...view,appendix_markdown:'transcript'},full_content:'original'},
  {markdown:s=>s,enhance:el=>enhanced.push(el)});
assert.equal(enhanced.length,1);
handlers.click({target:buttons[3]});
assert.equal(appendix.innerHTML,''); assert(appendix.hidden); assert(body.innerHTML.includes('original'));
assert.equal(enhanced.length,2);
handlers.click({target:buttons[0]});
assert.equal(appendix.innerHTML,'transcript'); assert(!appendix.hidden); assert.equal(enhanced.length,3);
handlers.click({target:buttons[2]});
assert.equal(enhanced.length,3); // No nested duplicate timecode enhancement.
console.log('minutes-reader model, status, media, mode lifecycle, fallback and HTML escaping contracts passed');
