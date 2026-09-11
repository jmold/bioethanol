"""V0.20 API extension for the connected discrete-event plant engine.

The existing V0.19 API remains intact for regression comparison.  The desktop
entry point imports this module, which adds the connected-engine endpoint to
the same FastAPI application.
"""

from fastapi import HTTPException

from api import app, FlowsheetPayload, validate_flowsheet_definition
from flowsheet import Flowsheet
from connected_dynamic_engine import build_connected_dynamic_simulation


@app.post("/api/connected-plant")
def connected_plant(payload: FlowsheetPayload):
    definition = validate_flowsheet_definition(payload.flowsheet)
    try:
        result = Flowsheet(definition).run()
        return build_connected_dynamic_simulation(definition, result)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"The V0.20 connected plant engine could not calculate this flowsheet: {exc}") from exc


@app.get("/api/v020-meta")
def v020_meta():
    return {
        "application": "Bio-Agri Process Simulator",
        "model_version": "0.20.0-alpha",
        "engine": "connected discrete-event plant engine",
        "connected_material_transfers": True,
        "legacy_model_retained": "0.19.0",
    }
