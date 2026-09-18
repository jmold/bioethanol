import json
import unittest
from pathlib import Path

from flowsheet import Flowsheet

HERE=Path(__file__).resolve().parent

class SpreadsheetAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definition=json.loads((HERE/"flowsheet_reference.json").read_text(encoding="utf-8"))
        cls.result=Flowsheet(cls.definition).run()
        cls.blocks={b["id"]:b for b in cls.definition["blocks"]}

    def test_p01_to_p12_reference_structure(self):
        names={b["id"]:b["name"] for b in self.definition["blocks"]}
        self.assertTrue(names["macerator"].startswith("P01A"))
        self.assertTrue(names["feed"].startswith("P01B"))
        self.assertTrue(names["pretreat"].startswith("P02"))
        self.assertTrue(names["pretreat_heat_recovery"].startswith("P03"))
        self.assertTrue(names["hydro"].startswith("P04"))
        self.assertTrue(names["ferm"].startswith("P05"))
        self.assertTrue(names["sep"].startswith("P06"))
        self.assertTrue(names["beer_cond"].startswith("P07"))
        self.assertTrue(names["beer"].startswith("P08"))
        self.assertTrue(names["rect"].startswith("P09"))
        self.assertTrue(names["sieve"].startswith("P10"))
        self.assertTrue(names["ethanol_product"].startswith("P11"))
        self.assertTrue(names["dist_util"].startswith("P12"))
        self.assertNotIn("pretreat_feed_heater",names)

    def test_p03_is_between_p02_and_p04(self):
        pairs={(c["from_block"],c["to_block"]) for c in self.definition["connections"]}
        self.assertIn(("pretreat","pretreat_heat_recovery"),pairs)
        self.assertIn(("pretreat_heat_recovery","hydro"),pairs)
        self.assertNotIn(("pretreat","hydro"),pairs)

    def test_dry_size_reduction_precedes_slurry_makeup(self):
        connections={(c["from_block"],c["to_block"]) for c in self.definition["connections"]}
        self.assertIn(("raw_feed","macerator"),connections)
        self.assertIn(("macerator","feed"),connections)
        self.assertIn(("feed","feed_transfer_pump"),connections)
        self.assertNotIn(("raw_feed","feed"),connections)
        self.assertNotIn(("feed","macerator"),connections)

    def test_feed_process_water_is_an_explicit_stream(self):
        connections={(c["from_block"],c["from_port"],c["to_block"],c["to_port"]) for c in self.definition["connections"]}
        self.assertIn(("site_process_water","process_water","feed","process_water"),connections)
        water=self.result["block_results"]["site_process_water"]["metrics"]
        feed=self.result["block_results"]["feed"]["metrics"]
        self.assertAlmostEqual(water["site_process_water_demand_tph"],11.413235294098625,places=9)
        self.assertAlmostEqual(feed["process_water_addition_tph"],water["site_process_water_demand_tph"],places=9)

    def test_p03_workbook_heat_basis(self):
        self.assertFalse(self.result["errors"],self.result["errors"])
        m=self.result["block_results"]["pretreat_heat_recovery"]["metrics"]
        self.assertAlmostEqual(m["gross_sensible_heat_kW"],2568.260833,places=3)
        self.assertAlmostEqual(m["recovered_heat_kW"],1926.195625,places=3)
        self.assertAlmostEqual(m["design_external_heat_kW"],738.374990,places=3)
        self.assertAlmostEqual(m["residual_cooling_kW"],37.768542,places=3)
        self.assertAlmostEqual(m["equivalent_cold_feed_preheat_C"],137.5,places=2)

    def test_distillation_design_parameters_from_master_sheet(self):
        beer=self.blocks["beer"]["params"]
        self.assertEqual(beer["actual_trays"],32)
        self.assertAlmostEqual(beer["overall_tray_efficiency_fraction"],0.48)
        self.assertEqual(beer["feed_tray_from_top"],4)
        self.assertEqual(beer["vapour_side_draw_tray_from_top"],8)
        self.assertAlmostEqual(beer["molar_reflux_ratio"],3.0)
        rect=self.blocks["rect"]["params"]
        self.assertEqual(rect["actual_trays"],45)
        self.assertAlmostEqual(rect["overall_tray_efficiency_fraction"],0.76)
        self.assertEqual(rect["beer_side_draw_feed_tray_from_top"],33)
        self.assertEqual(rect["molecular_sieve_recycle_tray_from_top"],14)
        self.assertAlmostEqual(rect["molar_reflux_ratio"],3.5)

    def test_p10_recycle_is_explicitly_specified_as_tear_stream(self):
        recycle=self.blocks["sieve_recycle"]["params"]
        self.assertEqual(recycle["destination"],"P09 Rectification")
        self.assertEqual(recycle["destination_tray_from_top"],14)
        self.assertAlmostEqual(recycle["recycle_ethanol_wt_fraction"],0.72)
        self.assertIn("CONVERGED INTERNAL RECYCLE",recycle["status"])



    def test_p07_economiser_closes_workbook_cold_side_duty(self):
        p07=self.result["block_results"]["beer_cond"]["metrics"]
        self.assertAlmostEqual(p07["required_preheat_kW"],774.1690284,places=5)
        self.assertAlmostEqual(p07["economiser_recovery_kW"],774.1690284,places=5)
        self.assertAlmostEqual(p07["external_trim_heat_kW"],0.0,places=8)
        self.assertAlmostEqual(p07["economiser_recovery_fraction"],1.0,places=8)

    def test_p09_p10_recycle_converges_and_preserves_workbook_product(self):
        self.assertFalse(self.result["errors"],self.result["errors"])
        rect=self.result["block_results"]["rect"]["metrics"]
        sieve=self.result["block_results"]["sieve"]["metrics"]
        self.assertTrue(rect["recycle_converged"])
        self.assertTrue(sieve["recycle_converged"])
        self.assertGreater(rect["converged_recycle_tph"],0.0)
        self.assertAlmostEqual(sieve["calculated_recycle_ethanol_wt_fraction"],0.72,places=9)
        ethanol_tph=sieve["ethanol_product_tph"]
        ethanol_lph=ethanol_tph*1000/0.78937
        self.assertAlmostEqual(ethanol_lph,643.8334866,places=4)
        self.assertAlmostEqual(self.result["overall_material_closure"]["closure_error_tph"],0.0,places=8)


    def test_p08_shortcut_distillation_is_active(self):
        self.assertFalse(self.result["errors"],self.result["errors"])
        p08=self.result["block_results"]["beer"]["metrics"]
        sc=p08["shortcut_distillation"]
        self.assertGreater(sc["relative_volatility"],1.0)
        self.assertGreater(sc["minimum_stages_fenske"],0.0)
        self.assertGreaterEqual(sc["minimum_reflux_underwood"],0.0)
        self.assertEqual(sc["required_theoretical_stages_gilliland"],"Infinity")
        self.assertEqual(sc["stage_margin"],"-Infinity")
        self.assertAlmostEqual(sc["installed_effective_stages"],32*0.48,places=8)
        self.assertGreater(p08["reboiler_kW"],0.0)
        self.assertGreater(p08["condenser_kW"],0.0)

    def test_p09_shortcut_distillation_is_active(self):
        self.assertFalse(self.result["errors"],self.result["errors"])
        p09=self.result["block_results"]["rect"]["metrics"]
        sc=p09["shortcut_distillation"]
        self.assertGreater(sc["relative_volatility"],1.0)
        self.assertGreater(sc["minimum_stages_fenske"],0.0)
        self.assertGreaterEqual(sc["minimum_reflux_underwood"],0.0)
        self.assertGreater(sc["required_theoretical_stages_gilliland"],0.0)
        self.assertAlmostEqual(sc["installed_effective_stages"],45*0.76,places=8)
        self.assertGreater(p09["reboiler_kW"],0.0)
        self.assertGreater(p09["condenser_kW"],0.0)

    def test_distillation_shortcut_exposes_internal_design_traffic(self):
        result = Flowsheet(self.definition).run()
        for bid in ("beer","rect"):
            metrics=result["block_results"][bid]["metrics"]
            shortcut=metrics.get("shortcut_distillation") or {}
            self.assertGreater(shortcut.get("internal_vapour_tph",0),0)
            self.assertGreaterEqual(shortcut.get("estimated_internal_liquid_tph",0),0)
            self.assertGreater(shortcut.get("distillate_tph",0),0)

    def test_distillation_design_summary_is_supplier_facing_and_generic(self):
        summary=self.result["distillation_design_summary"]
        self.assertEqual([x["block_id"] for x in summary["columns"]],["beer","rect"])
        self.assertTrue(all(x["internal_vapour_tph"]>0 for x in summary["columns"]))
        self.assertIn("turndown and startup requirements",summary["next_vendor_inputs"])

    def test_distillation_shortcut_preserves_workbook_product_basis(self):
        sieve=self.result["block_results"]["sieve"]["metrics"]
        ethanol_lph=sieve["ethanol_product_tph"]*1000/0.78937
        self.assertAlmostEqual(ethanol_lph,643.8334866,places=4)

if __name__=="__main__":
    unittest.main()
