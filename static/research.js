'use strict';

const batchLabels={scanning:'读取账号作品',review_ready:'等待确认清单',scan_failed:'账号读取需处理',preparing:'下载与转写',partial_ready:'部分作品需处理',awaiting_ai:'等待 AI 研究',analyzing:'研究进行中',completed:'已完成',partial:'部分完成',cancelled:'已取消',interrupted:'已中断',skipped:'本次未选择'};
const batchModeLabels={research:'首次账号研究',breakdown:'批量作品拆解',transcribe:'批量转写',download:'批量下载',comments:'批量采集评论',refresh:'批量刷新账号'};
Object.assign(pageMeta,{batch:['首次账号研究','确认代表作品，一次处理，分别保存结果。'],batches:['首次研究','每个账号的作品、拆解与研究结果集中查看。']});

async function createResearch(mode,secUids=[],ids=null,refreshSource=false){
 const scope=state.scope;
 const result=await api('/api/research/create',bodyScope({mode,sec_uids:secUids,...(ids?{aweme_ids:ids}:{}),sample_count:6,refresh:refreshSource}));
 if(scope!==state.scope)return;
 if($('#modal').open)$('#modal').close();
 state.batchComments=false;state.batch=result;location.hash='#batch/'+result.id;await refresh();
}
function batchCards(){return (state.data.batches||[]).map(b=>`<article class="report-row"><div><span class="pill">${esc(batchModeLabels[b.mode])}</span><h3>${b.account_count} 个账号 · ${b.selected_count} 条作品</h3><small>${date(b.created_at)} · ${esc(b.message)}</small></div><div class="actions"><span class="pill ${['partial','partial_ready','interrupted'].includes(b.status)?'waiting':''}">${esc(batchLabels[b.status])}</span><a class="button secondary small" href="#batch/${b.id}">${['completed','partial'].includes(b.status)?'查看成果':'继续处理'} →</a></div></article>`).join('');}
function researchListPage(){return `${pageHead('首次研究','从确认样本到逐条拆解，成果按账号分别保存。','<a class="button" href="#accounts">选择对标账号</a>')}<div id="research-batch-list" class="report-list">${batchCards()||emptyState('开始一次完整的账号研究','选择对标账号，确认推荐作品，批量转写、拆解并生成初步结论。','<a class="button" href="#accounts">选择一个账号</a>')}</div>`;}
function batchStats(b){const chosen=b.accounts.flatMap(a=>a.items.filter(i=>i.selected));return {count:chosen.length,text:chosen.filter(i=>i.text_ready).length,gallery:chosen.filter(i=>i.media_type==='gallery'&&!i.text_ready).length};}
function batchStages(b){
 const terminal=['completed','partial'].includes(b.status),isResearch=b.mode==='research',isAI=['research','breakdown'].includes(b.mode);
 const labels=['读取作品','确认清单',({download:'下载视频',transcribe:'转写文案',comments:'采集评论',refresh:'更新作品'})[b.mode]||'下载与转写',...(isAI?['逐条拆解']:[]),...(isResearch?['账号初步研究']:[]),'查看成果'];
 let current=b.status==='scanning'?0:['review_ready','scan_failed'].includes(b.status)?1:2;
 if(['awaiting_ai','analyzing'].includes(b.status))current=isResearch&&b.accounts.some(a=>a.summary_id&&a.summary_status!=='completed')?4:3;
 if(terminal)current=labels.length-1;
 return `<ol class="batch-stages">${labels.map((label,i)=>`<li class="${i<current?'done':i===current?'current':''}"><b>${i<current?'✓':i+1}</b><span>${label}</span></li>`).join('')}</ol>`;
}
function batchSummary(b){const s=batchStats(b);return `<div class="batch-summary"><div><strong>${esc(batchLabels[b.status])}</strong><p>${esc(b.message||'')}</p></div><div class="metric-inline"><span>${b.accounts.length} 个账号</span><span>${s.count} 条入选</span><span>${s.text} 条已有文案</span></div></div>`;}
function researchPage(b){return `${pageHead(batchModeLabels[b.mode]||'首次账号研究','同一批次按账号分别处理，所有成果保留来源。',`<a class="button secondary" href="${esc(scoped('/api/research/export',{id:b.id}))}">导出已有成果</a>`)}<a class="back-link" href="#batches">← 全部研究批次</a><div id="research-content">${researchContent(b)}</div>`;}
function researchContent(b){
 const editable=['review_ready','scan_failed'].includes(b.status),s=batchStats(b);
 const pending=b.accounts.some(a=>['partial_ready','scan_failed'].includes(a.status));
 return `${batchStages(b)}<section class="panel">${batchSummary(b)}${editable?`<div class="notice">先确认作品再开始。${s.text} 条文案可复用；${s.gallery} 条图文没有可用正文时，不进行语音识别。推荐依据仅来自当前已读取样本。</div><div class="toolbar"><div class="actions"><button class="button secondary small" data-action="batch-recommend" data-count="3">推荐 3 条 / 账号</button><button class="button secondary small" data-action="batch-recommend" data-count="6">推荐 6 条 / 账号</button><button class="button secondary small" data-action="batch-recommend" data-count="10">推荐 10 条 / 账号</button><button class="text-button" data-action="batch-select-all">全选清单</button><button class="text-button" data-action="batch-clear">清空选择</button></div></div>`:''}${b.ai_error?`<p class="form-error">${esc(b.ai_error)}</p>`:''}</section>
 ${b.accounts.map(a=>`<section class="panel batch-account" data-account="${esc(a.sec_uid)}"><div class="section-title"><div><h2>${esc(a.name)}</h2><small>平台报告 ${fmt(a.reported_count)} 条 · 本地已读取 ${a.read_count} 条 · 本清单 ${a.items.length} 条；不代表全账号扫描</small></div><span class="pill">${esc(batchLabels[a.status])}</span></div>${a.error?`<p class="form-error">${esc(a.error)}</p>`:''}${a.active_jobs?.length?`<div class="batch-current">${a.active_jobs.map(j=>`<strong>${esc(j.current_title||jobLabels[j.action])}</strong>${jobProgress(j)}<p>${esc(j.message)}</p>`).join('')}</div>`:''}${a.summary_id?`<div class="account-result"><div><h3>账号初步研究</h3><p>${a.summary_status==='completed'?'研究结论已保存，点击核对来源。':'等待逐条资料与账号结论生成完成。'}</p></div><a class="button ${a.summary_status==='completed'?'':'secondary'}" href="#report/${a.summary_id}">${a.summary_status==='completed'?'阅读账号结论':'查看研究阶段'} →</a></div>`:''}<div class="batch-items">${a.items.filter(i=>editable||i.selected).map(i=>batchItem(a,i,editable,b.mode)).join('')||'<p class="help-note">没有读取到可用作品，请检查账号主页和抖音窗口。</p>'}</div></section>`).join('')}
 <section class="panel batch-controls ${editable?'selection-bar':''} " >${editable?`<div class="actions"><label><input type="checkbox" id="batch-comments" ${state.batchComments?'checked':''}> 同时采集每条最多 30 条可见评论</label></div><p class="help-note">已选作品将按顺序处理。图文不下载为视频，已有文件和全文优先复用。确认后自动使用已配置的 AI 接口完成研究，可离开页面。</p><button class="button" data-action="batch-confirm">确认并开始处理（${s.count} 条）</button>`:`<div class="actions">${!['completed','cancelled','partial'].includes(b.status)?'<button class="button secondary" data-action="batch-cancel">停止本批任务</button>':''}${['interrupted','cancelled','partial_ready','scan_failed','partial'].includes(b.status)||pending?'<button class="button secondary" data-action="batch-retry">重试未完成项 / 恢复</button>':''}${pending&&b.confirmed_at?'<button class="button" data-action="batch-continue">先用成功作品继续</button>':''}<a class="text-button" href="#materials">查看已保存素材</a></div>`}
 ${!editable&&['research','breakdown'].includes(b.mode)&&!['completed','cancelled','interrupted'].includes(b.status)?`<div class="batch-ai"><h3>AI 自动研究</h3><p>${state.data.ai?.configured?'使用已配置的模型分析所选文案和评论，完成后自动保存逐条拆解与账号结论。':'请先配置 AI 接口，采集和转写可以先进行。'}</p><div class="actions"><button class="button" data-action="batch-run-ai" ${b.ai_auto?'disabled':''}>${b.ai_auto?'AI 已启用，自动接续本批':state.data.ai?.configured?'开始 / 重试 AI 研究':'配置 AI 接口'}</button><a class="text-button" href="#settings">AI 接口设置 →</a></div></div>`:''}
 </section>${['completed','partial'].includes(b.status)&&['research','breakdown'].includes(b.mode)?'<section class="panel account-result cloud-note"><div><h3>想持续跟踪这个账号，并用于自己的内容？</h3><p>了解灵思云端的持续跟踪、选题脚本与发布复盘。已有成果可以自由查看和导出。</p></div><a class="button secondary" href="#cloud">领取云端试用 →</a></section>':''}`;
}
function batchItem(a,i,editable,mode){
 const status=i.report_status==='completed'?'拆解已完成':i.report_phase?reportPhaseLabels[i.report_phase]:i.status==='failed'?'需要处理':i.status==='skipped'?'已跳过':i.text_ready?'文案已就绪':i.media_type==='gallery'?'图文作品':'待处理';
 return `<article class="batch-item ${i.selected?'selected':''}">${editable?`<label class="batch-check"><input type="checkbox" data-batch-select="${esc(i.aweme_id)}" data-sec="${esc(a.sec_uid)}" aria-label="选择 ${esc(i.title)}" ${i.selected?'checked':''}></label>`:''}<button class="batch-cover" data-action="preview-video" data-id="${esc(i.aweme_id)}" aria-label="预览 ${esc(i.title)}">${i.image_path?`<img src="${esc(imageSource('post',i.aweme_id))}" alt="" loading="lazy">`:'<span>预览</span>'}</button><div class="batch-item-content"><button class="work-preview-title" data-action="preview-video" data-id="${esc(i.aweme_id)}">${esc(i.title)}</button><small>${date(i.published_at)} · ${esc(i.reason)}${!i.selected&&!editable?' · 本次未选择':''}</small><div class="work-numbers"><span>赞 ${fmt(i.likes)}</span><span>评 ${fmt(i.comments)}</span><span>藏 ${fmt(i.collects)}</span><span>转 ${fmt(i.shares)}</span></div>${i.message?`<p class="${i.status==='failed'?'form-error':''}">${esc(i.message)}</p>`:''}${i.comment_warning?`<p class="form-error">评论：${esc(i.comment_warning)}</p>`:''}</div><div class="batch-item-action">${editable&&['research','breakdown'].includes(mode)?`<select data-batch-mode="${esc(i.aweme_id)}" data-sec="${esc(a.sec_uid)}" aria-label="${esc(i.title)}处理方式"><option value="breakdown" ${i.mode==='breakdown'?'selected':''}>转写并基础拆解</option><option value="transcribe" ${i.mode==='transcribe'?'selected':''}>仅准备文案</option></select>`:`<span class="pill ${i.status==='failed'?'waiting':''}">${esc(status)}</span>`}${i.text_ready?`<button class="text-button" data-action="preview-transcript" data-id="${esc(i.aweme_id)}">阅读文案</button>`:''}${i.report_id?`<a class="text-button" href="#report/${i.report_id}">${i.report_status==='completed'?'阅读拆解':'查看拆解阶段'} →</a>`:''}</div></article>`;
}

const beforeResearchAccountCard=accountCard;
accountCard=function(a){const selected=state.accountSelected?.has(a.sec_uid);return beforeResearchAccountCard(a).replace('<article class="account-card">',`<article class="account-card ${selected?'is-selected':''}"><label class="account-select"><input type="checkbox" data-account-select="${esc(a.sec_uid)}" aria-label="选择账号 ${esc(a.name)}" ${selected?'checked':''}> 选择账号</label>`).replace('<div class="card-footer">',`<button class="button small account-research-start" data-action="first-research" data-id="${esc(a.sec_uid)}">首次账号研究</button><div class="card-footer">`);};
const beforeResearchAccounts=accountsPage;
accountsPage=function(){return beforeResearchAccounts()+'<div id="account-bulk-bar" class="selection-bar" hidden></div>';};
const beforeResearchLibrary=renderAccountLibrary;
renderAccountLibrary=function(){beforeResearchLibrary();updateAccountBulk();};
function updateAccountBulk(){const bar=$('#account-bulk-bar');if(!bar)return;const n=state.accountSelected?.size||0;bar.hidden=!n;bar.innerHTML=`<span>已选 ${n} 个账号 · 分别保存结果</span><div class="actions"><button class="button secondary small" data-action="accounts-clear">取消选择</button><button class="button secondary small" data-action="accounts-refresh">批量刷新作品</button><button class="button small" data-action="accounts-research">分别进行首次研究</button></div>`;}
const beforeResearchAccountPage=accountPage;
accountPage=function(a){return beforeResearchAccountPage(a).replace('<div id="work-table"></div>','<div class="work-bulk-toolbar"><strong>选择要处理的作品</strong><button class="button secondary small" data-action="select-visible-works">全选当前筛选结果</button><button class="text-button" data-action="clear-works">取消选择</button><button class="button small" data-action="first-research" data-id="'+esc(a.sec_uid)+'">首次账号研究</button></div><div id="work-table"></div>').replace('>单条拆解</button>','>批量拆解</button>').replace('>开始 AI 研究 →</button>','>生成账号研究 →</button>');};
const beforeResearchMaterials=renderMaterials;
renderMaterials=function(){beforeResearchMaterials();if(!$('#material-list'))return;document.querySelectorAll('.material-row').forEach(row=>{const id=row.querySelector('[data-action="preview-video"]')?.dataset.id;if(id)row.insertAdjacentHTML('afterbegin',`<input type="checkbox" data-material-select="${esc(id)}" aria-label="选择素材 ${esc(id)}" ${state.materialSelected?.has(id)?'checked':''}>`);});$('#material-list').insertAdjacentHTML('beforeend','<div id="material-bulk-bar" class="selection-bar" hidden></div>');updateMaterialBulk();};
function updateMaterialBulk(){const bar=$('#material-bulk-bar');if(!bar)return;const n=state.materialSelected?.size||0;bar.hidden=!n;bar.innerHTML=`<span>已选 ${n} 条素材</span><div class="actions"><button class="text-button" data-action="materials-clear">取消选择</button><button class="button secondary small" data-action="materials-transcribe">批量转写</button><button class="button small" data-action="materials-breakdown">批量拆解</button></div>`;}
const beforeResearchTaskContent=taskContent;
taskContent=function(){const batches=(state.data.batches||[]).filter(b=>!['completed','partial','cancelled'].includes(b.status));return (batches.length?`<div class="section-title"><h2>账号研究与批量处理</h2><a class="text-button" href="#batches">全部研究</a></div><div class="report-list">${batches.map(b=>`<article class="report-row"><div><h3>${esc(batchModeLabels[b.mode])} · ${b.account_count} 个账号</h3><p>${esc(batchLabels[b.status])} · ${esc(b.message)}</p></div><a class="button secondary small" href="#batch/${b.id}">查看进度与成果 →</a></article>`).join('')}</div><div class="section-title"><h2>下载、转写与评论任务</h2></div>`:'')+beforeResearchTaskContent();};

Object.assign(actions,{
 'first-research':el=>createResearch('research',[el.dataset.id]),
 'accounts-research':()=>createResearch('research',[...(state.accountSelected||[])]),
 'accounts-refresh':async()=>{await createResearch('refresh',[...(state.accountSelected||[])]);if(state.batch?.mode==='refresh'&&state.batch.status==='review_ready'){await api('/api/research/confirm',bodyScope({id:state.batch.id,accounts:state.batch.accounts.map(a=>({sec_uid:a.sec_uid,items:[]}))}));await refresh();}},
 'accounts-clear':()=>{state.accountSelected=new Set();renderAccountLibrary();},
 'select-visible-works':()=>{visiblePosts().forEach(p=>state.selected.add(p.aweme_id));renderWorks();},
 'clear-works':()=>{state.selected.clear();renderWorks();},
 'download-selected':()=>createResearch('download',[],selectedIds()),
 'transcribe-selected':()=>createResearch('transcribe',[],selectedIds()),
 'comments-selected':()=>createResearch('comments',[],selectedIds()),
 'breakdown-selected':()=>createResearch('breakdown',[],selectedIds()),
 'create-report':()=>createResearch('research',[],selectedIds()),
 'materials-clear':()=>{state.materialSelected=new Set();renderMaterials();},
 'materials-transcribe':()=>createResearch('transcribe',[],[...(state.materialSelected||[])]),
 'materials-breakdown':()=>createResearch('breakdown',[],[...(state.materialSelected||[])]),
 'batch-select-all':()=>{state.batch.accounts.forEach(a=>a.items.forEach(i=>i.selected=true));$('#research-content').innerHTML=researchContent(state.batch);},
 'batch-clear':()=>{state.batch.accounts.forEach(a=>a.items.forEach(i=>i.selected=false));$('#research-content').innerHTML=researchContent(state.batch);},
 'batch-recommend':el=>{const n=Number(el.dataset.count);for(const a of state.batch.accounts){const chosen=new Set();const take=(rows,count,reason)=>{for(const i of rows){if(chosen.size>=n||count<=0)break;if(!chosen.has(i.aweme_id)){chosen.add(i.aweme_id);i.reason=reason;count--;}}};const recent=[...a.items].sort((x,y)=>String(y.published_at||'').localeCompare(String(x.published_at||'')));take(recent,2,'近期发布');take([...a.items].sort((x,y)=>(y.likes||0)-(x.likes||0)),2,'本批点赞靠前');take([...a.items].sort((x,y)=>(y.comments||0)-(x.comments||0)),1,'本批评论靠前');take([...a.items].sort((x,y)=>(y.collects||0)-(x.collects||0)),1,'本批收藏靠前');take(recent,n,'补充近期样本');a.items.forEach(i=>i.selected=chosen.has(i.aweme_id));}$('#research-content').innerHTML=researchContent(state.batch);},
 'batch-confirm':async()=>{const b=state.batch,scope=state.scope;await api('/api/research/confirm',bodyScope({id:b.id,accounts:b.accounts.map(a=>({sec_uid:a.sec_uid,items:a.items.filter(i=>i.selected).map(i=>({aweme_id:i.aweme_id,mode:i.mode,reason:i.reason}))})),comments:!!$('#batch-comments')?.checked,comment_limit:30}));if(scope===state.scope)await refresh();},
 'batch-retry':()=>batchMutation('retry'),
 'batch-continue':()=>batchMutation('continue'),
 'batch-cancel':()=>batchMutation('cancel'),
 'batch-run-ai':async()=>{if(!state.data.ai?.configured){location.hash='#settings';return;}await batchMutation('run-ai');},
});
async function batchMutation(action){const scope=state.scope,id=state.batch.id;await api('/api/research/'+action,bodyScope({id}));if(scope===state.scope&&location.hash==='#batch/'+id)await refresh();}
document.addEventListener('change',e=>{const t=e.target;
 if(t.dataset.accountSelect){state.accountSelected||=new Set();t.checked?state.accountSelected.add(t.dataset.accountSelect):state.accountSelected.delete(t.dataset.accountSelect);updateAccountBulk();}
 if(t.dataset.materialSelect){state.materialSelected||=new Set();t.checked?state.materialSelected.add(t.dataset.materialSelect):state.materialSelected.delete(t.dataset.materialSelect);updateMaterialBulk();}
 if(t.id==='batch-comments')state.batchComments=t.checked;
 if(t.dataset.batchSelect||t.dataset.batchMode){const a=state.batch?.accounts.find(a=>a.sec_uid===t.dataset.sec),item=a?.items.find(i=>i.aweme_id===(t.dataset.batchSelect||t.dataset.batchMode));if(!item)return;if(t.dataset.batchSelect){item.selected=t.checked;item.reason='手动选择';}else item.mode=t.value;const button=$('[data-action="batch-confirm"]');if(button)button.textContent=`确认并开始处理（${batchStats(state.batch).count} 条）`;}
});
let batchPolling=false;
async function updateBatchUI(changed){
 if($('#batch-count'))$('#batch-count').textContent=(state.data.batches||[]).length;
 const page=location.hash.slice(1).split('/')[0];
 if(page==='batches'&&changed&&$('#research-batch-list'))$('#research-batch-list').innerHTML=batchCards()||emptyState('还没有研究批次','从账号页开始一次研究。');
 if(page==='tasks'&&changed&&$('#task-list'))$('#task-list').innerHTML=taskContent();
 if(page!=='batch'||batchPolling||$('#modal').open||['review_ready','scan_failed'].includes(state.batch?.status))return;
 const scope=state.scope,id=location.hash.split('/')[1];batchPolling=true;
 try{const b=await api(scoped('/api/research',{id}));if(scope!==state.scope||location.hash!=='#batch/'+id)return;const before=JSON.stringify(state.batch);state.batch=b;if(before!==JSON.stringify(b)&&$('#research-content'))$('#research-content').innerHTML=researchContent(b);}finally{batchPolling=false;}
}
