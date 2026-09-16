import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {createRoot} from 'react-dom/client'
import {fetch as tauriFetch} from '@tauri-apps/plugin-http'
import {check} from '@tauri-apps/plugin-updater'
import {invoke} from '@tauri-apps/api/core'
import {
  ReactFlow, Background, Controls, MiniMap, addEdge, MarkerType,
  useNodesState, useEdgesState, Handle, Position, ConnectionLineType, Panel
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import './styles.css'
import embeddedReference from './flowsheet_reference.json'

const isDesktop=typeof window!=='undefined'&&('__TAURI_INTERNALS__' in window)
const API = import.meta.env.VITE_API_URL || (isDesktop?'http://127.0.0.1:47831':'http://localhost:8000')
const apiFetch:typeof window.fetch = isDesktop ? (tauriFetch as typeof window.fetch) : window.fetch.bind(window)

type BlockDef={id:string,type:string,name:string,params:Record<string,any>,position:{x:number,y:number}}
type Connection={id:string,from_block:string,from_port:string,to_block:string,to_port:string}
type Flowsheet={name:string,blocks:BlockDef[],connections:Connection[]}

const fallbackBlockLibrary=[
  ['raw_feed','Raw Miscanthus Feed',[],['feed'],{as_received_feed_tph:3.5294,dry_matter_fraction:.85,temperature_C:15}],
  ['water_supply','Process Water Supply',[],['water'],{flow_tph:11.4705882353,temperature_C:15,pressure_bar_abs:2,density_kg_per_m3:999}],
  ['feed_preparation','Feed Preparation / Slurry Make-up',['raw_feed','process_water'],['slurry'],{target_slurry_dry_matter_fraction:.20}],
  ['maceration','Maceration / Size Reduction',['feed'],['outlet'],{specific_energy_kWh_per_t_feed:12,manual_electrical_load_kW:0,electrical_load_factor_fraction:1,annual_operating_hours:8000}],
  ['pretreatment','Pretreatment',['feed'],['slurry'],{}],['hydrolysis','Hydrolysis',['feed'],['hydrolysate'],{}],
  ['fermentation','Fermentation',['feed'],['broth','co2'],{}],['solids_separation','Solids Separation',['feed'],['liquid','cake'],{}],
  ['beer_column','Beer Column',['feed'],['overhead','bottoms'],{}],['rectifier','Rectifier',['feed'],['overhead','bottoms'],{}],
  ['molecular_sieve','Molecular Sieve',['feed'],['product','recycle'],{}],
  ['pump','Transfer Pump',['feed'],['outlet'],{delta_p_bar:2,efficiency_fraction:.7,density_kg_per_m3:1000,manual_electrical_load_kW:0,electrical_load_factor_fraction:1,annual_operating_hours:8000}],
  ['heat_exchanger','Heat Exchanger',['process_in'],['process_out'],{target_process_outlet_temperature_C:50,process_cp_kJ_per_kgK:4,utility_supply_temperature_C:90,utility_return_temperature_C:70,utility_cp_kJ_per_kgK:4.18,overall_U_W_per_m2K:500,design_margin_fraction:.1}],
  ['heat_generator','Heat Generator / Thermal Header',[],[],{supply_temperature_C:90,return_temperature_C:70,generator_efficiency_fraction:.9,manual_thermal_demand_kW:0,available_recovered_heat_kW:0}],
  ['heater_cooler','Heater / Cooler',['feed'],['outlet'],{target_temperature_C:50,cp_kJ_per_kgK:4}],
  ['heat_recovery','Heat Recovery',['hot_feed','cold_feed'],['hot_out','cold_out'],{effectiveness:.7}],
  ['mixer','Mixer',['inlet_a','inlet_b'],['outlet'],{}],['splitter','Splitter',['feed'],['outlet_a','outlet_b'],{fraction_to_a:.5}],
  ['tank','Tank',['feed'],['outlet'],{residence_time_h:1,density_t_per_m3:1}],['flash_letdown','Flash / Letdown',['feed'],['vapour','liquid'],{}],
  ['beer_conditioning','Beer Conditioning / Preheat',['feed'],['outlet'],{target_temperature_C:90,cp_kJ_per_kgK:4}],
  ['material_dose','Material Dose',[],['dose'],{}],['enzyme_dose','Enzyme Dose',['substrate'],[],{}],['nutrient_dose','Nutrient Dose',['broth_basis'],[],{}],
  ['product_sink','Product',['feed'],[],{}],['wastewater_sink','Wastewater',['feed'],[],{}],['vent_sink','Vent',['feed'],[],{}],
  ['solid_sink','Solid Product / Waste',['feed'],[],{}],['recycle_sink','Recycle Placeholder',['feed'],[],{}],
  ['wastewater_collector','Wastewater Collector',['inlet_a','inlet_b'],['outlet'],{}],['quality_recycle','Quality Recycle',['feed'],['product','recycle'],{}],
  ['utility_header','Utility Header',[],[],{}],['batch_scheduler','Batch Scheduler',[],[],{}],['cip_demand','CIP Demand',[],[],{}],
  ['cooling_water','Cooling Water Demand',[],[],{}],['distillation_utility_envelope','Distillation Utility Envelope',['product_basis'],['product_out'],{}]
].map(([type,display_name,inputs,outputs,default_params]:any)=>({type,display_name,default_params,
  input_ports:Object.fromEntries(inputs.map((name:string)=>[name,{name,direction:'in'}])),
  output_ports:Object.fromEntries(outputs.map((name:string)=>[name,{name,direction:'out'}]))}))

const pretty=(s:string)=>s.replaceAll('_',' ').replace(/\b\w/g,m=>m.toUpperCase())
const fmt=(v:any,d=3)=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(d):String(v??'—')
const majorComponents=(components:Record<string,number>={})=>Object.entries(components).filter(([,v])=>Math.abs(Number(v))>1e-9).sort((a,b)=>Math.abs(Number(b[1]))-Math.abs(Number(a[1]))).slice(0,4).map(([k,v])=>`${pretty(k)} ${fmt(v,4)}`).join(', ')||'—'

function parameterMeta(key:string){
  const k=key.toLowerCase()
  if(k==='efficiency_fraction') return {unit:'%',min:.01,max:1,step:.01,help:'Pump efficiency: fraction of shaft/electrical input converted to useful hydraulic power. 0.70 = 70%.'}
  if(k==='delta_p_bar') return {unit:'bar',min:0,step:.1,help:'Pump Differential Pressure ΔP: required pressure rise from pump suction to discharge. Used with actual flow and efficiency to calculate power.'}
  if(k==='dry_matter_fraction'||k==='target_slurry_dry_matter_fraction') return {unit:'fraction',min:.01,max:1,step:.01,help:'Dry matter fraction. For example 0.20 = 20% DM.'}
  if(k==='manual_electrical_load_kw') return {unit:'kW',min:0,step:.1,help:'Optional vendor/manual connected electrical load. Where a calculated equipment load exists, a non-zero manual value overrides it.'}
  if(k==='electrical_load_factor_fraction') return {unit:'fraction',min:0,max:1,step:.01,help:'Fraction of connected electrical load applied during operation.'}
  if(k==='annual_operating_hours') return {unit:'h/y',min:0,step:100,help:'Annual operating hours used to calculate electrical energy consumption.'}
  if(k==='overall_u_w_per_m2k') return {unit:'W/m²K',min:1,step:10,help:'Overall heat-transfer coefficient U used with LMTD to calculate exchanger area.'}
  if(/fraction|conversion|yield|recovery|efficiency|moisture|purity|entrainment/.test(k)) return {unit:'fraction',min:0,max:1,step:.01,help:'Enter a fraction between 0 and 1. For example, 0.85 means 85%.'}
  if(/temperature|_c$/.test(k)) return {unit:'°C',step:1,help:'Operating temperature in degrees Celsius.'}
  if(/pressure/.test(k)) return {unit:'bar(a)',min:0,step:.1,help:'Operating pressure in bar.'}
  if(/time|_h$|hours/.test(k)) return {unit:'h',min:0,step:.1,help:'Operating or residence time in hours.'}
  if(/_kw|duty|power|heat/.test(k)) return {unit:'kW',min:0,step:1,help:'Thermal or electrical duty in kilowatts.'}
  if(/m3ph|m3_per_h|pump_rate/.test(k)) return {unit:'m³/h',min:0.01,step:.1,help:'Volumetric flow rate.'}
  if(/_tph|flow/.test(k)) return {unit:'t/h',min:0,step:.01,help:'Continuous mass flow in tonnes per hour.'}
  if(/volume|_m3/.test(k)) return {unit:'m³',min:0,step:1,help:'Working or installed volume in cubic metres.'}
  if(/density/.test(k)) return {unit:'kg/m³',min:0,step:1,help:'Material density.'}
  if(/count|vessels|stages/.test(k)) return {unit:'count',min:0,step:1,help:'Whole-number equipment count.'}
  return {unit:'',step:'any' as any,help:'Engineering input. Review the block basis before changing this value.'}
}

function downloadText(filename:string,text:string,type='application/json'){
  const url=URL.createObjectURL(new Blob([text],{type}));const a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
}

function statusClass(s:string){
  const v=(s||'').toUpperCase()
  if(v.includes('OPEN')||v.includes('TBC')) return 'warning'
  if(v.includes('PROVISIONAL')||v.includes('SCREENING')) return 'provisional'
  if(v.includes('CALCULATED')||v.includes('SELECTED')||v.includes('COMPLETE')) return 'good'
  return 'neutral'
}

function UtilisationBar({value}:any){
  const pct=Math.max(0,Number(value||0)*100),width=Math.min(pct,100)
  const tone=pct>100?'danger':pct>90?'warning':'good'
  return <div className="utilisation-cell"><div className="utilisation-track"><i className={tone} style={{width:`${width}%`}}/></div><strong>{fmt(pct,1)}%</strong></div>
}

function BatchTimeline({schedule}:any){
  const total=Number(schedule?.cycle_time_h||1)
  return <div className="batch-timeline"><div className="timeline-label"><strong>{schedule.block_name}</strong><span>{schedule.installed_vessels} installed · {fmt(total,1)} h cycle</span></div><div className="phase-track">{(schedule.phases||[]).map((p:any,i:number)=><div key={`${p.name}-${i}`} className={`phase phase-${i%5}`} style={{width:`${Math.max(4,(Number(p.duration_h||0)/total)*100)}%`}} title={`${pretty(p.name)}: ${fmt(p.duration_h,2)} h`}><span>{pretty(p.name)}</span><small>{fmt(p.duration_h,1)}h</small></div>)}</div></div>
}

function MiniSeries({rows,valueKey,label,color}:any){
  const source=rows||[],step=Math.max(1,Math.ceil(source.length/150)),sample=source.filter((_:any,i:number)=>i%step===0)
  const allValues=source.map((r:any)=>Number(r[valueKey]||0)),values=sample.map((r:any)=>Number(r[valueKey]||0)),max=Math.max(...allValues,1),min=Math.min(...allValues,0),average=allValues.reduce((a:number,b:number)=>a+b,0)/(allValues.length||1)
  const points=values.map((v:number,i:number)=>`${sample.length<2?0:(i/(sample.length-1))*100},${38-((v-min)/(max-min||1))*34}`).join(' ')
  return <div className="series-card"><div><strong>{label}</strong><span>{fmt(average,0)} avg · {fmt(max,0)} peak</span></div><svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-label={label}><line x1="0" y1="38" x2="100" y2="38"/><polyline points={points} style={{stroke:color}}/></svg><small>0 h <span>{fmt(source.at(-1)?.time_h||168,0)} h</span></small></div>
}

function TwinVessel({label,state,count,tone,level}:any){
  const active=String(state||'AVAILABLE')!=='AVAILABLE'
  const transferring=/FILL|EMPTY/.test(String(state||''))
  return <div className={`twin-unit ${active?'is-active':''} ${transferring?'is-transferring':''}`}>
    <div className="twin-vessel" style={{'--liquid':`${Math.max(8,Math.min(92,level))}%`,'--tone':tone} as any}>
      <div className="vessel-neck"/><div className="vessel-body"><i/><span>{count}</span></div><div className="vessel-legs"><i/><i/></div>
    </div>
    <strong>{label}</strong><span>{pretty(state||'AVAILABLE')}</span>
  </div>
}

function VesselBank({row,schedules}:any){
  const sections=[['pretreat','P02 Pretreatment','#cb7a36'],['hydro','P04 Hydrolysis','#4c9a70'],['ferm','P05 Fermentation','#6779b8']]
  return <div className="vessel-bank">{sections.map(([id,label,tone]:any)=>{
    const schedule=(schedules||[]).find((v:any)=>v.block_id===id)||{}
    const count=Number(schedule.installed_vessels||0)
    return <section key={id} className="vessel-bank-section"><div className="vessel-bank-head"><div><span>{label}</span><strong>{count} vessels · {fmt(schedule.vessel_working_volume_m3,0)} m³ working volume</strong></div><small>{fmt(schedule.utilisation_fraction*100,1)}% design utilisation</small></div><div className="vessel-bank-grid">
      {Array.from({length:count},(_,i)=>{const vid=`${id}-V${String(i+1).padStart(2,'0')}`;const state=row.vessel_state?.[vid]||'AVAILABLE';const frac=Number(row.vessel_fill_fraction?.[vid]||0);const mass=Number(row.vessel_inventory_t?.[vid]||0);const batches=row.vessel_batch_ids?.[vid]||[];return <article key={vid} className={`live-vessel-card state-${String(state).toLowerCase().replaceAll('_','-')}`} style={{'--vessel-tone':tone} as any}><div className="live-vessel-graphic"><i style={{height:`${Math.max(2,Math.min(100,frac*100))}%`}}/></div><div><span>{vid}</span><strong>{pretty(state)}</strong><small>{fmt(mass,1)} t · {fmt(frac*100,0)}%</small>{batches.length>0&&<em>{batches.join(' + ')}</em>}</div></article>})}
    </div></section>
  })}</div>
}

function PumpPanel({row,dynamic}:any){
  const pumps=dynamic?.shared_pumps||[]
  return <div className="pump-panel">{pumps.map((p:any)=>{const state=row.pump_state?.[p.pump_id]||'IDLE';const owner=row.pump_owner?.[p.pump_id];return <div key={p.pump_id} className={`pump-chip ${state==='BUSY'?'busy':''}`}><i/><span>{p.name}</span><strong>{state}</strong><small>{owner||'Available'}</small></div>})}</div>
}

function SchedulerGantt({dynamic}:any){
  const rows=dynamic?.timeline||[]
  if(!rows.length)return null
  const horizon=Number(dynamic.horizon_h||rows.at(-1)?.time_h||168)
  const vesselIds=Object.keys(rows[0]?.vessel_state||{})
  const segmentsFor=(vid:string)=>{
    const out:any[]=[];let current='';let start=0
    rows.forEach((r:any,i:number)=>{const state=r.vessel_state?.[vid]||'AVAILABLE';if(i===0){current=state;start=Number(r.time_h||0);return}if(state!==current){out.push({state:current,start,end:Number(r.time_h||0)});current=state;start=Number(r.time_h||0)}})
    out.push({state:current,start,end:horizon});return out
  }
  return <div className="gantt-wrap"><div className="gantt-axis"><span>Vessel</span><div>{[0,25,50,75,100].map(p=><i key={p} style={{left:`${p}%`}}><b>{fmt(horizon*p/100,0)}h</b></i>)}</div></div>{vesselIds.map(vid=><div className="gantt-row" key={vid}><strong>{vid}</strong><div>{segmentsFor(vid).map((s:any,i:number)=><span key={i} className={`gantt-segment state-${String(s.state).toLowerCase().replaceAll('_','-')}`} style={{left:`${(s.start/horizon)*100}%`,width:`${Math.max(.25,((s.end-s.start)/horizon)*100)}%`}} title={`${pretty(s.state)} · ${fmt(s.start,2)}–${fmt(s.end,2)} h`}/>)}</div></div>)}</div>
}

function PlantDashboard({results}:any){
  const dynamic=results?.dynamic_plant||{}
  const throughput=dynamic.connected_throughput||{}
  const util=results?.utility_totals||{}
  const closure=results?.overall_material_closure||{}
  const op=dynamic.operability||{}
  const annualL=Number(throughput.ethanol_product_L_per_8000h_year||0)
  const elec=Number(util.electricity_kW||0),heat=Number(util.thermal_kW||0)
  const productLph=annualL/8000
  const specificElec=productLph>0?elec/productLph:0
  const specificHeat=productLph>0?heat/productLph:0
  const bottleneck=(dynamic.vessel_schedules||[]).slice().sort((a:any,b:any)=>Number(b.utilisation_fraction||0)-Number(a.utilisation_fraction||0))[0]
  return <div className="plant-dashboard">
    <div className="page-hero"><div><span className="eyebrow">Plant dashboard</span><h2>Current design at a glance</h2><p>Production, utilities, scheduling health and engineering confidence from the latest run.</p></div><div className={`rag-status ${results?.errors?.length?'danger':results?.warnings?.length?'warning':'good'}`}><i/>{results?.errors?.length?'ACTION REQUIRED':results?.warnings?.length?'REVIEW WARNINGS':'MODEL HEALTHY'}</div></div>
    <div className="dashboard-kpi-grid">
      <article><span>Ethanol production</span><strong>{fmt(productLph,1)} L/h</strong><small>{fmt(annualL/1e6,2)} ML per 8,000 h year</small></article>
      <article><span>Scheduled feed</span><strong>{fmt(throughput.average_completed_feed_tph,3)} t/h</strong><small>Completed end-to-end feed</small></article>
      <article><span>Electricity</span><strong>{fmt(elec,1)} kW</strong><small>{fmt(specificElec,3)} kWh/L ethanol</small></article>
      <article><span>Process heat</span><strong>{fmt(heat,1)} kW</strong><small>{fmt(specificHeat,3)} kWh/L ethanol</small></article>
      <article><span>Material closure</span><strong>{fmt(closure.closure_error_tph||0,6)} t/h</strong><small>{Math.abs(Number(closure.closure_error_tph||0))<1e-6?'Closed':'Review balance'}</small></article>
      <article><span>Current bottleneck</span><strong>{bottleneck?.block_name||'—'}</strong><small>{fmt(Number(bottleneck?.utilisation_fraction||0)*100,1)}% design utilisation</small></article>
    </div>
    <div className="dashboard-two-col">
      <section className="dashboard-panel"><div className="section-heading"><div><span>Operability</span><h3>Scheduler health</h3></div></div><div className="metric-list"><div><span>Blocking events</span><strong>{op.blocking_events||0}</strong></div><div><span>Starvation events</span><strong>{op.starvation_events||0}</strong></div><div><span>Pump contention events</span><strong>{op.pump_contention_events||0}</strong></div><div><span>Connected mass-balance error</span><strong>{fmt(throughput.mass_balance_error_t||0,8)} t</strong></div></div></section>
      <section className="dashboard-panel"><div className="section-heading"><div><span>Engineering basis</span><h3>Open assumptions & decisions</h3></div></div>{(results?.open_decisions||[]).slice(0,8).map((d:any)=><div className="decision-row compact" key={d.block_id}><span className={`status-dot ${statusClass(d.status)}`}/><div><strong>{d.block_name}</strong><small>{d.note||d.basis}</small></div></div>)}{!(results?.open_decisions||[]).length&&<div className="empty-small">No open design decisions reported.</div>}</section>
    </div>
    <div className="dashboard-panel"><div className="section-heading"><div><span>Section utilisation</span><h3>Batch capacity margin</h3></div></div><div className="dashboard-util-grid">{(dynamic.vessel_schedules||[]).map((v:any)=><div key={v.block_id}><span>{v.block_name}</span><UtilisationBar value={v.utilisation_fraction}/><small>{v.installed_vessels} installed · {v.required_vessels} required</small></div>)}</div></div>
  </div>
}

function DigitalTwin({dynamic,results}:any){
  const rows=dynamic?.timeline||[]
  const [index,setIndex]=useState(0),[playing,setPlaying]=useState(false),[speed,setSpeed]=useState(4)
  useEffect(()=>{if(!playing||rows.length<2)return;const timer=window.setInterval(()=>setIndex(i=>(i+1)%rows.length),Math.max(80,800/speed));return()=>window.clearInterval(timer)},[playing,speed,rows.length])
  useEffect(()=>setIndex(0),[rows])
  const row=rows[Math.min(index,Math.max(0,rows.length-1))]||{}
  const schedules=dynamic?.vessel_schedules||[]
  const schedule=(id:string)=>schedules.find((v:any)=>v.block_id===id)||{}
  const states=(id:string)=>row.states?.[id]||[]
  const state=(id:string)=>states(id).find((s:string)=>s!=='AVAILABLE'&&s!=='STARVED')||states(id)[0]||'AVAILABLE'
  const averageLevel=(id:string)=>{const vals=Object.entries(row.vessel_fill_fraction||{}).filter(([k])=>k.startsWith(`${id}-`)).map(([,v]:any)=>Number(v||0));return vals.length?Math.max(8,Math.min(92,(vals.reduce((a,b)=>a+b,0)/vals.length)*84+8)):8}
  const transferActive=(from:string,to:string)=>Object.entries(row.pump_owner||{}).some(([pump,owner]:any)=>pump.startsWith(from+'_out')&&owner)||Object.entries(row.pump_owner||{}).some(([pump,owner]:any)=>pump.startsWith(to+'_in')&&owner)
  const pretreat=schedule('pretreat'),hydro=schedule('hydro'),ferm=schedule('ferm')
  const throughput=dynamic?.connected_throughput||{}
  const op=dynamic?.operability||{}
  const Box=({title,sub,tone='#526170'}:any)=><div style={{minWidth:120,padding:'12px 10px',border:'1px solid #d3dde3',borderRadius:10,background:'#fff',textAlign:'center'}}><div style={{width:28,height:28,borderRadius:8,margin:'0 auto 7px',background:tone,opacity:.16}}/><strong style={{display:'block'}}>{title}</strong><span style={{fontSize:11,color:'#65727c'}}>{sub}</span></div>
  const Pipe=({active=false,label=''}:any)=><div className={`twin-pipe ${active?'flowing':''}`} style={{minWidth:54}}><i/>{label&&<b>{label}</b>}</div>
  return <div className="twin-view">
    <div className="twin-toolbar"><div><span>V0.23 P01–P12 LIVE PLANT REPLAY</span><strong>Plant time {fmt(row.time_h||0,2)} h</strong></div><div className="twin-controls"><button onClick={()=>setPlaying(v=>!v)}>{playing?'Pause':'Play'}</button><button onClick={()=>setIndex(i=>Math.min(i+1,rows.length-1))}>Step</button><label>Speed <select value={speed} onChange={e=>setSpeed(Number(e.target.value))}><option value="1">1×</option><option value="4">4×</option><option value="12">12×</option><option value="32">32×</option></select></label></div></div>
    <input className="twin-scrubber" type="range" min="0" max={Math.max(0,rows.length-1)} value={index} onChange={e=>{setPlaying(false);setIndex(Number(e.target.value))}} aria-label="Digital twin time"/>
    <div style={{overflowX:'auto',padding:'8px 0 18px'}}><div style={{display:'flex',alignItems:'center',minWidth:1900,gap:4}}>
      <div className="feed-hopper"><div className="hopper-bin"/><strong>P01 Miscanthus + water</strong><span>Feed preparation & slurry make-up</span></div>
      <Pipe active={true} label="Feed pump"/>
      <TwinVessel label="P02 Pretreatment" state={state('pretreat')} level={averageLevel('pretreat')} count={`${pretreat.installed_vessels||0} × ${fmt(pretreat.vessel_working_volume_m3,0)} m³`} tone="#cb7a36"/>
      <Pipe active={transferActive('pretreat','hydro')} label="Hot discharge"/>
      <Box title="P03 Heat recovery" sub="180°C → 50°C · heat returned to cold feed" tone="#cb7a36"/>
      <Pipe active={transferActive('pretreat','hydro')} label="To P04"/>
      <TwinVessel label="P04 Hydrolysis" state={state('hydro')} level={averageLevel('hydro')} count={`${hydro.installed_vessels||0} × ${fmt(hydro.vessel_working_volume_m3,0)} m³`} tone="#4c9a70"/>
      <Pipe active={transferActive('hydro','ferm')} label="Direct transfer"/>
      <TwinVessel label="P05 Fermentation" state={state('ferm')} level={averageLevel('ferm')} count={`${ferm.installed_vessels||0} × ${fmt(ferm.vessel_working_volume_m3,0)} m³`} tone="#6779b8"/>
      <Pipe active={true}/><Box title="P06 Solids separation" sub="Post-fermentation cake + beer" tone="#68737b"/><Pipe active={true}/><Box title="P07 Beer conditioning" sub="32→90°C · bottoms economiser" tone="#c46b2b"/><Pipe active={true}/>
      <div className="twin-column"><div className="column-stack"><i/><i/><i/><i/><i/></div><strong>P08 Beer column</strong><span>32 trays · 40 wt% side draw</span></div>
      <Pipe active={true}/><div className="twin-column"><div className="column-stack"><i/><i/><i/><i/><i/></div><strong>P09 Rectification</strong><span>45 trays · 92.5 wt% overhead</span></div>
      <Pipe active={true}/><Box title="P10 Molecular sieve" sub="3A · 99.5 wt% · recycle to P09 tray 14" tone="#547fc1"/><Pipe active={true}/><Box title="P12 Utilities" sub="Integrated heat / steam / cooling" tone="#c46b2b"/><Pipe active={true}/>
      <div className="twin-product"><div>EtOH</div><strong>P11 Anhydrous ethanol</strong><span>{fmt(results?.terminal_component_totals?.ethanol,3)} t/h</span></div>
    </div></div>
    <div style={{display:'grid',gridTemplateColumns:'repeat(5,minmax(130px,1fr))',gap:8,margin:'4px 0 12px'}}>
      <div className="dashboard-card"><span>CO₂ vent</span><strong>Fermentation side stream</strong></div>
      <div className="dashboard-card"><span>Residue solids</span><strong>Separation cake</strong></div>
      <div className="dashboard-card"><span>Beer bottoms</span><strong>Wastewater stream</strong></div>
      <div className="dashboard-card"><span>Rectifier bottoms</span><strong>Wastewater stream</strong></div>
      <div className="dashboard-card"><span>P10 sieve recycle</span><strong>72 wt% EtOH → P09 tray 14 · converged recycle</strong></div>
    </div>
    <div className="twin-readouts"><div><span>Completed feed</span><strong>{fmt(throughput.average_completed_feed_tph,2)} t/h</strong></div><div><span>Annual ethanol</span><strong>{fmt((throughput.ethanol_product_L_per_8000h_year||0)/1e6,2)} ML/y</strong></div><div><span>Blocked events</span><strong>{op.blocking_events||0}</strong></div><div><span>Pump contention</span><strong>{op.pump_contention_events||0}</strong></div></div>
    <section className="twin-live-section"><div className="section-heading"><div><span>Live batch vessels</span><h3>Individual vessel inventory & state</h3></div><p>Levels, states and batch lineage are taken directly from the connected event timeline.</p></div><VesselBank row={row} schedules={schedules}/></section>
    <section className="twin-live-section"><div className="section-heading"><div><span>Active transfers</span><h3>Batch movement</h3></div><p>Only direct vessel-to-vessel transfers generated by the event engine are shown.</p></div><div className="active-transfer-grid">{(row.active_transfers||[]).length?(row.active_transfers||[]).map((x:any,i:number)=><div key={i}><span>{x.from}</span><b>→</b><span>{x.to}</span><small>{(x.batch_ids||[]).join(' + ')||'Batch transfer'}</small></div>):<div className="transfer-idle">No vessel-to-vessel transfer active at this time.</div>}</div></section>
    <section className="twin-live-section"><div className="section-heading"><div><span>Transfer resources</span><h3>Pump status</h3></div><p>Busy pumps show the vessel currently owning the resource.</p></div><PumpPanel row={row} dynamic={dynamic}/></section>
    <p className="model-boundary"><strong>V0.23 spreadsheet-aligned boundary:</strong> the application follows the master P01–P12 process sequence. P03 heat recovery is explicit after P02 and returns recovered sensible heat energetically to the incoming cold slurry; P07 represents beer-column-bottoms economising. P10 regeneration recycle is iteratively converged back to P09 on the workbook 72 wt% tear-stream basis. P02/P04/P05 batch states are event-resolved; P06–P10 remain continuous engineering-screening calculations.</p>
  </div>
}

function SchedulerPage({dynamic,flow}:any){
  if(!dynamic)return <div className="primary-page-empty"><strong>No scheduler data yet</strong><p>Run the current flowsheet to generate the connected batch schedule.</p></div>
  const schedules=dynamic.vessel_schedules||[]
  const op=dynamic.operability||{}
  const throughput=dynamic.connected_throughput||{}
  return <div className="scheduler-page">
    <div className="page-hero"><div><span className="eyebrow">Connected operations scheduler</span><h2>Batch sequencing & plant operability</h2><p>Direct vessel-to-vessel scheduling for P02 Pretreatment, P04 Hydrolysis and P05 Fermentation, including transfer pumps, blocking and starvation.</p></div><div className={`rag-status ${dynamic.plant_feasible_at_selected_throughput?'good':'danger'}`}><i/>{dynamic.plant_feasible_at_selected_throughput?'CAPACITY FEASIBLE':'CAPACITY CONSTRAINT'}</div></div>
    <div className="result-kpis operations-kpis">
      <div><span>Completed feed</span><strong>{fmt(throughput.average_completed_feed_tph,3)} <small>t/h</small></strong><p>End-to-end scheduled throughput</p></div>
      <div><span>Annual ethanol</span><strong>{fmt((throughput.ethanol_product_L_per_8000h_year||0)/1e6,2)} <small>ML/y</small></strong><p>Based on completed scheduled feed</p></div>
      <div><span>Blocking events</span><strong>{op.blocking_events||0}</strong><p>Upstream waits for downstream capacity</p></div>
      <div><span>Pump contention</span><strong>{op.pump_contention_events||0}</strong><p>Transfer resource conflicts</p></div>
    </div>
    <section className="operations-section"><div className="section-heading"><div><span>Installed capacity</span><h3>Vessel and transfer-pump assessment</h3></div><p>Fill and empty durations are working volume ÷ selected transfer-pump rate.</p></div><div className="vessel-cards">{schedules.map((v:any)=><article key={v.block_id} className={v.bottleneck?'constraint':''}><div><span>{v.block_name}</span><strong>{v.installed_vessels} installed / {v.required_vessels} required</strong></div><UtilisationBar value={v.utilisation_fraction}/><dl><div><dt>Working volume</dt><dd>{fmt(v.vessel_working_volume_m3,1)} m³</dd></div><div><dt>Batch mass</dt><dd>{fmt(v.batch_mass_t,1)} t</dd></div><div><dt>Inlet pump</dt><dd>{fmt(v.inlet_transfer_pump_rate_m3ph,2)} m³/h</dd></div><div><dt>Fill time</dt><dd>{fmt(v.fill_time_h,2)} h</dd></div><div><dt>Outlet pump</dt><dd>{fmt(v.outlet_transfer_pump_rate_m3ph,2)} m³/h</dd></div><div><dt>Empty time</dt><dd>{fmt(v.empty_time_h,2)} h</dd></div><div><dt>Cycle</dt><dd>{fmt(v.cycle_time_h,2)} h</dd></div><div><dt>Capacity</dt><dd>{fmt(v.capacity_tph,3)} t/h</dd></div></dl></article>)}</div></section>
    <section className="operations-section"><div className="section-heading"><div><span>Vessel schedule</span><h3>Full production Gantt</h3></div><p>Actual state occupancy for every installed batch vessel across the simulation horizon.</p></div><SchedulerGantt dynamic={dynamic}/></section>
    <section className="operations-section"><div className="section-heading"><div><span>Representative cycles</span><h3>Configured batch cycle</h3></div><p>Reference phase durations used by the event engine.</p></div><div className="timeline-list">{schedules.map((v:any)=><BatchTimeline key={v.block_id} schedule={v}/>)}</div></section>
    <section className="operations-section"><div className="section-heading"><div><span>Time-domain demand</span><h3>Utilities through the schedule</h3></div><p>{dynamic.horizon_h}-hour horizon · {dynamic.timestep_min}-minute timestep.</p></div><div className="series-grid"><MiniSeries rows={dynamic.timeline} valueKey="net_external_thermal_kW" label="Net external heat (kW)" color="#c46b2b"/><MiniSeries rows={dynamic.timeline} valueKey="electrical_kW" label="Electrical demand (kW)" color="#2e6f94"/><MiniSeries rows={dynamic.timeline} valueKey="used_heat_recovery_kW" label="Heat recovered (kW)" color="#3d8b5d"/><MiniSeries rows={dynamic.timeline} valueKey="cooling_kW" label="Cooling demand (kW)" color="#547fc1"/></div></section>
    <section className="operations-section"><div className="section-heading"><div><span>Operability log</span><h3>Recent transfer and batch events</h3></div><p>Useful for diagnosing blocking, starvation and transfer sequencing.</p></div><div className="event-table">{(dynamic.event_log||[]).slice(-40).reverse().map((e:any,i:number)=><div key={i}><span>{fmt(e.time_h,2)} h</span><strong>{pretty(e.event)}</strong><small>{e.vessel||[e.from,e.to].filter(Boolean).join(' → ')||''}</small></div>)}</div></section>
    <p className="model-boundary"><strong>Scheduler boundary:</strong> no intermediate buffer vessels are assumed. P02 → P04 → P05 transfers are direct and event-resolved. P06 onward remains continuous process-screening logic.</p>
  </div>
}

function ProcessNode({data,selected}:any){
  const inputs=data.inputs||[], outputs=data.outputs||[]
  const stage=stageMeta(data.type)
  return <div className={`process-node stage-${stage.tone} ${selected?'is-selected':''} ${data.hasError?'node-error':''}`}>
    {inputs.map((p:any,i:number)=><Handle key={p.name} type="target" position={Position.Left} id={p.name} style={{top:42+i*20}} />)}
    <div className="node-kicker"><span>{stage.step}</span>{stage.label}</div>
    <div className="node-title">{data.label}</div>
    <div className="node-bottom">
      {data.status?<span className={`status-dot ${statusClass(data.status)}`} title={data.status}></span>:<span/>}
      {data.balance!==undefined?<span className={`balance ${Math.abs(data.balance)<1e-8?'ok':'bad'}`}>{Math.abs(data.balance)<1e-8?'Balanced':'Check balance'}</span>:<span className="node-hint">Not run</span>}
    </div>
    {outputs.map((p:any,i:number)=><Handle key={p.name} type="source" position={Position.Right} id={p.name} style={{top:42+i*20}} />)}
  </div>
}
const nodeTypes={process:ProcessNode}

function category(type:string){
  if(/feed|dose/.test(type)) return 'Feed & dosing'
  if(/pretreatment|hydrolysis|fermentation/.test(type)) return 'Conversion'
  if(/separation|column|rectifier|sieve|conditioning/.test(type)) return 'Separation & recovery'
  if(/sink|recycle/.test(type)) return 'Terminals'
  if(/heat|heater|cooler|pump|tank|mixer|splitter|utility/.test(type)) return 'Utilities & general'
  return 'Other'
}

const stageMeta=(type:string)=>{
  const group=category(type)
  if(group==='Feed & dosing') return {step:'01',label:'Feed',tone:'feed'}
  if(group==='Conversion') return {step:'02',label:'Conversion',tone:'conversion'}
  if(group==='Separation & recovery') return {step:'03',label:'Recovery',tone:'recovery'}
  if(group==='Terminals') return {step:'04',label:'Outputs',tone:'output'}
  return {step:'S',label:'Support',tone:'support'}
}

function arrangeFlowsheet(source:Flowsheet){
  const byId=Object.fromEntries(source.blocks.map(b=>[b.id,b]))
  const incoming:Record<string,string[]>={}
  source.connections.forEach(c=>(incoming[c.to_block]||=[]).push(c.from_block))
  const product=source.blocks.find(b=>/ethanol/i.test(b.name))||source.blocks.find(b=>/product.*sink|product_sink/i.test(b.type))
  const memo:Record<string,string[]>={}
  const longestTo=(id:string,visiting=new Set<string>()):string[]=>{
    if(memo[id])return memo[id]
    if(visiting.has(id))return [id]
    const next=new Set(visiting);next.add(id)
    const candidates=(incoming[id]||[]).map(p=>longestTo(p,next))
    const best=candidates.sort((a,b)=>b.length-a.length)[0]||[]
    return memo[id]=[...best,id]
  }
  const mainPath=product?longestTo(product.id):source.blocks.filter(b=>stageMeta(b.type).tone!=='support').map(b=>b.id)
  const mainSet=new Set(mainPath)
  const positions:Record<string,{x:number,y:number}>={}
  mainPath.forEach((id,i)=>positions[id]={x:70+i*245,y:245})
  const upperSlots:Record<string,number>={},lowerSlots:Record<string,number>={}
  source.blocks.filter(b=>!mainSet.has(b.id)).forEach(b=>{
    const outToMain=source.connections.find(c=>c.from_block===b.id&&mainSet.has(c.to_block))
    const inFromMain=source.connections.find(c=>c.to_block===b.id&&mainSet.has(c.from_block))
    if(outToMain){
      const anchor=positions[outToMain.to_block];const slot=upperSlots[outToMain.to_block]||0;upperSlots[outToMain.to_block]=slot+1
      positions[b.id]={x:anchor.x-18+slot*38,y:55-slot*105};return
    }
    if(inFromMain){
      const anchor=positions[inFromMain.from_block];const slot=lowerSlots[inFromMain.from_block]||0;lowerSlots[inFromMain.from_block]=slot+1
      positions[b.id]={x:anchor.x+25+slot*45,y:445+slot*110};return
    }
    const linked=source.connections.find(c=>(c.from_block===b.id&&positions[c.to_block])||(c.to_block===b.id&&positions[c.from_block]))
    const anchorId=linked?(positions[linked.to_block]?linked.to_block:linked.from_block):mainPath[Math.floor(mainPath.length/2)]
    const anchor=positions[anchorId]||{x:600,y:245};const slot=Object.keys(positions).length
    positions[b.id]={x:anchor.x,y:590+(slot%4)*110}
  })
  const arranged=source.blocks.map(b=>({...b,position:positions[b.id]||b.position}))
  return {...source,blocks:arranged}
}

function App(){
  const [flow,setFlow]=useState<Flowsheet|null>(()=>arrangeFlowsheet(structuredClone(embeddedReference) as Flowsheet))
  const [library,setLibrary]=useState<any[]>(fallbackBlockLibrary)
  const [selected,setSelected]=useState<string|null>(null)
  const [selectedStream,setSelectedStream]=useState<string|null>(null)
  const [results,setResults]=useState<any>(null)
  const [heatProfile,setHeatProfile]=useState<any>(null)
  const [nodes,setNodes,onNodesChange]=useNodesState([])
  const [edges,setEdges,onEdgesChange]=useEdgesState([])
  const [busy,setBusy]=useState(false)
  const [notice,setNotice]=useState('Opening reference model…')
  const [savedFlows,setSavedFlows]=useState<any[]>([])
  const [libraryQuery,setLibraryQuery]=useState('')
  const [inspectorTab,setInspectorTab]=useState<'configure'|'results'>('configure')
  const [dashboardOpen,setDashboardOpen]=useState(false)
  const [activePage,setActivePage]=useState<'dashboard'|'flowsheet'|'twin'|'scheduler'>('dashboard')
  const [resultsTab,setResultsTab]=useState<'summary'|'streams'|'heat'|'electrical'>('summary')
  const [history,setHistory]=useState<Flowsheet[]>([])
  const [future,setFuture]=useState<Flowsheet[]>([])
  const [showLibrary,setShowLibrary]=useState(false)
  const [showInspector,setShowInspector]=useState(false)
  const [userMode,setUserMode]=useState<'simple'|'engineering'>('simple')
  const [issuesOpen,setIssuesOpen]=useState(false)
  const [toolsOpen,setToolsOpen]=useState(false)
  const [toolTab,setToolTab]=useState<'scenarios'|'sensitivity'|'engineering'>('scenarios')
  const [scenarios,setScenarios]=useState<Record<string,any>>({})
  const [appMeta,setAppMeta]=useState<any>(null)
  const [analysisData,setAnalysisData]=useState<any>(null)
  const [analysisBusy,setAnalysisBusy]=useState(false)
  const [sensitivityBlock,setSensitivityBlock]=useState('hydro')
  const [sensitivityParameter,setSensitivityParameter]=useState('glucan_to_glucose_conversion_fraction')
  const [sensitivityValues,setSensitivityValues]=useState('0.65, 0.72, 0.7674, 0.80, 0.85')
  const [isDirty,setIsDirty]=useState(false)
  const [currentFilePath,setCurrentFilePath]=useState<string|null>(null)
  const importRef=useRef<HTMLInputElement|null>(null)

  async function checkForUpdates(manual=false){
    if(!isDesktop){if(manual)setNotice('Updates are managed by the installed desktop application.');return}
    try{
      if(manual)setNotice('Checking GitHub for updates…')
      const update=await check({timeout:30000})
      if(!update){if(manual)setNotice('This application is up to date.');return}
      const accept=window.confirm(`BioAgri ${update.version} is available. Download and install it now?${update.body?`\n\n${update.body}`:''}`)
      if(!accept){setNotice(`Update ${update.version} is available`);return}
      setNotice(`Downloading BioAgri ${update.version}…`)
      await update.downloadAndInstall()
    }catch(e:any){if(manual)setNotice(`Update check failed: ${e.message}`)}
  }

  useEffect(()=>{if(!isDesktop)return;const timer=window.setTimeout(()=>checkForUpdates(false),4000);return()=>window.clearTimeout(timer)},[])

  const flowRef=useRef<Flowsheet|null>(null)
  const flowCanvas=useRef<any>(null)
  useEffect(()=>{flowRef.current=flow},[flow])
  useEffect(()=>{
    const warn=(e:BeforeUnloadEvent)=>{if(isDirty){e.preventDefault();e.returnValue=''}}
    window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn)
  },[isDirty])
  useEffect(()=>{
    if(!nodes.length||!flowCanvas.current)return
    const timer=setTimeout(()=>flowCanvas.current?.fitView({padding:.16,duration:350,maxZoom:1}),80)
    return ()=>clearTimeout(timer)
  },[nodes.length,flow?.name])

  useEffect(()=>{
    Promise.all([
      apiFetch(`${API}/api/reference-flowsheet`).then(r=>{if(!r.ok)throw new Error('Could not open the reference model');return r.json()}),
      apiFetch(`${API}/api/block-library`).then(r=>{if(!r.ok)throw new Error('Could not load the process block library');return r.json()}),
      apiFetch(`${API}/api/saved`).then(r=>r.ok?r.json():[]),
      apiFetch(`${API}/api/scenarios`).then(r=>r.ok?r.json():{}),
      apiFetch(`${API}/api/meta`).then(r=>r.ok?r.json():null),
      apiFetch(`${API}/api/dynamic-heat-profile`).then(r=>r.ok?r.json():null)
    ]).then(([f,l,s,sc,m,hp])=>{setFlow(arrangeFlowsheet(f));setLibrary(l);setSavedFlows(s);setScenarios(sc);setAppMeta(m);setHeatProfile(hp);setIsDirty(false);setNotice('Ready to run')})
      .catch((e:any)=>setNotice(`Process flow loaded · calculation engine unavailable: ${e.message}`))
  },[])

  const schemaByType=useMemo(()=>Object.fromEntries(library.map((x:any)=>[x.type,x])),[library])

  useEffect(()=>{
    if(!flow)return
    setNodes(flow.blocks.map((b:any)=>{
      const res=results?.block_results?.[b.id]
      const libraryInputs=Object.values(schemaByType[b.type]?.input_ports||{}) as any[]
      const libraryOutputs=Object.values(schemaByType[b.type]?.output_ports||{}) as any[]
      // The desktop app can render its embedded flowsheet before the backend block
      // library is ready. Derive its handles from the saved connections so edges
      // always have real attachment points during startup and offline use.
      const connectedInputs=[...new Set(
        flow.connections.filter((c:any)=>c.to_block===b.id).map((c:any)=>c.to_port)
      )].map(name=>({name}))
      const connectedOutputs=[...new Set(
        flow.connections.filter((c:any)=>c.from_block===b.id).map((c:any)=>c.from_port)
      )].map(name=>({name}))
      return {id:b.id,type:'process',position:b.position||{x:0,y:0},data:{
        label:b.name,type:b.type,
        status:b.params?.design_status||res?.metadata?.status||'',
        inputs:libraryInputs.length?libraryInputs:connectedInputs,
        outputs:libraryOutputs.length?libraryOutputs:connectedOutputs,
        hasError:(res?.errors?.length||0)>0,balance:res?.metrics?.closure_error_tph
      }}
    }))
    setEdges(flow.connections.map((c:any)=>({
      id:c.id,source:c.from_block,sourceHandle:c.from_port,target:c.to_block,targetHandle:c.to_port,
      type:'step',markerEnd:{type:MarkerType.ArrowClosed,width:14,height:14},className:'process-edge',
      style:{strokeWidth:2},
      label:(()=>{const stream=results?.streams?.[`${c.from_block}.${c.from_port}`];if(!stream)return undefined;const volume=stream.volumetric_flow_m3ph??(stream.total_tph==null?null:stream.total_tph);return volume==null?undefined:`${fmt(volume,2)} m³/h`})(),
      labelBgPadding:[5,3],labelBgBorderRadius:4
    })))
  },[flow,library,results])

  const selectedBlock=useMemo(()=>flow?.blocks.find(b=>b.id===selected)||null,[flow,selected])
  const selectedResult=selected?results?.block_results?.[selected]:null
  const selectedStreamData=selectedStream?results?.streams?.[selectedStream]:null

  function commit(next:Flowsheet,msg?:string){
    if(flow){setHistory(h=>[...h.slice(-29),structuredClone(flow)]);setFuture([])}
    setFlow(next);setResults(null)
    setIsDirty(true)
    if(msg)setNotice(msg)
  }
  function undo(){if(!history.length||!flow)return;const prev=history[history.length-1];setHistory(h=>h.slice(0,-1));setFuture(f=>[structuredClone(flow),...f]);setFlow(prev);setResults(null);setNotice('Undid last change')}
  function redo(){if(!future.length||!flow)return;const next=future[0];setFuture(f=>f.slice(1));setHistory(h=>[...h,structuredClone(flow)]);setFlow(next);setResults(null);setNotice('Redid change')}

  const onConnect=useCallback((params:any)=>{
    const current=flowRef.current;if(!current)return
    const id=`S${Date.now().toString().slice(-6)}`
    const nextConn={id,from_block:params.source,from_port:params.sourceHandle||'outlet',to_block:params.target,to_port:params.targetHandle||'feed'}
    commit({...current,connections:[...current.connections,nextConn]},'Connected process stream')
  },[flow])

  function onEdgesDelete(deleted:any[]){
    if(!flow)return
    const ids=new Set(deleted.map(e=>e.id));commit({...flow,connections:flow.connections.filter(c=>!ids.has(c.id))},'Removed process stream')
    setSelectedStream(null)
  }

  function updateBlock(patch:Partial<BlockDef>){
    if(!flow||!selectedBlock)return
    commit({...flow,blocks:flow.blocks.map(b=>b.id===selectedBlock.id?{...b,...patch}:b)})
  }
  function updateParam(k:string,v:any){if(!flow||!selectedBlock)return;commit({...flow,blocks:flow.blocks.map(b=>b.id===selectedBlock.id?{...b,params:{...b.params,[k]:v}}:b)})}

  function addBlock(type:string){
    if(!flow)return
    const schema=schemaByType[type],id=`${type}_${Date.now().toString().slice(-5)}`
    const viewportX=120+(flow.blocks.length%3)*30,viewportY=120+(flow.blocks.length%5)*70
    const b:BlockDef={id,type,name:schema?.display_name||pretty(type),params:{...(schema?.default_params||{})},position:{x:viewportX,y:viewportY}}
    commit({...flow,blocks:[...flow.blocks,b]},`Added ${b.name}`);setSelected(id);setSelectedStream(null);setShowInspector(true)
  }

  function deleteSelected(){
    if(!flow||!selectedBlock)return
    if(!window.confirm(`Delete “${selectedBlock.name}” and its connected streams?`))return
    commit({...flow,blocks:flow.blocks.filter(b=>b.id!==selectedBlock.id),connections:flow.connections.filter(c=>c.from_block!==selectedBlock.id&&c.to_block!==selectedBlock.id)},`Deleted ${selectedBlock.name}`)
    setSelected(null)
  }

  function autoArrange(){
    if(!flow)return
    commit(arrangeFlowsheet(flow),'Flow organised into process stages')
  }

  function onNodeDragStop(_:any,n:any){
    if(!flow)return
    setFlow({...flow,blocks:flow.blocks.map(b=>b.id===n.id?{...b,position:n.position}:b)});setIsDirty(true)
  }

  async function runModel(){
    if(!flow)return
    setBusy(true);setNotice('Running process model…')
    try{
      const live={...flow,blocks:flow.blocks.map(b=>{const n:any=nodes.find((x:any)=>x.id===b.id);return {...b,position:n?.position||b.position}})}
      const r=await apiFetch(`${API}/api/run`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({flowsheet:live})})
      const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(Array.isArray(data.detail)?data.detail.join(' '):data.detail||`Model run failed (${r.status})`)
      setFlow(live);setResults(data)
      const errors=data.errors?.length||0,warnings=data.warnings?.length||0
      setNotice(errors?`${errors} model error${errors===1?'':'s'} — select affected blocks for details`:warnings?`Run complete with ${warnings} warning${warnings===1?'':'s'}`:'Run complete — balances closed')
    }catch(e:any){setNotice(e.message)}finally{setBusy(false)}
  }

  async function refreshSaved(){try{const r=await apiFetch(`${API}/api/saved`);if(r.ok)setSavedFlows(await r.json())}catch{}}
  async function saveCurrent(forceSaveAs=false){
    if(!flow)return
    setBusy(true);setNotice(forceSaveAs?'Choose where to save…':'Saving…')
    try{
      const name=(flow.name||'Untitled flowsheet').trim()||'Untitled flowsheet'
      const content=JSON.stringify({...flow,name},null,2)
      if(isDesktop){
        const savedPath=await invoke<string|null>('save_flowsheet_file',{path:forceSaveAs?null:currentFilePath,suggestedName:name,content})
        if(!savedPath){setNotice('Save cancelled');return}
        setCurrentFilePath(savedPath);setFlow({...flow,name});setIsDirty(false)
        setNotice(`Saved “${name}” · ${savedPath}`)
      }else{
        downloadText(`${name.replace(/[^a-z0-9-_ ]/gi,'')||'flowsheet'}.bioagri.json`,content)
        setIsDirty(false);setNotice(`Downloaded “${name}”`)
      }
    }catch(e:any){setNotice(`Save failed: ${e.message||e}`)}finally{setBusy(false)}
  }
  function saveAs(){saveCurrent(true)}
  async function openFlowsheetFile(){
    if(isDirty&&!window.confirm('Open another flowsheet and discard unsaved changes?'))return
    if(!isDesktop){importRef.current?.click();return}
    setBusy(true);setNotice('Choose a BioAgri flowsheet…')
    try{
      const opened=await invoke<{path:string,content:string}|null>('open_flowsheet_file')
      if(!opened){setNotice('Open cancelled');return}
      const value=JSON.parse(opened.content)
      if(!value||!Array.isArray(value.blocks)||!Array.isArray(value.connections))throw new Error('This is not a valid BioAgri flowsheet file.')
      setFlow(value);setResults(null);setHistory([]);setFuture([]);setSelected(null);setSelectedStream(null)
      setCurrentFilePath(opened.path);setIsDirty(false);setNotice(`Opened “${value.name||opened.path}”`)
    }catch(e:any){setNotice(`Open failed: ${e.message||e}`)}finally{setBusy(false)}
  }
  function loadSaved(name:string){if(!name)return;if(isDirty&&!window.confirm('Open another flowsheet and discard unsaved changes?'))return;setBusy(true);setNotice(`Opening legacy saved flow “${name}”…`);apiFetch(`${API}/api/saved/${encodeURIComponent(name)}`).then(r=>{if(!r.ok)throw new Error(`Open failed (${r.status})`);return r.json()}).then(f=>{setFlow(f);setResults(null);setHistory([]);setFuture([]);setSelected(null);setSelectedStream(null);setCurrentFilePath(null);setIsDirty(false);setNotice(`Opened legacy saved flow “${name}”`)}).catch((e:any)=>setNotice(e.message)).finally(()=>setBusy(false))}
  function loadPreset(which:string){if(isDirty&&!window.confirm('Open this route and discard unsaved changes?'))return;setBusy(true);setNotice('Opening route…');apiFetch(`${API}/api/${which}`).then(r=>{if(!r.ok)throw new Error(`Could not open route (${r.status})`);return r.json()}).then(f=>{setFlow(arrangeFlowsheet(f));setResults(null);setHistory([]);setFuture([]);setSelected(null);setSelectedStream(null);setIsDirty(false);setNotice('Route ready')}).catch((e:any)=>setNotice(e.message)).finally(()=>setBusy(false))}
  function newFlowsheet(){if(isDirty&&!window.confirm('Start a new blank flowsheet? Unsaved changes will be lost.'))return;setFlow({name:'Untitled flowsheet',blocks:[],connections:[]});setResults(null);setSelected(null);setSelectedStream(null);setHistory([]);setFuture([]);setCurrentFilePath(null);setIsDirty(true);setNotice('Blank flowsheet created')}

  function exportFlowsheet(){if(flow)downloadText(`${(flow.name||'flowsheet').replace(/[^a-z0-9-_ ]/gi,'')}.json`,JSON.stringify(flow,null,2))}
  function importFlowsheet(file?:File){if(!file)return;const reader=new FileReader();reader.onload=()=>{try{const value=JSON.parse(String(reader.result));if(!Array.isArray(value.blocks)||!Array.isArray(value.connections))throw new Error('This is not a valid BioAgri flowsheet file.');setFlow(value);setResults(null);setHistory([]);setFuture([]);setCurrentFilePath(null);setIsDirty(true);setNotice(`Imported “${value.name||file.name}”`)}catch(e:any){setNotice(e.message)}};reader.readAsText(file)}
  async function downloadReport(){if(!flow)return;const reportWindow=window.open('','_blank');setBusy(true);try{const r=await apiFetch(`${API}/api/report`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({flowsheet:flow})});const text=await r.text();if(!r.ok)throw new Error('Report could not be generated.');if(reportWindow){reportWindow.document.open();reportWindow.document.write(text);reportWindow.document.close()}else downloadText(`${(flow.name||'run-report').replace(/[^a-z0-9-_ ]/gi,'')}-report.html`,text,'text/html');setNotice('PDF-ready engineering report opened — choose Print / save as PDF.')}catch(e:any){reportWindow?.close();setNotice(e.message)}finally{setBusy(false)}}
  async function runScenario(scenario:any){setAnalysisBusy(true);setAnalysisData(null);try{const r=await apiFetch(`${API}/api/scenario/run`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(scenario)});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Scenario failed');setAnalysisData({kind:'scenario',name:scenario.name,data})}catch(e:any){setAnalysisData({kind:'error',message:e.message})}finally{setAnalysisBusy(false)}}
  async function compareWithReference(){if(!flow)return;setAnalysisBusy(true);try{const reference=await apiFetch(`${API}/api/reference-flowsheet`).then(r=>r.json());const r=await apiFetch(`${API}/api/compare`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({flowsheets:[reference,flow]})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Comparison failed');setAnalysisData({kind:'comparison',data:data.scenarios})}catch(e:any){setAnalysisData({kind:'error',message:e.message})}finally{setAnalysisBusy(false)}}
  async function runSensitivity(){const values=sensitivityValues.split(',').map(x=>Number(x.trim())).filter(Number.isFinite);setAnalysisBusy(true);try{const r=await apiFetch(`${API}/api/sensitivity`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({block_id:sensitivityBlock,parameter:sensitivityParameter,values,name:'User sensitivity'})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Sensitivity failed');setAnalysisData({kind:'sensitivity',data})}catch(e:any){setAnalysisData({kind:'error',message:e.message})}finally{setAnalysisBusy(false)}}
  async function loadEngineeringData(){setAnalysisBusy(true);try{const [physics,vle,summary,heat]=await Promise.all(['physics-summary','vle-screening','v015-summary','dynamic-heat-profile'].map(p=>apiFetch(`${API}/api/${p}`).then(r=>r.json())));setAnalysisData({kind:'engineering',physics,vle,summary,heat})}catch(e:any){setAnalysisData({kind:'error',message:e.message})}finally{setAnalysisBusy(false)}}

  const groupedLibrary=useMemo(()=>{
    const q=libraryQuery.trim().toLowerCase();const rows=library.filter((b:any)=>!q||`${b.display_name} ${b.type}`.toLowerCase().includes(q));
    return rows.reduce((acc:Record<string,any[]>,b:any)=>{(acc[category(b.type)]||=[]).push(b);return acc},{})
  },[library,libraryQuery])

  const basicKeys=useMemo(()=>{
    if(!selectedBlock)return []
    const electricalDefaults=['manual_electrical_load_kW','electrical_load_factor_fraction','annual_operating_hours'];const keys=[...Object.keys(selectedBlock.params||{}),...electricalDefaults.filter(k=>!(k in (selectedBlock.params||{})))];const priority=/temp|temperature|time|hour|conversion|yield|pressure|dm|solids|loading|recovery|fraction|flow|volume|purity|reflux/i
    return [...keys.filter(k=>priority.test(k)),...keys.filter(k=>!priority.test(k))].slice(0,7)
  },[selectedBlock])
  const advancedKeys=useMemo(()=>selectedBlock?Object.keys(selectedBlock.params||{}).filter(k=>!basicKeys.includes(k)):[],[selectedBlock,basicKeys])

  const connectionIssue=useMemo(()=>{
    if(!selectedBlock||!flow)return ''
    const schema=schemaByType[selectedBlock.type];const reqIn=Object.keys(schema?.input_ports||{}).length
    const incoming=flow.connections.filter(c=>c.to_block===selectedBlock.id).length
    if(reqIn>0&&incoming===0)return `${selectedBlock.name} has no inlet stream. Connect a compatible upstream unit.`
    return ''
  },[selectedBlock,flow,schemaByType])

  const errorCount=results?.errors?.length||0
  const warningCount=results?.warnings?.length||0
  const decisionCount=results?.open_decisions?.length||0
  const runHealth=errorCount?'Action required':warningCount?'Review recommended':'Ready for review'

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-area">
        <div className="app-mark">B</div>
        <div className="title-stack">
          <input className="flow-name" aria-label="Flowsheet name" value={flow?.name||''} onChange={e=>{if(flow){setFlow({...flow,name:e.target.value});setIsDirty(true)}}}/>
          <div className="app-subtitle">Bio-Agri Process Simulator <span>V{appMeta?.version||'0.23.0'} · Model {appMeta?.model_version||'0.23.0'}{currentFilePath?` · ${currentFilePath.split(/[\\/]/).pop()}`:''}{isDirty?' · Unsaved changes':''}</span></div>
        </div>
      </div>
      <div className="toolbar" aria-label="Main controls">
        <button className="icon-button" title="New flowsheet" onClick={newFlowsheet}>＋</button>
        <button className="icon-button" title="Undo" disabled={!history.length} onClick={undo}>↶</button>
        <button className="icon-button" title="Redo" disabled={!future.length} onClick={redo}>↷</button>
        <div className="toolbar-divider"/>
        <div className="page-nav" aria-label="Primary workspace">
          <button className={activePage==='dashboard'?'active':''} onClick={()=>setActivePage('dashboard')}>Dashboard</button>
          <button className={activePage==='flowsheet'?'active':''} onClick={()=>setActivePage('flowsheet')}>Flowsheet</button>
          <button className={activePage==='twin'?'active':''} onClick={()=>setActivePage('twin')}>Digital Twin</button>
          <button className={activePage==='scheduler'?'active':''} onClick={()=>setActivePage('scheduler')}>Scheduler</button>
        </div>
        <div className="toolbar-divider"/>
        <button onClick={autoArrange} disabled={!flow?.blocks.length||activePage!=='flowsheet'}>Organise flow</button>
        <button onClick={()=>saveCurrent(false)} disabled={busy||!flow}>Save</button>
        <button onClick={openFlowsheetFile} disabled={busy}>Open…</button>
        <details className="route-menu"><summary>Routes</summary><div className="menu-popover"><button onClick={()=>loadPreset('reference-flowsheet')}>Reference route</button><button onClick={()=>loadPreset('alternative-flowsheet')}>Alternative route</button></div></details>
        <button onClick={()=>setIssuesOpen(true)}>Issues {results?`(${errorCount+warningCount+decisionCount})`:''}</button>
        <button onClick={()=>setToolsOpen(true)}>Analyse</button>
        <div className="mode-switch" aria-label="Interface mode"><button className={userMode==='simple'?'active':''} onClick={()=>setUserMode('simple')}>Simple</button><button className={userMode==='engineering'?'active':''} onClick={()=>setUserMode('engineering')}>Engineering</button></div>
        <details className="route-menu actions-menu"><summary>More</summary><div className="menu-popover"><button onClick={()=>checkForUpdates(true)}>Check for updates</button><button onClick={saveAs}>Save as…</button><button onClick={exportFlowsheet}>Export flowsheet</button><button onClick={()=>importRef.current?.click()}>Import flowsheet</button>{savedFlows.length>0&&<details><summary>Legacy saved flows</summary>{savedFlows.map((s:any)=><button key={s.name} onClick={()=>loadSaved(s.name)}>{s.name}</button>)}</details>}<button onClick={downloadReport} disabled={!results}>Print / save PDF report</button></div></details>
        <input ref={importRef} className="file-input" type="file" accept="application/json,.json" onChange={e=>{importFlowsheet(e.target.files?.[0]);e.currentTarget.value=''}}/>
        <button className="primary run-button" disabled={busy||!flow} onClick={runModel}>{busy?<><span className="spinner"/>Running</>:'Run'}</button>
      </div>
    </header>

    <div className={`workspace ${showLibrary?'with-library':''} ${showInspector?'with-inspector':''}`} style={{display:activePage==='flowsheet'?undefined:'none'}}>
      {showLibrary&&<aside className="library-panel">
        <div className="panel-header"><div><strong>Add process step</strong><span>Drag-free library</span></div><button className="ghost-icon" onClick={()=>setShowLibrary(false)} title="Hide library">‹</button></div>
        <div className="search-wrap"><span>⌕</span><input value={libraryQuery} onChange={e=>setLibraryQuery(e.target.value)} placeholder="Search equipment…"/></div>
        <div className="library-scroll">
          {Object.entries(groupedLibrary).map(([group,items]:any)=><section className="library-group" key={group}><h3>{group}</h3>{items.map((b:any)=><button className="library-item" key={b.type} onClick={()=>addBlock(b.type)}><span className="library-symbol">{b.display_name?.slice(0,1)||'•'}</span><span><strong>{b.display_name}</strong><small>{Object.keys(b.input_ports||{}).length} in · {Object.keys(b.output_ports||{}).length} out</small></span><span className="add-glyph">＋</span></button>)}</section>)}
          {!Object.keys(groupedLibrary).length&&<div className="empty-small">No matching process blocks.</div>}
        </div>
      </aside>}

      <main className="canvas-wrap">
        <div className="mobile-process-list">
          <div className="mobile-actions"><button onClick={()=>setShowLibrary(true)}>＋ Add step</button><button onClick={()=>setShowInspector(true)}>Inspector</button></div>
          <div className="mobile-route-title"><span>Process overview</span><strong>{flow?.blocks.length||0} units · {flow?.connections.length||0} streams</strong></div>
          {flow?.blocks.slice().sort((a,b)=>(a.position?.x||0)-(b.position?.x||0)||(a.position?.y||0)-(b.position?.y||0)).map((b,i)=>{const stage=stageMeta(b.type);return <button className={`mobile-unit stage-${stage.tone}`} key={b.id} onClick={()=>{setSelected(b.id);setSelectedStream(null);setShowInspector(true)}}><span>{i+1}</span><div><small>{stage.label}</small><strong>{b.name}</strong></div><i>›</i></button>})}
        </div>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
          onInit={instance=>{flowCanvas.current=instance;setTimeout(()=>instance.fitView({padding:.16,maxZoom:1}),80)}}
          onNodeDragStop={onNodeDragStop} onEdgesDelete={onEdgesDelete} onConnect={onConnect}
          defaultEdgeOptions={{type:'step',markerEnd:{type:MarkerType.ArrowClosed,width:14,height:14}}}
          snapToGrid snapGrid={[20,20]} connectionLineType={ConnectionLineType.Step}
          onNodeClick={(_,n:any)=>{setSelected(n.id);setSelectedStream(null);setInspectorTab('configure');setShowInspector(true)}}
          onEdgeClick={(_,e:any)=>{setSelected(null);setSelectedStream(`${e.source}.${e.sourceHandle}`);setShowInspector(true)}}
          onPaneClick={()=>{setSelected(null);setSelectedStream(null)}} fitView minZoom={0.25} deleteKeyCode={['Backspace','Delete']}>
          <Background gap={20} size={1}/><Controls showInteractive={false}/><MiniMap pannable zoomable nodeStrokeWidth={2}/>
          <Panel position="top-left" className="canvas-actions">
            {!showLibrary&&<button className="floating-control" onClick={()=>setShowLibrary(true)}>＋ Add step</button>}
            {!showInspector&&<button className="floating-control" onClick={()=>setShowInspector(true)}>Inspector</button>}
          </Panel>
          <Panel position="top-center" className="stage-guide" aria-label="Process stages">
            <div className="stage-item feed"><span>01</span><strong>Feed</strong></div><i>→</i>
            <div className="stage-item conversion"><span>02</span><strong>Conversion</strong></div><i>→</i>
            <div className="stage-item recovery"><span>03</span><strong>Recovery</strong></div><i>→</i>
            <div className="stage-item output"><span>04</span><strong>Outputs</strong></div>
          </Panel>
          {flow&&flow.blocks.length===0&&<Panel position="top-center" className="empty-canvas"><div className="empty-icon">＋</div><strong>Build your process</strong><p>Add the first process step from the library, then connect blocks from right to left ports.</p><button className="primary" onClick={()=>setShowLibrary(true)}>Open block library</button></Panel>}
        </ReactFlow>
      </main>

      {showInspector&&<aside className="inspector-panel">
        <div className="panel-header"><div><strong>{selectedStream?'Stream inspector':selectedBlock?'Configure block':'Inspector'}</strong><span>{selectedStream||selectedBlock?.name||'Select a block or stream'}</span></div><button className="ghost-icon" onClick={()=>setShowInspector(false)} title="Hide inspector">›</button></div>

        {!selectedBlock&&!selectedStream&&<div className="inspector-empty"><div className="selection-demo"><span></span><span></span><span></span></div><strong>Select something on the flowsheet</strong><p>Choose a process block to edit parameters, or choose a process line to inspect the calculated stream.</p></div>}

        {selectedStream&&<div className="inspector-content">
          <div className="section-title">Process stream</div>
          {selectedStreamData?<>
            <div className="hero-metric"><span>Total flow</span><strong>{fmt(selectedStreamData.total_tph,4)} <small>t/h</small></strong></div>
            <div className="compact-grid"><div><span>Temperature</span><strong>{selectedStreamData.temperature_C==null?'—':`${fmt(selectedStreamData.temperature_C,1)} °C`}</strong></div><div><span>Pressure</span><strong>{selectedStreamData.pressure_bar_abs==null?'—':`${fmt(selectedStreamData.pressure_bar_abs,2)} bar`}</strong></div></div>
            <div className="section-title">Composition</div><div className="metric-list">{Object.entries(selectedStreamData.components_tph||{}).filter(([,v]:any)=>Math.abs(v)>1e-10).map(([k,v]:any)=><div key={k}><span>{pretty(k)}</span><strong>{fmt(v,4)} t/h</strong></div>)}</div>
            <div className="status-row"><span className={`status-pill ${statusClass(selectedStreamData.status)}`}>{selectedStreamData.status}</span></div>
          </>:<div className="inline-message info"><strong>Run the model to calculate this stream.</strong><span>The connection exists, but no current stream result is available yet.</span></div>}
        </div>}

        {selectedBlock&&<>
          <div className="inspector-tabs"><button className={inspectorTab==='configure'?'active':''} onClick={()=>setInspectorTab('configure')}>Configure</button><button className={inspectorTab==='results'?'active':''} onClick={()=>setInspectorTab('results')}>Results</button></div>
          <div className="inspector-content">
            {inspectorTab==='configure'?<>
              {connectionIssue&&<div className="inline-message warning"><strong>Connection required</strong><span>{connectionIssue}</span></div>}
              <label className="field"><span>Block name</span><input value={selectedBlock.name} onChange={e=>updateBlock({name:e.target.value})}/></label>
              <div className="section-title">Operating parameters</div>
              {!basicKeys.length&&<div className="empty-small">This block has no editable operating parameters.</div>}
              {basicKeys.map(k=>{const v=selectedBlock.params[k],m=parameterMeta(k);return <label className="field" key={k} title={m.help}><span>{pretty(k)} {m.unit&&<em>{m.unit}</em>}<button type="button" className="help-dot" aria-label={`Help for ${pretty(k)}`}>?</button></span><input type={typeof v==='number'?'number':'text'} min={m.min} max={m.max} step={m.step} value={String(v)} onChange={e=>updateParam(k,typeof v==='number'?Number(e.target.value):e.target.value)}/><small>{m.help}</small></label>})}
              {userMode==='engineering'&&!!advancedKeys.length&&<details className="advanced"><summary>Advanced engineering parameters <span>{advancedKeys.length}</span></summary><div className="advanced-body">{advancedKeys.map(k=>{const v=selectedBlock.params[k],m=parameterMeta(k);return (typeof v==='number'||typeof v==='string')?<label className="field" key={k} title={m.help}><span>{pretty(k)} {m.unit&&<em>{m.unit}</em>}</span><input type={typeof v==='number'?'number':'text'} min={m.min} max={m.max} step={m.step} value={String(v)} onChange={e=>updateParam(k,typeof v==='number'?Number(e.target.value):e.target.value)}/><small>{m.help}</small></label>:null})}</div></details>}
              <div className="inspector-actions"><button className="danger-text" onClick={deleteSelected}>Delete block</button></div>
            </>:<>
              {!results?<div className="inspector-empty compact"><strong>No current results</strong><p>Run the process model to calculate this block.</p><button className="primary" onClick={runModel}>Run model</button></div>:<>
                <div className="section-title">Latest calculation</div>
                <div className="metric-list">{Object.entries(selectedResult?.metrics||{}).filter(([k,v]:any)=>k!=='shortcut_distillation'&&(typeof v==='number'||typeof v==='string'||typeof v==='boolean')).slice(0,12).map(([k,v]:any)=><div key={k}><span>{pretty(k)}</span><strong>{typeof v==='number'?fmt(v,4):String(v)}</strong></div>)}</div>
                {selectedResult?.metrics?.shortcut_distillation&&<><div className="section-title">Distillation design check</div><div className="metric-list">
                  <div><span>Relative volatility</span><strong>{fmt(selectedResult.metrics.shortcut_distillation.relative_volatility,3)}</strong></div>
                  <div><span>Minimum stages · Fenske</span><strong>{fmt(selectedResult.metrics.shortcut_distillation.minimum_stages_fenske,2)}</strong></div>
                  <div><span>Minimum reflux · Underwood</span><strong>{fmt(selectedResult.metrics.shortcut_distillation.minimum_reflux_underwood,2)}</strong></div>
                  <div><span>Required stages · Gilliland</span><strong>{fmt(selectedResult.metrics.shortcut_distillation.required_theoretical_stages_gilliland,2)}</strong></div>
                  <div><span>Installed effective stages</span><strong>{fmt(selectedResult.metrics.shortcut_distillation.installed_effective_stages,2)}</strong></div>
                  <div><span>Stage margin</span><strong>{fmt(selectedResult.metrics.shortcut_distillation.stage_margin,2)}</strong></div>
                  <div><span>Reflux / minimum reflux</span><strong>{fmt(selectedResult.metrics.shortcut_distillation.reflux_to_minimum_ratio,2)} ×</strong></div>
                  <div><span>Shortcut stage check</span><strong>{selectedResult.metrics.shortcut_distillation.stage_feasible?'PASS':'CHECK'}</strong></div>
                </div></>}
                {!!selectedResult?.utilities&&<><div className="section-title">Utilities</div><div className="metric-list">{Object.entries(selectedResult.utilities).filter(([,v]:any)=>typeof v==='number'&&Math.abs(v)>1e-12).map(([k,v]:any)=><div key={k}><span>{pretty(k)}</span><strong>{fmt(v,3)}</strong></div>)}</div></>}
                {selectedResult?.metadata&&<details className="advanced"><summary>Engineering basis</summary><div className="advanced-body metric-list"><div><span>Status</span><strong>{selectedResult.metadata.status}</strong></div><div><span>Confidence</span><strong>{selectedResult.metadata.confidence}</strong></div><div className="stacked"><span>Basis</span><strong>{selectedResult.metadata.basis}</strong></div></div></details>}
                {!!selectedResult?.errors?.length&&<div className="inline-message error"><strong>Model issue</strong>{selectedResult.errors.map((x:string)=><span key={x}>{x}</span>)}</div>}
                {!!selectedResult?.warnings?.length&&<div className="inline-message warning"><strong>Check this block</strong>{selectedResult.warnings.map((x:string)=><span key={x}>{x}</span>)}</div>}
              </>}
            </>}
          </div>
        </>}
      </aside>}
    </div>

    {activePage==='dashboard'&&<main className="primary-page-shell"><div className="primary-page-head"><div><span className="eyebrow">Dashboard</span><h1>Plant performance & design health</h1><p>Production, utilities, capacity and engineering confidence from the latest calculation.</p></div><button className="primary" onClick={runModel} disabled={busy||!flow}>{busy?'Running…':'Run current model'}</button></div><div className="primary-page-scroll">{results?<PlantDashboard results={results}/>:<div className="primary-page-empty"><strong>No plant run yet</strong><p>Run the current flowsheet to populate the plant dashboard.</p></div>}</div></main>}
    {activePage==='twin'&&<main className="primary-page-shell"><div className="primary-page-head"><div><span className="eyebrow">Digital Twin</span><h1>Plant replay & live batch state</h1><p>Full-width time-domain view of the connected process, vessel inventories and transfers.</p></div><button className="primary" onClick={runModel} disabled={busy||!flow}>{busy?'Running…':'Run current model'}</button></div><div className="primary-page-scroll">{results?.dynamic_plant?<DigitalTwin dynamic={results.dynamic_plant} results={results}/>:<div className="primary-page-empty"><strong>No Digital Twin run yet</strong><p>Run the flowsheet to generate the connected plant replay.</p></div>}</div></main>}
    {activePage==='scheduler'&&<main className="primary-page-shell"><div className="primary-page-head"><div><span className="eyebrow">Scheduler</span><h1>Production schedule & operability</h1><p>Vessel capacity, batch phases, direct transfers, pumps and plant constraints.</p></div><button className="primary" onClick={runModel} disabled={busy||!flow}>{busy?'Running…':'Recalculate schedule'}</button></div><div className="primary-page-scroll"><SchedulerPage dynamic={results?.dynamic_plant} flow={flow}/></div></main>}

    <footer className="statusbar">
      <div className="status-left"><span className={`model-indicator ${results?.errors?.length?'bad':results?'good':'idle'}`}></span><strong>{notice}</strong></div>
      <div className="status-right">
        {results&&<><span>{results.errors?.length||0} errors</span><span>{results.warnings?.length||0} warnings</span><span>{fmt(results.utility_totals?.electricity_kW||0,0)} kW elec</span><span>{fmt(results.utility_totals?.thermal_kW||0,0)} kW heat</span></>}
        <button className="status-button" onClick={()=>setDashboardOpen(true)} disabled={!results}>Plant results</button>
      </div>
    </footer>

    {dashboardOpen&&<div className="sheet-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)setDashboardOpen(false)}}><section className="results-sheet">
      <div className="sheet-handle"></div><div className="sheet-head"><div><span className="eyebrow">Latest plant run</span><h2>{flow?.name}</h2><p>A plain-language summary first, with engineering checks below.</p></div><button className="close-button" onClick={()=>setDashboardOpen(false)} aria-label="Close results">×</button></div>
      <div className="result-tabs"><button className={resultsTab==='summary'?'active':''} onClick={()=>setResultsTab('summary')}>Summary</button><button className={resultsTab==='streams'?'active':''} onClick={()=>setResultsTab('streams')}>Stream table</button><button className={resultsTab==='heat'?'active':''} onClick={()=>setResultsTab('heat')}>Heat data</button><button className={resultsTab==='electrical'?'active':''} onClick={()=>setResultsTab('electrical')}>Electrical data</button></div>
      {resultsTab==='summary'&&<>
      <div className={`run-summary ${errorCount?'has-errors':warningCount?'has-warnings':'is-clear'}`}>
        <div className="summary-icon">{errorCount?'!':warningCount?'△':'✓'}</div>
        <div><span>Overall result</span><strong>{runHealth}</strong><p>{errorCount?`${errorCount} issue${errorCount===1?'':'s'} must be resolved before using these results.`:warningCount?`The model completed. Review ${warningCount} engineering warning${warningCount===1?'':'s'} and ${decisionCount} open design decision${decisionCount===1?'':'s'}.`:'The model completed with no reported errors or warnings.'}</p></div>
        <div className="summary-counts"><span><strong>{errorCount}</strong> errors</span><span><strong>{warningCount}</strong> warnings</span><span><strong>{decisionCount}</strong> decisions</span></div>
      </div>
      <div className="result-kpis">
        <div className="kpi-product"><span>Product output</span><strong>{fmt(results?.terminal_component_totals?.ethanol||0,4)} <small>t/h ethanol</small></strong><p>Main production result</p></div>
        <div><span>Electricity</span><strong>{fmt(results?.utility_totals?.electricity_kW||0,1)} <small>kW</small></strong><p>Total connected demand</p></div>
        <div><span>Process heat</span><strong>{fmt(results?.utility_totals?.thermal_kW||0,1)} <small>kW</small></strong><p>Total thermal duty</p></div>
        <div><span>Steam</span><strong>{fmt(results?.utility_totals?.steam_kgph||0,0)} <small>kg/h</small></strong><p>Estimated consumption</p></div>
      </div>
      <div className="sheet-grid">
        <section className="dashboard-card"><div className="section-title">Engineering checks</div><div className="metric-list"><div><span>Material balance difference</span><strong>{fmt(results?.overall_material_closure?.closure_error_tph||0,6)} t/h</strong></div><div><span>Water balance difference</span><strong>{fmt(results?.water_balance?.reaction_adjusted_closure_error_tph||0,6)} t/h</strong></div><div><span>Open / provisional decisions</span><strong>{decisionCount}</strong></div></div></section>
        <section className="dashboard-card"><div className="section-title">Where material leaves the plant</div><div className="metric-list">{Object.entries(results?.terminal_summary||{}).map(([k,v]:any)=><div key={k}><span>{pretty(k)}</span><strong>{fmt(v,4)} t/h</strong></div>)}</div></section>
      </div>
      {!!results?.open_decisions?.length&&<section className="decisions"><div className="section-title">Design decisions still open</div>{results.open_decisions.slice(0,6).map((d:any)=><div className="decision-row" key={d.block_id}><span className={`status-dot ${statusClass(d.status)}`}></span><div><strong>{d.block_name}</strong><small>{d.note||d.basis}</small></div><span>{d.status}</span></div>)}</section>}
      </>}
      {resultsTab==='streams'&&<div className="result-table-wrap"><div className="table-note"><strong>{results?.stream_register?.length||0} calculated streams</strong><span>Component values are t/h. The four largest non-zero components are shown.</span></div><table className="engineering-table"><thead><tr><th>Stream</th><th>Total t/h</th><th>Phase</th><th>Temp °C</th><th>Pressure bar(a)</th><th>Major components t/h</th><th>Status</th></tr></thead><tbody>{(results?.stream_register||[]).map((s:any)=><tr key={s.stream_id}><td><strong>{s.stream_id}</strong><small>{s.note}</small></td><td className="num">{fmt(s.total_tph,4)}</td><td>{s.phase||'—'}</td><td className="num">{fmt(s.temperature_C,1)}</td><td className="num">{fmt(s.pressure_bar_abs,2)}</td><td>{majorComponents(s.components_tph)}</td><td><span className={`table-status ${statusClass(s.status)}`}>{s.status||'—'}</span></td></tr>)}</tbody></table></div>}
      {resultsTab==='heat'&&<div className="heat-results">
        <section className="heat-recovery-panel"><div className="table-note"><strong>Dynamic heat-recovery sequence</strong><span>{results?.dynamic_plant?.horizon_h||168}-hour native schedule · {results?.dynamic_plant?.timestep_min||15}-minute calculation steps</span></div>
          <div className="heat-flow">
            <div><span>1</span><small>Gross pretreatment heat</small><strong>{fmt(results?.dynamic_plant?.utility_summary?.gross_thermal_kW?.average??heatProfile?.summary?.gross_pretreatment_heat?.average,0)} kW avg</strong><em>{fmt(results?.dynamic_plant?.utility_summary?.gross_thermal_kW?.peak??heatProfile?.summary?.gross_pretreatment_heat?.peak,0)} kW peak</em></div><i>−</i>
            <div className="recovery"><span>2</span><small>Recovered from hot discharge</small><strong>{fmt(results?.dynamic_plant?.utility_summary?.used_heat_recovery_kW?.average??heatProfile?.summary?.used_heat_recovery?.average,0)} kW avg</strong><em>{fmt(results?.dynamic_plant?.utility_summary?.used_heat_recovery_kW?.peak??heatProfile?.summary?.used_heat_recovery?.peak,0)} kW peak</em></div><i>=</i>
            <div><span>3</span><small>External pretreatment heat</small><strong>{fmt(results?.dynamic_plant?.utility_summary?.net_external_thermal_kW?.average??heatProfile?.summary?.net_pretreatment_external?.average,0)} kW avg</strong><em>{fmt(results?.dynamic_plant?.utility_summary?.net_external_thermal_kW?.peak??heatProfile?.summary?.net_pretreatment_external?.peak,0)} kW peak</em></div><i>+</i>
            <div><span>4</span><small>Continuous distillation</small><strong>{fmt(results?.dynamic_plant?.utility_summary?.net_external_thermal_kW?.minimum??heatProfile?.summary?.total_external_thermal?.minimum,0)} kW</strong><em>continuous base load</em></div><i>=</i>
            <div className="total"><span>5</span><small>Total external heat</small><strong>{fmt(results?.dynamic_plant?.utility_summary?.net_external_thermal_kW?.average??heatProfile?.summary?.total_external_thermal?.average,0)} kW avg</strong><em>{fmt(results?.dynamic_plant?.utility_summary?.net_external_thermal_kW?.peak??heatProfile?.summary?.total_external_thermal?.peak,0)} kW peak</em></div>
          </div><p className="model-boundary"><strong>How this relates to the run:</strong> the table below is the steady reference duty used by the flowsheet. The sequence above is the time-resolved batch-scheduling model. It is now generated from the current flowsheet on every run and is the native basis for coincident batch utility screening.</p>
        </section>
        <div className="result-table-wrap"><div className="table-note"><strong>Heat and cooling duties by process unit</strong><span>Continuous screening duties; peak values are shown separately where available.</span></div><table className="engineering-table utility-table"><thead><tr><th>Process unit</th><th>Heat kW</th><th>Peak heat kW</th><th>Cooling kW</th><th>Peak cooling kW</th><th>Steam kg/h</th></tr></thead><tbody>{Object.entries(results?.utilities_by_block||{}).filter(([,u]:any)=>u.thermal_kW||u.cooling_kW||u.steam_kgph).map(([id,u]:any)=><tr key={id}><td><strong>{flow?.blocks.find(b=>b.id===id)?.name||pretty(id)}</strong></td><td className="num">{fmt(u.thermal_kW,2)}</td><td className="num">{fmt(u.peak_thermal_kW,2)}</td><td className="num">{fmt(u.cooling_kW,2)}</td><td className="num">{fmt(u.peak_cooling_kW,2)}</td><td className="num">{fmt(u.steam_kgph,1)}</td></tr>)}<tr className="total"><td>Plant total</td><td className="num">{fmt(results?.utility_totals?.thermal_kW,2)}</td><td className="num">{fmt(results?.utility_totals?.peak_thermal_kW,2)}</td><td className="num">{fmt(results?.utility_totals?.cooling_kW,2)}</td><td className="num">{fmt(results?.utility_totals?.peak_cooling_kW,2)}</td><td className="num">{fmt(results?.utility_totals?.steam_kgph,1)}</td></tr></tbody></table></div>
      </div>}
      {resultsTab==='electrical'&&<div className="result-table-wrap"><div className="table-note"><strong>Electrical demand by process unit</strong><span>Only units with a non-zero connected or peak demand are listed.</span></div><table className="engineering-table utility-table electrical"><thead><tr><th>Process unit</th><th>Connected demand kW</th><th>Peak demand kW</th><th>Share of plant total</th></tr></thead><tbody>{Object.entries(results?.utilities_by_block||{}).filter(([,u]:any)=>u.electricity_kW||u.peak_electricity_kW).map(([id,u]:any)=><tr key={id}><td><strong>{flow?.blocks.find(b=>b.id===id)?.name||pretty(id)}</strong></td><td className="num">{fmt(u.electricity_kW,2)}</td><td className="num">{fmt(u.peak_electricity_kW,2)}</td><td className="num">{fmt((u.electricity_kW/(results?.utility_totals?.electricity_kW||1))*100,1)}%</td></tr>)}<tr className="total"><td>Plant total</td><td className="num">{fmt(results?.utility_totals?.electricity_kW,2)}</td><td className="num">{fmt(results?.utility_totals?.peak_electricity_kW,2)}</td><td className="num">100.0%</td></tr></tbody></table></div>}
      <div className="sheet-actions"><button className="primary" onClick={downloadReport}>Print / save tables as PDF</button><button onClick={exportFlowsheet}>Export model inputs</button><button onClick={()=>{setDashboardOpen(false);setIssuesOpen(true)}}>Review all issues</button></div>
    </section></div>}

    {issuesOpen&&<div className="sheet-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)setIssuesOpen(false)}}><section className="results-sheet issues-sheet">
      <div className="sheet-handle"/><div className="sheet-head"><div><span className="eyebrow">Engineering assurance</span><h2>Issues and assumptions</h2><p>Items that affect interpretation, confidence, or readiness for design.</p></div><button className="close-button" onClick={()=>setIssuesOpen(false)} aria-label="Close issues">×</button></div>
      {!results?<div className="tool-empty"><strong>Run the model to populate the assurance register.</strong><p>The latest calculation will group errors, warnings, and provisional engineering decisions here.</p><button className="primary" onClick={()=>{setIssuesOpen(false);runModel()}}>Run model</button></div>:<>
        <div className="issue-summary"><div className="error"><strong>{errorCount}</strong><span>Calculation errors</span></div><div className="warning"><strong>{warningCount}</strong><span>Engineering warnings</span></div><div className="decision"><strong>{decisionCount}</strong><span>Open decisions</span></div></div>
        {!!results.errors?.length&&<section className="issue-group"><h3>Must be resolved</h3>{results.errors.map((x:string,i:number)=><div className="issue-card error" key={i}><span>ERROR</span><p>{x}</p></div>)}</section>}
        {!!results.warnings?.length&&<section className="issue-group"><h3>Review before relying on results</h3>{results.warnings.map((x:string,i:number)=><div className="issue-card warning" key={i}><span>WARNING</span><p>{x}</p></div>)}</section>}
        {!!results.open_decisions?.length&&<section className="issue-group"><h3>Assumptions and open design decisions</h3>{results.open_decisions.map((d:any)=><div className="issue-card decision" key={d.block_id}><span>{d.status}</span><div><strong>{d.block_name}</strong><p>{d.note||d.basis}</p><small>Confidence: {d.confidence||'Not stated'}</small></div></div>)}</section>}
      </>}
    </section></div>}

    {toolsOpen&&<div className="sheet-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)setToolsOpen(false)}}><section className="results-sheet tools-sheet">
      <div className="sheet-handle"/><div className="sheet-head"><div><span className="eyebrow">Decision support</span><h2>Analysis workspace</h2><p>Compare routes, test assumptions, and inspect the engineering model.</p></div><button className="close-button" onClick={()=>setToolsOpen(false)} aria-label="Close analysis">×</button></div>
      <div className="tool-tabs"><button className={toolTab==='scenarios'?'active':''} onClick={()=>{setToolTab('scenarios');setAnalysisData(null)}}>Scenarios & comparison</button><button className={toolTab==='sensitivity'?'active':''} onClick={()=>{setToolTab('sensitivity');setAnalysisData(null)}}>Sensitivity</button><button className={toolTab==='engineering'?'active':''} onClick={()=>{setToolTab('engineering');setAnalysisData(null);loadEngineeringData()}}>Engineering model</button></div>
      {toolTab==='scenarios'&&<div className="tool-content"><div className="tool-intro"><div><strong>Test a prepared scenario</strong><p>Run a controlled set of parameter changes against the validated reference route.</p></div><button onClick={compareWithReference} disabled={analysisBusy}>Compare current route with reference</button></div><div className="scenario-grid">{Object.entries(scenarios).map(([id,s]:any)=><article key={id}><span>{(s.tags||[]).join(' · ')}</span><h3>{s.name}</h3><p>{s.description}</p><small>{s.overrides?.length||0} parameter changes</small><button onClick={()=>runScenario(s)} disabled={analysisBusy}>Run scenario</button></article>)}</div></div>}
      {toolTab==='sensitivity'&&<div className="tool-content"><div className="sensitivity-form"><label><span>Process block</span><select value={sensitivityBlock} onChange={e=>{setSensitivityBlock(e.target.value);const b=flow?.blocks.find(x=>x.id===e.target.value);setSensitivityParameter(Object.keys(b?.params||{})[0]||'')}}>{flow?.blocks.filter(b=>Object.keys(b.params||{}).some(k=>typeof b.params[k]==='number')).map(b=><option key={b.id} value={b.id}>{b.name}</option>)}</select></label><label><span>Parameter</span><select value={sensitivityParameter} onChange={e=>setSensitivityParameter(e.target.value)}>{Object.entries(flow?.blocks.find(b=>b.id===sensitivityBlock)?.params||{}).filter(([,v])=>typeof v==='number').map(([k])=><option key={k} value={k}>{pretty(k)}</option>)}</select></label><label><span>Values (comma separated)</span><input value={sensitivityValues} onChange={e=>setSensitivityValues(e.target.value)}/></label><button className="primary" onClick={runSensitivity} disabled={analysisBusy}>Run sensitivity</button></div></div>}
      {toolTab==='engineering'&&<div className="tool-content"><div className="engineering-banner"><strong>Screening model—not a detailed-design property package</strong><p>Pressure-aware NRTL/VLE, dynamic heat recovery, and utility calculations are shown with their declared status.</p></div>{!analysisData&&!analysisBusy&&<button onClick={loadEngineeringData}>Load engineering summaries</button>}</div>}
      {analysisBusy&&<div className="tool-loading"><span className="spinner dark"/>Calculating…</div>}
      {analysisData?.kind==='error'&&<div className="inline-message error"><strong>Analysis could not be completed</strong><span>{String(analysisData.message)}</span></div>}
      {analysisData?.kind==='scenario'&&<div className="analysis-result"><h3>{analysisData.name}</h3><div className="result-kpis compact"><div><span>Ethanol</span><strong>{fmt(analysisData.data.scenario_kpis?.ethanol_product_tph,4)} <small>t/h</small></strong></div><div><span>Electricity</span><strong>{fmt(analysisData.data.scenario_kpis?.electrical_kW,1)} <small>kW</small></strong></div><div><span>Heat</span><strong>{fmt(analysisData.data.scenario_kpis?.thermal_kW,1)} <small>kW</small></strong></div><div><span>Steam</span><strong>{fmt(analysisData.data.scenario_kpis?.steam_kgph,0)} <small>kg/h</small></strong></div></div></div>}
      {analysisData?.kind==='comparison'&&<div className="analysis-result"><h3>Route comparison</h3><div className="comparison-table"><div className="table-head"><span>Route</span><span>Ethanol t/h</span><span>Electricity kW</span><span>Heat kW</span><span>Open decisions</span></div>{analysisData.data.map((r:any,i:number)=><div key={i}><strong>{r.name}</strong><span>{fmt(r.ethanol_product_tph,4)}</span><span>{fmt(r.utilities?.electricity_kW,1)}</span><span>{fmt(r.utilities?.thermal_kW,1)}</span><span>{r.open_decisions_count}</span></div>)}</div></div>}
      {analysisData?.kind==='sensitivity'&&<div className="analysis-result"><h3>Sensitivity result</h3><div className="sensitivity-chart">{analysisData.data.map((r:any,i:number)=>{const value=r.kpis?.ethanol_product_tph||0,max=Math.max(...analysisData.data.map((x:any)=>x.kpis?.ethanol_product_tph||0),1e-9);return <div key={i}><span>{r.value}</span><div><i style={{width:`${value/max*100}%`}}/></div><strong>{fmt(value,4)} t/h ethanol</strong></div>})}</div></div>}
      {analysisData?.kind==='engineering'&&<div className="analysis-result engineering-grid"><section><h3>Pressure-aware model</h3><div className="metric-list">{Object.entries(analysisData.summary||{}).filter(([,v])=>typeof v!=='object').map(([k,v]:any)=><div key={k}><span>{pretty(k)}</span><strong>{typeof v==='number'?fmt(v,3):String(v)}</strong></div>)}</div></section><section><h3>Dynamic heat system</h3><div className="metric-list">{Object.entries(analysisData.heat?.summary||{}).filter(([,v])=>typeof v!=='object').map(([k,v]:any)=><div key={k}><span>{pretty(k)}</span><strong>{typeof v==='number'?fmt(v,3):String(v)}</strong></div>)}</div></section><section><h3>VLE screening</h3><div className="metric-list">{Object.entries(analysisData.physics||{}).filter(([,v])=>typeof v!=='object').slice(0,10).map(([k,v]:any)=><div key={k}><span>{pretty(k)}</span><strong>{typeof v==='number'?fmt(v,3):String(v)}</strong></div>)}</div></section></div>}
    </section></div>}
  </div>
}

createRoot(document.getElementById('root')!).render(<App/>)

