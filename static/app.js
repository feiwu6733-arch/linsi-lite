'use strict';

const $ = (s) => document.querySelector(s);
const esc = (v = '') => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (n) => n == null ? '—' : Number(n).toLocaleString('zh-CN');
const date = (s) => s ? s.slice(0, 10) : '日期未知';
const state = {data:null, scope:'', account:null, report:null, selected:new Set(), query:'', sort:'published_at', load:0, render:0};
let toastTimer;

function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 4500);
}

async function api(path, body) {
  const init = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Linsi-Token':state.data?.token || ''}, body:JSON.stringify(body)};
  let response;
  try { response = await fetch(path, init); } catch { throw new Error('本地服务未连接，请启动灵思 Lite 后刷新页面。'); }
  let result;
  try { result = await response.json(); } catch { throw new Error('本地服务返回了无法读取的响应。'); }
  if (!response.ok) throw new Error(result.error || '操作未完成，请重试。');
  return result;
}

function scoped(path, args={}) {
  return path + '?' + new URLSearchParams({service_account_key:state.scope,...args});
}

function bodyScope(fields={}) { return {service_account_key:state.scope,...fields}; }
function countPosts(id) { return state.data.posts.filter(p => p.sec_uid === id); }
function demoNotice() { return state.data.profile?.demo ? '<div class="notice">◌ 演示空间：账号、作品、文案和互动数据均为虚构，仅用于体验流程。真实资料请新建研究空间。</div>' : ''; }
function trialStrip() { return '<div class="cloud-strip"><div><h3>研究之后，让内容持续发生。</h3><p>在灵思云端继续体验对标监控、选题脚本与发布复盘。</p></div><a class="button secondary small" href="#cloud">领取云端试用 ↗</a></div>'; }

async function refresh() {
  const load = ++state.load;
  const data = await api(state.scope ? scoped('/api/state') : '/api/state');
  if (load !== state.load) return;
  state.data = data;
  $('#space-select').innerHTML = '<option value="">选择工作台</option>' + data.profiles.map(p => `<option value="${esc(p.service_account_key)}" ${state.scope===p.service_account_key?'selected':''}>${esc(p.name)}</option>`).join('');
  $('#account-count').textContent = data.accounts.filter(a=>a.sec_uid!==data.binding?.sec_uid).length;
  $('#report-count').textContent = data.reports.length;
  await render();
}

function setScope(scope) {
  state.load++;
  state.render++;
  state.accountSelected=new Set();state.materialSelected=new Set();state.batch=null;
  if($('#modal').open)$('#modal').close();
  state.materialQuery='';state.materialAccount='';state.materialFilter='all';state.accountQuery='';state.accountSort='recent';state.taskFilter='active';state.pagePositions={};state.pageKey='';
  state.scope = scope;
  if(state.data)state.data={...state.data,profile:null,accounts:[],posts:[],reports:[],jobs:[],batches:[],binding:null,connection:{}};
  state.query='';state.sort='published_at';
  state.account = null;
  state.report = null;
  state.selected.clear();
  try { localStorage.setItem('linsi-space-preference', scope); } catch { /* preference only */ }
}

async function render() {
  if(typeof updateActivity==='function')updateActivity();
  const serial = ++state.render;
  const nextKey=state.scope+location.hash;const routeChanged=state.pageKey!==nextKey;
  state.pagePositions||={};if(routeChanged&&state.pageKey)state.pagePositions[state.pageKey]=scrollY;state.pageKey=nextKey;
  $('#task-count').textContent=(state.data?.jobs||[]).filter(j=>['queued','running','cancelling'].includes(j.status)).length;
  const parts = location.hash.slice(1).split('/');
  const page = parts[0] || 'home';
  const nav = ['batch','batches'].includes(page)?'batches':page === 'account' ? 'accounts' : page === 'report' ? 'reports' : page;
  document.querySelectorAll('[data-nav]').forEach(a => {a.classList.toggle('active', a.dataset.nav === nav); if(a.dataset.nav === nav)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  const labels = {home:'开始研究',mine:'我的账号 / 抖音连接',tasks:'采集与转写',accounts:'对标账号',account:'对标账号 / 作品研究',reports:'研究报告',report:'研究报告 / 阅读',cloud:'体验灵思云端',settings:'使用与设置'};
  $('#breadcrumb').textContent = '工作台 / ' + (labels[page] || '开始研究');
  if (['home','account','accounts','report','reports','mine','tasks','materials','batch','batches'].includes(page) && !state.scope) {
    $('#main').innerHTML = noSpace();
    if(typeof syncPageHeader==='function')syncPageHeader();
    return;
  }
  if (page === 'account') {
    const id = decodeURIComponent(parts.slice(1).join('/'));
    const account = state.data.accounts.find(a => a.sec_uid === id);
    if(!account) {$('#main').innerHTML='<div class="empty"><h3>当前空间中没有这个账号</h3><a class="button secondary" href="#accounts">返回账号列表</a></div>';if(typeof syncPageHeader==='function')syncPageHeader();return;}
    if(state.account?.sec_uid !== id) { state.selected.clear(); state.query=''; state.sort='published_at'; }
    state.account = account;
    $('#main').innerHTML = accountPage(account);
    renderWorks();
  } else if(page === 'batch'&&typeof researchPage==='function') {
    $('#main').innerHTML='<div class="loading">正在读取研究批次…</div>';
    const batch=await api(scoped('/api/research',{id:parts[1]||''}));
    if(serial!==state.render)return;state.batch=batch;$('#main').innerHTML=researchPage(batch);
  } else if(page === 'report') {
    $('#main').innerHTML = '<div class="loading">正在读取报告与来源…</div>';
    const report = await api(scoped('/api/report',{id:parts[1] || ''}));
    if(serial !== state.render) return;
    state.report = report;
    $('#main').innerHTML = reportPage(report);
  } else {
    const views = {home:homePage, accounts:accountsPage, reports:reportsPage, cloud:cloudPage, settings:settingsPage, mine:minePage,tasks:tasksPage,materials:materialsPage,batches:()=>typeof researchListPage==='function'?researchListPage():homePage()};
    $('#main').innerHTML = (views[page] || homePage)();
  }
  if(typeof syncPageHeader==='function')syncPageHeader();
  if(routeChanged)requestAnimationFrame(()=>{if(serial===state.render)scrollTo(0,state.pagePositions[nextKey]||0);});
}

function noSpace() {
  return `<div class="empty"><h3>继续哪个账号的工作？</h3><p>点一下进入，作品和报告会保持分开。</p><div class="actions">${state.data.profiles.map(p=>`<button class="button secondary" data-action="choose-workspace" data-id="${esc(p.service_account_key)}">${esc(p.name)}</button>`).join('')}<button class="text-button" data-action="new-space">添加另一个工作台</button></div></div>`;
}

function homePage() {
  const d = state.data;
  const completed = d.reports.filter(r=>r.status==='completed').length;
  return `${demoNotice()}<section class="hero"><div><div class="eyebrow">LINSI LITE / RESEARCH STARTS HERE</div><h1>把对标看明白，<br>把内容做扎实。</h1><p>把分散的作品、文案和观察，整理成有来源的研究。<br>从一个值得学习的账号，开始你的下一步。</p><div class="actions"><button class="button" data-action="${state.scope?'new-account':'new-space'}">${state.scope?'＋ 添加对标账号':'创建我的研究空间'} <span aria-hidden="true">→</span></button><button class="button quiet" data-action="demo">先看演示项目 ↗</button></div></div><div class="hero-visual" aria-hidden="true"><div class="paper-card"><div class="paper-top"><span>RESEARCH NOTE</span><span>01</span></div><h3>有依据，才有启发。</h3><div class="paper-line"></div><div class="paper-line short"></div><div class="paper-tags"><span>账号定位</span><span>作品样本</span><span>内容结构</span></div><div class="paper-line"></div><div class="paper-line short"></div><div class="paper-bottom">◎ 每个结论，都能回到来源</div></div><span class="hero-note">你的研究，保存在你的电脑。</span></div></section>
  <div class="stats"><div class="stat"><span class="stat-icon">◎</span><div><strong>${d.accounts.length}</strong><small>已整理的对标账号</small></div></div><div class="stat"><span class="stat-icon">≡</span><div><strong>${d.posts.filter(p=>p.transcript).length}</strong><small>有文案的作品样本</small></div></div><div class="stat"><span class="stat-icon">▤</span><div><strong>${completed}</strong><small>已完成的研究报告</small></div></div></div>
  <div class="section-title"><h2>一次完整研究，只需三步</h2><span>资料 → 理解 → 沉淀</span></div><div class="steps"><article class="step"><span class="number">01 /</span><h3>找到值得学习的账号</h3><p>添加账号主页，导入作品清单。先聚焦一个账号，把信息整理到一起。</p><button class="text-button" data-action="${state.scope?'new-account':'new-space'}">添加我的第一个对标 →</button></article><article class="step"><span class="number">02 /</span><h3>选几条作品，认真看懂</h3><p>按时间、互动量筛选，补齐文案与笔记，为研究选好有依据的样本。</p><a class="text-button" href="#accounts">查看账号与作品 →</a></article><article class="step"><span class="number">03 /</span><h3>让 AI API 整理你的发现</h3><p>生成研究资料，使用 AI 接口 分析，再回到网页阅读并导出报告。</p><a class="text-button" href="#reports">查看研究报告 →</a></article></div>
  ${d.accounts.length?'<div class="section-title"><h2>继续你的研究</h2><a href="#accounts" class="text-button">查看全部 →</a></div><div class="cards">'+d.accounts.slice(0,2).map(accountCard).join('')+'</div>':''}${trialStrip()}`;
}

function accountCard(a) {
  const posts = countPosts(a.sec_uid);
  const ready = posts.filter(p=>p.transcript).length;
  return `<article class="account-card"><div class="account-title"><span class="avatar">${esc(a.name.slice(0,1))}</span><div><h3>${esc(a.name)}</h3><small>更新于 ${date(a.updated_at)} ${a.demo?' · 演示账号':''}</small></div></div><p>${esc(a.bio || '还没有填写简介。进入账号，开始整理作品与文案。')}</p><div class="account-meta"><span><strong>${posts.length}</strong>条作品</span><span><strong>${ready}</strong>条有文案</span><span><strong>${state.data.reports.filter(r=>r.sec_uid===a.sec_uid).length}</strong>份研究</span></div><div class="card-footer"><span class="pill ${a.demo?'demo':''}">${a.demo?'虚构演示':'本地研究'}</span><a href="#account/${encodeURIComponent(a.sec_uid)}" class="text-button">进入研究 →</a></div></article>`;
}

function accountsPage() {
  return `${demoNotice()}<div class="page-head"><div><div class="eyebrow">COMPETITOR LIBRARY</div><h1>你的对标账号</h1><p>把值得学习的账号，变成可以反复查阅的研究资料。</p></div><div class="actions"><button class="button secondary" data-action="import">导入资料</button><button class="button" data-action="new-account">＋ 添加账号</button></div></div>${state.data.accounts.length?'<div class="cards">'+state.data.accounts.map(accountCard).join('')+'</div>':'<div class="empty"><span class="empty-icon">◎</span><h3>还没有对标账号</h3><p>粘贴完整主页链接建档，再导入作品清单或手动添加代表作品。</p><button class="button" data-action="new-account">添加对标账号</button></div>'}${trialStrip()}`;
}

function accountPage(a) {
  const posts = countPosts(a.sec_uid);
  return `<a class="back-link" href="#accounts">← 全部对标账号</a>${demoNotice()}<div class="detail-heading">${avatar(a)}<div><h1>${esc(a.name)}</h1><p>${esc(a.bio || '从作品中积累你的观察。')}</p><div class="actions"><span>已保存 ${posts.length} 条作品 · ${posts.filter(p=>p.transcript).length} 条有文案</span>${a.url?`<a class="source-link" href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">查看账号主页 ↗</a>`:''}</div></div></div><div class="toolbar"><div class="actions"><button class="button secondary small" data-action="new-post">＋ 添加作品</button><button class="button secondary small" data-action="import">导入作品清单</button><button class="button secondary small" data-action="sync">${state.data.connector_ready?'手动同步作品':'连接在线数据'}</button></div><a class="text-button" href="${esc(scoped('/api/account/export',{sec_uid:a.sec_uid}))}">导出账号资料 ↓</a></div><div class="section-title"><h2>选择这次要研究的作品</h2><span>支持 1～30 条有文案的样本</span></div><div class="toolbar"><input class="search" id="work-search" aria-label="搜索作品标题或文案" placeholder="搜索标题或文案…" value="${esc(state.query)}"><select id="work-sort" aria-label="作品排序"><option value="published_at">最新发布</option><option value="likes">点赞从高到低</option><option value="collects">收藏从高到低</option><option value="comments">评论从高到低</option><option value="shares">分享从高到低</option></select></div><div id="work-table"></div><div class="selection-bar"><span>已选择 <strong id="selected-count">0</strong> 条作品 · 空文案需补充后才能研究</span><button class="button small" data-action="create-report" id="create-report-button">开始 AI 研究 →</button></div><p class="inline-status spaced">这里展示的是已导入样本，互动数未知时显示「—」，不代表零互动。排序不等于爆款判断。</p>`;
}

function visiblePosts() {
  return countPosts(state.account.sec_uid).filter(p=>(p.title+' '+p.transcript).toLowerCase().includes(state.query.toLowerCase())).sort((a,b)=>state.sort==='published_at'?(b.published_at||'').localeCompare(a.published_at||''):(b[state.sort]??-1)-(a[state.sort]??-1));
}

function renderWorks() {
  const posts = visiblePosts();
  const allSelected = posts.length && posts.every(p=>state.selected.has(p.aweme_id));
  $('#work-table').innerHTML = posts.length ? `<div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="select-visible" aria-label="选择当前筛选结果" ${allSelected?'checked':''}></th><th>作品 / 发布日期</th><th>点赞</th><th>评论</th><th>收藏</th><th>分享</th><th>文案</th><th>操作</th></tr></thead><tbody>${posts.map(p=>`<tr><td><input type="checkbox" data-select="${esc(p.aweme_id)}" aria-label="选择 ${esc(p.title)}" ${state.selected.has(p.aweme_id)?'checked':''}></td><td class="work-title"><button class="work-preview-title" data-action="preview-video" data-id="${esc(p.aweme_id)}">${esc(p.title)}</button><small>${date(p.published_at)} ${p.demo?'· 演示':''}</small></td><td>${fmt(p.likes)}</td><td>${fmt(p.comments)}</td><td>${fmt(p.collects)}</td><td>${fmt(p.shares)}</td><td><span class="pill ${p.transcript?'':'waiting'}">${p.transcript?'已整理':'待补充'}</span></td><td><button class="table-action" data-action="preview-video" data-id="${esc(p.aweme_id)}">${p.media_type==='gallery'?'预览图文':p.media_path?'播放视频':'准备预览'}</button><button class="table-action" data-action="view-comments" data-id="${esc(p.aweme_id)}">评论采集</button><button class="table-action" data-action="transcript" data-id="${esc(p.aweme_id)}">${p.transcript?'阅读文案':'补充文案'}</button>${p.url?`<a class="table-action" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">原作 ↗</a>`:''}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty"><h3>这里还没有匹配的作品</h3><p>添加作品、导入资料，或调整搜索条件。</p></div>';
  $('#selected-count').textContent = state.selected.size;
  $('#create-report-button').disabled = state.selected.size === 0;
  $('#work-sort').value = state.sort;
}

function reportsPage() {
  return `${demoNotice()}<div class="page-head"><div><div class="eyebrow">RESEARCH NOTES</div><h1>研究报告</h1><p>查看研究进度与结果，每份报告都保留作品来源。</p></div><a class="button secondary" href="#accounts">从对标账号开始 →</a></div>${state.data.reports.length?'<div class="report-list">'+state.data.reports.map(r=>`<article class="report-row"><div><h3>${esc(r.title)}</h3><small>${date(r.created_at)} · ${r.demo?'虚构演示':'当前研究空间'}</small></div><div class="actions"><span class="pill ${r.status==='completed'?'':'waiting'}">${r.status==='completed'?'报告已完成':reportPhaseLabels[r.phase||'waiting']||'等待 AI 研究'}</span><a class="button secondary small" href="#report/${r.id}">打开报告 →</a></div></article>`).join('')+'</div>':'<div class="empty"><span class="empty-icon">▤</span><h3>你的第一份研究，正在起点</h3><p>进入对标账号，选好作品并补齐文案，就可以开始研究。</p><a class="button" href="#accounts">去选择作品</a></div>'}${trialStrip()}`;
}

function markdown(value) {
  const lines=String(value||'').split('\n'),result=[];let paragraph=[],list='',code=null;
  const inline=s=>{
    const safe=[];const keep=html=>'\u0000'+(safe.push(html)-1)+'\u0000';
    s=s.replace(/`([^`]+)`/g,(_,text)=>keep('<code>'+esc(text)+'</code>'));
    s=s.replace(/\[([^\]]+)\]\(([^\s)]+)\)/g,(all,label,url)=>{try{const u=new URL(url);if(!['https:','http:'].includes(u.protocol))return all;return keep('<a href="'+esc(u.href)+'" target="_blank" rel="noopener noreferrer">'+esc(label)+'</a>');}catch{return all;}});
    return esc(s).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/\u0000(\d+)\u0000/g,(_,i)=>safe[Number(i)]||'');
  };
  const flush=()=>{if(paragraph.length){result.push('<p>'+paragraph.map(inline).join('\n')+'</p>');paragraph=[];}};
  const closeList=()=>{if(list){result.push('</'+list+'>');list='';}};
  const cells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(x=>x.trim());
  for(let n=0;n<lines.length;n++){
    const line=lines[n],trim=line.trim();
    if(/^```/.test(trim)){flush();closeList();if(code!==null){result.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');code=null;}else code=[];continue;}
    if(code!==null){code.push(line);continue;}
    if(line.includes('|')&&n+1<lines.length&&/^\s*\|?\s*:?-{3,}/.test(lines[n+1])&&cells(lines[n+1]).every(c=>/^:?-{3,}:?$/.test(c))){flush();closeList();const header=cells(line);result.push('<div class="table-wrap"><table><thead><tr>'+header.map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>');n++;while(n+1<lines.length&&lines[n+1].includes('|')){const row=cells(lines[++n]);result.push('<tr>'+header.map((_,i)=>'<td>'+inline(row[i]||'')+'</td>').join('')+'</tr>');}result.push('</tbody></table></div>');continue;}
    const heading=line.match(/^(#{1,6})\s+(.+)$/),item=line.match(/^\s*(?:([-*+])|\d+[.)])\s+(.+)$/);
    if(heading){flush();closeList();result.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`);}
    else if(/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)){flush();closeList();result.push('<hr>');}
    else if(item){flush();const type=item[1]?'ul':'ol';if(list!==type){closeList();result.push('<'+type+'>');list=type;}result.push('<li>'+inline(item[2])+'</li>');}
    else if(line.startsWith('> ')){flush();closeList();result.push('<blockquote>'+inline(line.slice(2))+'</blockquote>');}
    else if(!trim){flush();closeList();}
    else{closeList();paragraph.push(line);}
  }
  flush();closeList();if(code!==null)result.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');return result.join('');
}

function reportPage(r) {return `<article class="report-paper">${markdown(r.markdown)}</article>`;}

function cloudPage() {
  const b = state.data.brand;
  const rows = [['对标账号','手动添加、整理和更新','持续管理与跟踪对标'],['作品与文案','导入 / 手动维护，连接自己的数据服务','批量素材处理与完整任务流程'],['账号研究','AI API 基础研究，可导出报告','深度研究与创作资料联动'],['内容机会','本次研究的观察与学习方向','持续监控、筛选和灵感沉淀'],['选题与脚本','了解云端功能','结合自有账号定位与历史生成内容'],['发布复盘','了解云端功能','持续记录表现，反馈到创作流程'],['运行方式','自己的电脑、自己的资料','在线工作台，按开通套餐使用']];
  const url = new URL(b.website || 'https://os.yanbeiai.com/');
  url.searchParams.set('utm_source','linsi-lite');url.searchParams.set('utm_medium','local-app');
  return `<section class="cloud-hero"><div><div class="eyebrow">THE NEXT CHAPTER / LINSI CLOUD</div><h1>研究一个账号之后，<br>开始经营你自己的内容。</h1><p>灵思云端把对标监控、素材、选题脚本与发布复盘连起来。让今天整理的研究，接上明天要做的内容。</p><div class="actions"><a class="button light" href="${esc(url.toString())}" target="_blank" rel="noopener noreferrer">了解灵思云端 ↗</a></div></div><div class="trial-card"><small>添加微信 · 备注「开源体验」</small><div class="wechat">${esc(b.wechat)}</div><div class="actions"><button class="button light" data-action="copy-wechat">复制微信号</button></div><p>${esc(b.trial_message)}</p></div></section><div class="section-title"><h2>从单次研究，到持续内容运营</h2><span>根据需要，选择适合你的方式</span></div><div class="table-wrap"><table class="comparison"><thead><tr><th>你想完成的事情</th><th>灵思 Lite · 本地开源版</th><th>灵思云端 · 完整流程</th></tr></thead><tbody>${rows.map(row=>'<tr>'+row.map(c=>'<td>'+esc(c)+'</td>').join('')+'</tr>').join('')}</tbody></table></div><div class="section-title"><h2>试用，围绕你的真实账号展开</h2></div><div class="steps"><article class="step"><span class="number">01 /</span><h3>告诉我们你的研究目标</h3><p>添加微信，备注「开源体验」，简单介绍你的行业和账号阶段。</p></article><article class="step"><span class="number">02 /</span><h3>领取试用资格</h3><p>确认开通方式、期限和额度，用自己的对标和素材开始体验。</p></article><article class="step"><span class="number">03 /</span><h3>完成一次真实内容流程</h3><p>从研究到选题，再到一条脚本，体验后决定是否继续使用。</p></article></div><p class="inline-status spaced">本地资料不会自动上传云端。云端功能、期限和额度以实际开通说明为准。</p>`;
}

function settingsPage() {
 const b=state.data.brand;
 return `<div class="page-head"><div><h1>设置与帮助</h1><p>管理你的账号、AI 接口和本地资料。</p></div></div><div class="settings-grid"><section class="panel"><h2>快速使用</h2><ol><li>连接抖音或粘贴对标链接。</li><li>选择作品，采集视频、评论并转写。</li><li>配置 AI 接口，在网页内开始研究。</li><li>查看研究报告与来源，按需导出。</li></ol></section><section class="panel"><h2>灵思云端试用</h2><p>微信 ${esc(b.wechat)} · 备注「开源体验」</p><button class="button secondary" data-action="copy-wechat">复制微信号</button><a class="text-button" href="#cloud">了解灵思云端 →</a></section></div>`;
}

function modal(title, html) {
  const previousVideo=$('#modal video');
  if(previousVideo){previousVideo.pause();previousVideo.removeAttribute('src');previousVideo.load();}
  $('#modal').classList.remove('media-wide');
  $('#modal-title').textContent=title;
  $('#modal-body').innerHTML=html;
  $('#modal').showModal();
}
const endForm = (label) => `<div class="form-error" role="alert"></div><div class="form-actions"><button type="button" class="button secondary" data-action="close-modal">取消</button><button type="submit" class="button">${label}</button></div></form>`;

function spaceModal() {
  modal('添加一个工作台', `<form id="space-form"><p class="help-note">只有管理另一个账号时才需要添加。作品和报告会分开保存。</p><label class="field"><span>取个名字（选填）</span><input name="name" maxlength="60" placeholder="例如：我的第二个账号"></label>${endForm('直接进入')}`);
}

function workspaceSettings() {
  const p=state.data.profile;
  if(!p)return;
  modal('工作台设置', `<form id="workspace-settings-form"><label class="field"><span>工作台名称</span><input name="name" required maxlength="60" value="${esc(p.name)}"></label><details><summary>补充研究目标（选填）</summary><p class="help-note">先采集、看数据都不用填。需要结合自己的情况研究时，再补充即可。</p><label class="field"><span>内容方向</span><select name="direction" id="direction-input"><option value="">暂不设置</option>${state.data.directions.map(d=>`<option ${p.direction===d?'selected':''}>${d}</option>`).join('')}</select></label><label class="field"><span>希望从对标中学习什么？</span><textarea name="positioning" maxlength="2000" placeholder="例如：想学习同行视频如何开头">${esc(p.positioning)}</textarea></label><div id="other-positioning" class="${p.direction==='其他定位'?'':'hidden'}">${[['niche','具体赛道'],['host','主理人'],['audience','目标用户'],['pillars','内容支柱'],['monetization','变现路径']].map(([k,l])=>`<label class="field"><span>${l}</span><input name="${k}" maxlength="500" value="${esc(p[k])}" ${p.direction==='其他定位'?'required':''}></label>`).join('')}</div></details>${endForm('保存设置')}`);
}

function accountModal() {
  if(!state.scope)return spaceModal();
  if(state.data.profile.demo){toast('真实账号请使用独立研究空间。');return spaceModal();}
  modal('添加对标账号', `<form id="account-form"><p class="help-note">先在浏览器打开账号主页，再复制完整地址。添加只建立本地记录；作品可手动导入，或连接你自己的数据服务。</p><label class="field"><span>账号昵称</span><input name="name" required maxlength="100" placeholder="输入对标账号昵称"></label><label class="field"><span>完整主页链接</span><input id="account-url-input" name="url" type="url" placeholder="https://www.douyin.com/user/…"><small>也可直接填写下方稳定账号 ID。短链接需先展开。</small></label><label class="field"><span>sec_uid · 稳定账号 ID</span><input id="account-id-input" name="sec_uid" required maxlength="160" placeholder="从完整主页链接自动识别"></label><label class="field"><span>账号简介 / 观察备注</span><textarea name="bio" maxlength="3000" placeholder="记录你为什么想研究这个账号"></textarea></label>${endForm('保存对标账号')}`);
}

function postModal() {
  if(!state.account)return;
  if(state.account.demo){toast('演示账号使用固定示例。真实资料请新建研究空间。');return;}
  modal('添加代表作品', `<form id="post-form"><label class="field"><span>作品标题</span><input name="title" required maxlength="1000"></label><label class="field"><span>完整作品链接</span><input id="post-url-input" name="url" type="url" placeholder="https://www.douyin.com/video/…"></label><div class="form-grid"><label class="field"><span>aweme_id · 稳定作品 ID</span><input id="post-id-input" name="aweme_id" required></label><label class="field"><span>发布日期</span><input name="published_at" type="date"></label></div><div class="form-grid">${[['likes','点赞'],['comments','评论'],['collects','收藏'],['shares','分享']].map(([k,l])=>`<label class="field"><span>${l}</span><input name="${k}" type="number" min="0" max="1000000000000" step="1" placeholder="未知留空"></label>`).join('')}</div><label class="field"><span>作品文案</span><textarea name="transcript" maxlength="100000" placeholder="粘贴已有转写，或先留空稍后补充"></textarea></label>${endForm('保存作品')}`);
}

function importModal() {
  if(!state.scope)return spaceModal();
  modal('导入对标资料', `<form id="import-form"><p class="help-note">选择本工具导出的 JSON 文件，可将账号与作品导入当前工作台。导入会更新同一账号与作品；空白文案会覆盖原有文案，请先确认内容。</p><label class="field"><span>选择 JSON 文件</span><input type="file" id="bundle-file" accept=".json,application/json"></label><label class="field"><span>资料内容</span><textarea id="bundle-json" name="bundle" class="code-input" required placeholder='{"account":{"sec_uid":"…","name":"…"},"posts":[]}'></textarea></label><p class="inline-status" id="bundle-preview">资料仅在点击「确认导入」后保存到当前空间。</p>${endForm('确认导入')}`);
}

function transcriptModal(id) {
  const p=state.data.posts.find(p=>p.aweme_id===id);
  if(!p)throw new Error('作品不属于当前空间。');
  modal('作品文案与笔记', `<form id="transcript-form" data-id="${esc(id)}"><p class="help-note">${esc(p.title)}<br>aweme_id：${esc(p.aweme_id)}${p.demo?' · 虚构演示':''}</p><label class="field"><span>从文件补充文案（TXT / SRT / VTT）</span><input type="file" id="transcript-file" accept=".txt,.srt,.vtt,text/plain"></label><label class="field"><span>原始文案</span><textarea id="transcript-text" name="transcript" class="code-input" maxlength="100000">${esc(p.transcript)}</textarea><small>这份文字是研究依据。没有观看画面时，不应据此断言视频的视觉表现。</small></label><label class="field"><span>我的观察笔记</span><textarea name="notes" maxlength="5000">${esc(p.notes)}</textarea></label>${endForm('保存文案与笔记')}`);
}

function reportModal() {
  const r=state.report;
  modal('导入或编辑研究报告', `<form id="report-form"><p class="help-note">保存后标记为「报告已完成」。请确认内容是对本次样本的实际分析，并保留所有作品的 aweme_id。</p><label class="field"><span>选择 Markdown 文件</span><input type="file" id="report-file" accept=".md,.txt,text/markdown"></label><label class="field"><span>报告正文</span><textarea class="code-input" id="report-markdown" name="markdown" maxlength="200000" required>${esc(r.markdown)}</textarea></label>${endForm('保存完成的报告')}`);
}

function connectorModal() {
  modal('连接你自己的数据服务', `<p>开源版不包含灵思私有采集服务，也不会自动使用云端额度。你可以使用本地导入，或接入自己已有的数据服务。</p><ol><li>在运行数据目录中新建 <code>connector.json</code>。</li><li>填写返回 account + posts 格式的 HTTPS 接口。</li><li>如需认证，在启动进程的环境变量中设置 <code>LINSI_CONNECTOR_TOKEN</code>。</li><li>回到账号页，点击「手动同步作品」。</li></ol><pre class="code-block">{
  "account_url": "https://YOUR-DATA-SERVICE/accounts/{sec_uid}"
}</pre><p class="help-note">接口字段见 README。请求只在你点击同步后发生；不发送本地报告、笔记或空间定位。连接配置仅保存在本机。</p><div class="actions"><button class="button secondary" data-action="close-modal">知道了</button><a class="button" href="#cloud" data-action="close-modal">想直接使用？了解云端 ↗</a></div>`);
}

async function copy(textValue) {
  try {await navigator.clipboard.writeText(textValue);toast('已复制，可直接粘贴。');}
  catch {modal('复制内容',`<label class="field"><span>请选择并复制下方内容</span><textarea class="code-input" readonly>${esc(textValue)}</textarea></label>`);}
}

const actions = {
  'workspace-settings':workspaceSettings,'choose-workspace':async el=>{setScope(el.dataset.id);location.hash='#home';await refresh();},'new-space':spaceModal,'new-account':accountModal,'new-post':postModal,'import':importModal,
  'close-modal':()=>$('#modal').close(),'connector-help':connectorModal,
  'transcript':el=>transcriptModal(el.dataset.id),'edit-report':reportModal,
  'copy-wechat':()=>copy(state.data.brand.wechat),
  'demo':async()=>{const p=await api('/api/demo',{});setScope(p.service_account_key);location.hash='#accounts';await refresh();toast('已打开虚构演示空间。');},
  'refresh':async()=>{await refresh();toast('已读取最新本地记录。');},
  'sync':async()=>{if(!state.data.connector_ready)return connectorModal();const r=await api('/api/sync',bodyScope({sec_uid:state.account.sec_uid}));await refresh();toast(`已同步 ${r.imported} 条作品。`);},
  'create-report':async()=>{const r=await api('/api/reports/create',bodyScope({sec_uid:state.account.sec_uid,aweme_ids:[...state.selected]}));await refresh();location.hash='#report/'+r.id;toast('研究资料已保存，请使用 AI 接口 完成分析。');}
};

document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');
  if(!el)return;
  const action=actions[el.dataset.action];
  if(!action)return;
  if(el.tagName==='BUTTON' && el.disabled)return;
  if(el.tagName==='BUTTON')el.disabled=true;
  try{await action(el);}catch(error){toast(error.message);}finally{if(el.isConnected)el.disabled=false;}
});

document.addEventListener('input',event=>{
  const el=event.target;
  if(el.id==='work-search'){state.query=el.value;renderWorks();}
  if(['account-url-input','post-url-input'].includes(el.id)){
    try{const p=new URL(el.value);const kind=el.id==='account-url-input'?'user':'video';const m=p.pathname.match(new RegExp('^/'+kind+'/([^/]+)/?$'));if(m && p.hostname==='www.douyin.com')$(kind==='user'?'#account-id-input':'#post-id-input').value=m[1];}catch{/* allow typing */}
  }
});

document.addEventListener('change',async event=>{
  const el=event.target;
  try{
    if(el.id==='space-select'){setScope(el.value);location.hash='#home';await refresh();}
    if(el.id==='work-sort'){state.sort=el.value;renderWorks();}
    if(el.dataset.select){if(el.checked)state.selected.add(el.dataset.select);else state.selected.delete(el.dataset.select);renderWorks();}
    if(el.id==='select-visible'){for(const p of visiblePosts()){if(el.checked)state.selected.add(p.aweme_id);else state.selected.delete(p.aweme_id);}renderWorks();}
    if(el.id==='direction-input'){const other=el.value==='其他定位';$('#other-positioning').classList.toggle('hidden',!other);$('#other-positioning').querySelectorAll('input').forEach(i=>i.required=other);}
    const files={'bundle-file':['#bundle-json',4*1024*1024],'transcript-file':['#transcript-text',400000],'report-file':['#report-markdown',800000]};
    if(files[el.id] && el.files[0]){
      const [target,limit]=files[el.id];const file=el.files[0];
      if(file.size>limit)throw new Error('文件超过允许大小，请拆分后导入。');
      $(target).value=await file.text();
      if(el.id==='bundle-file'){const bundle=JSON.parse($(target).value);$('#bundle-preview').textContent=`准备导入：${bundle.account?.name || '未命名'}，${bundle.posts?.length || 0} 条作品。点击确认后保存。`;}
    }
  }catch(error){toast(error.message);}
});

document.addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.target;const data=Object.fromEntries(new FormData(form));
  const submit=form.querySelector('[type=submit]');if(!submit || submit.disabled)return;
  const errorBox=form.querySelector('.form-error');if(errorBox)errorBox.textContent='';
  submit.disabled=true;
  try{
    if(form.id==='space-form'){const p=await api('/api/workspaces/create',data);setScope(p.service_account_key);location.hash='#home';}
    else if(form.id==='workspace-settings-form'){await api('/api/workspaces/update',bodyScope(data));}
    else if(form.id==='account-form'){const result=await api('/api/import',bodyScope({bundle:{account:data,posts:[]}}));location.hash='#account/'+encodeURIComponent(result.account.sec_uid);}
    else if(form.id==='post-form'){for(const k of ['likes','comments','collects','shares'])data[k]=data[k]===''?null:Number(data[k]);await api('/api/import',bodyScope({bundle:{account:state.account,posts:[data]}}));}
    else if(form.id==='import-form'){const result=await api('/api/import',bodyScope({bundle:JSON.parse(data.bundle)}));location.hash='#account/'+encodeURIComponent(result.account.sec_uid);}
    else if(form.id==='transcript-form'){await api('/api/transcript',bodyScope({aweme_id:form.dataset.id,...data}));}
    else if(form.id==='report-form'){await api('/api/reports/save',bodyScope({id:state.report.id,source_digest:state.report.source_digest,...data}));}
    await refresh();
    if($('#modal').open)$('#modal').close();
    toast('已保存到本机。');
  }catch(error){if(errorBox)errorBox.textContent=error.message;else toast(error.message);}
  finally{if(submit.isConnected)submit.disabled=false;}
});

window.addEventListener('hashchange',()=>{if(state.data)render().catch(error=>toast(error.message));});
(async()=>{
  try{
    state.data=await api('/api/state');
    let saved='';try{saved=new URLSearchParams(location.search).get('workspace')||localStorage.getItem('linsi-space-preference')||'';}catch{/* optional preference */}
    if(new URLSearchParams(location.search).has('workspace')){const entry=new URL(location.href);entry.searchParams.delete('workspace');history.replaceState(null,'',entry.pathname+entry.search+entry.hash);}
    if(state.data.profiles.some(p=>p.service_account_key===saved))setScope(saved);
    else {
      const real=state.data.profiles.filter(p=>!p.demo);
      if(real.length===1)setScope(real[0].service_account_key);
      else if(!real.length){const p=await api('/api/workspaces/start',{});setScope(p.service_account_key);}
    }
    await refresh();
  }catch(error){$('#main').innerHTML=`<div class="empty"><h3>暂时无法连接本地服务</h3><p>${esc(error.message)}</p><button class="button secondary" data-action="refresh">重试连接</button></div>`;}
})();
