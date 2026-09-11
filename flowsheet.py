
from __future__ import annotations
from collections import defaultdict, deque
from typing import Dict, Any
from models import Connection, BlockInstance, Stream
from blocks import BLOCK_REGISTRY

class FlowsheetError(Exception):
    pass

class Flowsheet:
    def __init__(self, definition: dict):
        self.definition = definition
        self.blocks = {
            b["id"]: BlockInstance(
                id=b["id"],
                type=b["type"],
                name=b.get("name",b["id"]),
                params=b.get("params",{}),
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

        for bid in order:
            bdef = self.blocks[bid]
            cls = BLOCK_REGISTRY[bdef.type]
            block = cls(bid,bdef.params)
            self.instances[bid] = block

            result = block.calculate(incoming_by_block[bid])
            self.results[bid] = result

            self.warnings.extend([f"{bid}: {w}" for w in result.warnings])
            self.errors.extend([f"{bid}: {e}" for e in result.errors])

            if result.errors:
                continue

            for port, stream in result.outputs.items():
                key = f"{bid}.{port}"
                self.streams[key] = stream

            for c in connections_from[bid]:
                if c.from_port not in result.outputs:
                    self.errors.append(f"{bid}: output '{c.from_port}' was not produced.")
                    continue
                incoming_by_block[c.to_block][c.to_port] = result.outputs[c.from_port].copy(
                    new_id=f"{c.id}"
                )

        return self.summary()


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
            source_total += res.metrics.get("as_received_feed_tph", 0.0)
            if not any(c.to_block == bid and c.to_port == "process_water" for c in self.connections):
                source_total += res.metrics.get("process_water_addition_tph", 0.0)
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
            source_water += res.metrics.get("incoming_feed_water_tph", 0.0)
            if not any(c.to_block == bid and c.to_port == "process_water" for c in self.connections):
                source_water += res.metrics.get("process_water_addition_tph", 0.0)
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
            "P01_slurry_to_pretreatment_tph": self.results["feed"].outputs["slurry"].total_tph,
            "P01_water_added_tph": metric("feed","process_water_addition_tph"),
            "P01_reject_total_tph": metric("feed","reject_total_tph"),
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
            "excel_parity_summary": self._excel_parity_summary(),
            "terminal_component_totals": self._component_terminal_totals(),
            "engineering_notes": [
                "Discharge totals currently represent block-declared external discharge categories only.",
                "Streams routed to downstream blocks remain material streams and should not be independently counted as plant discharge unless explicitly declared by a terminal block."
            ],
            "warnings":self.warnings,
            "errors":self.errors
        }
