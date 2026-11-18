"use strict";
window.SearchBenchRanking = (() => {
  const el = id => document.getElementById(id);
  const escape = value => String(value ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const names = {ndcg_at_10:"NDCG@10",mrr_at_10:"MRR@10",success_at_1:"Success@1",success_at_3:"Success@3",success_at_10:"Success@10"};
  const hints = ["前十条整体排序","首个完整答案的位置","第一条有完整答案","前三条有完整答案","前十条有完整答案"];
  const reasons = {invalid_scores:"评分失败或无效",pending_scores:"评分未完成",simulated:"模拟评分",collection_failed:"采集失败",invalid_ranks:"排名异常",missing_url:"缺少 URL",ambiguous_response:"结果列表归属不明确",query_changed:"query 内容不同",incomplete_or_invalid:"评分不完整或结果无效",evidence_changed:"同 URL 内容变化",query_spec_changed:"需求解析不同"};
  let detail=null, jobs=[], baseline=null, version=0, busy=false, exportUrl=null;
  const active = job => ["running","queued"].includes(job.status);
  const value = (key,n) => n===null||n===undefined ? "—" : key.startsWith("success") ? `${(n*100).toFixed(1)}%` : n.toFixed(3);
  const groupName = g => `${g.search_run_id} · ${g.model_id||g.experiment_id} · 第 ${g.repeat+1} 次`;
  const runName = job => `${(job.dataset_path||job.dataset).split('/').pop()} · ${new Date(job.created_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})} · ${job.id.slice(0,6)}`;
  const excluded = counts => Object.entries(counts).map(([k,n])=>`${k.split('+').map(x=>reasons[x]||x).join(' / ')} ${n}`).join('；') || '无';
  async function get(url) {const r=await fetch(url);const data=await r.json();if(!r.ok)throw new Error(data.error||'指标加载失败');return data;}
  function resetComparison(){version++;busy=false;el('ranking-comparison').replaceChildren();el('ranking-error').hidden=true;if(exportUrl){URL.revokeObjectURL(exportUrl);exportUrl=null;}}
  function buttons(){el('compare-ranking').disabled=busy||!detail||active(detail.job)||!baseline?.groups?.length||!el('ranking-group').value;el('compare-ranking').textContent=busy?'正在计算…':'对比排序效果';}
  function fillGroups(id,groups){const previous=el(id).value;el(id).innerHTML=groups.map(g=>`<option value="${escape(g.id)}">${escape(groupName(g))}</option>`).join('');if(groups.some(g=>g.id===previous))el(id).value=previous;el(id).disabled=!groups.length;}
  function history(next){
    jobs=next;if(!detail)return;
    const previous=el('ranking-baseline').value;
    const eligible=jobs.filter(j=>j.id!==detail.job.id&&!active(j)&&!j.simulated&&j.schemes.some(s=>['answerability_only_v1','legacy_five_v1'].includes(s)));
    const html='<option value="">请选择已结束的运行记录</option>'+eligible.map(j=>`<option value="${escape(j.id)}">${escape(runName(j))}</option>`).join('');
    if(el('ranking-baseline').innerHTML!==html){el('ranking-baseline').innerHTML=html;if(eligible.some(j=>j.id===previous))el('ranking-baseline').value=previous;else if(previous){baseline=null;resetComparison();fillGroups('ranking-baseline-group',[]);}}
    buttons();
  }
  function cards(){
    const g=detail?.ranking?.groups.find(g=>g.id===el('ranking-group').value);if(!g)return;
    el('ranking-cards').innerHTML=Object.entries(names).map(([k,name],i)=>`<div class="ranking-card"><span>${name}</span><strong>${value(k,g.metrics[k])}</strong><small>${hints[i]}${i===0?' · 列表内':''}</small></div>`).join('');
    el('ranking-coverage').textContent=`${active(detail.job)?'运行中 · 临时统计':'运行已结束'} · 纳入 ${g.eligible_queries} / ${g.query_total} 个 query；其中空结果 ${g.empty_queries} 个、无正收益 ${g.zero_gain_queries} 个、重复 URL 占位 ${g.duplicate_slots} 个。排除：${excluded(g.excluded)}。`;
  }
  function render(next,historyJobs){
    if(detail?.job.id!==next.job.id){baseline=null;resetComparison();fillGroups('ranking-baseline-group',[]);el('ranking-baseline').value='';}
    detail=next;el('ranking-section').hidden=!next.ranking?.groups?.length;
    if(el('ranking-section').hidden)return;
    el('ranking-run').textContent=`当前运行：${runName(next.job)}\n${next.job.dataset_path}`;
    el('download-ranking').href=`/api/jobs/${next.job.id}/download/ranking_metrics.json`;
    fillGroups('ranking-group',next.ranking.groups);cards();history(historyJobs);
  }
  async function chooseBaseline(){
    resetComparison();baseline=null;fillGroups('ranking-baseline-group',[]);buttons();
    const id=el('ranking-baseline').value, ticket=version;if(!id)return;
    try{const data=await get(`/api/jobs/${id}/ranking`);if(ticket!==version)return;baseline=data;fillGroups('ranking-baseline-group',data.groups);buttons();}
    catch(e){if(ticket===version){el('ranking-error').textContent=e.message;el('ranking-error').hidden=false;}}
  }
  async function compare(){
    resetComparison();const ticket=version;busy=true;buttons();
    const params=new URLSearchParams({baseline:el('ranking-baseline').value,current_group:el('ranking-group').value,baseline_group:el('ranking-baseline-group').value});
    try{
      const r=await get(`/api/jobs/${detail.job.id}/ranking-comparison?${params}`);if(ticket!==version)return;
      const baseJob=jobs.find(j=>j.id===r.baseline_job_id);
      exportUrl=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)],{type:'application/json'}));
      el('ranking-comparison').innerHTML=`<div class="ranking-comparison-heading"><h3>共同候选池对比</h3><a href="${exportUrl}" download="ranking-comparison.json">导出对比 ↗</a></div><p class="field-help">基准：${escape(baseJob?runName(baseJob):r.baseline_job_id)}<br>当前：${escape(runName(detail.job))}</p><div class="ranking-table-wrap"><table class="ranking-table"><thead><tr><th>指标</th><th>基准</th><th>当前</th><th>变化</th></tr></thead><tbody>${Object.entries(names).map(([key,name])=>{const d=r.delta[key],change=d===null?'—':`${d>0?'+':''}${key.startsWith('success')?(d*100).toFixed(1)+' pp':d.toFixed(3)}`;return `<tr><th>${name}</th><td>${value(key,r.baseline[key])}</td><td>${value(key,r.current[key])}</td><td class="${d>0?'ranking-up':d<0?'ranking-down':''}">${change}</td></tr>`}).join('')}</tbody></table></div><p class="field-help">共同 query ${r.common_queries} 个，实际比较 ${r.compared_queries} 个。排除：${escape(excluded(r.excluded))}。仅当前运行包含 ${r.unmatched_current} 个，仅基准包含 ${r.unmatched_baseline} 个。pp 表示百分点。</p><p class="info-note">${r.shared_labels} 个共同 URL 中，${r.label_conflicts} 个评分冲突已统一使用基准分数。本表五个指标均按统一评分重算，可能与上方单次运行数值不同；未修改原始评分。候选池仅覆盖这两次返回的前十条，不代表全网理想结果。</p>${r.compared_queries?'':'<p class="warning-note">没有可比较的完整 query，指标显示为 —。</p>'}`;
    }catch(e){if(ticket===version){el('ranking-error').textContent=e.message;el('ranking-error').hidden=false;}}
    finally{if(ticket===version){busy=false;buttons();}}
  }
  function clear(){resetComparison();detail=null;baseline=null;el('ranking-section').hidden=true;}
  el('ranking-group').addEventListener('change',()=>{resetComparison();cards();buttons();});
  el('ranking-baseline').addEventListener('change',chooseBaseline);
  el('ranking-baseline-group').addEventListener('change',()=>{resetComparison();buttons();});
  el('compare-ranking').addEventListener('click',compare);
  return {render,history,clear};
})();
