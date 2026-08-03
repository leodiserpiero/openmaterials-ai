"""Portable local map changes and deterministic reconciliation.

A bundle carries semantic operations, not branch-local log records.  The
canonical store replays accepted operations through :meth:`Store.propose`, so
it assigns the authoritative sequence, parent, and version hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from omai.gates import validate_contribution
from omai.operator.identity import canonical_json
from omai.store import CHANGE_OPS, GENESIS_PREV, Store

__all__ = [
    "MAP_CHANGE_BUNDLE_SCHEMA",
    "MAP_RECONCILIATION_RECEIPT_SCHEMA",
    "MapChangeBundle",
    "ReconciliationError",
    "reconcile_change_bundle",
]

MAP_CHANGE_BUNDLE_SCHEMA = "openmaterials.map-change-bundle@1"
MAP_RECONCILIATION_RECEIPT_SCHEMA = "openmaterials.map-reconciliation-receipt@1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReconciliationError(ValueError):
    """The bundle or resulting canonical chain is malformed."""


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ReconciliationError(f"{field} must be 64 lowercase hex characters")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationError(f"{field} must be a non-empty string")
    return value


def _json_copy(value: object, field: str) -> object:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(f"{field} must be canonical-JSON-compatible") from exc


def _operation(raw: object, index: int) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {"op", "payload"}:
        raise ReconciliationError(f"operations[{index}] must contain exactly op and payload")
    op = raw["op"]
    if op not in CHANGE_OPS:
        raise ReconciliationError(f"operations[{index}].op is not a supported map operation")
    payload = _json_copy(raw["payload"], f"operations[{index}].payload")
    if not isinstance(payload, dict):
        raise ReconciliationError(f"operations[{index}].payload must be an object")
    prefix = f"operations[{index}].payload"
    if op in ("add_node", "add_edge"):
        if set(payload) != {"uid", "identity", "meta"}:
            raise ReconciliationError(f"{prefix} must contain exactly uid, identity, and meta")
        _sha256(payload["uid"], f"{prefix}.uid")
        if not isinstance(payload["identity"], dict) or not isinstance(payload["meta"], dict):
            raise ReconciliationError(f"{prefix}.identity and .meta must be objects")
    elif op == "edit_meta":
        if set(payload) != {"uid", "meta"} or not isinstance(payload["meta"], dict):
            raise ReconciliationError(f"{prefix} must contain uid and an object meta")
        _sha256(payload["uid"], f"{prefix}.uid")
    elif op == "deprecate":
        if set(payload) not in ({"uid"}, {"uid", "note"}):
            raise ReconciliationError(f"{prefix} must contain uid and optional note")
        _sha256(payload["uid"], f"{prefix}.uid")
    else:
        uid_key = "uids" if op == "equate" else "old_uids"
        expected = {uid_key, "note"} if op == "equate" else {"old_uids", "new_uids", "note"}
        required = {uid_key} if op == "equate" else {"old_uids", "new_uids"}
        if not required.issubset(payload) or not set(payload).issubset(expected):
            raise ReconciliationError(f"{prefix} has malformed {op} fields")
        uid_lists = [payload[uid_key]]
        if op == "supersede":
            uid_lists.append(payload["new_uids"])
        if any(not isinstance(uids, list) or not uids for uids in uid_lists):
            raise ReconciliationError(f"{prefix} uid lists must be non-empty arrays")
        for uids in uid_lists:
            for uid in cast(list[object], uids):
                _sha256(uid, f"{prefix}.{uid_key}")
    return {"op": cast(str, op), "payload": payload}


@dataclass(frozen=True)
class MapChangeBundle:
    """A content-addressed contribution detached from a local hash chain."""

    base_version: str
    operations: tuple[dict[str, object], ...]
    author: str
    reason: str
    source_head: str | None = None

    def identity_dict(self) -> dict[str, object]:
        return {
            "schema": MAP_CHANGE_BUNDLE_SCHEMA,
            "base_version": self.base_version,
            "operations": [dict(operation) for operation in self.operations],
            "author": self.author,
            "reason": self.reason,
            "source_head": self.source_head,
        }

    @property
    def bundle_id(self) -> str:
        return hashlib.sha256(canonical_json(self.identity_dict()).encode()).hexdigest()

    def to_dict(self) -> dict[str, object]:
        result = self.identity_dict()
        result["bundle_id"] = self.bundle_id
        return result

    @classmethod
    def create(
        cls,
        *,
        base_version: str,
        operations: Sequence[Mapping[str, object]],
        author: str,
        reason: str,
        source_head: str | None = None,
    ) -> MapChangeBundle:
        return cls(
            base_version=_sha256(base_version, "base_version"),
            operations=tuple(
                _operation(dict(operation), i) for i, operation in enumerate(operations)
            ),
            author=_text(author, "author"),
            reason=_text(reason, "reason"),
            source_head=None if source_head is None else _sha256(source_head, "source_head"),
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> MapChangeBundle:
        expected = {
            "schema",
            "base_version",
            "operations",
            "author",
            "reason",
            "source_head",
            "bundle_id",
        }
        if set(raw) != expected:
            raise ReconciliationError("bundle fields differ from map-change-bundle@1")
        if raw["schema"] != MAP_CHANGE_BUNDLE_SCHEMA:
            raise ReconciliationError(f"unsupported bundle schema {raw['schema']!r}")
        if not isinstance(raw["operations"], list):
            raise ReconciliationError("operations must be an array")
        bundle = cls.create(
            base_version=cast(str, raw["base_version"]),
            operations=cast(list[Mapping[str, object]], raw["operations"]),
            author=cast(str, raw["author"]),
            reason=cast(str, raw["reason"]),
            source_head=cast(str | None, raw["source_head"]),
        )
        if _sha256(raw["bundle_id"], "bundle_id") != bundle.bundle_id:
            raise ReconciliationError("bundle_id does not recompute from bundle content")
        return bundle


def _entry(view: dict, uid: object) -> dict | None:
    if not isinstance(uid, str):
        return None
    return view.get("nodes", {}).get(uid) or view.get("edges", {}).get(uid)


def _kind_and_anchor(op: str, payload: dict) -> tuple[str, object] | None:
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        return None
    if op == "add_node":
        return "node", identity.get("quantity")
    if op == "add_edge":
        return "edge", identity.get("output")
    return None


def _conflict(index: int, code: str, message: str) -> dict[str, object]:
    return {"index": index, "code": code, "message": message}


def _classify(
    bundle: MapChangeBundle,
    base: dict,
    current: dict,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    accepted: list[dict[str, object]] = []
    no_ops: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    explicit_relations: set[str] = set()
    retired_uids: set[str] = set()
    available_uids = set(current.get("nodes", {})) | set(current.get("edges", {}))
    for operation in bundle.operations:
        payload = cast(dict, operation["payload"])
        if operation["op"] == "supersede":
            explicit_relations.update(payload.get("new_uids", []))
            retired_uids.update(payload.get("old_uids", []))
        elif operation["op"] == "equate":
            explicit_relations.update(payload.get("uids", []))
        elif operation["op"] == "deprecate" and isinstance(payload.get("uid"), str):
            retired_uids.add(payload["uid"])

    for index, operation in enumerate(bundle.operations):
        op = cast(str, operation["op"])
        payload = cast(dict, operation["payload"])
        uid = payload.get("uid")

        if op in ("add_node", "add_edge"):
            existing = _entry(current, uid)
            if existing is not None:
                if existing.get("identity") == payload.get("identity"):
                    no_ops.append({"index": index, "reason": "identical identity already exists"})
                    if isinstance(uid, str):
                        available_uids.add(uid)
                else:
                    conflicts.append(
                        _conflict(index, "identity_collision", "uid exists with different identity")
                    )
                continue
            anchor = _kind_and_anchor(op, payload)
            base_entries = base["nodes" if op == "add_node" else "edges"]
            same_anchor = [
                old_uid
                for old_uid, old in base_entries.items()
                if _kind_and_anchor(op, old) == anchor and old_uid != uid
            ]
            if retired_uids.intersection(same_anchor) and uid not in explicit_relations:
                conflicts.append(
                    _conflict(
                        index,
                        "identity_decision_required",
                        "a same-anchor identity is retired without an explicit supersede/equate decision",
                    )
                )
                continue
            accepted.append({"index": index, "op": op, "payload": payload})
            if isinstance(uid, str):
                available_uids.add(uid)
            continue

        base_entry = _entry(base, uid)
        current_entry = _entry(current, uid)
        if op in ("edit_meta", "deprecate") and (base_entry is None or current_entry is None):
            conflicts.append(
                _conflict(
                    index, "missing_target", f"{op} target is absent from base or current map"
                )
            )
            continue

        if op == "edit_meta":
            assert base_entry is not None and current_entry is not None
            if current_entry.get("deprecated") != base_entry.get("deprecated") or current_entry.get(
                "superseded_by"
            ) != base_entry.get("superseded_by"):
                conflicts.append(
                    _conflict(index, "lifecycle_conflict", "target lifecycle changed since base")
                )
                continue
            meta = payload.get("meta")
            if not isinstance(meta, dict):
                conflicts.append(
                    _conflict(index, "malformed_operation", "edit_meta.meta must be an object")
                )
                continue
            changed: dict[str, object] = {}
            field_conflicts: list[str] = []
            for key, desired in meta.items():
                before = base_entry.get("meta", {}).get(key)
                now = current_entry.get("meta", {}).get(key)
                if now == desired:
                    continue
                if now != before:
                    field_conflicts.append(key)
                else:
                    changed[key] = desired
            if field_conflicts:
                conflicts.append(
                    _conflict(
                        index,
                        "metadata_conflict",
                        f"fields changed differently since base: {sorted(field_conflicts)}",
                    )
                )
            elif changed:
                accepted.append(
                    {"index": index, "op": op, "payload": {"uid": uid, "meta": changed}}
                )
            else:
                no_ops.append({"index": index, "reason": "metadata already has requested values"})
            continue

        if op == "deprecate":
            assert base_entry is not None and current_entry is not None
            if current_entry.get("meta") != base_entry.get("meta") or current_entry.get(
                "superseded_by"
            ) != base_entry.get("superseded_by"):
                conflicts.append(
                    _conflict(
                        index, "lifecycle_conflict", "target was edited or superseded since base"
                    )
                )
            elif current_entry.get("deprecated"):
                if current_entry.get("deprecation_note") == payload.get("note"):
                    no_ops.append(
                        {"index": index, "reason": "identical deprecation already exists"}
                    )
                else:
                    conflicts.append(
                        _conflict(index, "lifecycle_conflict", "target was deprecated differently")
                    )
            else:
                accepted.append({"index": index, "op": op, "payload": payload})
            continue

        if op in ("supersede", "equate"):
            key = "old_uids" if op == "supersede" else "uids"
            uids = payload.get(key)
            if not isinstance(uids, list) or not uids:
                conflicts.append(
                    _conflict(index, "malformed_operation", f"{op}.{key} must be a non-empty array")
                )
                continue
            if op == "supersede":
                new_uids = payload.get("new_uids")
                if not isinstance(new_uids, list) or not new_uids:
                    conflicts.append(
                        _conflict(
                            index,
                            "malformed_operation",
                            "supersede.new_uids must be a non-empty array",
                        )
                    )
                    continue
                uids = [*uids, *new_uids]
            if any(related_uid not in available_uids for related_uid in uids):
                conflicts.append(
                    _conflict(index, "missing_dependency", f"{op} references an unknown uid")
                )
                continue
            state_key = "superseded_by" if op == "supersede" else "equivalent_to"
            targets = cast(list[str], payload[key])
            desired = payload.get("new_uids") if op == "supersede" else payload.get("uids")
            now_states = [(_entry(current, target) or {}).get(state_key) for target in targets]
            base_states = [(_entry(base, target) or {}).get(state_key) for target in targets]
            if all(state == desired for state in now_states):
                no_ops.append({"index": index, "reason": f"identical {op} already exists"})
            elif now_states != base_states or (
                op == "supersede"
                and any((_entry(current, target) or {}).get("deprecated") for target in targets)
            ):
                conflicts.append(
                    _conflict(index, "lifecycle_conflict", f"{op} relation changed since base")
                )
            else:
                accepted.append({"index": index, "op": op, "payload": payload})
            continue

    gate_records = [{"op": row["op"], "payload": row["payload"]} for row in accepted]
    for problem in validate_contribution(gate_records, current):
        conflicts.append(_conflict(-1, "gate_rejected", problem))
    return accepted, no_ops, conflicts


def _receipt_id(receipt: dict[str, object]) -> str:
    identity = {key: value for key, value in receipt.items() if key != "receipt_id"}
    return hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def reconcile_change_bundle(
    store: Store,
    bundle: MapChangeBundle,
    *,
    apply: bool = False,
    date: str | None = None,
) -> dict[str, object]:
    """Dry-run or apply a bundle against the store's current canonical head."""
    if bundle.base_version == GENESIS_PREV:
        base = {"nodes": {}, "edges": {}}
    else:
        try:
            base = store.read(bundle.base_version)
        except ValueError:
            base = None
    before = store.head
    if base is None:
        accepted: list[dict[str, object]] = []
        no_ops: list[dict[str, object]] = []
        conflicts = [_conflict(-1, "unknown_base", "base_version is not in canonical history")]
    else:
        accepted, no_ops, conflicts = _classify(bundle, base, store.read())

    applied = False
    versions: list[str] = []
    if apply and not conflicts:
        if date is None:
            raise ReconciliationError("date is required when apply=True")
        records = [{"op": row["op"], "payload": row["payload"]} for row in accepted]
        store.propose(records, author=bundle.author, date=date, reason_prefix=bundle.reason)
        problems = store.verify()
        if problems:
            raise ReconciliationError("canonical store failed verification: " + "; ".join(problems))
        after = store.head
        if after != before:
            versions = [record["version"] for record in store.diff(before, after)]
        applied = True
    else:
        after = before

    receipt: dict[str, object] = {
        "schema": MAP_RECONCILIATION_RECEIPT_SCHEMA,
        "bundle_id": bundle.bundle_id,
        "base_version": bundle.base_version,
        "source_head": bundle.source_head,
        "canonical_head_before": before,
        "accepted": accepted,
        "no_ops": no_ops,
        "conflicts": conflicts,
        "applied": applied,
        "record_versions": versions,
        "canonical_head_after": after,
    }
    receipt["receipt_id"] = _receipt_id(receipt)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omai.reconcile")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--store", type=Path, default=Path("map"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--date")
    args = parser.parse_args(argv)
    bundle = MapChangeBundle.from_dict(json.loads(args.bundle.read_text()))
    receipt = reconcile_change_bundle(Store(args.store), bundle, apply=args.apply, date=args.date)
    print(canonical_json(receipt))
    return 2 if receipt["conflicts"] else 0


if __name__ == "__main__":
    sys.exit(main())
