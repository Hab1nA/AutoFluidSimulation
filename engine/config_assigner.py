"""Assign pipeline configs to remote workstations."""
from __future__ import annotations


class ConfigAssigner:
    """Assign configs to workstations with a stable round-robin strategy."""

    def __init__(self, workstations: list[str], configs: list[int]) -> None:
        if not workstations:
            raise ValueError("至少需要一个工作站")
        if len(set(workstations)) != len(workstations):
            raise ValueError("工作站 ID 重复")

        self._assignment: dict[str, list[int]] = {ws: [] for ws in workstations}
        self._config_to_workstation: dict[int, str] = {}

        for index, config_name in enumerate(sorted(configs)):
            workstation_id = workstations[index % len(workstations)]
            self._assignment[workstation_id].append(config_name)
            self._config_to_workstation[config_name] = workstation_id

    def get_workstation(self, config_name: int) -> str:
        """Return the workstation assigned to one config."""
        try:
            return self._config_to_workstation[config_name]
        except KeyError as exc:
            raise ValueError(f"构型{config_name}未分配到任何工作站") from exc

    def get_configs(self, workstation_id: str) -> list[int]:
        """Return a copy of configs assigned to one workstation."""
        return list(self._assignment.get(workstation_id, []))

    def as_mapping(self) -> dict[str, list[int]]:
        """Return a copy of the full workstation-to-config mapping."""
        return {ws: list(configs) for ws, configs in self._assignment.items()}
