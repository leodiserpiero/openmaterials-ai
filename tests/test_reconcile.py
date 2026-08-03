"""Git-like semantic rebase for local map change bundles."""

from __future__ import annotations

import hashlib
import shutil

import pytest

from omai.operator.identity import canonical_json, edge_uid_from_identity, node_uid_from_identity
from omai.reconcile import MapChangeBundle, ReconciliationError, reconcile_change_bundle
from omai.store import Store


def _node(quantity: str, name: str, *, labels: dict[str, str] | None = None) -> dict:
    identity = {
        "quantity": quantity,
        "fields": [],
        "gauge": "observable",
        "labels": labels or {},
    }
    return {
        "uid": node_uid_from_identity(identity),
        "identity": identity,
        "meta": {"name": name, "symbol": name[0], "description": name, "tier": "Sources"},
    }


def _edge(name: str, inputs: list[str], output: str) -> dict:
    identity = {
        "inputs": sorted(inputs),
        "output": output,
        "outputs": [output],
        "formula": "true",
        "schemes": {},
    }
    return {
        "uid": edge_uid_from_identity(identity),
        "identity": identity,
        "meta": {
            "name": name,
            "description": name,
            "formula_srepr": "true",
            "formula_latex": r"\mathrm{true}",
            "schemes": {},
        },
    }


def _seed_store(root) -> tuple[Store, dict]:
    store = Store(root)
    base_node = _node("structure", "Structure")
    store.push("add_node", base_node, "genesis", "2026-08-01", "base")
    assert store.verify() == []
    return store, base_node


def _addition(base_node: dict, quantity: str, name: str) -> list[dict]:
    node = _node(quantity, name)
    edge = _edge(f"make_{name.lower()}", [base_node["uid"]], node["uid"])
    return [{"op": "add_node", "payload": node}, {"op": "add_edge", "payload": edge}]


def _bundle_from_fork(
    canonical: Store, root, operations: list[dict], *, reason: str
) -> MapChangeBundle:
    base = canonical.head
    shutil.copytree(canonical.root, root)
    fork = Store(root)
    fork.propose(
        operations,
        author="contributor",
        date="2026-08-02",
        reason_prefix=reason,
    )
    assert fork.verify() == []
    return MapChangeBundle.create(
        base_version=base,
        operations=operations,
        author="contributor",
        reason=reason,
        source_head=fork.head,
    )


def test_bundle_is_canonical_and_rejects_branch_local_record_fields(tmp_path) -> None:
    store, base_node = _seed_store(tmp_path / "canonical")
    operations = _addition(base_node, "potential", "Potential")
    first = MapChangeBundle.create(
        base_version=store.head,
        operations=operations,
        author="contributor",
        reason="add potential path",
    )
    reordered_payload = dict(reversed(list(operations[0]["payload"].items())))
    second = MapChangeBundle.create(
        base_version=store.head,
        operations=[{"op": "add_node", "payload": reordered_payload}, operations[1]],
        author="contributor",
        reason="add potential path",
    )

    assert first.bundle_id == second.bundle_id
    assert MapChangeBundle.from_dict(first.to_dict()) == first

    tampered = first.to_dict()
    tampered["reason"] = "different"
    with pytest.raises(ReconciliationError, match="bundle_id"):
        MapChangeBundle.from_dict(tampered)
    with pytest.raises(ReconciliationError, match="exactly op and payload"):
        MapChangeBundle.create(
            base_version=store.head,
            operations=[{"op": "add_node", "payload": operations[0]["payload"], "seq": 2}],
            author="contributor",
            reason="invalid local record",
        )


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_bundle_rejects_non_finite_json_values(tmp_path, non_finite: float) -> None:
    store, _ = _seed_store(tmp_path / "canonical")
    node = _node("potential", "Potential")
    node["meta"]["score"] = non_finite

    with pytest.raises(ReconciliationError, match="canonical-JSON-compatible"):
        MapChangeBundle.create(
            base_version=store.head,
            operations=[{"op": "add_node", "payload": node}],
            author="contributor",
            reason="invalid numeric metadata",
        )


def test_two_independent_forks_rebase_and_reapply_idempotently(tmp_path) -> None:
    canonical, base_node = _seed_store(tmp_path / "canonical")
    base = canonical.head
    first = _bundle_from_fork(
        canonical,
        tmp_path / "fork-a",
        _addition(base_node, "potential", "Potential"),
        reason="fork a",
    )
    second = _bundle_from_fork(
        canonical,
        tmp_path / "fork-b",
        _addition(base_node, "forces", "Forces"),
        reason="fork b",
    )
    assert first.base_version == second.base_version == base
    assert first.source_head != second.source_head

    first_receipt = reconcile_change_bundle(canonical, first, apply=True, date="2026-08-03")
    second_receipt = reconcile_change_bundle(canonical, second, apply=True, date="2026-08-03")
    assert first_receipt["applied"] is True
    assert second_receipt["applied"] is True
    assert len(first_receipt["record_versions"]) == 2
    assert len(second_receipt["record_versions"]) == 2
    assert first_receipt["canonical_head_after"] != first.source_head
    assert canonical.verify() == []

    head = canonical.head
    repeated = reconcile_change_bundle(canonical, second, apply=True, date="2026-08-03")
    assert repeated["applied"] is True
    assert repeated["accepted"] == []
    assert len(repeated["no_ops"]) == 2
    assert repeated["record_versions"] == []
    assert canonical.head == head
    identity = {key: value for key, value in repeated.items() if key != "receipt_id"}
    assert repeated["receipt_id"] == hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def test_added_node_can_be_equated_in_the_same_ordered_bundle(tmp_path) -> None:
    canonical, base_node = _seed_store(tmp_path / "canonical")
    added_node = _node("potential", "Potential")
    connected_edge = _edge("make_potential", [base_node["uid"]], added_node["uid"])
    operation_uids = [base_node["uid"], added_node["uid"]]
    bundle = MapChangeBundle.create(
        base_version=canonical.head,
        operations=[
            {"op": "add_node", "payload": added_node},
            {"op": "add_edge", "payload": connected_edge},
            {
                "op": "equate",
                "payload": {"uids": operation_uids, "note": "reviewed equivalence"},
            },
        ],
        author="contributor",
        reason="add and equate connected node",
    )

    dry_run = reconcile_change_bundle(canonical, bundle)
    assert dry_run["conflicts"] == []
    assert [row["op"] for row in dry_run["accepted"]] == ["add_node", "add_edge", "equate"]

    receipt = reconcile_change_bundle(canonical, bundle, apply=True, date="2026-08-03")
    assert receipt["conflicts"] == []
    assert len(receipt["record_versions"]) == 3
    assert canonical.verify() == []
    assert canonical.read()["nodes"][added_node["uid"]]["equivalent_to"] == operation_uids

    repeated = reconcile_change_bundle(canonical, bundle, apply=True, date="2026-08-03")
    assert repeated["conflicts"] == []
    assert repeated["accepted"] == []
    assert len(repeated["no_ops"]) == 3
    assert repeated["record_versions"] == []


def test_same_field_metadata_edits_conflict_but_matching_value_is_noop(tmp_path) -> None:
    canonical, base_node = _seed_store(tmp_path / "canonical")
    base = canonical.head
    operation = {
        "op": "edit_meta",
        "payload": {"uid": base_node["uid"], "meta": {"description": "local"}},
    }
    bundle = MapChangeBundle.create(
        base_version=base,
        operations=[operation],
        author="contributor",
        reason="edit description",
    )
    canonical.push(
        "edit_meta",
        {"uid": base_node["uid"], "meta": {"description": "remote"}},
        "authority",
        "2026-08-03",
        "remote edit",
    )

    receipt = reconcile_change_bundle(canonical, bundle)
    assert [conflict["code"] for conflict in receipt["conflicts"]] == ["metadata_conflict"]

    matching = MapChangeBundle.create(
        base_version=base,
        operations=[
            {
                "op": "edit_meta",
                "payload": {"uid": base_node["uid"], "meta": {"description": "remote"}},
            }
        ],
        author="contributor",
        reason="same edit",
    )
    assert reconcile_change_bundle(canonical, matching)["no_ops"]


def test_re_mint_lifecycle_and_missing_dependencies_fail_closed(tmp_path) -> None:
    canonical, base_node = _seed_store(tmp_path / "canonical")
    base = canonical.head

    reminted = _node("structure", "StructureV2", labels={"role": "matrix"})
    remint_bundle = MapChangeBundle.create(
        base_version=base,
        operations=[
            {"op": "add_node", "payload": reminted},
            {
                "op": "deprecate",
                "payload": {"uid": base_node["uid"], "note": "replaced identity"},
            },
        ],
        author="contributor",
        reason="identity change",
    )
    assert reconcile_change_bundle(canonical, remint_bundle)["conflicts"][0]["code"] == (
        "identity_decision_required"
    )

    explicit_edge = _edge("remint_structure", [base_node["uid"]], reminted["uid"])
    explicit_remint = MapChangeBundle.create(
        base_version=base,
        operations=[
            {"op": "add_node", "payload": reminted},
            {"op": "add_edge", "payload": explicit_edge},
            {
                "op": "supersede",
                "payload": {
                    "old_uids": [base_node["uid"]],
                    "new_uids": [reminted["uid"]],
                    "note": "reviewed identity replacement",
                },
            },
        ],
        author="contributor",
        reason="explicit identity change",
    )
    assert reconcile_change_bundle(canonical, explicit_remint)["conflicts"] == []
    explicit_store, _ = _seed_store(tmp_path / "explicit-remint")
    explicit_receipt = reconcile_change_bundle(
        explicit_store, explicit_remint, apply=True, date="2026-08-03"
    )
    assert explicit_receipt["conflicts"] == []
    assert len(explicit_receipt["record_versions"]) == 3
    assert explicit_store.verify() == []

    lifecycle_bundle = MapChangeBundle.create(
        base_version=base,
        operations=[
            {
                "op": "edit_meta",
                "payload": {"uid": base_node["uid"], "meta": {"description": "local"}},
            }
        ],
        author="contributor",
        reason="edit retired node",
    )
    canonical.push(
        "deprecate",
        {"uid": base_node["uid"], "note": "retired"},
        "authority",
        "2026-08-03",
        "retire",
    )
    lifecycle = reconcile_change_bundle(canonical, lifecycle_bundle)
    assert lifecycle["conflicts"][0]["code"] == "lifecycle_conflict"

    missing_output = _node("potential", "Potential")
    missing_edge = _edge("missing_input", ["f" * 64], missing_output["uid"])
    missing_bundle = MapChangeBundle.create(
        base_version=base,
        operations=[
            {"op": "add_node", "payload": missing_output},
            {"op": "add_edge", "payload": missing_edge},
        ],
        author="contributor",
        reason="missing dependency",
    )
    missing = reconcile_change_bundle(canonical, missing_bundle)
    assert any(
        conflict["code"] == "gate_rejected" and "[reachability]" in conflict["message"]
        for conflict in missing["conflicts"]
    )


def test_competing_supersede_and_equate_relations_conflict(tmp_path) -> None:
    canonical, base_node = _seed_store(tmp_path / "canonical")
    alternatives = [
        _node("potential", "PotentialA"),
        _node("forces", "Forces"),
        _node("stress", "Stress"),
    ]
    for node in alternatives:
        canonical.push("add_node", node, "genesis", "2026-08-01", "relation fixture")
    base = canonical.head

    supersede = MapChangeBundle.create(
        base_version=base,
        operations=[
            {
                "op": "supersede",
                "payload": {
                    "old_uids": [base_node["uid"]],
                    "new_uids": [alternatives[1]["uid"]],
                    "note": "local successor",
                },
            }
        ],
        author="contributor",
        reason="local supersede",
    )
    canonical.push(
        "supersede",
        {
            "old_uids": [base_node["uid"]],
            "new_uids": [alternatives[0]["uid"]],
            "note": "canonical successor",
        },
        "authority",
        "2026-08-03",
        "canonical supersede",
    )
    assert reconcile_change_bundle(canonical, supersede)["conflicts"][0]["code"] == (
        "lifecycle_conflict"
    )

    equate = MapChangeBundle.create(
        base_version=base,
        operations=[
            {
                "op": "equate",
                "payload": {
                    "uids": [alternatives[0]["uid"], alternatives[2]["uid"]],
                    "note": "local equivalence",
                },
            }
        ],
        author="contributor",
        reason="local equate",
    )
    canonical.push(
        "equate",
        {
            "uids": [alternatives[0]["uid"], alternatives[1]["uid"]],
            "note": "canonical equivalence",
        },
        "authority",
        "2026-08-03",
        "canonical equate",
    )
    assert reconcile_change_bundle(canonical, equate)["conflicts"][0]["code"] == (
        "lifecycle_conflict"
    )


def test_unknown_base_refuses_without_guessing_ancestry(tmp_path) -> None:
    canonical, _ = _seed_store(tmp_path / "canonical")
    bundle = MapChangeBundle.create(
        base_version="f" * 64,
        operations=[],
        author="contributor",
        reason="unknown base",
    )
    receipt = reconcile_change_bundle(canonical, bundle, apply=True, date="2026-08-03")
    assert receipt["applied"] is False
    assert receipt["conflicts"][0]["code"] == "unknown_base"
