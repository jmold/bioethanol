import json
import unittest
from pathlib import Path

from flowsheet import Flowsheet
from connected_dynamic_engine import build_connected_dynamic_simulation

HERE = Path(__file__).resolve().parent


class ConnectedDynamicEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definition = json.loads((HERE / "flowsheet_reference.json").read_text(encoding="utf-8"))
        cls.result = Flowsheet(cls.definition).run()
        cls.connected = build_connected_dynamic_simulation(
            cls.definition,
            cls.result,
            timestep_min=15,
            horizon_h=168,
        )

    def test_connected_engine_identity(self):
        self.assertEqual(self.connected["engine_version"], "0.20.0-alpha")
        self.assertTrue(self.connected["connected_material_transfers"])
        self.assertFalse(self.connected["external_simulator_dependency"])

    def test_buffers_never_go_negative_or_above_capacity(self):
        capacities = {b["buffer_id"]: b["capacity_t"] for b in self.connected["buffers"]}
        for row in self.connected["timeline"]:
            for buffer_id, inventory in row["buffer_inventory_t"].items():
                self.assertGreaterEqual(inventory, -1e-9)
                self.assertLessEqual(inventory, capacities[buffer_id] + 1e-9)

    def test_source_inventory_is_conserved(self):
        throughput = self.connected["connected_throughput"]
        self.assertAlmostEqual(throughput["mass_balance_error_t"], 0.0, places=7)
        self.assertGreater(throughput["source_feed_started_t"], 0.0)

    def test_downstream_cannot_start_before_upstream_material_exists(self):
        event_log = self.connected["event_log"]
        hydro_fill = next(e for e in event_log if e["event"] == "FILL_START" and e["vessel"].startswith("hydro-"))
        pretreat_empty = next(e for e in event_log if e["event"] == "EMPTY_COMPLETE" and e["vessel"].startswith("pretreat-"))
        self.assertGreaterEqual(hydro_fill["time_h"], pretreat_empty["time_h"])

    def test_starvation_is_explicitly_resolved(self):
        self.assertGreater(self.connected["operability"]["starvation_events"], 0)
        states = {
            state
            for row in self.connected["timeline"]
            for section_states in row["states"].values()
            for state in section_states
        }
        self.assertIn("STARVED", states)

    def test_shared_pumps_create_contention(self):
        self.assertGreater(self.connected["operability"]["pump_contention_events"], 0)
        for pump in self.connected["shared_pumps"]:
            self.assertGreaterEqual(pump["busy_h"], 0.0)

    def test_end_to_end_throughput_is_calculated_from_completed_batches(self):
        throughput = self.connected["connected_throughput"]
        self.assertGreater(throughput["source_feed_completed_t"], 0.0)
        self.assertGreater(throughput["average_completed_feed_tph"], 0.0)
        self.assertGreater(throughput["ethanol_product_tph"], 0.0)
        self.assertGreater(throughput["ethanol_product_L_per_8000h_year"], 0.0)

    def test_finite_buffers_can_block_upstream(self):
        constrained = build_connected_dynamic_simulation(
            self.definition,
            self.result,
            timestep_min=15,
            horizon_h=168,
            buffer_capacity_batches=1.0,
        )
        self.assertGreater(constrained["operability"]["blocking_events"], 0)
        states = {
            state
            for row in constrained["timeline"]
            for section_states in row["states"].values()
            for state in section_states
        }
        self.assertIn("BLOCKED", states)


if __name__ == "__main__":
    unittest.main()
