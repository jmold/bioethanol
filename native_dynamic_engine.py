from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class PhaseSpec:
    name: str
    duration_h: float
    thermal_kW: float = 0.0
    electricity_kW: float = 0.0
    cooling_kW: float = 0.0


@dataclass
class VesselSchedule:
    block_id: str
    block_name: str
    block_type: str
    installed_vessels: int
    required_vessels: int
    vessel_working_volume_m3: float
    batch_mass_t: float
    cycle_time_h: float
    capacity_tph: float
    required_throughput_tph: float
    utilisation_fraction: float
    bottleneck: bool
    inlet_transfer_pump_rate_m3ph: float
    outlet_transfer_pump_rate_m3ph: float
    inlet_transfer_pump_kW: float
    outlet_transfer_pump_kW: float
    fill_time_h: float
    empty_time_h: float
    phases: list[PhaseSpec]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["phases"] = [asdict(p) for p in self.phases]
        return data


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _block_result(results: dict, block_id: str) -> dict:
    return (results.get("block_results") or {}).get(block_id, {}) or {}


def _equipment_design_flow(result: dict) -> float:
    equipment = result.get("equipment") or []
    for item in equipment:
        v = _safe_float(item.get("design_flow_tph"), -1.0)
        if v >= 0:
            return v
    outputs = result.get("outputs") or {}
    if outputs:
        first = next(iter(outputs.values()))
        return _safe_float(first.get("total_tph"), 0.0)
    return 0.0


def _installed_count(params: dict, result: dict) -> int:
    explicit = params.get("installed_vessel_count", params.get("vessel_count"))
    if explicit is not None:
        return max(1, int(round(_safe_float(explicit, 1))))
    equipment = result.get("equipment") or []
    if equipment:
        q = equipment[0].get("quantity")
        if q is not None:
            return max(1, int(math.ceil(_safe_float(q, 1.0))))
    metrics = result.get("metrics") or {}
    for key in ("provisional_reactor_count", "vessel_count"):
        if key in metrics:
            return max(1, int(math.ceil(_safe_float(metrics[key], 1.0))))
    return 1


def _transfer_duration(volume_m3: float, params: dict, rate_key: str, legacy_time_key: str, fallback_h: float) -> float:
    """Calculate transfer duration from selected pump capacity, retaining legacy files as a fallback."""
    if rate_key in params:
        rate_m3ph = _safe_float(params.get(rate_key), -1.0)
        if rate_m3ph <= 0:
            raise ValueError(f"{rate_key} must be greater than zero.")
        return max(0.0, volume_m3) / rate_m3ph
    return _safe_float(params.get(legacy_time_key), fallback_h)


def _phase_at(t: float, offset: float, cycle_h: float, phases: list[PhaseSpec]) -> str:
    if cycle_h <= 0:
        return "AVAILABLE"
    p = (t - offset) % cycle_h
    cursor = 0.0
    for phase in phases:
        cursor += phase.duration_h
        if p < cursor - 1e-12:
            return phase.name
    return "AVAILABLE"


def _phase_spec_at(t: float, offset: float, cycle_h: float, phases: list[PhaseSpec]) -> PhaseSpec | None:
    if cycle_h <= 0:
        return None
    p = (t - offset) % cycle_h
    cursor = 0.0
    for phase in phases:
        cursor += phase.duration_h
        if p < cursor - 1e-12:
            return phase
    return None


def _kinetic_profile(final_fraction: float, residence_h: float, points: int = 49) -> dict:
    final_fraction = min(max(final_fraction, 0.0), 0.999999)
    residence_h = max(residence_h, 1e-9)
    if final_fraction <= 0:
        k = 0.0
    else:
        k = -math.log(1.0 - final_fraction) / residence_h
    rows = []
    for i in range(points):
        t = residence_h * i / (points - 1)
        x = 1.0 - math.exp(-k * t) if k > 0 else 0.0
        rows.append({"time_h": round(t, 6), "fraction": x})
    return {
        "model": "first_order_empirical_calibrated_to_selected_endpoint",
        "rate_constant_per_h": k,
        "selected_residence_h": residence_h,
        "selected_endpoint_fraction": final_fraction,
        "rows": rows,
        "status": "CALIBRATED EMPIRICAL DYNAMIC SCREENING MODEL",
    }


def build_native_dynamic_simulation(definition: dict, results: dict, timestep_min: int = 15, horizon_h: float = 168.0) -> dict:
    """Build a native time-domain operating model from the configured flowsheet.

    This is deliberately dependency-free process logic: it does not call DWSIM,
    Aspen, MATLAB, Excel, or any external simulator. It uses the current block
    configuration and the already-calculated mass/utility results as its basis.
    """
    timestep_min = max(1, int(timestep_min))
    horizon_h = max(float(horizon_h), 1.0)
    schedules: list[VesselSchedule] = []
    kinetics: dict[str, dict] = {}

    # Specialist chemistry models currently provide these batch schedule adapters.
    # The rest of the dynamic engine consumes the common VesselSchedule contract;
    # future batch-capable blocks only need to add an adapter here, not alter the
    # connected scheduler or flowsheet topology logic.
    BATCH_SCHEDULE_ADAPTER_TYPES = {"pretreatment", "hydrolysis", "fermentation"}

    for block in definition.get("blocks", []):
        btype = block.get("type")
        if btype not in BATCH_SCHEDULE_ADAPTER_TYPES:
            continue
        bid = block["id"]
        name = block.get("name", bid)
        p = block.get("params", {}) or {}
        r = _block_result(results, bid)
        metrics = r.get("metrics") or {}
        required_tph = _equipment_design_flow(r)

        if btype == "pretreatment":
            volume = _safe_float(p.get("reactor_working_volume_m3"), 50.0)
            density = _safe_float(p.get("slurry_density_t_per_m3"), 1.0)
            batch_mass = volume * density
            legacy_fill_h = batch_mass / required_tph if required_tph > 0 else 0.0
            fill_h = _transfer_duration(volume, p, "inlet_transfer_pump_rate_m3ph", "fill_time_h", legacy_fill_h)
            heat_h = _safe_float(p.get("heat_up_time_h"), 1.5)
            react_h = _safe_float(p.get("hold_time_h"), 1.0)
            empty_h = _transfer_duration(volume, p, "outlet_transfer_pump_rate_m3ph", "empty_time_h", fill_h)
            cip_h = _safe_float(p.get("cip_turnaround_h"), 0.5)
            fill_pump_kW = _safe_float(p.get("inlet_transfer_pump_kW"), 0.0)
            empty_pump_kW = _safe_float(p.get("outlet_transfer_pump_kW"), 0.0)
            inlet_temp = _safe_float(p.get("assumed_inlet_temperature_C"), 10.0)
            target_temp = _safe_float(p.get("temperature_C_placeholder"), 180.0)
            cp = _safe_float(p.get("slurry_cp_kJ_per_kgK"), 3.644)
            allowance = 1 + _safe_float(p.get("design_heat_allowance_fraction"), 0.15)
            batch_heat_kWh = max(0.0, batch_mass * 1000.0 * cp * (target_temp - inlet_temp) / 3600.0) * allowance
            heat_peak_kW = batch_heat_kWh / heat_h if heat_h > 0 else 0.0
            phases = [
                PhaseSpec("FILLING", fill_h, electricity_kW=fill_pump_kW),
                PhaseSpec("HEATING_PRETREATMENT", heat_h, thermal_kW=heat_peak_kW),
                PhaseSpec("REACTION_HOLD", react_h),
                PhaseSpec("EMPTYING_HOT", empty_h, electricity_kW=empty_pump_kW),
                PhaseSpec("CIP_TURNAROUND", cip_h),
            ]
        elif btype == "hydrolysis":
            volume = _safe_float(p.get("vessel_working_volume_m3"), 100.0)
            density = _safe_float(p.get("slurry_density_t_per_m3"), 1.0)
            batch_mass = volume * density
            legacy_fill_h = batch_mass / required_tph if required_tph > 0 else 0.0
            fill_h = _transfer_duration(volume, p, "inlet_transfer_pump_rate_m3ph", "fill_time_h", legacy_fill_h)
            react_h = _safe_float(p.get("residence_time_h"), 72.0)
            empty_h = _transfer_duration(volume, p, "outlet_transfer_pump_rate_m3ph", "empty_time_h", fill_h)
            cip_h = _safe_float(p.get("turnaround_h"), 2.0)
            fill_pump_kW = _safe_float(p.get("inlet_transfer_pump_kW"), 0.0)
            empty_pump_kW = _safe_float(p.get("outlet_transfer_pump_kW"), 0.0)
            per_vessel_agitator = volume * _safe_float(p.get("specific_agitation_kW_per_m3"), 0.35)
            hold_heat = volume * _safe_float(p.get("heat_loss_kW_per_m3"), 0.0)
            phases = [
                PhaseSpec("FILLING", fill_h, electricity_kW=fill_pump_kW),
                PhaseSpec("ENZYMATIC_HYDROLYSIS", react_h, thermal_kW=hold_heat, electricity_kW=per_vessel_agitator),
                PhaseSpec("EMPTYING", empty_h, electricity_kW=empty_pump_kW),
                PhaseSpec("CIP_TURNAROUND", cip_h),
            ]
            kinetics[bid] = _kinetic_profile(
                _safe_float(p.get("glucan_to_glucose_conversion_fraction"), 0.7674), react_h
            )
        else:
            volume = _safe_float(p.get("vessel_working_volume_m3"), 100.0)
            density = _safe_float(p.get("broth_density_t_per_m3"), 1.0)
            batch_mass = volume * density
            legacy_fill_h = batch_mass / required_tph if required_tph > 0 else 0.0
            fill_h = _transfer_duration(volume, p, "inlet_transfer_pump_rate_m3ph", "fill_time_h", legacy_fill_h)
            react_h = _safe_float(p.get("residence_time_h"), 48.0)
            empty_h = _transfer_duration(volume, p, "outlet_transfer_pump_rate_m3ph", "empty_time_h", fill_h)
            cip_h = _safe_float(p.get("turnaround_h"), 2.0)
            fill_pump_kW = _safe_float(p.get("inlet_transfer_pump_kW"), 0.0)
            empty_pump_kW = _safe_float(p.get("outlet_transfer_pump_kW"), 0.0)
            per_vessel_agitator = volume * _safe_float(p.get("specific_agitation_kW_per_m3"), 0.10)
            total_cooling = _safe_float(metrics.get("fermentation_cooling_kW"), 0.0)
            installed_guess = max(1, _installed_count(p, r))
            cooling_per_active = total_cooling / installed_guess
            phases = [
                PhaseSpec("FILLING", fill_h, electricity_kW=fill_pump_kW),
                PhaseSpec("FERMENTATION", react_h, electricity_kW=per_vessel_agitator, cooling_kW=cooling_per_active),
                PhaseSpec("EMPTYING", empty_h, electricity_kW=empty_pump_kW),
                PhaseSpec("CIP_TURNAROUND", cip_h),
            ]
            kinetics[bid] = _kinetic_profile(
                _safe_float(p.get("fraction_theoretical_ethanol_yield"), 0.95), react_h
            )

        cycle_h = sum(max(0.0, ph.duration_h) for ph in phases)
        installed = _installed_count(p, r)
        single_capacity = batch_mass / cycle_h if cycle_h > 0 else 0.0
        capacity = installed * single_capacity
        required_vessels = max(1, math.ceil(required_tph / single_capacity - 1e-12)) if single_capacity > 0 else installed
        util = required_tph / capacity if capacity > 0 else 0.0
        schedules.append(VesselSchedule(
            block_id=bid,
            block_name=name,
            block_type=btype,
            installed_vessels=installed,
            required_vessels=required_vessels,
            vessel_working_volume_m3=volume,
            batch_mass_t=batch_mass,
            cycle_time_h=cycle_h,
            capacity_tph=capacity,
            required_throughput_tph=required_tph,
            utilisation_fraction=util,
            bottleneck=required_tph > capacity + 1e-9,
            inlet_transfer_pump_rate_m3ph=volume / fill_h if fill_h > 0 else 0.0,
            outlet_transfer_pump_rate_m3ph=volume / empty_h if empty_h > 0 else 0.0,
            inlet_transfer_pump_kW=fill_pump_kW,
            outlet_transfer_pump_kW=empty_pump_kW,
            fill_time_h=fill_h,
            empty_time_h=empty_h,
            phases=phases,
        ))

    dt_h = timestep_min / 60.0
    rows = []
    t = 0.0
    while t < horizon_h - 1e-12:
        thermal = _safe_float((results.get("utility_totals") or {}).get("thermal_kW"), 0.0)
        electricity_cont = _safe_float((results.get("utility_totals") or {}).get("electricity_kW"), 0.0)
        cooling_cont = _safe_float((results.get("utility_totals") or {}).get("cooling_kW"), 0.0)
        # Remove the batch-block average utilities; add them back according to actual state below.
        for schedule in schedules:
            u = ((results.get("utilities_by_block") or {}).get(schedule.block_id) or {})
            thermal -= _safe_float(u.get("thermal_kW"), 0.0)
            electricity_cont -= _safe_float(u.get("electricity_kW"), 0.0)
            cooling_cont -= _safe_float(u.get("cooling_kW"), 0.0)

        dynamic_thermal = 0.0
        dynamic_electricity = 0.0
        dynamic_cooling = 0.0
        state_map: dict[str, list[str]] = {}
        hot_discharges = 0
        active_heatups = 0
        pretreat_heat_demand = 0.0

        for schedule in schedules:
            state_map[schedule.block_id] = []
            stagger = schedule.cycle_time_h / schedule.installed_vessels if schedule.installed_vessels else 0.0
            for v in range(schedule.installed_vessels):
                spec = _phase_spec_at(t, v * stagger, schedule.cycle_time_h, schedule.phases)
                state = spec.name if spec else "AVAILABLE"
                state_map[schedule.block_id].append(state)
                if not spec:
                    continue
                dynamic_thermal += spec.thermal_kW
                dynamic_electricity += spec.electricity_kW
                dynamic_cooling += spec.cooling_kW
                if schedule.block_type == "pretreatment":
                    if state == "HEATING_PRETREATMENT":
                        active_heatups += 1
                        pretreat_heat_demand += spec.thermal_kW
                    elif state == "EMPTYING_HOT":
                        hot_discharges += 1

        # Native time-aligned heat recovery. Recovery cannot exceed simultaneous cold-side demand.
        pretreat_block = next((b for b in definition.get("blocks", []) if b.get("type") == "pretreatment"), None)
        recovery_fraction = _safe_float((pretreat_block or {}).get("params", {}).get("gross_heat_recovery_fraction"), 0.0)
        available_recovery = pretreat_heat_demand * recovery_fraction if hot_discharges > 0 else 0.0
        used_recovery = min(pretreat_heat_demand, available_recovery)
        net_thermal = max(0.0, thermal) + max(0.0, dynamic_thermal - used_recovery)
        total_electricity = max(0.0, electricity_cont) + dynamic_electricity
        total_cooling = max(0.0, cooling_cont) + dynamic_cooling

        rows.append({
            "time_h": round(t, 6),
            "thermal_gross_kW": max(0.0, thermal) + dynamic_thermal,
            "available_heat_recovery_kW": available_recovery,
            "used_heat_recovery_kW": used_recovery,
            "net_external_thermal_kW": net_thermal,
            "electrical_kW": total_electricity,
            "cooling_kW": total_cooling,
            "active_pretreatment_heatups": active_heatups,
            "active_hot_discharges": hot_discharges,
            "states": state_map,
        })
        t += dt_h

    def stat(key: str) -> dict[str, float]:
        vals = [row[key] for row in rows]
        return {"average": sum(vals) / len(vals), "peak": max(vals), "minimum": min(vals)} if vals else {"average": 0.0, "peak": 0.0, "minimum": 0.0}

    feasible = all(not s.bottleneck for s in schedules)
    if schedules:
        capacity_ratios = [s.capacity_tph / s.required_throughput_tph for s in schedules if s.required_throughput_tph > 0]
        max_feed_multiplier = min(capacity_ratios) if capacity_ratios else 1.0
        bottleneck = min(schedules, key=lambda s: (s.capacity_tph / s.required_throughput_tph) if s.required_throughput_tph > 0 else float("inf"))
        bottleneck_id = bottleneck.block_id
    else:
        max_feed_multiplier = 1.0
        bottleneck_id = None

    return {
        "engine": "Bio-Agri native dynamic plant engine",
        "engine_version": "0.19.0",
        "external_simulator_dependency": False,
        "timestep_min": timestep_min,
        "horizon_h": horizon_h,
        "operating_basis_h_per_year": 8000,
        "plant_feasible_at_selected_throughput": feasible,
        "bottleneck_block_id": bottleneck_id,
        "maximum_feed_multiplier_before_batch_capacity_limit": max_feed_multiplier,
        "vessel_schedules": [s.to_dict() for s in schedules],
        "kinetic_profiles": kinetics,
        "utility_summary": {
            "net_external_thermal_kW": stat("net_external_thermal_kW"),
            "gross_thermal_kW": stat("thermal_gross_kW"),
            "used_heat_recovery_kW": stat("used_heat_recovery_kW"),
            "electrical_kW": stat("electrical_kW"),
            "cooling_kW": stat("cooling_kW"),
        },
        "timeline": rows,
        "status": "NATIVE TIME-DOMAIN ENGINEERING SCREENING",
        "notes": [
            "Batch capacity is calculated from configured vessel working volume, inlet/outlet transfer-pump capacity, reaction/hold time and CIP turnaround.",
            "Hydrolysis and fermentation time profiles are empirical first-order curves calibrated exactly to the selected endpoint conversion/yield; they are not claimed as fundamental kinetics.",
            "Heat recovery is credited only when pretreatment heating demand and a hot pretreatment discharge coincide in the same timestep.",
            "No external process-simulation software is called or required.",
        ],
    }
