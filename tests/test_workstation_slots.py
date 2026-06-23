from __future__ import annotations

import os
import shutil
import tempfile

from engine.config import DEFAULT_WORKSTATION_ID, IPC_CONFIG, STATUS_COMPLETED, STATUS_RUNNING
from engine.scheduler.workstation_slots import WorkstationSlotCoordinator
from engine.state_manager import StateManager


class TestWorkstationSlotCoordinator:
    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="workstation_slots_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path
        self.state = StateManager(db_path=self.db_path)
        self.state.load_configs({
            1: [1.0, 2.0, 3.0, 4.0],
            2: [5.0, 6.0, 7.0, 8.0],
            3: [9.0, 10.0, 11.0, 12.0],
        })

    def teardown_method(self) -> None:
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_claim_uses_first_idle_slot_and_persists_assignment(self) -> None:
        slots = WorkstationSlotCoordinator(self.state, ["WS-A", "WS-B", "WS-C"])

        assert slots.claim(1) == "WS-A"
        assert slots.claim(2) == "WS-B"

        assert self.state.get_config_workstation(1) == "WS-A"
        assert self.state.get_config_workstation(2) == "WS-B"
        assert slots.busy_workstations() == {"WS-A": 1, "WS-B": 2}

    def test_release_allows_reuse_of_idle_workstation(self) -> None:
        slots = WorkstationSlotCoordinator(self.state, ["WS-A", "WS-B"])

        assert slots.claim(1) == "WS-A"
        assert slots.claim(2) == "WS-B"
        assert slots.claim(3) is None

        slots.release_config(1)

        assert slots.claim(3) == "WS-A"
        assert self.state.get_config_workstation(3) == "WS-A"

    def test_seed_from_state_restores_active_transfer_and_meshing_slots(self) -> None:
        self.state.set_config_workstation(1, "WS-A")
        self.state.set_config_workstation(2, "WS-B")
        self.state.set_config_workstation(3, "WS-C")
        self.state.set_step_status(1, "transfer", STATUS_RUNNING)
        self.state.set_step_status(2, "meshing", STATUS_RUNNING)
        self.state.set_step_status(3, "meshing", STATUS_COMPLETED)

        slots = WorkstationSlotCoordinator(self.state, ["WS-A", "WS-B", "WS-C"])
        slots.seed_from_state()

        assert slots.busy_workstations() == {"WS-A": 1, "WS-B": 2}
        assert slots.claim(3) == "WS-C"

    def test_seed_from_state_ignores_default_in_multi_workstation_slots(self) -> None:
        self.state.set_config_workstation(1, DEFAULT_WORKSTATION_ID)
        self.state.set_step_status(1, "transfer", STATUS_RUNNING)

        slots = WorkstationSlotCoordinator(self.state, ["WS-A", "WS-B"])
        slots.seed_from_state()

        assert slots.busy_workstations() == {}
