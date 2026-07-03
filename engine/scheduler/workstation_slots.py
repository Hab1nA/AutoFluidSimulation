from __future__ import annotations

"""Meshing workstation slot coordination."""

import threading

from engine.config import (
    STATUS_PAUSED,
    STATUS_RETRYING,
    STATUS_RUNNING,
    STATUS_WAITING,
)
from engine.state_manager import StateManager


class WorkstationSlotCoordinator:
    """Track workstation slots reserved for Transfer-to-Meshing work."""

    _ACTIVE_TRANSFER_STATUSES = {
        STATUS_RUNNING,
        STATUS_PAUSED,
        STATUS_RETRYING,
    }
    _ACTIVE_MESHING_STATUSES = {
        STATUS_WAITING,
        STATUS_RUNNING,
        STATUS_PAUSED,
        STATUS_RETRYING,
    }

    def __init__(self, state_manager: StateManager, workstation_ids: list[str]) -> None:
        if not workstation_ids:
            raise ValueError("至少需要一个工作站槽位")
        if len(set(workstation_ids)) != len(workstation_ids):
            raise ValueError("工作站槽位 ID 重复")
        self.state = state_manager
        self._workstation_ids = list(workstation_ids)
        self._busy_by_workstation: dict[str, int] = {}
        self._lock = threading.RLock()

    def busy_workstations(self) -> dict[str, int]:
        """Return a snapshot of currently reserved workstation slots."""
        with self._lock:
            return dict(self._busy_by_workstation)

    def update_workstation_ids(self, workstation_ids: list[str]) -> None:
        """Refresh assignable workstation IDs without dropping active reservations."""
        if not workstation_ids:
            raise ValueError("至少需要一个工作站槽位")
        if len(set(workstation_ids)) != len(workstation_ids):
            raise ValueError("工作站槽位 ID 重复")
        with self._lock:
            self._workstation_ids = list(workstation_ids)

    def claim(self, config_name: int) -> str | None:
        """Reserve the first idle workstation and persist it on the config."""
        config_name = int(config_name)
        with self._lock:
            existing = self._workstation_for_active_config(config_name)
            if existing is not None:
                return existing

            for workstation_id in self._workstation_ids:
                if workstation_id in self._busy_by_workstation:
                    continue
                self._busy_by_workstation[workstation_id] = config_name
                self.state.set_config_workstation(config_name, workstation_id)
                return workstation_id
        return None

    def release_config(self, config_name: int) -> None:
        """Release whichever workstation is reserved by a config."""
        config_name = int(config_name)
        with self._lock:
            for workstation_id, busy_config in list(self._busy_by_workstation.items()):
                if busy_config == config_name:
                    self._busy_by_workstation.pop(workstation_id, None)

    def release_workstation(self, workstation_id: str, config_name: int | None = None) -> None:
        """Release one workstation slot, optionally only when owned by a config."""
        with self._lock:
            busy_config = self._busy_by_workstation.get(workstation_id)
            if busy_config is None:
                return
            if config_name is not None and busy_config != int(config_name):
                return
            self._busy_by_workstation.pop(workstation_id, None)

    def move_config(self, config_name: int, target_workstation_id: str) -> tuple[bool, str]:
        """Move one config assignment, preserving active slot exclusivity."""
        config_name = int(config_name)
        with self._lock:
            if target_workstation_id not in self._workstation_ids:
                return False, f"目标工作站无效或未配置: {target_workstation_id}"

            source_workstation_id = self._workstation_for_active_config(config_name)
            owns_active_slot = source_workstation_id is not None
            if not owns_active_slot and self._is_config_slot_active(config_name):
                persisted = self.state.get_config_workstation(config_name)
                if persisted in self._workstation_ids:
                    source_workstation_id = persisted
                    owns_active_slot = True
                    self._busy_by_workstation.setdefault(persisted, config_name)

            target_owner = self._busy_by_workstation.get(target_workstation_id)
            if target_owner is not None and target_owner != config_name:
                return (
                    False,
                    f"目标工作站 {target_workstation_id} 已被构型{target_owner}占用",
                )

            if owns_active_slot:
                for workstation_id, busy_config in list(self._busy_by_workstation.items()):
                    if busy_config == config_name:
                        self._busy_by_workstation.pop(workstation_id, None)
                self._busy_by_workstation[target_workstation_id] = config_name

            self.state.set_config_workstation(config_name, target_workstation_id)
            return True, ""

    def clear(self) -> None:
        """Release all in-memory slot reservations."""
        with self._lock:
            self._busy_by_workstation.clear()

    def seed_from_state(self) -> None:
        """Restore slot reservations for transfer/meshing work after restart."""
        with self._lock:
            self._busy_by_workstation.clear()
            for config_name in self.state.get_all_configs():
                workstation_id = self.state.get_config_workstation(config_name)
                if workstation_id not in self._workstation_ids:
                    continue
                if self._is_config_slot_active(config_name):
                    self._busy_by_workstation.setdefault(workstation_id, int(config_name))

    def _workstation_for_active_config(self, config_name: int) -> str | None:
        for workstation_id, busy_config in self._busy_by_workstation.items():
            if busy_config == config_name:
                return workstation_id
        return None

    def _is_config_slot_active(self, config_name: int) -> bool:
        transfer_status = self.state.get_step_status(config_name, "transfer")
        meshing_status = self.state.get_step_status(config_name, "meshing")
        return (
            transfer_status in self._ACTIVE_TRANSFER_STATUSES
            or meshing_status in self._ACTIVE_MESHING_STATUSES
        )
