
from pathlib import Path
import json
import math
from html import escape
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from flowsheet import Flowsheet
from scenario_engine import ScenarioManager,ScenarioDefinition,ParameterOverride,SensitivityRunner,extract_kpis
from blocks import BLOCK_REGISTRY
from native_dynamic_engine import build_native_dynamic_simulation

HERE = Path(__file__).resolve().parent

APP_VERSION = "0.25.0"
MODEL_VERSION = "0.25.0"

app = FastAPI(title="Bio-Agri Process Simulator API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"],
    allow_origin_regex=r"^http://(127\.0\.0\.1|localhost):\d+$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class FlowsheetPayload(BaseModel):
    flowsheet: dict

def validate_flowsheet_definition(definition: dict, allow_empty: bool = False) -> dict:
    """Reject malformed user input before it reaches the calculation engine."""
    if not isinstance(definition, dict):
        raise HTTPException(status_code=422, detail="Flowsheet must be a JSON object.")
    blocks = definition.get("blocks")
    connections = definition.get("connections")
    if not isinstance(blocks, list) or not isinstance(connections, list):
        raise HTTPException(status_code=422, detail="Flowsheet requires 'blocks' and 'connections' lists.")
    if not blocks and not allow_empty:
        raise HTTPException(status_code=422, detail="The flowsheet contains no process blocks.")
    ids = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise HTTPException(status_code=422, detail=f"Block {index + 1} must be an object.")
        bid = str(block.get("id", "")).strip()
        btype = str(block.get("type", "")).strip()
        if not bid:
            raise HTTPException(status_code=422, detail=f"Block {index + 1} has no ID.")
        if btype not in BLOCK_REGISTRY:
            raise HTTPException(status_code=422, detail=f"Block '{bid}' has unknown type '{btype}'.")
        if not isinstance(block.get("params", {}), dict):
            raise HTTPException(status_code=422, detail=f"Parameters for block '{bid}' must be an object.")
        for key, value in block.get("params", {}).items():
            if isinstance(value, float) and not math.isfinite(value):
                raise HTTPException(status_code=422, detail=f"Parameter '{bid}.{key}' must be a finite number.")
        ids.append(bid)
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="Every process block must have a unique ID.")
    connection_ids = []
    for index, connection in enumerate(connections):
        if not isinstance(connection, dict):
            raise HTTPException(status_code=422, detail=f"Connection {index + 1} must be an object.")
        required = ("id", "from_block", "from_port", "to_block", "to_port")
        missing = [key for key in required if not str(connection.get(key, "")).strip()]
        if missing:
            raise HTTPException(status_code=422, detail=f"Connection {index + 1} is missing: {', '.join(missing)}.")
        connection_ids.append(connection["id"])
    if len(connection_ids) != len(set(connection_ids)):
        raise HTTPException(status_code=422, detail="Every process connection must have a unique ID.")
    try:
        structural_errors = Flowsheet(definition).validate_structure()
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid flowsheet structure: {exc}") from exc
    if structural_errors:
        raise HTTPException(status_code=422, detail=structural_errors)
    return definition

@app.get("/api/health")
def health():
    return {"status":"ok","version":APP_VERSION,"model_version":MODEL_VERSION}

@app.get("/api/meta")
def metadata():
    return {"application":"Bio-Agri Process Simulator","version":APP_VERSION,"model_version":MODEL_VERSION,"schema_version":"1.0.0","status":"engineering screening tool"}

@app.get("/api/block-library")
def block_library():
    result = []
    for key, cls in BLOCK_REGISTRY.items():
        result.append(cls("_schema_").schema())
    return result

@app.get("/api/reference-flowsheet")
def reference_flowsheet():
    return json.loads((HERE/"flowsheet_reference.json").read_text())

@app.get("/api/alternative-flowsheet")
def alternative_flowsheet():
    return json.loads((HERE/"flowsheet_alt_separation_before_fermentation.json").read_text())

@app.post("/api/run")
def run_flowsheet(payload: FlowsheetPayload):
    definition = validate_flowsheet_definition(payload.flowsheet)
    try:
        result = Flowsheet(definition).run()
        result["dynamic_plant"] = build_native_dynamic_simulation(definition, result)
        return result
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise HTTPException(status_code=422, detail=f"The model could not calculate this flowsheet: {exc}") from exc

@app.post("/api/dynamic-plant")
def dynamic_plant(payload: FlowsheetPayload):
    definition = validate_flowsheet_definition(payload.flowsheet)
    try:
        result = Flowsheet(definition).run()
        return build_native_dynamic_simulation(definition, result)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise HTTPException(status_code=422, detail=f"The dynamic plant engine could not calculate this flowsheet: {exc}") from exc



SAVED_DIR = HERE/"saved_flowsheets"
SAVED_DIR.mkdir(exist_ok=True)

class SavePayload(BaseModel):
    name: str
    flowsheet: dict

@app.get("/api/saved")
def list_saved():
    return [{"name":p.stem} for p in sorted(SAVED_DIR.glob("*.json"))]

@app.get("/api/saved/{name}")
def load_saved(name: str):
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ")
    p = SAVED_DIR/f"{safe}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Flowsheet not found")
    return json.loads(p.read_text())

@app.post("/api/save")
def save_flowsheet(payload: SavePayload):
    safe = "".join(ch for ch in payload.name if ch.isalnum() or ch in "-_ ").strip()
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid flowsheet name")
    validate_flowsheet_definition(payload.flowsheet, allow_empty=True)
    (SAVED_DIR/f"{safe}.json").write_text(json.dumps(payload.flowsheet,indent=2), encoding="utf-8")
    return {"saved":True,"name":safe}


class ComparePayload(BaseModel):
    flowsheets: list[dict]

@app.post("/api/compare")
def compare_flowsheets(payload: ComparePayload):
    if not 2 <= len(payload.flowsheets) <= 10:
        raise HTTPException(status_code=422, detail="Comparison requires between 2 and 10 flowsheets.")
    results=[]
    for fs_def in payload.flowsheets:
        validate_flowsheet_definition(fs_def)
        fs=Flowsheet(fs_def)
        r=fs.run()
        results.append({
            "name":fs_def.get("name","Unnamed"),
            "errors":r.get("errors",[]),
            "warnings":r.get("warnings",[]),
            "material_closure":r.get("overall_material_closure",{}),
            "water_balance":r.get("water_balance",{}),
            "utilities":r.get("utility_totals",{}),
            "terminals":r.get("terminal_summary",{}),
            "open_decisions_count":len(r.get("open_decisions",[])),
            "ethanol_product_tph":r.get("terminal_component_totals",{}).get("ethanol",0.0)
        })
    return {"scenarios":results}

@app.get("/api/scenarios")
def get_scenarios():
    h=Path(__file__).resolve().parent
    return json.loads((h/"scenario_library.json").read_text())

@app.post("/api/scenario/run")
def run_scenario(payload:dict):
    h=Path(__file__).resolve().parent
    base=json.loads((h/"flowsheet_reference.json").read_text())
    overrides=payload.get("overrides",[])
    if not isinstance(overrides,list) or len(overrides)>50:
        raise HTTPException(status_code=422, detail="Scenario overrides must be a list containing no more than 50 items.")
    blocks={b["id"]:b for b in base["blocks"]}
    parsed=[]
    for item in overrides:
        try: override=ParameterOverride(**item)
        except TypeError as exc: raise HTTPException(status_code=422, detail=f"Invalid scenario override: {exc}") from exc
        if override.block_id not in blocks:
            raise HTTPException(status_code=422, detail=f"Scenario block '{override.block_id}' does not exist.")
        if override.parameter not in blocks[override.block_id].get("params",{}):
            raise HTTPException(status_code=422, detail=f"Parameter '{override.parameter}' does not exist on block '{override.block_id}'.")
        parsed.append(override)
    s=ScenarioDefinition(payload.get("name","Custom"),payload.get("description",""),parsed,payload.get("tags",["custom"]))
    try: r=ScenarioManager(base).run(s)
    except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc: raise HTTPException(status_code=422, detail=f"Scenario could not be calculated: {exc}") from exc
    r["scenario_kpis"]=extract_kpis(r);return r

@app.post("/api/sensitivity")
def run_sensitivity(payload:dict):
    h=Path(__file__).resolve().parent
    base=json.loads((h/"flowsheet_reference.json").read_text())
    block_id=str(payload.get("block_id","")).strip();parameter=str(payload.get("parameter","")).strip();values=payload.get("values",[])
    blocks={b["id"]:b for b in base["blocks"]}
    if block_id not in blocks: raise HTTPException(status_code=422, detail=f"Sensitivity block '{block_id}' does not exist.")
    if parameter not in blocks[block_id].get("params",{}): raise HTTPException(status_code=422, detail=f"Parameter '{parameter}' does not exist on block '{block_id}'.")
    if not isinstance(values,list) or not 2<=len(values)<=50: raise HTTPException(status_code=422, detail="Sensitivity requires between 2 and 50 values.")
    if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in values): raise HTTPException(status_code=422, detail="Sensitivity values must be finite numbers.")
    try: return SensitivityRunner(ScenarioManager(base)).one_at_a_time(block_id,parameter,values,payload.get("name","Sensitivity"))
    except (KeyError,TypeError,ValueError,ZeroDivisionError) as exc: raise HTTPException(status_code=422, detail=f"Sensitivity could not be calculated: {exc}") from exc

@app.post("/api/report", response_class=HTMLResponse)
def create_report(payload: FlowsheetPayload):
    definition=validate_flowsheet_definition(payload.flowsheet)
    result=Flowsheet(definition).run()
    name=escape(str(definition.get("name","Untitled flowsheet")))
    ethanol=result.get("terminal_component_totals",{}).get("ethanol",0.0)
    utilities=result.get("utility_totals",{})
    decisions=result.get("open_decisions",[])
    issue_rows="".join(f"<tr><td>{escape(str(d.get('block_name','')))}</td><td>{escape(str(d.get('status','')))}</td><td>{escape(str(d.get('note') or d.get('basis') or ''))}</td></tr>" for d in decisions)
    block_names={b.get("id"):b.get("name",b.get("id")) for b in definition.get("blocks",[])}
    stream_rows=""
    for stream in result.get("stream_register",[]):
        components=stream.get("components_tph",{})
        major=sorted(((k,v) for k,v in components.items() if isinstance(v,(int,float)) and abs(v)>1e-9),key=lambda x:abs(x[1]),reverse=True)[:4]
        component_text=", ".join(f"{escape(str(k).replace('_',' ').title())}: {v:.4f}" for k,v in major) or "-"
        temp="-" if stream.get("temperature_C") is None else f"{stream['temperature_C']:.1f}"
        pressure="-" if stream.get("pressure_bar_abs") is None else f"{stream['pressure_bar_abs']:.2f}"
        stream_rows+=f"<tr><td>{escape(str(stream.get('stream_id','')))}</td><td class='num'>{stream.get('total_tph',0):.4f}</td><td>{escape(str(stream.get('phase','-')))}</td><td class='num'>{temp}</td><td class='num'>{pressure}</td><td>{component_text}</td><td>{escape(str(stream.get('status','')))}</td></tr>"
    utility_rows=""
    for block_id,data in result.get("utilities_by_block",{}).items():
        electricity=float(data.get("electricity_kW",0) or 0); thermal=float(data.get("thermal_kW",0) or 0); cooling=float(data.get("cooling_kW",0) or 0); steam=float(data.get("steam_kgph",0) or 0)
        if max(abs(electricity),abs(thermal),abs(cooling),abs(steam))<1e-9: continue
        utility_rows+=f"<tr><td>{escape(str(block_names.get(block_id,block_id)))}</td><td class='num'>{electricity:.2f}</td><td class='num'>{thermal:.2f}</td><td class='num'>{cooling:.2f}</td><td class='num'>{steam:.1f}</td></tr>"
    utility_rows+=f"<tr class='total'><td>Plant total</td><td class='num'>{utilities.get('electricity_kW',0):.2f}</td><td class='num'>{utilities.get('thermal_kW',0):.2f}</td><td class='num'>{utilities.get('cooling_kW',0):.2f}</td><td class='num'>{utilities.get('steam_kgph',0):.1f}</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>{name} - Engineering report</title><style>@page{{size:A4 landscape;margin:12mm}}*{{box-sizing:border-box}}body{{font:11px Arial,sans-serif;color:#18202a;max-width:1200px;margin:30px auto;padding:0 20px}}h1{{margin:4px 0}}h2{{margin:24px 0 8px;border-bottom:2px solid #245f73;padding-bottom:5px}}.meta,.note{{color:#66717a}}.actions{{position:sticky;top:0;text-align:right;background:#fff;padding:8px 0}}button{{background:#0b5d78;color:#fff;border:0;border-radius:6px;padding:9px 14px;font-weight:bold;cursor:pointer}}.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}}.kpis div{{background:#eef4f6;padding:12px;border-radius:8px}}.kpis strong{{display:block;font-size:18px;margin-top:5px}}table{{width:100%;border-collapse:collapse;table-layout:auto}}thead{{display:table-header-group}}th,td{{text-align:left;border:1px solid #d9dfe2;padding:6px;vertical-align:top}}th{{background:#e8f0f3;white-space:nowrap}}td.num{{text-align:right;font-variant-numeric:tabular-nums}}tr.total td{{font-weight:bold;background:#edf6f2}}@media print{{body{{margin:0;padding:0;max-width:none}}.actions{{display:none}}h2{{break-after:avoid}}tr{{break-inside:avoid}}}}</style></head><body><div class='actions'><button onclick='window.print()'>Print / save as PDF</button></div><p class='meta'>Bio-Agri Process Simulator {APP_VERSION} · Model {MODEL_VERSION} · Engineering screening report</p><h1>{name}</h1><p class='note'>Generated from the current flowsheet. Mass flows are tonnes per hour. Results remain subject to the assumptions and open decisions below.</p><div class='kpis'><div>Ethanol<strong>{ethanol:.4f} t/h</strong></div><div>Electricity<strong>{utilities.get('electricity_kW',0):.1f} kW</strong></div><div>Process heat<strong>{utilities.get('thermal_kW',0):.1f} kW</strong></div><div>Steam<strong>{utilities.get('steam_kgph',0):.0f} kg/h</strong></div></div><h2>Run status</h2><p>{len(result.get('errors',[]))} errors · {len(result.get('warnings',[]))} warnings · {len(decisions)} open/provisional decisions</p><h2>Stream table</h2><table><thead><tr><th>Stream</th><th>Total t/h</th><th>Phase</th><th>Temp °C</th><th>Pressure bar(a)</th><th>Major components t/h</th><th>Status</th></tr></thead><tbody>{stream_rows}</tbody></table><h2>Heat and electrical data</h2><table><thead><tr><th>Process unit</th><th>Electricity kW</th><th>Heat kW</th><th>Cooling kW</th><th>Steam kg/h</th></tr></thead><tbody>{utility_rows}</tbody></table><h2>Engineering assumptions and decisions</h2><table><thead><tr><th>Block</th><th>Status</th><th>Basis / note</th></tr></thead><tbody>{issue_rows}</tbody></table></body></html>"""

@app.get("/api/physics-summary")
def get_physics_summary():
    h=Path(__file__).resolve().parent
    return json.loads((h/"v0_14_physics_summary.json").read_text())

@app.get("/api/vle-screening")
def get_vle_screening():
    h=Path(__file__).resolve().parent
    return json.loads((h/"vle_screening_results.json").read_text())

@app.get("/api/v015-summary")
def get_v015_summary():
    h=Path(__file__).resolve().parent
    return json.loads((h/"v0_15_summary.json").read_text())

@app.get("/api/dynamic-heat-profile")
def get_dynamic_heat_profile():
    h=Path(__file__).resolve().parent
    return json.loads((h/"dynamic_heat_recovery_profile.json").read_text())
