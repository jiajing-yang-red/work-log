"use strict";
const $ = (id) => document.getElementById(id);
const state = {catalog: null, selected: null, job: null, page: 1, items: [], total: 0, busy: false, version: 0, discovering: false, discoveryVersion: 0, modelGroups: [], scoringChoices: new Map(), queryChoice: "", discoveryError: "", historyBusy: false, historyVersion: 0, undoJob: null, importing:false};
const promptState = {version:0, job:null, items:[], current:null, tab:"messages", response:null, responseLoading:false, responseError:"", attempt:0};
const promptConfig = {version:0, items:[], selected:{}, drafts:{}, loading:false, error:"", editor:null, saving:false};
const fileState = {version:0, data:null, timer:null};
const scopeState = {version:0, data:null, loading:false};
const calibrationState = {version:0, page:1};
const blockScheme = "block_content_needs_met_v1";
const blockGrades = ["FailsM", "SM", "MM", "HM", "FullyM"];
const isBlock = () => checkedSchemes().includes(blockScheme);
const labels = {block_content_needs_met:"Needs Met",answerability:"可回答", authoritative:"权威", freshness:"时效", noise:"噪声", duplicate:"重复", needs_met:"需求满足", page_quality:"页面质量"};
const schemeNames = {block_content_needs_met_v1:"Block Content Needs Met",answerability_only_v1:"可回答性 · 人工校准", legacy_five_v1:"旧体系 · 五维", google_overview_v1:"Google · NM / PQ"};
const schemeDimensions = {block_content_needs_met_v1:1,answerability_only_v1:1, legacy_five_v1:5, google_overview_v1:2};
const statuses = {queued:"排队中", running:"运行中", completed:"已完成", partial:"部分完成", failed:"运行失败", cancelled:"已停止", interrupted:"已中断", rated:"已评分", unjudgeable:"无法判断", not_applicable:"不适用", error:"评分失败", success:"成功", invalid_output:"输出不合规", partial_output:"部分输出有效", input_too_large:"输入超限"};
const reasons = {page_snapshot_missing:"缺少网页正文快照，无法评估页面质量。", page_snapshot_incomplete_or_unverified:"网页快照不完整或未经确认。", query_spec_missing_or_failed:"该 query 的需求解析缺失或失败。", query_requires_clarification:"查询存在需要澄清的歧义。", evaluation_time_missing:"缺少评估时间，无法判断时效。", collection_not_successful:"原始搜索结果未成功采集。", "SIMULATION ONLY: no semantic quality judgment was performed.":"模拟流程：未执行真实的语义质量判断。", "SIMULATION: First occurrence or exact URL rule.":"模拟流程：依据列表首条或完全相同 URL 的确定性规则。", "SIMULATION: Semantic comparison unavailable.":"模拟流程：未执行内容语义去重。"};
const active = (job) => job && ["queued","running"].includes(job.status);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const number = (n) => Number(n || 0).toLocaleString("zh-CN");
const shortDate = (s) => new Date(s).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
function error(message) { $("global-error").textContent = message; $("global-error").hidden = !message; }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json","X-Eval-Token":state.catalog.token}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}
function checkedModels() { return [...document.querySelectorAll('input[name="model"]:checked')].map(e=>e.value); }
function checkedSchemes() { return [...document.querySelectorAll('input[name="scheme"]:checked')].map(e=>e.value); }
function parsesQuery() { return $("parse-query").checked; }
function scoringSelections() {
  return Object.fromEntries([...document.querySelectorAll("select[data-score-profile]")].map(e=>[e.dataset.scoreProfile,e.value]));
}
function scoringReady() {
  const names=checkedModels(), selections=scoringSelections();
  return names.length===1 && names.every(name=>!!selections[name]);
}
function providerLabel(profile) { return {qianwen:"千问AI · qianwen",zhipu:"智谱 · GLM-4.7-Flash",ateve_jev:"Ateve-Jev · TokenDance"}[profile] || profile; }
function nativeScorer() { return checkedModels().some(name=>state.catalog.models.find(m=>m.id===name)?.protocol==="typesafe"); }
function queryProfiles() {
  return nativeScorer()?state.catalog.models.filter(m=>m.ready&&!m.simulated&&m.protocol!=="typesafe").map(m=>m.id):checkedModels();
}
function modelLabel(profile, modelId) { return modelId ? `${providerLabel(profile)} / ${modelId}` : providerLabel(profile); }
function jobModelLabel(job, profile) { return modelLabel(profile, job?.scoring_model_ids?.[profile]); }
function experimentLabel(job, id) {
  for(const name of job.models) for(const [scheme,label] of Object.entries(schemeNames)) {
    if(id===`${scheme}-${name}`) return `${label} · ${jobModelLabel(job,name)}`;
  }
  return id;
}
function historyTiming(job, now) {
  const timing=SearchBenchTiming.measure(job, now);
  return `${timing.label} · ${timing.text}`;
}
function renderRunTiming(now = Date.now()) {
  if(!state.job)return;
  const timing=SearchBenchTiming.measure(state.job, now);
  const stamp=value=>Number.isFinite(value)?new Date(value).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}):"—";
  $("run-elapsed-label").textContent=timing.label;
  $("run-elapsed").textContent=timing.text;
  $("run-started").textContent=stamp(timing.start);
  $("run-ended-label").textContent=timing.approximate?"最后记录":"结束时间";
  $("run-ended").textContent=timing.running?"进行中":stamp(timing.end);
  $("run-timing-note").textContent=timing.approximate?
    "此历史记录缺少结束时间，耗时仅统计至最后一次保存；实际耗时可能更长。":
    "从创建评测开始计时，包含需求解析、评分、等待和重试；并行任务耗时不累加。";
}
function refreshRunClocks() {
  const now=Date.now();
  if(active(state.job))renderRunTiming(now);
  for(const job of state.catalog?.jobs||[]) {
    if(!active(job))continue;
    const element=document.querySelector(`[data-job-duration="${CSS.escape(job.id)}"]`);
    if(element)element.textContent=historyTiming(job, now);
  }
}
function renderHistory(jobs) {
  state.catalog.jobs = jobs;
  $("history").innerHTML = jobs.length ? jobs.map(job=>`<div class="history-entry"><button class="history-item ${job.id===state.selected?"active":""}" data-job="${esc(job.id)}" ${state.historyBusy?"disabled":""} title="${esc(job.models.map(name=>jobModelLabel(job,name)).join(" + "))}"><strong>${job.simulated?"模拟":"真实"} · ${esc(job.models.map(name=>jobModelLabel(job,name)).join(" + "))} · ${number(job.query_count)} 条</strong><small><span>${esc(shortDate(job.created_at))}</span><span>${esc(statuses[job.status]||job.status)}</span></small><span class="history-duration" data-job-duration="${esc(job.id)}">${esc(historyTiming(job))}</span></button><button class="history-delete" data-delete-job="${esc(job.id)}" ${active(job)||state.historyBusy?"disabled":""} title="${active(job)?"请先停止评测后再删除":"删除此运行记录及评分结果，可撤销"}" aria-label="${esc(`删除运行记录：${shortDate(job.created_at)} · ${job.models.join(" + ")} · ${job.id.slice(0,6)}`)}">删除</button></div>`).join("") : '<p class="history-empty">还没有运行记录。<br>从右侧创建一次评测。</p>';
  $("history-undo").hidden = !state.undoJob;
  $("undo-delete").disabled = state.historyBusy;
  $("refresh-history").disabled = state.historyBusy;
  $("start-button").disabled = state.busy || state.importing || !scopeState.data?.selected.queries || !checkedSchemes().length || state.discovering || !scoringReady() || (parsesQuery() && !$("query-model").value) || jobs.some(active);
}
function renderDatasets(preferred) {
  let remembered=null, firstDefault=state.catalog.default_dataset;
  try {remembered=localStorage.getItem("searchbench-dataset");if(localStorage.getItem("searchbench-workspace-mode")===state.catalog.workspace_mode)firstDefault=null;} catch {}
  const selected=[preferred,$("dataset").value,firstDefault,remembered,state.catalog.default_dataset,"search-quality-input-20261020-v1"].find(
    id=>state.catalog.datasets.some(d=>d.id===id&&d.status==="ready"));
  $("dataset").innerHTML=state.catalog.datasets.length?state.catalog.datasets.map(d=>
    `<option value="${esc(d.id)}" title="${esc(d.path)}" ${d.status!=="ready"?"disabled":""}>${esc(d.name)} · ${number(d.queries)} 条查询</option>`
  ).join(""):'<option value="">请从本机加载数据集</option>';
  if(selected)$("dataset").value=selected;
  rememberDataset();
  updateDataset();
}
function rememberDataset() {
  try {if($("dataset").value){localStorage.setItem("searchbench-dataset",$("dataset").value);localStorage.setItem("searchbench-workspace-mode",state.catalog.workspace_mode);}} catch {}
}
function updateSchemeStats() {
  $("workspace-title").textContent=isBlock()?"Block Content Needs Met":"可回答性校准";
  $("workspace-eyebrow").textContent=isBlock()?"RESULT BLOCK EVALUATION":"ANSWERABILITY CALIBRATION";
  $("workspace-description").textContent=isBlock()?"按完整五档规则，评价搜索结果块已展示的内容对 query 的帮助。":"以人工评分为参考，检查模型对每条搜索结果的判断。";
  $("workspace-version").textContent=isBlock()?"skill 2.1.1 · 0–4":"可回答性";
  const schemes=checkedSchemes();
  $("stat-schemes").textContent=number(schemes.length);
  $("stat-dimensions").textContent=` / ${schemes.reduce((sum,id)=>sum+schemeDimensions[id],0)} 个子指标`;
  $("stat-scheme-note").textContent=schemes.map(id=>schemeNames[id]).join(" + ")||"请至少勾选一套方案";
}
async function updateScope() {
  const version=++scopeState.version;
  const d=state.catalog.datasets.find(item=>item.id===$("dataset").value);
  const limit=$("limit").value?Number($("limit").value):null;
  scopeState.data=null;scopeState.loading=!!d;
  $("scope-stats").setAttribute("aria-busy",String(scopeState.loading));
  $("refresh-scope").disabled=scopeState.loading||!d;
  $("scope-dataset").textContent=d?`数据集：${d.name}`:"请选择数据集";
  $("scope-dataset").title=d?.path||"";
  for(const id of ["stat-queries","stat-results","stat-pages"])$(id).textContent="—";
  for(const id of ["stat-query-note","stat-result-note","stat-page-note"])$(id).textContent=d?"正在统计…":"选择数据集后统计";
  $("scope-status").className="scope-status";
  $("scope-status").textContent=d?"正在按所选查询读取实际结果数量…":"加载数据集后可预览本次评测范围。";
  renderHistory(state.catalog.jobs);
  if(!d)return;
  try {
    const data=await api("/api/datasets/preview",{dataset:d.id,limit});
    if(version!==scopeState.version)return;
    scopeState.data=data;
    const s=data.selected,t=data.total;
    $("stat-queries").textContent=number(s.queries);
    $("stat-results").textContent=number(s.results);
    $("stat-pages").textContent=number(s.human_labels);
    $("stat-query-note").textContent=`数据集共 ${number(t.queries)} 条 · ${limit===null?"已选全部":`取前 ${number(s.queries)} 条`}`;
    $("stat-result-note").textContent=`数据集共 ${number(t.results)} 条 · 保留重复 URL`;
    $("stat-page-note").textContent=`覆盖 ${number(s.human_labels)} / ${number(s.results)} 条结果`;
    $("scope-status").textContent=(s.queries_without_results?`所选查询中 ${number(s.queries_without_results)} 条无搜索结果。`:"")+
      "随下方配置更新；历史运行结果见“运行与结果概览”。";
  } catch(err) {
    if(version!==scopeState.version)return;
    for(const id of ["stat-query-note","stat-result-note","stat-page-note"])$(id).textContent="统计未完成";
    $("scope-status").classList.add("failed");
    $("scope-status").textContent=`统计失败：${err.message} 可点击“重新统计”重试。`;
  } finally {
    if(version===scopeState.version){
      scopeState.loading=false;$("scope-stats").setAttribute("aria-busy","false");
      $("refresh-scope").disabled=false;renderHistory(state.catalog.jobs);
    }
  }
}
function updateDataset() {
  const d = state.catalog.datasets.find(d=>d.id===$("dataset").value);
  $("dataset-location").hidden = !d;
  $("dataset-path").textContent = d?.path || "";
  $("dataset-source-location").hidden=!d?.source_path;
  $("dataset-source-path").textContent=d?.source_path||"";
  $("dataset-name").textContent = d?.name || "";
  $("dataset-name-note").textContent = "ID 来自数据集的 manifest.json。新导入可自定义名称，默认使用源文件名。";
  $("dataset").title = d?.path || "";
  $("dataset-info").textContent = d ? `${number(d.queries)} 条查询 · ${number(d.results)} 条结果 · ${d.empty_queries} 条查询为空结果` : "选择本机文件或填写完整路径，加载后即可评测。";
  if(d?.reference_labels_by_source?.jev)$("dataset-info").textContent+=` · 参考标签：人工 ${number(d.reference_labels_by_source.human||0)} 条，Jev ${number(d.reference_labels_by_source.jev)} 条（模型标签）。当前人工一致率只统计人工部分。`;
  $("mode").options[1].disabled = !d?.pages;
  if (!d?.pages) $("mode").value = "api_only";
  $("evidence-note").textContent = d?.reference_labels ? "使用保存的 API 内容评分。人工/Jev 参考分数和理由均不传给评分模型；本次也不加载历史 Golden Answer。人工一致率仅统计人工标签。" : (isBlock()?"仅使用 query、title、snippet、URL；不使用排序、features 或 teacherScore。":"依据所选方案与证据模式评分。");
  updateScope();
  loadCalibration(true);
}
async function loadCalibration(reset=false) {
  const version=++calibrationState.version;
  const d=state.catalog.datasets.find(d=>d.id===$("dataset").value);
  $("calibration-dataset").hidden=!d?.reference_labels;
  if(reset){calibrationState.page=1;$("calibration-query").innerHTML='<option value="">全部 Query</option>';}
  if(!d?.reference_labels)return;
  $("calibration-load-status").textContent="正在读取人工样本…";
  $("calibration-rows").replaceChildren();
  $("calibration-prev").disabled=true;$("calibration-next").disabled=true;
  try {
    const data=await api("/api/datasets/references",{dataset:d.id,page:calibrationState.page,query_id:$("calibration-query").value});
    if(version!==calibrationState.version)return;
    $("calibration-dataset").hidden=!data.total_references;
    if(!data.total_references)return;
    if(reset)$("calibration-query").innerHTML='<option value="">全部 Query</option>'+data.queries.map(q=>`<option value="${esc(q.query_id)}">${esc(q.query_id)} · ${esc(q.query)}</option>`).join("");
    calibrationState.page=data.page;
    $("calibration-size").textContent=`${number(data.queries.length)} 条 Query · ${number(data.total_references)} 条人工评分`;
    $("calibration-dataset-note").textContent="保留原始人工分数和理由，用于可回答性校准。历史标签尚未按 v10 重新复核；下方分布展示各分档覆盖量。";
    $("calibration-distribution").innerHTML=[-1,0,1,2,3].map(g=>`<span class="distribution-value">${g} 分 <b>${number(data.distribution[String(g)])}</b></span>`).join("");
    $("calibration-rows").innerHTML=data.items.map(r=>`<tr><td><strong class="result-title">#${r.rank} ${esc(r.title||"无标题")}</strong><div class="result-query">${esc(r.query_id)} · ${esc(r.query)}</div><div class="reference-url">${esc(r.url)}</div><details class="source-block"><summary>查看原始内容</summary><pre>${esc(r.snippet||"无文本内容")}</pre></details></td><td><span class="score-chip">${r.reference.grade} 分</span></td><td>${esc(r.reference.reason||"未填写人工理由")}</td></tr>`).join("");
    $("calibration-page").textContent=`共 ${number(data.total)} 条 · 第 ${data.page} / ${Math.max(1,Math.ceil(data.total/20))} 页`;
    $("calibration-prev").disabled=data.page<=1;$("calibration-next").disabled=data.page*20>=data.total;
    $("calibration-load-status").textContent="人工标签与输入证据独立保存，原始 rank 保持不变。";
  } catch(err){if(version===calibrationState.version)$("calibration-load-status").textContent=`读取失败：${err.message}`;}
}
function renderCalibration(detail) {
  const c=detail.calibration;
  $("calibration-results").hidden=!c?.reference_count||!c.experiments.length;
  if(!c?.reference_count)return;
  $("calibration-metrics").innerHTML=c.experiments.map(e=>`<div class="calibration-experiment"><strong>${esc(experimentLabel(detail.job,e.experiment_id))} · 第 ${e.repeat+1} 次</strong><div class="calibration-totals"><span>全量同分率 <b>${e.exact_over_all===null||e.exact_over_all===undefined?"—":(e.exact_over_all*100).toFixed(1)+"%"}</b></span><span>已对照 <b>${e.compared} / ${e.reference_count}</b></span><span>同分 / 分歧 <b>${e.agreed} / ${e.disagreed}</b></span></div><p class="field-help">目标 95% · ${e.target_met?"本次已达到":"本次尚未达到"}。分母包括全部 ${e.reference_count} 条人工样本；未比较 ${e.uncompared} 条（未完成、失败或模拟）。有效评分同分率：${e.agreement===null?"—":(e.agreement*100).toFixed(1)+"%"}。</p><div class="distribution-values">${[-1,0,1,2,3].map(g=>{const s=e.by_human_grade[String(g)];return `<span class="distribution-value">人工 ${g} 分：同分 ${s.agreed} / ${s.compared}</span>`;}).join("")}</div></div>`).join("");
  $("download-calibration").href=`/api/jobs/${detail.job.id}/download/calibration.json`;
}
function humanBadge(r) {
  if(!r.human_reference)return "";
  return `<div class="human-comparison"><span>人工 <b>${r.human_reference.grade}</b></span><span class="${r.human_agreement===false?"reference-mismatch":""}">${r.human_agreement===null?"未比较":r.human_agreement?"与人工一致":"与人工不同"}</span></div>`;
}
function dimensionOptions(job) {
  const available=new Set((job?.schemes||["answerability_only_v1"]).flatMap(s=>s===blockScheme?["block_content_needs_met"]:s==="legacy_five_v1"?["answerability","authoritative","freshness","noise","duplicate"]:s==="google_overview_v1"?["needs_met","page_quality"]:["answerability"]));
  $("filter-dimension").innerHTML='<option value="">全部指标</option>'+Object.entries(state.catalog.dimensions).filter(([key])=>available.has(key)).map(([key,label])=>`<option value="${esc(key)}">${esc(label)}</option>`).join("");
}
async function loadDataset() {
  if(state.importing)return;
  state.importing=true;
  const status=$("dataset-load-status");
  status.hidden=false;status.className="dataset-load-status";status.textContent="正在检查文件并加载数据集…";
  for(const id of ["load-dataset","browse-dataset","dataset"])$(id).disabled=true;
  renderHistory(state.catalog.jobs);
  try {
    const policy=$("dataset-time-policy").value;
    const data=await api("/api/datasets",{
      path:$("dataset-input-path").value,name:$("dataset-import-name").value,
      time_policy:policy,evaluation_as_of:policy==="explicit"?$("dataset-as-of").value:null,
      mapping_path:$("dataset-mapping-path").value
    });
    if(data.status!=="ready") {
      const issues=(data.report?.issues||[]).filter(i=>i.severity==="error");
      status.classList.add("failed");
      status.textContent=`导入未通过检查，请根据诊断修正输入。\n${issues.map(i=>`${i.code}：${i.message}`).join("\n")}\n完整报告：${data.report_path}`;
      if(issues.some(i=>i.code==="EVALUATION_TIME_UNAVAILABLE"))status.textContent+="\n文件没有采集时间。仅评可回答性时，可在“导入选项”将评估时间来源改为“暂不指定”，然后重新加载；不要用网页发布时间或标注时间代替。";
      return;
    }
    state.catalog.datasets=data.datasets;renderDatasets(data.dataset.id);rememberDataset();
    const warnings=(data.report?.issues||[]).filter(i=>i.severity==="warning");
    status.textContent=`已${data.imported?"导入并选中":"选中"}「${data.dataset.name}」：${number(data.dataset.queries)} 条 Query、${number(data.dataset.results)} 条结果。`+
      (warnings.length?`\n检查提示：${warnings.slice(0,3).map(i=>`${i.code}：${i.message}`).join("\n")}`:"");
  } catch(err) {status.classList.add("failed");status.textContent=err.message;}
  finally {
    state.importing=false;
    for(const id of ["load-dataset","browse-dataset","dataset"])$(id).disabled=false;
    renderHistory(state.catalog.jobs);
  }
}
async function browseFiles(path, page=1) {
  const version=++fileState.version;
  fileState.data=null;
  $("file-error").hidden=true;$("file-list").textContent="正在读取目录…";$("file-page-info").textContent="";
  for(const id of ["choose-directory","file-parent","file-prev","file-next"])$(id).disabled=true;
  try {
    const data=await api("/api/datasets/browse",{path,page,filter:$("file-filter").value});
    if(version!==fileState.version||!$("file-dialog").open)return;
    fileState.data=data;$("file-directory").value=data.path;
    $("file-list").innerHTML=data.items.length?data.items.map((item,i)=>
      `<button type="button" class="file-entry" data-file-index="${i}"><span class="file-kind">${item.kind==="directory"?"目录":"文件"}</span><span class="file-name">${esc(item.name)}</span><small>${item.bundle?"标准化数据集":item.kind==="directory"?"进入 →":"选择"}</small></button>`
    ).join(""):'<p class="file-empty">没有匹配的文件或目录。支持 SQLite、JSON、JSONL、CSV。</p>';
    $("file-page-info").textContent=`${number(data.total)} 项 · 第 ${data.page} / ${data.pages} 页`;
    $("file-parent").disabled=data.path===data.parent;
    $("file-prev").disabled=data.page<=1;$("file-next").disabled=data.page>=data.pages;
    $("choose-directory").disabled=!data.bundle;
  } catch(err) {
    if(version!==fileState.version)return;
    $("file-list").textContent="";$("file-error").hidden=false;$("file-error").textContent=err.message;
  }
}
function openFileBrowser() {
  $("file-filter").value="";
  const d=state.catalog.datasets.find(d=>d.id===$("dataset").value);
  const path=$("dataset-input-path").value.trim()||d?.source_path||d?.path||"";
  $("file-directory").value=path;
  $("file-dialog").showModal();browseFiles(path);
}
function chooseFile(path) {
  $("dataset-input-path").value=path;
  $("file-dialog").close();$("load-dataset").focus();
  $("dataset-load-status").hidden=false;$("dataset-load-status").className="dataset-load-status";
  $("dataset-load-status").textContent="路径已选择，点击“加载数据集”完成检查。";
}
function renderScoringModels() {
  const selected = checkedModels();
  $("refresh-scoring-models").disabled = !selected.length || state.discovering;
  $("scoring-model-options").innerHTML = selected.map(name=>{
    const group=state.modelGroups.find(g=>g.profile===name);
    const models=group?.status==="ready"?group.models:[];
    const available=models.filter(m=>m.selectable);
    let options, note="";
    if(state.discovering) options='<option value="">正在获取接口型号…</option>';
    else if(available.length) {
      options=models.map(m=>`<option value="${esc(m.id)}" ${m.selectable?"":"disabled"}>${esc(m.id)}${m.reason?` · ${esc(m.reason)}`:""}</option>`).join("");
      note=group.source==="simulation"?"本地模拟型号，无网络请求。":group.source==="configured"?
        (name==="zhipu"?"预置官方免费型号 glm-4.7-flash；开启思考、JSON 输出、并发 1。账户可用性与限额以智谱平台为准。":
        `本地预置 ${models.length} 个型号，未查询 /models。未验证账户权限及免费额度，请选择账户有额度的型号。`):
        `接口返回 ${models.length} 个型号 · GET ${group.endpoint}`;
      if(name==="ateve_jev")note+="。使用独立的 Jev 可回答性 Prompt：共同规则在 instructions，五档定义在 criteria；返回 −1～3 分及概率，不生成文字理由或引文。";
    } else {
      options='<option value="">未获取到可用型号，请刷新</option>';
      note=group?.error||state.discoveryError||"接口没有返回可用于评分的型号。";
    }
    return `<div class="scoring-model-card"><label class="field-label" for="score-model-${esc(name)}">${esc(providerLabel(name))} · 评分型号</label><select id="score-model-${esc(name)}" data-score-profile="${esc(name)}" ${state.discovering||!available.length?"disabled":""}>${options}</select>${note?`<p class="field-help ${available.length?"":"model-list-error"}">${esc(note)}</p>`:""}</div>`;
  }).join("");
  if(!state.discovering) for(const input of document.querySelectorAll("select[data-score-profile]")) {
    const group=state.modelGroups.find(g=>g.profile===input.dataset.scoreProfile);
    const available=group?.status==="ready"?group.models.filter(m=>m.selectable):[];
    if(!available.length) continue;
    const previous=state.scoringChoices.get(group.profile);
    input.value=available.some(m=>m.id===previous)?previous:(available.find(m=>m.id===group.default_model)||available[0]).id;
    state.scoringChoices.set(group.profile,input.value);
  }
  $("scoring-model-status").textContent = !selected.length?"请先选择一个评分接口。":state.discovering?"正在获取接口型号列表…":"每次评测使用一个评分型号；需求解析型号可单独选择。";
}
function renderQueryModels() {
  const selected=queryProfiles();
  $("query-module-note").textContent = parsesQuery()
    ? "先解析需求并冻结需求说明，再交给评分模型。"
    : "已关闭：评分模型直接理解原始 query，不调用需求解析模型。";
  $("flow-query").textContent = parsesQuery() ? "解析 query" : "原始 query";
  $("query-model-status").className = "field-help";
  $("refresh-models").disabled = !parsesQuery() || !selected.length || state.discovering;
  $("query-model").disabled = true;
  if (!parsesQuery()) {
    $("query-model").innerHTML = '<option value="">已关闭，无需选择</option>';
    $("query-model-status").textContent = "跳过独立需求解析，直接进入 URL 评分。";
    return;
  }
  if(state.discovering||!selected.length) {
    $("query-model").innerHTML=`<option value="">${state.discovering?"正在获取接口模型…":"请先选择评分接口"}</option>`;
    $("query-model-status").textContent=state.discovering?"正在调用所选接口的模型列表…":"选择后，将从对应接口获取可用型号。";
    return;
  }
  const choices=[];
  $("query-model").innerHTML=state.modelGroups.filter(g=>selected.includes(g.profile)&&g.status==="ready"&&g.models.length).map(group=>{
    const options=group.models.map(model=>{
      const value=JSON.stringify([group.profile,model.id]);
      if(model.selectable) choices.push({value,preferred:model.id===group.default_model});
      return `<option value="${esc(value)}" ${model.selectable?"":"disabled"}>${esc(model.id)}${model.reason?` · ${esc(model.reason)}`:""}</option>`;
    }).join("");
    return `<optgroup label="${esc(group.profile)}${group.source==="simulation"?" · 模拟":""}">${options}</optgroup>`;
  }).join("");
  if(choices.length) {
    $("query-model").disabled=false;
    $("query-model").value=choices.some(c=>c.value===state.queryChoice)?state.queryChoice:(choices.find(c=>c.preferred)||choices[0]).value;
    state.queryChoice=$("query-model").value;
    $("query-model-status").textContent=state.modelGroups.some(g=>selected.includes(g.profile)&&g.source==="configured")?
      "可与评分型号不同；部分接口使用本地预置型号，账户权限与额度以平台为准。":
      "使用所选接口返回的全部可用型号，可与评分型号不同。";
    if(nativeScorer())$("query-model-status").textContent="Jev 负责评分；需求解析可选下列已配置的生成式模型，或关闭该模块。";
  } else {
    $("query-model").innerHTML='<option value="">未获取到可用模型，请刷新</option>';
    $("query-model-status").textContent=state.discoveryError||"请查看评分接口的获取结果并刷新重试。";
    $("query-model-status").className="field-help model-list-error";
  }
}
async function updateModels() {
  const selected=checkedModels();
  const simulated=selected.length&&selected.every(name=>state.catalog.models.find(m=>m.id===name).simulated);
  $("run-cost-note").textContent = !selected.length ? "请选择一个可用评分模型。" : simulated ? "模拟运行 · 无 API 调用，仅验证流程。" : "真实模型 · 开始后会调用模型 API，产生相应用量。";
  const version=++state.discoveryVersion;
  state.discovering=selected.length>0;
  state.modelGroups=[];
  state.discoveryError="";
  renderScoringModels(); renderQueryModels();
  renderHistory(state.catalog.jobs);
  if (!selected.length) return;
  try {
    const profiles=[...new Set([...selected,...(parsesQuery()?queryProfiles():[])])];
    const data = await api("/api/models/discover", {profiles});
    if (version !== state.discoveryVersion) return;
    state.modelGroups=data.groups;
  } catch (err) {
    if (version !== state.discoveryVersion) return;
    state.discoveryError=err.message;
  } finally {
    if (version === state.discoveryVersion) {
      state.discovering = false;
      renderScoringModels(); renderQueryModels();
      renderHistory(state.catalog.jobs);
    }
  }
}
function initControls() {
  renderDatasets();
  const models = state.catalog.models.filter(m=>!m.simulated).sort((a,b)=>Number(b.ready)-Number(a.ready));
  const initial = models.find(m=>m.id===state.catalog.recommended_model&&m.ready) || models.find(m=>m.id==="deepseek"&&m.ready) || models.find(m=>m.id==="qianwen"&&m.ready) || models.find(m=>m.ready);
  $("model-options").innerHTML = models.map(m=>`<label class="model-choice" title="${esc(m.reason||m.model)}"><input name="model" type="radio" value="${esc(m.id)}" required ${m.id===initial?.id?"checked":""} ${m.ready?"":"disabled"}><span>${esc(providerLabel(m.id))}</span><small>${m.simulated?"模拟":m.ready?"已配置":"未配置"}</small></label>`).join("");
  const qianwen=models.find(m=>m.id==="qianwen");
  $("qianwen-key-note").hidden=!qianwen||qianwen.ready;
  $("qianwen-key-note").textContent=qianwen&&!qianwen.ready?`启用千问AI：在 ${state.catalog.key_file} 填写 QIANWEN_API_KEY，保存后刷新页面。`:"";
  const zhipu=models.find(m=>m.id==="zhipu");
  $("zhipu-key-note").hidden=!zhipu||zhipu.ready;
  $("zhipu-key-note").textContent=zhipu&&!zhipu.ready?`启用 GLM-4.7-Flash：在 ${state.catalog.key_file} 填写 ZHIPU_API_KEY（智谱 BigModel 平台），保存后刷新页面。`:"";
  const jev=models.find(m=>m.id==="ateve_jev");
  $("jev-key-note").hidden=!jev||jev.ready;
  $("jev-key-note").textContent=jev&&!jev.ready?`启用 Ateve-Jev：在 ${state.catalog.key_file} 填写 ATEVE_JEV_API_KEY（TokenDance 平台），保存后刷新页面。`:"";
  dimensionOptions();
  updateSchemeStats(); updateModels();
}
function valueLabel(key, item) {
  if (item.judgment_status === "simulated") return "仅模拟";
  if (item.judgment_status === "error") return item.failure?.label || "评分失败";
  if (key==="block_content_needs_met" && item.judgment_status==="unjudgeable") return item.task_status==="input_too_large"?"输入超限":"证据不足";
  if (item.judgment_status !== "rated") return {unjudgeable:"未知",not_applicable:"不适用",error:"失败"}[item.judgment_status] || "未知";
  if (typeof item.value === "boolean") return item.value ? "是" : "否";
  return key==="block_content_needs_met" ? `${blockGrades[item.value]} · ${item.value}` : String(item.value);
}
function scoreClass(key,item) {
  if (item.judgment_status==="error") return "bad";
  if (item.judgment_status!=="rated") return "unknown";
  if (["noise","duplicate"].includes(key)) return item.value?"bad":"";
  if (["authoritative","freshness"].includes(key)) return item.value?"":"bad";
  return (typeof item.value==="number"&&item.value<=0)||["FailsM","Lowest","Low"].includes(item.value)?"bad":"";
}
function renderAnswerabilityDistributions(detail) {
  const groups = [...(detail.answerability_distributions || []), ...(detail.block_needs_met_distributions || [])];
  $("answerability-distribution-title").textContent=detail.block_needs_met_distributions?.length?"Needs Met 五档分布":"可回答性评分分布";
  $("answerability-distribution").hidden = !groups.length;

  const colors = ["#be6c60", "#9baab5", "#dbad57", "#6ca99b", "#287459"];
  const excludedNames = {error:"评分失败", unjudgeable:"证据不足 / 无法判断", input_too_large:"输入超限", not_applicable:"不适用", simulated:"仅模拟", invalid:"无效评分"};
  $("answerability-charts").innerHTML = groups.map(group=>{
    const grades = group.dimension==="block_content_needs_met" ? [0,1,2,3,4] : [-1,0,1,2,3];
    const gradeLabel = grade => group.dimension==="block_content_needs_met" ? `${blockGrades[grade]} · ${grade}` : `${grade} 分`;
    let offset = 0;
    const shares = grades.map(grade=>group.rated ? 100 * group.counts[String(grade)] / group.rated : 0);
    const segments = grades.map((grade,i)=>{
      const start = offset;
      offset += shares[i];
      if (!shares[i]) return "";
      return `<circle cx="90" cy="90" r="70" pathLength="100" fill="none" stroke="${colors[i]}" stroke-width="22" stroke-dasharray="${shares[i]} ${100-shares[i]}" stroke-dashoffset="${-start}" transform="rotate(-90 90 90)"><title>${gradeLabel(grade)}：${number(group.counts[String(grade)])} 条（${shares[i].toFixed(1)}%）</title></circle>`;
    }).join("");
    const omitted = Object.entries(group.excluded).filter(([,count])=>count).map(([status,count])=>`${excludedNames[status] || status} ${number(count)} 条`);
    if (group.pending) omitted.push(`未评分 ${number(group.pending)} 条`);
    const legend = grades.map((grade,i)=>`<div class="grade-legend-row"><span><i style="background:${colors[i]}" aria-hidden="true"></i>${gradeLabel(grade)}</span><span>${number(group.counts[String(grade)])}</span><strong>${group.rated ? shares[i].toFixed(1)+"%" : "—"}</strong></div>`).join("");
    const accessible = group.rated ? grades.map((grade,i)=>`${gradeLabel(grade)} ${group.counts[String(grade)]} 条，占 ${shares[i].toFixed(1)}%`).join("；") : "暂无有效评分";
    return `<article class="answerability-chart">
      <div class="answerability-chart-heading"><h4>${esc(experimentLabel(detail.job,group.experiment_id))}</h4>${detail.job.repeats>1?`<span>第 ${group.repeat+1} 轮</span>`:""}</div>
      <div class="answerability-chart-body">
        <div class="score-donut"><svg viewBox="0 0 180 180" role="img" aria-label="${esc(accessible)}"><circle cx="90" cy="90" r="70" fill="none" stroke="#edf1ee" stroke-width="22"/>${segments}</svg><div class="score-donut-center" aria-hidden="true"><strong>${group.rated ? number(group.rated) : "—"}</strong><span>${group.rated ? "有效评分" : "暂无有效评分"}</span></div></div>
        <div class="grade-legend"><div class="grade-legend-row grade-legend-heading"><span>分数</span><span>条数</span><span>占比</span></div>${legend}</div>
      </div>
      <p class="chart-coverage">有效评分 ${number(group.rated)} / ${number(group.expected)} 条结果</p>
      ${omitted.length?`<p class="chart-excluded">未计入环形图：${omitted.join(" · ")}</p>`:""}
    </article>`;
  }).join("");
}
function renderDistributions(metrics, hasAnswerabilityChart) {
  const entries = Object.entries(metrics).filter(([key])=>!hasAnswerabilityChart || !["answerability","block_content_needs_met"].includes(key));
  $("other-distributions").hidden = hasAnswerabilityChart && !entries.length;
  $("distributions").innerHTML = entries.map(([key,m])=>{
    const values = Object.entries(m.distribution).map(([v,n])=>`<span class="distribution-value">${esc(v==="true"?"是":v==="false"?"否":v.replaceAll('"',""))} <b>${number(n)}</b></span>`);
    for (const [status,n] of Object.entries(m.statuses)) if (status!=="rated") values.push(`<span class="distribution-value unknown">${esc(status==="simulated"?"仅模拟":statuses[status]||status)} ${number(n)}</span>`);
    return `<div class="distribution"><span class="distribution-label">${esc(state.catalog.dimensions[key])}</span><div class="distribution-values">${values.join("")}</div></div>`;
  }).join("") || '<p class="field-help">完成 URL 评分后会显示分布。</p>';
}
function renderJob(detail) {
  const job = detail.job;
  state.job = job;
  renderRunTiming();
  $("idle-state").hidden = true; $("active-state").hidden = false;
  $("run-badge").textContent = statuses[job.status] || job.status;
  $("run-badge").className = "badge " + (active(job)?"running":job.status==="completed"?"success":job.status==="failed"?"error":"warning");
  $("run-title").textContent = `${job.models.map(name=>jobModelLabel(job,name)).join(" + ")} · ${number(job.query_count)} 条查询 · ${number(job.result_count)} 条结果`;
  const direct = job.query_understanding==="direct";
  $("run-subtitle").textContent = `${shortDate(job.created_at)} · ${job.mode==="api_only"?"API 返回内容":"网页正文"} · 每组 ${job.repeats} 次 · ${direct?"需求解析已关闭 · 直接评分":`需求解析：${job.query_model_id||job.query_model}`}`;
  $("run-prompt-version").textContent = `Prompt：${Object.values(job.prompt_versions||{}).map(v=>v.name).join(" / ")||"运行时的内置版本"}`;
  $("cancel-button").hidden = !active(job);
  $("resume-button").hidden = !(job.schemes.includes(blockScheme) && ["cancelled","interrupted","failed","partial"].includes(job.status));
  $("download-native").hidden = !job.schemes.includes(blockScheme);
  $("download-native").href = `/api/jobs/${job.id}/download/block_content_needs_met.jsonl`;
  $("simulation-note").hidden = !job.simulated;
  const stageIndex = {prepare:0,plan:1,evaluate:1,finished:2}[job.stage]??0;
  document.querySelectorAll("#stages span").forEach((e,i)=>{
    e.className=direct&&i===0?"skipped":i===stageIndex?"current":i<stageIndex?"done":"";
    if(i===0) e.innerHTML=direct?"<b>−</b> 需求解析已跳过":"<b>1</b> 解析需求";
  });
  $("progress-text").textContent = {prepare:"正在解析并冻结 query 需求",plan:"正在规划评分任务",evaluate:"正在逐项评估搜索结果",finished:"本次运行已结束"}[job.stage];
  $("progress-count").textContent = job.total ? `${number(job.done)} / ${number(job.total)}` : "—";
  $("progress-fill").style.width = `${job.total ? Math.min(100,100*job.done/job.total) : 0}%`;
  $("run-message").textContent = job.message || (job.stage==="prepare"?"需求解析完成后，所有模型将共用同一份需求说明。":"任务完成即保存；可在下方查看已完成记录。");
  $("count-rated").textContent = number(detail.statuses.rated);
  $("count-unknown").textContent = number(detail.statuses.unjudgeable);
  $("unknown-counter").hidden = !detail.statuses.unjudgeable && !job.schemes.includes("google_overview_v1") && !job.schemes.includes(blockScheme);
  const unknownOption=[...$("filter-status").options].find(option=>option.value==="unjudgeable");
  unknownOption.hidden=$("unknown-counter").hidden;
  unknownOption.disabled=unknownOption.hidden;
  if(unknownOption.hidden&&$("filter-status").value==="unjudgeable")$("filter-status").value="";
  $("count-error").textContent = number(detail.statuses.error);
  const failures = detail.failures || [];
  const repairs = detail.evidence_repairs || {};
  const recovery = detail.auto_recovery || {};
  $("validation-summary").hidden = !failures.length && !repairs.count && !recovery.initial_failures;
  $("validation-summary").innerHTML = failures.map(f=>`<p><strong>${esc(f.label)}</strong> · ${number(f.tasks)} 个任务影响 ${number(f.records)} 项评分。${esc(f.description)}</p>`).join("") +
    (failures.length ? "<p>这些项目没有可用评分；失败不等于 URL 质量差。点击下方结果可查看具体原因。</p>" : "") +
    (recovery.initial_failures ? `<p>自动恢复：首次失败 ${number(recovery.initial_failures)} 个任务，${number(recovery.retried_tasks)} 个已重试，${number(recovery.recovered_tasks)} 个恢复成功。逐次失败原因及响应可在结果详情中查看。</p>` : "") +
    (repairs.count ? `<p>已核实并修正 ${number(repairs.count)} 处证据路径，涉及 ${number(repairs.records)} 项评分；原始回答和修正记录均已保留。</p>` : "");
  renderAnswerabilityDistributions(detail);
  renderDistributions(detail.metrics, !!(detail.answerability_distributions?.length || detail.block_needs_met_distributions?.length));
  renderCalibration(detail);
  SearchBenchRanking.render(detail, state.catalog.jobs);
  const empty = detail.query_executions.filter(q=>q.result_count===0);
  $("coverage-note").textContent = `本次包含 ${job.result_count} 条搜索结果；已有 ${number(detail.records)} 条子指标记录，${number(detail.statuses.not_applicable)} 条不适用。${detail.statuses.simulated?` ${number(detail.statuses.simulated)} 条仅模拟，未生成质量分数。`:""}${empty.length?` ${empty.length} 条查询无结果，保留采集状态。`:""}`;
  $("diagnostics").innerHTML = (direct?"<p>本次已关闭独立需求解析；评分模型直接依据原始 query 判断。</p>":"") + detail.preparation.map(p=>`<div class="diagnostic-line">${esc(p.query_text)} <span class="badge ${p.status==="success"?"success":"warning"}">${esc(statuses[p.status]||p.status)}</span>${p.reason?`<small>${esc(reasons[p.reason]||p.reason)}</small>`:""}</div>`).join("") + empty.map(q=>`<p>空结果：${esc(q.query_text)}（${esc(q.status)}）</p>`).join("") || "<p>等待需求解析记录。</p>";
  const pairs = detail.comparison?.pairs || [];
  $("comparison-section").hidden = !pairs.length;
  $("comparison").innerHTML = '<p>一致率只统计双方均有真实有效评分的同条件记录；一致不代表正确。</p>' + pairs.map(p=>`<div class="diagnostic-line">${esc(experimentLabel(job,p.left))} ↔ ${esc(experimentLabel(job,p.right))}${Object.entries(p.dimensions).map(([d,m])=>`<small>${esc(state.catalog.dimensions[d])}：${m.exact_agreement===null?"无可比真实评分":(m.exact_agreement*100).toFixed(1)+"%"} · 配对 ${m.both_rated_real} 条</small>`).join("")}<small>分歧 ${p.disagreements.length} 条（可导出模型对比查看）</small></div>`).join("");
  for (const [id,file,visible] of [["judgments","judgments.jsonl",true],["summary","summary.json",!!detail.summary],["specs","query_specs.jsonl",!direct&&!!job.preparation],["comparison","model_comparison.json",!!detail.comparison]]) {
    $(`download-${id}`).href=`/api/jobs/${job.id}/download/${file}`;
    $(`download-${id}`).hidden=!visible;
  }
}
function domain(url) { try { return new URL(url).hostname; } catch { return url; } }
function renderResults(data) {
  state.items=data.items; state.total=data.total; state.page=data.page;
  $("result-total").textContent=number(data.total);
  $("results-body").innerHTML=data.items.map((r,i)=>`<tr data-index="${i}" tabindex="0" role="button" aria-label="${esc("查看评分："+r.title)}"><td><span class="result-title"><span class="rank">${r.rank?`#${r.rank}`:""}</span>${esc(r.title)}</span><div class="result-query">${esc(r.query)}</div><div class="result-domain">${esc(domain(r.url))}</div></td><td><span class="scheme-name">${esc(schemeNames[r.scheme])}</span><span class="model-name">${esc(modelLabel(r.model,r.model_id))} · 第 ${r.repeat+1} 次${r.simulated?" · 模拟":""}</span></td><td><div class="score-chips">${Object.entries(r.dimensions).map(([key,item])=>`<span class="score-chip ${scoreClass(key,item)}" title="${esc(item.failure?.description || (item.evidence_repairs?.length ? "证据路径已核实修正，点击查看记录" : ""))}">${esc(labels[key])} <strong>${esc(valueLabel(key,item))}</strong>${item.evidence_repairs?.length?' <span class="repair-mark">已修正引用</span>':""}</span>`).join("")}</div>${humanBadge(r)}</td><td aria-hidden="true">↗</td></tr>`).join("");
  $("results-empty").hidden=!!data.items.length;
  $("results-empty").textContent=state.selected?"当前条件下没有可展示的评分记录。":"开始评测后，这里会逐步显示已完成的记录。";
  $("page-info").textContent=`共 ${number(data.total)} 条 URL × 实验记录 · 第 ${data.page} / ${Math.max(1,Math.ceil(data.total/20))} 页`;
  $("prev-page").disabled=data.page<=1;
  $("next-page").disabled=data.page*20>=data.total;
}
async function loadResults() {
  if (!state.selected) return;
  const version=state.version, id=state.selected;
  const query=new URLSearchParams({page:String(state.page),q:$("search").value,scheme:$("filter-scheme").value,model:$("filter-model").value,dimension:$("filter-dimension").value,status:$("filter-status").value});
  const data=await api(`/api/jobs/${id}/results?${query}`);
  if (version===state.version&&id===state.selected) renderResults(data);
}
async function refreshSelected() {
  if (!state.selected) return;
  const id=state.selected, version=state.version;
  const detail=await api(`/api/jobs/${id}`);
  if (version!==state.version||id!==state.selected) return;
  renderJob(detail);
  await loadResults();
}
async function selectJob(id) {
  SearchBenchRanking.clear();
  state.version++; state.selected=id; state.page=1; state.items=[];
  $("search").value="";
  for (const key of ["scheme","status","dimension"]) $(`filter-${key}`).value="";
  const job=state.catalog.jobs.find(j=>j.id===id);
  dimensionOptions(job);
  $("filter-model").innerHTML='<option value="">全部模型</option>'+(job?.models||[]).map(m=>`<option value="${esc(m)}">${esc(jobModelLabel(job,m))}</option>`).join("");
  renderHistory(state.catalog.jobs);
  await refreshSelected();
}
function validationDetail(m) {
  const issues = (m.validation_issues || []).map(issue=>`<div class="validation-issue"><code>引用位置：${esc(issue.path || issue.depends_on || "")}</code>${issue.quote?`<blockquote>${esc(issue.quote)}</blockquote>`:""}</div>`).join("");
  const repairs = (m.evidence_repairs || []).map(repair=>`<div class="repair-note"><strong>已核实修正引用</strong><code>${esc(repair.original_path)} → ${esc(repair.corrected_path)}</code><p>原文逐字匹配；评分值与引文未修改。</p></div>`).join("");
  const failure = m.failure ? `<p class="failure-description">${esc(m.failure.description)}</p><details class="validation-raw"><summary>原始诊断</summary><code>${esc(m.reason)}</code>${issues}</details>` : `<p>${esc(reasons[m.reason]||m.reason)}</p>`;
  const decision=m.decision;
  const native=decision?`<div class="native-decision"><strong>Jev 原生分档：${esc(decision.choice)} 分</strong><p>置信度 ${esc(decision.confidence)}（接口指标，不等于正确率）</p><div class="distribution-values">${[-1,0,1,2,3].map(v=>`<span class="distribution-value">${v} 分 <b>${(100*decision.probabilities[String(v)]).toFixed(2)}%</b></span>`).join("")}</div><p>本项校验分档及概率结构，不进行生成式引文校验。</p></div>`:"";
  return failure + repairs + native;
}
function promptError(message) {
  $("prompt-error").textContent=message;
  $("prompt-error").hidden=!message;
}
function promptKey(kind, mode=parsesQuery()?"frozen":"direct") { return `${kind}:${kind==="query_spec"?"frozen":mode}`; }
function activePromptKinds() {
  const parts={block_content_needs_met_v1:["block_needs_met"],answerability_only_v1:[nativeScorer()?"answerability_jev":"answerability"],legacy_five_v1:["legacy_url","duplicates"],google_overview_v1:["google_nm","google_pq"]};
  return [...(parsesQuery()?["query_spec"]:[]),...checkedSchemes().flatMap(s=>parts[s]||[])];
}
function selectedPromptVersions() {
  return Object.fromEntries(activePromptKinds().filter(k=>promptConfig.selected[promptKey(k)]).map(k=>[k,promptConfig.selected[promptKey(k)]]));
}
function promptTemplateParams() {
  const profile=checkedModels()[0]||"";
  return new URLSearchParams({query_understanding:parsesQuery()?"frozen":"direct",schemes:checkedSchemes().join(","),prompt_versions:JSON.stringify(selectedPromptVersions()),model_profile:profile,model_id:state.scoringChoices.get(profile)||""});
}
function rememberPromptSettings() {
  try {localStorage.setItem("searchbench-prompt-editor-v1",JSON.stringify({selected:promptConfig.selected,drafts:promptConfig.drafts}));}
  catch {$("prompt-editor-status").textContent="浏览器无法保存草稿；请保存为版本后再离开页面。";}
}
function restorePromptSettings() {
  try {
    const saved=JSON.parse(localStorage.getItem("searchbench-prompt-editor-v1")||"{}");
    if(saved.selected&&typeof saved.selected==="object"&&!Array.isArray(saved.selected))promptConfig.selected=saved.selected;
    if(saved.drafts&&typeof saved.drafts==="object"&&!Array.isArray(saved.drafts))promptConfig.drafts=saved.drafts;
  } catch {}
}
function renderPromptConfigStatus() {
  const drafts=activePromptKinds().filter(k=>promptConfig.drafts[promptKey(k)]);
  $("prompt-config-status").textContent=promptConfig.loading?"正在加载 Prompt…":promptConfig.error||
    (isBlock()?"API 结构版 2 · 三字段输出 随运行冻结；可预览完整五档标准、条件、例外和输出 schema。":drafts.length?"有未保存草稿。请编辑后保存并应用，或放弃草稿，再开始评测。":
      Object.keys(selectedPromptVersions()).length?"新评测将使用上方选中的自定义版本；历史运行保留原始 Prompt。":"新评测使用内置 Prompt。可编辑并保存为自定义版本。");
  $("reset-prompt-selection").hidden=!promptConfig.error;
}
async function refreshPromptConfig() {
  const version=++promptConfig.version;
  $("prompt-rubric-note").textContent=isBlock()?"skill 2.1.1 · API 结构版 2 · 三字段输出 · 五档 0–4。证据不足单列。仅发送四字段；关闭独立需求解析、只评 API 块文字。每条一次调用，DeepSeek 与可回答性共用运行配置：并发上限 32、每秒最多启动 16 个请求（其他接口为 4 并发、2 次/秒）。本方案输出额度 16,384 token、输入字符上限 100,000；超限不截断。":nativeScorer()?"Jev 专用可回答性 v1 · 五档 Choice（−1、0、1、2、3）。共同规则与各档定义可独立编辑；2 分表示完整正确答案伴随未化解冲突。此版本尚未进行新的人工一致率校准。":"内置规则为可回答性校准版 v1.1，分档 -1～3。选择自定义 Prompt 后，以所选版本为准。校准配置：千问 qwen3.8-max、temperature=0、JSON 输出模式、关闭独立需求解析、API 内容模式。";
  promptConfig.loading=true;promptConfig.error="";renderPromptConfigStatus();
  $("prompt-version-options").replaceChildren();
  try {
    const data=checkedSchemes().length?await api(`/api/prompts?${promptTemplateParams()}`):{items:[]};
    if(version!==promptConfig.version)return;
    promptConfig.items=data.items;
    $("prompt-version-options").innerHTML=data.items.map(item=>`<div class="prompt-version-row"><label for="prompt-choice-${esc(item.kind)}">${esc(item.label)}</label><div class="prompt-version-controls"><select id="prompt-choice-${esc(item.kind)}" data-prompt-choice="${esc(item.kind)}"><option value="">内置 Prompt</option>${item.versions.map(v=>`<option value="${esc(v.id)}" ${v.id===item.prompt_version?.id?"selected":""}>${esc(v.name)} · ${esc(shortDate(v.created_at))}</option>`).join("")}</select>${item.kind==="block_needs_met"?"<span>API 结构版 2 · 三字段输出</span>":`<button type="button" class="secondary-button" data-edit-prompt="${esc(item.kind)}">编辑 Prompt</button>`}</div></div>`).join("");
  } catch(err) {if(version===promptConfig.version){promptConfig.error=err.message;promptConfig.items=[];}}
  finally {if(version===promptConfig.version){promptConfig.loading=false;renderPromptConfigStatus();}}
}
function updatePromptDraft() {
  const item=promptConfig.editor;if(!item||promptConfig.saving)return;
  const rules=$("prompt-rules-editor").value,name=$("prompt-version-name").value;
  promptConfig.drafts[promptKey(item.kind,item.query_understanding)]={rules,name};
  $("prompt-editor-count").textContent=`${number(rules.length)} / 50,000 字符`;
  $("prompt-editor-status").textContent="草稿尚未用于评测；保存新版本并应用后生效。关闭编辑器会保留草稿。";
  $("prompt-editor-error").hidden=true;
  rememberPromptSettings();renderPromptConfigStatus();
}
function openPromptEditor(kind, rules=null) {
  const item=promptConfig.items.find(p=>p.kind===kind);
  if(!item||promptConfig.loading){error("Prompt 配置尚未加载完成，请稍后重试。");return;}
  promptConfig.editor=item;
  const draft=promptConfig.drafts[promptKey(kind,item.query_understanding)];
  const fromHistory=rules!==null;
  $("prompt-editor-title").textContent=`编辑 ${item.label}`;
  $("prompt-editor-context").textContent=`${fromHistory?"编辑来源：查看器中的 Prompt":`当前版本：${item.prompt_version?.name||"内置 Prompt"}`}\n适用模式：${kind==="query_spec"?"独立需求解析":item.query_understanding==="direct"?"关闭独立需求解析，直接评分":"开启独立需求解析后评分"}`;
  $("prompt-version-name").value=fromHistory?"基于所查看 Prompt 调试":draft?.name??`${item.prompt_version?.name||item.label} · 调试版`;
  $("prompt-rules-editor").value=fromHistory?rules:draft?.rules??item.rules;
  $("prompt-editor-count").textContent=`${number($("prompt-rules-editor").value.length)} / 50,000 字符`;
  const native=kind==="answerability_jev";
  $("prompt-editor-help").textContent=native?"Jev 专用版本，独立于其他评分模型。编辑 JSON 中的 instructions（共同规则）和 criteria（五档定义），保留 type=choice 与 -1、0、1、2、3 五个键。保存版本不会调用模型。":"编辑 System 评分规则，输出 Schema 由系统自动附加。保存版本不会调用模型。";
  $("prompt-rules-label").textContent=native?"Jev 评分规则与五档定义（JSON，可编辑）":"System 指令（可编辑）";
  $("prompt-editor-schema-block").hidden=native;
  $("prompt-editor-schema").textContent=JSON.stringify(item.schema,null,2);
  $("prompt-editor-error").hidden=true;
  $("prompt-editor-status").textContent=draft?"已恢复本机草稿，尚未应用。":"修改后保存为新版本，再点击主页面的“开始评测”。";
  if(fromHistory)updatePromptDraft();
  if(!$("prompt-editor-dialog").open)$("prompt-editor-dialog").showModal();
}
async function savePromptVersion() {
  const item=promptConfig.editor;if(!item||promptConfig.saving)return;
  const request={kind:item.kind,query_understanding:item.query_understanding,name:$("prompt-version-name").value,rules:$("prompt-rules-editor").value};
  if(!request.name.trim()||!request.rules.trim()){$("prompt-editor-error").textContent="请填写版本名称和评分规则。";$("prompt-editor-error").hidden=false;return;}
  promptConfig.saving=true;
  const controls=["save-prompt-version","restore-builtin-prompt","discard-prompt-draft","close-prompt-editor","prompt-version-name","prompt-rules-editor"];
  controls.forEach(id=>$(id).disabled=true);
  $("prompt-editor-status").textContent="正在保存版本…";
  try {
    const saved=await api("/api/prompt-versions",request), key=promptKey(item.kind,item.query_understanding);
    promptConfig.selected[key]=saved.id;delete promptConfig.drafts[key];rememberPromptSettings();
    await refreshPromptConfig();
    $("prompt-editor-dialog").close();
    if($("prompt-dialog").open)$("prompt-dialog").close();
    renderPromptConfigStatus();
  } catch(err) {$("prompt-editor-error").textContent=err.message;$("prompt-editor-error").hidden=false;$("prompt-editor-status").textContent="保存失败，草稿仍保留，请重试。";}
  finally {promptConfig.saving=false;controls.forEach(id=>$(id).disabled=false);}
}
function renderPromptPart() {
  const p=promptState.current;
  if(!p)return;
  const native=p.wire_request?.protocol==="typesafe";
  if(native&&!["request","response"].includes(promptState.tab))promptState.tab="request";
  const part=promptState.tab;
  document.querySelectorAll("[data-prompt-tab]").forEach(b=>{
    b.hidden=native?!(promptState.job?["request","response"]:["request"]).includes(b.dataset.promptTab):!promptState.job&&["request","response"].includes(b.dataset.promptTab);
    b.setAttribute("aria-pressed",String(b.dataset.promptTab===part));
    b.disabled=p.available===false&&b.dataset.promptTab!=="response";
  });
  const response=part==="response";
  const editKind=p.prompt_kind||p.kind;
  $("edit-inspected-prompt").hidden=response||p.available===false||promptConfig.loading||!promptConfig.items.some(item=>item.kind===editKind&&promptKey(item.kind,item.query_understanding)===promptKey(editKind,p.query_understanding));
  const messages=part==="messages"&&p.available!==false;
  $("prompt-messages").hidden=!messages;
  $("prompt-text").hidden=messages;
  $("request-meta").hidden=part!=="request"||!p.wire_request?.available;
  $("response-view").hidden=!response;
  $("response-summary").hidden=!response;
  $("response-details-block").hidden=!response;
  $("copy-response").hidden=!response;
  $("copy-prompt-all").hidden=response;
  if(native)$("copy-prompt-all").hidden=true;
  $("copy-prompt-all").disabled=p.available===false;
  $("copy-prompt-part").hidden=messages;
  $("copy-prompt-part").textContent=response?"复制回答原文":part==="request"?"复制请求体 JSON":"复制当前内容";
  $("copy-prompt-all").textContent=promptState.job?"复制完整消息 JSON":"复制模板 System";
  $("prompt-copy-status").textContent="";
  if(response){renderResponse();return;}
  if(p.available===false){$("prompt-text").textContent="这条历史记录未保存 Prompt，无法还原当时的实际输入。";$("prompt-part-note").textContent="未使用当前模板替代历史输入。";$("copy-prompt-part").disabled=true;return;}
  if(messages) {
    $("prompt-system-message").textContent=p.system;
    $("prompt-user-message").textContent=p.user??"模板预览尚无 User 消息。运行后可查看具体 Query、URL 和证据。";
    $("prompt-system-message").scrollTop=0;$("prompt-user-message").scrollTop=0;
    document.querySelector('[data-copy-message="user"]').disabled=p.user===null;
    $("prompt-part-note").textContent=promptState.job?
      "按消息顺序展示保存的完整内容：System 包含末尾 Schema；User 保留发送时的紧凑 JSON。仅自动换行，不删减或改写。":
      "这是当前模板，尚未发送给模型。运行后，通过“查看实际 Prompt”读取历史任务的完整输入。";
    return;
  }
  const wire=p.wire_request;
  const text=part==="system"?p.system:part==="payload"?
    (p.user??"模板预览尚无 User 消息。运行后可查看具体 Query、URL 和证据。"):
    part==="request"?(wire?.available?JSON.stringify(wire.body,null,2):wire?.reason||"未保存请求配置。"):
    (p.schema===null?"这份历史 Prompt 未包含可单独展示的 JSON Schema，请查看完整 System 内容。":JSON.stringify(p.schema,null,2));
  $("prompt-text").textContent=text;
  $("prompt-text").scrollTop=0;
  $("prompt-part-note").textContent=part==="system"?
    "完整 System 消息原文，包含评分规则与末尾 JSON Schema。":
    part==="payload"?"User 消息原文，保留协议层使用的紧凑 JSON 序列化内容。":
    part==="request"?"按本次保存的 Prompt 和历史模型配置还原请求体，并非网络抓包；展示模型、消息及生成参数，不包含鉴权头。":
    "模型必须遵守的结构化输出格式，读取自这份 System Prompt。";
  if(part==="request"&&wire?.available) {
    $("request-meta").textContent=`POST ${wire.endpoint}\n协议：${wire.protocol} · 来源：${wire.source==="saved_request"?"调用时保存的请求体":wire.source==="template"?"当前模板（尚未填入样本）":"历史快照还原"}`;
    if(native)$("prompt-part-note").textContent=wire.source==="template"?"Jev 专用模板：instructions 是共同规则，criteria 是完整五档定义。state 暂为空，运行时填入 Query 与本条证据。此预览不会调用模型。":"Jev 原生请求：state 是本条评分输入，questions 包含实际评分指令与五个候选分档；不使用 Chat 的 System/User 消息，不包含鉴权头。";
  }
  $("copy-prompt-part").disabled=(part==="payload"&&p.user===null)||(part==="schema"&&p.schema===null)||(part==="request"&&!wire?.available);
  $("prompt-copy-status").textContent="";
}
async function loadResponse() {
  const version=promptState.version, task=promptState.current.id;
  promptState.responseLoading=true;
  try {
    const response=await api(`/api/jobs/${promptState.job}/responses/${task}`);
    if(version!==promptState.version||!$("prompt-dialog").open)return;
    promptState.response=response;
    promptState.attempt=Math.max(0,(response.diagnostics?.attempts?.length||0)-1);
  } catch(err) {
    if(version!==promptState.version)return;
    promptState.responseError=err.message;
  }
  if(version!==promptState.version)return;
  promptState.responseLoading=false;
  if(promptState.tab==="response")renderResponse();
}
function renderResponse() {
  const r=promptState.response;
  $("prompt-part-note").textContent="读取本次任务保存的回答和接口元数据。保留未通过校验的原文；不包含密钥或模型内部推理字段。查看不会重新调用模型。";
  $("copy-response").disabled=!r;
  $("copy-prompt-part").disabled=true;
  $("response-refusal-block").hidden=true;
  $("response-summary").replaceChildren();
  $("response-details").textContent=r?JSON.stringify(r,null,2):"";
  $("response-message").textContent="";
  const attempts=r?.diagnostics?.attempts||[];
  $("response-attempt").innerHTML=attempts.length?attempts.map((a,i)=>`<option value="${i}">${r.simulated?"本地模拟":`${a.recovery_round?`第 ${a.recovery_round} 轮 · `:""}第 ${a.attempt??"?"} 次请求`} · ${esc(a.outcome==="completed"?"已返回回答":a.outcome||"未记录")}${a.http_status?` · HTTP ${a.http_status}`:""}</option>`).join(""):'<option value="0">无请求记录</option>';
  $("response-attempt").disabled=attempts.length<2;
  $("response-attempt").value=String(promptState.attempt);
  if(!r) {
    $("prompt-text").textContent=promptState.responseError||"正在读取模型响应…";
    if(!promptState.responseLoading&&!promptState.responseError)loadResponse();
    return;
  }
  const a=attempts[promptState.attempt], response=a?.response;
  const stream=response?.stream_info, last=promptState.attempt===attempts.length-1;
  const streamLimit=stream?.timeout_seconds??r.diagnostics?.stream_timeout_seconds??r.limits?.stream_timeout_seconds;
  const missing={not_recorded:"这条历史失败记录没有保存模型响应，无法还原。更新后的新运行会保存失败时返回的可见内容和结束原因。",not_called:"该任务未调用模型，因此没有模型响应。"};
  let message=missing[r.availability]||"";
  if(r.simulated)message="这是本地模拟输出，未调用外部模型。";
  else if(r.availability==="legacy_text")message="已读取历史记录中保存的回答原文。当时未记录接口结束原因及逐次请求详情。";
  else if(a&&!response)message="未取得可读取的模型响应。请结合请求错误和响应详情排查。";
  else if(["length","max_tokens","MAX_TOKENS"].includes(response?.finish_reason)||response?.incomplete_reason==="max_output_tokens")message="模型因输出长度限制而停止；下方保留已返回的部分文本，该回答未用于生成有效评分。";
  if(last&&r.failure)message=`${r.failure.label}：${r.failure.description}${message?`\n${message}`:""}`;
  if(stream&&!stream.completed) {
    message+=`\n已接收 ${stream.events??"未知数量"} 个流式事件；保存了 ${response.text?.length||0} 个字符的回答正文。`;
    if(streamLimit!=null)message+=` 本次生成时限 ${streamLimit} 秒。`;
    if(!response.text)message+="\n该请求的回答正文为空；接口元数据已保存，下方“响应详情 · JSON”已展开。";
    if(stream.reasoning_chunks>0&&!stream.answer_chunks)message+=" 已收到思考阶段的数据，但尚未收到最终回答文本。";
    if(!Object.keys(response.usage||{}).length)message+="\n接口尚未返回 token 用量，不能按 0 token 计算。";
  }
  const genericDiagnostic="Provider response refused, incomplete or malformed";
  if(r.reason&&!(r.failure&&r.reason===genericDiagnostic))message+=`${message?"\n":""}任务诊断：${r.reason}`;
  if(a?.error_message&&a.error_message!==r.reason&&!(last&&r.failure&&a.error_message===genericDiagnostic))message+=`\n请求诊断：${a.error_message}`;
  if(a?.body_note)message+=`\n响应读取：${a.body_note}`;
  if(stream?.error&&!(last&&r.failure?.code.startsWith("stream_")))message+=`\n流式响应：${stream.error}`;
  if(response?.error?.message)message+=`\n接口错误：${response.error.message}`;
  if(a?.validation_reason && a.validation_reason!==r.reason)message+=`\n本轮校验：${a.validation_reason}`;
  if(r.auto_recovery)message+=`\n自动恢复：共 ${r.auto_recovery.attempt_count} 轮，${r.auto_recovery.recovered?"已恢复":"结束原因："+r.auto_recovery.stop_reason}。每轮诊断、调整及请求记录见响应详情。`;
  $("response-message").textContent=message;
  $("response-message").hidden=!message;
  if(response?.refusal){$("response-refusal-block").hidden=false;$("response-refusal").textContent=response.refusal;}
  const usage=response?.usage||{}, tokens=usage.output_tokens??usage.completion_tokens??usage.candidatesTokenCount;
  const fields=[["结束原因",response?.finish_reason||response?.incomplete_reason||(last?r.failure?.label:null)||"未记录"],
    ["接口状态",response?.provider_status||(a?.http_status?`HTTP ${a.http_status}`:"未记录")],
    ["返回模型",response?.returned_model||"未记录"],["输出 token / 上限",`${tokens??"—"} / ${a?.max_output_tokens??r.diagnostics?.max_output_tokens??"—"}`],
    ["请求耗时",a?.latency_seconds!=null?`${Number(a.latency_seconds).toFixed(2)} 秒`:"未记录"],
    ["Response ID",response?.response_id||"未记录"]];
  if(stream)fields.push(["已接收流式事件",`${stream.events??"—"} 个`],["回答正文",`${response.text?.length||0} 字符`],["生成时限",streamLimit!=null?`${streamLimit} 秒`:"未记录"]);
  if(a)$("response-summary").innerHTML=fields.map(([label,value])=>`<div><small>${esc(label)}</small><span>${esc(value)}</span></div>`).join("");
  $("response-text-label").textContent=response?.text?`模型可见回答原文 · ${response.text.length} 字符`:"模型可见回答原文";
  $("prompt-text").textContent=response?.text||(response&&stream?"接口已返回流式数据，但未包含可见的评分正文。已保存的 Response ID、流式事件数量和中断原因见响应详情。":"没有可展示的回答正文。可查看上方诊断、拒绝信息及下方响应详情。");
  $("response-details-block").open=!!a&&!response?.text;
  $("prompt-text").scrollTop=0;
  $("copy-prompt-part").disabled=!response?.text;
}
async function loadPrompt() {
  const version=++promptState.version, item=promptState.items.find(p=>p.id===$("prompt-task").value);
  promptState.current=null;
  promptState.response=null;promptState.responseLoading=false;promptState.responseError="";promptState.attempt=0;
  $("prompt-view").hidden=true; promptError(""); $("prompt-meta").textContent="正在读取…";
  if(!item){$("prompt-meta").textContent="暂无已保存的任务；任务完成后可重新打开查看。";return;}
  try {
    const p=promptState.job?await api(`/api/jobs/${promptState.job}/prompts/${item.id}`):item;
    if(version!==promptState.version||!$("prompt-dialog").open)return;
    promptState.current=p;
    const direct=p.query_understanding==="direct";
    const model=modelLabel(p.model||item.model,p.model_id||item.model_id);
    const meta=promptState.job?
      `${item.label} · ${model} · ${statuses[p.status]||p.status}${item.rank?` · 排名 #${item.rank}`:""}\n${item.query}${item.title?`\n${item.title}`:""}`:
      `${item.label} · ${direct?"独立需求解析已关闭":"独立需求解析已开启"}`;
    $("prompt-meta").textContent=meta+`\nPrompt 版本：${p.prompt_version?.name||(promptState.job?"运行时保存的内置版本":"内置 Prompt")}`;
    const invocationNote={not_called:"该任务未调用模型；展示的是准备好的输入。",simulated:"本次为本地模拟任务，未发送给外部模型。",attempted:"展示本条任务调用模型时保存的完整输入。",unknown:"已保存输入，但缺少调用记录，无法确认是否发送。"};
    $("prompt-note").textContent=promptState.job?
      `${invocationNote[p.invocation]||invocationNote.unknown} 查看不会重新调用模型。${p.kind==="legacy_url"?" 可回答性、权威性、时效性和噪声共用一次模型响应。":""}`:
      "当前模板预览，尚未填入样本。查看真实的 Query、URL 和完整消息，请使用“查看实际 Prompt”。";
    $("prompt-provenance").textContent=promptState.job?
      `运行编号：${promptState.job}\n任务编号：${p.id}\nPrompt 版本：${p.prompt_version?.id||"内置"}\nPrompt 哈希：${p.prompt_hash||"未记录"}\n输入哈希：${p.input_hash||"未记录"}\n显式采样次数：${p.sampling_attempt}\n自动恢复轮数：${p.auto_recovery?.attempt_count||1}\n实际 Prompt 哈希：${p.effective_prompt_hash||p.prompt_hash||"未记录"}`:
      `来源：当前评分器 prompt_for()\n需求解析模式：${p.query_understanding}\n任务类型：${p.kind}`;
    $("prompt-view").hidden=false;
    renderPromptPart();
  } catch(err) {
    if(version===promptState.version){$("prompt-meta").textContent="";promptError(err.message);}
  }
}
async function openPrompts(jobId=null, taskId=null, tab="messages") {
  const version=++promptState.version;
  promptState.job=jobId;promptState.items=[];promptState.current=null;promptState.tab=tab;
  $("prompt-title").textContent=jobId?"实际 Prompt 与模型响应":"当前 Prompt 模板预览";
  $("response-tab").hidden=!jobId;
  $("request-tab").hidden=!jobId;
  document.querySelector('[data-prompt-tab="messages"]').textContent=jobId?"完整 Prompt":"模板预览";
  $("prompt-note").textContent=jobId?"正在读取保存的任务…":"正在读取当前评分规则…";
  $("prompt-task").innerHTML='<option value="">正在加载…</option>';
  $("prompt-task").disabled=true;$("prompt-meta").textContent="";$("prompt-view").hidden=true;promptError("");
  if(!$("prompt-dialog").open)$("prompt-dialog").showModal();
  try {
    const schemes=[...document.querySelectorAll('input[name="scheme"]:checked')].map(e=>e.value);
    if(!jobId&&!schemes.length)throw new Error("请先勾选至少一套评测方案。");
    const params=promptTemplateParams();
    const data=await api(jobId?`/api/jobs/${jobId}/prompts`:`/api/prompts?${params}`);
    if(version!==promptState.version||!$("prompt-dialog").open)return;
    promptState.items=data.items;
    $("prompt-task").innerHTML=data.items.length?data.items.map(p=>{
      const label=jobId?`${p.label}${p.rank?` · #${p.rank}`:""} · ${modelLabel(p.model,p.model_id)} · ${statuses[p.status]||p.status} · 第 ${p.repeat+1} 次 · ${p.query}`:p.label;
      return `<option value="${esc(p.id)}">${esc(label)}</option>`;
    }).join(""):'<option value="">暂无已保存的任务</option>';
    $("prompt-task").disabled=!data.items.length;
    if(taskId&&data.items.some(p=>p.id===taskId))$("prompt-task").value=taskId;
    await loadPrompt();
  } catch(err) {if(version===promptState.version)promptError(err.message);}
}
async function copyPrompt(complete) {
  const p=promptState.current;if(!p)return;
  const text=complete?(promptState.job?JSON.stringify(p.messages,null,2):p.system):$("prompt-text").textContent;
  try {
    await navigator.clipboard.writeText(text);
    $("prompt-copy-status").textContent=complete?(promptState.job?"完整消息 JSON 已复制":"模板 System 已复制"):"当前内容已复制";
  } catch {
    $("prompt-copy-status").textContent="复制失败，请选中文字手动复制。";
  }
}
function showDetail(index) {
  const r=state.items[index]; if (!r) return;
  let safeUrl=""; try {const url=new URL(r.url); if(["https:","http:"].includes(url.protocol)) safeUrl=url.href;} catch {}
  $("detail-content").innerHTML=`<div class="detail-query"><small>USER QUERY</small>${esc(r.query)}</div><h3 class="detail-title">${esc(r.title)}</h3>${safeUrl?`<a class="detail-url" href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer">${esc(r.url)} ↗</a>`:`<p class="detail-url">${esc(r.url)}</p>`}<p class="detail-meta">${esc(schemeNames[r.scheme])} · ${esc(modelLabel(r.model,r.model_id))} · 第 ${r.repeat+1} 次${r.simulated?" · 模拟结果":""}</p>${Object.entries(r.dimensions).map(([key,m])=>`<section class="measure-card"><div class="measure-head"><strong>${esc(state.catalog.dimensions[key])}</strong><span class="score-chip ${scoreClass(key,m)}">${esc(valueLabel(key,m))}${m.judgment_status==="error"?"":` · ${esc(statuses[m.judgment_status]||m.judgment_status)}`}</span></div>${validationDetail(m)}${m.duplicate_of?`<code>重复对象：${esc(m.duplicate_of)}</code>`:""}${m.evidence.map(e=>`<blockquote>${esc(e.quote)}</blockquote><code>证据位置 ${esc(e.path)}</code>`).join("")}<code>依据：${m.rating_target==="page_snapshot"?"网页快照":"API 可见内容"}</code></section>`).join("")}<details class="source-block"><summary>查看 API 原始内容</summary><pre>${esc(r.source.text||r.source.snippet||"无文本内容")}</pre></details>${r.source.page_text?`<details class="source-block"><summary>查看网页正文快照</summary><pre>${esc(r.source.page_text)}</pre></details>`:""}${r.query_spec?`<details class="source-block"><summary>查看冻结的 query 需求</summary><pre>${esc(JSON.stringify(r.query_spec,null,2))}</pre></details>`:""}`;
  if(r.query_understanding==="direct") {
    $("detail-content").insertAdjacentHTML("afterbegin",'<p class="info-note">独立需求解析已关闭：本次直接依据原始 query 评分，没有共享需求说明。</p>');
    const assessment=Object.values(r.dimensions).find(m=>m.query_assessment)?.query_assessment;
    if(assessment) $("detail-content").insertAdjacentHTML("beforeend",`<details class="source-block"><summary>查看评分模型本次的需求理解</summary><pre>${esc(JSON.stringify(assessment,null,2))}</pre></details>`);
  }
  if(r.human_reference)$("detail-content").insertAdjacentHTML("afterbegin",`<section class="human-reference-card"><strong>人工可回答性：${r.human_reference.grade} 分</strong>${humanBadge(r)}<p>${esc(r.human_reference.reason||"未填写人工理由")}</p><small>人工参考分数不参与模型输入。</small></section>`);
  const dimensions=Object.values(r.dimensions);
  $("detail-content").querySelectorAll(".measure-card").forEach((card,i)=>{
    const button=document.createElement("button");
    button.type="button";button.className="measure-prompt";
    button.textContent="查看该项实际 Prompt ↗";
    button.dataset.promptTask=dimensions[i].task_id;button.dataset.promptJob=state.selected;
    card.append(button);
    const responseButton=button.cloneNode(true);
    responseButton.textContent="查看模型响应 ↗";responseButton.dataset.promptPart="response";
    card.append(responseButton);
  });
  const native=Object.values(r.dimensions).find(m=>m.skill_result)?.skill_result;
  if(native && "confidence" in native) $("detail-content").insertAdjacentHTML("beforeend",`<section class="measure-card"><strong>意图、置信度与复核</strong><p>${esc(native.intent)} · ${esc(native.interpretation)}</p><p>置信度：${esc(native.confidence||"证据不足")} · 需要复核：${native.needs_review?"是":"否"}</p><p>${native.limitations.map(esc).join("；")}</p></section>`);
  $("detail-dialog").showModal();
}
function clearSelection() {
  SearchBenchRanking.clear();
  state.version++; state.selected=null; state.job=null; state.items=[]; state.page=1; state.total=0;
  if($("detail-dialog").open) $("detail-dialog").close();
  $("active-state").hidden=true; $("idle-state").hidden=false;
  $("run-badge").textContent="等待开始"; $("run-badge").className="badge neutral";
  $("search").value="";
  for(const name of ["scheme","dimension","status"]) $(`filter-${name}`).value="";
  $("filter-model").innerHTML='<option value="">全部模型</option>';
  renderResults({items:[],total:0,page:1});
}
function rememberUndo(jobId) {
  state.undoJob=jobId;
  try {
    if(jobId) sessionStorage.setItem("searchbench-deleted-run",jobId);
    else sessionStorage.removeItem("searchbench-deleted-run");
  } catch {}
}
async function deleteJob(id) {
  if(state.historyBusy) return;
  state.historyBusy=true; state.historyVersion++; error("");
  renderHistory(state.catalog.jobs);
  try {
    await api(`/api/jobs/${id}/delete`,{});
    rememberUndo(id);
    if(state.selected===id) clearSelection();
    await refreshHistory();
    if(!state.selected && state.catalog.jobs.length) await selectJob(state.catalog.jobs[0].id);
  } catch(err) {error(err.message);}
  finally {state.historyBusy=false;renderHistory(state.catalog.jobs);}
}
async function undoDelete() {
  if(state.historyBusy||!state.undoJob) return;
  const id=state.undoJob;
  state.historyBusy=true;state.historyVersion++;error("");
  renderHistory(state.catalog.jobs);
  try {
    await api(`/api/jobs/${id}/restore`,{});
    rememberUndo(null);
    await refreshHistory();await selectJob(id);
  } catch(err) {error(err.message);}
  finally {state.historyBusy=false;renderHistory(state.catalog.jobs);}
}
async function refreshHistory(){
  const version=++state.historyVersion;
  const data=await api("/api/jobs");
  if(version!==state.historyVersion)return;
  renderHistory(data.jobs);
  SearchBenchRanking.history(data.jobs);
  if(state.selected&&!data.jobs.some(j=>j.id===state.selected)){
    clearSelection();
    if(data.jobs.length)await selectJob(data.jobs[0].id);
  }
}
async function poll() {
  try {
    if(!state.historyBusy) {
      await refreshHistory();
      if (state.selected&&active(state.job)) await refreshSelected();
    }
    $("connection-text").textContent="本地服务已连接";
  } catch (err) { $("connection-text").textContent="服务连接中断"; }
  setTimeout(poll,2500);
}
$("dataset").addEventListener("change",()=>{updateDataset();rememberDataset();renderHistory(state.catalog.jobs);});
$("limit").addEventListener("change",updateScope);
$("refresh-scope").addEventListener("click",updateScope);
document.querySelectorAll('input[name="scheme"]').forEach(input=>input.addEventListener("change",()=>{
  if(input.checked) document.querySelectorAll('input[name="scheme"]').forEach(other=>{if(other!==input && (input.value===blockScheme || other.value===blockScheme))other.checked=false;});
  $("parse-query").disabled=isBlock();$("mode").disabled=isBlock();
  if(isBlock()) {$("parse-query").checked=false;$("mode").value="api_only";}
  $("plan-result").hidden=true;updateDataset();renderQueryModels();
  updateSchemeStats();renderHistory(state.catalog.jobs);refreshPromptConfig();
}));
$("load-dataset").addEventListener("click",loadDataset);
$("dataset-input-path").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();loadDataset();}});
document.querySelectorAll(".dataset-import-options input").forEach(input=>input.addEventListener("keydown",e=>{
  if(e.key==="Enter"){e.preventDefault();loadDataset();}
}));
$("dataset-time-policy").addEventListener("change",()=>{$("dataset-time-field").hidden=$("dataset-time-policy").value!=="explicit";});
$("browse-dataset").addEventListener("click",openFileBrowser);
$("close-files").addEventListener("click",()=>$("file-dialog").close());
$("file-dialog").addEventListener("close",()=>{fileState.version++;clearTimeout(fileState.timer);});
function goToDirectory(){clearTimeout(fileState.timer);$("file-filter").value="";browseFiles($("file-directory").value);}
$("file-go").addEventListener("click",goToDirectory);
$("file-directory").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();goToDirectory();}});
$("file-filter").addEventListener("input",()=>{
  clearTimeout(fileState.timer);
  const path=fileState.data?.path||$("file-directory").value;
  fileState.timer=setTimeout(()=>browseFiles(path),250);
});
$("file-list").addEventListener("click",e=>{
  const button=e.target.closest("[data-file-index]");
  const item=button&&fileState.data?.items[Number(button.dataset.fileIndex)];
  if(!item)return;
  clearTimeout(fileState.timer);
  if(item.kind==="directory"){$("file-filter").value="";browseFiles(item.path);}else chooseFile(item.path);
});
$("file-parent").addEventListener("click",()=>{
  if(fileState.data){clearTimeout(fileState.timer);$("file-filter").value="";browseFiles(fileState.data.parent);}
});
for(const [id,delta] of [["file-prev",-1],["file-next",1]])$(id).addEventListener("click",()=>{
  if(fileState.data)browseFiles(fileState.data.path,fileState.data.page+delta);
});
$("choose-directory").addEventListener("click",()=>{if(fileState.data?.bundle)chooseFile(fileState.data.path);});
$("model-options").addEventListener("change",()=>{updateModels();refreshPromptConfig();});
$("scoring-model-options").addEventListener("change",e=>{
  if(e.target.matches("select[data-score-profile]")) {
    state.scoringChoices.set(e.target.dataset.scoreProfile,e.target.value);
    renderHistory(state.catalog.jobs);
    refreshPromptConfig();
  }
});
$("query-model").addEventListener("change",()=>{state.queryChoice=$("query-model").value;renderHistory(state.catalog.jobs);});
$("parse-query").addEventListener("change",()=>{if(nativeScorer())updateModels();else renderQueryModels();renderHistory(state.catalog.jobs);refreshPromptConfig();});
$("calibration-query").addEventListener("change",()=>{calibrationState.page=1;loadCalibration();});
for(const [id,delta] of [["calibration-prev",-1],["calibration-next",1]])$(id).addEventListener("click",()=>{calibrationState.page+=delta;loadCalibration();});
$("refresh-models").addEventListener("click",()=>updateModels());
$("refresh-scoring-models").addEventListener("click",()=>updateModels());
$("history").addEventListener("click",e=>{
  const remove=e.target.closest("[data-delete-job]");
  if(remove&&!remove.disabled){deleteJob(remove.dataset.deleteJob);return;}
  const b=e.target.closest("[data-job]");
  if(b&&!b.disabled)selectJob(b.dataset.job).catch(e=>error(e.message));
});
$("undo-delete").addEventListener("click",undoDelete);
$("refresh-history").addEventListener("click",()=>refreshHistory().catch(e=>error(e.message)));
$("run-form").addEventListener("submit",async e=>{
  e.preventDefault(); error("");
  if(state.busy||state.importing) return; state.busy=true; $("start-button").disabled=true;
  try {
    if(promptConfig.loading||promptConfig.error)throw new Error("请先完成 Prompt 版本加载，再开始评测。");
    if(activePromptKinds().some(k=>promptConfig.drafts[promptKey(k)]))throw new Error("有未保存的 Prompt 草稿，请先保存并应用，或在编辑器中放弃草稿。");
    if(checkedModels().length!==1) throw new Error("每次评测只能选择一个评分模型。");
    if(state.discovering||!scoringReady()) throw new Error("请先为所选评分接口选择具体型号。");
    if(parsesQuery()&&(state.discovering||!$("query-model").value)) throw new Error("请先获取并选择需求解析模型。");
    const body={dataset:$("dataset").value,models:checkedModels(),scoring_model_ids:scoringSelections(),query_understanding:parsesQuery()?"frozen":"direct",schemes:[...document.querySelectorAll('input[name="scheme"]:checked')].map(e=>e.value),mode:$("mode").value,limit:$("limit").value?Number($("limit").value):null,repeats:Number($("repeats").value),prompt_versions:selectedPromptVersions()};
    if(parsesQuery()) {
      const [queryProfile,queryModelId]=JSON.parse($("query-model").value);
      Object.assign(body,{query_model:queryProfile,query_model_id:queryModelId});
    }
    const job=await api("/api/jobs",body);
    await refreshHistory(); await selectJob(job.id);
  } catch(err){error(err.message);}
  finally {state.busy=false; renderHistory(state.catalog.jobs);}
});
$("plan-button").addEventListener("click",async()=>{
  if(state.busy||state.importing)return;state.busy=true;$("plan-button").disabled=true;error("");
  try {
    const body={dataset:$("dataset").value,models:checkedModels(),scoring_model_ids:scoringSelections(),query_understanding:parsesQuery()?"frozen":"direct",schemes:checkedSchemes(),mode:$("mode").value,limit:$("limit").value?Number($("limit").value):null,repeats:Number($("repeats").value),prompt_versions:selectedPromptVersions()};
    const p=await api("/api/jobs/plan",body), groups=Object.values(p.experiments);
    $("plan-result").textContent=`预检完成：${number(p.selected_queries)} 个 query，${number(groups.reduce((n,g)=>n+g.tasks,0))} 个评分任务；预计 ${number(groups.reduce((n,g)=>n+g.planned_model_invocations,0))} 次模型调用，${number(groups.reduce((n,g)=>n+g.input_too_large,0))} 条输入超限。尚未创建运行任务，评分 API 调用为 0。配置变化后请重新预检。`;
    $("plan-result").hidden=false;
  } catch(err){error(err.message);} finally {state.busy=false;$("plan-button").disabled=false;renderHistory(state.catalog.jobs);}
});
$("resume-button").addEventListener("click",async()=>{
  $("resume-button").disabled=true;
  try {await api(`/api/jobs/${state.selected}/resume`,{});await refreshHistory();await refreshSelected();}
  catch(err){error(err.message);} finally {$("resume-button").disabled=false;}
});
$("cancel-button").addEventListener("click",async()=>{
  $("cancel-button").disabled=true;
  try {await api(`/api/jobs/${state.selected}/cancel`,{}); await refreshHistory(); await refreshSelected();}
  catch(err){error(err.message);} finally{$("cancel-button").disabled=false;}
});
for (const name of ["scheme","model","dimension","status"]) $(`filter-${name}`).addEventListener("change",()=>{state.page=1;state.version++;loadResults().catch(e=>error(e.message));});
let searchTimer;
$("search").addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.page=1;state.version++;loadResults().catch(e=>error(e.message));},200);});
for(const [id,delta] of [["prev-page",-1],["next-page",1]]) $(id).addEventListener("click",()=>{state.page+=delta;state.version++;loadResults().catch(e=>error(e.message));});
$("results-body").addEventListener("click",e=>{const r=e.target.closest("[data-index]");if(r)showDetail(Number(r.dataset.index));});
$("results-body").addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){const r=e.target.closest("[data-index]");if(r){e.preventDefault();showDetail(Number(r.dataset.index));}}});
$("close-detail").addEventListener("click",()=>$("detail-dialog").close());
 $("preview-prompts").addEventListener("click",()=>openPrompts());
 $("actual-prompts").addEventListener("click",()=>{
   const id=state.selected||state.catalog.jobs[0]?.id;
   if(id)openPrompts(id);else error("还没有运行记录。完成一次评测后，可在这里查看实际 Prompt；当前可先预览模板。");
 });
 $("run-prompts").addEventListener("click",()=>{if(state.selected)openPrompts(state.selected);});
 $("detail-content").addEventListener("click",e=>{
   const b=e.target.closest("[data-prompt-task]");
   if(b)openPrompts(b.dataset.promptJob,b.dataset.promptTask,b.dataset.promptPart||"messages");
 });
 $("prompt-task").addEventListener("change",loadPrompt);
 document.querySelectorAll("[data-prompt-tab]").forEach(b=>b.addEventListener("click",()=>{
   promptState.tab=b.dataset.promptTab;renderPromptPart();
 }));
 $("copy-prompt-part").addEventListener("click",()=>copyPrompt(false));
 $("copy-prompt-all").addEventListener("click",()=>copyPrompt(true));
 $("prompt-version-options").addEventListener("click",e=>{
   const button=e.target.closest("[data-edit-prompt]");if(button)openPromptEditor(button.dataset.editPrompt);
 });
 $("prompt-version-options").addEventListener("change",e=>{
   const select=e.target.closest("[data-prompt-choice]");if(!select)return;
   const key=promptKey(select.dataset.promptChoice);
   if(select.value)promptConfig.selected[key]=select.value;else delete promptConfig.selected[key];
   rememberPromptSettings();refreshPromptConfig();
 });
 $("reset-prompt-selection").addEventListener("click",()=>{
   activePromptKinds().forEach(kind=>delete promptConfig.selected[promptKey(kind)]);
   rememberPromptSettings();refreshPromptConfig();
 });
 $("edit-inspected-prompt").addEventListener("click",()=>{
   const p=promptState.current;if(p)openPromptEditor(p.prompt_kind||p.kind,p.rules);
 });
 $("prompt-rules-editor").addEventListener("input",updatePromptDraft);
 $("prompt-version-name").addEventListener("input",updatePromptDraft);
 $("save-prompt-version").addEventListener("click",savePromptVersion);
 $("restore-builtin-prompt").addEventListener("click",()=>{
   if(!promptConfig.editor)return;
   $("prompt-rules-editor").value=promptConfig.editor.default_rules;updatePromptDraft();
 });
 $("discard-prompt-draft").addEventListener("click",()=>{
   const item=promptConfig.editor;if(!item)return;
   delete promptConfig.drafts[promptKey(item.kind,item.query_understanding)];rememberPromptSettings();
   $("prompt-editor-dialog").close();renderPromptConfigStatus();
 });
 $("close-prompt-editor").addEventListener("click",()=>$("prompt-editor-dialog").close());
 $("prompt-editor-dialog").addEventListener("cancel",e=>{if(promptConfig.saving)e.preventDefault();});
 document.querySelectorAll("[data-copy-message]").forEach(button=>button.addEventListener("click",async()=>{
   const role=button.dataset.copyMessage, text=promptState.current?.[role];
   if(typeof text!=="string")return;
   try {await navigator.clipboard.writeText(text);$("prompt-copy-status").textContent=`${role.toUpperCase()} 原文已复制`;}
   catch {$("prompt-copy-status").textContent="复制失败，请选中文字手动复制。";}
 }));
 $("response-attempt").addEventListener("change",()=>{promptState.attempt=Number($("response-attempt").value);$("prompt-copy-status").textContent="";renderResponse();});
 $("copy-response").addEventListener("click",async()=>{
   if(!promptState.response)return;
   try{await navigator.clipboard.writeText(JSON.stringify(promptState.response,null,2));$("prompt-copy-status").textContent="响应详情已复制";}
   catch{$("prompt-copy-status").textContent="复制失败，请在响应详情中选中文字手动复制。";}
 });
 $("close-prompt").addEventListener("click",()=>$("prompt-dialog").close());
 $("prompt-dialog").addEventListener("close",()=>{promptState.version++;});
$("detail-dialog").addEventListener("click",e=>{if(e.target===$("detail-dialog")){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
(async()=>{
  try{
    state.catalog=await api("/api/bootstrap");
    try {state.undoJob=sessionStorage.getItem("searchbench-deleted-run");} catch {}
    initControls();renderHistory(state.catalog.jobs);restorePromptSettings();refreshPromptConfig();
    $("connection-text").textContent="本地服务已连接";
    if(state.catalog.jobs.length&&!state.catalog.datasets.find(d=>d.id===$("dataset").value)?.reference_labels)await selectJob(state.catalog.jobs[0].id);
    poll();
    setInterval(refreshRunClocks,1000);
  }catch(err){error(err.message);$("connection-text").textContent="初始化失败，请刷新重试";}
})();
