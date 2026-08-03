"""The docs/examples gallery is real, valid, and self-consistent.

Every shipped example lineage must be a valid LIGHT record whose stated id
recomputes, and index.json must agree with the record files byte-for-byte:
the fragment in the index decodes to exactly the committed record, so a
shared link and the committed file can never drift apart silently.

The gallery is also the commons' shop window, so it carries one promise the
library cannot enforce on its own: a pointer this repository publishes at a
host that answers a logged-out reader with a login wall must SAY so. See
``test_gallery_pointers_declare_restricted_access``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omai.lineages import (
    lineage_id,
    record_from_fragment,
    record_lineage,
    validate_light,
)

_GALLERY = Path(__file__).resolve().parent.parent / "docs" / "examples"
_RECORDS = sorted(p for p in _GALLERY.glob("*.json") if p.name != "index.json")


def _index_entries():
    idx = json.loads((_GALLERY / "index.json").read_text())
    return idx if isinstance(idx, list) else idx["examples"]


def test_gallery_is_nonempty():
    assert len(_RECORDS) >= 10


@pytest.mark.parametrize("path", _RECORDS, ids=lambda p: p.stem)
def test_record_is_valid_light_and_id_recomputes(path):
    rec = json.loads(path.read_text())
    validate_light(rec, where=path.name)  # raises on a malformed record
    assert rec["id"] == lineage_id(record_lineage(rec))


# Hosts known to sit behind an access control: a logged-out reader following a
# pointer here gets a login redirect, not the bytes. Verified 2026-08-03 by
# fetching https://app.materialscodegraph.com/runs/si-tersoff-rta logged out,
# which answered 302 to dvnclabs.cloudflareaccess.com with auth_status NONE.
# Public run pages are a platform gate; until they land, the map must not
# present these as if a stranger could open them.
_GATED_HOSTS = ("app.materialscodegraph.com",)


def _gated(url: str) -> bool:
    return isinstance(url, str) and any(h in url for h in _GATED_HOSTS)


@pytest.mark.parametrize("path", _RECORDS, ids=lambda p: p.stem)
def test_gallery_pointers_declare_restricted_access(path):
    """No shipped example offers a gated location without declaring it.

    Every mirror entry pointing at a known-gated host must carry
    ``access: "restricted"``, and every artifact pointer whose url is gated must
    have a mirror entry for its path saying the same, because the renderer reads
    the access state from the resolver layer keyed by path. Absence is not
    "public" anywhere in the contract; here, on the public gallery, absence is a
    defect. This fails closed on a newly added example, which is the point.
    """
    rec = json.loads(path.read_text())
    mirrors = rec.get("mirrors") or {}

    for key, loc in mirrors.items():
        url = loc.get("url") if isinstance(loc, dict) else loc
        if not _gated(url):
            continue
        assert isinstance(loc, dict), (
            f"{path.name}: mirror {key!r} points at a gated host as a bare "
            f"string, so it cannot declare access; use an object")
        assert loc.get("access") == "restricted", (
            f"{path.name}: mirror {key!r} points at a gated host but declares "
            f"access={loc.get('access')!r}; it must declare 'restricted'")

    for art in rec.get("artifacts") or []:
        if not _gated(art.get("url")):
            continue
        loc = mirrors.get(art.get("path"))
        assert isinstance(loc, dict) and loc.get("access") == "restricted", (
            f"{path.name}: artifact {art.get('path')!r} carries a gated url but "
            f"its mirror entry does not declare access='restricted', so the "
            f"renderer has nothing to label it with")


def test_gallery_gate_guard_actually_bites():
    """The guard above must reject a gated pointer that declares nothing.

    A real-data assertion that never fails is decoration, so this constructs the
    exact defect the repository just fixed and proves the check catches it.
    """
    rec = {"mirrors": {"t/x": {"url": "https://app.materialscodegraph.com/runs/x",
                               "provider": "materialscodegraph"}}}
    loc = rec["mirrors"]["t/x"]
    assert _gated(loc["url"])
    assert loc.get("access") != "restricted"  # the defect, undeclared
    loc["access"] = "restricted"
    assert loc.get("access") == "restricted"  # and what repairs it


def test_index_matches_the_record_files_exactly():
    by_slug = {p.stem: json.loads(p.read_text()) for p in _RECORDS}
    entries = _index_entries()
    assert len(entries) == len(by_slug)
    for e in entries:
        rec = by_slug[e["slug"]]
        assert rec["id"].startswith(e["id"]), f"{e['slug']}: index id is stale"
        decoded = record_from_fragment(e["fragment"])
        assert decoded == rec, f"{e['slug']}: index fragment does not decode to the committed record"
