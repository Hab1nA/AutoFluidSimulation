from __future__ import annotations

import pytest

from engine.config import DEFAULT_WORKSTATION_ID, STATUS_COMPLETED, STATUS_RUNNING
from engine.scheduler.workstation_slots import WorkstationSlotCoordinator
from engine.state_manager import StateManager


class TestWorkstationSlotCoordinator:
    @staticmethod
    def _setup_state(db_path):
        state = StateManager(db_path=db_path)
        state.load_configs({
            1: [1.0, 2.0, 3.0, 4.0],
            2: [5.0, 6.0, 7.0, 8.0],
            3: [9.0, 10.0, 11.0, 12.0],
        })
        return state

    def test_claim_uses_first_idle_slot_and_persists_assignment(self, tmp_db_path):
        state = self._setup_state(tmp_db_path)
        slots = WorkstationSlotCoordinator(state, ["WS-A", "WS-B", "WS-C"])

        assert slots.claim(1) == "WS-A"
        assert slots.claim(2) == "WS-B"

        assert state.get_config_workstation(1) == "WS-A"
        assert state.get_config_workstation(2) == "WS-B"
        assert slots.busy_workstations() == {"WS-A": 1, "WS-B": 2}

    def test_release_allows_reuse_of_idle_workstation(self, tmp_db_path):
        state = self._setup_state(tmp_db_path)
        slots = WorkstationSlotCoordinator(state, ["WS-A", "WS-B"])

        assert slots.claim(1) == "WS-A"
        assert slots.claim(2) == "WS-B"
        assert slots.claim(3) is None

        slots.release_config(1)

        assert slots.claim(3) == "WS-A"
        assert state.get_config_workstation(3) == "WS-A"

    def test_update_workstation_ids_adds_new_idle_slot(self, tmp_db_path):
        state = self._setup_state(tmp_db_path)
        slots = WorkstationSlotCoordinator(state, ["WS-A", "WS-B"])

        assert slots.claim(1) == "WS-A"
        assert slots.claim(2) == "WS-B"
        assert slots.claim(3) is None

        slots.update_workstation_ids(["WS-A", "WS-B", "WS-C"])

        assert slots.claim(3) == "WS-C"
        assert state.get_config_workstation(3) == "WS-C"
        assert slots.busy_workstations() == {"WS-A": 1, "WS-B": 2, "WS-C": 3}

    def test_claims_fourth_workstation_slot(self, tmp_db_path):
        state = StateManager(db_path=tmp_db_path)
        state.load_configs({
            1: [1.0, 2.0, 3.0, 4.0],
            2: [5.0, 6.0, 7.0, 8.0],
            3: [9.0, 10.0, 11.0, 12.0],
            4: [13.0, 14.0, 15.0, 16.0],
        })
        slots = WorkstationSlotCoordinator(state, ["WS-A", "WS-B", "WS-C", "WS-D"])

        assert slots.claim(1) == "WS-A"
        assert slots.claim(2) == "WS-B"
        assert slots.claim(3) == "WS-C"
        assert slots.claim(4) == "WS-D"
        assert state.get_config_workstation(4) == "WS-D"
        assert slots.busy_workstations() == {
            "WS-A": 1,
            "WS-B": 2,
            "WS-C": 3,
            "WS-D": 4,
        }

    def test_update_workstation_ids_stops_assigning_removed_idle_slot(self, tmp_db_path):
        state = self._setup_state(tmp_db_path)
        slots = WorkstationSlotCoordinator(state, ["WS-A", "WS-B"])

        slots.update_workstation_ids(["WS-B"])

        assert slots.claim(1) == "WS-B"
        slots.release_config(1)
        assert slots.claim(2) == "WS-B"
        assert state.get_config_workstation(1) == "WS-B"
        assert state.get_config_workstation(2) == "WS-B"

    def test_update_workstation_ids_preserves_removed_busy_slot_until_release(self, tmp_db_path):
        state = self._setup_state(tmp_db_path)
        slots = WorkstationSlotCoordinator(state, ["WS-A", "WS-B"])

        assert slots.claim(1) == "WS-A"
        slots.update_workstation_ids(["WS-B"])

        assert slots.busy_workstations() == {"WS-A": 1}
        assert slots.claim(2) == "WS-B"
        assert slots.claim(3) is None

        slots.release_config(1)

        assert slots.busy_workstations() == {"WS-B": 2}

    def test_update_workstation_ids_rejects_empty_or_duplicate_ids(self, tmp_db_path):
        state = self._setup_state(tmp_db_path)
        slots = WorkstationSlotCoordinator(state, ["WS-A"])

        with pytest.raises(ValueError, match="至少需要一个工作站槽位"):
            slots.update_workstation_ids([])
        with pytest.raises(ValueError, match="工作站槽位 ID 重复"):
            slots.update_workstation_ids(["WS-A", "WS-A"])

    def test_seed_from_state_restores_active_transfer_and_meshing_slots(self, tmp_db_path):
        state = self._setup_state(tmp_db_path)
        state.set_config_workstation(1, "WS-A")
        state.set_config_workstation(2, "WS-B")
        state.set_config_workstation(3, "WS-C")
        state.set_step_status(1, "transfer", STATUS_RUNNING)
        state.set_step_status(2, "meshing", STATUS_RUNNING)
        state.set_step_status(3, "meshing", STATUS_COMPLETED)

        slots = WorkstationSlotCoordinator(state, ["WS-A", "WS-B", "WS-C"])
        slots.seed_from_state()

        assert slots.busy_workstations() == {"WS-A": 1, "WS-B": 2}
        assert slots.claim(3) == "WS-C"

    def test_seed_from_state_ignores_default_in_multi_workstation_slots(self, tmp_db_path):
        state = self._setup_state(tmp_db_path)
        state.set_config_workstation(1, DEFAULT_WORKSTATION_ID)
        state.set_step_status(1, "transfer", STATUS_RUNNING)

        slots = WorkstationSlotCoordinator(state, ["WS-A", "WS-B"])
        slots.seed_from_state()

        assert slots.busy_workstations() == {}
