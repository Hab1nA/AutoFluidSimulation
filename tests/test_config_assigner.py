from __future__ import annotations

import pytest

from engine.config_assigner import ConfigAssigner


def test_round_robin_assigns_sorted_configs_to_workstations() -> None:
    assigner = ConfigAssigner(
        workstations=["WS-A", "WS-B", "WS-C"],
        configs=[5, 1, 3, 2],
    )

    assert assigner.get_workstation(1) == "WS-A"
    assert assigner.get_workstation(2) == "WS-B"
    assert assigner.get_workstation(3) == "WS-C"
    assert assigner.get_workstation(5) == "WS-A"
    assert assigner.get_configs("WS-A") == [1, 5]
    assert assigner.get_configs("WS-B") == [2]
    assert assigner.get_configs("WS-C") == [3]


def test_get_configs_returns_copy() -> None:
    assigner = ConfigAssigner(workstations=["WS-A"], configs=[1])

    configs = assigner.get_configs("WS-A")
    configs.append(99)

    assert assigner.get_configs("WS-A") == [1]


def test_unknown_config_raises_value_error() -> None:
    assigner = ConfigAssigner(workstations=["WS-A"], configs=[1])

    with pytest.raises(ValueError, match="未分配"):
        assigner.get_workstation(2)


def test_empty_workstations_rejected() -> None:
    with pytest.raises(ValueError, match="至少需要一个工作站"):
        ConfigAssigner(workstations=[], configs=[1])


def test_duplicate_workstations_rejected() -> None:
    with pytest.raises(ValueError, match="重复"):
        ConfigAssigner(workstations=["WS-A", "WS-A"], configs=[1])
