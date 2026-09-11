
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict,Any,List
import copy
from flowsheet import Flowsheet

@dataclass
class ParameterOverride:
    block_id:str
    parameter:str
    value:Any

@dataclass
class ScenarioDefinition:
    name:str
    description:str
    overrides:List[ParameterOverride]
    tags:List[str]

class ScenarioManager:
    def __init__(self,baseline_definition):
        self.baseline_definition=copy.deepcopy(baseline_definition)

    def apply(self,scenario):
        d=copy.deepcopy(self.baseline_definition)
        blocks={b["id"]:b for b in d["blocks"]}
        for o in scenario.overrides:
            if o.block_id not in blocks: raise KeyError(o.block_id)
            blocks[o.block_id].setdefault("params",{})[o.parameter]=o.value
        d["name"]=scenario.name
        d["scenario_metadata"]={
            "description":scenario.description,
            "tags":scenario.tags,
            "overrides":[o.__dict__ for o in scenario.overrides]}
        return d

    def run(self,scenario):
        return Flowsheet(self.apply(scenario)).run()

def extract_kpis(r):
    br=r.get("block_results",{})
    def m(b,k,d=0.0): return br.get(b,{}).get("metrics",{}).get(k,d)
    ts=r.get("terminal_summary",{})
    return {
        "ethanol_product_tph":r.get("terminal_component_totals",{}).get("ethanol",0.0),
        "gross_ethanol_tph":m("ferm","ethanol_tph"),
        "glucose_tph":m("hydro","glucose_produced_tph"),
        "process_water_tph":r.get("utility_totals",{}).get("process_water_tph",0.0),
        "electrical_kW":r.get("utility_totals",{}).get("electricity_kW",0.0),
        "thermal_kW":r.get("utility_totals",{}).get("thermal_kW",0.0),
        "cooling_kW":r.get("utility_totals",{}).get("cooling_kW",0.0),
        "steam_kgph":r.get("utility_totals",{}).get("steam_kgph",0.0),
        "wastewater_tph":ts.get("wastewater_tph",0.0),
        "solid_product_tph":ts.get("solid_products_tph",0.0),
        "hydrolysis_vessels":m("hydro","vessel_count"),
        "fermentation_vessels":m("ferm","vessel_count"),
        "separator_cake_tph":br.get("sep",{}).get("outputs",{}).get("cake",{}).get("total_tph",0.0),
    }

def compare_to_baseline(b,c):
    out={}
    for k,bv in b.items():
        cv=c.get(k)
        if cv is None: continue
        d=cv-bv
        out[k]={"baseline":bv,"scenario":cv,"delta":d,"delta_percent":100*d/bv if bv not in (0,None) else None}
    return out

class SensitivityRunner:
    def __init__(self,manager): self.manager=manager

    def one_at_a_time(self,block_id,parameter,values,base_name="Sensitivity"):
        bk=extract_kpis(Flowsheet(self.manager.baseline_definition).run())
        rows=[]
        for v in values:
            s=ScenarioDefinition(f"{base_name}: {v}","One-at-a-time sensitivity",
                [ParameterOverride(block_id,parameter,v)],["sensitivity","OAT"])
            r=self.manager.run(s); k=extract_kpis(r)
            rows.append({"value":v,"errors":r.get("errors",[]),"kpis":k,"delta_from_baseline":compare_to_baseline(bk,k)})
        return rows
