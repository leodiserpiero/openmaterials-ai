"""Content-addressed requests for adapter-owned external graph solves.

The representation executor can evaluate closed-form operators itself.  An
implicit operator, such as the direct BTE solve, crosses a runtime boundary:
OpenMaterials owns the graph identity while an execution engine owns the code
invocation.  This module makes that boundary explicit without moving runtime
metadata into lineage identity.

``build_external_solve_request`` binds one implicit operator and one code
representation to the live map.  Every graph element is pinned by uid and the
request pins the map-store head.  ``validate_external_solve_request`` repeats
those checks on received JSON before an execution engine dispatches anything.
Unknown, deprecated, superseded, or stale bindings therefore fail closed.

Input and output bindings use the map identity's canonical uid order.  Their
array position has no execution meaning: consumers resolve bindings by uid (and
may cross-check the pinned name), never by positional adapter arguments.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast

from omai.lineages import lineage_id
from omai.operator.identity import canonical_json, edge_id, node_id
from omai.operator.operator import Operator
from omai.representation.adapter import OperatorRepresentationSpec
from omai.store import Store

__all__ = [
    "EXTERNAL_SOLVE_REQUEST_SCHEMA",
    "ExternalSolveBindingError",
    "ExternalSolveRequest",
    "MapSnapshot",
    "NodeBinding",
    "OperatorBinding",
    "RepresentationBinding",
    "build_external_solve_request",
    "load_live_map_snapshot",
    "validate_external_solve_request",
]


EXTERNAL_SOLVE_REQUEST_SCHEMA = "openmaterials.external-solve-request@1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_MAP_ROOT = Path(__file__).resolve().parents[1] / "map"


class ExternalSolveBindingError(ValueError):
    """An external-solve request is not bound to the current live map."""


def _json_object_copy(value: Mapping[str, object], *, field_name: str) -> dict[str, object]:
    """Return a detached, canonical-JSON-compatible object."""
    try:
        copied = json.loads(
            json.dumps(
                dict(value),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ExternalSolveBindingError(
            f"{field_name} must be a canonical-JSON-compatible object"
        ) from exc
    if not isinstance(copied, dict):  # defensive; input is a Mapping
        raise ExternalSolveBindingError(f"{field_name} must be a JSON object")
    return cast(dict[str, object], copied)


def _require_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ExternalSolveBindingError(f"{field_name} must be 64 lowercase hex characters")
    return value


def _require_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExternalSolveBindingError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class NodeBinding:
    name: str
    uid: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "uid": self.uid}

    @classmethod
    def from_dict(cls, raw: object, *, field_name: str) -> NodeBinding:
        if not isinstance(raw, dict) or set(raw) != {"name", "uid"}:
            raise ExternalSolveBindingError(
                f"{field_name} must contain exactly name and uid"
            )
        return cls(
            name=_require_text(raw["name"], field_name=f"{field_name}.name"),
            uid=_require_sha256(raw["uid"], field_name=f"{field_name}.uid"),
        )


@dataclass(frozen=True)
class OperatorBinding:
    name: str
    uid: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "uid": self.uid}

    @classmethod
    def from_dict(cls, raw: object) -> OperatorBinding:
        if not isinstance(raw, dict) or set(raw) != {"name", "uid"}:
            raise ExternalSolveBindingError("operator must contain exactly name and uid")
        return cls(
            name=_require_text(raw["name"], field_name="operator.name"),
            uid=_require_sha256(raw["uid"], field_name="operator.uid"),
        )


@dataclass(frozen=True)
class RepresentationBinding:
    name: str
    parameter_units: dict[str, str]
    schemes: dict[str, str]
    discretization: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "parameter_units": dict(self.parameter_units),
            "schemes": dict(self.schemes),
            "discretization": dict(self.discretization),
        }

    @classmethod
    def from_dict(cls, raw: object) -> RepresentationBinding:
        expected = {"name", "parameter_units", "schemes", "discretization"}
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ExternalSolveBindingError(
                "representation must contain exactly name, parameter_units, schemes, "
                "and discretization"
            )

        def string_map(value: object, field_name: str) -> dict[str, str]:
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                raise ExternalSolveBindingError(f"representation.{field_name} must map strings")
            return {str(k): str(v) for k, v in value.items()}

        return cls(
            name=_require_text(raw["name"], field_name="representation.name"),
            parameter_units=string_map(raw["parameter_units"], "parameter_units"),
            schemes=string_map(raw["schemes"], "schemes"),
            discretization=string_map(raw["discretization"], "discretization"),
        )


@dataclass(frozen=True)
class MapSnapshot:
    """The immutable-version view against which a request is checked."""

    version: str
    nodes: dict[str, dict[str, object]]
    edges: dict[str, dict[str, object]]


@dataclass(frozen=True)
class ExternalSolveRequest:
    """A JSON-serializable request for one adapter-owned implicit operator."""

    schema: ClassVar[str] = EXTERNAL_SOLVE_REQUEST_SCHEMA

    map_version: str
    operator: OperatorBinding
    inputs: tuple[NodeBinding, ...]
    outputs: tuple[NodeBinding, ...]
    target: NodeBinding
    representation: RepresentationBinding
    lineage: dict[str, object]
    execution: dict[str, object]

    @property
    def lineage_id(self) -> str:
        return lineage_id(self.lineage)

    def identity_dict(self) -> dict[str, object]:
        """Return every request field except the derived request id."""
        return {
            "schema": self.schema,
            "map_version": self.map_version,
            "operator": self.operator.to_dict(),
            "inputs": [binding.to_dict() for binding in self.inputs],
            "outputs": [binding.to_dict() for binding in self.outputs],
            "target": self.target.to_dict(),
            "representation": self.representation.to_dict(),
            "lineage": dict(self.lineage),
            "lineage_id": self.lineage_id,
            "execution": dict(self.execution),
        }

    @property
    def request_id(self) -> str:
        blob = canonical_json(self.identity_dict())
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        payload = self.identity_dict()
        payload["request_id"] = self.request_id
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ExternalSolveRequest:
        expected = {
            "schema",
            "map_version",
            "operator",
            "inputs",
            "outputs",
            "target",
            "representation",
            "lineage",
            "lineage_id",
            "execution",
            "request_id",
        }
        if set(raw) != expected:
            missing = sorted(expected - set(raw))
            extra = sorted(set(raw) - expected)
            raise ExternalSolveBindingError(
                f"request fields differ from schema (missing={missing}, extra={extra})"
            )
        if raw["schema"] != cls.schema:
            raise ExternalSolveBindingError(f"unsupported request schema {raw['schema']!r}")
        map_version = _require_sha256(raw["map_version"], field_name="map_version")
        if not isinstance(raw["inputs"], list) or not isinstance(raw["outputs"], list):
            raise ExternalSolveBindingError("inputs and outputs must be arrays")
        if not isinstance(raw["lineage"], dict) or not isinstance(raw["execution"], dict):
            raise ExternalSolveBindingError("lineage and execution must be objects")

        request = cls(
            map_version=map_version,
            operator=OperatorBinding.from_dict(raw["operator"]),
            inputs=tuple(
                NodeBinding.from_dict(item, field_name=f"inputs[{index}]")
                for index, item in enumerate(raw["inputs"])
            ),
            outputs=tuple(
                NodeBinding.from_dict(item, field_name=f"outputs[{index}]")
                for index, item in enumerate(raw["outputs"])
            ),
            target=NodeBinding.from_dict(raw["target"], field_name="target"),
            representation=RepresentationBinding.from_dict(raw["representation"]),
            lineage=_json_object_copy(raw["lineage"], field_name="lineage"),
            execution=_json_object_copy(raw["execution"], field_name="execution"),
        )
        stated_lineage_id = _require_sha256(raw["lineage_id"], field_name="lineage_id")
        if stated_lineage_id != request.lineage_id:
            raise ExternalSolveBindingError("lineage_id does not recompute from lineage")
        stated_request_id = _require_sha256(raw["request_id"], field_name="request_id")
        if stated_request_id != request.request_id:
            raise ExternalSolveBindingError("request_id does not recompute from request content")
        return request


def load_live_map_snapshot(map_root: Path | None = None) -> MapSnapshot:
    """Load and verify the current log-first map before constructing a request."""
    store = Store(map_root or _DEFAULT_MAP_ROOT)
    problems = store.verify()
    if problems:
        raise ExternalSolveBindingError(
            "map store verification failed: " + "; ".join(problems)
        )
    view = store.read()
    return MapSnapshot(
        version=store.head,
        nodes=cast(dict[str, dict[str, object]], view["nodes"]),
        edges=cast(dict[str, dict[str, object]], view["edges"]),
    )


def _require_live_entry(
    entries: Mapping[str, Mapping[str, object]],
    *,
    uid: str,
    name: str,
    kind: str,
) -> Mapping[str, object]:
    entry = entries.get(uid)
    if entry is None:
        raise ExternalSolveBindingError(
            f"{kind} {name!r} uid {uid} is not present in map"
        )
    meta = entry.get("meta")
    if not isinstance(meta, dict) or meta.get("name") != name:
        raise ExternalSolveBindingError(
            f"{kind} uid {uid} does not resolve to name {name!r}"
        )
    if entry.get("deprecated") is True:
        raise ExternalSolveBindingError(f"{kind} {name!r} uid {uid} is deprecated")
    superseded_by = entry.get("superseded_by")
    if isinstance(superseded_by, list) and superseded_by:
        raise ExternalSolveBindingError(f"{kind} {name!r} uid {uid} is superseded")
    return entry


def _target_is_reachable(request: ExternalSolveRequest, snapshot: MapSnapshot) -> bool:
    """Whether whole live hyperedges can derive the target from the frontier."""
    # The external solve's inputs already exist at dispatch and its outputs are
    # the newly produced values.  Both sides are therefore available to later
    # hyperedges; unrelated co-inputs are not.  Taint starts only at the solve
    # outputs and propagates through a ready edge, so the target must actually
    # depend on the external result rather than merely on its inputs.
    tainted = {binding.uid for binding in request.outputs}
    reached = {binding.uid for binding in (*request.inputs, *request.outputs)}
    if request.target.uid in tainted:
        return True

    hyperedges: list[tuple[frozenset[str], frozenset[str]]] = []
    for entry in snapshot.edges.values():
        if entry.get("deprecated") is True or entry.get("superseded_by"):
            continue
        identity = entry.get("identity")
        if not isinstance(identity, dict):
            continue
        inputs = identity.get("inputs")
        outputs = identity.get("outputs")
        if not isinstance(inputs, list) or not isinstance(outputs, list):
            continue
        if not inputs or not outputs:
            continue
        if not all(isinstance(uid, str) for uid in (*inputs, *outputs)):
            continue
        hyperedges.append((frozenset(inputs), frozenset(outputs)))

    changed = True
    while changed:
        changed = False
        for input_uids, output_uids in hyperedges:
            if not input_uids.issubset(reached):
                continue
            new_outputs = output_uids - reached
            if new_outputs:
                reached.update(new_outputs)
                changed = True
            if input_uids.intersection(tainted):
                new_tainted = output_uids - tainted
                if new_tainted:
                    tainted.update(new_tainted)
                    changed = True
            if request.target.uid in tainted:
                return True
    return request.target.uid in tainted


def _validate_live_bindings(request: ExternalSolveRequest, snapshot: MapSnapshot) -> None:
    _require_sha256(snapshot.version, field_name="snapshot.version")
    if request.map_version != snapshot.version:
        raise ExternalSolveBindingError(
            f"stale map_version {request.map_version}; live map is {snapshot.version}"
        )
    edge_entry = _require_live_entry(
        snapshot.edges,
        uid=request.operator.uid,
        name=request.operator.name,
        kind="operator",
    )
    for binding in (*request.inputs, *request.outputs, request.target):
        _require_live_entry(
            snapshot.nodes,
            uid=binding.uid,
            name=binding.name,
            kind="node",
        )
    identity = edge_entry.get("identity")
    if not isinstance(identity, dict):
        raise ExternalSolveBindingError("operator map entry has no identity object")
    stored_inputs = identity.get("inputs")
    stored_outputs = identity.get("outputs")
    request_inputs = [binding.uid for binding in request.inputs]
    request_outputs = [binding.uid for binding in request.outputs]
    if stored_inputs != request_inputs:
        raise ExternalSolveBindingError("request inputs do not match operator identity")
    if stored_outputs != request_outputs:
        raise ExternalSolveBindingError("request outputs do not match operator identity")
    if request.lineage.get("node") != request.target.name:
        raise ExternalSolveBindingError("lineage.node does not match target.name")
    if request.lineage.get("node_uid") != request.target.uid:
        raise ExternalSolveBindingError("lineage.node_uid does not match target.uid")
    if not _target_is_reachable(request, snapshot):
        raise ExternalSolveBindingError(
            "target is not reachable downstream of the external operator outputs"
        )


def build_external_solve_request(
    operator: Operator,
    representation: OperatorRepresentationSpec,
    lineage: Mapping[str, object],
    *,
    execution: Mapping[str, object] | None = None,
    snapshot: MapSnapshot | None = None,
) -> ExternalSolveRequest:
    """Bind an implicit graph operator to a representation and the live map."""
    if operator.is_executable_in_sympy:
        raise ExternalSolveBindingError(
            f"operator {operator.name!r} is locally executable and needs no external solve"
        )
    operator_uid = edge_id(operator, node_id)
    represented_uid = edge_id(representation.operator, node_id)
    if represented_uid != operator_uid:
        raise ExternalSolveBindingError(
            f"representation {representation.representation_name!r} binds a different operator"
        )

    live = snapshot or load_live_map_snapshot()
    effective_schemes = dict(operator.schemes)
    effective_schemes.update(representation.scheme_overrides)
    lineage_copy = _json_object_copy(lineage, field_name="lineage")
    execution_copy = _json_object_copy(execution or {}, field_name="execution")

    target_name = _require_text(lineage_copy.get("node"), field_name="lineage.node")
    target_uid = _require_sha256(lineage_copy.get("node_uid"), field_name="lineage.node_uid")
    request = ExternalSolveRequest(
        map_version=live.version,
        operator=OperatorBinding(operator.name, operator_uid),
        inputs=tuple(
            sorted(
                (NodeBinding(space.name, node_id(space)) for space in operator.inputs),
                key=lambda binding: binding.uid,
            )
        ),
        outputs=tuple(
            sorted(
                (NodeBinding(space.name, node_id(space)) for space in operator.outputs),
                key=lambda binding: binding.uid,
            )
        ),
        target=NodeBinding(target_name, target_uid),
        representation=RepresentationBinding(
            name=_require_text(
                representation.representation_name,
                field_name="representation.representation_name",
            ),
            parameter_units={
                str(k): str(v) for k, v in sorted(representation.parameter_units.items())
            },
            schemes={str(k): str(v) for k, v in sorted(effective_schemes.items())},
            discretization={
                str(k): str(v)
                for k, v in sorted(representation.discretization_choices.items())
            },
        ),
        lineage=lineage_copy,
        execution=execution_copy,
    )
    _validate_live_bindings(request, live)
    return request


def validate_external_solve_request(
    payload: Mapping[str, object],
    *,
    snapshot: MapSnapshot | None = None,
) -> ExternalSolveRequest:
    """Parse and validate an incoming request before adapter dispatch."""
    request = ExternalSolveRequest.from_dict(payload)
    _validate_live_bindings(request, snapshot or load_live_map_snapshot())
    return request
