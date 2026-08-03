"""External-solve requests bind graph identity before MapEngine dispatch."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from omai.execution import (
    EXTERNAL_SOLVE_REQUEST_SCHEMA,
    ExternalSolveBindingError,
    ExternalSolveRequest,
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
from omai.thermal_transport.operator.nodes import (
    ENTROPY,
    FREQUENCY_STATE,
    THERMAL_CONDUCTIVITY_DIRECT,
)
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
    live_edge = load_live_map_snapshot().edges[request.operator.uid]["identity"]
    assert isinstance(live_edge, dict)
    assert [binding.uid for binding in request.inputs] == live_edge["inputs"]
    assert [binding.uid for binding in request.outputs] == live_edge["outputs"]
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


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field_name", ["lineage", "execution"])
def test_received_request_rejects_readdressed_non_finite_json(
    non_finite: float,
    field_name: str,
) -> None:
    request = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
    )
    forged_value = {"non_finite": non_finite}
    forged = replace(request, **{field_name: forged_value})

    with pytest.raises(ExternalSolveBindingError, match="canonical-JSON-compatible"):
        validate_external_solve_request(forged.to_dict())


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


def test_received_request_rejects_readdressed_noncanonical_input_order() -> None:
    request = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
    )
    assert len(request.inputs) > 1
    reordered = replace(request, inputs=tuple(reversed(request.inputs)))

    with pytest.raises(ExternalSolveBindingError, match="inputs do not match"):
        validate_external_solve_request(reordered.to_dict())


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


def test_request_rejects_target_derived_only_from_external_inputs() -> None:
    lineage = {
        "node": ENTROPY.name,
        "node_uid": node_id(ENTROPY),
        "material": "Si",
        "conditions": {"bte_solver": "direct_inverse"},
    }
    with pytest.raises(ExternalSolveBindingError, match="not reachable downstream"):
        build_external_solve_request(
            solve_bte_direct,
            KALDO_SOLVE_BTE_DIRECT,
            lineage,
        )


def _request_with_synthetic_target(
    *,
    target_uid: str,
    target_name: str,
) -> tuple[ExternalSolveRequest, MapSnapshot]:
    request = build_external_solve_request(
        solve_bte_direct,
        KALDO_SOLVE_BTE_DIRECT,
        _lineage(),
    )
    live = load_live_map_snapshot()
    nodes = deepcopy(live.nodes)
    nodes[target_uid] = {
        "identity": {"name": target_name},
        "meta": {"name": target_name},
    }
    lineage = deepcopy(request.lineage)
    lineage["node"] = target_name
    lineage["node_uid"] = target_uid
    return (
        replace(
            request,
            target=NodeBinding(target_name, target_uid),
            lineage=lineage,
        ),
        MapSnapshot(live.version, nodes, deepcopy(live.edges)),
    )


def test_request_rejects_target_when_hyperedge_coinput_is_unreachable() -> None:
    target_uid = "1" * 64
    unavailable_potential_uid = (
        "80a4273845e09207860e06db18d1f1ee5670a9b9c8b1020a99928c872878f901"
    )
    request, snapshot = _request_with_synthetic_target(
        target_uid=target_uid,
        target_name="SyntheticTarget",
    )
    assert unavailable_potential_uid not in {
        binding.uid for binding in (*request.inputs, *request.outputs)
    }
    snapshot.edges["2" * 64] = {
        "identity": {
            "inputs": [request.outputs[0].uid, unavailable_potential_uid],
            "outputs": [target_uid],
        },
        "meta": {"name": "synthetic-multi-input-edge"},
    }

    with pytest.raises(ExternalSolveBindingError, match="not reachable downstream"):
        validate_external_solve_request(request.to_dict(), snapshot=snapshot)


def test_request_reaches_target_when_every_hyperedge_input_is_reachable() -> None:
    target_uid = "4" * 64
    intermediate_uid = "5" * 64
    request, snapshot = _request_with_synthetic_target(
        target_uid=target_uid,
        target_name="SyntheticTarget",
    )
    frontier_uid = request.outputs[0].uid
    snapshot.edges["6" * 64] = {
        "identity": {"inputs": [frontier_uid], "outputs": [intermediate_uid]},
        "meta": {"name": "synthetic-intermediate-edge"},
    }
    snapshot.edges["7" * 64] = {
        "identity": {
            "inputs": [frontier_uid, intermediate_uid],
            "outputs": [target_uid],
        },
        "meta": {"name": "synthetic-multi-input-edge"},
    }

    assert validate_external_solve_request(request.to_dict(), snapshot=snapshot) == request


def test_received_request_rejects_unaddressed_content_change() -> None:
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
