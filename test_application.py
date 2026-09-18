import json
import unittest
from pathlib import Path

from api import FlowsheetPayload, create_report, validate_flowsheet_definition
from fastapi import HTTPException
from flowsheet import Flowsheet
from blocks import BLOCK_REGISTRY
from scenario_engine import ScenarioManager, SensitivityRunner, extract_kpis

HERE = Path(__file__).resolve().parent


class ApplicationRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = json.loads((HERE / "flowsheet_reference.json").read_text(encoding="utf-8"))
        cls.alternative = json.loads((HERE / "flowsheet_alt_separation_before_fermentation.json").read_text(encoding="utf-8"))

    def test_reference_route_closes_and_matches_ethanol_baseline(self):
        result = Flowsheet(self.reference).run()
        self.assertEqual(result["errors"], [])
        json.dumps(result, allow_nan=False)
        self.assertAlmostEqual(result["overall_material_closure"]["closure_error_tph"], 0.0, places=9)
        self.assertAlmostEqual(result["water_balance"]["reaction_adjusted_closure_error_tph"], 0.0, places=9)
        self.assertAlmostEqual(result["terminal_component_totals"]["ethanol"], 0.512321410609, places=10)

    def test_alternative_route_runs_without_errors(self):
        result = Flowsheet(self.alternative).run()
        self.assertEqual(result["errors"], [])
        self.assertGreater(result["terminal_component_totals"]["ethanol"], 0)

    def test_sensitivity_changes_ethanol_output(self):
        rows = SensitivityRunner(ScenarioManager(self.reference)).one_at_a_time(
            "hydro", "glucan_to_glucose_conversion_fraction", [0.65, 0.85]
        )
        self.assertEqual(len(rows), 2)
        self.assertLess(rows[0]["kpis"]["ethanol_product_tph"], rows[1]["kpis"]["ethanol_product_tph"])

    def test_kpi_extraction_contains_required_plant_metrics(self):
        kpis = extract_kpis(Flowsheet(self.reference).run())
        for key in ("ethanol_product_tph", "electrical_kW", "thermal_kW", "steam_kgph", "wastewater_tph"):
            self.assertIn(key, kpis)

    def test_global_operating_basis_controls_annual_energy(self):
        definition = json.loads(json.dumps(self.reference))
        definition["operating_basis"] = {"hours_per_day": 20, "days_per_year": 300}
        result = Flowsheet(definition).run()
        self.assertEqual(result["operating_basis"]["annual_operating_hours"], 6000)
        macerator = result["block_results"]["macerator"]["metrics"]
        self.assertAlmostEqual(macerator["annual_electricity_kWh"], macerator["applied_electrical_load_kW"] * 6000)

    def test_sources_run_and_maceration_split_closes(self):
        result = Flowsheet(self.reference).run()
        self.assertIn("raw_feed", result["block_results"])
        self.assertIn("site_process_water", result["block_results"])
        macerator = result["block_results"]["macerator"]["metrics"]
        self.assertGreater(macerator["main_outlet_tph"], 0)
        self.assertGreater(macerator["reject_total_tph"], 0)
        self.assertAlmostEqual(macerator["reject_fraction"], 0.005)
        self.assertAlmostEqual(macerator["closure_error_tph"], 0.0, places=12)

    def test_process_water_tank_aggregates_demand_and_recovery(self):
        definition = json.loads(json.dumps(self.reference))
        tank = next(b for b in definition["blocks"] if b["id"] == "site_process_water")
        tank["params"]["recovered_water_tph"] = 5.0
        result = Flowsheet(definition).run()
        water = result["site_process_water"]
        self.assertTrue(water["configured"])
        self.assertAlmostEqual(water["total_demand_tph"], 11.413235294098625, places=9)
        self.assertAlmostEqual(water["recovered_water_used_tph"], 5.0)
        self.assertAlmostEqual(water["fresh_water_makeup_tph"], 6.413235294098625, places=9)
        self.assertGreater(water["required_working_volume_m3"], 0)
        self.assertEqual(water["consumers"][0]["block_id"], "feed")

    def test_feed_water_manual_mode_is_reported_to_site_tank(self):
        definition = json.loads(json.dumps(self.reference))
        feed = next(b for b in definition["blocks"] if b["id"] == "feed")
        feed["params"].update({"water_demand_mode": "manual", "manual_process_water_tph": 9.0})
        result = Flowsheet(definition).run()
        self.assertAlmostEqual(result["site_process_water"]["total_demand_tph"], 9.0)
        self.assertEqual(result["site_process_water"]["consumers"][0]["mode"], "manual")
        self.assertTrue(any("Manual process water" in warning for warning in result["warnings"]))

    def test_block_capabilities_are_exposed_for_generic_engine_consumers(self):
        from blocks import PretreatmentBlock, BeerColumnBlock, ProductSinkBlock
        self.assertIn("batch_process", PretreatmentBlock("x").schema()["capabilities"])
        self.assertEqual(PretreatmentBlock("x").schema()["schedule_adapter"],"pretreatment")
        self.assertIn("distillation", BeerColumnBlock("x").schema()["capabilities"])
        self.assertIn("sink", ProductSinkBlock("x").schema()["capabilities"])

    def test_block_schema_exposes_stable_catalogue_metadata(self):
        tank = BLOCK_REGISTRY["process_water_tank"]("schema").schema()
        self.assertEqual(tank["type_id"], "process_water_tank")
        self.assertEqual(tank["catalogue_group"], "Utilities")
        self.assertEqual(tank["model_role"], "standard_equipment")

    def test_empty_flowsheet_is_rejected_with_422(self):
        with self.assertRaises(HTTPException) as caught:
            validate_flowsheet_definition({"name": "Empty", "blocks": [], "connections": []})
        self.assertEqual(caught.exception.status_code, 422)

    def test_duplicate_block_ids_are_rejected(self):
        broken = json.loads(json.dumps(self.reference))
        broken["blocks"][1]["id"] = broken["blocks"][0]["id"]
        with self.assertRaises(HTTPException) as caught:
            validate_flowsheet_definition(broken)
        self.assertEqual(caught.exception.status_code, 422)

    def test_unknown_connection_port_is_rejected(self):
        broken = json.loads(json.dumps(self.reference))
        broken["connections"][0]["from_port"] = "not_a_port"
        with self.assertRaises(HTTPException) as caught:
            validate_flowsheet_definition(broken)
        self.assertEqual(caught.exception.status_code, 422)

    def test_report_contains_engineering_tables_and_pdf_control(self):
        report = create_report(FlowsheetPayload(flowsheet=self.reference))
        for text in ("Stream table", "Heat and electrical data", "Plant total", "Print / save as PDF"):
            self.assertIn(text, report)


if __name__ == "__main__":
    unittest.main()
