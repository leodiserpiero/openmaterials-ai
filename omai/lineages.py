"""The lineage layer: a light, lineage-identified, URL-first run record.

A lineage record is the shareable unit of a computation, and it is LIGHT: it
stores whatever we have of an experiment. The primary path is the lineage (the
map node when it is known, else a template plus its hyperparameters and setup
values), with NO fixed reproducibility guarantee and NO required heavy input
files. Heavy artifacts (structures, force sets, trajectories) are OPTIONAL and
POINTER-ONLY: an entry ``{path, role, url?, sha256?}`` where only ``path`` and
``role`` are required and a ``url`` (an MCG object, a Zenodo DOI) and its
``sha256`` are optional. A record with NO artifacts at all is valid and normal.
Identity comes from the LINEAGE, never from a per-artifact manifest.

The record is URL-FIRST: :func:`record_to_fragment` gzip+base64url-encodes a
light record into a link fragment, exactly the scheme the map-view sharing in
``docs/play/index.html`` uses (a ``g`` gzip prefix, base64url, ``=`` stripped),
so an experiment IS a link that opens in the openmaterials playground under
``#x=`` and, when its lineage names a map node, lights that node on the map. A
Python-produced fragment and a browser-produced one interoperate.

This is deliberately a two-tier design. OpenMaterials is the open FORMAT (this
data structure, host-agnostic); MaterialsCodeGraph (MCG) is the cheap host for
the heavy bytes, referenced by pointer, never embedded in identity. The heavy,
byte-verifiable path (:func:`record_from_bundle` over a checksummed bundle,
:func:`verify_bundle_bytes` over its bytes) still exists as an OPTIONAL
enrichment for when a full manifest DOES exist and someone wants byte
verification; it is no longer the primary path, and it no longer refuses a
bundle whose artifacts lack a sha256 (those become pointers, outside identity).

Records live in ``docs/data/simulations/<slug>.json`` when written, never on the
map page itself. The structural siblings are the value, configuration, and
spectrum records.

Identity (the content-addressing rule, this module's protocol commitment):

    record_id = sha256 of the canonical JSON of exactly the LINEAGE. Identity is
    the asked computation, nothing else. The lineage is the map ``node`` (with a
    ``node_uid`` pin when a map node is known, else absent), the ``material``,
    the ``template`` (the code family, when there is no map node), the
    ``conditions``, ``params``, ``hyperparameters``, and setup ``values``.
    Everything else a record carries is OUTSIDE the hash: ``execution`` (what
    ran: code, version, seeds, wall time), ``artifacts`` (the optional pointer
    list), ``mirrors`` (the resolver), and ``results``. This is deliberate: an
    experiment's identity must not change when you attach a trajectory pointer,
    move its bytes to a new MCG url, re-run it on a different pod, or bundle the
    values it produced. Two records with the same lineage are the same
    experiment; a different node, material, template, condition, param,
    hyperparameter, or setup value is a different one.

    Artifacts NEVER enter identity. An artifact is a pointer
    ``{path, role, url?, sha256?}``, and even its sha256 is a claim about bytes
    for verification, not a component of the lineage id. Adding, removing, or
    re-hosting an artifact pointer leaves the record id unchanged. ``results``
    is likewise outside: a result instance carries the record id as its
    ``simulation`` backref, so it cannot sit inside the payload that mints that
    id (a circular reference).

Canonicalization (the configurations precedent, extended to the lineage): sorted
keys, compact separators, and an explicit float rule. Every float inside
``conditions``, ``params``, ``hyperparameters``, and ``values`` (at any nesting
depth) is rounded to :data:`FLOAT_DECIMALS` decimals before hashing, so
refetch-level numerical noise in a shared lineage collapses to one identity while
physically distinct runs stay apart; booleans and integers are preserved
exactly. Because identity is the lineage alone, there is no float anywhere else
in the hashed payload. The canonical blob is
``json.dumps(lineage, sort_keys=True, separators=(",", ":"))`` and the id is its
sha256; this rule is a protocol commitment, not an implementation detail.

Validation (gate-shaped, cheap, deterministic, and HONEST about gaps): the light
validator holds a lineage to "whatever we have". When the lineage names a ``node``,
it must resolve against the live map by BOTH id and content uid (the instances
node-pin discipline: a stale pin is a mismatch, not a silent pass); when it does
NOT, the record is valid and simply carries its template plus values, FLAGGED as
node-unresolved rather than rejected (the honesty rule). Artifact pointers, when
present, must be well-formed: ``path`` and ``role`` non-empty, ``url`` a string
if present, ``sha256`` a 64-hex string if present; nothing about artifacts is
mandatory. The heavy validator (:func:`_validate`) additionally checks the
execution block and any named configuration and result instances, for the
enrichment path that carries them.

Verification (a report, NEVER a gate): :func:`verify_simulation` checks the
mirrors for reachability and checksum match when URLs are present and returns a
dated report. A mirror entry may carry an optional free-form ``provider`` string
naming who holds the bytes (e.g. "materialscodegraph", "zenodo"), outside
identity like the rest of the mirror layer and echoed onto the report; the
commons standardizes the key, not a registry of values. A record whose bytes
moved or whose mirror is down is stale, not invalid; the map owns identity, MCG
owns the bytes.

The heavy import/verify path (an OPTIONAL enrichment): :func:`record_from_bundle`
maps a hosted MCG-style bundle manifest onto the record shape and mints the same
lineage-derived id. It does NOT require checksums: an artifact carrying a sha256
is kept as a verifiable pointer, one lacking a sha256 becomes a plain pointer
(``{path, role, url?}``, outside identity exactly like the light path), and the
id comes from the lineage regardless. :func:`verify_bundle_bytes` then reports
(never gates) whether each artifact's bytes match its checksum when a checksum is
present and the bytes are locally or fetchably reachable. A full checksummed
bundle therefore ENRICHES a record with verifiable bytes but is never REQUIRED.

The share ENVELOPE (multi-lineage bundles): one link can carry SEVERAL records
plus shared document context. :func:`envelope` builds
``{"v": 1, "doc"?: {...}, "lineages": [record, ...]}`` (``doc`` is publication
metadata for a parsed paper: ``source`` as a "scheme:ref" string, ``title``,
``authors``, ``year``, ``journal``, extras tolerated);
:func:`envelope_to_fragment` / :func:`envelope_from_fragment` are the transport
(same gzip+base64url+mode-prefix scheme, unchanged). The read side is DUAL:
:func:`envelope_from_fragment` also accepts every legacy bare-record fragment,
normalizing it to a one-element envelope with no doc, so no existing link ever
breaks. Each member keeps its own lineage-derived id (bundling never re-mints
one); the BUNDLE gets a derived :func:`bundle_id`, the sha256 of the canonical
JSON of ``{"doc": doc-or-null, "ids": [member ids in order]}``. A member with
no source inherits ``doc.source`` at read/display time only, never into its
hash: sharing a record solo or inside a bundle mints the same id.

Entry points: :func:`record_light` (build a light, lineage-identified record; the
primary builder), :func:`record_to_fragment` / :func:`record_from_fragment` (the
URL round-trip, interoperable with the playground's gzip fragment),
:func:`envelope` / :func:`bundle_id` / :func:`envelope_to_fragment` /
:func:`envelope_from_fragment` (the multi-lineage share bundle),
:func:`record_simulation` (write a record to ``docs/data/simulations/``,
idempotent on identical content, refusing a silent overwrite, the
``record_instance`` discipline), :func:`record_from_bundle` (build a record from
a hosted bundle, checksums optional), :func:`verify_simulation` /
:func:`verify_bundle_bytes` (the dated reports). Bundling into
``docs/data/simulations.json`` lives in :mod:`omai.map_data`
(``build_simulations``), mirroring the other record kinds.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SIM_DIR = _REPO_ROOT / "docs" / "data" / "simulations"
_CONFIG_DIR = _REPO_ROOT / "docs" / "data" / "configurations"

# Decimals every float in a lineage's conditions/params/hyperparameters/values is
# rounded to before hashing. Six collapses refetch-level noise in a shared lineage
# (e.g. a temperature written 300.0000001) to one identity while keeping
# physically distinct runs apart. The configurations layer rounds coordinates to
# 5; lineage scalars are physical, not fractional coordinates, so 6 is the
# resolution here.
FLOAT_DECIMALS = 6

# The lineage keys whose floats are rounded before hashing (see FLOAT_DECIMALS).
# These are the setup dials of an experiment; a float elsewhere is not a lineage
# scalar and is not part of identity in the light model (identity is the lineage).
_ROUNDED_LINEAGE_KEYS = ("conditions", "params", "hyperparameters", "values")

# A sha256 hex digest: 64 lowercase hex characters. Artifact pointers and result
# backrefs are checked against this shape (a hex64), never guessed.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The source of a lineage, when it has one, is a compact namespaced string
# "scheme:ref" (paper:cnt-2021-barbalinardo, doi:10.1103/PhysRevLett.127.025902,
# zotero:ABC123, ...). It lives INSIDE the lineage object, so it is
# identity-bearing: the same claim from the same source mints the same id
# (deduplication), while two values from one paper stay distinct records.
# The scheme set is open; SOURCE_SCHEMES names the ones the ecosystem already
# speaks. Legacy records carry a top-level record["source"] OUTSIDE identity;
# that placement stays readable forever and never re-mints an old id.
SOURCE_SCHEMES = ("paper", "doi", "zotero", "arxiv", "repo", "dataset")
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9+.-]*:.+$")

# The share ENVELOPE: the multi-lineage bundle a link fragment carries. One
# parsed paper reports several values; the envelope lets one link carry them
# all, with the publication metadata stated ONCE as shared context. Version 1
# is {"v": 1, "doc": {...}?, "lineages": [record, ...]}: "doc" (optional) is
# the shared document context (recognized keys: source, title, authors, year,
# journal; extras tolerated), "lineages" a non-empty list of ordinary light
# records. Decoders accept BOTH a bare record (every existing link, forever)
# and the envelope, normalizing a bare record to a one-element envelope with
# no doc; encoders always emit the envelope. Bundling NEVER changes a member's
# id (identity stays the member's own lineage), and a member with no source
# inherits doc.source at read/display time only, never into its hash.
ENVELOPE_VERSION = 1

# The machine-readable descriptor of the instance format: the half of THE
# LINEAGE (the one versioned schema artifact; the other half is the derivation
# graph) that states what an instance is. Assembled from this module's real
# constants so it can never drift from the reference implementation; the site
# builder folds it, with the graph, into docs/data/lineage.json and the rolling
# lineage_version.
FORMAT_DESCRIPTOR = {
    "record_fields": ["id", "lineage", "kind", "execution", "artifacts",
                      "mirrors", "results", "source"],
    "identity": {
        "rule": "sha256 of the canonical JSON of the lineage object alone",
        "canonical_json": {"sort_keys": True, "separators": [",", ":"]},
        "float_decimals": FLOAT_DECIMALS,
        "rounded_lineage_keys": list(_ROUNDED_LINEAGE_KEYS),
    },
    "wire": {
        "write_key": "lineage",
        "read_keys": ["lineage", "recipe"],
        "both_keys": "malformed",
        "version_write_key": "lineage_version",
        "version_read_keys": ["lineage_version", "map_version"],
        "fragment": {"gzip_prefix": "g", "raw_prefix": "r",
                     "encoding": "base64url"},
    },
    "source": {
        "placement": "lineage.source (identity-bearing); legacy top-level "
                     "record.source rides outside identity",
        "shape": "scheme:ref",
        "schemes": list(SOURCE_SCHEMES),
        "open_scheme_set": True,
    },
    "envelope": {
        "v": ENVELOPE_VERSION,
        "keys": ["v", "doc", "lineages"],
        "doc": {
            "keys": ["source", "title", "authors", "year", "journal"],
            "extra_keys": "tolerated",
            "source_shape": "scheme:ref (the lineage source grammar)",
        },
        "lineages": "a non-empty list of light records, each keeping its own "
                    "lineage-derived id",
        "bundle_id": {
            "rule": "sha256 of the canonical JSON of "
                    "{\"doc\": doc-or-null, \"ids\": [member ids in order]}",
            "canonical_json": {"sort_keys": True, "separators": [",", ":"]},
            "member_ids": "bundling never changes a member's id: identity "
                          "stays the member's own lineage",
        },
        "inheritance": "a member with no source inherits doc.source at "
                       "read/display time only, never into its hash",
        "dual_read": "decoders accept a bare record or an envelope; a bare "
                     "record normalizes to a one-element envelope with no "
                     "doc; encoders always emit the envelope",
    },
}


def format_rules_version() -> str:
    """The content hash of :data:`FORMAT_DESCRIPTOR` (the format half of the
    Lineage artifact's rolling version)."""
    payload = json.dumps(FORMAT_DESCRIPTOR, sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "LineageError",
    "ENVELOPE_VERSION",
    "FORMAT_DESCRIPTOR",
    "SOURCE_SCHEMES",
    "format_rules_version",
    "canonical_id",
    "lineage_id",
    "record_lineage",
    "committed_ids",
    "record_light",
    "record_simulation",
    "record_from_bundle",
    "record_to_fragment",
    "record_from_fragment",
    "envelope",
    "bundle_id",
    "envelope_to_fragment",
    "envelope_from_fragment",
    "validate_light",
    "verify_simulation",
    "verify_bundle_bytes",
    "slugify",
]


class LineageError(Exception):
    """A lineage record failed validation, or could not be written."""


def record_lineage(record: dict, *, where: str = "<record>") -> dict:
    """Return a record's lineage, accepting the legacy ``recipe`` key.

    ``lineage`` is preferred for current records. Existing shared links may
    still carry ``recipe`` and remain readable, but carrying both keys is
    ambiguous and therefore malformed.
    """
    if not isinstance(record, dict):
        raise LineageError(f"{where}: record must be an object")
    has_lineage = "lineage" in record
    has_recipe = "recipe" in record
    if has_lineage and has_recipe:
        raise LineageError(
            f"{where}: record must not carry both 'lineage' and legacy "
            f"'recipe' keys")
    if has_lineage:
        return record["lineage"]
    if has_recipe:
        return record["recipe"]
    raise LineageError(f"{where}: missing 'lineage'")


def slugify(text: str) -> str:
    """Lowercase, non-alphanumerics to single hyphens, trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


# --------------------------------------------------------------------------
# Canonicalization / identity.
# --------------------------------------------------------------------------

def _round_floats(obj):
    """Recursively round every float to FLOAT_DECIMALS; leave all else intact.

    Booleans are left exactly (a bool is an int in Python, never a float here);
    integers pass through; dict keys are preserved. This normalizes the lineage
    scalars (conditions, params, hyperparameters, values) so that a shared lineage
    carrying refetch-level float noise collapses to one identity.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return round(obj, FLOAT_DECIMALS)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v) for v in obj]
    return obj


def _canonical_lineage(lineage: dict) -> dict:
    """The lineage as it enters the identity hash: a shallow copy with each of
    conditions/params/hyperparameters/values float-normalized. No other key is
    touched (node, node_uid, material, template pass through exactly), and no
    non-lineage field (execution, artifacts, mirrors, results) is ever present:
    identity is the lineage alone.
    """
    if not isinstance(lineage, dict):
        return lineage
    normalized = dict(lineage)
    for key in _ROUNDED_LINEAGE_KEYS:
        if key in normalized:
            normalized[key] = _round_floats(normalized[key])
    return normalized


def lineage_id(lineage: dict) -> str:
    """The content-addressed id of an experiment: sha256 of the canonical lineage.

    Identity is the asked computation and nothing else. Two records with the
    same lineage (same node/template, material, conditions, params,
    hyperparameters, and setup values, up to :data:`FLOAT_DECIMALS`) mint the
    same id; a different lineage mints a different one. Artifacts, execution,
    mirrors, and results are all OUTSIDE this hash: attaching a trajectory
    pointer, moving its bytes, re-running on another pod, or bundling the values
    produced never re-mints the id.
    """
    blob = json.dumps(_canonical_lineage(lineage),
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def canonical_id(lineage: dict, execution: dict | None = None,
                 artifacts: list[dict] | None = None) -> str:
    """The record id, back-compatible signature: sha256 of the canonical LINEAGE.

    Identity is lineage-only in the light model, so ``execution`` and
    ``artifacts`` are accepted (older call sites pass the triple) but ignored:
    only the lineage determines the id. New code should call :func:`lineage_id`.
    """
    return lineage_id(lineage)


def _pointer(art: dict) -> dict:
    """One artifact reduced to a POINTER: ``{path, role}`` always, plus ``url``,
    ``sha256``, and ``bytes`` when present. Only path and role are required; the
    url is a location (an MCG object, a mirror), the sha256 a byte claim for
    verification, the bytes an optional size. None of it enters identity
    (:func:`lineage_id` never sees an artifact); this is purely the stored shape
    of an optional pointer, whether it came from a light record or a bundle
    (where an artifact lacking a sha256 becomes a plain pointer, no exception).
    """
    row = {"path": art.get("path"), "role": art.get("role")}
    url = art.get("url")
    if url is not None:
        row["url"] = url
    sha = art.get("sha256")
    if sha is not None:
        row["sha256"] = sha
    nbytes = art.get("bytes")
    if nbytes is not None:
        row["bytes"] = nbytes
    return row


def _manifest_entry(art: dict) -> dict:
    """The hashed-manifest view of one artifact for the STRICT heavy writer:
    exactly ``{path, bytes, sha256, role}``. Used only by :func:`record_simulation`
    (the checksummed writer) and its four-key stored row; the light and bundle
    paths use :func:`_pointer`. Any ``url`` on the input is dropped here.
    """
    return {
        "path": art.get("path"),
        "bytes": art.get("bytes"),
        "sha256": art.get("sha256"),
        "role": art.get("role"),
    }


# --------------------------------------------------------------------------
# Validation (the cheap, deterministic gate).
# --------------------------------------------------------------------------

def _validate_manifest(artifacts, *, where: str) -> None:
    """Every manifest entry is well-formed: sha256 hex64, bytes positive int,
    role non-empty, path non-empty. Malformed bytes are a data error, not a
    reachability question (that is verify's job).
    """
    if not isinstance(artifacts, list):
        raise LineageError(f"{where}: artifacts must be a list")
    for i, art in enumerate(artifacts):
        if not isinstance(art, dict):
            raise LineageError(f"{where}: artifact {i} must be an object")
        path = art.get("path")
        if not isinstance(path, str) or not path:
            raise LineageError(f"{where}: artifact {i} path must be a non-empty string")
        nbytes = art.get("bytes")
        if isinstance(nbytes, bool) or not isinstance(nbytes, int) or nbytes <= 0:
            raise LineageError(
                f"{where}: artifact {i} bytes must be a positive integer")
        sha = art.get("sha256")
        if not isinstance(sha, str) or not _SHA256_RE.match(sha):
            raise LineageError(
                f"{where}: artifact {i} sha256 must be a 64-hex-character string")
        role = art.get("role")
        if not isinstance(role, str) or not role:
            raise LineageError(f"{where}: artifact {i} role must be non-empty")


def _validate_pointers(artifacts, *, where: str) -> None:
    """Every artifact POINTER is well-formed, and nothing about it is mandatory.

    The light model's artifact contract: a pointer is ``{path, role, url?,
    sha256?}`` where ``path`` and ``role`` are required non-empty strings, ``url``
    (when present) is a string, and ``sha256`` (when present) is a 64-hex string.
    A record with NO artifacts is valid (the list may be empty or absent);
    ``bytes`` (when present) is optional metadata and is not constrained here.
    None of this enters identity; this is a shape check on an optional list.
    """
    if artifacts is None:
        return
    if not isinstance(artifacts, list):
        raise LineageError(f"{where}: artifacts must be a list")
    for i, art in enumerate(artifacts):
        if not isinstance(art, dict):
            raise LineageError(f"{where}: artifact {i} must be an object")
        path = art.get("path")
        if not isinstance(path, str) or not path:
            raise LineageError(
                f"{where}: artifact {i} path must be a non-empty string")
        role = art.get("role")
        if not isinstance(role, str) or not role:
            raise LineageError(
                f"{where}: artifact {i} role must be a non-empty string")
        url = art.get("url")
        if url is not None and not isinstance(url, str):
            raise LineageError(
                f"{where}: artifact {i} url, when present, must be a string")
        sha = art.get("sha256")
        if sha is not None and (not isinstance(sha, str) or not _SHA256_RE.match(sha)):
            raise LineageError(
                f"{where}: artifact {i} sha256, when present, must be a "
                f"64-hex-character string")


def _validate_mirrors(mirrors, *, where: str) -> None:
    """Every mirror entry is well-formed, and nothing about it is mandatory.

    The resolver layer maps an artifact path to a location, either a bare url
    string or an object ``{url?, provider?, ...}``. It is OUTSIDE identity
    (:func:`lineage_id` never sees it), so this is only a shape check on an
    optional dict: when a mirror entry is an object, its ``url`` (if present) is
    a string and its ``provider`` (if present) is a string. ``provider`` is the
    optional free-form name of who holds the bytes (e.g. "materialscodegraph",
    "zenodo"); the commons standardizes the KEY, not a registry of values, so a
    provider is any string. An absent or empty mirror layer is normal.
    """
    if mirrors is None:
        return
    if not isinstance(mirrors, dict):
        raise LineageError(f"{where}: mirrors must be an object (path -> location)")
    for path, loc in mirrors.items():
        if isinstance(loc, str) or loc is None:
            continue
        if not isinstance(loc, dict):
            raise LineageError(
                f"{where}: mirror {path!r} must be a url string or an object")
        url = loc.get("url")
        if url is not None and not isinstance(url, str):
            raise LineageError(
                f"{where}: mirror {path!r} url, when present, must be a string")
        provider = loc.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise LineageError(
                f"{where}: mirror {path!r} provider, when present, must be a "
                f"string (who holds the bytes; free-form)")


def _validate_execution(execution, *, where: str) -> None:
    """The execution block is well-formed: an object naming the code that ran.

    The block is the record's "what ran" claim (code, version, container
    digest, runner, wall time, seeds); everything in it is optional EXCEPT that
    the block is an object and ``code`` is a non-empty string. A run
    necessarily ran some code; a record whose execution is null, a bare string,
    or an empty object is degenerate and fails here, the manifest gate's
    well-formedness bar applied to the execution claim.
    """
    if not isinstance(execution, dict):
        raise LineageError(f"{where}: execution must be an object")
    code = execution.get("code")
    if not isinstance(code, str) or not code:
        raise LineageError(
            f"{where}: execution.code must be a non-empty string (the code "
            f"that ran)")


def _validate_lineage_node(lineage, name_to_uid, *, where: str) -> None:
    """The lineage's node resolves against the live map by id AND uid pin.

    ``lineage.node`` is the map node id; ``lineage.node_uid`` (when the record
    carries one, the pin) must equal the live content uid of that id. A stale
    pin is a mismatch that fails the gate, never a silent pass: the instances
    node-pin discipline, applied to the lineage's node.
    """
    if not isinstance(lineage, dict):
        raise LineageError(f"{where}: lineage must be an object")
    node = lineage.get("node")
    if not isinstance(node, str) or node not in name_to_uid:
        raise LineageError(f"{where}: lineage.node {node!r} is not a live map node")
    pin = lineage.get("node_uid")
    if pin is not None and pin != name_to_uid[node]:
        raise LineageError(
            f"{where}: lineage.node_uid pin {str(pin)[:12]} does not match the "
            f"live uid {name_to_uid[node][:12]} of node {node!r}")


def _validate_configuration(lineage, *, config_dir: Path, where: str) -> None:
    """A named configuration uid exists under docs/data/configurations/.

    The configuration is optional (a bare material name is legitimate); when
    the lineage pins one, it must resolve to a committed configuration record.
    """
    material = lineage.get("material") or {}
    uid = material.get("configuration") if isinstance(material, dict) else None
    if uid is None:
        return
    # A lineage may carry the uid bare or as "sha256:<uid>"; accept both.
    wanted = uid.split(":", 1)[1] if isinstance(uid, str) and uid.startswith("sha256:") else uid
    if config_dir.exists():
        for path in config_dir.glob("*.json"):
            try:
                rec = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if rec.get("canonical", {}).get("uid") == wanted:
                return
    raise LineageError(
        f"{where}: configuration {str(wanted)[:12]} is not a committed "
        f"configuration record under {config_dir.name}/")


def _validate_results(results, record_id, name_to_uid, *, where: str,
                      instances_dir: Path | None = None) -> None:
    """Bundled result instances pass the existing instance checks and backref
    this record. A result may be a bare backref slug (a string, pointing at an
    already-committed instance file) or an inline instance stub (a dict). An
    inline stub is held to the instance contract (the same required keys as
    ``build_instances``, ``conditions`` included) AND must carry
    ``simulation == record_id``: the run owns the values it produced. A slug is
    accepted bare by the writer (results sit outside the hashed claim and may
    be appended after the instance lands); when ``instances_dir`` is given (the
    bundler), the slug must resolve to a committed instance file that backrefs
    this record.
    """
    if results is None:
        return
    if not isinstance(results, list):
        raise LineageError(f"{where}: results must be a list")
    for i, res in enumerate(results):
        if isinstance(res, str):
            # A backref slug: a pointer to a committed instance file.
            if not res:
                raise LineageError(f"{where}: result {i} backref slug is empty")
            if instances_dir is not None:
                inst_path = Path(instances_dir) / f"{res}.json"
                if not inst_path.exists():
                    raise LineageError(
                        f"{where}: result {i} slug {res!r} has no committed "
                        f"instance file under {Path(instances_dir).name}/")
                try:
                    inst = json.loads(inst_path.read_text())
                except (json.JSONDecodeError, OSError) as exc:
                    raise LineageError(
                        f"{where}: result {i} instance {res}.json is "
                        f"unreadable: {exc}")
                if inst.get("simulation") != record_id:
                    raise LineageError(
                        f"{where}: result {i} instance {res!r} does not "
                        f"backref this record (simulation "
                        f"{str(inst.get('simulation'))[:12]} != {record_id[:12]})")
            continue
        if not isinstance(res, dict):
            raise LineageError(
                f"{where}: result {i} must be a backref slug or an instance stub")
        for key in ("variable", "material", "conditions", "value", "units", "source"):
            if key not in res:
                raise LineageError(f"{where}: result {i} instance stub missing {key!r}")
        if res.get("source", {}).get("kind") not in ("simulation", "measurement"):
            raise LineageError(
                f"{where}: result {i} source.kind must be simulation|measurement")
        if res["variable"] not in name_to_uid:
            raise LineageError(
                f"{where}: result {i} unknown variable {res['variable']!r}")
        if res.get("simulation") != record_id:
            raise LineageError(
                f"{where}: result {i} simulation backref "
                f"{str(res.get('simulation'))[:12]} does not equal the record id "
                f"{record_id[:12]}")


def _validate(record: dict, *, name_to_uid: dict, config_dir: Path, where: str,
              instances_dir: Path | None = None) -> str:
    """Validate a whole STRICT (heavy-writer) record and return its id.

    The strict gate for :func:`record_simulation`: requires a four-key
    checksummed manifest and an execution block, recomputes the id from the
    lineage (:func:`lineage_id`; a stated id that disagrees is rejected), then runs
    the execution, lineage-node, configuration, manifest, and results gates. A
    LIGHT record (pointer artifacts, optional execution/node) is validated by
    :func:`validate_light` instead. ``instances_dir`` turns on slug resolution
    for results (the bundler passes it; the writer leaves it None, since a slug
    may name an instance that lands after the record does).
    """
    for key in ("execution", "artifacts"):
        if key not in record:
            raise LineageError(f"{where}: missing '{key}'")
    lineage = record_lineage(record, where=where)
    execution = record["execution"]
    artifacts = record["artifacts"]

    _validate_manifest(artifacts, where=where)
    _validate_execution(execution, where=where)
    record_id = canonical_id(lineage, execution, artifacts)
    stated = record.get("id")
    if stated is not None and stated != record_id:
        raise LineageError(
            f"{where}: stated id {str(stated)[:12]} does not match the id "
            f"recomputed from the claim {record_id[:12]} "
            f"(location must be out of the hash; put urls in 'mirrors')")

    _validate_lineage_node(lineage, name_to_uid, where=where)
    _validate_configuration(lineage, config_dir=config_dir, where=where)
    _validate_mirrors(record.get("mirrors"), where=where)
    _validate_results(record.get("results"), record_id, name_to_uid, where=where,
                      instances_dir=instances_dir)
    return record_id


def validate_light(record: dict, *, name_to_uid: dict | None = None,
                   where: str = "<record>", config_dir: Path | None = None,
                   instances_dir: Path | None = None) -> dict:
    """Validate a LIGHT record and return a report (honest about gaps).

    The light contract, "whatever we have":

    - ``lineage`` is an object; its id recomputes to :func:`lineage_id`, and a
      stated ``id`` that disagrees is rejected (a folded-in artifact or url).
    - When the lineage names a ``node``, it MUST resolve against the live map by
      id and, if a ``node_uid`` pin is present, by uid (the P3 instances
      node-pin discipline: a stale pin is a mismatch, never a silent pass). When
      the lineage names NO node, the record is VALID and simply node-unresolved:
      the report says so, and nothing is rejected (the honesty rule).
    - ``artifacts`` (when present) are well-formed pointers (path+role required,
      url/sha256 shape-checked); nothing about artifacts is mandatory and an
      empty or absent list is normal.
    - ``mirrors`` (when present) is a well-formed resolver layer: each entry a
      url string or an object whose ``url`` and optional free-form ``provider``
      (who holds the bytes) are strings. Outside identity; a shape check only.
    - A named ``material.configuration`` MUST resolve to a committed
      configuration record under ``config_dir`` (default: the real
      ``docs/data/configurations/``): the structure pin sits inside the lineage
      (part of ``lineage_id``), so a bogus uid is rejected on this path exactly
      as it is on the heavy writer. A lineage with no configuration key is
      unaffected (the common bare-material-name case is still valid).
    - ``execution`` and ``results`` are OPTIONAL enrichment: each is validated
      only when present.

    Returns a report ``{"id", "node_resolved": bool, "node": <id or None>,
    "artifact_count": int}``. Raises :class:`LineageError` only on a genuinely
    malformed record (bad lineage, stated-id mismatch, a stale node pin, an
    unresolved configuration pin, or a malformed pointer); a node-unresolved
    record is a normal return, not a raise.
    """
    if not isinstance(record, dict):
        raise LineageError(f"{where}: record must be an object")
    lineage = record_lineage(record, where=where)
    if not isinstance(lineage, dict):
        raise LineageError(f"{where}: lineage must be an object")

    record_id = lineage_id(lineage)
    stated = record.get("id")
    if stated is not None and stated != record_id:
        raise LineageError(
            f"{where}: stated id {str(stated)[:12]} does not match the id "
            f"recomputed from the lineage {record_id[:12]} (identity is the "
            f"lineage alone; artifacts/execution/mirrors must be out of the hash)")

    node = lineage.get("node")
    node_resolved = False
    if node is not None:
        if name_to_uid is None:
            from omai.map_data import _domains, build_graph_dict
            name_to_uid = {n["id"]: n["uid"]
                           for n in build_graph_dict(_domains())["nodes"]}
        _validate_lineage_node(lineage, name_to_uid, where=where)
        node_resolved = True
    # else: node-unresolved. Valid. The record carries its template + values.

    # The source, when the lineage carries one, is a namespaced "scheme:ref"
    # string inside the hash (see SOURCE_SCHEMES). The legacy placement, a
    # top-level record["source"] object, stays readable outside identity; the
    # two may coexist only when they agree (a conflicting pair is ambiguous
    # about what the record IS, so it is malformed).
    lin_source = lineage.get("source")
    if lin_source is not None:
        if not isinstance(lin_source, str) or not _SOURCE_RE.match(lin_source):
            raise LineageError(
                f"{where}: lineage.source must be a 'scheme:ref' string "
                f"(e.g. paper:cnt-2021-barbalinardo, doi:10.1103/...)")
        legacy_ref = (record.get("source") or {}).get("ref") \
            if isinstance(record.get("source"), dict) else None
        if legacy_ref is not None and legacy_ref != lin_source:
            raise LineageError(
                f"{where}: lineage.source ({lin_source}) conflicts with the "
                f"legacy top-level source.ref ({legacy_ref})")

    artifacts = record.get("artifacts")
    _validate_pointers(artifacts, where=where)
    _validate_mirrors(record.get("mirrors"), where=where)

    # Optional enrichment, checked only when present.
    if record.get("execution") is not None:
        _validate_execution(record["execution"], where=where)
    # The configuration pin sits inside the lineage (material.configuration is
    # part of lineage_id), so a bogus uid must be caught on the primary path
    # too, not just the heavy writer. config_dir defaults to the real
    # configurations dir; a caller may still point it elsewhere (tests). A
    # lineage with no configuration key is unaffected (_validate_configuration
    # no-ops on that): the common bare-material-name case still passes.
    _validate_configuration(lineage, config_dir=config_dir or _CONFIG_DIR, where=where)
    if record.get("results") is not None:
        if name_to_uid is None:
            from omai.map_data import _domains, build_graph_dict
            name_to_uid = {n["id"]: n["uid"]
                           for n in build_graph_dict(_domains())["nodes"]}
        _validate_results(record["results"], record_id, name_to_uid,
                          where=where, instances_dir=instances_dir)

    return {
        "id": record_id,
        "node_resolved": node_resolved,
        "node": node,
        "artifact_count": len(artifacts) if isinstance(artifacts, list) else 0,
    }


# --------------------------------------------------------------------------
# The record.
# --------------------------------------------------------------------------

def record_light(*, lineage, execution=None, artifacts=None, mirrors=None,
                 results=None, name_to_uid=None, domains=None,
                 config_dir=None):
    """Build a LIGHT, lineage-identified record: the primary builder.

    A light record stores whatever we have of an experiment. Its identity is the
    LINEAGE alone (:func:`lineage_id`); everything else rides outside the hash.

    Parameters
    ----------
    lineage : dict
        The asked computation. Recognized keys (all optional except that the
        lineage must be a non-empty object): ``node`` (a map node id, present when
        a map node is known) with an optional ``node_uid`` pin; ``material``;
        ``template`` (the code family, the honest fallback when there is no map
        node); ``conditions``; ``params``; ``hyperparameters``; setup ``values``.
        Floats in conditions/params/hyperparameters/values are rounded to
        :data:`FLOAT_DECIMALS` for identity.
    execution : dict | None
        Optional "what ran": ``code``, ``code_version``, ``seeds``,
        ``wall_time_s``, whatever is known. Outside identity. When present it
        must name a non-empty ``code`` (the execution well-formedness bar).
    artifacts : list[dict] | None
        Optional POINTER list, each ``{path, role, url?, sha256?}``. Only path
        and role are required; url/sha256 are optional and never hashed. A record
        with no artifacts is valid and normal.
    mirrors : dict | None
        Optional resolver layer (path -> {url, ...}). Outside identity. A mirror
        entry may carry an optional free-form ``provider`` string naming who
        holds the bytes (e.g. "materialscodegraph", "zenodo").
    results : list | None
        Optional results (backref slugs or instance stubs). Outside identity.
    name_to_uid : dict | None
        Precomputed node-id -> uid map. Consulted when the lineage names a node
        (to pin the live uid and validate) or when ``results`` is given (to
        validate a result stub's variable); built from the live map on demand
        when omitted and needed. A node-less lineage with no results never
        builds the map.
    domains : tuple | None
        Domains to build ``name_to_uid`` from when the lineage names a node and no
        map was supplied (default: the live map).
    config_dir : Path | None
        Configurations dir for the configuration-pin check (default: the real
        ``docs/data/configurations/``). A lineage naming no configuration is
        unaffected regardless of this value; pass an explicit dir (e.g. a test
        fixture) to point the check elsewhere.

    Returns
    -------
    dict
        The record: ``{id, lineage, execution?, artifacts?, mirrors?, results?}``.
        ``artifacts`` is included (possibly empty) so a consumer sees the (light)
        manifest explicitly; ``execution``/``mirrors``/``results`` appear only
        when given.

    Raises
    ------
    LineageError
        On a malformed lineage, a stale node pin, an unresolved configuration
        pin, a malformed pointer, or (when present) a malformed
        execution/result.
    """
    if not isinstance(lineage, dict) or not lineage:
        raise LineageError("record_light: lineage must be a non-empty object")

    where = lineage.get("node") or lineage.get("template") or "<light>"

    # Pin the live node uid when the lineage names a node but carried no pin, so a
    # later validation is a real match. A node-less lineage never resolves a map.
    # The pin is part of the lineage, so the id is computed AFTER it is attached.
    node = lineage.get("node")
    if isinstance(node, str) and lineage.get("node_uid") is None:
        if name_to_uid is None:
            from omai.map_data import _domains, build_graph_dict
            doms = domains if domains is not None else _domains()
            name_to_uid = {n["id"]: n["uid"]
                           for n in build_graph_dict(doms)["nodes"]}
        if node in name_to_uid:
            lineage = dict(lineage)
            lineage["node_uid"] = name_to_uid[node]

    record_id = lineage_id(lineage)
    record: dict = {"id": record_id, "lineage": lineage}
    if execution is not None:
        record["execution"] = execution
    # The artifact pointers as stored: {path, role, url?, sha256?, bytes?} with a
    # url mirrored from the resolver layer when one is declared for that path.
    record["artifacts"] = [_stored_pointer(a, mirrors)
                           for a in (artifacts or [])]
    if mirrors is not None:
        record["mirrors"] = mirrors
    if results is not None:
        record["results"] = results

    validate_light(record, name_to_uid=name_to_uid, where=where,
                   config_dir=config_dir)
    return record


def _stored_pointer(art: dict, mirrors) -> dict:
    """The on-disk artifact pointer: :func:`_pointer` plus a url mirrored from
    the resolver layer for that path when one is declared and the pointer did not
    already carry one. The url is a convenience for a reader; identity never sees
    it (:func:`lineage_id` hashes only the lineage).
    """
    row = _pointer(art)
    if "url" not in row and isinstance(mirrors, dict):
        loc = mirrors.get(art.get("path"))
        if isinstance(loc, dict) and "url" in loc:
            row["url"] = loc["url"]
        elif isinstance(loc, str):
            row["url"] = loc
    return row


def record_to_fragment(record: dict) -> str:
    """Encode a light record into a URL fragment payload, gzip+base64url.

    The exact scheme ``docs/play/index.html`` uses for its shared map view: JSON
    is UTF-8 encoded, gzip-compressed, base64url-encoded (standard base64 with
    ``+``/``/`` -> ``-``/``_`` and ``=`` padding stripped), and prefixed with a
    single mode char ``g`` (gzip). A browser's ``gzipToB64url`` produces the same
    string and its ``b64urlToObj`` decodes this one: a Python-minted experiment
    link opens in the playground, and a playground-minted one round-trips here.

    The result is meant to sit after ``#x=`` in a playground URL. It is compact
    for a light record; a heavy record carrying many artifact pointers may exceed
    a practical URL length, which is fine (the URL is the light case) and the
    playground already guards its own length. A legacy ``recipe``-keyed record
    is upgraded to ``lineage`` in the serialized payload.
    """
    record_lineage(record, where="record_to_fragment")
    if "recipe" in record:
        record = {
            ("lineage" if key == "recipe" else key): value
            for key, value in record.items()
        }
    blob = json.dumps(record, separators=(",", ":")).encode("utf-8")
    # mtime=0 is pinned, never left to the interpreter. A gzip header carries a
    # 4-byte MTIME field, and through Python 3.12 ``gzip.compress`` defaults it
    # to the wall clock, so one unchanged record encoded twice produced two
    # different fragments. Nothing decoded wrong (the read path never looks at
    # MTIME) but the committed ``docs/examples/index.json`` fragments could not
    # be regenerated byte-identically, so any tool that rebuilt them emitted a
    # large diff in which every changed line was a timestamp. Python 3.13 pins
    # it to 0 by default, which made the defect invisible on new interpreters
    # while it still bit on the 3.11 and 3.12 CI matrix. Pinning it here makes
    # a fragment a function of the record alone, on every supported version.
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(blob)
    packed = buf.getvalue()
    b64 = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    return "g" + b64


def record_from_fragment(fragment: str) -> dict:
    """Decode a URL fragment payload back to a record (the reciprocal get).

    Accepts the mode-prefixed base64url string :func:`record_to_fragment` (or the
    playground's ``gzipToB64url``) produces: ``g`` for gzip, ``r`` for a raw
    (uncompressed) fallback. A bare ``#x=<payload>`` may include the ``#x=``
    prefix or not; a leading ``#x=`` / ``x=`` is stripped. base64url padding is
    restored before decoding.
    """
    if not isinstance(fragment, str) or not fragment:
        raise LineageError("fragment must be a non-empty string")
    frag = fragment.strip()
    for prefix in ("#x=", "x="):
        if frag.startswith(prefix):
            frag = frag[len(prefix):]
            break
    mode, body = frag[0], frag[1:]
    pad = "=" * (-len(body) % 4)
    raw = base64.urlsafe_b64decode(body + pad)
    if mode == "g":
        raw = gzip.decompress(raw)
    elif mode == "r":
        pass
    else:
        raise LineageError(
            f"unknown fragment mode {mode!r} (expected 'g' gzip or 'r' raw)")
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------
# The share ENVELOPE: a multi-lineage bundle in one link fragment.
# --------------------------------------------------------------------------

def _normalize_member(record, *, where: str) -> dict:
    """One envelope member, normalized and shape-checked (the WIRE bar).

    A member is an ordinary light record. The check here is deliberately the
    wire-tolerant subset of :func:`validate_light`: the lineage must be a
    non-empty object (via :func:`record_lineage`, which also rejects the
    ambiguous lineage+recipe pair), a stated ``id`` must equal the id recomputed
    from the lineage (identity honesty), ``lineage.source`` when present must
    match the source grammar, and artifact pointers when present must be
    well-formed (:func:`_validate_pointers`). It does NOT resolve the node
    against the live map: a shared link minted against an older map version must
    stay decodable forever (the dual-read promise), and node resolution is a
    display/validation concern, not a wire one. A legacy ``recipe`` key is
    upgraded to ``lineage`` in the returned member, exactly as
    :func:`record_to_fragment` upgrades it on emit.
    """
    lineage = record_lineage(record, where=where)
    if not isinstance(lineage, dict) or not lineage:
        raise LineageError(f"{where}: lineage must be a non-empty object")
    stated = record.get("id")
    computed = lineage_id(lineage)
    if stated is not None and stated != computed:
        raise LineageError(
            f"{where}: stated id {str(stated)[:12]} does not match the id "
            f"recomputed from the lineage {computed[:12]}")
    src = lineage.get("source")
    if src is not None and (not isinstance(src, str) or not _SOURCE_RE.match(src)):
        raise LineageError(
            f"{where}: lineage.source must be a 'scheme:ref' string")
    _validate_pointers(record.get("artifacts"), where=where)
    if "recipe" in record:
        record = {("lineage" if key == "recipe" else key): value
                  for key, value in record.items()}
    return record


def _validate_doc(doc, *, where: str) -> None:
    """The envelope's shared document context is well-formed.

    ``doc`` is publication metadata stated once for the whole bundle. The
    recognized keys are ``source`` (a "scheme:ref" string, the lineage source
    grammar), ``title``, ``authors`` (a list), ``year``, ``journal``; anything
    else rides along tolerated (the open-context rule). Only the recognized
    keys are shape-checked, and none is required.
    """
    if not isinstance(doc, dict):
        raise LineageError(f"{where}: doc must be an object")
    src = doc.get("source")
    if src is not None and (not isinstance(src, str) or not _SOURCE_RE.match(src)):
        raise LineageError(
            f"{where}: doc.source must be a 'scheme:ref' string "
            f"(e.g. doi:10.1103/PhysRevLett.127.025902)")
    for key in ("title", "journal"):
        if doc.get(key) is not None and not isinstance(doc[key], str):
            raise LineageError(f"{where}: doc.{key}, when present, must be a string")
    if doc.get("authors") is not None and not isinstance(doc["authors"], list):
        raise LineageError(f"{where}: doc.authors, when present, must be a list")


def envelope(records, doc: dict | None = None) -> dict:
    """Build a share envelope: ``{"v": 1, "doc"?: {...}, "lineages": [...]}``.

    ``records`` is a non-empty list of ordinary light records (each member is
    normalized and shape-checked by :func:`_normalize_member`; a single-lineage
    share is a one-element envelope). ``doc`` (optional) is the shared document
    context, publication metadata for a parsed paper, validated by
    :func:`_validate_doc` and omitted from the envelope when None. Bundling
    never touches a member: each keeps its own lineage-derived id, and a member
    with no source inherits ``doc.source`` at read/display time only, never
    into its hash.
    """
    if not isinstance(records, list) or not records:
        raise LineageError("envelope: records must be a non-empty list")
    members = [_normalize_member(r, where=f"envelope member {i}")
               for i, r in enumerate(records)]
    env: dict = {"v": ENVELOPE_VERSION}
    if doc is not None:
        _validate_doc(doc, where="envelope")
        env["doc"] = doc
    env["lineages"] = members
    return env


def _validate_envelope(env, *, where: str = "<envelope>") -> dict:
    """Validate an envelope in place and return it with normalized members.

    The gate: ``v`` must be a known envelope version, ``lineages`` a non-empty
    list of valid-shaped members, ``doc`` (when present) well-formed. A dict
    carrying BOTH the envelope's ``lineages`` key and a bare record's
    ``lineage``/``recipe`` key is ambiguous about what it is, so it is
    malformed (the both-keys rule, mirroring the lineage/recipe pair).
    """
    if not isinstance(env, dict):
        raise LineageError(f"{where}: envelope must be an object")
    if "lineage" in env or "recipe" in env:
        raise LineageError(
            f"{where}: an envelope must not also carry a bare record's "
            f"'lineage' or 'recipe' key (ambiguous payload)")
    v = env.get("v")
    if v != ENVELOPE_VERSION:
        raise LineageError(
            f"{where}: unknown envelope version {v!r} "
            f"(this reader speaks v={ENVELOPE_VERSION})")
    lineages = env.get("lineages")
    if not isinstance(lineages, list) or not lineages:
        raise LineageError(f"{where}: lineages must be a non-empty list")
    doc = env.get("doc")
    if doc is not None:
        _validate_doc(doc, where=where)
    members = [_normalize_member(r, where=f"{where}: member {i}")
               for i, r in enumerate(lineages)]
    out = dict(env)
    out["lineages"] = members
    return out


def bundle_id(env: dict) -> str:
    """The derived id of a BUNDLE: sha256 of the canonical JSON of
    ``{"doc": doc-or-null, "ids": [member ids in order]}``.

    Canonical means sorted keys and compact separators, the same rule as
    :func:`lineage_id`. Each member id is the member's own lineage-derived id
    (:func:`lineage_id` over its lineage), so bundling never changes a member's
    id and the bundle id changes exactly when the shared doc or the member set
    (or its order) does.
    """
    env = _validate_envelope(env, where="bundle_id")
    ids = [lineage_id(record_lineage(m, where=f"bundle_id: member {i}"))
           for i, m in enumerate(env["lineages"])]
    payload = json.dumps({"doc": env.get("doc"), "ids": ids},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def envelope_to_fragment(env: dict) -> str:
    """Encode an envelope into a URL fragment payload, gzip+base64url.

    The exact transport :func:`record_to_fragment` uses (a ``g`` mode prefix,
    gzip, base64url, ``=`` stripped), unchanged: only the payload shape differs.
    The envelope is validated first (members normalized, any legacy ``recipe``
    key upgraded), so every emitted fragment is a well-formed v1 envelope.
    Encoders emit the envelope; the bare-record encoder stays available for the
    legacy single-record path.
    """
    env = _validate_envelope(env, where="envelope_to_fragment")
    blob = json.dumps(env, separators=(",", ":")).encode("utf-8")
    packed = gzip.compress(blob)
    b64 = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    return "g" + b64


def envelope_from_fragment(fragment: str) -> dict:
    """Decode a URL fragment payload to a NORMALIZED envelope (the dual read).

    Accepts everything :func:`record_from_fragment` accepts: every legacy
    single-record fragment ever minted decodes here, forever. A payload that is
    an envelope is validated and returned with normalized members; a payload
    that is a bare record is normalized to a one-element envelope with no doc.
    A payload carrying both shapes at once is ambiguous and rejected. The
    return is always ``{"v": 1, "doc"?: {...}, "lineages": [...]}``.
    """
    payload = record_from_fragment(fragment)
    if isinstance(payload, dict) and "lineages" in payload:
        return _validate_envelope(payload, where="envelope_from_fragment")
    member = _normalize_member(payload, where="envelope_from_fragment")
    return {"v": ENVELOPE_VERSION, "lineages": [member]}


def record_simulation(*, lineage, execution, artifacts, results=None, mirrors=None,
                      domains=None, sim_dir=None, name_to_uid=None):
    """Write a STRICT, checksummed record to ``docs/data/simulations/``.

    The heavy on-disk writer: unlike :func:`record_light`, it requires a full
    execution block and a four-key checksummed manifest (this is the "a full
    checksummed bundle DOES exist" writer). Identity is still the lineage alone
    (:func:`lineage_id`); the manifest and execution are stored but never hashed.
    For the light, URL-first path use :func:`record_light`.

    Parameters
    ----------
    lineage : dict
        The asked computation: ``node`` (map id), optional ``node_uid`` pin,
        ``material`` ({name, optional configuration uid}), ``conditions``,
        ``params``. Identity is this lineage, canonicalized by :func:`lineage_id`.
    execution : dict
        What ran: ``code`` (codes rail; required, a non-empty string),
        ``code_version``, ``container_digest`` (the per-run image digest),
        ``runner``, ``wall_time_s``, ``seeds``. Stored, outside identity.
    artifacts : list[dict]
        The strict manifest: each entry ``{path, bytes, sha256, role}`` (this
        writer requires all four; the light path takes pointers instead). Any
        ``url`` is dropped from the stored row and preserved only if declared in
        ``mirrors``. Never hashed into identity.
    results : list | None
        The run's instances as backref slugs (strings) or inline stubs (dicts
        carrying ``simulation == <this record id>``). Outside the hashed claim.
        A slug is accepted bare here (its instance may land after the record
        does); the bundler resolves it against docs/data/instances/ and
        requires the instance to backref this record.
    mirrors : dict | None
        The mutable resolver layer, keyed by artifact path -> {url, ...} (or any
        location metadata). Outside the hashed claim: moving bytes never
        re-mints the id.
    domains : tuple | None
        Domains to resolve the lineage node and result variables against
        (default: the live map).
    sim_dir : Path | None
        Override the default docs/data/simulations directory (tests).
    name_to_uid : dict | None
        Precomputed node-id -> uid map (tests); built from ``domains`` if omitted.

    Returns
    -------
    Path
        The written record's path. Idempotent on identical content (the same
        path back); a different record for the same slug gets a numeric suffix
        rather than silently overwriting (the record_instance discipline).

    Raises
    ------
    LineageError
        On any validation failure.
    """
    if name_to_uid is None:
        from omai.map_data import _domains, build_graph_dict
        doms = domains if domains is not None else _domains()
        name_to_uid = {n["id"]: n["uid"] for n in build_graph_dict(doms)["nodes"]}

    where = lineage.get("node", "<simulation>") if isinstance(lineage, dict) else "<simulation>"
    _validate_manifest(artifacts, where=where)
    record_id = canonical_id(lineage, execution, artifacts)

    record = {
        "id": record_id,
        "lineage": lineage,
        "execution": execution,
        # The manifest as stored: the hashed four keys, plus a url pulled from
        # mirrors when one is declared for that path (a convenience mirror of
        # the resolver layer; the hash never sees it).
        "artifacts": [_stored_artifact(a, mirrors) for a in artifacts],
    }
    if results is not None:
        record["results"] = results
    if mirrors is not None:
        record["mirrors"] = mirrors

    # Full validation on the assembled record (id pin, node pin, configuration,
    # results) before anything touches disk.
    _validate(record, name_to_uid=name_to_uid,
              config_dir=_CONFIG_DIR, where=where)

    sim_dir = Path(sim_dir) if sim_dir else _SIM_DIR
    sim_dir.mkdir(parents=True, exist_ok=True)
    # Filename = short id prefix + material/node slug, mirroring instances/.
    material = lineage.get("material") or {}
    label = material.get("name") if isinstance(material, dict) else None
    base = slugify(f"{record_id[:8]}-{label or ''}-{lineage.get('node', '')}") or record_id[:12]
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    path = sim_dir / f"{base}.json"
    n = 1
    while path.exists() and path.read_text() != payload:
        n += 1
        path = sim_dir / f"{base}-{n}.json"
    path.write_text(payload)
    return path


def _stored_artifact(art: dict, mirrors) -> dict:
    """The STRICT writer's on-disk artifact row: the four manifest keys
    ``{path, bytes, sha256, role}``, plus a url mirrored from the resolver layer
    for that path when one is declared. The url is a convenience for a consumer
    reading the record; it is NOT part of identity (identity is the lineage) and
    its authority is the ``mirrors`` field. (The light and bundle paths store
    pointers via :func:`_stored_pointer` instead.)
    """
    row = _manifest_entry(art)
    if isinstance(mirrors, dict):
        loc = mirrors.get(art.get("path"))
        if isinstance(loc, dict) and "url" in loc:
            row["url"] = loc["url"]
        elif isinstance(loc, str):
            row["url"] = loc
    return row


def committed_ids(sim_dir: Path | None = None) -> set[str]:
    """The record ids of every committed simulation under docs/data/simulations/.

    The membership set for the instance-side backref gate: an instance whose
    ``simulation`` field names an id absent from this set cites a run the
    commons does not hold, and ``build_instances`` refuses it. An unreadable
    file contributes nothing (the bundler's own pass reports it).
    """
    sim_dir = Path(sim_dir) if sim_dir else _SIM_DIR
    ids: set[str] = set()
    if not sim_dir.exists():
        return ids
    for path in sorted(sim_dir.glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        rid = rec.get("id")
        if isinstance(rid, str):
            ids.add(rid)
    return ids


# --------------------------------------------------------------------------
# Import (the reciprocal of a host's export): a hosted bundle -> a record.
# --------------------------------------------------------------------------

# The map-lineage keys a bundle spec may carry. A spec that names them lifts
# them into the lineage (the asked computation); a spec that does not is kept
# verbatim under lineage.spec and its missing node is left for validation to
# report, never invented here.
_LINEAGE_SPEC_KEYS = ("node", "node_uid", "material", "conditions", "params")


def _wall_time_s(started, finished):
    """Seconds between two ISO-8601 timestamps, or None when either is absent
    or unparseable. Derived, never guessed: a bundle whose timestamps do not
    parse simply carries no wall time rather than a fabricated one.
    """
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    from datetime import datetime
    try:
        a = datetime.fromisoformat(started)
        b = datetime.fromisoformat(finished)
    except ValueError:
        return None
    return (b - a).total_seconds()


def _lineage_from_bundle(manifest: dict) -> dict:
    """The asked computation, lifted from the bundle's template + spec.

    The bundle's ``template`` is the code family that ran; it is kept on the
    lineage as ``template`` and (below) mirrored into ``execution.code``. The
    ``spec`` is the input the host was given. If the spec names any map-lineage
    key (``node``, ``material``, ``conditions``, ``params``, or a ``node_uid``
    pin), those are lifted onto the lineage so validation can resolve the node
    against the live map; the spec itself is always kept under ``lineage.spec``
    so nothing the host asked is dropped. A spec that names NO node leaves
    ``lineage.node`` absent: the writer never invents a node id, and
    :func:`_validate` reports the gap (the honesty rule, a bundle without a
    resolvable map node is recorded but flagged).
    """
    template = manifest.get("template")
    spec = manifest.get("spec")
    lineage: dict = {"template": template, "spec": spec}
    if isinstance(spec, dict):
        for key in _LINEAGE_SPEC_KEYS:
            if key in spec:
                lineage[key] = spec[key]
    return lineage


def _execution_from_bundle(manifest: dict) -> dict:
    """The "what ran" claim, mapped from the bundle's run metadata.

    ``code`` is the bundle's explicit ``code`` field when present, else its
    ``template`` (the code family a host run is filed under); a run necessarily
    ran some code, and :func:`_validate_execution` refuses a block that names
    none. The run's timing (``started_at``, ``finished_at``, ``current_stage``)
    is mapped straight onto the execution block; ``wall_time_s`` is derived from
    finished-minus-started only when both are parseable ISO timestamps, else
    omitted (never guessed). The platform ``kind`` rides along as provenance.
    """
    code = manifest.get("code") or manifest.get("template")
    execution: dict = {"code": code}
    for src, dst in (("started_at", "started_at"),
                     ("finished_at", "finished_at"),
                     ("current_stage", "current_stage"),
                     ("kind", "kind"),
                     ("code_version", "code_version"),
                     ("container_digest", "container_digest"),
                     ("runner", "runner")):
        if manifest.get(src) is not None:
            execution[dst] = manifest[src]
    wall = _wall_time_s(manifest.get("started_at"), manifest.get("finished_at"))
    if wall is not None:
        execution["wall_time_s"] = wall
    return execution


def _bundle_artifacts(manifest: dict, *, where: str) -> list[dict]:
    """The bundle's artifacts, each reduced to a POINTER (never refused).

    Under the light, URL-first model a bundle artifact is optional and
    pointer-only, so this NO LONGER raises on a missing checksum. An artifact
    carrying a ``sha256`` is kept as a verifiable pointer (the byte claim rides
    along for :func:`verify_bundle_bytes`); one lacking a sha256 becomes a plain
    pointer ``{path, role?, url?, bytes?}``, outside identity exactly like the
    light path. A missing ``role`` defaults to ``"artifact"`` (the honest generic
    role) rather than being rejected: a bundle enriches a record with bytes, it
    is never required to complete it. Identity is the lineage regardless
    (:func:`lineage_id`), so no artifact, checksummed or not, changes the id.

    A pointer still needs a ``path`` (there is nothing to point at otherwise); an
    entry with no path is dropped, since it can neither be located nor verified.
    """
    artifacts = manifest.get("artifacts")
    if artifacts is None:
        return []
    if not isinstance(artifacts, list):
        raise LineageError(f"{where}: bundle 'artifacts' must be a list")
    out: list[dict] = []
    for i, art in enumerate(artifacts):
        if not isinstance(art, dict):
            raise LineageError(f"{where}: bundle artifact {i} must be an object")
        if not art.get("path"):
            # No path: not a pointer to anything. Drop it rather than mint a
            # locationless, unverifiable row.
            continue
        pointer = {"path": art.get("path"),
                   "role": art.get("role") or "artifact"}
        if art.get("url") is not None:
            pointer["url"] = art["url"]
        if art.get("sha256") is not None:
            pointer["sha256"] = art["sha256"]
        if art.get("bytes") is not None:
            pointer["bytes"] = art["bytes"]
        out.append(pointer)
    return out


def record_from_bundle(manifest: dict, *, mirrors: dict | None = None,
                       name_to_uid=None) -> dict:
    """Build a record from a hosted bundle manifest (the OPTIONAL heavy path).

    The reciprocal of a host's export: takes an MCG-style bundle manifest (keys
    ``name``, ``kind``, ``template``, ``spec``, ``outputs``, ``artifacts``,
    ``started_at``, ``finished_at``, ``current_stage``) and returns a record
    whose identity is the lineage (:func:`lineage_id`), exactly like a light
    record. A checksummed bundle ENRICHES the record with verifiable byte
    pointers, but it is never required: this path no longer refuses a bundle
    whose artifacts lack a sha256.

    The mapping (honest, never inventing what the bundle did not carry):

    - ``lineage`` comes from ``template`` + ``spec`` (:func:`_lineage_from_bundle`).
      If the spec names a map ``node`` (and optionally ``material``,
      ``conditions``, ``params``, a ``node_uid`` pin), those are lifted so the
      lineage resolves against the live map; the spec is always kept verbatim
      under ``lineage.spec``. A spec that names no node yields a record with
      ``lineage.node`` ABSENT, which validation reports (node-unresolved) rather
      than the writer guessing a node id.
    - ``execution`` comes from ``template`` (or an explicit ``code``) plus the
      run's ``started_at`` / ``finished_at`` / ``current_stage``;
      ``wall_time_s`` is derived only when both timestamps parse as ISO, else
      omitted (:func:`_execution_from_bundle`). Outside identity.
    - ``artifacts`` are the bundle's entries as POINTERS
      (:func:`_bundle_artifacts`): one carrying a ``sha256`` stays a verifiable
      pointer, one lacking a sha256 becomes a plain pointer, and NONE enters the
      hash. A missing role defaults to ``"artifact"``. No artifact is required.
    - ``mirrors`` (optional) is the resolver layer (where the bytes are hosted),
      outside identity; a mirrored url is copied onto the matching pointer as a
      reader convenience.
    - ``outputs`` (the bundle's headline result JSON) rides along as provenance
      OUTSIDE identity: it is what the run produced, not the lineage it was asked,
      and it is raw stage JSON, not a validated instance stub, so it is not filed
      under ``results``. ``name`` and ``kind`` likewise ride as provenance.

    Parameters
    ----------
    manifest : dict
        The host bundle manifest (the MCG export shape).
    mirrors : dict | None
        The optional resolver layer, keyed by artifact path -> {url, ...}.
        Outside identity: adding or moving a url never re-mints the id.
    name_to_uid : dict | None
        Precomputed node-id -> uid map (tests); built from the live map if
        omitted. Only consulted to attach a live ``node_uid`` pin when the spec
        named a node but no pin; NEVER used to invent a node the spec omitted.

    Returns
    -------
    dict
        The record dict (``id``, ``lineage``, ``execution``, ``artifacts`` as
        pointers, plus ``mirrors`` / ``outputs`` / ``name`` / ``kind`` when
        present).

    Raises
    ------
    LineageError
        Only on a structurally malformed bundle (a non-dict manifest, or a
        non-list / non-object ``artifacts``). A missing checksum is NOT an error.
    """
    if not isinstance(manifest, dict):
        raise LineageError("bundle manifest must be an object")
    where = manifest.get("name") or manifest.get("template") or "<bundle>"

    lineage = _lineage_from_bundle(manifest)
    execution = _execution_from_bundle(manifest)
    artifacts = _bundle_artifacts(manifest, where=where)

    # Attach the live uid pin ONLY when the spec named a node but carried no
    # pin: pin what the map currently says, so a later validation is a real
    # match, never a silent pass. A spec that named no node stays unpinned and
    # unresolved on purpose (validation reports it).
    node = lineage.get("node")
    if isinstance(node, str) and lineage.get("node_uid") is None:
        if name_to_uid is None:
            from omai.map_data import _domains, build_graph_dict
            name_to_uid = {n["id"]: n["uid"]
                           for n in build_graph_dict(_domains())["nodes"]}
        if node in name_to_uid:
            lineage["node_uid"] = name_to_uid[node]

    record_id = lineage_id(lineage)
    record: dict = {
        "id": record_id,
        "lineage": lineage,
        "execution": execution,
        # The stored pointers: {path, role, url?, sha256?, bytes?}, plus a url
        # mirrored from the resolver layer when one is declared (never hashed).
        "artifacts": [_stored_pointer(a, mirrors) for a in artifacts],
    }
    if mirrors is not None:
        record["mirrors"] = mirrors
    # Provenance carried outside the hashed claim (like mirrors): what the run
    # produced and how the host labeled it, never part of the lineage identity.
    if manifest.get("outputs") is not None:
        record["outputs"] = manifest["outputs"]
    if manifest.get("name") is not None:
        record["name"] = manifest["name"]
    if manifest.get("kind") is not None:
        record["kind"] = manifest["kind"]
    return record


# --------------------------------------------------------------------------
# Verification (a dated report, NEVER a gate).
# --------------------------------------------------------------------------

def verify_simulation(record: dict, *, fetcher=None, today=None) -> dict:
    """A dated reachability/checksum report for a record's mirrors.

    For every artifact that has a resolvable url (in ``mirrors`` or mirrored on
    the artifact row), fetch it and compare the sha256 of the bytes to the
    manifest. Returns a report, never raises on a miss and never gates: a record
    whose bytes moved is STALE, not wrong.

    ``fetcher`` is a callable ``url -> bytes`` (injected; the tests and any
    offline caller pass a fake). When omitted, no network is attempted and every
    url-bearing artifact is reported ``unreachable`` with reason
    ``no fetcher``: the report still dates and enumerates, it just cannot check.

    Report shape::

        {"id": <record id>, "date": "YYYY-MM-DD", "checked": [
            {"path", "role", "url", "provider"?, "status", ...}, ...]}

    where ``status`` is ``ok`` (fetched, sha256 matches), ``mismatch`` (fetched,
    sha256 differs: reports expected/actual), ``unreachable`` (fetch failed or
    no url), or ``no-url`` (the artifact declares no location to check). When a
    mirror entry names a ``provider`` (who holds the bytes), it is echoed onto
    that artifact's report entry, closing the provenance loop; the key is
    outside identity and the value is free-form.
    """
    stamp = (today or date.today()).isoformat()
    artifacts = record.get("artifacts", []) or []
    mirrors = record.get("mirrors", {}) or {}
    checked: list[dict] = []
    for art in artifacts:
        path = art.get("path")
        role = art.get("role")
        loc = mirrors.get(path) if isinstance(mirrors, dict) else None
        url = None
        provider = None
        if isinstance(loc, dict):
            url = loc.get("url")
            provider = loc.get("provider")
        elif isinstance(loc, str):
            url = loc
        if url is None:
            url = art.get("url")

        entry = {"path": path, "role": role, "url": url}
        # Echo the mirror's provider (who holds the bytes) when it names one:
        # provenance carried outside identity, useful regardless of status.
        if provider is not None:
            entry["provider"] = provider
        if url is None:
            entry["status"] = "no-url"
            checked.append(entry)
            continue
        if fetcher is None:
            entry["status"] = "unreachable"
            entry["reason"] = "no fetcher"
            checked.append(entry)
            continue
        try:
            blob = fetcher(url)
        except Exception as exc:  # noqa: BLE001 - any fetch failure is unreachable, not raised
            entry["status"] = "unreachable"
            entry["reason"] = str(exc)
            checked.append(entry)
            continue
        actual = hashlib.sha256(blob).hexdigest()
        if actual == art.get("sha256"):
            entry["status"] = "ok"
        else:
            entry["status"] = "mismatch"
            entry["expected"] = art.get("sha256")
            entry["actual"] = actual
        checked.append(entry)
    return {"id": record.get("id"), "date": stamp, "checked": checked}


def verify_bundle_bytes(record: dict, artifact_dir=None, fetcher=None, *,
                        today=None) -> dict:
    """A dated byte-vs-checksum report for an imported record's artifacts.

    The import-side companion of :func:`verify_simulation`: where that reports
    the reachability of a record's mirrors, this checks each artifact's actual
    bytes against its manifest sha256 when the bytes are reachable. Same
    contract, verbatim: a REPORT, never a gate, and it NEVER raises on a
    mismatch or a miss. A record whose local bytes were tampered or whose
    hosted bytes moved is surfaced (``mismatch`` / ``unchecked``), never thrown.

    A pointer with NO sha256 (a light or unsummed-bundle pointer) is reported
    ``unchecked`` with reason ``no sha256 to verify against``: there is no byte
    claim to check, and that is not a failure. For a pointer that DOES carry a
    sha256, in order:

    - if ``artifact_dir`` is given and ``artifact_dir/path`` exists, the local
      file is hashed and compared: ``ok`` on match, ``mismatch`` on differ;
    - else if a ``fetcher`` (``url -> bytes``) is given and the record has a
      mirror url (or an on-row url) for that path, the bytes are fetched and
      compared (a fetch failure is ``unchecked``, reason the error);
    - else the artifact is ``unchecked`` with reason ``bytes not reachable``.

    ``artifact_dir`` wins over ``fetcher`` when both can reach a path: a local
    copy is the cheaper, authoritative check. Paths are joined POSIX-style under
    ``artifact_dir`` (the bundle stores bundle-relative paths, e.g.
    ``results/summary.json``), never escaping the directory.

    Report shape::

        {"id": <record id>, "date": "YYYY-MM-DD",
         "checked": [{"path", "role", "status", ...}, ...],
         "summary": {"ok": n, "mismatch": n, "unchecked": n}}

    where ``status`` is ``ok`` (bytes hash to the pointer's sha256),
    ``mismatch`` (bytes differ: reports expected/actual), or ``unchecked``
    (no sha256 to check, bytes not reachable, or a fetch failed: reports a
    reason).
    """
    stamp = (today or date.today()).isoformat()
    artifacts = record.get("artifacts", []) or []
    mirrors = record.get("mirrors", {}) or {}
    base = Path(artifact_dir) if artifact_dir is not None else None
    checked: list[dict] = []
    summary = {"ok": 0, "mismatch": 0, "unchecked": 0}
    for art in artifacts:
        path = art.get("path")
        role = art.get("role")
        expected = art.get("sha256")
        entry = {"path": path, "role": role}

        # A pointer with no checksum cannot be byte-verified: unchecked, not a
        # miss and not a failure. (The light model: bytes are optional.)
        if expected is None:
            entry["status"] = "unchecked"
            entry["reason"] = "no sha256 to verify against"
            summary["unchecked"] += 1
            checked.append(entry)
            continue

        blob = None
        source = None
        # 1) Local file under artifact_dir wins: cheapest, authoritative.
        if base is not None and isinstance(path, str):
            local = base / path
            if local.is_file():
                try:
                    blob = local.read_bytes()
                    source = "local"
                except OSError as exc:
                    entry["status"] = "unchecked"
                    entry["reason"] = f"local read failed: {exc}"
                    summary["unchecked"] += 1
                    checked.append(entry)
                    continue
        # 2) Else fetch the mirror url, if a fetcher and a url are both present.
        if blob is None and fetcher is not None:
            loc = mirrors.get(path) if isinstance(mirrors, dict) else None
            url = None
            if isinstance(loc, dict):
                url = loc.get("url")
            elif isinstance(loc, str):
                url = loc
            if url is None:
                url = art.get("url")
            if url is not None:
                try:
                    blob = fetcher(url)
                    source = url
                except Exception as exc:  # noqa: BLE001 - a fetch miss is unchecked, not raised
                    entry["status"] = "unchecked"
                    entry["reason"] = f"fetch failed: {exc}"
                    summary["unchecked"] += 1
                    checked.append(entry)
                    continue

        if blob is None:
            entry["status"] = "unchecked"
            entry["reason"] = "bytes not reachable"
            summary["unchecked"] += 1
            checked.append(entry)
            continue

        entry["source"] = source
        actual = hashlib.sha256(blob).hexdigest()
        if actual == expected:
            entry["status"] = "ok"
            summary["ok"] += 1
        else:
            entry["status"] = "mismatch"
            entry["expected"] = expected
            entry["actual"] = actual
            summary["mismatch"] += 1
        checked.append(entry)
    return {"id": record.get("id"), "date": stamp, "checked": checked,
            "summary": summary}
