import json
import copy
import unittest
from pathlib import Path

from flowsheet import Flowsheet
from native_dynamic_engine import build_native_dynamic_simulation

HERE = Path(__file__).resolve().parent


class NativeDynamicEngineTests(unittest.TestCase):
    def setUp(self):
        self.definition = json.loads((HERE / "flowsheet_reference.json").read_text())
        self.result = Flowsheet(self.definition).run()
        self.dynamic = build_native_dynamic_simulation(self.definition, self.result, timestep_min=30, horizon_h=48)

    def test_is_native_and_time_resolved(self):
        self.assertFalse(self.dynamic["external_simulator_dependency"])
        self.assertGreater(len(self.dynamic["timeline"]), 20)
        self.assertEqual(self.dynamic["engine_version"], "0.19.0")

    def test_three_batch_sections_are_scheduled(self):
        ids = {x["block_id"] for x in self.dynamic["vessel_schedules"]}
        self.assertTrue({"pretreat", "hydro", "ferm"}.issubset(ids))

    def test_capacity_and_kinetics_are_exposed(self):
        for row in self.dynamic["vessel_schedules"]:
            self.assertGreater(row["cycle_time_h"], 0)
            self.assertGreater(row["capacity_tph"], 0)
            self.assertGreaterEqual(row["required_vessels"], 1)
        self.assertIn("hydro", self.dynamic["kinetic_profiles"])
        self.assertIn("ferm", self.dynamic["kinetic_profiles"])
        self.assertAlmostEqual(self.dynamic["kinetic_profiles"]["hydro"]["rows"][-1]["fraction"], 0.7674, places=4)

    def test_dynamic_utilities_have_peaks(self):
        u = self.dynamic["utility_summary"]
        self.assertGreaterEqual(u["net_external_thermal_kW"]["peak"], u["net_external_thermal_kW"]["average"])
        self.assertGreaterEqual(u["electrical_kW"]["peak"], u["electrical_kW"]["average"])

    def test_transfer_times_are_set_by_selected_pump_capacity(self):
        blocks = {b["id"]: b for b in self.definition["blocks"]}
        for schedule in self.dynamic["vessel_schedules"]:
            params = blocks[schedule["block_id"]]["params"]
            expected_fill = schedule["vessel_working_volume_m3"] / params["inlet_transfer_pump_rate_m3ph"]
            expected_empty = schedule["vessel_working_volume_m3"] / params["outlet_transfer_pump_rate_m3ph"]
            self.assertAlmostEqual(schedule["fill_time_h"], expected_fill, places=8)
            self.assertAlmostEqual(schedule["empty_time_h"], expected_empty, places=8)

    def test_larger_transfer_pumps_reduce_cycle_time(self):
        faster = copy.deepcopy(self.definition)
        hydro = next(b for b in faster["blocks"] if b["id"] == "hydro")
        hydro["params"]["inlet_transfer_pump_rate_m3ph"] *= 2
        hydro["params"]["outlet_transfer_pump_rate_m3ph"] *= 2
        faster_result = Flowsheet(faster).run()
        faster_dynamic = build_native_dynamic_simulation(faster, faster_result, timestep_min=30, horizon_h=48)
        original = next(v for v in self.dynamic["vessel_schedules"] if v["block_id"] == "hydro")
        changed = next(v for v in faster_dynamic["vessel_schedules"] if v["block_id"] == "hydro")
        self.assertLess(changed["cycle_time_h"], original["cycle_time_h"])
        self.assertGreater(changed["capacity_tph"], original["capacity_tph"])

    def test_transfer_pump_motor_load_is_applied_to_transfer_phase(self):
        powered = copy.deepcopy(self.definition)
        hydro = next(b for b in powered["blocks"] if b["id"] == "hydro")
        hydro["params"]["inlet_transfer_pump_kW"] = 18.5
        powered_result = Flowsheet(powered).run()
        dynamic = build_native_dynamic_simulation(powered, powered_result, timestep_min=30, horizon_h=48)
        schedule = next(v for v in dynamic["vessel_schedules"] if v["block_id"] == "hydro")
        filling = next(p for p in schedule["phases"] if p["name"] == "FILLING")
        self.assertEqual(filling["electricity_kW"], 18.5)

    def test_explicit_non_positive_pump_capacity_is_rejected(self):
        invalid = copy.deepcopy(self.definition)
        hydro = next(b for b in invalid["blocks"] if b["id"] == "hydro")
        hydro["params"]["inlet_transfer_pump_rate_m3ph"] = 0
        with self.assertRaisesRegex(ValueError, "must be greater than zero"):
            build_native_dynamic_simulation(invalid, Flowsheet(invalid).run())


if __name__ == "__main__":
    unittest.main()
