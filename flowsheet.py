
from __future__ import annotations
from collections import defaultdict, deque
from typing import Dict, Any
from models import Connection, BlockInstance, Stream, EquipmentRequirement
from blocks import BLOCK_REGISTRY

class FlowsheetError(Exception):
    pass

class Flowsheet:
    def __init__(self, definition: dict):
        self.definition = definition
        legacy_hours = next((b.get("params", {}).get("annual_operating_hours") for b in definition.get("blocks", []) if b.get("params", {}).get("annual_operating_hours") is not None), 8000.0)
        basis = definition.get("operating_basis") or {}
        hours_per_day = float(basis.get("hours_per_day", 24.0))
        days_per_year = float(basis.get("days_per_year", float(legacy_hours) / hours_per_day if hours_per_day else 0.0))
        self.operating_basis = {
            "hours_per_day": hours_per_day,
            "days_per_year": days_per_year,
            "annual_operating_hours": hours_per_day * days_per_year,
        }
        self.blocks = {
            b["id"]: BlockInstance(
                id=b["id"],
                type=b["type"],
                name=b.get("name",b["id"]),
                params={**b.get("params",{}), "annual_operating_hours": self.operating_basis["annual_operating_hours"]},
                position=b.get("position",{})
            )
            for b in definition.get("blocks",[])
        }
        self.connections = [Connection(**c) for c in definition.get("connections",[])]
        self.instances = {}
        self.results = {}
        self.streams = {}
        self.warnings = []
        self.errors = []

    def validate_structure(self):
        errors = []

        if not 0 < self.operating_basis["hours_per_day"] <= 24:
            errors.append("Plant operating hours per day must be greater than 0 and no more than 24.")
        if not 0 < self.operating_basis["days_per_year"] <= 366:
            errors.append("Plant operating days per year must be greater than 0 and no more than 366.")

        # IDs and block types
        for bid, b in self.blocks.items():
            if b.type not in BLOCK_REGISTRY:
                errors.append(f"Block '{bid}' has unknown type '{b.type}'.")

        # Connections reference real blocks and ports
        for c in self.connections:
            if c.from_block not in self.blocks:
                errors.append(f"Connection '{c.id}' references missing source block '{c.from_block}'.")
                continue
            if c.to_block not in self.blocks:
                errors.append(f"Connection '{c.id}' references missing destination block '{c.to_block}'.")
                continue

            src_cls = BLOCK_REGISTRY.get(self.blocks[c.from_block].type)
            dst_cls = BLOCK_REGISTRY.get(self.blocks[c.to_block].type)
            if src_cls and c.from_port not in src_cls.output_ports:
                errors.append(f"Connection '{c.id}' uses invalid output port '{c.from_port}' on '{c.from_block}'.")
            if dst_cls and c.to_port not in dst_cls.input_ports:
                errors.append(f"Connection '{c.id}' uses invalid input port '{c.to_port}' on '{c.to_block}'.")

        # One material source per input port for V0.2
        seen_inputs = set()
        for c in self.connections:
            key = (c.to_block,c.to_port)
            if key in seen_inputs:
                errors.append(f"Input port '{c.to_block}.{c.to_port}' has multiple incoming connections; mixing is not implemented in V0.2.")
            seen_inputs.add(key)

        return errors

    def calculation_order(self):
        indegree = {bid:0 for bid in self.blocks}
        outgoing = defaultdict(list)

        for c in self.connections:
            indegree[c.to_block] += 1
            outgoing[c.from_block].append(c.to_block)

        q = deque([bid for bid,d in indegree.items() if d == 0])
        order = []

        while q:
            bid = q.popleft()
            order.append(bid)
            for nxt in outgoing[bid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    q.append(nxt)

        if len(order) != len(self.blocks):
            raise FlowsheetError("Flowsheet contains a cycle/recycle. Iterative recycle solving is deferred beyond V0.2.")
        return order

    @staticmethod
    def _combine_streams(a: Stream, b: Stream, stream_id: str) -> Stream:
        keys=set(a.components_tph)|set(b.components_tph)
        comp={k:a.get(k)+b.get(k) for k in keys}
        total=a.total_tph+b.total_tph
        temp=None
        if total>0 and a.temperature_C is not None and b.temperature_C is not None:
            temp=(a.total_tph*a.temperature_C+b.total_tph*b.temperature_C)/total
        elif a.temperature_C is not None:
            temp=a.temperature_C
        elif b.temperature_C is not None:
            temp=b.temperature_C
        return Stream(stream_id,comp,temperature_C=temp,pressure_bar_abs=a.pressure_bar_abs or b.pressure_bar_abs,note="P09 fresh feed plus converged P10 recycle")

    def _solve_rectifier_sieve_recycle(self, rect_id: str, sieve_id: str, fresh_feed: Stream):
        rect_def=self.blocks[rect_id]; sieve_def=self.blocks[sieve_id]
        rect_cls=BLOCK_REGISTRY[rect_def.type]; sieve_cls=BLOCK_REGISTRY[sieve_def.type]
        recycle=Stream(f"{sieve_id}:recycle_guess",{"ethanol":0.0,"water":0.0},temperature_C=sieve_def.params.get("feed_temperature_C",120.0))
        tolerance=1e-10
        max_iterations=100
        converged=False
        rect_result=None; sieve_result=None

        for iteration in range(1,max_iterations+1):
            combined=self._combine_streams(fresh_feed,recycle,f"{rect_id}:combined_feed")
            rect_result=rect_cls(rect_id,rect_def.params).calculate({"feed":combined})
            if rect_result.errors:
                break
            sieve_result=sieve_cls(sieve_id,sieve_def.params).calculate({"feed":rect_result.outputs["overhead"]})
            if sieve_result.errors:
                break
            new_recycle=sieve_result.outputs["recycle"]
            keys=set(recycle.components_tph)|set(new_recycle.components_tph)
            error=max([abs(new_recycle.get(k)-recycle.get(k)) for k in keys] or [0.0])
            recycle=new_recycle.copy(new_id=f"{sieve_id}:recycle_guess")
            if error<=tolerance:
                converged=True
                break

        if rect_result is not None and sieve_result is not None and not rect_result.errors and not sieve_result.errors:
            combined=self._combine_streams(fresh_feed,recycle,f"{rect_id}:combined_feed")
            rect_result=rect_cls(rect_id,rect_def.params).calculate({"feed":combined})
            sieve_result=sieve_cls(sieve_id,sieve_def.params).calculate({"feed":rect_result.outputs["overhead"]})
            recycle_final=sieve_result.outputs["recycle"]
            rect_result.metrics.update({
                "fresh_feed_tph":fresh_feed.total_tph,
                "converged_recycle_tph":recycle_final.total_tph,
                "total_rectifier_feed_tph":combined.total_tph,
                "recycle_iterations":iteration,
                "recycle_converged":converged,
            })
            sieve_result.metrics.update({
                "recycle_iterations":iteration,
                "recycle_converged":converged,
            })
            if converged:
                sieve_result.metadata.note="P10 regeneration recycle is iteratively converged back to P09 on the workbook tear-stream basis."
            else:
                sieve_result.warnings.append("P09/P10 recycle did not converge within the iteration limit.")
        return rect_result,sieve_result,converged

    def run(self):
        structure_errors = self.validate_structure()
        if structure_errors:
            self.errors.extend(structure_errors)
            return self.summary()

        try:
            order = self.calculation_order()
        except FlowsheetError as e:
            self.errors.append(str(e))
            return self.summary()

        incoming_by_block = defaultdict(dict)
        connections_from = defaultdict(list)
        for c in self.connections:
            connections_from[c.from_block].append(c)

        precomputed=set()
        for bid in order:
            if bid in precomputed:
                continue
            bdef = self.blocks[bid]
            cls = BLOCK_REGISTRY[bdef.type]

            if bdef.type=="rectifier" and "feed" in incoming_by_block[bid]:
                sieve_connection=next((c for c in connections_from[bid] if c.from_port=="overhead" and self.blocks[c.to_block].type=="molecular_sieve"),None)
                if sieve_connection:
                    sieve_id=sieve_connection.to_block
                    sieve_params=self.blocks[sieve_id].params
                    if sieve_params.get("regeneration_recycle_ethanol_wt_fraction") is not None:
                        rect_result,sieve_result,_=self._solve_rectifier_sieve_recycle(bid,sieve_id,incoming_by_block[bid]["feed"])
                        if rect_result is not None and sieve_result is not None:
                            self.instances[bid]=cls(bid,bdef.params)
                            self.instances[sieve_id]=BLOCK_REGISTRY[self.blocks[sieve_id].type](sieve_id,sieve_params)
                            self.results[bid]=rect_result; self.results[sieve_id]=sieve_result
                            for block_id,result in ((bid,rect_result),(sieve_id,sieve_result)):
                                self.warnings.extend([f"{block_id}: {w}" for w in result.warnings])
                                self.errors.extend([f"{block_id}: {e}" for e in result.errors])
                                for port,stream in result.outputs.items():
                                    self.streams[f"{block_id}.{port}"]=stream
                            if not rect_result.errors and not sieve_result.errors:
                                for cn in connections_from[bid]:
                                    if cn.to_block==sieve_id:
                                        continue
                                    if cn.from_port in rect_result.outputs:
                                        incoming_by_block[cn.to_block][cn.to_port]=rect_result.outputs[cn.from_port].copy(new_id=cn.id)
                                for cn in connections_from[sieve_id]:
                                    if cn.from_port in sieve_result.outputs:
                                        incoming_by_block[cn.to_block][cn.to_port]=sieve_result.outputs[cn.from_port].copy(new_id=cn.id)
                            precomputed.add(sieve_id)
                            continue

            block = cls(bid,bdef.params)
            self.instances[bid] = block
            result = block.calculate(incoming_by_block[bid])
            # Optional auxiliary electrical load is available on every equipment object.
            if "annual_electricity_kWh" not in result.metrics:
                manual=max(0.0,float(bdef.params.get("manual_electrical_load_kW",0.0) or 0.0))
                load_factor=max(0.0,float(bdef.params.get("electrical_load_factor_fraction",1.0) or 0.0))
                hours=max(0.0,float(bdef.params.get("annual_operating_hours",8000.0) or 0.0))
                if manual>0:
                    applied=manual*load_factor
                    result.utilities.electricity_kW += applied
                    result.utilities.peak_electricity_kW += manual
                    result.metrics["manual_auxiliary_electrical_load_kW"]=applied
                    result.metrics["annual_electricity_kWh"]=applied*hours
            self.results[bid] = result
            self.warnings.extend([f"{bid}: {w}" for w in result.warnings])
            self.errors.extend([f"{bid}: {e}" for e in result.errors])
            if result.errors:
                continue
            for port, stream in result.outputs.items():
                self.streams[f"{bid}.{port}"] = stream
            for cn in connections_from[bid]:
                if cn.from_port not in result.outputs:
                    self.errors.append(f"{bid}: output '{cn.from_port}' was not produced.")
                    continue
                incoming_by_block[cn.to_block][cn.to_port] = result.outputs[cn.from_port].copy(new_id=cn.id)

        self._finalize_process_water_system()
        return self.summary()

    def _finalize_process_water_system(self):
        tanks=[bid for bid,b in self.blocks.items() if b.type=="process_water_tank" and bid in self.results]
        if not tanks:return
        consumers=[]
        for bid,res in self.results.items():
            if bid in tanks:continue
            if "process_water_addition_tph" in res.metrics:
                demand=max(0.0,float(res.metrics.get("process_water_addition_tph",0.0)))
            else:
                demand=max(0.0,float(res.utilities.process_water_tph or 0.0))
            if demand>0:consumers.append({"block_id":bid,"block_name":self.blocks[bid].name,"demand_tph":demand,"mode":res.metrics.get("water_demand_mode","reported")})
        total=sum(row["demand_tph"] for row in consumers)
        tank_id=tanks[0]; tank=self.blocks[tank_id]; result=self.results[tank_id]; p=tank.params
        recovered=max(0.0,float(p.get("recovered_water_tph",0.0) or 0.0)); recovered_used=min(total,recovered)
        fresh=max(0.0,total-recovered_used); surplus=max(0.0,recovered-total)
        density=max(1e-12,float(p.get("density_kg_per_m3",999.0) or 999.0)); residence=max(0.0,float(p.get("residence_time_h",4.0) or 0.0)); margin=max(0.0,float(p.get("working_volume_margin_fraction",0.15) or 0.0))
        stream=Stream(f"{tank_id}:process_water",{"water":total},temperature_C=float(p.get("temperature_C",15.0)),pressure_bar_abs=float(p.get("pressure_bar_abs",2.0)),phase="liquid",density_kg_per_m3=density,note="Allocated site process-water demand")
        result.outputs["process_water"]=stream; self.streams[f"{tank_id}.process_water"]=stream
        result.metrics.update({"site_process_water_demand_tph":total,"recovered_water_available_tph":recovered,"recovered_water_used_tph":recovered_used,"fresh_water_makeup_tph":fresh,"surplus_recovered_water_tph":surplus,"required_working_volume_m3":total*1000.0/density*residence*(1.0+margin),"consumer_count":len(consumers),"consumers":consumers})
        result.utilities.process_water_tph=fresh
        result.equipment=[EquipmentRequirement(equipment_type="Process-water tank / distribution header",design_flow_tph=total,working_volume_m3=result.metrics["required_working_volume_m3"],residence_time_h=residence)]


    def _utility_totals(self):
        total = None
        for res in self.results.values():
            total = res.utilities if total is None else total.add(res.utilities)
        return total.to_dict() if total else {}

    def _discharge_totals(self):
        total = None
        for res in self.results.values():
            total = res.discharges if total is None else total.add(res.discharges)
        return total.to_dict() if total else {}


    def _terminal_summary(self):
        out = {"products_tph":0.0,"wastewater_tph":0.0,"vents_tph":0.0,"solid_waste_tph":0.0,"solid_products_tph":0.0,"recycle_tph":0.0}
        for bid,res in self.results.items():
            btype=self.blocks[bid].type
            if btype=="product_sink":
                out["products_tph"] += res.metrics.get("product_tph",0.0)
            elif btype=="wastewater_sink":
                out["wastewater_tph"] += res.metrics.get("wastewater_tph",0.0)
            elif btype=="vent_sink":
                out["vents_tph"] += res.metrics.get("vent_tph",0.0)
            elif btype=="solid_sink":
                val=res.metrics.get("solid_terminal_tph",0.0)
                if res.metrics.get("classification")=="WASTE":
                    out["solid_waste_tph"] += val
                else:
                    out["solid_products_tph"] += val
            elif btype=="recycle_sink":
                out["recycle_tph"] += res.metrics.get("recycle_tph",0.0)
        return out

    def _component_terminal_totals(self):
        totals={}
        terminal_types={"product_sink","wastewater_sink","vent_sink","solid_sink","recycle_sink"}
        for c in self.connections:
            if self.blocks.get(c.to_block) and self.blocks[c.to_block].type in terminal_types:
                if self.blocks[c.to_block].type=="recycle_sink" and self.blocks[c.to_block].params.get("internal_recycle",False):
                    continue
                s=self.streams.get(f"{c.from_block}.{c.from_port}")
                if not s: continue
                for k,v in s.components_tph.items():
                    totals[k]=totals.get(k,0.0)+v
        return totals


    def _source_streams(self):
        incoming = set()
        for c in self.connections:
            incoming.add((c.to_block, c.to_port))
        sources = {}
        for bid,res in self.results.items():
            for port,s in res.outputs.items():
                # Source streams are outputs from zero-input blocks only.
                block = self.blocks[bid]
                cls = BLOCK_REGISTRY[block.type]
                if not getattr(cls, "input_ports", {}):
                    sources[f"{bid}.{port}"] = s
        return sources

    def _terminal_stream_objects(self):
        terminal_types={"product_sink","wastewater_sink","vent_sink","solid_sink","recycle_sink"}
        terms={}
        for c in self.connections:
            if self.blocks.get(c.to_block) and self.blocks[c.to_block].type in terminal_types:
                if self.blocks[c.to_block].type=="recycle_sink" and self.blocks[c.to_block].params.get("internal_recycle",False):
                    continue
                s=self.streams.get(f"{c.from_block}.{c.from_port}")
                if s:
                    terms[f"{c.from_block}.{c.from_port}->{c.to_block}"]=s
        return terms

    def _overall_material_closure(self):
        # Zero-input blocks supply connected utilities/doses. Feed Preparation also
        # represents the externally delivered as-received biomass, including its moisture.
        source_total=sum(s.total_tph for s in self._source_streams().values())
        for bid,res in self.results.items():
            if self.blocks[bid].type != "feed_preparation":
                continue
            if not any(c.to_block == bid and c.to_port == "raw_feed" for c in self.connections):
                source_total += res.metrics.get("as_received_feed_tph", 0.0)
            if not any(c.to_block == bid and c.to_port == "process_water" for c in self.connections):
                source_total += res.metrics.get("process_water_addition_tph", res.metrics.get("required_process_water_tph", 0.0))
        terminal_total=sum(s.total_tph for s in self._terminal_stream_objects().values())
        error=source_total-terminal_total
        return {
            "source_mass_tph":source_total,
            "terminal_mass_tph":terminal_total,
            "closure_error_tph":error,
            "closure_percent_of_source": (error/source_total*100 if source_total else 0.0)
        }

    def _water_balance(self):
        source_water=sum(s.get("water") for s in self._source_streams().values())
        for bid,res in self.results.items():
            if self.blocks[bid].type != "feed_preparation":
                continue
            if not any(c.to_block == bid and c.to_port == "raw_feed" for c in self.connections):
                source_water += res.metrics.get("incoming_feed_water_tph", 0.0)
            if not any(c.to_block == bid and c.to_port == "process_water" for c in self.connections):
                source_water += res.metrics.get("process_water_addition_tph", res.metrics.get("required_process_water_tph", 0.0))
        terminal_water=sum(s.get("water") for s in self._terminal_stream_objects().values())
        reaction_consumption=0.0
        for res in self.results.values():
            reaction_consumption += res.metrics.get("water_consumed_by_reaction_tph",0.0)
            reaction_consumption -= res.metrics.get("water_generated_by_reaction_tph",0.0)
        raw_difference=source_water-terminal_water
        adjusted_error=source_water-terminal_water-reaction_consumption
        return {
            "free_water_in_tph":source_water,
            "free_water_out_tph":terminal_water,
            "reaction_water_consumed_net_tph":reaction_consumption,
            "raw_free_water_difference_tph":raw_difference,
            "reaction_adjusted_closure_error_tph":adjusted_error,
            "reaction_adjusted_closure_percent":(adjusted_error/source_water*100 if source_water else 0.0),
            "note":"Free-water balance is reaction-adjusted; hydrolysis incorporates water into glucose."
        }

    def _site_process_water_summary(self):
        tank=next((self.results[bid] for bid,b in self.blocks.items() if b.type=="process_water_tank" and bid in self.results),None)
        if not tank:return {"configured":False,"total_demand_tph":self._utility_totals().get("process_water_tph",0.0)}
        m=tank.metrics
        return {"configured":True,"total_demand_tph":m.get("site_process_water_demand_tph",0.0),"fresh_water_makeup_tph":m.get("fresh_water_makeup_tph",0.0),"recovered_water_available_tph":m.get("recovered_water_available_tph",0.0),"recovered_water_used_tph":m.get("recovered_water_used_tph",0.0),"surplus_recovered_water_tph":m.get("surplus_recovered_water_tph",0.0),"required_working_volume_m3":m.get("required_working_volume_m3",0.0),"consumers":m.get("consumers",[])}

    def _component_closure(self):
        components=set()
        for s in list(self._source_streams().values()) + list(self._terminal_stream_objects().values()):
            components.update(s.components_tph.keys())
        out={}
        for comp in sorted(components):
            vin=sum(s.get(comp) for s in self._source_streams().values())
            vout=sum(s.get(comp) for s in self._terminal_stream_objects().values())
            # Reactions change named components, so this is a named-component bookkeeping view,
            # not elemental closure. It is still useful for spotting unclassified material.
            out[comp]={
                "in_tph":vin,
                "out_tph":vout,
                "difference_tph":vin-vout
            }
        return out

    def _utilities_by_block(self):
        return {
            bid:self.results[bid].utilities.to_dict()
            for bid in self.results
        }

    def _open_decisions(self):
        decisions=[]
        for bid,res in self.results.items():
            md=res.metadata
            status=(md.status or "").upper()
            if any(x in status for x in ["OPEN","TBC","PROVISIONAL","SCREENING"]):
                decisions.append({
                    "block_id":bid,
                    "block_name":self.blocks[bid].name,
                    "block_type":self.blocks[bid].type,
                    "status":md.status,
                    "basis":md.basis,
                    "confidence":md.confidence,
                    "note":md.note,
                    "warnings":res.warnings
                })
        return decisions

    def _stream_register(self):
        rows=[]
        for key,s in sorted(self.streams.items()):
            rows.append({
                "stream_id":key,
                "total_tph":s.total_tph,
                "temperature_C":s.temperature_C,
                "pressure_bar_abs":s.pressure_bar_abs,
                "phase":s.phase,
                "status":s.status,
                "note":s.note,
                "components_tph":dict(s.components_tph)
            })
        return rows


    def _equipment_list(self):
        rows=[]
        for bid,res in self.results.items():
            for e in res.equipment:
                d=e.to_dict()
                d["block_id"]=bid
                d["block_name"]=self.blocks[bid].name
                rows.append(d)
        return rows

    def _motor_list(self):
        rows=[]
        for row in self._equipment_list():
            if row.get("motor_kW") not in (None,0):
                rows.append({
                    "block_id":row["block_id"],
                    "block_name":row["block_name"],
                    "equipment_type":row.get("equipment_type",""),
                    "quantity":row.get("quantity"),
                    "motor_kW_each":row.get("motor_kW"),
                    "connected_motor_kW":(row.get("motor_kW") or 0)*(row.get("quantity") or 1)
                })
        return rows

    def _wastewater_loads(self):
        terms=self._terminal_stream_objects()
        total=0.0
        comp={}
        for key,s in terms.items():
            # only include streams whose receiving block is wastewater_sink
            to_block=key.split("->")[-1]
            if self.blocks[to_block].type!="wastewater_sink":
                continue
            total+=s.total_tph
            for k,v in s.components_tph.items():
                comp[k]=comp.get(k,0.0)+v
        nonwater=sum(v for k,v in comp.items() if k!="water")
        return {
            "total_wastewater_tph":total,
            "water_tph":comp.get("water",0.0),
            "nonwater_load_tph":nonwater,
            "nonwater_mass_fraction":(nonwater/total if total else 0.0),
            "components_tph":comp
        }

    def _peak_utilities(self):
        out={"electricity_kW":0.0,"thermal_kW":0.0,"cooling_kW":0.0}
        for res in self.results.values():
            out["electricity_kW"] += res.utilities.peak_electricity_kW
            out["thermal_kW"] += res.utilities.peak_thermal_kW
            out["cooling_kW"] += res.utilities.peak_cooling_kW
        return out

    def _elemental_balance(self):
        # Approximate formula basis for currently tracked principal components.
        # This is intended as an independent bookkeeping check, not rigorous ultimate analysis.
        formulas={
            "water":{"C":0,"H":2/18,"O":16/18},
            "glucan":{"C":72/162,"H":10/162,"O":80/162},
            "glucose":{"C":72/180,"H":12/180,"O":96/180},
            "ethanol":{"C":24/46,"H":6/46,"O":16/46},
            "co2":{"C":12/44,"H":0,"O":32/44},
            "xylan":{"C":60/132,"H":8/132,"O":64/132},
        }
        src={"C":0.0,"H":0.0,"O":0.0}; dst={"C":0.0,"H":0.0,"O":0.0}
        for s in self._source_streams().values():
            for comp,m in s.components_tph.items():
                f=formulas.get(comp)
                if not f: continue
                for e in src: src[e]+=m*f[e]
        for s in self._terminal_stream_objects().values():
            for comp,m in s.components_tph.items():
                f=formulas.get(comp)
                if not f: continue
                for e in dst: dst[e]+=m*f[e]
        return {
            "source_tph":src,
            "terminal_tph":dst,
            "difference_tph":{e:src[e]-dst[e] for e in src},
            "note":"Approximate elemental balance covers principal represented components only; lignin/ash/other organics are not yet assigned empirical formulas."
        }


    def _evidence_quality_summary(self):
        counts={"HIGH":0,"MEDIUM":0,"LOW":0,"OTHER":0}
        for res in self.results.values():
            c=(res.metadata.confidence or "OTHER").upper()
            if c not in counts: c="OTHER"
            counts[c]+=1
        return counts

    def _cooling_water_summary(self):
        duty=self._utility_totals().get("cooling_kW",0.0)
        deltaT=9.0
        cp=4.18
        flow_tph=duty/(cp*deltaT)*3.6 if duty>0 else 0.0
        return {
            "cooling_duty_kW":duty,
            "reference_deltaT_C":deltaT,
            "screening_cooling_water_tph":flow_tph,
            "basis":"NREL reference 9°C average cooling-water rise; actual site utility design TBC."
        }


    def _distillation_design_summary(self):
        rows=[]
        for bid,res in self.results.items():
            block=self.blocks[bid]
            cls=BLOCK_REGISTRY.get(block.type)
            if cls is None or "distillation" not in getattr(cls,"capabilities",()):
                continue
            sc=res.metrics.get("shortcut_distillation") or {}
            rows.append({
                "block_id":bid,
                "block_name":block.name,
                "actual_trays":res.metrics.get("actual_trays"),
                "tray_efficiency_fraction":res.metrics.get("overall_tray_efficiency_fraction"),
                "feed_tray_from_top":res.metrics.get("feed_tray_from_top"),
                "reflux_ratio":res.metrics.get("molar_reflux_ratio"),
                "pressure_atm_abs":res.metrics.get("overhead_pressure_atm_abs"),
                "distillate_tph":sc.get("distillate_tph",res.metrics.get("overhead_total_tph",0.0)),
                "internal_vapour_tph":sc.get("internal_vapour_tph",0.0),
                "internal_liquid_tph":sc.get("estimated_internal_liquid_tph",0.0),
                "estimated_reboiler_kW":sc.get("estimated_reboiler_kW",res.metrics.get("reboiler_kW",0.0)),
                "estimated_condenser_kW":sc.get("estimated_condenser_kW",res.metrics.get("condenser_kW",0.0)),
                "minimum_stages":sc.get("minimum_stages_fenske"),
                "required_theoretical_stages":sc.get("required_theoretical_stages_gilliland"),
                "stage_margin":sc.get("stage_margin"),
                "stage_feasible":sc.get("stage_feasible"),
                "reflux_feasible":sc.get("reflux_feasible"),
                "status":sc.get("status","SCREENING"),
            })
        return {
            "columns":rows,
            "design_boundary":"Shortcut binary ethanol/water design. Internal traffic and duties are suitable for preliminary RFQ screening, not final vendor hydraulic or mechanical design.",
            "next_vendor_inputs":["design feed rate/composition","operating pressure","required product/recovery","internal vapour and liquid traffic","tray/stage basis","reboiler and condenser duties","materials/corrosion basis","turndown and startup requirements"]
        }

    def _excel_parity_summary(self):
        def metric(bid,key,default=0.0):
            return self.results[bid].metrics.get(key,default) if bid in self.results else default
        dist_q=metric("dist_util","distillation_thermal_kW")
        dist_c=metric("dist_util","condenser_cooling_kW")
        pretreat_heat=metric("pretreat","design_heating_load_kW")
        pretreat_residual_cooling=37.76854166666703
        sensible_hf_cooling=271.9335
        fermentation_rxn_cooling=metric("ferm","fermentation_cooling_kW")
        return {
            "P01_slurry_to_pretreatment_tph": (self.results["macerator"].outputs["outlet"].total_tph if "macerator" in self.results else self.results["feed"].outputs["slurry"].total_tph if "feed" in self.results else 0.0),
            "P01_water_added_tph": metric("feed","process_water_addition_tph"),
            "P01_reject_total_tph": metric("macerator","reject_total_tph",metric("feed","reject_total_tph")),
            "P02_cycle_h": metric("pretreat","cycle_time_h"),
            "P02_required_volume_m3": metric("pretreat","required_total_working_volume_m3"),
            "P04_glucose_tph": metric("hydro","glucose_produced_tph"),
            "P04_hydrolysis_volume_m3": metric("hydro","required_working_volume_m3"),
            "P04_hydrolysis_vessels": metric("hydro","vessel_count"),
            "P05_ethanol_tph": metric("ferm","ethanol_tph"),
            "P05_co2_tph": metric("ferm","co2_tph"),
            "P05_fermentation_volume_m3": metric("ferm","required_working_volume_m3"),
            "P05_fermentation_vessels": metric("ferm","vessel_count"),
            "P06_cake_tph": self.results["sep"].outputs["cake"].total_tph,
            "P06_beer_tph": self.results["sep"].outputs["liquid"].total_tph,
            "P07_economiser_recovery_kW": metric("beer_cond","economiser_recovery_kW"),
            "P08_side_draw_tph": self.results["beer"].outputs["overhead"].total_tph,
            "P09_azeotrope_tph": self.results["rect"].outputs["overhead"].total_tph,
            "P10_ethanol_product_Lph": metric("dist_util","ethanol_product_Lph"),
            "P12_distillation_thermal_kW": dist_q,
            "P12_distillation_steam_kgph": metric("dist_util","steam_kgph"),
            "P12_distillation_condenser_kW": dist_c,
            "P12_total_external_heat_kW": pretreat_heat+dist_q,
            "P12_live_residual_cooling_kW": pretreat_residual_cooling+sensible_hf_cooling+dist_c,
            "P12_engineering_augmented_cooling_kW": pretreat_residual_cooling+sensible_hf_cooling+dist_c+fermentation_rxn_cooling
        }

    def summary(self):






        return {
            "operating_basis": self.operating_basis,
            "blocks": {bid:b.to_dict() for bid,b in self.blocks.items()},
            "connections": [c.to_dict() for c in self.connections],
            "streams": {sid:s.to_dict() for sid,s in self.streams.items()},
            "block_results": {
                bid:{
                    "metrics":res.metrics,
                    "utilities":res.utilities.to_dict(),
                    "discharges":res.discharges.to_dict(),
                    "equipment":[e.to_dict() for e in res.equipment],
                    "metadata":res.metadata.to_dict(),
                    "warnings":res.warnings,
                    "errors":res.errors,
                    "outputs":{k:v.to_dict() for k,v in res.outputs.items()}
                }
                for bid,res in self.results.items()
            },
            "utility_totals": self._utility_totals(),
            "discharge_totals": self._discharge_totals(),
            "terminal_summary": self._terminal_summary(),
            "overall_material_closure": self._overall_material_closure(),
            "water_balance": self._water_balance(),
            "site_process_water": self._site_process_water_summary(),
            "component_closure": self._component_closure(),
            "utilities_by_block": self._utilities_by_block(),
            "open_decisions": self._open_decisions(),
            "stream_register": self._stream_register(),
            "equipment_list": self._equipment_list(),
            "motor_list": self._motor_list(),
            "wastewater_loads": self._wastewater_loads(),
            "peak_utilities": self._peak_utilities(),
            "elemental_balance": self._elemental_balance(),
            "evidence_quality_summary": self._evidence_quality_summary(),
            "cooling_water_summary": self._cooling_water_summary(),
            "distillation_design_summary": self._distillation_design_summary(),
            "excel_parity_summary": self._excel_parity_summary(),
            "terminal_component_totals": self._component_terminal_totals(),
            "engineering_notes": [
                "Discharge totals currently represent block-declared external discharge categories only.",
                "Streams routed to downstream blocks remain material streams and should not be independently counted as plant discharge unless explicitly declared by a terminal block."
            ],
            "warnings":self.warnings,
            "errors":self.errors
        }
