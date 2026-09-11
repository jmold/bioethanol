from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any

from native_dynamic_engine import build_native_dynamic_simulation


def _f(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


@dataclass
class BufferState:
    buffer_id: str
    name: str
    capacity_t: float
    inventory_t: float = 0.0
    maximum_inventory_t: float = 0.0

    def add(self, mass_t: float) -> None:
        if mass_t < -1e-9:
            raise ValueError("Cannot add negative mass to a buffer.")
        if self.inventory_t + mass_t > self.capacity_t + 1e-8:
            raise ValueError(f"Buffer {self.buffer_id} capacity exceeded.")
        self.inventory_t += mass_t
        self.maximum_inventory_t = max(self.maximum_inventory_t, self.inventory_t)

    def remove(self, mass_t: float) -> None:
        if mass_t < -1e-9:
            raise ValueError("Cannot remove negative mass from a buffer.")
        if self.inventory_t + 1e-8 < mass_t:
            raise ValueError(f"Buffer {self.buffer_id} inventory would become negative.")
        self.inventory_t = max(0.0, self.inventory_t - mass_t)


@dataclass
class PumpResource:
    pump_id: str
    name: str
    busy_until_h: float = 0.0
    owner: str | None = None
    busy_h: float = 0.0
    contention_count: int = 0

    def available(self, now_h: float) -> bool:
        return now_h >= self.busy_until_h - 1e-9

    def reserve(self, now_h: float, duration_h: float, owner: str) -> None:
        if not self.available(now_h):
            self.contention_count += 1
            raise RuntimeError(f"Pump {self.pump_id} is already reserved.")
        duration_h = max(0.0, duration_h)
        self.busy_until_h = now_h + duration_h
        self.owner = owner
        self.busy_h += duration_h


@dataclass
class Vessel:
    vessel_id: str
    section_id: str
    section_type: str
    batch_capacity_t: float
    state: str = "AVAILABLE"
    state_until_h: float = 0.0
    batch_mass_t: float = 0.0
    source_feed_equivalent_t: float = 0.0
    pending_action: str | None = None
    completed_batches: int = 0
    state_time_h: dict[str, float] = field(default_factory=dict)


def _section_specs(legacy: dict) -> list[dict]:
    return [dict(x) for x in legacy.get("vessel_schedules", []) if x.get("block_type") in {"pretreatment", "hydrolysis", "fermentation"}]


def _duration(schedule: dict, phase_name: str) -> float:
    for phase in schedule.get("phases", []):
        if phase.get("name") == phase_name:
            return max(0.0, _f(phase.get("duration_h")))
    return 0.0


def _processing_duration(schedule: dict) -> float:
    names = {
        "pretreatment": ("HEATING_PRETREATMENT", "REACTION_HOLD"),
        "hydrolysis": ("ENZYMATIC_HYDROLYSIS",),
        "fermentation": ("FERMENTATION",),
    }[schedule["block_type"]]
    return sum(_duration(schedule, n) for n in names)


def _cip_duration(schedule: dict) -> float:
    return _duration(schedule, "CIP_TURNAROUND")


def _buffer_capacity(schedule: dict, batches: float) -> float:
    return max(_f(schedule.get("batch_mass_t")), _f(schedule.get("batch_mass_t")) * max(1.0, batches))


def _ethanol_ratio(results: dict, first_section: dict | None) -> float:
    ethanol_tph = _f((results.get("terminal_component_totals") or {}).get("ethanol"), 0.0)
    feed_tph = _f((first_section or {}).get("required_throughput_tph"), 0.0)
    return ethanol_tph / feed_tph if feed_tph > 0 else 0.0


def build_connected_dynamic_simulation(
    definition: dict,
    results: dict,
    timestep_min: int = 15,
    horizon_h: float = 168.0,
    buffer_capacity_batches: float = 2.0,
) -> dict:
    """V0.20 connected discrete-event engineering screening engine.

    V0.19 remains the source of section sizing and phase durations. V0.20 adds
    causal batch hand-offs, finite intermediate buffers, one shared inlet and
    outlet pump per section, starvation, blocking and end-to-end throughput.
    """
    legacy = build_native_dynamic_simulation(definition, results, timestep_min=timestep_min, horizon_h=horizon_h)
    sections = _section_specs(legacy)
    order = {"pretreatment": 0, "hydrolysis": 1, "fermentation": 2}
    sections.sort(key=lambda x: order[x["block_type"]])
    if len(sections) < 3:
        out = dict(legacy)
        out.update({
            "engine": "Bio-Agri connected dynamic plant engine",
            "engine_version": "0.20.0-alpha",
            "connected_material_transfers": False,
            "status": "CONNECTED ENGINE REQUIRES PRETREATMENT, HYDROLYSIS AND FERMENTATION",
        })
        return out

    pt, hy, fe = sections[:3]
    buffer_pt_hy = BufferState("buffer_pt_hy", "Pretreatment to Hydrolysis Buffer", _buffer_capacity(hy, buffer_capacity_batches))
    buffer_hy_fe = BufferState("buffer_hy_fe", "Hydrolysis to Fermentation Buffer", _buffer_capacity(fe, buffer_capacity_batches))
    buffers = [buffer_pt_hy, buffer_hy_fe]

    vessels: list[Vessel] = []
    for sec in sections[:3]:
        for i in range(int(sec["installed_vessels"])):
            vessels.append(Vessel(
                vessel_id=f'{sec["block_id"]}-V{i+1:02d}',
                section_id=sec["block_id"],
                section_type=sec["block_type"],
                batch_capacity_t=_f(sec["batch_mass_t"]),
            ))

    schedule_by_type = {s["block_type"]: s for s in sections[:3]}
    pumps: dict[str, PumpResource] = {}
    for sec in sections[:3]:
        pumps[f'{sec["block_id"]}_in'] = PumpResource(f'{sec["block_id"]}_in', f'{sec["block_name"]} inlet transfer pump')
        pumps[f'{sec["block_id"]}_out'] = PumpResource(f'{sec["block_id"]}_out', f'{sec["block_name"]} outlet transfer pump')

    dt_h = max(1, int(timestep_min)) / 60.0
    horizon_h = max(1.0, float(horizon_h))
    timeline: list[dict] = []
    events: list[dict] = []
    completed_source_feed_t = 0.0
    source_feed_started_t = 0.0
    blocked_events = 0
    starved_events = 0

    def set_state(v: Vessel, state: str, now: float, duration_h: float = 0.0, action: str | None = None) -> None:
        v.state = state
        v.state_until_h = now + max(0.0, duration_h)
        v.pending_action = action

    def complete_due(v: Vessel, now: float) -> None:
        nonlocal completed_source_feed_t
        if now + 1e-9 < v.state_until_h:
            return
        action = v.pending_action
        if v.state == "FILLING":
            process_h = _processing_duration(schedule_by_type[v.section_type])
            process_state = {
                "pretreatment": "HEATING_PRETREATMENT",
                "hydrolysis": "ENZYMATIC_HYDROLYSIS",
                "fermentation": "FERMENTATION",
            }[v.section_type]
            set_state(v, process_state, now, process_h, "PROCESS_COMPLETE")
            events.append({"time_h": now, "event": "FILL_COMPLETE", "vessel": v.vessel_id, "mass_t": v.batch_mass_t})
        elif action == "PROCESS_COMPLETE":
            set_state(v, "WAITING_FOR_DESTINATION", now)
            events.append({"time_h": now, "event": "PROCESS_COMPLETE", "vessel": v.vessel_id})
        elif v.state == "EMPTYING":
            if v.section_type == "pretreatment":
                buffer_pt_hy.add(v.batch_mass_t)
            elif v.section_type == "hydrolysis":
                buffer_hy_fe.add(v.batch_mass_t)
            else:
                completed_source_feed_t += v.source_feed_equivalent_t
            events.append({"time_h": now, "event": "EMPTY_COMPLETE", "vessel": v.vessel_id, "mass_t": v.batch_mass_t})
            v.batch_mass_t = 0.0
            v.source_feed_equivalent_t = 0.0
            set_state(v, "CIP_TURNAROUND", now, _cip_duration(schedule_by_type[v.section_type]), "CIP_COMPLETE")
        elif action == "CIP_COMPLETE":
            v.completed_batches += 1
            set_state(v, "AVAILABLE", now)

    def try_start_fill(v: Vessel, now: float) -> bool:
        nonlocal source_feed_started_t, starved_events
        if v.state not in {"AVAILABLE", "WAITING_FOR_FEED", "STARVED"}:
            return False
        sched = schedule_by_type[v.section_type]
        mass = v.batch_capacity_t
        pump = pumps[f'{v.section_id}_in']
        if not pump.available(now):
            pump.contention_count += 1
            return False
        if v.section_type == "pretreatment":
            feed_equiv = mass
        elif v.section_type == "hydrolysis":
            if buffer_pt_hy.inventory_t + 1e-9 < mass:
                if v.state != "STARVED":
                    starved_events += 1
                set_state(v, "STARVED", now)
                return False
            buffer_pt_hy.remove(mass)
            feed_equiv = mass
        else:
            if buffer_hy_fe.inventory_t + 1e-9 < mass:
                if v.state != "STARVED":
                    starved_events += 1
                set_state(v, "STARVED", now)
                return False
            buffer_hy_fe.remove(mass)
            feed_equiv = mass
        dur = _f(sched.get("fill_time_h"), 0.0)
        pump.reserve(now, dur, v.vessel_id)
        v.batch_mass_t = mass
        v.source_feed_equivalent_t = feed_equiv
        if v.section_type == "pretreatment":
            source_feed_started_t += mass
        set_state(v, "FILLING", now, dur, "FILL_COMPLETE")
        events.append({"time_h": now, "event": "FILL_START", "vessel": v.vessel_id, "mass_t": mass, "pump": pump.pump_id})
        return True

    def try_start_empty(v: Vessel, now: float) -> bool:
        nonlocal blocked_events
        if v.state not in {"WAITING_FOR_DESTINATION", "BLOCKED"}:
            return False
        sched = schedule_by_type[v.section_type]
        pump = pumps[f'{v.section_id}_out']
        if not pump.available(now):
            pump.contention_count += 1
            return False
        if v.section_type == "pretreatment":
            if buffer_pt_hy.capacity_t - buffer_pt_hy.inventory_t + 1e-9 < v.batch_mass_t:
                if v.state != "BLOCKED":
                    blocked_events += 1
                set_state(v, "BLOCKED", now)
                return False
        elif v.section_type == "hydrolysis":
            if buffer_hy_fe.capacity_t - buffer_hy_fe.inventory_t + 1e-9 < v.batch_mass_t:
                if v.state != "BLOCKED":
                    blocked_events += 1
                set_state(v, "BLOCKED", now)
                return False
        dur = _f(sched.get("empty_time_h"), 0.0)
        pump.reserve(now, dur, v.vessel_id)
        set_state(v, "EMPTYING", now, dur, "EMPTY_COMPLETE")
        events.append({"time_h": now, "event": "EMPTY_START", "vessel": v.vessel_id, "mass_t": v.batch_mass_t, "pump": pump.pump_id})
        return True

    t = 0.0
    while t < horizon_h - 1e-9:
        for v in vessels:
            complete_due(v, t)

        for section_type in ("fermentation", "hydrolysis", "pretreatment"):
            for v in vessels:
                if v.section_type == section_type:
                    try_start_empty(v, t)

        for section_type in ("pretreatment", "hydrolysis", "fermentation"):
            for v in vessels:
                if v.section_type == section_type:
                    try_start_fill(v, t)

        for v in vessels:
            v.state_time_h[v.state] = v.state_time_h.get(v.state, 0.0) + dt_h

        timeline.append({
            "time_h": round(t, 6),
            "states": {
                sec["block_id"]: [v.state for v in vessels if v.section_id == sec["block_id"]]
                for sec in sections[:3]
            },
            "buffer_inventory_t": {
                buffer_pt_hy.buffer_id: buffer_pt_hy.inventory_t,
                buffer_hy_fe.buffer_id: buffer_hy_fe.inventory_t,
            },
            "pump_owner": {pid: (p.owner if not p.available(t) else None) for pid, p in pumps.items()},
            "completed_source_feed_t": completed_source_feed_t,
        })
        t += dt_h

    ethanol_ratio = _ethanol_ratio(results, pt)
    ethanol_t = completed_source_feed_t * ethanol_ratio
    connected_feed_tph = completed_source_feed_t / horizon_h if horizon_h > 0 else 0.0
    ethanol_tph = ethanol_t / horizon_h if horizon_h > 0 else 0.0
    ethanol_density_t_per_m3 = 0.78937
    ethanol_Lph = ethanol_tph / ethanol_density_t_per_m3 * 1000.0 if ethanol_density_t_per_m3 > 0 else 0.0

    source_in_system_t = sum(v.source_feed_equivalent_t for v in vessels) + buffer_pt_hy.inventory_t + buffer_hy_fe.inventory_t
    mass_balance_error_t = source_feed_started_t - completed_source_feed_t - source_in_system_t

    out = dict(legacy)
    out.update({
        "engine": "Bio-Agri connected dynamic plant engine",
        "engine_version": "0.20.0-alpha",
        "connected_material_transfers": True,
        "legacy_scheduler_comparison": {
            "engine_version": legacy.get("engine_version"),
            "maximum_feed_multiplier_before_batch_capacity_limit": legacy.get("maximum_feed_multiplier_before_batch_capacity_limit"),
        },
        "buffers": [asdict(b) for b in buffers],
        "shared_pumps": [asdict(p) for p in pumps.values()],
        "event_log": events,
        "timeline": timeline,
        "connected_throughput": {
            "source_feed_started_t": source_feed_started_t,
            "source_feed_completed_t": completed_source_feed_t,
            "average_completed_feed_tph": connected_feed_tph,
            "ethanol_product_t": ethanol_t,
            "ethanol_product_tph": ethanol_tph,
            "ethanol_product_Lph": ethanol_Lph,
            "ethanol_product_L_per_8000h_year": ethanol_Lph * 8000.0,
            "mass_balance_error_t": mass_balance_error_t,
        },
        "operability": {
            "starvation_events": starved_events,
            "blocking_events": blocked_events,
            "pump_contention_events": sum(p.contention_count for p in pumps.values()),
            "vessels": [
                {
                    "vessel_id": v.vessel_id,
                    "section_id": v.section_id,
                    "completed_batches": v.completed_batches,
                    "state_time_h": dict(v.state_time_h),
                }
                for v in vessels
            ],
        },
        "status": "V0.20 CONNECTED DISCRETE-EVENT ENGINEERING SCREENING",
        "notes": list(legacy.get("notes", [])) + [
            "V0.20 enforces causal batch hand-offs between pretreatment, hydrolysis and fermentation.",
            "Intermediate buffers have finite capacity; vessels can become STARVED or BLOCKED.",
            "Each batch section has one shared inlet and one shared outlet transfer-pump resource, so simultaneous transfers contend for that resource.",
            "Connected transfer inventory is tracked on a hydraulic wet-mass basis; transient reaction chemistry remains represented by the steady-state block calculations.",
            "The V0.19 independently staggered scheduler remains available internally as a comparison baseline.",
        ],
    })
    return out
