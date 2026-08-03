"""External-solve requests bind graph identity before MapEngine dispatch."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from omai.execution import (
    EXTERNAL_SOLVE_REQUEST_SCHEMA,
    ExternalSolveBindingError,
    MapSnapshot,
    NodeBinding,
    build_external_solve_request,
    load_live_map_snapshot,
    validate_external_solve_request,
)
from omai.lineages import lineage_id
from omai.operator.identity import node_id
from omai.thermal_transport.operator.edges import (
    contract_kappa_direct,
    solve_bte_direct,
)
from omai.thermal_transport.operator.nodes import FREQUENCY_STATE, THERMAL_CONDUCTIVITY_DIRECT
from omai.thermal_transport.representation.kaldo import (
    KALDO_CONTRACT_KAPPA_DIRECT,
    KALDO_SOLVE_BTE_DIRECT,
)


def _lineage() -> dict[str, object]:
    return {
        "node": THERMAL_CONDUCTIVITY_DIRECT.name,
        "node_uid": node_id(THERMAL_CONDUCTIVITY_DIRECT),
        "material": "Si",
        "conditions": {"bte_solver": "direct_inverse", "temperature_K": 300.0},
    }


def test_build_request_pins_live_graph_and_kaldo_capability() -> None:
    request = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
        execution={"runtime": "fake", "attempt": 1},
    )
    payload = request.to_dict()

    assert payload["schema"] == EXTERNAL_SOLVE_REQUEST_SCHEMA
    assert payload["map_version"] == load_live_map_snapshot().version
    assert payload["operator"] == {
        "name": "solve_bte[bte_solver=direct_inverse]",
        "uid": request.operator.uid,
    }
    assert payload["representation"] == {
        "name": "kaldo",
        "parameter_units": {},
        "schemes": {"bte_solver": "direct_inverse", "symmetry_group": "C1"},
        "discretization": {
            "collision_matrix_assembly": "full_grid",
            "linear_solver": "scipy.linalg.solve",
        },
    }
    assert payload["lineage_id"] == lineage_id(_lineage())
    assert validate_external_solve_request(payload) == request


def test_request_is_deterministic_but_execution_does_not_change_lineage_identity() -> None:
    first = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
        execution={"attempt": 1, "runtime": "fake"},
    )
    reordered = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
        execution={"runtime": "fake", "attempt": 1},
    )
    rerun = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
        execution={"runtime": "fake", "attempt": 2},
    )

    assert first.request_id == reordered.request_id
    assert first.to_dict() == reordered.to_dict()
    assert first.request_id != rerun.request_id
    assert first.lineage_id == rerun.lineage_id == lineage_id(_lineage())


def test_request_rejects_locally_executable_edge() -> None:
    with pytest.raises(ExternalSolveBindingError, match="locally executable"):
        build_external_solve_request(
            contract_kappa_direct,
            KALDO_CONTRACT_KAPPA_DIRECT,
            _lineage(),
        )


def test_request_rejects_missing_target_pin() -> None:
    lineage = _lineage()
    del lineage["node_uid"]
    with pytest.raises(ExternalSolveBindingError, match="lineage.node_uid"):
        build_external_solve_request(
            solve_bte_direct,
            KALDO_SOLVE_BTE_DIRECT,
            lineage,
        )


def test_received_request_rejects_stale_map_version_before_dispatch() -> None:
    request = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
    )
    # Re-address the envelope after changing its pin so it is internally
    # self-consistent. It must still fail against the receiver's live map.
    stale = replace(request, map_version="0" * 64)
    with pytest.raises(ExternalSolveBindingError, match="stale map_version"):
        validate_external_solve_request(stale.to_dict())


def test_received_request_rejects_unknown_or_superseded_bindings() -> None:
    request = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
    )
    live = load_live_map_snapshot()

    missing_edges = deepcopy(live.edges)
    del missing_edges[request.operator.uid]
    missing = MapSnapshot(live.version, deepcopy(live.nodes), missing_edges)
    with pytest.raises(ExternalSolveBindingError, match="is not present in map"):
        validate_external_solve_request(request.to_dict(), snapshot=missing)

    superseded_nodes = deepcopy(live.nodes)
    superseded_nodes[request.target.uid]["superseded_by"] = ["f" * 64]
    superseded = MapSnapshot(live.version, superseded_nodes, deepcopy(live.edges))
    with pytest.raises(ExternalSolveBindingError, match="is superseded"):
        validate_external_solve_request(request.to_dict(), snapshot=superseded)


def test_received_request_rejects_nodes_that_do_not_belong_to_operator() -> None:
    request = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
    )
    wrong_inputs = (NodeBinding(request.target.name, request.target.uid), *request.inputs[1:])
    forged = replace(request, inputs=wrong_inputs)

    with pytest.raises(ExternalSolveBindingError, match="inputs do not match"):
        validate_external_solve_request(forged.to_dict())


def test_request_rejects_live_but_unreachable_target() -> None:
    lineage = {
        "node": FREQUENCY_STATE.name,
        "node_uid": node_id(FREQUENCY_STATE),
        "material": "Si",
        "conditions": {"bte_solver": "direct_inverse"},
    }
    with pytest.raises(ExternalSolveBindingError, match="not reachable downstream"):
        build_external_solve_request(
            solve_bte_direct,
            KALDO_SOLVE_BTE_DIRECT,
            lineage,
        )


def test_received_request_rejects_tampering() -> None:
    payload = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
    ).to_dict()
    representation = deepcopy(payload["representation"])
    assert isinstance(representation, dict)
    representation["name"] = "unregistered"
    payload["representation"] = representation

    with pytest.raises(ExternalSolveBindingError, match="request_id does not recompute"):
        validate_external_solve_request(payload)
