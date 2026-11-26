/* Adaptive ErgoVest: single-page app. One SSE stream feeds every view. */
(function(){
'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const IS_ADMIN = document.querySelector('meta[name=role]').content === 'admin';
let CSRF = document.querySelector('meta[name=csrf]').content;

/* ---------- api / toast ---------- */
async function api(url, method, body, retry){
  method = method||'GET';
  const opt = {method, credentials:'same-origin', headers:{'X-CSRF-Token':CSRF}};
  if(body!==undefined){ opt.headers['Content-Type']='application/json'; opt.body=JSON.stringify(body); }
  let r; try{ r = await fetch(url,opt); }catch(e){ return {ok:false,msg:'Cannot reach the server.'}; }
  if(r.status===401){ location.href='/login'; return {ok:false}; }
  const d = await r.json().catch(()=>({ok:r.ok}));
  if(r.status===403 && d.error==='csrf' && !retry){ CSRF=(await (await fetch('/api/csrf')).json()).csrf; return api(url,method,body,true); }
  return d;
}
function toast(msg, kind){
  const box=$('toasts'); while(box.children.length>=3) box.firstChild.remove();
  const t=document.createElement('div'); t.className='toast '+(kind||''); t.textContent=msg; box.appendChild(t);
  setTimeout(()=>t.remove(), kind==='bad'?5200:3200);
}
const say = d => toast(d.msg||(d.ok?'Done':'Failed'), d.ok?'ok':'bad');

/* ---------- state ---------- */
const MAXROWS=750, MAXRISK=600;
let S={}, COL={}, rows=[], riskHist=[], flexHist=[], epoch=-1, lastStatusSeq=-1, lastMsg=0, sseUp=false, evSeq=-1;
let fwdSm=null, sideSm=null, cur='dash';
const val = n => { const r=rows[rows.length-1]; const i=COL[n]; return (r&&i!=null)?r[i]:null; };
const LV = {
  Low:{cls:'lvl-low',word:'LOW',msg:'Good form.'},
  Moderate:{cls:'lvl-moderate',word:'MODERATE',msg:'Keep it smooth.'},
  Elevated:{cls:'lvl-elevated',word:'ELEVATED',msg:'Bend your knees, keep the load close.'},
  High:{cls:'lvl-high',word:'HIGH RISK',msg:'Stop. Reduce forward bending and twisting.'}};
const fmt=(v,d)=> v==null||!isFinite(v)?'--':(+v).toFixed(d);
const dur=s=>{s=Math.max(0,Math.floor(s));return [s/3600,s/60%60,s%60].map(x=>String(Math.floor(x)).padStart(2,'0')).join(':');};

function health(){
  const st=S.status||{}, c=S.connection, m=S.model||{};
  if(!sseUp) return {k:'offline',word:'Offline',msg:'Lost contact with the app. Reconnecting.',fresh:false};
  if(S.mode==='live'){
    if(c==='disconnected') return {k:'none',word:'No sensor',msg:'LIVE mode. Open Device and connect the ESP32.',fresh:false};
    if(c==='waiting') return {k:'wait',word:'Waiting',msg:'Port open. Waiting for the first sensor reading.',fresh:false};
    if(c==='stale') return {k:'stale',word:'No data',msg:'No readings for '+fmt(S.age,0)+' s. Check the USB cable and power.',fresh:false};
  }
  if(m.preparing) return {k:'wait',word:'Preparing',msg:'Building the simulated demo model.',fresh:true};
  if(!m.has_baseline) return {k:'cal',word:'Calibrate',msg:'Open Device and calibrate to get a risk score.',fresh:true};
  if(st.risk==null || (S.server_time-st.t)>3) return {k:'wait',word:'Waiting',msg:'Collecting the first 2 seconds of data.',fresh:true};
  const L=LV[st.level]; return {k:'level',level:st.level,word:L.word,msg:L.msg,fresh:true,cls:L.cls};
}

/* ---------- live tile definitions ---------- */
const TILES = {
  motion:[['ax','Accel X','m/s²',2],['ay','Accel Y','m/s²',2],['az','Accel Z','m/s²',2],['gx','Gyro X','rad/s',3],['gy','Gyro Y','rad/s',3],['gz','Gyro Z','rad/s',3]],
  angles:[['forwardTilt','Forward tilt','°',1],['sideTilt','Side tilt','°',1],['totalTilt','Total tilt','°',1]],
  flex:[['flex1','Flex 1','ADC',0,4095],['flex2','Flex 2','ADC',0,4095],['flex3','Flex 3','ADC',0,4095]],
  fsr:[['fsr1','FSR 1','ADC',0,4095],['fsr2','FSR 2','ADC',0,4095],['fsr3','FSR 3','ADC',0,4095]],
  env:[['ambientTempC','Ambient temperature (DHT11)','°C',1],['dsTempC','DS18B20 temperature','°C',1],['humidity','Humidity (DHT11)','%',0]]};
Object.keys(TILES).forEach(g=>{ $('g-'+g).innerHTML = TILES[g].map(t=>`<div class="tile"><div class="l">${t[1]}</div><div class="v num"><span id="t-${t[0]}">--</span><small>${t[2]}</small></div>${t[4]?`<div class="bar"><i id="b-${t[0]}"></i></div>`:''}</div>`).join(''); });
const DT=[['forwardTilt','Forward tilt','°',1],['sideTilt','Side tilt','°',1],['totalTilt','Total tilt','°',1],['_flex','Trunk flexion','°',0],['ambientTempC','Ambient','°C',1],['dsTempC','DS18B20','°C',1]];
$('dTiles').innerHTML = DT.map(t=>`<div class="tile"><div class="l">${t[1]}</div><div class="v num"><span id="d-${t[0]}">--</span><small>${t[2]}</small></div></div>`).join('');
if(innerWidth<720) document.querySelectorAll('#liveGroups details').forEach(d=>{ if(!['motion','angles'].includes(d.dataset.grp)) d.open=false; });

/* ---------- charts ---------- */
const pal = () => document.documentElement.getAttribute('data-theme')==='light' ? ['#0a8fc7','#6d49d8','#d98100'] : ['#3cc8ff','#a98bff','#ffb12b'];
let rawSecs=10, riskSecs=60;
const tail = (name,n) => { const i=COL[name]; return i==null?[]:rows.slice(-n).map(r=>r[i]); };
const rawGet = names => () => ({ys:names.map(n=>tail(n,rawSecs*50)), xLabels:['-'+rawSecs+' s','','now'], xAt:i=>''});
const raw3 = (id,names,labels,lgid,extra) => { const c=new LineChart(id,Object.assign({get:rawGet(names),colors:pal,names:labels},extra||{}));
  $(lgid).innerHTML = labels.map((l,i)=>`<span><i data-c="${i}"></i>${l}</span>`).join(''); return c; };
const riskGet = secs => () => { const n=secs*2, a=riskHist.slice(-n); return {ys:[a], xLabels:['-'+(secs>=120?secs/60+' min':secs+' s'),'','now']}; };
const bandsRisk=[{y:30,color:'--low'},{y:55,color:'--elevated'},{y:75,color:'--high'}];
const charts = {
  analytics:[ raw3('ch-acc',['ax','ay','az'],['X','Y','Z'],'lg-acc'), raw3('ch-gyro',['gx','gy','gz'],['X','Y','Z'],'lg-gyro'),
    raw3('ch-flex',['flex1','flex2','flex3'],['Flex 1','Flex 2','Flex 3'],'lg-flex',{min:0,max:4095}), raw3('ch-fsr',['fsr1','fsr2','fsr3'],['FSR 1','FSR 2','FSR 3'],'lg-fsr',{min:0,max:4095}),
    raw3('ch-tilt',['forwardTilt','sideTilt','totalTilt'],['Forward','Side','Total'],'lg-tilt',{min:0}),
    new LineChart('ch-risk',{get:()=>riskGet(riskSecs)(),colors:pal,names:['Risk'],min:0,max:100,bands:bandsRisk,empty:'No risk data yet'}) ],
  dash:[ new LineChart('ch-dash-risk',{get:riskGet(60),colors:pal,names:['Risk'],min:0,max:100,bands:bandsRisk,empty:'No risk data yet'}) ]};
$('lg-risk').innerHTML='<span><i data-c="0"></i>Risk</span><span class="faint">Dashed lines: 30, 55, 75</span>';
function paintLegends(){ const p=pal(); document.querySelectorAll('.legend i[data-c]').forEach(i=>i.style.background=p[+i.dataset.c%p.length]); }
let dirty=true;
(function loop(){ if(dirty){ dirty=false; (charts[cur]||[]).forEach(c=>c.draw()); } setTimeout(()=>requestAnimationFrame(loop),100); })();

/* ---------- router ---------- */
const TITLES={dash:'Dashboard',live:'Live sensors',posture:'Posture',risk:'Risk analysis',analytics:'Analytics',history:'History',device:'Device',settings:'Settings'};
let portsTimer=null;
function show(name){
  if(!TITLES[name]) name='dash'; cur=name;
  document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id==='v-'+name));
  document.querySelectorAll('[data-nav]').forEach(a=>{ a.dataset.nav===name?a.setAttribute('aria-current','page'):a.removeAttribute('aria-current'); });
  $('pageTitle').textContent=TITLES[name]; document.title=TITLES[name]+' | Adaptive ErgoVest';
  $('sheet').classList.remove('open'); window.scrollTo(0,0);
  clearInterval(portsTimer);
  if(name==='history') loadHistory();
  if(name==='settings') loadLan();
  if(name==='device'){ loadPorts(); portsTimer=setInterval(loadPorts,4000); }
  paintLegends(); dirty=true; render();
}
document.querySelectorAll('[data-nav]').forEach(a=>a.addEventListener('click',e=>{ e.preventDefault(); history.pushState(null,'','#/'+a.dataset.nav); show(a.dataset.nav); }));
addEventListener('hashchange',()=>show(location.hash.replace('#/','')||'dash'));
$('moreBtn').onclick=()=>$('sheet').classList.add('open');
$('sheet').addEventListener('click',e=>{ if(e.target===$('sheet')) $('sheet').classList.remove('open'); });
addEventListener('keydown',e=>{ if(e.key==='Escape') $('sheet').classList.remove('open'); });

/* ---------- SSE ---------- */
function applyFrame(f){
  lastMsg=Date.now(); sseUp=true;
  if(f.type==='full' || f.epoch!==epoch){
    COL={}; (f.cols||Object.keys(COL)).forEach((c,i)=>COL[c]=i);
    if(f.type==='full'){ rows=f.rows||[]; riskHist=f.risk_hist||[]; flexHist=f.flex_hist||[]; lastStatusSeq=(f.status||{}).seq??-1; S.events=f.events; S.report=f.report; S.labels=f.labels; S.counts=f.counts; fwdSm=sideSm=null; }
    epoch=f.epoch;
  } else {
    if(f.rows&&f.rows.length){ rows.push(...f.rows); if(rows.length>MAXROWS) rows=rows.slice(-MAXROWS); }
    const st=f.status||{};
    if(st.seq!=null && st.seq!==lastStatusSeq){ lastStatusSeq=st.seq; riskHist.push(st.risk); flexHist.push(st.flex); if(riskHist.length>MAXRISK){ riskHist.shift(); flexHist.shift(); } }
    if(f.events) S.events=f.events;
  }
  const {rows:_r,events:_e,risk_hist:_h,flex_hist:_f,cols:_c,...rest}=f; Object.assign(S,rest);
  if(f.type==='full'){ S.events=f.events; }
  render();
}
let es=null;
function connect(){
  es=new EventSource('/api/stream');
  es.onmessage=e=>{ try{ applyFrame(JSON.parse(e.data)); }catch(err){ console.error(err); } };
  es.addEventListener('auth',()=>{ location.href='/login'; });
  es.onerror=()=>{ sseUp=false; render(); };
}
setInterval(()=>{ if(sseUp && Date.now()-lastMsg>3500){ sseUp=false; render(); } },1000);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden && !sseUp){ try{es.close();}catch(e){} connect(); } });

/* ---------- audio gating (single source of truth) ---------- */
function shouldAlert(h){ return sseUp && h.k==='level' && h.level==='High' && h.fresh; }

/* ---------- render ---------- */
function setLvl(el,cls){ ['lvl-low','lvl-moderate','lvl-elevated','lvl-high','lvl-none'].forEach(c=>el.classList.remove(c)); el.classList.add(cls||'lvl-none'); }
let lastEvSeq=-1, lastRep='';
function render(){
  const h=health(), st=S.status||{}, live=S.mode==='live', cls=h.k==='level'?h.cls:'lvl-none';
  window.EV_HEALTH=h;
  EVAudio.update(shouldAlert(h));
  // header chips
  const mc=$('modeChip'); mc.className='chip '+(live?'live':'demo'); $('modeTxt').textContent=live?'LIVE':'DEMO';
  const cc=$('connChip'); let ct='', cn='chip';
  if(!sseUp){ ct='Reconnecting'; cn+=' bad'; }
  else if(!live){ ct='Simulated data'; cn+=' demo'; }
  else if(S.connection==='ok'){ ct='Connected '+(S.port||''); cn+=' live'; }
  else if(S.connection==='waiting'){ ct='Waiting for data'; cn+=' warn'; }
  else if(S.connection==='stale'){ ct='No data'; cn+=' warn'; }
  else { ct='Not connected'; cn+=' bad'; }
  cc.className=cn; $('connTxt').textContent=ct;
  document.body.classList.toggle('stale-dim', !h.fresh);
  // banners
  const tb=$('taskBanner'), t=S.task||{name:'idle'};
  if(t.name!=='idle'){ tb.classList.add('show'); tb.innerHTML=esc(t.text)+(t.remaining?' &nbsp; '+Math.ceil(t.remaining)+' s':'')+'<small>Keep the vest on and follow the instruction.</small>'; } else tb.classList.remove('show');
  const sb=$('staleBanner'); let sbt='', sbc='banner stale';
  if(!sseUp){ sbt='Connection to the app lost. Sound alerts are paused until it returns.'; sbc='banner offline'; }
  else if(live && S.connection!=='ok'){ sbt=({disconnected:'LIVE mode: no ESP32 connected. Nothing simulated is shown.',waiting:'Port open. Waiting for the ESP32 to send data.',stale:'Sensor data stopped arriving. Showing the last reading, dimmed. Sound alerts are paused.'})[S.connection]||''; }
  sb.className=sbc+(sbt?' show':''); sb.textContent=sbt;

  // dashboard
  setLvl($('heroCard'),cls); $('stateWord').textContent=h.word; $('stateMsg').textContent=h.msg;
  const shown=h.k==='level'; $('riskNum').textContent=shown?Math.round(st.risk):'--';
  $('gaugeVal').style.strokeDashoffset=245*(1-(shown?Math.min(100,st.risk):0)/100);
  document.querySelectorAll('#v-dash .scale div').forEach((d,i)=>d.classList.toggle('cur',shown&&['Low','Moderate','Elevated','High'][i]===st.level));
  const sbd=$('srcBadge'); sbd.textContent=live?'LIVE hardware':'DEMO: simulated'; sbd.className='badge '+(live?'live':'demo');
  $('moveBadge').textContent='Movement: '+(shown&&st.movement?st.movement.replace('_',' '):'--');
  $('alertBar').classList.toggle('show', shouldAlert(h));
  const m=S.model||{};
  $('dConn').textContent=!sseUp?'Offline':!live?'Simulated':({ok:'Connected',stale:'No data',waiting:'Waiting',disconnected:'Not connected'})[S.connection];
  $('dConnSub').textContent=live?(S.port?S.port+' at '+fmt(S.rate,0)+' Hz':'No port open'):'No hardware in DEMO mode';
  $('dCal').textContent=m.has_baseline?'Done':'Needed'; $('dCalSub').textContent=m.preparing?'Preparing demo baseline':m.has_baseline?(m.synthetic?'Simulated baseline':'Worker '+m.worker):'Calibrate on the Device page';
  $('dModel').textContent=m.has_model?'Ready':'Not trained';
  $('dModelSub').textContent=m.has_model?(m.synthetic?'Simulated demo model':(m.meta&&m.meta.cv_accuracy!=null?'Cross-validated '+Math.round(m.meta.cv_accuracy*100)+'% on your data':'Trained on your recordings')):'Needs your recordings';
  $('dLast').textContent=S.age==null?'--':(S.age<1?'now':fmt(S.age,0)+' s ago');
  $('dLastSub').textContent=S.last_row_time?new Date(S.last_row_time*1000).toLocaleTimeString():(live?'No reading yet':'Simulated');
  DT.forEach(d=>{ const e=$('d-'+d[0]); e.textContent=d[0]==='_flex'?fmt(st.flex,0):fmt(val(d[0]),d[3]); });
  // session
  const se=S.session; $('sessBtn').textContent=se?'Stop session':'Start session'; $('sessBtn').className='btn '+(se?'danger':'primary');
  $('sessBadge').textContent=se?'Recording':'Not recording'; $('sessBadge').className='badge '+(se?'ok':'');
  $('sessDur').textContent=se?dur(S.server_time-se.started):'00:00:00'; $('sessWin').textContent=se?se.windows:0;
  $('sessRisk').textContent=se&&se.windows?fmt(se.avg_risk,0)+' / '+fmt(se.max_risk,0):'-- / --'; $('sessCues').textContent=se?se.cues:0;
  $('sessHint').textContent=se&&se.mode==='demo'?'This session is DEMO data and is labelled that way in History.':'Sessions are saved to History under your account.';

  if(cur==='live'){ Object.keys(TILES).forEach(g=>TILES[g].forEach(t=>{ const v=val(t[0]); $('t-'+t[0]).textContent=fmt(v,t[3]); if(t[4]){ $('b-'+t[0]).style.width=v==null?'0':Math.max(0,Math.min(100,v/t[4]*100))+'%'; } }));
    $('liveNote').textContent=live?'Readings straight from the vest, in the units the firmware sends.':'DEMO: these readings are simulated.'; }
  if(cur==='posture') renderPosture(h,cls,st);
  if(cur==='risk') renderRisk(h,cls,st);
  if(cur==='device') renderDevice();
  dirty=true;
}
function arc(el,deg){ const a=Math.min(90,Math.max(0,deg))*Math.PI/180, r=62; el.setAttribute('d',deg>1?`M120 ${212-r}A${r} ${r} 0 0 1 ${(120+r*Math.sin(a)).toFixed(1)} ${(212-r*Math.cos(a)).toFixed(1)}`:'M120 150A62 62 0 0 1 120 150'); }
function renderPosture(h,cls,st){
  setLvl($('postureCard'),cls);
  const f=val('forwardTilt'), s=val('sideTilt'), t=val('totalTilt');
  if(f!=null) fwdSm = fwdSm==null?f:fwdSm+(f-fwdSm)*.4; if(s!=null) sideSm = sideSm==null?s:sideSm+(s-sideSm)*.4;
  const fa=h.fresh&&fwdSm!=null?fwdSm:0, sa=h.fresh&&sideSm!=null?sideSm:0;
  $('torsoSide').setAttribute('transform',`rotate(${Math.min(90,fa).toFixed(1)} 120 212)`); $('torsoRear').setAttribute('transform',`rotate(${Math.min(45,sa).toFixed(1)} 120 212)`);
  arc($('arcSide'),fa); arc($('arcRear'),sa);
  $('pFwd').textContent=fmt(f,0)+(f==null?'':'°'); $('pSide').textContent=fmt(s,0)+(s==null?'':'°');
  $('pFwd2').textContent=fmt(f,1)+'°'; $('pSide2').textContent=fmt(s,1)+'°'; $('pTot2').textContent=fmt(t,1)+'°';
  $('pFlex').textContent=st.flex==null?'--':fmt(st.flex,0)+'°'; $('pLvl').textContent=h.k==='level'?h.word:'--';
  $('postureNote').textContent=S.mode==='live'?'live from the vest':'DEMO: simulated';
}
const NICE={trunk_flexion:'Forward bending',trunk_rotation:'Trunk rotation',velocity_jerk:'Speed and jerk',repetition:'Repetition',loading_proxy:'Load (FSR)',personal_deviation:'Deviation from your calibration',asymmetry:'Left-right pressure difference'};
const FCOL=['#3cc8ff','#5b7cff','#a98bff','#38d996','#ffb12b','#ff7ab0','#8d9aa9'];
function explain(k,st){ return ({
  trunk_flexion:`Forward tilt is ${fmt(st.flex,0)} degrees. Counts from 10 degrees, full at 60.`,
  trunk_rotation:`Peak twist speed ${st.twist} deg/s. Counts above 30 deg/s.`,
  velocity_jerk:`Jerk ${st.jerk} m/s³ compared with your calibrated movement.`,
  repetition:`${st.reps} bending episodes in the last minute. 10 is the maximum.`,
  loading_proxy:`Pressure is ${st.load} ADC above your resting level.`,
  personal_deviation:'How far this movement differs from the movement you calibrated.',
  asymmetry:`FSR 1 and FSR 2 differ by ${st.asym} ADC.`})[k]; }
function renderRisk(h,cls,st){
  setLvl($('riskCard'),cls); $('rWord').textContent=h.word; $('rMsg').textContent=h.msg; $('rNum').textContent=h.k==='level'?Math.round(st.risk):'--';
  document.querySelectorAll('#v-risk .scale div').forEach((d,i)=>d.classList.toggle('cur',h.k==='level'&&['Low','Moderate','Elevated','High'][i]===st.level));
  $('riskNote').textContent=S.mode==='live'?'LIVE':'DEMO: simulated';
  const keys=Object.keys(NICE), c=st.contributors||{}, p=st.points||{};
  if(h.k!=='level'){ $('contrib').innerHTML='<div class="empty"><b>No risk score yet</b>'+esc(h.msg)+'</div>'; $('stack').innerHTML=''; $('stackLegend').innerHTML=''; $('topFactor').textContent=''; }
  else {
    $('stack').innerHTML=keys.map((k,i)=>`<i style="width:${(p[k]||0)}%;background:${FCOL[i]}"></i>`).join('');
    $('stackLegend').innerHTML=keys.map((k,i)=>`<span><i style="background:${FCOL[i]}"></i>${NICE[k]} ${fmt(p[k],0)}</span>`).join('');
    const top=keys.slice().sort((a,b)=>(p[b]||0)-(p[a]||0))[0]; $('topFactor').textContent='Biggest: '+NICE[top];
    $('contrib').innerHTML=keys.map(k=>`<div class="c"><b>${NICE[k]}</b><em class="num">${fmt(p[k],1)} pts</em><div class="t"><i class="${(c[k]||0)>66?'h':(c[k]||0)>33?'m':''}" style="width:${c[k]||0}%"></i></div><small>${esc(explain(k,st))}</small></div>`).join('');
  }
  $('verPct').textContent=S.total?Math.round(100*S.improved/S.total)+'%':'--'; $('verTxt').textContent=(S.improved||0)+' of '+(S.total||0); $('cues').textContent=S.cues||0; $('lastRes').textContent=S.last_result||'--';
  const lc=S.last_cue; $('lastCue').textContent=lc?({L:'Left',R:'Right',B:'Both'}[lc.cmd]||lc.cmd)+', '+lc.why.replace('_',' ')+' (risk '+lc.risk+')'+(lc.sent?'':', not sent'):'--';
  if(S.events && S.event_seq!==lastEvSeq){ lastEvSeq=S.event_seq; $('events').innerHTML=S.events.length?S.events.map(e=>`<div class="k-${esc(e.kind)}"><span class="tm">${esc(e.t)}</span><span>${esc(e.msg)}</span></div>`).join(''):'<div class="empty">No events yet.</div>'; }
}

/* ---------- device ---------- */
let portsKey='';
async function loadPorts(){
  const d=await api('/api/ports'); const ports=d.ports||[], sel=$('portSel'), key=JSON.stringify(ports);
  if(key===portsKey) return; portsKey=key; const keep=sel.value;
  sel.innerHTML=ports.length?ports.map(p=>`<option value="${esc(p.device)}">${esc(p.device)}${p.description?' - '+esc(p.description):''}${p.likely?' (likely ESP32)':''}</option>`).join(''):'<option value="">No ports found. Plug in the ESP32.</option>';
  if(keep&&ports.some(p=>p.device===keep)) sel.value=keep; else if(S.port&&ports.some(p=>p.device===S.port)) sel.value=S.port;
}
function renderDevice(){
  const live=S.mode==='live', m=S.model||{};
  $('devMode').textContent=live?'LIVE':'DEMO'; $('devMode').className='badge '+(live?'live':'demo');
  document.querySelectorAll('#modeSeg button').forEach(b=>b.setAttribute('aria-pressed',String((b.dataset.m==='live')===live)));
  $('modeHint').textContent=live?'LIVE uses only readings from the connected ESP32.':'DEMO shows simulated readings. It never mixes with real data.';
  $('devPort').textContent=S.port||'None'; $('devRate').textContent=live?fmt(S.rate,0)+' Hz':'Simulated 50 Hz'; $('devBad').textContent=(S.diag||{}).bad_lines||0; $('devFw').textContent=(S.diag||{}).fw_message||'--';
  const adm=IS_ADMIN; ['btnConnect','btnDisconnect','portRefresh'].forEach(i=>{ if(!adm) $(i).disabled=true; });
  document.querySelectorAll('#modeSeg button').forEach(b=>b.disabled=!adm); document.querySelector('.admin-note').classList.toggle('hide',adm);
  if(adm){ $('btnDisconnect').disabled=!S.port; $('btnConnect').disabled=!live; $('btnConnect').textContent=S.port?'Reconnect':'Connect'; }
  $('calState').textContent=m.has_baseline?(m.synthetic?'Simulated baseline':'Baseline: '+m.worker):'No baseline yet';
  $('recCount').textContent=((S.counts||{}).labeled_files||0)+' files recorded'; $('modelState').textContent=m.has_model?(m.synthetic?'Simulated demo model':'Model ready'):'No model yet';
  ['btnCal','btnRec','btnTrain'].forEach(i=>$(i).disabled=!adm||S.task&&S.task.name!=='idle');
  if(S.report && S.report!==lastRep){ lastRep=S.report; $('repBox').classList.remove('hide'); $('repTxt').textContent=S.report; }
}
document.querySelectorAll('#modeSeg button').forEach(b=>b.onclick=async()=>{ say(await api('/api/mode','POST',{mode:b.dataset.m})); });
$('portRefresh').onclick=()=>{ portsKey=''; loadPorts(); };
$('btnConnect').onclick=async()=>{ const b=$('btnConnect'); b.disabled=true; b.textContent='Connecting...'; say(await api('/api/connect','POST',{port:$('portSel').value})); renderDevice(); };
$('btnDisconnect').onclick=async()=>say(await api('/api/disconnect','POST',{}));
document.querySelectorAll('[data-cue]').forEach(b=>b.onclick=async()=>say(await api('/api/cue','POST',{cmd:b.dataset.cue})));
$('btnCal').onclick=async()=>say(await api('/api/calibrate','POST',{worker:$('worker').value}));
$('btnRec').onclick=async()=>say(await api('/api/record','POST',{label:$('label').value,seconds:+$('secs').value}));
$('btnTrain').onclick=async()=>say(await api('/api/train','POST',{}));
$('btnCancel').onclick=async()=>say(await api('/api/cancel','POST',{}));
$('btnReset').onclick=async()=>say(await api('/api/reset','POST',{}));
if(!IS_ADMIN) document.querySelectorAll('.admin-only').forEach(e=>e.classList.add('hide'));

/* ---------- sessions / history ---------- */
$('sessBtn').onclick=async()=>{ const d=await api('/api/session/'+(S.session?'stop':'start'),'POST',{}); say(d); };
const LC={Low:'var(--low)',Moderate:'var(--moderate)',Elevated:'var(--elevated)',High:'var(--high)'};
function levelBar(lv){ const tot=Object.values(lv||{}).reduce((a,b)=>a+b,0); if(!tot) return '<div class="stack"></div>'; return '<div class="stack">'+Object.keys(LC).map(k=>`<i title="${k}" style="width:${(lv[k]||0)/tot*100}%;background:${LC[k]}"></i>`).join('')+'</div>'; }
async function loadHistory(){
  const d=await api('/api/sessions'), box=$('sessList'), list=d.sessions||[];
  if(!list.length){ box.innerHTML='<div class="empty"><b>No sessions yet</b>Start a session on the Dashboard. It will be saved here.</div>'; return; }
  box.innerHTML=list.map(s=>{ const dsec=(s.ended||s.started)-s.started; return `<div class="sess" tabindex="0" role="button" data-id="${s.id}"><div class="t"><b>${esc(new Date(s.started*1000).toLocaleString())}</b><small><span class="badge ${s.mode==='demo'?'demo':'live'}">${s.mode==='demo'?'DEMO':'LIVE'}</span> ${esc(s.status)}</small></div>
    <div class="m"><b class="num">${dur(dsec)}</b><small>Duration</small></div><div class="m"><b class="num">${s.avg_risk==null?'--':Math.round(s.avg_risk)+' / '+Math.round(s.max_risk)}</b><small>Avg / peak risk</small></div><div class="m"><b class="num">${s.cues}</b><small>Haptic cues</small></div><div style="min-width:90px">${levelBar(s.levels)}</div></div>`; }).join('');
  box.querySelectorAll('.sess').forEach(el=>{ const go=()=>openSession(+el.dataset.id); el.onclick=go; el.onkeydown=e=>{ if(e.key==='Enter') go(); }; });
}
async function openSession(id){
  const d=await api('/api/sessions/'+id); if(!d.ok) return say(d); const s=d.session, dlg=$('sessDlg'), tot=Object.values(s.levels).reduce((a,b)=>a+b,0);
  $('sessDlgBody').innerHTML=`<h2 style="font-size:20px;margin-bottom:4px">${esc(new Date(s.started*1000).toLocaleString())}</h2>
  <p class="mute" style="margin-bottom:14px"><span class="badge ${s.mode==='demo'?'demo':'live'}">${s.mode==='demo'?'DEMO: simulated data':'LIVE hardware'}</span> &nbsp;${esc(s.status)}${s.note?' ('+esc(s.note)+')':''}</p>
  <div class="grid" style="margin-bottom:14px"><div class="s4 stat"><span class="l">Duration</span><span class="v num">${dur((s.ended||s.started)-s.started)}</span></div><div class="s4 stat"><span class="l">Average risk</span><span class="v num">${s.avg_risk==null?'--':Math.round(s.avg_risk)}</span></div><div class="s4 stat"><span class="l">Peak risk</span><span class="v num">${s.max_risk==null?'--':Math.round(s.max_risk)}</span></div></div>
  ${tot?`<div class="chartcard"><canvas id="sessChart" style="width:100%;height:190px"></canvas></div>
  <h3 style="font-size:14px;margin:14px 0 8px">Time in each category</h3>${levelBar(s.levels)}<div class="legend" style="margin-top:8px">${Object.keys(LC).map(k=>`<span><i style="background:${LC[k]}"></i>${k} ${Math.round((s.levels[k]||0)/tot*100)}%</span>`).join('')}</div>`
  :'<div class="empty"><b>No risk data was recorded</b>The risk score needs a calibration baseline and incoming sensor data.</div>'}
  <p class="hint" style="margin-top:10px">${s.windows} risk windows, ${s.cues} haptic cues. The graph is averaged over short intervals.</p>
  <div class="row" style="margin-top:16px;justify-content:space-between"><div class="row"><a class="btn sm" href="/api/sessions/${s.id}/export.csv"><svg class="ic"><use href="#i-dl"/></svg>Export CSV</a><button class="btn sm danger" id="delSess">Delete</button></div><button class="btn sm" id="closeSess">Close</button></div>`;
  dlg.showModal();
  if(tot){ const series=s.series; const c=new LineChart('sessChart',{get:()=>({ys:[series.map(r=>r[1]),series.map(r=>r[2])],xLabels:['0:00','',dur(series.length?series[series.length-1][0]:0).slice(3)],xAt:i=>dur(series[i][0]).slice(3)}),colors:pal,names:['Average','Peak'],min:0,max:100,bands:bandsRisk}); c.draw(); setTimeout(()=>c.draw(),60); }
  $('closeSess').onclick=()=>dlg.close();
  $('delSess').onclick=async()=>{ if(!confirm('Delete this session permanently?')) return; const r=await api('/api/sessions/'+id,'DELETE'); if(r.ok){ dlg.close(); loadHistory(); toast('Session deleted','ok'); } else toast('Could not delete (is it still recording?)','bad'); };
}

/* ---------- settings ---------- */
async function loadLan(){
  const d=await api('/api/lan'); $('clientsTxt').textContent=(S.clients||d.clients||1)+' device(s) connected';
  $('lanUrls').innerHTML=d.urls&&d.urls.length?d.urls.map(u=>`<div><b>${esc(u)}</b></div>`).join('')+(d.bound==='127.0.0.1'?'<p class="hint">The app was started in local-only mode, so phones cannot connect.</p>':''):'No network address found. Connect the laptop to Wi-Fi.';
  $('qr').innerHTML=d.qr||'';
}
function formErrors(form,d){ form.querySelectorAll('[data-err]').forEach(e=>e.textContent=''); if(d.errors) Object.keys(d.errors).forEach(k=>{ const e=form.querySelector('[data-err="'+k+'"]'); if(e) e.textContent=d.errors[k]; }); }
function bindForm(id,url,after){ const f=$(id); f.addEventListener('submit',async e=>{ e.preventDefault(); const body={}; [...f.elements].forEach(x=>{ if(x.name) body[x.name]=x.value; }); const d=await api(url,'POST',body); formErrors(f,d); say(d); if(d.ok&&after) after(d); }); }
bindForm('profForm','/api/me/profile',d=>{ $('meName').textContent=d.user.full_name; $('avatar').textContent=d.user.full_name.slice(0,1).toUpperCase(); });
bindForm('pwForm','/api/me/password',()=>$('pwForm').reset());
$('logoutBtn').onclick=async()=>{ const d=await api('/api/auth/logout','POST',{}); location.href=d.next||'/login'; };
function setTheme(t){ try{ localStorage.setItem('ev-theme',t); }catch(e){} const eff=t==='system'?(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'):t; document.documentElement.setAttribute('data-theme',eff);
  document.querySelectorAll('#themeSeg button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.t===t))); paintLegends(); dirty=true; }
document.querySelectorAll('#themeSeg button').forEach(b=>b.onclick=()=>setTheme(b.dataset.t));
try{ const t=localStorage.getItem('ev-theme')||'dark'; document.querySelectorAll('#themeSeg button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.t===t))); }catch(e){}

/* ---------- sound switch (OFF every page load; no test beep) ---------- */
function paintSound(on){
  $('soundBtn').classList.toggle('on',on); $('soundBtn').setAttribute('aria-pressed',String(on));
  $('soundBtn').setAttribute('aria-label',on?'Sound alerts on. Turn off':'Sound alerts off. Turn on'); $('soundBtn').firstElementChild.firstElementChild.setAttribute('href',on?'#i-vol':'#i-voloff');
  $('soundSw').setAttribute('aria-checked',String(on));
}
async function toggleSound(){
  if(EVAudio.enabled){ EVAudio.disable(); paintSound(false); toast('Sound alerts off'); return; }
  const ok=await EVAudio.enable();
  if(!ok){ toast('This browser cannot play audio','bad'); return; }
  paintSound(true); toast('Sound on. It plays only at HIGH RISK, from this device.','ok'); render();
}
$('soundBtn').onclick=toggleSound; $('soundSw').onclick=toggleSound;
try{ const v=+localStorage.getItem('ev-vol'); if(v){ $('vol').value=v; EVAudio.setVolume(v/100); } }catch(e){}
$('vol').oninput=()=>{ EVAudio.setVolume($('vol').value/100); try{ localStorage.setItem('ev-vol',$('vol').value); }catch(e){} };

/* ---------- misc ---------- */
document.querySelectorAll('#rawRange button').forEach(b=>b.onclick=()=>{ rawSecs=+b.dataset.s; document.querySelectorAll('#rawRange button').forEach(x=>x.setAttribute('aria-pressed',String(x===b))); dirty=true; });
document.querySelectorAll('#riskRange button').forEach(b=>b.onclick=()=>{ riskSecs=+b.dataset.s; document.querySelectorAll('#riskRange button').forEach(x=>x.setAttribute('aria-pressed',String(x===b))); dirty=true; });
addEventListener('resize',()=>{ dirty=true; });
window.EV={get state(){return S;},get rows(){return rows;},health,get sseUp(){return sseUp;}};
paintLegends(); show((location.hash.replace('#/','')||'dash')); connect();
})();
