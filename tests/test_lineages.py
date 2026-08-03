"""Tests for the lineage layer (omai/lineages.py).

Lineage identity (the light model: same lineage -> same id; a url, an artifact
pointer, or an execution change does NOT change the id; a lineage change,
including a hyperparameter or setup value, DOES), the light record builder
record_light (a record with no artifacts and no node is valid, flagged
node-unresolved not rejected), the URL round-trip (record_to_fragment ->
record_from_fragment is identity-stable and base64url, and a hand-built fragment
decodes), artifact-pointer well-formedness, the optional mirror `provider` key
(who holds the bytes: outside identity, shape-checked as a string, round-tripping
the fragment, echoed onto the verify report), the heavy checksummed writer's
idempotence and overwrite refusal, the OPTIONAL heavy import path (a no-sha256
bundle now yields a pointer record, not an exception; a full checksummed bundle
verifies), the verify report shapes (offline: a fake fetcher, no network), the
build_simulations bundler (including slug resolution), and the instance
simulation-backref gate in both directions. All hermetic: node identity is
supplied as a fixture name_to_uid or resolved against the live map read-only,
and no test touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from omai import lineages as lin

# A fixed fixture map: a lineage node and a result variable with pinned uids. The
# ids are arbitrary 64-hex strings; the point is that the pin must MATCH, not
# that it equals any live value.
_NODE = "ThermalConductivity[transport_model=hnemd]"
_NODE_UID = "e7157a52da5eb7d2ae79e57692acc30d7e63a3a0d1c731b14da4d6ab8fd5eb88"
_NAME_TO_UID = {_NODE: _NODE_UID}

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _lineage(**over):
    r = {
        "node": _NODE,
        "node_uid": _NODE_UID,
        "material": {"name": "Si"},
        "conditions": {"T": "300 K", "potential": "Si-NEP"},
        "params": {},
    }
    r.update(over)
    return r


def _execution(**over):
    e = {
        "code": "gpumd",
        "code_version": "3.9.5",
        "container_digest": "sha256:" + ("c" * 64),
        "runner": "mcg-cloud/pod-abc",
        "wall_time_s": 4210,
        "seeds": [12345],
    }
    e.update(over)
    return e


def _artifacts(**over):
    a = [{"path": "results/thermal_summary.json", "bytes": 2048,
          "sha256": _SHA_A, "role": "result"},
         {"path": "traj/dump.xyz", "bytes": 88123456, "sha256": _SHA_B,
          "role": "trajectory"}]
    if over.get("single"):
        return a[:1]
    return a


# --------------------------------------------------------------------------
# Lineage identity (the light model: the id is the lineage alone).
# --------------------------------------------------------------------------

def test_same_lineage_same_id():
    a = lin.lineage_id(_lineage())
    b = lin.lineage_id(_lineage())
    assert a == b
    assert len(a) == 64


def test_canonical_id_is_lineage_only():
    """The back-compatible canonical_id(lineage, execution, artifacts) hashes the
    LINEAGE alone: execution and artifacts are accepted but ignored, so it agrees
    with lineage_id and never folds either into identity."""
    assert (lin.canonical_id(_lineage(), _execution(), _artifacts())
            == lin.lineage_id(_lineage()))
    # A different execution / different artifacts, same lineage -> same id.
    assert (lin.canonical_id(_lineage(), _execution(wall_time_s=9999), [])
            == lin.lineage_id(_lineage()))


def test_url_change_does_not_change_id():
    """Location is out of identity: an artifact url (or any resolver-layer key)
    never perturbs the lineage id."""
    base = lin.lineage_id(_lineage())
    arts = _artifacts()
    arts[0]["url"] = "https://r2.example.com/runs/abc/thermal_summary.json"
    arts[1]["url"] = "https://zenodo.org/record/12345/files/dump.xyz"
    # Artifacts are not part of identity at all; the id is the lineage.
    assert lin.canonical_id(_lineage(), _execution(), arts) == base


def test_lineage_change_changes_id():
    base = lin.lineage_id(_lineage())
    other = lin.lineage_id(_lineage(conditions={"T": "500 K"}))
    assert other != base


def test_artifact_change_does_not_change_id():
    """Artifacts NEVER enter identity: changing a checksum, adding, or removing
    a pointer leaves the lineage id unchanged (the light model's core promise)."""
    base = lin.lineage_id(_lineage())
    arts = _artifacts()
    arts[0]["sha256"] = "f" * 64
    assert lin.canonical_id(_lineage(), _execution(), arts) == base
    assert lin.canonical_id(_lineage(), _execution(), []) == base


def test_execution_change_does_not_change_id():
    """Execution rides outside identity: a different wall time, seed, or code
    version is the same experiment lineage and mints the same id."""
    base = lin.lineage_id(_lineage())
    assert lin.canonical_id(_lineage(), _execution(wall_time_s=4210.5), []) == base
    assert lin.canonical_id(_lineage(), _execution(seeds=[999]), []) == base


def test_float_conditions_round_to_one_identity():
    """Refetch-level float noise in conditions collapses to one id; a
    physically distinct value does not."""
    noisy = _lineage(conditions={"T": 300.0000001, "fmax": 0.05})
    clean = _lineage(conditions={"T": 300.0, "fmax": 0.05})
    assert lin.lineage_id(noisy) == lin.lineage_id(clean)
    distinct = _lineage(conditions={"T": 301.0, "fmax": 0.05})
    assert lin.lineage_id(distinct) != lin.lineage_id(clean)


def test_hyperparameters_and_values_round_and_matter():
    """hyperparameters and setup values are lineage scalars: their floats round
    to one identity, and a distinct value changes the id (they are part of the
    lineage, unlike a url or an execution stamp)."""
    noisy = _lineage(hyperparameters={"cutoff": 8.0000001},
                    values={"pressure_GPa": 1.0000001})
    clean = _lineage(hyperparameters={"cutoff": 8.0},
                    values={"pressure_GPa": 1.0})
    assert lin.lineage_id(noisy) == lin.lineage_id(clean)
    other = _lineage(hyperparameters={"cutoff": 9.0},
                    values={"pressure_GPa": 1.0})
    assert lin.lineage_id(other) != lin.lineage_id(clean)


# --------------------------------------------------------------------------
# Writer: idempotence and overwrite refusal.
# --------------------------------------------------------------------------

def test_record_simulation_writes_and_is_idempotent(tmp_path):
    p1 = lin.record_simulation(
        lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
        sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)
    # A byte-identical re-record returns the SAME path, no second file.
    p2 = lin.record_simulation(
        lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
        sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)
    assert p1 == p2
    assert len(list(tmp_path.glob("*.json"))) == 1
    rec = json.loads(p1.read_text())
    assert rec["id"] == lin.canonical_id(_lineage(), _execution(), _artifacts())
    assert rec["lineage"]["node"] == _NODE


def test_record_simulation_stores_id_and_manifest(tmp_path):
    rec_id = lin.canonical_id(_lineage(), _execution(), _artifacts())
    path = lin.record_simulation(
        lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
        sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)
    rec = json.loads(path.read_text())
    assert rec["id"] == rec_id
    # The manifest keeps exactly the four hashed keys per entry (no url when no
    # mirror was declared).
    for art in rec["artifacts"]:
        assert set(art) == {"path", "bytes", "sha256", "role"}


def test_overwrite_refusal_on_different_content(tmp_path, monkeypatch):
    """Two DIFFERENT records that slug to the same base must not clobber each
    other: the record_instance discipline. Force a slug collision by pinning
    slugify to a constant, so distinct ids land a numeric suffix instead of an
    overwrite."""
    monkeypatch.setattr(lin, "slugify", lambda *_a, **_k: "collide")
    p1 = lin.record_simulation(
        lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
        sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)
    p2 = lin.record_simulation(
        lineage=_lineage(conditions={"T": "500 K"}), execution=_execution(),
        artifacts=_artifacts(), sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)
    assert p1 != p2
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_mirrors_ride_outside_the_hash_and_mirror_urls(tmp_path):
    mirrors = {"results/thermal_summary.json":
               {"url": "https://r2.example.com/x.json", "store": "r2"}}
    rec_id_no_mirror = lin.canonical_id(_lineage(), _execution(), _artifacts())
    path = lin.record_simulation(
        lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
        mirrors=mirrors, sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)
    rec = json.loads(path.read_text())
    # The id is the same as without any mirror: location is not identity.
    assert rec["id"] == rec_id_no_mirror
    assert rec["mirrors"] == mirrors
    # The mirrored url appears on the matching artifact row as a convenience.
    row = next(a for a in rec["artifacts"]
               if a["path"] == "results/thermal_summary.json")
    assert row["url"] == "https://r2.example.com/x.json"


# --------------------------------------------------------------------------
# Validation failures.
# --------------------------------------------------------------------------

def test_bad_node_uid_pin_rejected(tmp_path):
    with pytest.raises(lin.LineageError, match="node_uid"):
        lin.record_simulation(
            lineage=_lineage(node_uid="0" * 64), execution=_execution(),
            artifacts=_artifacts(), sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)


def test_unknown_node_rejected(tmp_path):
    with pytest.raises(lin.LineageError, match="live map node"):
        lin.record_simulation(
            lineage=_lineage(node="NotANode", node_uid=None),
            execution=_execution(), artifacts=_artifacts(),
            sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)


def test_unknown_configuration_rejected(tmp_path):
    """A lineage naming a configuration uid absent from docs/data/configurations/
    fails, but the check reads the REAL configurations dir; point it at an empty
    one so a missing record is deterministic."""
    empty_cfg = tmp_path / "cfg"
    empty_cfg.mkdir()
    lineage = _lineage(material={"name": "Si", "configuration": "sha256:" + "9" * 64})
    with pytest.raises(lin.LineageError, match="configuration"):
        lin._validate(
            {"lineage": lineage, "execution": _execution(),
             "artifacts": _artifacts()},
            name_to_uid=_NAME_TO_UID, config_dir=empty_cfg, where="t")


def test_known_configuration_accepted(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    uid = "9" * 64
    (cfg / "si.json").write_text(json.dumps({"canonical": {"uid": uid}}))
    lineage = _lineage(material={"name": "Si", "configuration": uid})
    # Should not raise: the configuration resolves.
    lin._validate(
        {"lineage": lineage, "execution": _execution(), "artifacts": _artifacts()},
        name_to_uid=_NAME_TO_UID, config_dir=cfg, where="t")


@pytest.mark.parametrize("bad", [
    {"path": "", "bytes": 10, "sha256": _SHA_A, "role": "result"},
    {"path": "x", "bytes": 0, "sha256": _SHA_A, "role": "result"},
    {"path": "x", "bytes": -1, "sha256": _SHA_A, "role": "result"},
    {"path": "x", "bytes": 10, "sha256": "tooshort", "role": "result"},
    {"path": "x", "bytes": 10, "sha256": _SHA_A, "role": ""},
    {"path": "x", "bytes": True, "sha256": _SHA_A, "role": "result"},
])
def test_malformed_manifest_rejected(tmp_path, bad):
    with pytest.raises(lin.LineageError, match="artifact"):
        lin.record_simulation(
            lineage=_lineage(), execution=_execution(), artifacts=[bad],
            sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)


def test_result_backref_mismatch_rejected(tmp_path):
    """An inline result stub whose simulation backref is not this record's id is
    rejected: the run owns the values it produced."""
    good_id = lin.canonical_id(_lineage(), _execution(), _artifacts())
    stub = {"variable": _NODE, "material": "Si",
            "conditions": {"T": "300 K"}, "value": 1.37, "units": "W/(m K)",
            "source": {"kind": "simulation", "ref": "gpumd", "detail": "HNEMD"},
            "simulation": "0" * 64}  # wrong backref
    assert stub["simulation"] != good_id
    with pytest.raises(lin.LineageError, match="backref"):
        lin.record_simulation(
            lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
            results=[stub], sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)


def test_result_stub_with_correct_backref_accepted(tmp_path):
    rec_id = lin.canonical_id(_lineage(), _execution(), _artifacts())
    stub = {"variable": _NODE, "material": "Si",
            "conditions": {"T": "300 K"}, "value": 1.37, "units": "W/(m K)",
            "source": {"kind": "simulation", "ref": "gpumd", "detail": "HNEMD"},
            "simulation": rec_id}
    path = lin.record_simulation(
        lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
        results=[stub], sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)
    rec = json.loads(path.read_text())
    assert rec["results"][0]["simulation"] == rec_id


def test_result_stub_missing_conditions_rejected(tmp_path):
    """The instance contract (build_instances) requires conditions; an inline
    stub is held to the same bar, so a stub that would be refused as an
    instance file cannot ride into a simulation record either."""
    rec_id = lin.canonical_id(_lineage(), _execution(), _artifacts())
    stub = {"variable": _NODE, "material": "Si", "value": 1.37,
            "units": "W/(m K)",
            "source": {"kind": "simulation", "ref": "gpumd", "detail": "HNEMD"},
            "simulation": rec_id}  # no conditions
    with pytest.raises(lin.LineageError, match="conditions"):
        lin.record_simulation(
            lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
            results=[stub], sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)


@pytest.mark.parametrize("bad_exec", [
    None,
    "gpumd",
    {},
    {"code": ""},
    {"code": 3},
    {"code_version": "3.9.5"},
])
def test_malformed_execution_rejected(tmp_path, bad_exec):
    """The execution block is the record's "what ran" claim: it must be an
    object naming the code that ran. A null, bare-string, or code-less block is
    degenerate and fails the gate (the manifest well-formedness bar applied to
    execution)."""
    with pytest.raises(lin.LineageError, match="execution"):
        lin.record_simulation(
            lineage=_lineage(), execution=bad_exec, artifacts=_artifacts(),
            sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)


def test_result_backref_slug_string_accepted(tmp_path):
    """A bare backref slug (a pointer to a committed instance file) is a legal
    result entry; its own backref is checked where that instance is bundled."""
    path = lin.record_simulation(
        lineage=_lineage(), execution=_execution(), artifacts=_artifacts(),
        results=["si-thermalconductivity-gpumd-hnemd"],
        sim_dir=tmp_path, name_to_uid=_NAME_TO_UID)
    rec = json.loads(path.read_text())
    assert rec["results"] == ["si-thermalconductivity-gpumd-hnemd"]


def test_stated_id_mismatch_rejected(tmp_path):
    """A record whose stated id does not recompute from its claim (e.g. someone
    folded a url into the hash) is rejected."""
    with pytest.raises(lin.LineageError, match="recomputed"):
        lin._validate(
            {"id": "0" * 64, "lineage": _lineage(), "execution": _execution(),
             "artifacts": _artifacts()},
            name_to_uid=_NAME_TO_UID, config_dir=tmp_path, where="t")


# --------------------------------------------------------------------------
# verify_simulation: a dated report, never a gate (offline, fake fetcher).
# --------------------------------------------------------------------------

def _record_for_verify():
    return {
        "id": "deadbeef",
        "artifacts": [
            {"path": "a.json", "bytes": 3, "sha256":
             __import__("hashlib").sha256(b"abc").hexdigest(), "role": "result"},
            {"path": "b.bin", "bytes": 3, "sha256": _SHA_B, "role": "trajectory"},
            {"path": "c.txt", "bytes": 3, "sha256": _SHA_A, "role": "log"},
        ],
        "mirrors": {
            "a.json": {"url": "https://x/a.json"},
            "b.bin": {"url": "https://x/b.bin"},
            # c.txt has NO mirror -> no-url
        },
    }


def test_verify_report_shape_ok_mismatch_and_unreachable():
    import datetime

    rec = _record_for_verify()

    def fetcher(url):
        if url.endswith("a.json"):
            return b"abc"          # sha matches -> ok
        if url.endswith("b.bin"):
            return b"xyz"          # sha differs -> mismatch
        raise RuntimeError("boom")

    report = lin.verify_simulation(rec, fetcher=fetcher,
                                   today=datetime.date(2026, 7, 13))
    assert report["id"] == "deadbeef"
    assert report["date"] == "2026-07-13"
    by_path = {c["path"]: c for c in report["checked"]}
    assert by_path["a.json"]["status"] == "ok"
    assert by_path["b.bin"]["status"] == "mismatch"
    assert by_path["b.bin"]["expected"] == _SHA_B
    assert by_path["c.txt"]["status"] == "no-url"


def test_verify_unreachable_url_no_network():
    """The required case: an unreachable url is reported unreachable, never
    raised, never a gate."""
    import datetime

    rec = _record_for_verify()

    def down(url):
        raise ConnectionError("host unreachable")

    report = lin.verify_simulation(rec, fetcher=down,
                                   today=datetime.date(2026, 7, 13))
    statuses = {c["path"]: c["status"] for c in report["checked"]}
    assert statuses["a.json"] == "unreachable"
    assert statuses["b.bin"] == "unreachable"
    assert statuses["c.txt"] == "no-url"
    # Never raised: verify is a report.
    assert report["date"] == "2026-07-13"


def test_verify_without_fetcher_reports_unreachable():
    rec = _record_for_verify()
    report = lin.verify_simulation(rec)
    urlful = [c for c in report["checked"] if c["url"] is not None]
    assert urlful and all(c["status"] == "unreachable" for c in urlful)
    assert all(c.get("reason") == "no fetcher" for c in urlful)


# --------------------------------------------------------------------------
# build_simulations bundler and the instance backref passthrough.
# --------------------------------------------------------------------------

def test_build_simulations_bundles_and_pins_node_uid(tmp_path):
    """A written record round-trips through build_simulations, is pinned to the
    LIVE lineage-node uid, and carries its file. Uses a real live node so the
    bundler's live-map resolution agrees with the record's pin."""
    from omai.map_data import _domains, build_graph_dict, build_simulations

    live = {n["id"]: n["uid"] for n in build_graph_dict(_domains())["nodes"]}
    node = "ThermalConductivity[transport_model=hnemd]"
    lineage = _lineage(node=node, node_uid=live[node])
    lin.record_simulation(
        lineage=lineage, execution=_execution(), artifacts=_artifacts(),
        sim_dir=tmp_path, name_to_uid=live)
    bundle = build_simulations(tmp_path)
    assert len(bundle) == 1
    entry = bundle[0]
    assert entry["node_uid"] == live[node]
    assert entry["file"].endswith(".json")
    assert entry["id"] == lin.canonical_id(lineage, _execution(), _artifacts())


def test_build_simulations_empty_dir_returns_empty(tmp_path):
    from omai.map_data import build_simulations

    assert build_simulations(tmp_path) == []


# --------------------------------------------------------------------------
# The backref loop, record -> instance: a result slug must resolve at bundle
# time to a committed instance that backrefs the record.
# --------------------------------------------------------------------------

def _live_uids():
    from omai.map_data import _domains, build_graph_dict

    return {n["id"]: n["uid"] for n in build_graph_dict(_domains())["nodes"]}


_LIVE_NODE = "ThermalConductivity[transport_model=hnemd]"


def _record_and_instance(tmp_path, *, backref=None, slug=None):
    """Write one simulation record (results=[slug]) into tmp_path/sims and one
    instance into tmp_path/insts carrying ``backref`` (default: the record's
    id). Returns (sims_dir, insts_dir, record_id, instance_slug)."""
    from omai.map_data import _domains, record_instance

    live = _live_uids()
    lineage = _lineage(node=_LIVE_NODE, node_uid=live[_LIVE_NODE])
    rec_id = lin.canonical_id(lineage, _execution(), _artifacts())
    sims, insts = tmp_path / "sims", tmp_path / "insts"
    inst_path = record_instance(
        domains=_domains(), variable=_LIVE_NODE, material="Si", value=1.37,
        units="W/(m K)", source_kind="simulation", source_ref="gpumd",
        detail="HNEMD bulk Si", simulation=backref or rec_id,
        instances_dir=insts)
    lin.record_simulation(
        lineage=lineage, execution=_execution(), artifacts=_artifacts(),
        results=[slug or inst_path.stem], sim_dir=sims, name_to_uid=live)
    return sims, insts, rec_id, inst_path.stem


def test_bundler_resolves_result_slugs_and_their_backrefs(tmp_path):
    """The good path: a record whose result slug names a committed instance
    that backrefs it bundles cleanly and carries the slug through."""
    from omai.map_data import build_simulations

    sims, insts, rec_id, slug = _record_and_instance(tmp_path)
    bundle = build_simulations(sims, instances_dir=insts)
    assert len(bundle) == 1
    assert bundle[0]["id"] == rec_id
    assert bundle[0]["results"] == [slug]


def test_bundler_refuses_a_dangling_result_slug(tmp_path):
    """A record citing an instance file the commons does not hold must not
    enter simulations.json. The WRITER accepts the bare slug (results sit
    outside the hashed claim and may land after the record); the BUNDLER is the
    gate."""
    from omai.map_data import build_simulations

    sims, insts, _rec_id, _slug = _record_and_instance(
        tmp_path, slug="no-such-instance")
    with pytest.raises(lin.LineageError, match="no committed instance"):
        build_simulations(sims, instances_dir=insts)


def test_bundler_refuses_a_slug_whose_instance_backrefs_another_run(tmp_path):
    """The cited instance exists but names a different run: the loop must not
    close on someone else's record."""
    from omai.map_data import build_simulations

    sims, insts, _rec_id, _slug = _record_and_instance(
        tmp_path, backref="0" * 64)
    with pytest.raises(lin.LineageError, match="does not backref"):
        build_simulations(sims, instances_dir=insts)


# --------------------------------------------------------------------------
# The backref loop, instance -> record: shape at record_instance, membership
# against the committed record ids at build_instances.
# --------------------------------------------------------------------------

def test_instance_carries_simulation_backref(tmp_path):
    """record_instance writes the simulation key only when set (the
    configuration-field precedent), and the bundler checks it against the
    committed record ids and carries it through."""
    from omai.map_data import _domains, build_instances, record_instance

    live = _live_uids()
    sims, insts = tmp_path / "sims", tmp_path / "insts"
    lineage = _lineage(node=_LIVE_NODE, node_uid=live[_LIVE_NODE])
    rec_path = lin.record_simulation(
        lineage=lineage, execution=_execution(), artifacts=_artifacts(),
        sim_dir=sims, name_to_uid=live)
    rec_id = json.loads(rec_path.read_text())["id"]
    path = record_instance(
        domains=_domains(), variable=_LIVE_NODE, material="Si",
        value=1.37, units="W/(m K)", source_kind="simulation",
        source_ref="gpumd", detail="HNEMD bulk Si", simulation=rec_id,
        instances_dir=insts)
    rec = json.loads(path.read_text())
    assert rec["simulation"] == rec_id
    bundle = build_instances(insts, simulations_dir=sims)
    assert bundle[0]["simulation"] == rec_id


def test_record_instance_refuses_malformed_backref(tmp_path):
    """A record id is a sha256: anything that is not 64 hex characters can
    never resolve, so the writer refuses it outright."""
    from omai.map_data import _domains, record_instance

    with pytest.raises(ValueError, match="64-hex"):
        record_instance(
            domains=_domains(), variable=_LIVE_NODE, material="Si",
            value=1.37, units="W/(m K)", source_kind="simulation",
            source_ref="gpumd", detail="bad backref", simulation="s" * 64,
            instances_dir=tmp_path)


def _handwritten_instance(insts, backref):
    insts.mkdir(parents=True, exist_ok=True)
    lineage = {"node": _LIVE_NODE, "material": "Si",
               "conditions": {"T": "300 K"},
               "values": {"value": 1.37, "units": "W/(m K)"}}
    (insts / "si-thermalconductivity-gpumd.json").write_text(json.dumps({
        "id": lin.lineage_id(lineage), "kind": "simulation",
        "lineage": lineage,
        "source": {"kind": "simulation", "ref": "gpumd", "detail": "HNEMD"},
        "simulation": backref}))


def test_build_instances_refuses_backref_to_uncommitted_record(tmp_path):
    """An instance citing a well-formed record id that no committed simulation
    record carries is a dangling citation: the bundler refuses it."""
    from omai.map_data import build_instances

    insts = tmp_path / "insts"
    _handwritten_instance(insts, "a" * 64)
    with pytest.raises(ValueError, match="matches no committed"):
        build_instances(insts, simulations_dir=tmp_path / "sims-empty")


def test_build_instances_refuses_malformed_backref(tmp_path):
    """A hand-written instance file with a non-sha256 backref fails the shape
    check before any membership lookup."""
    from omai.map_data import build_instances

    insts = tmp_path / "insts"
    _handwritten_instance(insts, "s" * 64)
    with pytest.raises(ValueError, match="64-hex"):
        build_instances(insts, simulations_dir=tmp_path / "sims-empty")


def test_committed_ids_reads_the_record_ids(tmp_path):
    assert lin.committed_ids(tmp_path / "nowhere") == set()
    live = _live_uids()
    lineage = _lineage(node=_LIVE_NODE, node_uid=live[_LIVE_NODE])
    rec_path = lin.record_simulation(
        lineage=lineage, execution=_execution(), artifacts=_artifacts(),
        sim_dir=tmp_path, name_to_uid=live)
    rec_id = json.loads(rec_path.read_text())["id"]
    assert lin.committed_ids(tmp_path) == {rec_id}


def test_instance_without_simulation_omits_key(tmp_path):
    from omai.map_data import _domains, record_instance

    doms = _domains()
    node = "ThermalConductivity[transport_model=hnemd]"
    path = record_instance(
        domains=doms, variable=node, material="Si",
        value=1.37, units="W/(m K)", source_kind="simulation",
        source_ref="gpumd", detail="no backref", instances_dir=tmp_path)
    rec = json.loads(path.read_text())
    assert "simulation" not in rec


# --------------------------------------------------------------------------
# Import: a hosted MCG-shape bundle -> a verified record (record_from_bundle).
# The reciprocal of the host's export; refuses an unverifiable bundle rather
# than mint a null-checksum record. All offline (real bytes hashed in-process).
# --------------------------------------------------------------------------

import hashlib  # noqa: E402  (import-half tests hash real bytes offline)

# Two artifact bodies with their real checksums: the well-formed MCG export
# carries a sha256 and a role per file (the companion PR's manifest shape).
_BODY_RESULT = b'{"kappa": 1.37, "units": "W/(m K)"}'
_BODY_TRAJ = b"HNEMD trajectory frames ..."
_SHA_RESULT = hashlib.sha256(_BODY_RESULT).hexdigest()
_SHA_TRAJ = hashlib.sha256(_BODY_TRAJ).hexdigest()


def _bundle(**over):
    """A realistic MCG export manifest: name/kind/template/spec, ISO
    timestamps, a current stage, headline outputs, and artifacts each carrying
    {path, bytes, sha256, role}. The spec names a real live map node so the
    resulting lineage resolves (the enriched-spec case)."""
    m = {
        "name": "Si HNEMD (local)",
        "kind": "composite_kappa",
        "template": "gpumd",
        "spec": {
            "node": _NODE,
            "material": {"name": "Si"},
            "conditions": {"T": "300 K", "potential": "Si-NEP"},
            "params": {},
        },
        "created_at": "2026-07-13T10:00:00+00:00",
        "started_at": "2026-07-13T10:00:00+00:00",
        "finished_at": "2026-07-13T11:10:10+00:00",
        "current_stage": "thermal_summary",
        "outputs": {"thermal_summary": {"kappa": 1.37, "units": "W/(m K)"}},
        "artifacts": [
            {"path": "results/thermal_summary.json", "bytes": len(_BODY_RESULT),
             "sha256": _SHA_RESULT, "role": "result"},
            {"path": "traj/dump.xyz", "bytes": len(_BODY_TRAJ),
             "sha256": _SHA_TRAJ, "role": "trajectory"},
        ],
        "skipped_artifacts": [],
    }
    m.update(over)
    return m


def test_record_from_bundle_builds_stable_record():
    """A well-formed MCG-shape manifest becomes a record whose id is stable
    (same manifest -> same id) and whose id is the lineage alone."""
    r1 = lin.record_from_bundle(_bundle(), name_to_uid=_NAME_TO_UID)
    r2 = lin.record_from_bundle(_bundle(), name_to_uid=_NAME_TO_UID)
    assert r1["id"] == r2["id"]
    assert len(r1["id"]) == 64
    # The lineage lifts the spec's node and pins the live uid.
    assert r1["lineage"]["node"] == _NODE
    assert r1["lineage"]["node_uid"] == _NODE_UID
    assert r1["lineage"]["spec"]["material"] == {"name": "Si"}
    # Execution maps template -> code, timestamps, and derives wall time.
    assert r1["execution"]["code"] == "gpumd"
    assert r1["execution"]["current_stage"] == "thermal_summary"
    assert r1["execution"]["wall_time_s"] == 4210.0  # 70 min 10 s
    # Identity is the lineage alone.
    assert r1["id"] == lin.lineage_id(r1["lineage"])


def test_record_from_bundle_artifacts_are_pointers_outside_identity():
    """Bundle artifacts are stored as POINTERS {path, role, sha256?, bytes?,
    url?}, and none of it enters identity: the lineage id is unchanged whether the
    artifacts carry checksums or not."""
    rec = lin.record_from_bundle(_bundle(), name_to_uid=_NAME_TO_UID)
    for art in rec["artifacts"]:
        assert art["path"] and art["role"]
        assert set(art) <= {"path", "role", "url", "sha256", "bytes"}
    # Identity is the lineage; the checksummed pointers do not enter it.
    assert rec["id"] == lin.lineage_id(rec["lineage"])


def test_record_from_bundle_validates_light_against_live_map():
    """The record record_from_bundle produces passes the LIGHT validator against
    the LIVE map (node resolves by id and uid pin, pointers well-formed): the
    import writer and the light gate agree on the shape."""
    from pathlib import Path

    from omai.map_data import _domains, build_graph_dict

    live = {n["id"]: n["uid"] for n in build_graph_dict(_domains())["nodes"]}
    # Rebuild against the live uid so the pin matches this checkout's node.
    man = _bundle(spec={"node": _NODE, "material": {"name": "Si"},
                        "conditions": {"T": "300 K"}, "params": {}})
    rec = lin.record_from_bundle(man, name_to_uid=live)
    report = lin.validate_light(rec, name_to_uid=live,
                                config_dir=Path("docs/data/configurations"),
                                where="bundle")
    assert report["id"] == rec["id"]
    assert report["node_resolved"] is True


def test_record_from_bundle_no_sha256_yields_pointer_record():
    """The corrected model: an unsummed bundle (artifacts {path, bytes} only, no
    sha256) is NO LONGER refused. It yields a valid record whose artifact is a
    plain pointer (a defaulted role, no sha256, outside identity)."""
    old = _bundle(artifacts=[{"path": "results/thermal_summary.json",
                              "bytes": len(_BODY_RESULT)}])
    rec = lin.record_from_bundle(old, name_to_uid=_NAME_TO_UID)
    assert rec["id"] == lin.lineage_id(rec["lineage"])
    art = rec["artifacts"][0]
    assert art["path"] == "results/thermal_summary.json"
    assert art["role"] == "artifact"          # defaulted, not rejected
    assert "sha256" not in art                # unsummed: a plain pointer
    # It validates as a light record (pointer well-formed, node resolves).
    report = lin.validate_light(rec, name_to_uid=_NAME_TO_UID, where="bundle")
    assert report["id"] == rec["id"]


def test_record_from_bundle_mixed_sha256_keeps_the_summed_pointer():
    """A bundle with one summed and one unsummed artifact keeps the sha256 on the
    summed pointer and drops it from the other; both are pointers, neither is in
    identity."""
    man = _bundle(artifacts=[
        {"path": "results/thermal_summary.json", "bytes": len(_BODY_RESULT),
         "sha256": _SHA_RESULT, "role": "result"},
        {"path": "traj/dump.xyz", "bytes": len(_BODY_TRAJ)},   # no sha256/role
    ])
    rec = lin.record_from_bundle(man, name_to_uid=_NAME_TO_UID)
    by_path = {a["path"]: a for a in rec["artifacts"]}
    assert by_path["results/thermal_summary.json"]["sha256"] == _SHA_RESULT
    assert by_path["results/thermal_summary.json"]["role"] == "result"
    assert "sha256" not in by_path["traj/dump.xyz"]
    assert by_path["traj/dump.xyz"]["role"] == "artifact"
    assert rec["id"] == lin.lineage_id(rec["lineage"])


def test_record_from_bundle_mirror_does_not_change_id():
    """Identity excludes location: adding or changing a mirror url never
    re-mints the record id (the same url-invariance the lineage id holds)."""
    base = lin.record_from_bundle(_bundle(), name_to_uid=_NAME_TO_UID)["id"]
    mirrors = {"results/thermal_summary.json":
               {"url": "https://r2.example.com/x.json", "store": "r2"}}
    with_mirror = lin.record_from_bundle(
        _bundle(), mirrors=mirrors, name_to_uid=_NAME_TO_UID)
    assert with_mirror["id"] == base
    assert with_mirror["mirrors"] == mirrors
    # The mirrored url appears on the matching artifact pointer as a convenience.
    row = next(a for a in with_mirror["artifacts"]
               if a["path"] == "results/thermal_summary.json")
    assert row["url"] == "https://r2.example.com/x.json"
    # A DIFFERENT mirror url still mints the same id.
    other = lin.record_from_bundle(
        _bundle(), mirrors={"results/thermal_summary.json":
                            {"url": "https://zenodo.org/record/9/files/x.json"}},
        name_to_uid=_NAME_TO_UID)
    assert other["id"] == base


def test_record_from_bundle_outputs_ride_outside_the_claim():
    """The bundle's headline outputs are what the run PRODUCED, not the lineage
    it was asked: they ride outside the hashed claim (like mirrors), so they
    neither pollute the lineage identity nor pretend to be validated instance
    stubs under 'results'."""
    rec = lin.record_from_bundle(_bundle(), name_to_uid=_NAME_TO_UID)
    assert rec["outputs"] == {"thermal_summary": {"kappa": 1.37,
                                                  "units": "W/(m K)"}}
    assert "results" not in rec
    # Changing only the outputs does NOT change the id (identity is the claim).
    changed = _bundle(outputs={"thermal_summary": {"kappa": 9.99}})
    assert lin.record_from_bundle(changed, name_to_uid=_NAME_TO_UID)["id"] \
        == rec["id"]


def test_record_from_bundle_omits_wall_time_when_unparseable():
    """wall_time_s is derived only when both timestamps parse as ISO; a bundle
    whose timestamps do not parse carries no wall time (never guessed)."""
    man = _bundle(started_at="not-a-timestamp", finished_at="also-not")
    rec = lin.record_from_bundle(man, name_to_uid=_NAME_TO_UID)
    assert "wall_time_s" not in rec["execution"]
    # The raw strings are still carried through as the run's stated stamps.
    assert rec["execution"]["started_at"] == "not-a-timestamp"


def test_record_from_bundle_no_node_is_flagged_not_invented():
    """A bundle whose spec names NO map node (the common MCG case: specs carry
    template physics, not a map node id) is still recorded, but the writer does
    NOT invent a node. The light validator FLAGS it node-unresolved (valid, not
    rejected): the honesty rule, whatever we have."""
    # A real MCG-style spec: template physics params, no 'node'.
    man = _bundle(spec={"temperature_K": 300.0, "potential": "Si-NEP"})
    rec = lin.record_from_bundle(man, name_to_uid=_NAME_TO_UID)
    assert "node" not in rec["lineage"]
    assert "node_uid" not in rec["lineage"]
    # The spec is kept verbatim so nothing the host asked is dropped.
    assert rec["lineage"]["spec"] == {"temperature_K": 300.0, "potential": "Si-NEP"}
    # The record is VALID and flagged node-unresolved, not rejected.
    report = lin.validate_light(rec, name_to_uid=_NAME_TO_UID, where="bundle")
    assert report["node_resolved"] is False
    assert report["node"] is None
    assert report["id"] == rec["id"]


# --------------------------------------------------------------------------
# verify_bundle_bytes: a dated byte-vs-checksum report, never a gate. Fully
# offline (real temp files, or an injected fake fetcher; no network).
# --------------------------------------------------------------------------

def test_verify_bundle_bytes_local_ok(tmp_path):
    """Local artifact_dir whose bytes hash to the manifest sha256 reports ok."""
    import datetime

    rec = lin.record_from_bundle(_bundle(), name_to_uid=_NAME_TO_UID)
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "thermal_summary.json").write_bytes(_BODY_RESULT)
    (tmp_path / "traj").mkdir()
    (tmp_path / "traj" / "dump.xyz").write_bytes(_BODY_TRAJ)
    report = lin.verify_bundle_bytes(rec, artifact_dir=tmp_path,
                                     today=datetime.date(2026, 7, 13))
    assert report["id"] == rec["id"]
    assert report["date"] == "2026-07-13"
    by_path = {c["path"]: c for c in report["checked"]}
    assert by_path["results/thermal_summary.json"]["status"] == "ok"
    assert by_path["traj/dump.xyz"]["status"] == "ok"
    assert report["summary"] == {"ok": 2, "mismatch": 0, "unchecked": 0}


def test_verify_bundle_bytes_tampered_reports_mismatch_not_raise(tmp_path):
    """Tampered local bytes are reported mismatch (with expected/actual), never
    raised: verify is a report, never a gate."""
    rec = lin.record_from_bundle(_bundle(), name_to_uid=_NAME_TO_UID)
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "thermal_summary.json").write_bytes(b"TAMPERED")
    # traj file absent -> unchecked; the result file present but wrong -> mismatch
    report = lin.verify_bundle_bytes(rec, artifact_dir=tmp_path)
    by_path = {c["path"]: c for c in report["checked"]}
    bad = by_path["results/thermal_summary.json"]
    assert bad["status"] == "mismatch"
    assert bad["expected"] == _SHA_RESULT
    assert bad["actual"] == hashlib.sha256(b"TAMPERED").hexdigest()
    assert by_path["traj/dump.xyz"]["status"] == "unchecked"
    assert report["summary"] == {"ok": 0, "mismatch": 1, "unchecked": 1}


def test_verify_bundle_bytes_no_dir_no_fetcher_reports_unchecked():
    """No artifact_dir and no fetcher: every artifact is unchecked with reason
    'bytes not reachable'. Never raises."""
    rec = lin.record_from_bundle(_bundle(), name_to_uid=_NAME_TO_UID)
    report = lin.verify_bundle_bytes(rec)
    assert all(c["status"] == "unchecked" for c in report["checked"])
    assert all(c["reason"] == "bytes not reachable" for c in report["checked"])
    assert report["summary"] == {"ok": 0, "mismatch": 0, "unchecked": 2}


def test_verify_bundle_bytes_fetcher_ok_and_mismatch():
    """With no local dir but a fetcher and mirror urls, bytes are fetched and
    compared: ok on match, mismatch on differ, unchecked on a fetch failure.
    Offline: the fetcher is a fake, no network."""
    mirrors = {
        "results/thermal_summary.json": {"url": "https://x/summary.json"},
        "traj/dump.xyz": {"url": "https://x/dump.xyz"},
    }
    rec = lin.record_from_bundle(_bundle(), mirrors=mirrors,
                                 name_to_uid=_NAME_TO_UID)

    def fetcher(url):
        if url.endswith("summary.json"):
            return _BODY_RESULT              # matches -> ok
        if url.endswith("dump.xyz"):
            return b"corrupted-in-transit"   # differs -> mismatch
        raise RuntimeError("boom")

    report = lin.verify_bundle_bytes(rec, fetcher=fetcher)
    by_path = {c["path"]: c for c in report["checked"]}
    assert by_path["results/thermal_summary.json"]["status"] == "ok"
    assert by_path["results/thermal_summary.json"]["source"] == "https://x/summary.json"
    assert by_path["traj/dump.xyz"]["status"] == "mismatch"
    assert report["summary"] == {"ok": 1, "mismatch": 1, "unchecked": 0}


def test_verify_bundle_bytes_fetch_failure_is_unchecked_not_raised():
    """A fetch that raises is reported unchecked (reason carries the error),
    never propagated: the report philosophy of verify_simulation."""
    mirrors = {"results/thermal_summary.json": {"url": "https://x/summary.json"},
               "traj/dump.xyz": {"url": "https://x/dump.xyz"}}
    rec = lin.record_from_bundle(_bundle(), mirrors=mirrors,
                                 name_to_uid=_NAME_TO_UID)

    def down(url):
        raise ConnectionError("host unreachable")

    report = lin.verify_bundle_bytes(rec, fetcher=down)
    assert all(c["status"] == "unchecked" for c in report["checked"])
    assert all("host unreachable" in c["reason"] for c in report["checked"])


def test_verify_bundle_bytes_local_wins_over_fetcher(tmp_path):
    """When both a local file and a fetcher can reach a path, the local copy is
    the authoritative check (the fetcher is not consulted for that path)."""
    mirrors = {"results/thermal_summary.json": {"url": "https://x/summary.json"}}
    rec = lin.record_from_bundle(_bundle(), mirrors=mirrors,
                                 name_to_uid=_NAME_TO_UID)
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "thermal_summary.json").write_bytes(_BODY_RESULT)

    def poisoned(url):
        raise AssertionError("fetcher must not be called when local bytes exist")

    report = lin.verify_bundle_bytes(rec, artifact_dir=tmp_path, fetcher=poisoned)
    by_path = {c["path"]: c for c in report["checked"]}
    assert by_path["results/thermal_summary.json"]["status"] == "ok"
    assert by_path["results/thermal_summary.json"]["source"] == "local"
    # The traj file is neither local nor (this test proves) fetched without a
    # crash: its url is absent from mirrors, so it is simply unchecked.
    assert by_path["traj/dump.xyz"]["status"] == "unchecked"


def test_verify_bundle_bytes_pointer_without_sha256_is_unchecked(tmp_path):
    """A pointer carrying NO sha256 (a light or unsummed-bundle artifact) is
    reported unchecked with reason 'no sha256 to verify against', even when its
    bytes are present: there is no byte claim to check, and that is not a
    failure."""
    old = _bundle(artifacts=[{"path": "results/thermal_summary.json",
                              "bytes": len(_BODY_RESULT)}])
    rec = lin.record_from_bundle(old, name_to_uid=_NAME_TO_UID)
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "thermal_summary.json").write_bytes(_BODY_RESULT)
    report = lin.verify_bundle_bytes(rec, artifact_dir=tmp_path)
    entry = report["checked"][0]
    assert entry["status"] == "unchecked"
    assert entry["reason"] == "no sha256 to verify against"
    assert report["summary"] == {"ok": 0, "mismatch": 0, "unchecked": 1}


# --------------------------------------------------------------------------
# record_light: the primary builder. Identity is the lineage alone; artifact
# pointers are optional and never enter the id.
# --------------------------------------------------------------------------

def _light_lineage(**over):
    """A full light lineage: a map node plus material, template, hyperparameters,
    and setup values (the lineage-identified experiment)."""
    r = {
        "node": _NODE,
        "node_uid": _NODE_UID,
        "material": {"name": "Si"},
        "template": "gpumd",
        "conditions": {"T": 300.0, "potential": "Si-NEP"},
        "params": {},
        "hyperparameters": {"nep_cutoff": 8.0, "neighbor": 800},
        "values": {"supercell": [4, 4, 4]},
    }
    r.update(over)
    return r


def _pointer(**over):
    p = {"path": "traj/dump.xyz", "role": "trajectory",
         "url": "https://mcg.example/runs/abc/dump.xyz", "sha256": _SHA_B}
    p.update(over)
    return p


def test_record_light_mints_stable_lineage_id():
    """record_light with a full lineage (map node + material + hyperparameters +
    values) mints a stable, lineage-derived id."""
    r1 = lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID)
    r2 = lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID)
    assert r1["id"] == r2["id"] == lin.lineage_id(_light_lineage())
    assert len(r1["id"]) == 64
    # A record with NO artifacts is valid and normal: artifacts is an empty list.
    assert r1["artifacts"] == []


def test_record_light_artifact_pointers_do_not_change_id():
    """Adding or removing artifact POINTERS does not change the id (identity is
    the lineage); the pointers are stored as {path, role, url?, sha256?}."""
    base = lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID)["id"]
    withp = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[_pointer(), _pointer(path="results/summary.json",
                                        role="result", sha256=_SHA_A)],
        name_to_uid=_NAME_TO_UID)
    assert withp["id"] == base
    assert len(withp["artifacts"]) == 2
    row = next(a for a in withp["artifacts"] if a["path"] == "traj/dump.xyz")
    assert row["url"] == "https://mcg.example/runs/abc/dump.xyz"
    assert row["sha256"] == _SHA_B
    # Removing the pointers again -> still the same id.
    assert lin.record_light(lineage=_light_lineage(),
                            name_to_uid=_NAME_TO_UID)["id"] == base


def test_record_light_hyperparameter_change_changes_id():
    """Changing a hyperparameter DOES change the id: it is part of the lineage."""
    base = lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID)["id"]
    other = lin.record_light(
        lineage=_light_lineage(hyperparameters={"nep_cutoff": 9.0, "neighbor": 800}),
        name_to_uid=_NAME_TO_UID)
    assert other["id"] != base


def test_record_light_execution_and_mirrors_ride_outside_identity():
    """execution and mirrors are carried but never hashed: the id is the lineage
    with or without them."""
    base = lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID)["id"]
    rec = lin.record_light(
        lineage=_light_lineage(),
        execution={"code": "gpumd", "code_version": "3.9.5", "seeds": [7]},
        artifacts=[_pointer(url=None, sha256=None)],
        mirrors={"traj/dump.xyz": {"url": "https://r2.example.com/dump.xyz"}},
        name_to_uid=_NAME_TO_UID)
    assert rec["id"] == base
    assert rec["execution"]["code_version"] == "3.9.5"
    assert rec["mirrors"] == {"traj/dump.xyz":
                              {"url": "https://r2.example.com/dump.xyz"}}
    # The mirror url is copied onto the pointer that had none, as a convenience.
    assert rec["artifacts"][0]["url"] == "https://r2.example.com/dump.xyz"


# --------------------------------------------------------------------------
# The optional mirror `provider` key (#23): who holds the bytes, outside
# identity, shape-checked (a string if present), round-tripping the fragment,
# and echoed onto the verify report.
# --------------------------------------------------------------------------

def test_mirror_provider_does_not_change_identity():
    """A `provider` on a mirror entry names who holds the bytes and rides
    OUTSIDE identity (mirrors never reach lineage_id): the record id is the same
    with the provider, without it, and with a different provider."""
    base = lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID)["id"]
    with_provider = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[_pointer(url=None, sha256=None)],
        mirrors={"traj/dump.xyz": {"url": "https://r2.example.com/dump.xyz",
                                   "provider": "materialscodegraph"}},
        name_to_uid=_NAME_TO_UID)
    assert with_provider["id"] == base
    assert with_provider["mirrors"]["traj/dump.xyz"]["provider"] == "materialscodegraph"
    # A DIFFERENT provider still mints the same id (free-form, never hashed).
    other = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[_pointer(url=None, sha256=None)],
        mirrors={"traj/dump.xyz": {"url": "https://r2.example.com/dump.xyz",
                                   "provider": "zenodo"}},
        name_to_uid=_NAME_TO_UID)
    assert other["id"] == base


def test_mirror_provider_round_trips_through_fragment():
    """The provider survives record_to_fragment -> record_from_fragment intact
    (it is part of the mirror layer carried in the #x= fragment)."""
    rec = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[_pointer(url=None, sha256=None)],
        mirrors={"traj/dump.xyz": {"url": "https://r2.example.com/dump.xyz",
                                   "provider": "materialscodegraph"}},
        name_to_uid=_NAME_TO_UID)
    back = lin.record_from_fragment(lin.record_to_fragment(rec))
    assert back == rec
    assert back["mirrors"]["traj/dump.xyz"]["provider"] == "materialscodegraph"


def test_mirror_provider_echoed_onto_verify_report():
    """verify_simulation echoes the mirror's provider onto that artifact's
    report entry (the provenance loop), whatever the reachability status."""
    rec = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[_pointer(url=None, sha256=_SHA_B)],
        mirrors={"traj/dump.xyz": {"url": "https://r2.example.com/dump.xyz",
                                   "provider": "materialscodegraph"}},
        name_to_uid=_NAME_TO_UID)
    report = lin.verify_simulation(rec)  # no fetcher: unreachable, still echoes
    entry = next(e for e in report["checked"] if e["path"] == "traj/dump.xyz")
    assert entry["provider"] == "materialscodegraph"


def test_mirror_provider_must_be_a_string_if_present():
    """provider is optional and free-form, but when present it must be a string:
    a non-string provider is a malformed mirror, rejected by validate_light."""
    with pytest.raises(lin.LineageError, match="provider"):
        lin.record_light(
            lineage=_light_lineage(),
            artifacts=[_pointer(url=None, sha256=None)],
            mirrors={"traj/dump.xyz": {"url": "https://r2.example.com/dump.xyz",
                                       "provider": 123}},
            name_to_uid=_NAME_TO_UID)


def test_mirror_without_provider_is_valid():
    """provider is optional: a mirror entry with only a url, and a bare url
    string mirror, both validate (provider absent is normal)."""
    rec = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[_pointer(url=None, sha256=None)],
        mirrors={"traj/dump.xyz": "https://r2.example.com/dump.xyz"},
        name_to_uid=_NAME_TO_UID)
    assert rec["mirrors"]["traj/dump.xyz"] == "https://r2.example.com/dump.xyz"


def test_record_light_no_node_no_artifacts_is_valid_and_flagged():
    """A light record with NO artifacts and NO node is valid (whatever we have):
    validate_light flags node-unresolved but does NOT reject. The lineage carries
    the template + values honestly."""
    rec = lin.record_light(lineage={"template": "gpumd",
                                   "conditions": {"T": 300.0},
                                   "values": {"pressure_GPa": 0.0}})
    assert rec["artifacts"] == []
    assert "node" not in rec["lineage"]
    report = lin.validate_light(rec)
    assert report["node_resolved"] is False
    assert report["node"] is None
    assert report["id"] == lin.lineage_id(rec["lineage"])


def test_record_light_rejects_stale_node_pin():
    """When the lineage names a node, the P3 pin discipline applies: a stale
    node_uid is a mismatch, not a silent pass."""
    with pytest.raises(lin.LineageError, match="node_uid"):
        lin.record_light(lineage=_light_lineage(node_uid="0" * 64),
                         name_to_uid=_NAME_TO_UID)


def test_record_light_pins_live_node_uid_when_absent():
    """A lineage naming a node but no pin gets the live uid attached (so a later
    validation is a real match), pulled from name_to_uid."""
    lineage = _light_lineage()
    lineage.pop("node_uid")
    rec = lin.record_light(lineage=lineage, name_to_uid=_NAME_TO_UID)
    assert rec["lineage"]["node_uid"] == _NODE_UID


# --------------------------------------------------------------------------
# The configuration pin on the light path (the primary builder): material is
# inside lineage_id, so an unresolved structure pin is an identity-contract
# gap, not a cosmetic one. record_light/validate_light must catch it exactly
# as the heavy writer does, without a caller having to opt in.
# --------------------------------------------------------------------------

def test_record_light_rejects_unknown_configuration():
    """A light record pinning a configuration uid absent from the real
    docs/data/configurations/ is rejected by record_light itself: config_dir
    now defaults to the real directory, so this is caught without a caller
    passing anything extra."""
    lineage = _light_lineage(
        material={"name": "Si", "configuration": "sha256:" + "9" * 64})
    with pytest.raises(lin.LineageError, match="configuration"):
        lin.record_light(lineage=lineage, name_to_uid=_NAME_TO_UID)


def test_record_light_accepts_the_committed_si_configuration():
    """A light record pinning the real, committed Si configuration uid (the
    si-diamond-primitive-mp-149 record under docs/data/configurations/)
    resolves and passes, against the default config_dir."""
    committed = json.loads(
        Path("docs/data/configurations/si-diamond-primitive-mp-149.json")
        .read_text())
    uid = committed["canonical"]["uid"]
    lineage = _light_lineage(material={"name": "Si", "configuration": uid})
    rec = lin.record_light(lineage=lineage, name_to_uid=_NAME_TO_UID)
    assert rec["id"] == lin.lineage_id(lineage)


def test_record_light_bare_material_name_without_configuration_still_passes():
    """The common case: a material with no configuration key at all is not
    affected by the now-default config_dir (no regression). No node either,
    so name_to_uid is never consulted."""
    lineage = {"material": {"name": "Si"}, "template": "gpumd",
              "conditions": {"T": 300.0}}
    rec = lin.record_light(lineage=lineage)
    assert rec["id"] == lin.lineage_id(lineage)
    report = lin.validate_light(rec)
    assert report["id"] == rec["id"]


def test_record_light_nodeless_malformed_result_stub_rejected():
    """A node-less light record (no map node named, so record_light never
    builds a node_to_uid map on its own) still validates a results[] stub: a
    stub missing the required instance keys is rejected, not silently let
    through because there was no node to force the map build."""
    lineage = {"template": "gpumd", "conditions": {"T": 300.0}}
    bad_stub = {"variable": _NODE}  # missing material/conditions/value/units/source
    with pytest.raises(lin.LineageError, match="missing"):
        lin.record_light(lineage=lineage, results=[bad_stub])


# --------------------------------------------------------------------------
# Artifact-pointer well-formedness (validate_light / _validate_pointers).
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"role": "trajectory"},                                   # missing path
    {"path": "", "role": "trajectory"},                       # empty path
    {"path": "traj/dump.xyz"},                                # missing role
    {"path": "traj/dump.xyz", "role": ""},                    # empty role
    {"path": "x", "role": "r", "url": 5},                     # url not a string
    {"path": "x", "role": "r", "sha256": "tooshort"},         # bad sha256 shape
])
def test_light_pointer_wellformedness_rejected(bad):
    """A pointer missing path or role is rejected; a url present must be a
    string and a sha256 present must be 64-hex (shape-only)."""
    with pytest.raises(lin.LineageError, match="artifact"):
        lin.record_light(lineage=_light_lineage(), artifacts=[bad],
                         name_to_uid=_NAME_TO_UID)


def test_light_pointer_minimal_path_role_accepted():
    """path + role with no url and no sha256 is a legal pointer (the minimum)."""
    rec = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[{"path": "log/run.log", "role": "log"}],
        name_to_uid=_NAME_TO_UID)
    assert rec["artifacts"] == [{"path": "log/run.log", "role": "log"}]


def test_light_stated_id_mismatch_rejected():
    """A record whose stated id does not recompute from its lineage (someone
    folded an artifact or url into the hash) is rejected."""
    with pytest.raises(lin.LineageError, match="lineage"):
        lin.validate_light({"id": "0" * 64, "lineage": _light_lineage()},
                           name_to_uid=_NAME_TO_UID)


# --------------------------------------------------------------------------
# The URL round-trip: record_to_fragment <-> record_from_fragment, matching the
# playground's gzip fragment scheme (gzip, base64url, '=' stripped, 'g' prefix).
# --------------------------------------------------------------------------

def test_fragment_round_trip_is_identity_stable():
    """record -> fragment -> record round-trips: the record is identical and the
    lineage id is unchanged."""
    rec = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[_pointer()],
        execution={"code": "gpumd", "seeds": [7]},
        name_to_uid=_NAME_TO_UID)
    frag = lin.record_to_fragment(rec)
    back = lin.record_from_fragment(frag)
    assert back == rec
    assert back["id"] == rec["id"] == lin.lineage_id(rec["lineage"])


def test_fragment_is_base64url_no_plus_slash_or_padding():
    """The fragment is base64url with a mode prefix: no '+', '/', or '=' (so it
    is safe in a URL fragment), and it starts with 'g' (gzip)."""
    frag = lin.record_to_fragment(
        lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID))
    assert frag[0] == "g"
    body = frag[1:]
    assert "+" not in body and "/" not in body and "=" not in body


def test_fragment_is_byte_reproducible_and_pins_gzip_mtime():
    """The same record always encodes to the same fragment bytes.

    A gzip header carries a 4-byte MTIME field. Through Python 3.12
    ``gzip.compress`` defaulted it to the wall clock, so one unchanged record
    encoded twice yielded two different fragments and the committed
    docs/examples/index.json fragments could not be regenerated byte-identically.
    Nothing decoded wrong, which is exactly why no other test caught it: the
    round-trip guards above compare DECODED records, so they pass either way.
    This one compares bytes.

    Read honestly: Python 3.13 pins MTIME to 0 by default, so on 3.13+ this
    passes with or without the fix. The CI matrix is 3.11 and 3.12, which are
    the versions that embed the clock, so the guard is live where the defect
    lives. The explicit MTIME assertion below holds on every version and is the
    part that pins the intent rather than the symptom.
    """
    import base64
    import time

    rec = lin.record_light(
        lineage=_light_lineage(),
        artifacts=[_pointer()],
        execution={"code": "gpumd", "seeds": [7]},
        name_to_uid=_NAME_TO_UID)

    first = lin.record_to_fragment(rec)
    time.sleep(1.1)  # longer than the 1 second MTIME resolution
    second = lin.record_to_fragment(rec)
    assert first == second, (
        "record_to_fragment is not byte-reproducible: the same record encoded "
        "twice produced different fragments")

    # The intent, not the symptom: MTIME is pinned to zero, on every version.
    body = first[1:]
    packed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    assert packed[:2] == b"\x1f\x8b", "not a gzip stream"
    assert int.from_bytes(packed[4:8], "little") == 0, (
        "gzip MTIME field is not pinned to 0")

    # Still a real fragment: it round-trips to the identical record.
    assert lin.record_from_fragment(first) == rec


def test_fragment_from_fragment_strips_x_prefix():
    """record_from_fragment tolerates a leading '#x=' or 'x=' (a pasted fragment
    key), decoding the payload after it."""
    rec = lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID)
    frag = lin.record_to_fragment(rec)
    assert lin.record_from_fragment("#x=" + frag) == rec
    assert lin.record_from_fragment("x=" + frag) == rec


def test_hand_built_gzip_fragment_decodes():
    """A fragment built by hand with the playground's exact lineage (gzip, then
    urlsafe base64 with '=' stripped, 'g' prefix) decodes to the record: this is
    the interop contract with docs/play/index.html's gzipToB64url."""
    import base64
    import gzip
    import json

    rec = {"id": lin.lineage_id({"template": "gpumd", "conditions": {"T": 300.0}}),
           "lineage": {"template": "gpumd", "conditions": {"T": 300.0}},
           "artifacts": []}
    blob = json.dumps(rec, separators=(",", ":")).encode("utf-8")
    hand = "g" + base64.urlsafe_b64encode(gzip.compress(blob)).decode().rstrip("=")
    assert lin.record_from_fragment(hand) == rec


def test_raw_mode_fragment_decodes():
    """The 'r' (raw, uncompressed) fallback the playground emits when
    CompressionStream is unavailable also decodes."""
    import base64
    import json

    rec = lin.record_light(lineage=_light_lineage(), name_to_uid=_NAME_TO_UID)
    blob = json.dumps(rec, separators=(",", ":")).encode("utf-8")
    raw = "r" + base64.urlsafe_b64encode(blob).decode().rstrip("=")
    assert lin.record_from_fragment(raw) == rec


def test_legacy_recipe_record_validates_with_same_id():
    lineage = {"template": "gpumd", "conditions": {"T": 300.0}}
    record_id = lin.lineage_id(lineage)
    current = {"id": record_id, "lineage": lineage, "artifacts": []}
    legacy = {"id": record_id, "recipe": lineage, "artifacts": []}

    assert lin.validate_light(legacy) == lin.validate_light(current)
    assert lin.validate_light(legacy)["id"] == record_id


def test_legacy_recipe_fragment_decodes_and_validates():
    import base64
    import gzip

    lineage = {"template": "gpumd", "conditions": {"T": 300.0}}
    legacy = {
        "id": lin.lineage_id(lineage),
        "recipe": lineage,
        "artifacts": [],
    }
    blob = json.dumps(legacy, separators=(",", ":")).encode("utf-8")
    fragment = "g" + base64.urlsafe_b64encode(gzip.compress(blob)).decode().rstrip("=")

    decoded = lin.record_from_fragment(fragment)
    assert decoded == legacy
    assert lin.validate_light(decoded)["id"] == legacy["id"]


def test_fragment_serialization_upgrades_legacy_recipe_key():
    lineage = {"template": "gpumd", "conditions": {"T": 300.0}}
    legacy = {
        "id": lin.lineage_id(lineage),
        "recipe": lineage,
        "artifacts": [],
    }

    decoded = lin.record_from_fragment(lin.record_to_fragment(legacy))
    assert decoded["lineage"] == lineage
    assert "recipe" not in decoded


def test_record_with_lineage_and_legacy_recipe_keys_is_rejected():
    lineage = {"template": "gpumd", "conditions": {"T": 300.0}}
    malformed = {"lineage": lineage, "recipe": lineage}

    with pytest.raises(lin.LineageError, match="both"):
        lin.validate_light(malformed)


def test_simulations_shim_is_gone():
    # The transition shim was removed once MaterialsCodeGraph confirmed its
    # oracle imports omai.lineages directly (dvnclabs/materialscodegraph#109,
    # #111). The WIRE still reads the legacy "recipe" key forever; only the
    # legacy import path is retired.
    with pytest.raises(ModuleNotFoundError):
        import omai.simulations  # noqa: F401


def test_fragment_unknown_mode_rejected():
    with pytest.raises(lin.LineageError, match="mode"):
        lin.record_from_fragment("zabcdef")
