
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Optional

@dataclass
class EngineeringMetadata:
    status: str = "OPEN DESIGN DECISION"
    basis: str = "UNSPECIFIED"
    source: str = ""
    confidence: str = "LOW"
    note: str = ""

    def to_dict(self):
        return asdict(self)

@dataclass
class UtilityDemand:
    electricity_kW: float = 0.0
    thermal_kW: float = 0.0
    cooling_kW: float = 0.0
    steam_kgph: float = 0.0
    process_water_tph: float = 0.0
    cooling_water_tph: float = 0.0
    compressed_air_Nm3ph: float = 0.0
    nitrogen_Nm3ph: float = 0.0
    cip_water_tph: float = 0.0
    peak_electricity_kW: float = 0.0
    peak_thermal_kW: float = 0.0
    peak_cooling_kW: float = 0.0
    other: Dict[str, float] = field(default_factory=dict)

    def add(self, other: "UtilityDemand") -> "UtilityDemand":
        out = UtilityDemand()
        for f in ["electricity_kW","thermal_kW","cooling_kW","steam_kgph",
                  "process_water_tph","cooling_water_tph","compressed_air_Nm3ph",
                  "nitrogen_Nm3ph","cip_water_tph","peak_electricity_kW","peak_thermal_kW","peak_cooling_kW"]:
            setattr(out, f, getattr(self,f)+getattr(other,f))
        keys = set(self.other)|set(other.other)
        out.other = {k:self.other.get(k,0)+other.other.get(k,0) for k in keys}
        return out

    def to_dict(self):
        return asdict(self)

@dataclass
class Discharge:
    wastewater_tph: float = 0.0
    vent_gas_tph: float = 0.0
    solid_waste_tph: float = 0.0
    hazardous_waste_tph: float = 0.0
    peak_electricity_kW: float = 0.0
    peak_thermal_kW: float = 0.0
    peak_cooling_kW: float = 0.0
    other: Dict[str, float] = field(default_factory=dict)

    def add(self, other: "Discharge") -> "Discharge":
        out = Discharge(
            wastewater_tph=self.wastewater_tph+other.wastewater_tph,
            vent_gas_tph=self.vent_gas_tph+other.vent_gas_tph,
            solid_waste_tph=self.solid_waste_tph+other.solid_waste_tph,
            hazardous_waste_tph=self.hazardous_waste_tph+other.hazardous_waste_tph
        )
        keys=set(self.other)|set(other.other)
        out.other={k:self.other.get(k,0)+other.other.get(k,0) for k in keys}
        return out

    def to_dict(self):
        return asdict(self)

@dataclass
class EquipmentRequirement:
    equipment_type: str = ""
    quantity: Optional[float] = None
    working_volume_m3: Optional[float] = None
    design_flow_tph: Optional[float] = None
    design_duty_kW: Optional[float] = None
    motor_kW: Optional[float] = None
    area_m2: Optional[float] = None
    residence_time_h: Optional[float] = None
    turndown_fraction: Optional[float] = None
    note: str = ""

    def to_dict(self):
        return asdict(self)

@dataclass
class Stream:
    id: str
    components_tph: Dict[str, float] = field(default_factory=dict)
    temperature_C: Optional[float] = None
    pressure_bar_abs: Optional[float] = None
    phase: str = "mixed"
    status: str = "CALCULATED"
    note: str = ""
    density_kg_per_m3: Optional[float] = None

    @property
    def total_tph(self) -> float:
        return sum(self.components_tph.values())

    def get(self, name: str) -> float:
        return self.components_tph.get(name, 0.0)

    @property
    def volumetric_flow_m3ph(self) -> Optional[float]:
        if self.density_kg_per_m3 is None or self.density_kg_per_m3 <= 0:
            return None
        return self.total_tph * 1000.0 / self.density_kg_per_m3

    def copy(self, new_id: Optional[str] = None) -> "Stream":
        return Stream(
            id=new_id or self.id,
            components_tph=dict(self.components_tph),
            temperature_C=self.temperature_C,
            pressure_bar_abs=self.pressure_bar_abs,
            phase=self.phase,
            status=self.status,
            note=self.note,
            density_kg_per_m3=self.density_kg_per_m3
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["total_tph"] = self.total_tph
        d["volumetric_flow_m3ph"] = self.volumetric_flow_m3ph
        return d

@dataclass
class PortSpec:
    name: str
    direction: str
    stream_type: str = "material"
    required: bool = True
    description: str = ""

@dataclass
class BlockResult:
    outputs: Dict[str, Stream]
    metrics: Dict[str, Any] = field(default_factory=dict)
    utilities: UtilityDemand = field(default_factory=UtilityDemand)
    discharges: Discharge = field(default_factory=Discharge)
    equipment: List[EquipmentRequirement] = field(default_factory=list)
    metadata: EngineeringMetadata = field(default_factory=EngineeringMetadata)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

@dataclass
class Connection:
    id: str
    from_block: str
    from_port: str
    to_block: str
    to_port: str
    def to_dict(self): return asdict(self)

@dataclass
class BlockInstance:
    id: str
    type: str
    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    position: Dict[str, float] = field(default_factory=dict)
    def to_dict(self): return asdict(self)
