import json
import unittest
from pathlib import Path

from api import FlowsheetPayload
from api_v020 import run_flowsheet_v020, v020_meta

HERE = Path(__file__).resolve().parent


class V020ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definition = json.loads((HERE / "flowsheet_reference.json").read_text(encoding="utf-8"))

    def test_meta_identifies_connected_engine(self):
        meta = v020_meta()
        self.assertEqual(meta["model_version"], "0.20.0-alpha")
        self.assertTrue(meta["connected_material_transfers"])

    def test_normal_run_returns_connected_dynamic_payload(self):
        result = run_flowsheet_v020(FlowsheetPayload(flowsheet=self.definition))
        dynamic = result["dynamic_plant"]
        self.assertEqual(dynamic["engine_version"], "0.20.0-alpha")
        self.assertTrue(dynamic["connected_material_transfers"])
        self.assertIn("connected_throughput", dynamic)
        self.assertIn("buffers", dynamic)
        self.assertIn("operability", dynamic)
        self.assertGreater(len(dynamic["timeline"]), 0)
        self.assertIn("net_external_thermal_kW", dynamic["timeline"][0])
        self.assertIn("electrical_kW", dynamic["timeline"][0])


if __name__ == "__main__":
    unittest.main()
