from pathlib import Path
import re

p=Path('main.tsx')
text=p.read_text(encoding='utf-8')

new_twin=r'''function DigitalTwin({dynamic,results}:any){
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
    <div className="twin-toolbar"><div><span>V0.20 CONNECTED PLANT REPLAY</span><strong>Plant time {fmt(row.time_h||0,2)} h</strong></div><div className="twin-controls"><button onClick={()=>setPlaying(v=>!v)}>{playing?'Pause':'Play'}</button><button onClick={()=>setIndex(i=>Math.min(i+1,rows.length-1))}>Step</button><label>Speed <select value={speed} onChange={e=>setSpeed(Number(e.target.value))}><option value="1">1×</option><option value="4">4×</option><option value="12">12×</option><option value="32">32×</option></select></label></div></div>
    <input className="twin-scrubber" type="range" min="0" max={Math.max(0,rows.length-1)} value={index} onChange={e=>{setPlaying(false);setIndex(Number(e.target.value))}} aria-label="Digital twin time"/>
    <div style={{overflowX:'auto',padding:'8px 0 18px'}}><div style={{display:'flex',alignItems:'center',minWidth:1900,gap:4}}>
      <div className="feed-hopper"><div className="hopper-bin"/><strong>Miscanthus + water</strong><span>Feed preparation</span></div>
      <Pipe active={true} label="Feed pump"/><Box title="Pretreatment HX" sub="Feed heat exchanger" tone="#cb7a36"/><Pipe active={true}/>
      <TwinVessel label="Pretreatment" state={state('pretreat')} level={averageLevel('pretreat')} count={`${pretreat.installed_vessels||0} × ${fmt(pretreat.vessel_working_volume_m3,0)} m³`} tone="#cb7a36"/>
      <Pipe active={transferActive('pretreat','hydro')} label="Direct transfer"/>
      <TwinVessel label="Hydrolysis" state={state('hydro')} level={averageLevel('hydro')} count={`${hydro.installed_vessels||0} × ${fmt(hydro.vessel_working_volume_m3,0)} m³`} tone="#4c9a70"/>
      <Pipe active={transferActive('hydro','ferm')} label="Direct transfer"/>
      <TwinVessel label="Fermentation" state={state('ferm')} level={averageLevel('ferm')} count={`${ferm.installed_vessels||0} × ${fmt(ferm.vessel_working_volume_m3,0)} m³`} tone="#6779b8"/>
      <Pipe active={true}/><Box title="Solids separation" sub="Cake + liquid" tone="#68737b"/><Pipe active={true}/><Box title="Beer conditioning" sub="Preheat to column" tone="#c46b2b"/><Pipe active={true}/>
      <div className="twin-column"><div className="column-stack"><i/><i/><i/><i/><i/></div><strong>Beer column</strong><span>Continuous screening</span></div>
      <Pipe active={true}/><div className="twin-column"><div className="column-stack"><i/><i/><i/><i/><i/></div><strong>Rectifier</strong><span>Continuous screening</span></div>
      <Pipe active={true}/><Box title="Molecular sieve" sub="99.5 wt% ethanol" tone="#547fc1"/><Pipe active={true}/><Box title="Distillation utilities" sub="Thermal envelope" tone="#c46b2b"/><Pipe active={true}/>
      <div className="twin-product"><div>EtOH</div><strong>Anhydrous ethanol</strong><span>{fmt(results?.terminal_component_totals?.ethanol,3)} t/h</span></div>
    </div></div>
    <div style={{display:'grid',gridTemplateColumns:'repeat(5,minmax(130px,1fr))',gap:8,margin:'4px 0 12px'}}>
      <div className="dashboard-card"><span>CO₂ vent</span><strong>Fermentation side stream</strong></div>
      <div className="dashboard-card"><span>Residue solids</span><strong>Separation cake</strong></div>
      <div className="dashboard-card"><span>Beer bottoms</span><strong>Wastewater stream</strong></div>
      <div className="dashboard-card"><span>Rectifier bottoms</span><strong>Wastewater stream</strong></div>
      <div className="dashboard-card"><span>Sieve recycle</span><strong>Recycle placeholder</strong></div>
    </div>
    <div className="twin-readouts"><div><span>Completed feed</span><strong>{fmt(throughput.average_completed_feed_tph,2)} t/h</strong></div><div><span>Annual ethanol</span><strong>{fmt((throughput.ethanol_product_L_per_8000h_year||0)/1e6,2)} ML/y</strong></div><div><span>Blocked events</span><strong>{op.blocking_events||0}</strong></div><div><span>Pump contention</span><strong>{op.pump_contention_events||0}</strong></div></div>
    <p className="model-boundary"><strong>V0.20 model boundary:</strong> no intermediate buffer vessels are assumed. Pretreatment transfers directly into Hydrolysis and Hydrolysis directly into Fermentation. Batch vessel states and inventories are event-resolved; Solids Separation, Beer Conditioning, Beer Column, Rectifier, Molecular Sieve and the utility envelope remain continuous engineering-screening blocks.</p>
  </div>
}
'''

pattern=r'function DigitalTwin\(\{dynamic,results\}:any\)\{.*?\n\}\n\nfunction ProcessNode'
if not re.search(pattern,text,re.S):
    raise SystemExit('DigitalTwin function boundary not found')
text=re.sub(pattern,new_twin+'\nfunction ProcessNode',text,count=1,flags=re.S)

text=text.replace('Current V0.18 boundary:</strong> each process section is staggered independently. Inter-stage material availability, buffers, shared transfer pumps, blocking, starvation and shared utility-resource contention are not yet enforced.',
                  'V0.20 connected boundary:</strong> no intermediate buffer vessels are assumed. Pretreatment → Hydrolysis → Fermentation transfers are direct and event-resolved; downstream recovery remains continuous screening.')

p.write_text(text,encoding='utf-8')
print('Patched V0.20 Digital Twin to match the reference process flow.')
