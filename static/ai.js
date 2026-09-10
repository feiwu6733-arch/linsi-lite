'use strict';
const settingsBeforeAPI=settingsPage;
function aiProvider(base){try{const u=new URL(base);return u.hostname==='api.deepseek.com'?'deepseek':u.hostname==='api.openai.com'?'openai':['localhost','127.0.0.1','::1'].includes(u.hostname)?'local':'custom';}catch{return 'custom';}}
function aiSignature(c){return JSON.stringify([c.base_url,c.model,c.timeout,c.thinking_mode||'fast']);}
function aiVerified(c){const f=$('#ai-form');return state.aiVerified===aiSignature(c)&&(!f||(f.elements.base_url.value===c.base_url&&f.elements.model.value===c.model&&!f.elements.api_key.value&&!f.elements.clear_key.checked&&Number(f.elements.timeout.value)===c.timeout&&f.elements.thinking_mode.value===(c.thinking_mode||'fast')));}
function updateAIBadge(c){const b=$('#ai-settings .pill');if(!b)return;b.textContent=aiVerified(c)?'连接已验证':c.configured?'已保存 · 尚未验证':'待配置';b.className='pill '+(aiVerified(c)?'good':'waiting');}
function aiModelOptions(base,current){const models=state.aiModelCatalog?.base===base?state.aiModelCatalog.models:[];return '<option value="">'+(models.length?'从接口返回的模型中选择':'点击“获取模型列表”后选择')+'</option>'+models.map(m=>`<option value="${esc(m)}" ${m===current?'selected':''}>${esc(m)}</option>`).join('');}

settingsPage=function(){
 const c=state.data.ai||{};
 const panel=`<section class="panel ai-settings" id="ai-settings"><div class="section-title"><div><h2>AI 接口配置</h2><p>配置一次，在网页内直接完成作品拆解和账号研究。</p></div><span class="pill ${aiVerified(c)?'good':'waiting'}">${aiVerified(c)?'连接已验证':c.configured?'已保存 · 尚未验证':'待配置'}</span></div><form id="ai-form" autocomplete="off"><div class="form-grid"><label>接口预设<select id="ai-provider"><option value="custom">自定义兼容接口</option value="deepseek">DeepSeek</option><option value="openai">OpenAI</option><option value="local">本机模型（兼容接口）</option></select></label><label>接口地址（Base URL）<input id="ai-base" name="base_url" type="url" required value="${esc(c.base_url||'')}" placeholder="https://api.deepseek.com"></label><label>API Key<input id="ai-key" name="api_key" type="password" autocomplete="new-password" placeholder="${c.has_key?'密钥已保存，留空保留；不会回显':'填写服务商提供的 API Key'}"><small>仅保存在本机，不写入浏览器缓存或导出文件。</small></label><label>模型名称<input id="ai-model" name="model" list="ai-model-options" value="${esc(c.model||'')}" placeholder="填写服务商的模型 ID，或获取列表"><datalist id="ai-model-options"></datalist><select id="ai-model-select" aria-label="接口返回的可用模型">${aiModelOptions(c.base_url,c.model)}</select><small>填写接口模型 ID，不能直接填产品展示名称；获取列表后可直接选择。</small></label><label>DeepSeek 分析模式<select name="thinking_mode" id="ai-thinking">${[['fast','快速研究（推荐）'],['deep','深度思考（等待更久）'],['provider','使用服务商默认']].map(([v,label])=>`<option value="${v}" ${(c.thinking_mode||'fast')===v?'selected':''}>${label}</option>`).join('')}</select><small>只对 DeepSeek 官方接口生效，其他接口保持默认。</small></label><label>单次响应超时（秒）<input name="timeout" type="number" min="30" max="600" value="${c.timeout||180}"></label><label class="ai-key-clear"><input name="clear_key" type="checkbox">清除已保存密钥（用于无密钥本机服务）</label></div><p class="help-note">支持 Chat Completions 兼容接口。Base URL 可填写根地址或 /v1 地址，程序自动连接 /chat/completions。研究时只发送所选文案、评论和研究定位给你配置的服务；音视频转写仍在本机完成。测试连接会发起一次简短模型调用。</p><p class="form-error" role="alert"></p><p id="ai-feedback" role="status" aria-live="polite"></p><div class="actions"><button class="button" type="submit">保存 API 配置</button><button class="button secondary" type="button" data-action="ai-test">保存并测试连接</button><button class="button secondary" type="button" data-action="ai-models">获取模型列表</button><a class="text-button" href="#batches">继续已有研究 →</a></div></form></section>`;
 return settingsBeforeAPI().replace('<div class="settings-grid">',panel+'<div class="settings-grid">');
};
async function saveAIForm(){
 const form=$('#ai-form');if(!form.reportValidity())throw new Error('请检查接口地址。');
 const data=Object.fromEntries(new FormData(form));data.timeout=Number(data.timeout);data.clear_key=form.elements.clear_key.checked;
 const c=await api('/api/ai/config',data);if(data.api_key||data.clear_key||state.aiVerified!==aiSignature(c))state.aiVerified=null;state.data.ai=c;
 $('#ai-key').value='';$('#ai-key').placeholder=c.has_key?'密钥已保存，留空保留；不会回显':'填写服务商提供的 API Key';form.elements.clear_key.checked=false;
 updateAIBadge(c);
 return c;
}
async function loadAIModels(form){
 const c=state.data.ai,r=await api('/api/ai/models',{});
 if(!form.isConnected)return '';
 state.aiModelCatalog={base:c.base_url,models:r.models};
 $('#ai-model-options').innerHTML=r.models.map(m=>`<option value="${esc(m)}"></option>`).join('');
 let note='';const current=$('#ai-model').value.trim(),matches=r.models.filter(m=>m.toLowerCase()===current.toLowerCase());
 if(current&&matches.length===1&&matches[0]!==current){$('#ai-model').value=matches[0];await saveAIForm();note=`已将模型名称校正为 ${matches[0]}。`;}
 if(!form.isConnected)return note;
 $('#ai-model-select').innerHTML=aiModelOptions(c.base_url,$('#ai-model').value);
 return note+(r.models.length?`已获取 ${r.models.length} 个可用模型，可直接下拉选择。`:'服务未返回模型列表，请手动填写模型 ID。');
}
async function apiSettingAction(kind){
 const form=$('#ai-form');if(!form||form.dataset.busy)return;form.dataset.busy='1';
 const buttons=[...form.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
 form.querySelector('.form-error').textContent='';$('#ai-feedback').textContent=kind==='test'?'正在检查模型并测试响应…':kind==='models'?'正在获取模型列表…':'正在保存…';
 try{
  const c=await saveAIForm();if(!form.isConnected)return;
  if(kind==='test'){
   let note='';try{note=await loadAIModels(form);}catch{/* Some compatible services do not expose /models. */}
   if(!form.isConnected)return;
   $('#ai-feedback').textContent=(note?note+' ':'')+'正在测试模型响应…';
   const tested={...state.data.ai},r=await api('/api/ai/test',{});state.aiVerified=aiSignature(tested);
   if(!form.isConnected)return;
   updateAIBadge(state.data.ai);$('#ai-feedback').textContent=r.message+' 当前模型：'+tested.model+'。'+(note.startsWith('已将')?note:'');
  }
  else if(kind==='models'){$('#ai-feedback').textContent=await loadAIModels(form);}
  else $('#ai-feedback').textContent=c.configured?'配置已保存。可测试连接，或返回作品页开始研究。':'地址和密钥已保存，请获取模型列表或手动填写模型。';
 }catch(e){state.aiVerified=null;if(form.isConnected){updateAIBadge(state.data.ai);form.querySelector('.form-error').textContent=e.message;$('#ai-feedback').textContent='';}}
 finally{delete form.dataset.busy;buttons.forEach(b=>b.disabled=false);}
}
document.addEventListener('submit',e=>{if(e.target.id!=='ai-form')return;e.preventDefault();e.stopImmediatePropagation();apiSettingAction('save');},true);
document.addEventListener('change',e=>{
 if(e.target.id==='ai-base'){state.aiModelCatalog=null;$('#ai-model-options').innerHTML='';$('#ai-model-select').innerHTML=aiModelOptions('','');}
 if(e.target.id==='ai-model-select'&&e.target.value){$('#ai-model').value=e.target.value;$('#ai-feedback').textContent='模型已选好，点击“保存并测试连接”。';}
 if(e.target.id==='ai-provider'){
  const bases={deepseek:'https://api.deepseek.com',openai:'https://api.openai.com/v1',local:'http://127.0.0.1:11434/v1'};
  if(bases[e.target.value]&&$('#ai-base').value!==bases[e.target.value]){$('#ai-base').value=bases[e.target.value];$('#ai-model').value='';$('#ai-key').value='';state.aiModelCatalog=null;$('#ai-model-select').innerHTML=aiModelOptions('','');$('#ai-feedback').textContent='请填写该服务的密钥，再获取模型列表。';}
 }
 if(['ai-provider','ai-model-select','ai-model','ai-base','ai-key','ai-thinking'].includes(e.target.id)){state.aiVerified=null;updateAIBadge(state.data.ai);}
});
Object.assign(actions,{
 'ai-test':()=>apiSettingAction('test'),
 'ai-models':()=>apiSettingAction('models'),
 'report-run-ai':async()=>{if(!state.data.ai?.configured){location.hash='#settings';return;}await api('/api/reports/run-ai',bodyScope({id:state.report.id}));toast('当前报告已加入研究队列。');await refresh();},
 'report-cancel-ai':async()=>{await api('/api/reports/cancel-ai',bodyScope({id:state.report.id}));await refresh();}
});
