
from __future__ import annotations
import math
from typing import Dict, Type
from models import Stream, PortSpec, BlockResult, UtilityDemand, Discharge, EquipmentRequirement, EngineeringMetadata
from distillation_shortcut import shortcut_column

class BaseBlock:
    type_name = "base"
    display_name = "Base Block"
    input_ports: Dict[str, PortSpec] = {}
    output_ports: Dict[str, PortSpec] = {}
    default_params: dict = {}

    def __init__(self, block_id: str, params: dict | None = None):
        self.id = block_id
        self.params = params or {}

    def validate_inputs(self, inputs: Dict[str, Stream]) -> list[str]:
        errors = []
        for name, spec in self.input_ports.items():
            if spec.required and name not in inputs:
                errors.append(f"Missing required input port '{name}'")
        return errors

    def calculate(self, inputs: Dict[str, Stream]) -> BlockResult:
        raise NotImplementedError

    def schema(self):
        return {
            "type": self.type_name,
            "display_name": self.display_name,
            "input_ports": {k: vars(v) for k,v in self.input_ports.items()},
            "output_ports": {k: vars(v) for k,v in self.output_ports.items()},
            "default_params": dict(self.default_params),
        }

class FeedPreparationBlock(BaseBlock):
    type_name = "feed_preparation"
    display_name = "Feed Preparation"
    input_ports = {
        "process_water": PortSpec("process_water", "in", required=False, description="Fresh or recovered process water")
    }
    output_ports = {
        "slurry": PortSpec("slurry", "out", description="Prepared biomass slurry"),
        "rejects": PortSpec("rejects", "out", required=False, description="Maceration/grit rejects")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        p = self.params
        dry = p.get("dry_miscanthus_tph", 3.0)
        moisture = p.get("incoming_moisture_wet_fraction", 0.15)
        target_dm = p.get("target_slurry_dry_matter_fraction", 0.20)
        reject_frac = p.get("maceration_grit_reject_fraction", 0.005)
        comp = p.get("dry_feed_composition", {
            "glucan": 0.4287, "xylan": 0.2202, "lignin": 0.1967, "ash": 0.0233, "other_organics": 0.1311
        })
        specific_drive_kWh_per_t_dry = p.get("specific_drive_kWh_per_t_dry", 12.0)
        status = p.get("drive_basis_status","PROVISIONAL ENGINEERING ASSUMPTION")

        if not (0 <= moisture < 1):
            return BlockResult({}, errors=["Incoming moisture fraction must be between 0 and 1."])
        if target_dm <= 0 or target_dm >= 1:
            return BlockResult({}, errors=["Target slurry dry matter must be between 0 and 1."])

        as_received = dry/(1-moisture)
        incoming_water = as_received-dry
        has_explicit_water = "process_water" in inputs
        supplied_process_water = inputs["process_water"].get("water") if has_explicit_water else None
        excel_parity_mode = p.get("excel_parity_mode", False)

        if excel_parity_mode:
            gross_slurry_total = dry/target_dm
            required_process_water = max(0.0, gross_slurry_total-as_received)
            process_water = required_process_water if supplied_process_water is None else supplied_process_water
            gross_water = incoming_water+process_water

            reject_total = gross_slurry_total*reject_frac
            rejected_dry = dry*reject_frac
            rejected_water = reject_total-rejected_dry
            retained_dry = dry-rejected_dry
            slurry_total = gross_slurry_total-reject_total
            water_required_total = gross_water-rejected_water
        else:
            retained_dry = dry*(1-reject_frac)
            rejected_dry = dry-retained_dry
            rejected_water = 0.0
            required_slurry_total = retained_dry/target_dm
            required_water_total = required_slurry_total-retained_dry
            required_process_water = max(0.0, required_water_total-incoming_water)
            process_water = required_process_water if supplied_process_water is None else supplied_process_water
            water_required_total = incoming_water+process_water
            slurry_total = retained_dry+water_required_total

        slurry_comp = {
            "water": water_required_total,
            "glucan": retained_dry*comp["glucan"],
            "xylan": retained_dry*comp["xylan"],
            "lignin": retained_dry*comp["lignin"],
            "ash": retained_dry*comp["ash"],
            "other_organics": retained_dry*comp["other_organics"],
        }
        reject_comp = {
            "water": rejected_water,
            "glucan": rejected_dry*comp["glucan"],
            "xylan": rejected_dry*comp["xylan"],
            "lignin": rejected_dry*comp["lignin"],
            "ash": rejected_dry*comp["ash"],
            "other_organics": rejected_dry*comp["other_organics"],
        }

        water_temperature = inputs["process_water"].temperature_C if has_explicit_water else p.get("process_water_temperature_C",15.0)
        slurry = Stream(f"{self.id}:slurry", slurry_comp, temperature_C=water_temperature, note="Prepared Miscanthus slurry", density_kg_per_m3=p.get("slurry_density_kg_per_m3",1000.0))
        actual_dm = retained_dry/slurry.total_tph if slurry.total_tph else 0.0
        rejects = Stream(f"{self.id}:rejects", reject_comp, phase="solid", note="Maceration/grit reject")

        drive_kW = dry*specific_drive_kWh_per_t_dry
        closure = (as_received + process_water) - (slurry.total_tph + rejects.total_tph)
        warnings = []
        if abs(process_water-required_process_water) > 1e-6:
            warnings.append(
                f"Connected process water is {process_water:.4f} t/h; {required_process_water:.4f} t/h is required for the selected dry-matter target. Actual slurry dry matter is {actual_dm:.4f}."
            )

        return BlockResult(
            {"slurry": slurry, "rejects": rejects},
            metrics={
                "as_received_feed_tph": as_received,
                "incoming_feed_water_tph": incoming_water,
                "process_water_addition_tph": process_water,
                "required_process_water_tph": required_process_water,
                "actual_slurry_dry_matter_fraction": actual_dm,
                "reject_dry_tph": rejected_dry,
                "reject_water_tph": rejected_water,
                "reject_total_tph": rejects.total_tph,
                "slurry_tph": slurry.total_tph,
                "closure_error_tph": closure
            },
            utilities=UtilityDemand(
                electricity_kW=drive_kW,
                peak_electricity_kW=drive_kW,
                process_water_tph=0.0 if has_explicit_water else process_water
            ),
            discharges=Discharge(
                solid_waste_tph=rejects.total_tph
            ),
            equipment=[
                EquipmentRequirement(
                    equipment_type="Feed preparation / size reduction",
                    design_flow_tph=as_received,
                    motor_kW=drive_kW,
                    note="Drive load is a provisional screening basis until selected equipment/vendor data are available."
                )
            ],
            metadata=EngineeringMetadata(
                status=status,
                basis="Project mass balance + provisional specific drive",
                confidence="MEDIUM",
                note="Process-water addition is calculated from feed moisture and slurry dry-matter target."
            ),
            warnings=warnings
        )

class WaterSupplyBlock(BaseBlock):
    type_name = "water_supply"
    display_name = "Process Water Supply"
    input_ports = {}
    output_ports = {"water": PortSpec("water", "out", description="Process water")}
    default_params = {"flow_tph": 11.4705882353, "temperature_C": 15.0, "pressure_bar_abs": 2.0, "density_kg_per_m3": 999.0}

    def calculate(self, inputs):
        p = {**self.default_params, **self.params}
        flow = p["flow_tph"]
        density = p["density_kg_per_m3"]
        if flow < 0 or density <= 0:
            return BlockResult({}, errors=["Water flow must be non-negative and density must be positive."])
        water = Stream(f"{self.id}:water", {"water": flow}, temperature_C=p["temperature_C"],
                       pressure_bar_abs=p["pressure_bar_abs"], phase="liquid", density_kg_per_m3=density,
                       note="Explicit process-water input")
        return BlockResult(
            {"water": water},
            metrics={"water_tph": flow, "water_m3ph": water.volumetric_flow_m3ph},
            utilities=UtilityDemand(process_water_tph=flow),
            metadata=EngineeringMetadata(status="USER INPUT", basis="Connected process-water supply", confidence="HIGH")
        )

class PretreatmentBlock(BaseBlock):
    type_name = "pretreatment"
    display_name = "Pretreatment"
    default_params = {
        "inlet_transfer_pump_rate_m3ph": 15.0,
        "outlet_transfer_pump_rate_m3ph": 15.0,
        "inlet_transfer_pump_kW": 0.0,
        "outlet_transfer_pump_kW": 0.0,
    }
    input_ports = {
        "feed": PortSpec("feed","in",description="Biomass slurry"),
    }
    output_ports = {
        "slurry": PortSpec("slurry","out",description="Pretreated slurry"),
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)

        s = inputs["feed"]
        p = self.params
        r = p.get("retention_surrogates", {"glucan":0.967,"xylan":0.212,"lignin":0.804})
        gi, xi, li = s.get("glucan"), s.get("xylan"), s.get("lignin")
        gr, xr, lr = gi*r["glucan"], xi*r["xylan"], li*r["lignin"]

        comp = dict(s.components_tph)
        comp["glucan"] = gr
        comp["xylan"] = xr
        comp["lignin"] = lr
        comp["soluble_c6_pool"] = comp.get("soluble_c6_pool",0)+gi-gr
        comp["soluble_c5_pool"] = comp.get("soluble_c5_pool",0)+xi-xr
        comp["soluble_lignin_pool"] = comp.get("soluble_lignin_pool",0)+li-lr

        temp = p.get("temperature_C_placeholder",180.0)
        pressure = p.get("pressure_bar_abs_placeholder",10.0)
        inlet_temp = s.temperature_C if s.temperature_C is not None else p.get("assumed_inlet_temperature_C",10.0)
        cp = p.get("slurry_cp_kJ_per_kgK",3.644)
        heat_recovery_fraction = p.get("gross_heat_recovery_fraction",0.75)
        design_allowance = p.get("design_heat_allowance_fraction",0.15)
        useful_steam_enthalpy_kJ_per_kg = p.get("useful_steam_enthalpy_kJ_per_kg",2100.0)

        m_kg_s = s.total_tph*1000/3600
        gross_kW = max(0.0,m_kg_s*cp*(temp-inlet_temp))
        net_kW = gross_kW*(1-heat_recovery_fraction)
        design_kW = net_kW*(1+design_allowance)
        steam_kgph = design_kW*3600/useful_steam_enthalpy_kJ_per_kg if useful_steam_enthalpy_kJ_per_kg>0 else 0

        working_volume = p.get("reactor_working_volume_m3",50.0)
        density = p.get("slurry_density_t_per_m3",1.0)
        batch_mass = working_volume*density
        hold_h = p.get("hold_time_h",1.0)
        heatup_h = p.get("heat_up_time_h",1.5)
        turnaround_h = p.get("cip_turnaround_h",0.5)
        fill_h = batch_mass/s.total_tph if s.total_tph>0 else 0
        empty_h = fill_h
        include_heatup_in_cycle = p.get("include_heatup_in_cycle", True)
        cycle_h = fill_h+hold_h+empty_h+turnaround_h+(heatup_h if include_heatup_in_cycle else 0.0)
        total_working_required = s.total_tph/density*cycle_h if density>0 else 0
        reactor_count = max(1, int(total_working_required/working_volume + 0.999999)) if working_volume>0 else 0

        out = Stream(
            id=f"{self.id}:slurry",
            components_tph=comp,
            temperature_C=temp,
            pressure_bar_abs=pressure,
            status="PROVISIONAL",
            note="Commercial pretreatment route unresolved; surrogate conversion model in use."
        )

        warnings = [
            "Pretreatment chemistry and selected commercial operating conditions remain unresolved.",
            "Steam/heat demand is a screening sensible-heat model using provisional temperature and heat-recovery assumptions."
        ]

        return BlockResult(
            {"slurry":out},
            metrics={
                "gross_sensible_heat_kW":gross_kW,
                "net_external_heat_kW":net_kW,
                "design_heating_load_kW":design_kW,
                "steam_kgph":steam_kgph,
                "cycle_time_h":cycle_h,
                "required_total_working_volume_m3":total_working_required,
                "provisional_reactor_count":reactor_count,
                "closure_error_tph":_closure_error(inputs,{"slurry":out})
            },
            utilities=UtilityDemand(
                thermal_kW=design_kW,
                peak_thermal_kW=design_kW,
                steam_kgph=steam_kgph
            ),
            equipment=[
                EquipmentRequirement(
                    equipment_type="Pretreatment reactor",
                    quantity=reactor_count,
                    working_volume_m3=working_volume,
                    design_flow_tph=s.total_tph,
                    design_duty_kW=design_kW,
                    residence_time_h=hold_h,
                    note="Provisional batch sizing only; reactor type/configuration not selected."
                )
            ],
            metadata=EngineeringMetadata(
                status="OPEN DESIGN DECISION",
                basis="Commercial pretreatment route TBC; current mass-conversion and heat model are surrogates",
                confidence="LOW",
                note="No chemical, neutralisation, flash loss or vent discharge is credited until route selection."
            ),
            warnings=warnings
        )

class HydrolysisBlock(BaseBlock):
    type_name = "hydrolysis"
    display_name = "Enzymatic Hydrolysis"
    default_params = {
        "inlet_transfer_pump_rate_m3ph": 15.0,
        "outlet_transfer_pump_rate_m3ph": 15.0,
        "inlet_transfer_pump_kW": 0.0,
        "outlet_transfer_pump_kW": 0.0,
    }
    input_ports = {
        "feed": PortSpec("feed","in",description="Pretreated biomass slurry")
    }
    output_ports = {
        "hydrolysate": PortSpec("hydrolysate","out",description="Hydrolysed slurry")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        s = inputs["feed"]
        if s.get("glucan") <= 0:
            return BlockResult({}, errors=["Hydrolysis feed contains no glucan."])

        p = self.params
        conv = p.get("glucan_to_glucose_conversion_fraction",0.7674)
        reacted = s.get("glucan")*conv
        glucose = reacted*180/162
        water_use = glucose-reacted

        comp = dict(s.components_tph)
        comp["glucan"] = s.get("glucan")-reacted
        comp["water"] = s.get("water")-water_use
        comp["glucose"] = comp.get("glucose",0)+glucose

        temp = p.get("temperature_C",50.0)
        residence_h = p.get("residence_time_h",72.0)
        density = p.get("slurry_density_t_per_m3",1.0)
        working_volume = s.total_tph*residence_h/density if density>0 else 0
        vessel_working_volume=p.get("vessel_working_volume_m3",100.0)
        availability=p.get("availability_fraction",0.90)
        turnaround_h=p.get("turnaround_h",2.0)
        required_volume_with_availability=working_volume/availability if availability>0 else working_volume
        vessel_count=max(1,math.ceil(required_volume_with_availability/vessel_working_volume)) if vessel_working_volume>0 else 0

        # Provisional agitation envelope. Default deliberately editable and labelled.
        specific_agitation_kW_per_m3 = p.get("specific_agitation_kW_per_m3",0.35)
        agitator_kW = working_volume*specific_agitation_kW_per_m3

        # Holding duty is not yet calculated rigorously; optional design loss can be enabled.
        heat_loss_kW_per_m3 = p.get("heat_loss_kW_per_m3",0.0)
        hold_heat_kW = working_volume*heat_loss_kW_per_m3

        out = Stream(
            id=f"{self.id}:hydrolysate",
            components_tph=comp,
            temperature_C=temp,
            note="Hydrolysed slurry"
        )
        return BlockResult(
            {"hydrolysate":out},
            metrics={
                "glucan_reacted_tph": reacted,
                "glucose_produced_tph": glucose,
                "water_consumed_by_reaction_tph": water_use,
                "residual_glucan_tph":comp["glucan"],
                "required_working_volume_m3":working_volume,
                "required_volume_with_availability_m3":required_volume_with_availability,
                "vessel_count":vessel_count,
                "turnaround_h":turnaround_h,
                "agitation_load_kW":agitator_kW,
                "holding_heat_kW":hold_heat_kW,
                "closure_error_tph":_closure_error(inputs,{"hydrolysate":out})
            },
            utilities=UtilityDemand(
                electricity_kW=agitator_kW,
                peak_electricity_kW=agitator_kW,
                thermal_kW=hold_heat_kW,
                peak_thermal_kW=hold_heat_kW
            ),
            equipment=[
                EquipmentRequirement(
                    equipment_type="Hydrolysis reactor",
                    quantity=vessel_count,
                    working_volume_m3=vessel_working_volume,
                    design_flow_tph=s.total_tph,
                    motor_kW=agitator_kW,
                    residence_time_h=residence_h,
                    note="Total required working volume before batching/availability margin."
                )
            ],
            metadata=EngineeringMetadata(
                status="LITERATURE BASIS / TRIAL-INFORMED FUTURE INPUT",
                basis="Current conversion and residence-time reference model",
                confidence="MEDIUM",
                note="Agitation is a provisional power-density assumption. Enzyme dose and nutrient additions are not yet populated."
            ),
            warnings=["Hydrolysis agitation load is provisional until rheology and mixing design are established."]
        )

class FermentationBlock(BaseBlock):
    type_name = "fermentation"
    display_name = "Fermentation"
    default_params = {
        "inlet_transfer_pump_rate_m3ph": 15.0,
        "outlet_transfer_pump_rate_m3ph": 15.0,
        "inlet_transfer_pump_kW": 0.0,
        "outlet_transfer_pump_kW": 0.0,
    }
    input_ports = {
        "feed": PortSpec("feed","in",description="Fermentable hydrolysate")
    }
    output_ports = {
        "broth": PortSpec("broth","out",description="Fermentation broth"),
        "co2": PortSpec("co2","out",description="Fermentation CO2")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        s = inputs["feed"]
        glucose = s.get("glucose")
        if glucose <= 0:
            return BlockResult({}, errors=["Fermentation feed contains no glucose."])

        p = self.params
        yield_frac = p.get("fraction_theoretical_ethanol_yield",0.95)
        theo = p.get("theoretical_ethanol_per_glucose_mass",92/180)
        co2_ratio = p.get("co2_to_ethanol_mass_ratio",88/92)
        temp = p.get("temperature_C",32.0)
        residence_h = p.get("residence_time_h",48.0)
        density = p.get("broth_density_t_per_m3",1.0)

        ethanol = glucose*theo*yield_frac
        co2 = ethanol*co2_ratio
        residual_glucose = glucose*(1-yield_frac)

        comp = dict(s.components_tph)
        comp["glucose"] = residual_glucose
        comp["ethanol"] = comp.get("ethanol",0)+ethanol

        broth = Stream(
            id=f"{self.id}:broth",
            components_tph=comp,
            temperature_C=temp,
            note="Fermentation broth after CO2 release"
        )
        gas = Stream(
            id=f"{self.id}:co2",
            components_tph={"co2":co2},
            temperature_C=temp,
            phase="gas",
            note="Fermentation CO2"
        )

        volume_basis = p.get("volume_basis","broth_outlet")
        volume_basis_flow_tph = s.total_tph if volume_basis=="inlet" else broth.total_tph
        volume = volume_basis_flow_tph*residence_h/density if density>0 else 0
        vessel_working_volume=p.get("vessel_working_volume_m3",100.0)
        availability=p.get("availability_fraction",0.90)
        turnaround_h=p.get("turnaround_h",2.0)
        required_volume_with_availability=volume/availability if availability>0 else volume
        vessel_count=max(1,math.ceil(required_volume_with_availability/vessel_working_volume)) if vessel_working_volume>0 else 0
        specific_agitation_kW_per_m3 = p.get("specific_agitation_kW_per_m3",0.10)
        agitator_kW = volume*specific_agitation_kW_per_m3

        # Optional fermentation cooling duty from heat-release assumption.
        heat_release_kJ_per_mol_glucose = p.get("fermentation_heat_release_kJ_per_mol_glucose",55.0)
        glucose_consumed_tph = glucose-residual_glucose
        glucose_kmolph = glucose_consumed_tph*1000/180.156
        cooling_kW = glucose_kmolph*heat_release_kJ_per_mol_glucose/3.6
        heat_release_kJ_per_kg_glucose = heat_release_kJ_per_mol_glucose/0.180156

        cip_water_fraction_of_working_volume_per_cycle = p.get("cip_water_fraction_of_working_volume_per_cycle",0.0)
        cip_water_per_cycle_t = volume*cip_water_fraction_of_working_volume_per_cycle
        cip_average_tph = cip_water_per_cycle_t/residence_h if residence_h>0 else 0

        outputs={"broth":broth,"co2":gas}
        return BlockResult(
            outputs,
            metrics={
                "ethanol_tph":ethanol,
                "co2_tph":co2,
                "residual_glucose_tph":residual_glucose,
                "required_working_volume_m3":volume,
                "volume_basis":volume_basis,
                "volume_basis_flow_tph":volume_basis_flow_tph,
                "required_volume_with_availability_m3":required_volume_with_availability,
                "vessel_count":vessel_count,
                "turnaround_h":turnaround_h,
                "agitation_load_kW":agitator_kW,
                "fermentation_cooling_kW":cooling_kW,
                "fermentation_heat_release_kJ_per_mol_glucose":heat_release_kJ_per_mol_glucose,
                "fermentation_heat_release_kJ_per_kg_glucose":heat_release_kJ_per_kg_glucose,
                "closure_error_tph":_closure_error(inputs,outputs)
            },
            utilities=UtilityDemand(
                electricity_kW=agitator_kW,
                peak_electricity_kW=agitator_kW,
                cooling_kW=cooling_kW,
                peak_cooling_kW=cooling_kW,
                cip_water_tph=cip_average_tph
            ),
            discharges=Discharge(
                vent_gas_tph=co2,
                wastewater_tph=cip_average_tph
            ),
            equipment=[
                EquipmentRequirement(
                    equipment_type="Fermenter",
                    quantity=vessel_count,
                    working_volume_m3=vessel_working_volume,
                    design_flow_tph=broth.total_tph,
                    motor_kW=agitator_kW,
                    residence_time_h=residence_h,
                    note="Total required working volume before batch staggering/availability margin."
                )
            ],
            metadata=EngineeringMetadata(
                status="LITERATURE BASIS / OPEN DETAIL DESIGN",
                basis="C6 fermentation stoichiometry + provisional equipment envelope",
                confidence="MEDIUM",
                note="CO2 is calculated stoichiometrically. Fermentation cooling uses a literature-bounded screening heat-release basis; CIP remains separately parameterised."
            ),
            warnings=[
                "Fermenter agitation is provisional.",
                "Fermentation heat release is a screening assumption; measured/pilot heat data should replace it.",
                "CO2 is shown as vent discharge by default; capture/purification remains a separate design decision."
            ]
        )

class SolidsSeparationBlock(BaseBlock):
    type_name = "solids_separation"
    display_name = "Solids Separation"
    input_ports = {
        "feed": PortSpec("feed","in",description="Slurry or broth containing insoluble solids"),
        "wash_water": PortSpec("wash_water","in",required=False,description="Optional cake wash water")
    }
    output_ports = {
        "liquid": PortSpec("liquid","out",description="Liquid-rich stream"),
        "cake": PortSpec("cake","out",description="Wet solids cake")
    }

    insoluble_keys = ("glucan","xylan","lignin","ash")

    def calculate(self, inputs):
        errors = []
        if "feed" not in inputs:
            errors.append("Missing required input port 'feed'")
        if errors:
            return BlockResult({},errors=errors)
        s = inputs["feed"]
        wash = inputs.get("wash_water")
        p = self.params
        cake_dm = p.get("cake_dry_matter_fraction",0.40)
        dissolved_entrainment_fraction = p.get("dissolved_solute_entrainment_fraction",0.0)

        if cake_dm <= 0 or cake_dm >= 1:
            return BlockResult({}, errors=["Cake dry matter fraction must be between 0 and 1."])
        if not 0 <= dissolved_entrainment_fraction <= 1:
            return BlockResult({}, errors=["Dissolved solute entrainment fraction must be between 0 and 1."])

        feed_comp = dict(s.components_tph)
        if wash:
            for k,v in wash.components_tph.items():
                feed_comp[k]=feed_comp.get(k,0)+v
        feed_total=sum(feed_comp.values())

        dry_comp = {k:feed_comp.get(k,0) for k in self.insoluble_keys}
        dry = sum(dry_comp.values())
        if dry <= 0:
            return BlockResult({}, errors=["Separation feed contains no modelled insoluble solids."])

        dissolved_keys=[k for k in feed_comp if k not in self.insoluble_keys and k!="water"]
        entrained_solutes={k:feed_comp.get(k,0)*dissolved_entrainment_fraction for k in dissolved_keys}
        entrained_solute_mass=sum(entrained_solutes.values())

        target_wet_mass = dry/cake_dm
        cake_liquid_total = target_wet_mass-dry
        cake_water = max(0.0,cake_liquid_total-entrained_solute_mass)
        if cake_water > feed_comp.get("water",0):
            return BlockResult({}, errors=["Insufficient water in feed to achieve selected cake dry matter/entrainment basis."])

        cake_comp=dict(dry_comp)
        cake_comp.update(entrained_solutes)
        cake_comp["water"]=cake_water

        liquid_comp=dict(feed_comp)
        for k in self.insoluble_keys:
            liquid_comp[k]=0.0
        for k,v in entrained_solutes.items():
            liquid_comp[k]=liquid_comp.get(k,0)-v
        liquid_comp["water"]=liquid_comp.get("water",0)-cake_water

        cake=Stream(f"{self.id}:cake",cake_comp,note="Wet solids cake")
        liquid=Stream(f"{self.id}:liquid",liquid_comp,temperature_C=s.temperature_C,note="Liquid-rich stream")
        outputs={"liquid":liquid,"cake":cake}

        specific_power_kWh_per_t_feed=p.get("specific_power_kWh_per_t_feed",1.5)
        electrical_kW=feed_total*specific_power_kWh_per_t_feed

        return BlockResult(
            outputs,
            metrics={
                "dry_residue_tph":dry,
                "wet_cake_tph":cake.total_tph,
                "entrained_dissolved_solutes_tph":entrained_solute_mass,
                "wash_water_tph":wash.total_tph if wash else 0.0,
                "electrical_load_kW":electrical_kW,
                "closure_error_tph":_closure_error(inputs,outputs)
            },
            utilities=UtilityDemand(
                electricity_kW=electrical_kW,
                peak_electricity_kW=electrical_kW,
                process_water_tph=wash.total_tph if wash else 0.0
            ),
            discharges=Discharge(
                solid_waste_tph=cake.total_tph
            ),
            equipment=[
                EquipmentRequirement(
                    equipment_type="Solid/liquid separator",
                    design_flow_tph=feed_total,
                    motor_kW=electrical_kW,
                    note="Separator type and location remain unselected."
                )
            ],
            metadata=EngineeringMetadata(
                status="OPEN DESIGN DECISION",
                basis="Reference dewatering calculation",
                confidence="LOW",
                note="Separation location and separator technology remain TBC. Solute entrainment defaults to zero until dewatering tests establish a basis."
            ),
            warnings=["Separator location and technology are not selected; electrical load is provisional."]
        )

class BeerColumnBlock(BaseBlock):
    type_name = "beer_column"
    display_name = "Beer Column"
    input_ports = {
        "feed": PortSpec("feed","in",description="Dilute ethanol-containing liquid")
    }
    output_ports = {
        "overhead": PortSpec("overhead","out",description="Ethanol-rich side draw"),
        "bottoms": PortSpec("bottoms","out",description="Ethanol-lean bottoms")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        s = inputs["feed"]
        etoh = s.get("ethanol")
        if etoh <= 0:
            return BlockResult({}, errors=["Beer column feed contains no ethanol."])

        p = self.params
        recovery = p.get("ethanol_recovery_fraction",0.992)
        wt = p.get("overhead_ethanol_wt_fraction",0.40)
        feed_temp = s.temperature_C if s.temperature_C is not None else p.get("feed_temperature_C",32.0)
        preheat_target = p.get("preheat_target_C",90.0)
        cp = p.get("feed_cp_kJ_per_kgK",4.0)
        economiser_fraction = p.get("feed_preheat_recovery_fraction",0.0)

        rec_etoh = etoh*recovery
        overhead_total = rec_etoh/wt
        overhead_water = overhead_total-rec_etoh
        if overhead_water > s.get("water"):
            return BlockResult({}, errors=["Selected beer-column overhead composition requires more water than available."])

        overhead = Stream(
            f"{self.id}:overhead",
            {"ethanol":rec_etoh,"water":overhead_water},
            phase="mixed",
            status="SCREENING",
            note="Screening beer-column ethanol-rich stream; not rigorous VLE."
        )
        bottoms_comp = dict(s.components_tph)
        bottoms_comp["ethanol"] = etoh-rec_etoh
        bottoms_comp["water"] = s.get("water")-overhead_water
        bottoms = Stream(
            f"{self.id}:bottoms",
            bottoms_comp,
            status="SCREENING",
            note="Beer-column bottoms screening mass balance."
        )

        m_kg_s=s.total_tph*1000/3600
        sensible_kW=max(0.0,m_kg_s*cp*(preheat_target-feed_temp))
        external_preheat_kW=sensible_kW*(1-economiser_fraction)

        # Reboiler basis is parameterised, not asserted as design truth.
        specific_reboiler_kWh_per_kg_ethanol = p.get("specific_reboiler_kWh_per_kg_ethanol",0.0)
        reboiler_kW = rec_etoh*1000*specific_reboiler_kWh_per_kg_ethanol
        useful_steam_enthalpy_kJ_per_kg = p.get("useful_steam_enthalpy_kJ_per_kg",2100.0)
        steam_kgph = reboiler_kW*3600/useful_steam_enthalpy_kJ_per_kg if useful_steam_enthalpy_kJ_per_kg>0 else 0

        condenser_fraction_of_reboiler = p.get("condenser_fraction_of_reboiler",0.0)
        condenser_kW = reboiler_kW*condenser_fraction_of_reboiler

        feed_wt = etoh/s.total_tph if s.total_tph else 0.0
        bottoms_wt = bottoms.get("ethanol")/bottoms.total_tph if bottoms.total_tph else 0.0
        shortcut = shortcut_column(
            feed_ethanol_wt_fraction=feed_wt,
            distillate_ethanol_wt_fraction=wt,
            bottoms_ethanol_wt_fraction=max(bottoms_wt,1e-9),
            pressure_bar_abs=p.get("overhead_pressure_atm_abs",2.0)*1.01325,
            actual_trays=p.get("actual_trays",32),
            tray_efficiency_fraction=p.get("overall_tray_efficiency_fraction",0.48),
            reflux_ratio=p.get("molar_reflux_ratio",3.0),
            distillate_tph=overhead.total_tph,
        )
        if specific_reboiler_kWh_per_kg_ethanol <= 0:
            reboiler_kW = shortcut.estimated_reboiler_kW
            condenser_kW = shortcut.estimated_condenser_kW
            steam_kgph = reboiler_kW*3600/useful_steam_enthalpy_kJ_per_kg if useful_steam_enthalpy_kJ_per_kg>0 else 0

        outputs={"overhead":overhead,"bottoms":bottoms}
        return BlockResult(
            outputs,
            metrics={
                "ethanol_recovered_tph":rec_etoh,
                "overhead_total_tph":overhead.total_tph,
                "bottoms_total_tph":bottoms.total_tph,
                "feed_preheat_kW":external_preheat_kW,
                "reboiler_kW":reboiler_kW,
                "condenser_kW":condenser_kW,
                "actual_trays":p.get("actual_trays"),
                "overall_tray_efficiency_fraction":p.get("overall_tray_efficiency_fraction"),
                "feed_tray_from_top":p.get("feed_tray_from_top"),
                "vapour_side_draw_tray_from_top":p.get("vapour_side_draw_tray_from_top"),
                "molar_reflux_ratio":p.get("molar_reflux_ratio"),
                "overhead_pressure_atm_abs":p.get("overhead_pressure_atm_abs"),
                "reboiler_type":p.get("reboiler_type"),
                "shortcut_distillation":shortcut.to_dict(),
                "closure_error_tph":_closure_error(inputs,outputs)
            },
            utilities=UtilityDemand(
                thermal_kW=external_preheat_kW+reboiler_kW,
                cooling_kW=condenser_kW,
                steam_kgph=steam_kgph
            ),
            discharges=Discharge(
                wastewater_tph=bottoms.total_tph
            ),
            equipment=[
                EquipmentRequirement(
                    equipment_type="Beer column",
                    design_flow_tph=s.total_tph,
                    design_duty_kW=reboiler_kW,
                    note="Mass recovery model active; thermal duties remain parameterised until rigorous distillation basis is selected."
                )
            ],
            metadata=EngineeringMetadata(
                status="MECHANISTIC SHORTCUT / WORKBOOK TARGET MASS BALANCE",
                basis="Workbook product/recovery targets checked with native binary Fenske-Underwood-Gilliland shortcut",
                confidence="MEDIUM",
                note="Mass split remains workbook-selected while tray/reflux feasibility and thermal traffic are independently calculated. Bottoms remain provisionally classified as wastewater."
            ),
            warnings=[
                "P08 mass split still follows the workbook target; shortcut thermodynamics are a feasibility/design screen, not a rigorous rate-based column simulation.",
                *(["Configured P08 tray/reflux design is below shortcut requirement."] if not shortcut.stage_feasible else [])
            ]
        )

class RectifierBlock(BaseBlock):
    type_name = "rectifier"
    display_name = "Rectifier"
    input_ports = {
        "feed": PortSpec("feed","in",description="Ethanol-rich column feed")
    }
    output_ports = {
        "overhead": PortSpec("overhead","out",description="Near-azeotropic ethanol"),
        "bottoms": PortSpec("bottoms","out",description="Rectifier bottoms")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        s = inputs["feed"]
        etoh = s.get("ethanol")
        if etoh <= 0:
            return BlockResult({}, errors=["Rectifier feed contains no ethanol."])

        p = self.params
        overhead_wt = p.get("overhead_ethanol_wt_fraction",0.925)
        etoh_recovery = p.get("ethanol_recovery_fraction",1.0)
        recovered_etoh=etoh*etoh_recovery
        overhead_total = recovered_etoh/overhead_wt
        water_needed = overhead_total-recovered_etoh
        if water_needed > s.get("water"):
            return BlockResult({}, errors=["Rectifier overhead composition requires more water than available."])

        overhead = Stream(
            f"{self.id}:overhead",
            {"ethanol":recovered_etoh,"water":water_needed},
            status="SCREENING",
            note="Near-azeotrope screening model."
        )
        bottoms_comp = dict(s.components_tph)
        bottoms_comp["ethanol"] = etoh-recovered_etoh
        bottoms_comp["water"] = s.get("water")-water_needed
        bottoms = Stream(
            f"{self.id}:bottoms",
            bottoms_comp,
            status="SCREENING",
            note="Rectifier screening bottoms."
        )

        specific_reboiler_kWh_per_kg_ethanol = p.get("specific_reboiler_kWh_per_kg_ethanol",0.0)
        reboiler_kW = recovered_etoh*1000*specific_reboiler_kWh_per_kg_ethanol
        useful_steam_enthalpy_kJ_per_kg = p.get("useful_steam_enthalpy_kJ_per_kg",2100.0)
        steam_kgph = reboiler_kW*3600/useful_steam_enthalpy_kJ_per_kg if useful_steam_enthalpy_kJ_per_kg>0 else 0
        condenser_fraction_of_reboiler = p.get("condenser_fraction_of_reboiler",0.0)
        condenser_kW = reboiler_kW*condenser_fraction_of_reboiler

        feed_wt = etoh/s.total_tph if s.total_tph else 0.0
        bottoms_wt = bottoms.get("ethanol")/bottoms.total_tph if bottoms.total_tph else p.get("bottoms_ethanol_wt_fraction_target",0.0005)
        shortcut = shortcut_column(
            feed_ethanol_wt_fraction=feed_wt,
            distillate_ethanol_wt_fraction=overhead_wt,
            bottoms_ethanol_wt_fraction=max(bottoms_wt,p.get("bottoms_ethanol_wt_fraction_target",0.0005),1e-9),
            pressure_bar_abs=p.get("overhead_pressure_bar_abs",1.01325),
            actual_trays=p.get("actual_trays",45),
            tray_efficiency_fraction=p.get("overall_tray_efficiency_fraction",0.76),
            reflux_ratio=p.get("molar_reflux_ratio",3.5),
            distillate_tph=overhead.total_tph,
        )
        if specific_reboiler_kWh_per_kg_ethanol <= 0:
            reboiler_kW = shortcut.estimated_reboiler_kW
            condenser_kW = shortcut.estimated_condenser_kW
            steam_kgph = reboiler_kW*3600/useful_steam_enthalpy_kJ_per_kg if useful_steam_enthalpy_kJ_per_kg>0 else 0

        outputs={"overhead":overhead,"bottoms":bottoms}
        return BlockResult(
            outputs,
            metrics={
                "overhead_total_tph":overhead.total_tph,
                "recovered_ethanol_tph":recovered_etoh,
                "bottoms_total_tph":bottoms.total_tph,
                "reboiler_kW":reboiler_kW,
                "condenser_kW":condenser_kW,
                "actual_trays":p.get("actual_trays"),
                "overall_tray_efficiency_fraction":p.get("overall_tray_efficiency_fraction"),
                "beer_side_draw_feed_tray_from_top":p.get("beer_side_draw_feed_tray_from_top"),
                "molecular_sieve_recycle_tray_from_top":p.get("molecular_sieve_recycle_tray_from_top"),
                "molar_reflux_ratio":p.get("molar_reflux_ratio"),
                "bottoms_ethanol_wt_fraction_target":p.get("bottoms_ethanol_wt_fraction_target"),
                "shortcut_distillation":shortcut.to_dict(),
                "closure_error_tph":_closure_error(inputs,outputs)
            },
            utilities=UtilityDemand(
                thermal_kW=reboiler_kW,
                cooling_kW=condenser_kW,
                steam_kgph=steam_kgph
            ),
            discharges=Discharge(
                wastewater_tph=bottoms.total_tph
            ),
            equipment=[
                EquipmentRequirement(
                    equipment_type="Rectification column",
                    design_flow_tph=s.total_tph,
                    design_duty_kW=reboiler_kW,
                    note="Screening mass model; rigorous stage/VLE design deferred."
                )
            ],
            metadata=EngineeringMetadata(
                status="MECHANISTIC SHORTCUT / WORKBOOK TARGET MASS BALANCE",
                basis="Workbook rectifier target checked with native binary Fenske-Underwood-Gilliland shortcut",
                confidence="MEDIUM",
                note="P09 workbook overhead and bottoms targets remain the mass-balance basis while stage/reflux feasibility and internal vapour-duty estimates are calculated independently."
            ),
            warnings=[
                "P09 shortcut model is binary ethanol/water and does not replace a rigorous activity-coefficient or rate-based column model.",
                *(["Configured P09 tray/reflux design is below shortcut requirement."] if not shortcut.stage_feasible else [])
            ]
        )

class MolecularSieveBlock(BaseBlock):
    type_name = "molecular_sieve"
    display_name = "Molecular Sieve"
    input_ports = {
        "feed": PortSpec("feed","in",description="Near-azeotropic ethanol feed")
    }
    output_ports = {
        "product": PortSpec("product","out",description="Dry ethanol product"),
        "recycle": PortSpec("recycle","out",description="Regeneration recycle")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        s = inputs["feed"]
        etoh = s.get("ethanol")
        if etoh <= 0:
            return BlockResult({}, errors=["Molecular sieve feed contains no ethanol."])

        p = self.params
        product_wt = p.get("product_ethanol_wt_fraction",0.995)
        recycle_wt = p.get("regeneration_recycle_ethanol_wt_fraction")
        product_recovery = p.get("ethanol_product_recovery_fraction",1.0)

        if recycle_wt is not None:
            recycle_wt=float(recycle_wt)
            if not (0.0 <= recycle_wt < product_wt <= 1.0):
                return BlockResult({}, errors=["Molecular-sieve product/recycle ethanol fractions are invalid."])
            total=s.total_tph
            product_total=(etoh-recycle_wt*total)/(product_wt-recycle_wt)
            if product_total < -1e-9 or product_total > total+1e-9:
                return BlockResult({}, errors=["Molecular-sieve product and recycle compositions cannot close on the current feed."])
            product_total=max(0.0,min(total,product_total))
            recycle_total=total-product_total
            product_etoh=product_total*product_wt
            recycle_etoh=recycle_total*recycle_wt
            product_water=product_total-product_etoh
            recycle_water=recycle_total-recycle_etoh
        else:
            product_etoh=etoh*product_recovery
            recycle_etoh=etoh-product_etoh
            product_total = product_etoh/product_wt
            product_water = product_total-product_etoh
            if product_water > s.get("water"):
                return BlockResult({}, errors=["Feed does not contain sufficient water for target product definition."])
            recycle_water = s.get("water")-product_water

        product = Stream(
            f"{self.id}:product",
            {"ethanol":product_etoh,"water":product_water},
            status="SCREENING",
            note="Dry ethanol product screening model."
        )
        recycle = Stream(
            f"{self.id}:recycle",
            {"ethanol":recycle_etoh,"water":recycle_water},
            status="SCREENING",
            note="Regeneration recycle placeholder."
        )

        specific_regeneration_kWh_per_kg_product = p.get("specific_regeneration_kWh_per_kg_product",0.0)
        regeneration_kW = product.total_tph*1000*specific_regeneration_kWh_per_kg_product
        outputs={"product":product,"recycle":recycle}
        return BlockResult(
            outputs,
            metrics={
                "dry_product_tph":product.total_tph,
                "ethanol_product_tph":product_etoh,
                "recycle_tph":recycle.total_tph,
                "regeneration_heat_kW":regeneration_kW,
                "dehydration_method":p.get("dehydration_method","Vapour-phase 3A molecular sieve"),
                "minimum_adsorption_beds":p.get("minimum_adsorption_beds",2),
                "feed_temperature_C":p.get("feed_temperature_C",120.0),
                "specified_recycle_ethanol_wt_fraction":p.get("regeneration_recycle_ethanol_wt_fraction",0.72),
                "calculated_recycle_ethanol_wt_fraction":(recycle_etoh/recycle.total_tph if recycle.total_tph else 0.0),
                "specified_recycle_destination":p.get("regeneration_recycle_destination","P09 Rectification, tray 14"),
                "closure_error_tph":_closure_error(inputs,outputs)
            },
            utilities=UtilityDemand(thermal_kW=regeneration_kW),
            equipment=[
                EquipmentRequirement(
                    equipment_type="3A molecular sieve dehydration",
                    design_flow_tph=s.total_tph,
                    design_duty_kW=regeneration_kW,
                    note="Minimum two-bed concept retained; regeneration design remains open."
                )
            ],
            metadata=EngineeringMetadata(
                status="SCREENING / OPEN DETAIL DESIGN",
                basis="Current dehydration product-purity model",
                confidence="LOW",
                note="Recycle is explicit but not yet connected to an iterative recycle solver."
            ),
            warnings=["Molecular sieve regeneration/recycle remains simplified."]
        )

class MixerBlock(BaseBlock):
    type_name = "mixer"
    display_name = "Mixer"
    input_ports = {
        "inlet_a": PortSpec("inlet_a","in",description="Material inlet A"),
        "inlet_b": PortSpec("inlet_b","in",description="Material inlet B")
    }
    output_ports = {
        "outlet": PortSpec("outlet","out",description="Mixed material stream")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        a, b = inputs["inlet_a"], inputs["inlet_b"]
        keys = set(a.components_tph) | set(b.components_tph)
        comp = {k:a.get(k)+b.get(k) for k in keys}
        total = a.total_tph + b.total_tph
        t = None
        if total > 0 and a.temperature_C is not None and b.temperature_C is not None:
            t = (a.total_tph*a.temperature_C + b.total_tph*b.temperature_C)/total
        out = Stream(f"{self.id}:outlet",comp,temperature_C=t,note="Mixed stream")
        return BlockResult({"outlet":out}, metrics={"outlet_tph":out.total_tph})

class SplitterBlock(BaseBlock):
    type_name = "splitter"
    display_name = "Splitter"
    input_ports = {
        "feed": PortSpec("feed","in",description="Material feed")
    }
    output_ports = {
        "outlet_a": PortSpec("outlet_a","out",description="Split outlet A"),
        "outlet_b": PortSpec("outlet_b","out",description="Split outlet B")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        s = inputs["feed"]
        frac = self.params.get("fraction_to_a",0.5)
        if not 0 <= frac <= 1:
            return BlockResult({}, errors=["Splitter fraction_to_a must be between 0 and 1."])
        a = Stream(f"{self.id}:outlet_a",{k:v*frac for k,v in s.components_tph.items()},
                   temperature_C=s.temperature_C,pressure_bar_abs=s.pressure_bar_abs,note="Splitter outlet A")
        b = Stream(f"{self.id}:outlet_b",{k:v*(1-frac) for k,v in s.components_tph.items()},
                   temperature_C=s.temperature_C,pressure_bar_abs=s.pressure_bar_abs,note="Splitter outlet B")
        return BlockResult({"outlet_a":a,"outlet_b":b},
                           metrics={"fraction_to_a":frac,"outlet_a_tph":a.total_tph,"outlet_b_tph":b.total_tph})

class TankBlock(BaseBlock):
    type_name = "tank"
    display_name = "Tank"
    input_ports = {
        "feed": PortSpec("feed","in",description="Material feed")
    }
    output_ports = {
        "outlet": PortSpec("outlet","out",description="Tank outlet")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        s = inputs["feed"].copy(new_id=f"{self.id}:outlet")
        residence = self.params.get("residence_time_h",1.0)
        density = self.params.get("density_t_per_m3",1.0)
        if residence < 0 or density <= 0:
            return BlockResult({}, errors=["Tank residence time must be non-negative and density positive."])
        volume = s.total_tph*residence/density
        s.note = "Tank outlet; no reaction model applied."
        return BlockResult({"outlet":s},
                           metrics={"residence_time_h":residence,"required_working_volume_m3":volume})

class HeatExchangerBlock(BaseBlock):
    type_name = "heat_exchanger"
    display_name = "Heat Exchanger"
    input_ports = {
        "hot_in": PortSpec("hot_in","in",description="Hot-side inlet"),
        "cold_in": PortSpec("cold_in","in",description="Cold-side inlet")
    }
    output_ports = {
        "hot_out": PortSpec("hot_out","out",description="Hot-side outlet"),
        "cold_out": PortSpec("cold_out","out",description="Cold-side outlet")
    }

    def calculate(self, inputs):
        errors = self.validate_inputs(inputs)
        if errors:
            return BlockResult({}, errors=errors)
        h, c = inputs["hot_in"], inputs["cold_in"]
        if h.temperature_C is None or c.temperature_C is None:
            return BlockResult({}, errors=["Heat exchanger requires temperatures on both inlet streams."])
        eff = self.params.get("effectiveness",0.75)
        cp_hot = self.params.get("hot_cp_kJ_per_kgK",4.0)
        cp_cold = self.params.get("cold_cp_kJ_per_kgK",4.0)
        if not 0 <= eff <= 1:
            return BlockResult({}, errors=["Heat exchanger effectiveness must be between 0 and 1."])
        ch = h.total_tph*1000/3600*cp_hot
        cc = c.total_tph*1000/3600*cp_cold
        cmin = min(ch,cc)
        qmax = cmin*max(0,h.temperature_C-c.temperature_C)
        q = eff*qmax
        hot_out_t = h.temperature_C - (q/ch if ch>0 else 0)
        cold_out_t = c.temperature_C + (q/cc if cc>0 else 0)
        ho = h.copy(new_id=f"{self.id}:hot_out"); ho.temperature_C=hot_out_t; ho.note="Heat exchanger hot outlet"
        co = c.copy(new_id=f"{self.id}:cold_out"); co.temperature_C=cold_out_t; co.note="Heat exchanger cold outlet"
        return BlockResult({"hot_out":ho,"cold_out":co},
                           metrics={"recovered_duty_kW":q,"hot_out_C":hot_out_t,"cold_out_C":cold_out_t})


def _closure_error(inputs, outputs):
    return sum(s.total_tph for s in inputs.values()) - sum(s.total_tph for s in outputs.values())

class PumpBlock(BaseBlock):
    type_name = "pump"
    display_name = "Pump"
    input_ports = {"feed": PortSpec("feed","in",description="Liquid/slurry feed")}
    output_ports = {"outlet": PortSpec("outlet","out",description="Pressurised outlet")}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]
        p=self.params
        dp_bar=p.get("delta_p_bar",2.0)
        efficiency=p.get("efficiency_fraction",0.70)
        density=p.get("density_kg_per_m3",1000.0)
        if efficiency<=0 or density<=0 or dp_bar<0:
            return BlockResult({},errors=["Pump efficiency/density must be positive and delta P non-negative."])
        m_kg_s=s.total_tph*1000/3600
        q_m3_s=m_kg_s/density
        hydraulic_kW=dp_bar*1e5*q_m3_s/1000
        elec=hydraulic_kW/efficiency
        out=s.copy(new_id=f"{self.id}:outlet")
        out.pressure_bar_abs=(s.pressure_bar_abs or 1.0)+dp_bar
        out.note="Pump outlet"
        return BlockResult(
            {"outlet":out},
            metrics={"delta_p_bar":dp_bar,"hydraulic_power_kW":hydraulic_kW,"electrical_power_kW":elec,
                     "closure_error_tph":_closure_error(inputs,{"outlet":out})},
            utilities=UtilityDemand(electricity_kW=elec),
            equipment=[EquipmentRequirement(equipment_type="Pump",design_flow_tph=s.total_tph,motor_kW=elec)],
            metadata=EngineeringMetadata(status="PROVISIONAL ENGINEERING ASSUMPTION",basis="Hydraulic screening",confidence="MEDIUM")
        )

class HeaterCoolerBlock(BaseBlock):
    type_name = "heater_cooler"
    display_name = "Heater / Cooler"
    input_ports = {"feed": PortSpec("feed","in",description="Material feed")}
    output_ports = {"outlet": PortSpec("outlet","out",description="Temperature-adjusted outlet")}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]; p=self.params
        target=p.get("target_temperature_C",50.0)
        cp=p.get("cp_kJ_per_kgK",4.0)
        if s.temperature_C is None:
            return BlockResult({},errors=["Heater/cooler requires inlet temperature."])
        m_kg_s=s.total_tph*1000/3600
        q=m_kg_s*cp*(target-s.temperature_C)
        out=s.copy(new_id=f"{self.id}:outlet"); out.temperature_C=target
        if q>=0:
            utilities=UtilityDemand(thermal_kW=q)
        else:
            utilities=UtilityDemand(cooling_kW=abs(q))
        return BlockResult(
            {"outlet":out},
            metrics={"duty_kW":q,"closure_error_tph":_closure_error(inputs,{"outlet":out})},
            utilities=utilities,
            equipment=[EquipmentRequirement(equipment_type="Heater/Cooler",design_flow_tph=s.total_tph,design_duty_kW=abs(q))],
            metadata=EngineeringMetadata(status="CALCULATED",basis="Sensible heat screening",confidence="MEDIUM")
        )


class ProductSinkBlock(BaseBlock):
    type_name = "product_sink"
    display_name = "Product"
    input_ports = {"feed": PortSpec("feed","in",description="Product stream")}
    output_ports = {}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]
        return BlockResult(
            {},
            metrics={"product_tph":s.total_tph},
            metadata=EngineeringMetadata(status="TERMINAL PRODUCT",basis="User-classified terminal stream",confidence="HIGH")
        )

class WastewaterSinkBlock(BaseBlock):
    type_name = "wastewater_sink"
    display_name = "Wastewater"
    input_ports = {"feed": PortSpec("feed","in",description="Wastewater/discharge stream")}
    output_ports = {}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]
        return BlockResult(
            {},
            metrics={"wastewater_tph":s.total_tph},
            discharges=Discharge(wastewater_tph=s.total_tph),
            metadata=EngineeringMetadata(status="TERMINAL DISCHARGE",basis="User-classified terminal stream",confidence="HIGH")
        )

class VentSinkBlock(BaseBlock):
    type_name = "vent_sink"
    display_name = "Vent / Gas"
    input_ports = {"feed": PortSpec("feed","in",description="Vent or gas stream")}
    output_ports = {}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]
        return BlockResult(
            {},
            metrics={"vent_tph":s.total_tph},
            discharges=Discharge(vent_gas_tph=s.total_tph),
            metadata=EngineeringMetadata(status="TERMINAL VENT",basis="User-classified terminal stream",confidence="HIGH")
        )

class SolidSinkBlock(BaseBlock):
    type_name = "solid_sink"
    display_name = "Solid Product / Waste"
    input_ports = {"feed": PortSpec("feed","in",description="Solid terminal stream")}
    output_ports = {}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]
        classification=self.params.get("classification","PRODUCT")
        discharge=Discharge(solid_waste_tph=s.total_tph if classification=="WASTE" else 0.0)
        return BlockResult(
            {},
            metrics={"solid_terminal_tph":s.total_tph,"classification":classification},
            discharges=discharge,
            metadata=EngineeringMetadata(status=f"TERMINAL {classification}",basis="User-classified terminal stream",confidence="HIGH")
        )

class RecycleSinkBlock(BaseBlock):
    type_name = "recycle_sink"
    display_name = "Recycle Placeholder"
    input_ports = {"feed": PortSpec("feed","in",description="Recycle stream")}
    output_ports = {}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]
        return BlockResult(
            {},
            metrics={"recycle_tph":s.total_tph},
            metadata=EngineeringMetadata(status="RECYCLE PLACEHOLDER",basis="Recycle not yet iteratively solved",confidence="MEDIUM"),
            warnings=["Recycle stream is terminated as a placeholder because convergence solving is not yet implemented."]
        )


class MaterialDoseBlock(BaseBlock):
    type_name = "material_dose"
    display_name = "Material / Chemical Dose"
    input_ports = {}
    output_ports = {"dose": PortSpec("dose","out",description="Defined dosing stream")}

    def calculate(self, inputs):
        p=self.params
        component=p.get("component","water")
        mass_tph=p.get("mass_tph",0.0)
        if mass_tph < 0:
            return BlockResult({},errors=["Dose mass flow cannot be negative."])
        s=Stream(
            f"{self.id}:dose",{component:mass_tph},
            temperature_C=p.get("temperature_C",20.0),
            pressure_bar_abs=p.get("pressure_bar_abs",1.0),
            note=f"{component} dosing stream"
        )
        elec=p.get("dosing_pump_kW",0.0)
        return BlockResult(
            {"dose":s}, metrics={"dose_tph":mass_tph},
            utilities=UtilityDemand(electricity_kW=elec),
            equipment=[EquipmentRequirement(equipment_type="Dosing system",design_flow_tph=mass_tph,motor_kW=elec)],
            metadata=EngineeringMetadata(
                status=p.get("status","OPEN DESIGN DECISION"),
                basis=p.get("basis","Explicit dosing input"),
                source=p.get("source",""),
                confidence=p.get("confidence","LOW"),
                note=p.get("note","")
            )
        )

class FlashLetdownBlock(BaseBlock):
    type_name = "flash_letdown"
    display_name = "Flash / Let-down"
    input_ports = {"feed": PortSpec("feed","in",description="Pressurised hot slurry/liquid")}
    output_ports = {
        "liquid": PortSpec("liquid","out",description="Let-down liquid/slurry"),
        "vapour": PortSpec("vapour","out",description="Flash vapour")
    }

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]; p=self.params
        frac=p.get("water_flash_fraction",0.0)
        if not 0<=frac<=1:
            return BlockResult({},errors=["water_flash_fraction must be between 0 and 1."])
        fw=s.get("water")*frac
        lc=dict(s.components_tph); lc["water"]=s.get("water")-fw
        liquid=Stream(f"{self.id}:liquid",lc,temperature_C=p.get("outlet_temperature_C",100.0),
                      pressure_bar_abs=p.get("outlet_pressure_bar_abs",1.2),note="Flash/let-down liquid")
        vapour=Stream(f"{self.id}:vapour",{"water":fw},temperature_C=p.get("outlet_temperature_C",100.0),
                      pressure_bar_abs=p.get("outlet_pressure_bar_abs",1.2),phase="gas",note="Flash vapour")
        outputs={"liquid":liquid,"vapour":vapour}
        return BlockResult(
            outputs,
            metrics={"flashed_water_tph":fw,"closure_error_tph":_closure_error(inputs,outputs)},
            metadata=EngineeringMetadata(status="OPEN DESIGN DECISION",
                basis="Parameterised flash fraction; rigorous thermodynamics deferred",confidence="LOW"),
            warnings=["Flash fraction is exploratory, not a selected commercial value."]
        )

class WastewaterCollectorBlock(BaseBlock):
    type_name = "wastewater_collector"
    display_name = "Wastewater Collector"
    input_ports = {
        "inlet_a": PortSpec("inlet_a","in",required=False),
        "inlet_b": PortSpec("inlet_b","in",required=False),
        "inlet_c": PortSpec("inlet_c","in",required=False),
        "inlet_d": PortSpec("inlet_d","in",required=False)
    }
    output_ports = {"outlet": PortSpec("outlet","out")}

    def calculate(self, inputs):
        if not inputs:
            return BlockResult({},errors=["Wastewater collector requires at least one inlet."])
        keys=set()
        for s in inputs.values(): keys.update(s.components_tph.keys())
        comp={k:sum(s.get(k) for s in inputs.values()) for k in keys}
        total=sum(s.total_tph for s in inputs.values())
        temp=None
        if total>0 and all(s.temperature_C is not None for s in inputs.values()):
            temp=sum(s.total_tph*s.temperature_C for s in inputs.values())/total
        out=Stream(f"{self.id}:outlet",comp,temperature_C=temp,note="Combined wastewater")
        return BlockResult({"outlet":out},
            metrics={"combined_wastewater_tph":out.total_tph,"closure_error_tph":_closure_error(inputs,{"outlet":out})},
            metadata=EngineeringMetadata(status="CALCULATED",basis="Material mixing balance",confidence="HIGH"))

class HeatRecoveryBlock(BaseBlock):
    type_name = "heat_recovery"
    display_name = "Heat Recovery"
    input_ports = {"hot_in": PortSpec("hot_in","in"),"cold_in": PortSpec("cold_in","in")}
    output_ports = {"hot_out": PortSpec("hot_out","out"),"cold_out": PortSpec("cold_out","out")}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        h,c=inputs["hot_in"],inputs["cold_in"]
        if h.temperature_C is None or c.temperature_C is None:
            return BlockResult({},errors=["Heat recovery requires both inlet temperatures."])
        p=self.params; eff=p.get("effectiveness",0.75)
        cp_h=p.get("hot_cp_kJ_per_kgK",4.0); cp_c=p.get("cold_cp_kJ_per_kgK",4.0)
        approach=p.get("minimum_approach_C",10.0)
        mh=h.total_tph*1000/3600; mc=c.total_tph*1000/3600
        ch=mh*cp_h; cc=mc*cp_c
        qmax=min(ch,cc)*max(0.0,h.temperature_C-c.temperature_C-approach)
        q=eff*qmax
        ho=h.copy(new_id=f"{self.id}:hot_out"); co=c.copy(new_id=f"{self.id}:cold_out")
        ho.temperature_C=h.temperature_C-(q/ch if ch>0 else 0)
        co.temperature_C=c.temperature_C+(q/cc if cc>0 else 0)
        outputs={"hot_out":ho,"cold_out":co}
        return BlockResult(outputs,
            metrics={"recovered_heat_kW":q,"hot_out_C":ho.temperature_C,"cold_out_C":co.temperature_C,
                     "closure_error_tph":_closure_error(inputs,outputs)},
            metadata=EngineeringMetadata(status="PROVISIONAL ENGINEERING MODEL",
                basis="Sensible heat/effectiveness/minimum approach",confidence="MEDIUM"))


class PretreatmentHeatRecoveryBlock(BaseBlock):
    type_name = "pretreatment_heat_recovery"
    display_name = "P03 Pretreatment Heat Recovery & Cooling"
    input_ports = {"feed": PortSpec("feed","in",description="Hot pretreated slurry from P02")}
    output_ports = {"cooled_slurry": PortSpec("cooled_slurry","out",description="Pretreated slurry cooled to P04 hydrolysis temperature")}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]; p=self.params
        cp=p.get("slurry_cp_kJ_per_kgK",3.644)
        cold_feed_C=p.get("cold_feed_temperature_C",10.0)
        hot_C=s.temperature_C if s.temperature_C is not None else p.get("hot_discharge_temperature_C",180.0)
        hydro_C=p.get("hydrolysis_target_temperature_C",50.0)
        eff=p.get("heat_recovery_effectiveness",0.75)
        allowance=p.get("design_heat_allowance_fraction",0.15)
        steam_h=p.get("useful_steam_enthalpy_kJ_per_kg",2100.0)
        m_kg_s=s.total_tph*1000/3600
        gross_heat=m_kg_s*cp*max(0.0,hot_C-cold_feed_C)
        available=m_kg_s*cp*max(0.0,hot_C-hydro_C)
        recovered=min(available,gross_heat*eff)
        net_external=max(0.0,gross_heat-recovered)
        design_heat=net_external*(1+allowance)
        residual_cooling=max(0.0,available-recovered)
        equivalent_preheat=cold_feed_C+(recovered/(m_kg_s*cp) if m_kg_s*cp>0 else 0.0)
        steam=design_heat*3600/steam_h if steam_h>0 else 0.0
        gross_steam=gross_heat*3600/steam_h if steam_h>0 else 0.0
        out=s.copy(new_id=f"{self.id}:cooled_slurry")
        out.temperature_C=hydro_C
        out.pressure_bar_abs=1.0
        return BlockResult(
            {"cooled_slurry":out},
            metrics={
                "gross_sensible_heat_kW":gross_heat,
                "available_hot_side_heat_kW":available,
                "recovered_heat_kW":recovered,
                "net_external_pretreatment_heat_kW":net_external,
                "design_external_heat_kW":design_heat,
                "residual_cooling_kW":residual_cooling,
                "equivalent_cold_feed_preheat_C":equivalent_preheat,
                "steam_kgph":steam,
                "closure_error_tph":_closure_error(inputs,{"cooled_slurry":out})
            },
            utilities=UtilityDemand(
                thermal_kW=design_heat-gross_heat,
                cooling_kW=residual_cooling,
                steam_kgph=steam-gross_steam,
                other={"heat_recovered_kW":recovered,"pretreatment_design_heat_kW":design_heat,"pretreatment_steam_kgph":steam}
            ),
            equipment=[EquipmentRequirement(equipment_type="P03 pretreatment heat recovery / final cooler",design_flow_tph=s.total_tph,design_duty_kW=available)],
            metadata=EngineeringMetadata(
                status="SPREADSHEET P03 ALIGNMENT",
                basis="P03 workbook basis: selected recovery is 75% of gross 10→180C sensible duty, capped by hot-side availability above 50C",
                confidence="MEDIUM",
                note="Cold-side feed preheat is represented energetically to avoid a material calculation cycle in the current acyclic solver."
            )
        )


class QualityRecycleBlock(BaseBlock):
    type_name = "quality_recycle"
    display_name = "Water Recycle / Quality Gate"
    input_ports = {"feed": PortSpec("feed","in",description="Candidate recycle-water stream")}
    output_ports = {
        "recycle": PortSpec("recycle","out",description="Accepted recycle stream"),
        "reject": PortSpec("reject","out",description="Rejected/discharged stream")
    }

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]; p=self.params
        max_nonwater_mass_fraction=p.get("max_nonwater_mass_fraction",0.02)
        requested_fraction=p.get("requested_recycle_fraction",0.5)
        if not 0<=requested_fraction<=1:
            return BlockResult({},errors=["requested_recycle_fraction must be 0..1"])
        total=s.total_tph
        nonwater=sum(v for k,v in s.components_tph.items() if k!="water")
        frac=nonwater/total if total>0 else 0
        accepted = requested_fraction if frac<=max_nonwater_mass_fraction else 0.0

        rc={k:v*accepted for k,v in s.components_tph.items()}
        rj={k:v*(1-accepted) for k,v in s.components_tph.items()}
        recycle=Stream(f"{self.id}:recycle",rc,temperature_C=s.temperature_C,pressure_bar_abs=s.pressure_bar_abs,
                       note="Recycle-water stream passing quality gate")
        reject=Stream(f"{self.id}:reject",rj,temperature_C=s.temperature_C,pressure_bar_abs=s.pressure_bar_abs,
                      note="Recycle reject / wastewater")
        outputs={"recycle":recycle,"reject":reject}
        return BlockResult(
            outputs,
            metrics={
                "candidate_nonwater_mass_fraction":frac,
                "requested_recycle_fraction":requested_fraction,
                "accepted_recycle_fraction":accepted,
                "recycle_tph":recycle.total_tph,
                "reject_tph":reject.total_tph,
                "closure_error_tph":_closure_error(inputs,outputs)
            },
            metadata=EngineeringMetadata(
                status="PROVISIONAL ENGINEERING MODEL",
                basis="Simple mass-fraction quality gate",
                confidence="LOW",
                note="Placeholder for future COD/salts/inhibitor/solids quality constraints."
            ),
            warnings=[] if accepted>0 else ["Recycle rejected by current quality threshold."]
        )

class UtilityHeaderBlock(BaseBlock):
    type_name = "utility_header"
    display_name = "Utility Header"
    input_ports = {}
    output_ports = {}

    def calculate(self, inputs):
        p=self.params
        return BlockResult(
            {},
            metrics={
                "description":p.get("description","Plant utility header"),
                "design_margin_fraction":p.get("design_margin_fraction",0.15)
            },
            metadata=EngineeringMetadata(status="SYSTEM SUMMARY",basis="Utility aggregation placeholder",confidence="MEDIUM")
        )

class BatchSchedulerBlock(BaseBlock):
    type_name = "batch_scheduler"
    display_name = "Batch Scheduler"
    input_ports = {}
    output_ports = {}

    def calculate(self, inputs):
        p=self.params
        residence_h=p.get("residence_time_h",48.0)
        turnaround_h=p.get("turnaround_h",2.0)
        vessel_count=int(p.get("vessel_count",1))
        if vessel_count<=0:
            return BlockResult({},errors=["vessel_count must be positive"])
        cycle=residence_h+turnaround_h
        stagger=cycle/vessel_count
        starts=[round(i*stagger,3) for i in range(vessel_count)]
        return BlockResult(
            {},
            metrics={
                "cycle_h":cycle,
                "stagger_interval_h":stagger,
                "vessel_count":vessel_count,
                "start_offsets_h":starts
            },
            metadata=EngineeringMetadata(status="CALCULATED",basis="Evenly staggered batch scheduler",confidence="MEDIUM")
        )


class EnzymeDoseBlock(BaseBlock):
    type_name = "enzyme_dose"
    display_name = "Enzyme Dose"
    input_ports = {"substrate": PortSpec("substrate","in",description="Glucan-containing hydrolysis feed")}
    output_ports = {
        "substrate_out": PortSpec("substrate_out","out",description="Unchanged substrate stream"),
        "enzyme": PortSpec("enzyme","out",description="Enzyme addition bookkeeping stream")
    }

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["substrate"]; p=self.params
        glucan_tph=s.get("glucan")
        fpu_per_g=p.get("loading_FPU_per_g_glucan",15.0)
        product_activity_FPU_per_mL=p.get("product_activity_FPU_per_mL",0.0)
        product_density_kg_per_L=p.get("product_density_kg_per_L",1.0)

        required_FPU_per_h=glucan_tph*1e6*fpu_per_g
        enzyme_m3ph=0.0
        enzyme_tph=0.0
        if product_activity_FPU_per_mL>0:
            enzyme_mLph=required_FPU_per_h/product_activity_FPU_per_mL
            enzyme_m3ph=enzyme_mLph/1e6
            enzyme_tph=enzyme_m3ph*product_density_kg_per_L

        substrate=s.copy(new_id=f"{self.id}:substrate_out")
        enzyme=Stream(
            f"{self.id}:enzyme",
            {"enzyme":enzyme_tph},
            temperature_C=p.get("temperature_C",20.0),
            pressure_bar_abs=1.0,
            note="Enzyme product mass only calculated when product activity is explicitly supplied."
        )
        outputs={"substrate_out":substrate,"enzyme":enzyme}
        return BlockResult(
            outputs,
            metrics={
                "glucan_tph":glucan_tph,
                "loading_FPU_per_g_glucan":fpu_per_g,
                "required_FPU_per_h":required_FPU_per_h,
                "product_activity_FPU_per_mL":product_activity_FPU_per_mL,
                "calculated_enzyme_m3ph":enzyme_m3ph,
                "calculated_enzyme_tph":enzyme_tph,
                "closure_error_material_tph":0.0
            },
            metadata=EngineeringMetadata(
                status="LITERATURE-SUPPORTED SCREENING BASIS",
                basis="Miscanthus literature commonly tests ~15-20 FPU/g glucan; exact commercial product dose must be trial/vendor selected.",
                source=p.get("source","Miscanthus literature"),
                confidence="MEDIUM",
                note="FPU requirement is calculated. Product mass flow remains zero unless a batch-specific enzyme activity is supplied."
            ),
            warnings=["Do not treat 15 FPU/g glucan as selected commercial enzyme dose without trial optimisation."]
        )

class NutrientDoseBlock(BaseBlock):
    type_name = "nutrient_dose"
    display_name = "Fermentation Nutrient Dose"
    input_ports = {"broth_basis": PortSpec("broth_basis","in",description="Fermentation feed used as volumetric basis")}
    output_ports = {
        "broth_out": PortSpec("broth_out","out"),
        "nutrient": PortSpec("nutrient","out")
    }

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["broth_basis"]; p=self.params
        density=p.get("broth_density_t_per_m3",1.0)
        target_N_g_per_L=p.get("assimilable_N_target_g_per_L",0.4)
        source_N_mass_fraction=p.get("nutrient_source_N_mass_fraction",0.0)

        vol_m3ph=s.total_tph/density if density>0 else 0.0
        N_kgph=vol_m3ph*target_N_g_per_L
        nutrient_kgph=N_kgph/source_N_mass_fraction if source_N_mass_fraction>0 else 0.0
        nutrient_tph=nutrient_kgph/1000

        broth=s.copy(new_id=f"{self.id}:broth_out")
        nutrient=Stream(f"{self.id}:nutrient",{"nutrient":nutrient_tph},note="Nutrient product mass only if N fraction entered.")
        return BlockResult(
            {"broth_out":broth,"nutrient":nutrient},
            metrics={
                "broth_volume_m3ph":vol_m3ph,
                "assimilable_N_target_g_per_L":target_N_g_per_L,
                "required_assimilable_N_kgph":N_kgph,
                "nutrient_source_N_mass_fraction":source_N_mass_fraction,
                "calculated_nutrient_kgph":nutrient_kgph
            },
            metadata=EngineeringMetadata(
                status="SCREENING / STRAIN-DEPENDENT",
                basis="Literature demonstrates nitrogen supplementation can improve ethanol fermentation; 0.4 g/L N is a screening reference from a strain study.",
                source=p.get("source","Ethanol fermentation nitrogen literature"),
                confidence="LOW",
                note="Actual nutrient package must be determined with selected yeast and Miscanthus hydrolysate."
            ),
            warnings=["Nutrient requirement is strongly strain/feed dependent; do not use as procurement basis."]
        )

class CIPDemandBlock(BaseBlock):
    type_name = "cip_demand"
    display_name = "CIP Demand"
    input_ports = {}
    output_ports = {}

    def calculate(self, inputs):
        p=self.params
        vessel_volume_m3=p.get("vessel_volume_m3",0.0)
        vessel_count=p.get("vessel_count",0)
        cip_volume_fraction=p.get("cip_volume_fraction_of_vessel",0.0)
        cycles_per_day=p.get("cycles_per_day",0.0)
        if min(vessel_volume_m3,vessel_count,cip_volume_fraction,cycles_per_day)<0:
            return BlockResult({},errors=["CIP parameters cannot be negative."])
        water_m3pd=vessel_volume_m3*vessel_count*cip_volume_fraction*cycles_per_day
        water_tph=water_m3pd/24.0
        heat_target=p.get("cip_temperature_C",0.0)
        inlet_temp=p.get("water_inlet_temperature_C",13.0)
        cp=4.18
        thermal_kW=water_tph*1000/3600*cp*max(0,heat_target-inlet_temp)
        return BlockResult(
            {},
            metrics={
                "cip_water_m3_per_day":water_m3pd,
                "cip_water_tph_average":water_tph,
                "cip_heating_kW_average":thermal_kW
            },
            utilities=UtilityDemand(
                cip_water_tph=water_tph,
                thermal_kW=thermal_kW,
                peak_thermal_kW=thermal_kW
            ),
            discharges=Discharge(wastewater_tph=water_tph),
            metadata=EngineeringMetadata(
                status="OPEN DETAIL DESIGN",
                basis="Parametric CIP demand model; no universal consumption value imposed.",
                source="NREL biochemical ethanol design confirms dedicated CIP service but does not justify a universal vessel-volume fraction.",
                confidence="LOW",
                note="Populate after vessel/CIP philosophy and cleaning validation are defined."
            )
        )

class CoolingWaterBlock(BaseBlock):
    type_name = "cooling_water"
    display_name = "Cooling Water Demand"
    input_ports = {}
    output_ports = {}

    def calculate(self, inputs):
        p=self.params
        duty_kW=p.get("cooling_duty_kW",0.0)
        deltaT=p.get("cooling_water_deltaT_C",9.0)
        cp=4.18
        if duty_kW<0 or deltaT<=0:
            return BlockResult({},errors=["Cooling duty must be non-negative and deltaT positive."])
        kg_s=duty_kW/(cp*deltaT)
        tph=kg_s*3.6
        return BlockResult(
            {},
            metrics={"cooling_water_tph":tph,"cooling_duty_kW":duty_kW,"deltaT_C":deltaT},
            utilities=UtilityDemand(cooling_kW=duty_kW,cooling_water_tph=tph,peak_cooling_kW=duty_kW),
            metadata=EngineeringMetadata(
                status="LITERATURE/DESIGN BASIS",
                basis="Cooling water sensible heat balance using 9°C system rise.",
                source="NREL biochemical ethanol process design uses 28°C supply and 9°C average rise.",
                confidence="MEDIUM"
            )
        )


class BeerConditioningBlock(BaseBlock):
    type_name = "beer_conditioning"
    display_name = "Beer Conditioning / Preheat"
    input_ports = {"feed": PortSpec("feed","in",description="Solids-free beer")}
    output_ports = {"outlet": PortSpec("outlet","out",description="Conditioned beer")}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["feed"]; p=self.params
        tin=s.temperature_C if s.temperature_C is not None else p.get("feed_temperature_C",32.0)
        tout=p.get("target_temperature_C",90.0)
        cp=p.get("cp_kJ_per_kgK",4.0)
        required_q=s.total_tph*1000/3600*cp*max(0.0,tout-tin)
        selected_recovery=max(0.0,p.get("economiser_recovery_kW",required_q))
        recovered_q=min(required_q,selected_recovery)
        trim_heat=max(0.0,required_q-recovered_q)
        out=s.copy(new_id=f"{self.id}:outlet"); out.temperature_C=tout
        warnings=[]
        if selected_recovery > required_q+1e-6:
            warnings.append("Selected P07 economiser recovery exceeds the current beer sensible-preheat requirement; recovery has been capped at the required duty.")
        return BlockResult(
            {"outlet":out},
            metrics={
                "required_preheat_kW":required_q,
                "economiser_recovery_kW":recovered_q,
                "economiser_recovery_fraction":(recovered_q/required_q if required_q else 0.0),
                "external_trim_heat_kW":trim_heat,
                "economiser_source":p.get("economiser_source","P08 beer-column bottoms"),
                "selected_economiser_recovery_kW":selected_recovery,
                "ethanol_wt_fraction":s.get("ethanol")/s.total_tph if s.total_tph else 0.0,
                "closure_error_tph":_closure_error(inputs,{"outlet":out})
            },
            utilities=UtilityDemand(thermal_kW=trim_heat,peak_thermal_kW=trim_heat,other={"internal_heat_recovery_kW":recovered_q}),
            metadata=EngineeringMetadata(
                status="EXCEL-MATCHED SCREENING BASIS",
                basis="Workbook P07 beer preheat / bottoms-to-beer economiser",
                confidence="MEDIUM",
                note="P07 now closes the cold-side energy balance explicitly. The workbook recovery duty is credited up to the beer preheat requirement; P08 bottoms temperature/enthalpy is still required before hot-side availability can be independently verified."
            ),
            warnings=warnings
        )

class DistillationUtilityEnvelopeBlock(BaseBlock):
    type_name = "distillation_utility_envelope"
    display_name = "Distillation Utility Envelope"
    input_ports = {"product_basis": PortSpec("product_basis","in")}
    output_ports = {"product_out": PortSpec("product_out","out")}

    def calculate(self, inputs):
        errors=self.validate_inputs(inputs)
        if errors: return BlockResult({},errors=errors)
        s=inputs["product_basis"]; p=self.params
        density=p.get("ethanol_density_kg_per_L",0.789)
        etoh=s.get("ethanol")
        Lph=etoh*1000/density if density>0 else 0.0
        spec=p.get("specific_thermal_MJ_per_L",7.958276533504055)
        q=Lph*spec/3.6
        steam_h=p.get("useful_steam_enthalpy_kJ_per_kg",2100.0)
        steam=q*3600/steam_h if steam_h>0 else 0.0
        ratio=p.get("condenser_to_reboiler_ratio",0.6112782817836794)
        condenser=q*ratio
        out=s.copy(new_id=f"{self.id}:product_out")
        return BlockResult(
            {"product_out":out},
            metrics={
                "ethanol_product_Lph":Lph,
                "specific_thermal_MJ_per_L":spec,
                "distillation_thermal_kW":q,
                "steam_kgph":steam,
                "condenser_cooling_kW":condenser
            },
            utilities=UtilityDemand(
                thermal_kW=q, peak_thermal_kW=q,
                steam_kgph=steam,
                cooling_kW=condenser, peak_cooling_kW=condenser
            ),
            metadata=EngineeringMetadata(
                status="EXCEL-MATCHED FEED SCREENING BASIS",
                basis="Workbook P12 NREL-normalised distillation duty",
                confidence="MEDIUM",
                note="Parity envelope only; rigorous VLE model to replace."
            )
        )

BLOCK_REGISTRY: Dict[str, Type[BaseBlock]] = {
    WaterSupplyBlock.type_name: WaterSupplyBlock,
    FeedPreparationBlock.type_name: FeedPreparationBlock,
    PretreatmentBlock.type_name: PretreatmentBlock,
    HydrolysisBlock.type_name: HydrolysisBlock,
    FermentationBlock.type_name: FermentationBlock,
    SolidsSeparationBlock.type_name: SolidsSeparationBlock,
    BeerColumnBlock.type_name: BeerColumnBlock,
    RectifierBlock.type_name: RectifierBlock,
    MolecularSieveBlock.type_name: MolecularSieveBlock,
    MixerBlock.type_name: MixerBlock,
    SplitterBlock.type_name: SplitterBlock,
    TankBlock.type_name: TankBlock,
    HeatExchangerBlock.type_name: HeatExchangerBlock,
    PumpBlock.type_name: PumpBlock,
    HeaterCoolerBlock.type_name: HeaterCoolerBlock,
    ProductSinkBlock.type_name: ProductSinkBlock,
    WastewaterSinkBlock.type_name: WastewaterSinkBlock,
    VentSinkBlock.type_name: VentSinkBlock,
    SolidSinkBlock.type_name: SolidSinkBlock,
    RecycleSinkBlock.type_name: RecycleSinkBlock,
    MaterialDoseBlock.type_name: MaterialDoseBlock,
    FlashLetdownBlock.type_name: FlashLetdownBlock,
    WastewaterCollectorBlock.type_name: WastewaterCollectorBlock,
    HeatRecoveryBlock.type_name: HeatRecoveryBlock,
    PretreatmentHeatRecoveryBlock.type_name: PretreatmentHeatRecoveryBlock,
    QualityRecycleBlock.type_name: QualityRecycleBlock,
    UtilityHeaderBlock.type_name: UtilityHeaderBlock,
    BatchSchedulerBlock.type_name: BatchSchedulerBlock,
    EnzymeDoseBlock.type_name: EnzymeDoseBlock,
    NutrientDoseBlock.type_name: NutrientDoseBlock,
    CIPDemandBlock.type_name: CIPDemandBlock,
    CoolingWaterBlock.type_name: CoolingWaterBlock,
    BeerConditioningBlock.type_name: BeerConditioningBlock,
    DistillationUtilityEnvelopeBlock.type_name: DistillationUtilityEnvelopeBlock,
}
