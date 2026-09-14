from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any

from native_dynamic_engine import build_native_dynamic_simulation


def _f(value: Any, default: float = 0.0) -> float:
    try:
        value=float(value)
        return value if math.isfinite(value) else default
    except (TypeError,ValueError):
        return default


@dataclass
class PumpResource:
    pump_id: str
    name: str
    busy_until_h: float=0.0
    owner: str|None=None
    busy_h: float=0.0
    contention_count: int=0

    def available(self, now: float)->bool:
        return now >= self.busy_until_h-1e-9

    def reserve(self, now: float, duration_h: float, owner: str)->None:
        if not self.available(now):
            self.contention_count += 1
            raise RuntimeError(f"Pump {self.pump_id} already reserved")
        self.busy_until_h=now+max(0.0,duration_h)
        self.owner=owner
        self.busy_h += max(0.0,duration_h)


@dataclass
class Vessel:
    vessel_id: str
    section_id: str
    section_type: str
    batch_capacity_t: float
    state: str="AVAILABLE"
    state_started_h: float=0.0
    state_until_h: float=0.0
    batch_mass_t: float=0.0
    transfer_base_mass_t: float=0.0
    source_feed_equivalent_t: float=0.0
    pending_action: str|None=None
    transfer_source_id: str|None=None
    completed_batches: int=0
    state_time_h: dict[str,float]=field(default_factory=dict)


def _section_specs(legacy:dict)->list[dict]:
    rows=[dict(x) for x in legacy.get("vessel_schedules",[]) if x.get("block_type") in {"pretreatment","hydrolysis","fermentation"}]
    order={"pretreatment":0,"hydrolysis":1,"fermentation":2}
    return sorted(rows,key=lambda x:order[x["block_type"]])


def _duration(schedule:dict,name:str)->float:
    for p in schedule.get("phases",[]):
        if p.get("name")==name:
            return max(0.0,_f(p.get("duration_h")))
    return 0.0


def _processing_duration(schedule:dict)->float:
    names={"pretreatment":("HEATING_PRETREATMENT","REACTION_HOLD"),"hydrolysis":("ENZYMATIC_HYDROLYSIS",),"fermentation":("FERMENTATION",)}[schedule["block_type"]]
    return sum(_duration(schedule,n) for n in names)


def _cip_duration(schedule:dict)->float:
    return _duration(schedule,"CIP_TURNAROUND")


def _ethanol_ratio(results:dict,first:dict)->float:
    ethanol=_f((results.get("terminal_component_totals") or {}).get("ethanol"))
    feed=_f(first.get("required_throughput_tph"))
    return ethanol/feed if feed>0 else 0.0


def build_connected_dynamic_simulation(definition:dict,results:dict,timestep_min:int=15,horizon_h:float=168.0)->dict:
    """V0.21.0 direct-transfer connected plant scheduler aligned to the P01-P12 reference route.

    No intermediate buffer vessels are assumed. Upstream vessels may only
    discharge directly into an available downstream process vessel. If the
    downstream vessel is smaller than the transferred batch or unavailable,
    the upstream vessel remains BLOCKED. If a downstream vessel is only
    partially filled it remains WAITING_FOR_FEED until enough upstream batches
    arrive to reach its configured working batch mass.
    """
    legacy=build_native_dynamic_simulation(definition,results,timestep_min=timestep_min,horizon_h=horizon_h)
    sections=_section_specs(legacy)
    if len(sections)<3:
        out=dict(legacy)
        out.update({"engine":"Bio-Agri direct-transfer dynamic plant engine","engine_version":"0.21.0","connected_material_transfers":False})
        return out

    pt,hy,fe=sections[:3]
    schedule_by_type={s["block_type"]:s for s in sections[:3]}
    vessels=[]
    for sec in sections[:3]:
        for i in range(int(sec["installed_vessels"])):
            vessels.append(Vessel(f'{sec["block_id"]}-V{i+1:02d}',sec["block_id"],sec["block_type"],_f(sec["batch_mass_t"])))

    pumps={}
    for sec in sections[:3]:
        pumps[f'{sec["block_id"]}_in']=PumpResource(f'{sec["block_id"]}_in',f'{sec["block_name"]} inlet transfer pump')
        pumps[f'{sec["block_id"]}_out']=PumpResource(f'{sec["block_id"]}_out',f'{sec["block_name"]} outlet transfer pump')

    dt=max(1,int(timestep_min))/60.0
    horizon=max(1.0,float(horizon_h))
    events=[];timeline=[]
    source_feed_started=0.0;completed_source_feed=0.0
    starvation_events=0;blocking_events=0

    def set_state(v,state,now,dur=0.0,action=None):
        v.state=state
        v.state_started_h=now
        v.state_until_h=now+max(0.0,dur)
        v.pending_action=action

    def process_state(v):
        return {"pretreatment":"HEATING_PRETREATMENT","hydrolysis":"ENZYMATIC_HYDROLYSIS","fermentation":"FERMENTATION"}[v.section_type]

    def maybe_start_processing(v,now):
        if v.batch_mass_t+1e-9 >= v.batch_capacity_t:
            set_state(v,process_state(v),now,_processing_duration(schedule_by_type[v.section_type]),"PROCESS_COMPLETE")
            events.append({"time_h":now,"event":"BATCH_READY","vessel":v.vessel_id,"mass_t":v.batch_mass_t})
        else:
            set_state(v,"WAITING_FOR_FEED",now)

    def complete_due(v,now):
        nonlocal completed_source_feed
        if now+1e-9 < v.state_until_h:return
        if v.pending_action=="SOURCE_FILL_COMPLETE":
            maybe_start_processing(v,now)
        elif v.pending_action=="PROCESS_COMPLETE":
            set_state(v,"WAITING_FOR_DESTINATION",now)
            events.append({"time_h":now,"event":"PROCESS_COMPLETE","vessel":v.vessel_id})
        elif v.pending_action=="TRANSFER_COMPLETE":
            src=next(x for x in vessels if x.vessel_id==v.transfer_source_id)
            transferred=src.batch_mass_t
            v.batch_mass_t += transferred
            v.source_feed_equivalent_t += src.source_feed_equivalent_t
            events.append({"time_h":now,"event":"DIRECT_TRANSFER_COMPLETE","from":src.vessel_id,"to":v.vessel_id,"mass_t":transferred})
            src.batch_mass_t=0.0;src.source_feed_equivalent_t=0.0
            src.transfer_source_id=None
            set_state(src,"CIP_TURNAROUND",now,_cip_duration(schedule_by_type[src.section_type]),"CIP_COMPLETE")
            v.transfer_source_id=None
            maybe_start_processing(v,now)
        elif v.pending_action=="FINAL_EMPTY_COMPLETE":
            completed_source_feed += v.source_feed_equivalent_t
            events.append({"time_h":now,"event":"FINAL_DISCHARGE_COMPLETE","vessel":v.vessel_id,"mass_t":v.batch_mass_t})
            v.batch_mass_t=0.0;v.source_feed_equivalent_t=0.0
            set_state(v,"CIP_TURNAROUND",now,_cip_duration(schedule_by_type[v.section_type]),"CIP_COMPLETE")
        elif v.pending_action=="CIP_COMPLETE":
            v.completed_batches+=1
            set_state(v,"AVAILABLE",now)

    def try_source_fill(v,now):
        nonlocal source_feed_started
        if v.section_type!="pretreatment" or v.state!="AVAILABLE":return False
        pump=pumps[f'{v.section_id}_in']
        if not pump.available(now):
            pump.contention_count+=1;return False
        dur=_f(schedule_by_type[v.section_type].get("fill_time_h"))
        pump.reserve(now,dur,v.vessel_id)
        v.batch_mass_t=v.batch_capacity_t
        v.transfer_base_mass_t=0.0
        v.source_feed_equivalent_t=v.batch_capacity_t
        source_feed_started += v.batch_capacity_t
        set_state(v,"FILLING",now,dur,"SOURCE_FILL_COMPLETE")
        events.append({"time_h":now,"event":"SOURCE_FILL_START","vessel":v.vessel_id,"mass_t":v.batch_capacity_t})
        return True

    def downstream_candidates(section_type):
        next_type={"pretreatment":"hydrolysis","hydrolysis":"fermentation"}.get(section_type)
        if not next_type:return []
        return [v for v in vessels if v.section_type==next_type and v.state in {"AVAILABLE","WAITING_FOR_FEED","STARVED"}]

    def try_direct_transfer(src,now):
        nonlocal blocking_events
        if src.state not in {"WAITING_FOR_DESTINATION","BLOCKED"}:return False
        if src.section_type=="fermentation":
            pump=pumps[f'{src.section_id}_out']
            if not pump.available(now):
                pump.contention_count+=1;return False
            dur=_f(schedule_by_type[src.section_type].get("empty_time_h"))
            pump.reserve(now,dur,src.vessel_id)
            set_state(src,"EMPTYING",now,dur,"FINAL_EMPTY_COMPLETE")
            events.append({"time_h":now,"event":"FINAL_DISCHARGE_START","vessel":src.vessel_id,"mass_t":src.batch_mass_t})
            return True

        candidates=downstream_candidates(src.section_type)
        dest=None
        for d in candidates:
            remaining=d.batch_capacity_t-d.batch_mass_t
            if remaining+1e-9 >= src.batch_mass_t:
                dest=d;break
        if dest is None:
            if src.state!="BLOCKED":blocking_events+=1
            set_state(src,"BLOCKED",now)
            return False

        outp=pumps[f'{src.section_id}_out']; inp=pumps[f'{dest.section_id}_in']
        if not outp.available(now) or not inp.available(now):
            if not outp.available(now):outp.contention_count+=1
            if not inp.available(now):inp.contention_count+=1
            return False

        out_rate=_f(schedule_by_type[src.section_type].get("outlet_transfer_pump_rate_m3ph"))
        in_rate=_f(schedule_by_type[dest.section_type].get("inlet_transfer_pump_rate_m3ph"))
        mass=src.batch_mass_t
        dur=max(mass/max(out_rate,1e-9),mass/max(in_rate,1e-9))
        outp.reserve(now,dur,src.vessel_id);inp.reserve(now,dur,dest.vessel_id)
        set_state(src,"EMPTYING",now,dur,"TRANSFER_SOURCE_WAIT")
        dest.transfer_source_id=src.vessel_id
        dest.transfer_base_mass_t=dest.batch_mass_t
        set_state(dest,"FILLING",now,dur,"TRANSFER_COMPLETE")
        events.append({"time_h":now,"event":"DIRECT_TRANSFER_START","from":src.vessel_id,"to":dest.vessel_id,"mass_t":mass})
        return True

    def display_inventory(v,now):
        dur=max(v.state_until_h-v.state_started_h,0.0)
        progress=1.0 if dur<=1e-12 else min(max((now-v.state_started_h)/dur,0.0),1.0)
        if v.state=="FILLING" and v.pending_action=="SOURCE_FILL_COMPLETE":
            return v.batch_capacity_t*progress
        if v.state=="FILLING" and v.pending_action=="TRANSFER_COMPLETE":
            src=next((x for x in vessels if x.vessel_id==v.transfer_source_id),None)
            incoming=(src.batch_mass_t if src else 0.0)*progress
            return min(v.batch_capacity_t,v.transfer_base_mass_t+incoming)
        if v.state=="EMPTYING" and v.pending_action in {"TRANSFER_SOURCE_WAIT","FINAL_EMPTY_COMPLETE"}:
            return max(0.0,v.batch_mass_t*(1.0-progress))
        return v.batch_mass_t

    t=0.0
    while t<horizon-1e-9:
        for v in vessels: complete_due(v,t)

        for typ in ("fermentation","hydrolysis","pretreatment"):
            for v in vessels:
                if v.section_type==typ: try_direct_transfer(v,t)

        for v in vessels: try_source_fill(v,t)

        for v in vessels:
            if v.section_type!="pretreatment" and v.state=="AVAILABLE":
                v.state="STARVED"
                starvation_events+=1
            v.state_time_h[v.state]=v.state_time_h.get(v.state,0.0)+dt

        timeline.append({
            "time_h":round(t,6),
            "states":{sec["block_id"]:[v.state for v in vessels if v.section_id==sec["block_id"]] for sec in sections[:3]},
            "vessel_inventory_t":{v.vessel_id:display_inventory(v,t) for v in vessels},
            "vessel_fill_fraction":{v.vessel_id:(display_inventory(v,t)/v.batch_capacity_t if v.batch_capacity_t>0 else 0.0) for v in vessels},
            "pump_owner":{pid:(p.owner if not p.available(t) else None) for pid,p in pumps.items()},
            "completed_source_feed_t":completed_source_feed,
        })
        t+=dt

    ratio=_ethanol_ratio(results,pt)
    ethanol_t=completed_source_feed*ratio
    feed_tph=completed_source_feed/horizon
    ethanol_tph=ethanol_t/horizon
    ethanol_Lph=ethanol_tph/0.78937*1000.0
    source_in_system=sum(v.source_feed_equivalent_t for v in vessels)
    mass_error=source_feed_started-completed_source_feed-source_in_system

    out=dict(legacy)
    out.update({
        "engine":"Bio-Agri direct-transfer dynamic plant engine",
        "engine_version":"0.21.0",
        "connected_material_transfers":True,
        "intermediate_buffers_assumed":False,
        "shared_pumps":[asdict(p) for p in pumps.values()],
        "event_log":events,
        "timeline":timeline,
        "connected_throughput":{
            "source_feed_started_t":source_feed_started,
            "source_feed_completed_t":completed_source_feed,
            "average_completed_feed_tph":feed_tph,
            "ethanol_product_t":ethanol_t,
            "ethanol_product_tph":ethanol_tph,
            "ethanol_product_Lph":ethanol_Lph,
            "ethanol_product_L_per_8000h_year":ethanol_Lph*8000.0,
            "mass_balance_error_t":mass_error,
        },
        "operability":{
            "starvation_events":starvation_events,
            "blocking_events":blocking_events,
            "pump_contention_events":sum(p.contention_count for p in pumps.values()),
            "vessels":[{"vessel_id":v.vessel_id,"section_id":v.section_id,"completed_batches":v.completed_batches,"final_inventory_t":v.batch_mass_t,"state_time_h":dict(v.state_time_h)} for v in vessels],
        },
        "status":"V0.20 DIRECT VESSEL-TO-VESSEL DISCRETE-EVENT SCREENING",
        "notes":list(legacy.get("notes",[]))+[
            "No intermediate buffer vessels are assumed by V0.20.",
            "Pretreatment transfers directly into hydrolysis vessels; hydrolysis transfers directly into fermentation vessels.",
            "Downstream vessels may accumulate partial direct fills where upstream and downstream batch sizes differ.",
            "An upstream vessel remains BLOCKED until a downstream vessel has sufficient free capacity.",
            "Reaction chemistry and utility calculations remain based on the validated steady-state/V0.19 engineering model during this alpha.",
        ],
    })
    return out
