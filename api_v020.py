"""V0.20.1 API extension for the connected discrete-event plant engine.

The steady-state V0.19 engineering model remains authoritative for chemistry,
thermodynamics and utilities. V0.20.1 retains only the time-domain plant payload
with the connected event engine, while retaining the V0.19 utility basis.
"""

from fastapi import HTTPException

from api import app, FlowsheetPayload, validate_flowsheet_definition
from flowsheet import Flowsheet
from native_dynamic_engine import build_native_dynamic_simulation
from connected_dynamic_engine import build_connected_dynamic_simulation


def _remove_post_route(path: str) -> None:
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (getattr(route, "path", None) == path and "POST" in (getattr(route, "methods", set()) or set()))
    ]


def _connected_payload(definition: dict, result: dict) -> dict:
    """Build V0.20.1 and retain V0.19 utility readings on the common time grid."""
    legacy = build_native_dynamic_simulation(definition, result)
    connected = build_connected_dynamic_simulation(definition, result)
    legacy_rows = legacy.get("timeline", [])
    for index, row in enumerate(connected.get("timeline", [])):
        if index >= len(legacy_rows):
            break
        legacy_row = legacy_rows[index]
        for key in (
            "thermal_gross_kW",
            "available_heat_recovery_kW",
            "used_heat_recovery_kW",
            "net_external_thermal_kW",
            "electrical_kW",
            "cooling_kW",
            "active_pretreatment_heatups",
            "active_hot_discharges",
        ):
            row[key] = legacy_row.get(key, 0.0)
    connected["legacy_utility_basis"] = {
        "engine_version": legacy.get("engine_version", "0.19.0"),
        "utility_basis": "V0.19 time-domain utility model retained during V0.20.1",
    }
    return connected


_remove_post_route("/api/run")
_remove_post_route("/api/dynamic-plant")


@app.post("/api/run")
def run_flowsheet_v020(payload: FlowsheetPayload):
    definition = validate_flowsheet_definition(payload.flowsheet)
    try:
        result = Flowsheet(definition).run()
        result["dynamic_plant"] = _connected_payload(definition, result)
        return result
    except (KeyError, TypeError, ValueError, ZeroDivisionError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"The V0.20.1 model could not calculate this flowsheet: {exc}") from exc


@app.post("/api/dynamic-plant")
def dynamic_plant_v020(payload: FlowsheetPayload):
    definition = validate_flowsheet_definition(payload.flowsheet)
    try:
        result = Flowsheet(definition).run()
        return _connected_payload(definition, result)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"The V0.20.1 connected plant engine could not calculate this flowsheet: {exc}") from exc


@app.post("/api/connected-plant")
def connected_plant(payload: FlowsheetPayload):
    definition = validate_flowsheet_definition(payload.flowsheet)
    try:
        result = Flowsheet(definition).run()
        return _connected_payload(definition, result)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"The V0.20.1 connected plant engine could not calculate this flowsheet: {exc}") from exc


@app.get("/api/v020-meta")
def v020_meta():
    return {
        "application": "Bio-Agri Process Simulator",
        "model_version": "0.20.1",
        "engine": "direct vessel-to-vessel discrete-event plant engine",
        "connected_material_transfers": True,
        "intermediate_buffers_assumed": False,
        "legacy_model_retained": "0.19.0",
        "chemistry_utility_basis": "V0.19 dynamic utility basis retained; steady-state flowsheet aligned to P01-P12 master sheet",
    }
