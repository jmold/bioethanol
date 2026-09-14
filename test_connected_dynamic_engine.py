import json
import unittest
from pathlib import Path

from flowsheet import Flowsheet
from connected_dynamic_engine import build_connected_dynamic_simulation

HERE=Path(__file__).resolve().parent


class ConnectedDynamicEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definition=json.loads((HERE/"flowsheet_reference.json").read_text(encoding="utf-8"))
        cls.result=Flowsheet(cls.definition).run()
        cls.connected=build_connected_dynamic_simulation(cls.definition,cls.result,timestep_min=15,horizon_h=168)

    def test_connected_engine_identity(self):
        self.assertEqual(self.connected["engine_version"],"0.23.0")
        self.assertTrue(self.connected["connected_material_transfers"])
        self.assertFalse(self.connected["intermediate_buffers_assumed"])
        self.assertNotIn("buffers",self.connected)

    def test_source_inventory_is_conserved(self):
        t=self.connected["connected_throughput"]
        self.assertAlmostEqual(t["mass_balance_error_t"],0.0,places=7)
        self.assertGreater(t["source_feed_started_t"],0.0)

    def test_transfers_are_direct_vessel_to_vessel(self):
        direct=[e for e in self.connected["event_log"] if e["event"]=="DIRECT_TRANSFER_START"]
        self.assertTrue(direct)
        self.assertTrue(all("from" in e and "to" in e for e in direct))
        self.assertFalse(any("buffer" in str(e).lower() for e in self.connected["event_log"]))

    def test_hydrolysis_accepts_partial_pretreatment_fills(self):
        transfers=[e for e in self.connected["event_log"] if e["event"]=="DIRECT_TRANSFER_COMPLETE" and e["to"].startswith("hydro-")]
        self.assertTrue(transfers)
        self.assertTrue(any(abs(e["mass_t"]-50.0)<1e-6 for e in transfers))
        partial=False
        for row in self.connected["timeline"]:
            for vid,mass in row["vessel_inventory_t"].items():
                if vid.startswith("hydro-") and 0<mass<100:
                    partial=True
        self.assertTrue(partial)

    def test_downstream_waits_for_upstream_material(self):
        states={state for row in self.connected["timeline"] for ss in row["states"].values() for state in ss}
        self.assertIn("STARVED",states)

    def test_blocking_is_possible_without_buffers(self):
        self.assertGreater(self.connected["operability"]["blocking_events"],0)
        states={state for row in self.connected["timeline"] for ss in row["states"].values() for state in ss}
        self.assertIn("BLOCKED",states)

    def test_shared_pumps_create_contention(self):
        self.assertGreater(self.connected["operability"]["pump_contention_events"],0)

    def test_end_to_end_throughput_is_from_completed_fermentation_batches(self):
        t=self.connected["connected_throughput"]
        self.assertGreater(t["source_feed_completed_t"],0.0)
        self.assertGreater(t["average_completed_feed_tph"],0.0)
        self.assertGreater(t["ethanol_product_L_per_8000h_year"],0.0)

    def test_vessel_inventory_never_exceeds_working_batch_capacity(self):
        caps={}
        for v in self.connected["operability"]["vessels"]:
            section=next(s for s in self.connected["vessel_schedules"] if s["block_id"]==v["section_id"])
            caps[v["vessel_id"]]=section["batch_mass_t"]
        for row in self.connected["timeline"]:
            for vid,mass in row["vessel_inventory_t"].items():
                self.assertGreaterEqual(mass,-1e-9)
                self.assertLessEqual(mass,caps[vid]+1e-9)



    def test_fill_levels_animate_during_transfer_states(self):
        filling_levels=[]
        emptying_levels=[]
        for row in self.connected["timeline"]:
            for vid,level in row["vessel_fill_fraction"].items():
                states=row["states"].get(vid.split("-V")[0],[])
                index=int(vid.split("-V")[1])-1 if "-V" in vid else -1
                state=states[index] if 0 <= index < len(states) else ""
                if state=="FILLING" and 0.0 < level < 1.0:
                    filling_levels.append(level)
                if state=="EMPTYING" and 0.0 < level < 1.0:
                    emptying_levels.append(level)
        self.assertTrue(filling_levels,"Expected at least one partially filled vessel while FILLING.")
        self.assertTrue(emptying_levels,"Expected at least one partially emptied vessel while EMPTYING.")


    def test_timeline_exposes_vessel_batch_and_pump_state(self):
        rows=self.connected["timeline"]
        self.assertTrue(rows)
        row=next(r for r in rows if r.get("vessel_state"))
        self.assertTrue(row["vessel_state"])
        self.assertTrue(row["vessel_fill_fraction"])
        self.assertTrue(row["vessel_batch_ids"])
        self.assertTrue(row["pump_state"])
        self.assertEqual(set(row["pump_state"]),set(row["pump_owner"]))

    def test_batch_ids_are_created_and_propagated(self):
        starts=[e for e in self.connected["event_log"] if e["event"]=="SOURCE_FILL_START"]
        transfers=[e for e in self.connected["event_log"] if e["event"]=="DIRECT_TRANSFER_START"]
        discharges=[e for e in self.connected["event_log"] if e["event"]=="FINAL_DISCHARGE_COMPLETE"]
        self.assertTrue(starts)
        self.assertTrue(all(e.get("batch_ids") for e in starts))
        self.assertTrue(transfers)
        self.assertTrue(any(e.get("batch_ids") for e in transfers))
        self.assertTrue(discharges)
        self.assertTrue(any(e.get("batch_ids") for e in discharges))

if __name__=="__main__":
    unittest.main()
