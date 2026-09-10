'use strict';
let updateState=null,updateInstalling=false,updatePolling=false;
const settingsBeforeUpdates=settingsPage;
settingsPage=function(){return settingsBeforeUpdates()+`<section class="panel update-settings" id="update-settings"><div class="section-title"><h2>版本与更新</h2><span class="pill">v${esc(state.data.version)}</span></div><div id="update-panel-body">正在读取更新状态…</div></section>`;};
function updateDetails(u){
 const downloading=u.status==='downloading',ready=u.status==='ready',available=u.status==='available';
 return `<p role="status">${esc(u.message)}</p>${u.latest_version?`<p class="help-note">当前 v${esc(u.current_version)} · 最新 v${esc(u.latest_version)}</p>`:''}${downloading?`<progress max="${u.total||1}" value="${u.received||0}"></progress><small>已下载 ${((u.received||0)/1048576).toFixed(1)} / ${((u.total||0)/1048576).toFixed(1)} MB</small>`:''}${u.notes?`<details class="update-notes"><summary>本次更新内容</summary><p>${esc(u.notes)}</p></details>`:''}<div class="actions"><button class="button secondary" data-action="update-check" ${u.busy?'disabled':''}>检查更新</button>${available&&u.download_ready?'<button class="button" data-action="update-download">下载更新</button>':''}${ready?'<button class="button" data-action="update-confirm">更新并重启</button>':''}<a class="text-button" href="${esc(u.releases_url)}" target="_blank" rel="noopener noreferrer">查看发布历史 ↗</a></div><small class="update-privacy">每天自动检查一次。更新前备份，保留账号、作品和 API 配置；有任务运行时暂不安装。</small>${u.last_install?.status==='failed'?`<p class="form-error">上次更新未完成：${esc(u.last_install.message)}</p>`:''}`;
}
function paintUpdates(){
 if(!updateState)return;
 const panel=$('#update-panel-body');if(panel)panel.innerHTML=updateDetails(updateState);
 let notice=$('#update-notice');if(!notice){notice=document.createElement('aside');notice.id='update-notice';notice.className='update-notice';notice.setAttribute('aria-label','版本更新提醒');$('.topbar').insertAdjacentElement('afterend',notice);}
 notice.hidden=!updateState.notify||!['available','ready'].includes(updateState.status);
 if(!notice.hidden)notice.innerHTML=`<span><strong>灵思有新版本 v${esc(updateState.latest_version)}</strong>　${updateState.status==='ready'?'更新包已准备好':'看看这次有哪些改进'}</span><div class="actions"><button class="text-button" data-action="update-open">查看更新</button><button class="text-button" data-action="update-later">稍后提醒</button><button class="text-button" data-action="update-ignore">忽略此版本</button></div>`;
}
async function pollUpdates(){
 if(updateInstalling||updatePolling||document.hidden)return;updatePolling=true;
 try{updateState=await api('/api/updates');paintUpdates();}catch{if($('#update-panel-body'))$('#update-panel-body').textContent='暂时无法读取更新状态，稍后刷新重试。';}finally{updatePolling=false;}
}
const syncHeaderBeforeUpdates=syncPageHeader;
syncPageHeader=function(){syncHeaderBeforeUpdates();paintUpdates();};
Object.assign(actions,{
 'update-open':()=>{location.hash='#settings';setTimeout(()=>$('#update-settings')?.scrollIntoView({block:'center',behavior:'smooth'}),100);},
 'update-check':async()=>{updateState=await api('/api/updates/check',{});paintUpdates();},
 'update-download':async()=>{updateState=await api('/api/updates/download',{});paintUpdates();},
 'update-later':async()=>{updateState=await api('/api/updates/dismiss',{action:'later'});paintUpdates();},
 'update-ignore':async()=>{updateState=await api('/api/updates/dismiss',{action:'ignore'});paintUpdates();},
 'update-confirm':()=>modal('更新灵思',`<p>安装 v${esc(updateState.latest_version)}，完成后自动重启。</p><p>更新前会备份程序和数据库；账号、作品、登录状态和 API 配置保留。有进行中的任务时，会提示你先完成或取消。</p><div class="actions"><button class="button secondary" data-action="close-modal">稍后</button><button class="button" data-action="update-install">备份并更新</button></div>`),
 'update-install':async()=>{
  const previous=state.data.version,result=await api('/api/updates/install',{});updateInstalling=true;
  modal('正在更新',`<p role="status">${esc(result.message)}</p><progress aria-label="正在更新"></progress><p>请保留此页面，恢复服务后会自动刷新。</p>`);
  const started=Date.now();
  const wait=async()=>{
   try{const health=await fetch('/api/health',{cache:'no-store'}).then(r=>r.json());
    if(health.ok&&health.version!==previous){location.reload();return;}
    const status=await fetch('/api/updates',{cache:'no-store'}).then(r=>r.json());
    if(status.last_install?.status==='failed'&&status.last_install.time*1000>started){updateInstalling=false;modal('更新未完成',`<p>${esc(status.last_install.message)}</p><button class="button" data-action="close-modal">知道了</button>`);return;}
   }catch{}
   if(Date.now()-started>120000){updateInstalling=false;modal('服务正在恢复',`<p>更新等待时间较长，请稍后刷新。若仍无法打开，请双击「启动灵思」。</p>`);return;}
   setTimeout(wait,2000);
  };setTimeout(wait,2500);
 }
});
setTimeout(pollUpdates,1200);setInterval(pollUpdates,5000);
