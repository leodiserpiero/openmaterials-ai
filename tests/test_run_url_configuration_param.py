"""The "Run on MaterialsCodeGraph" link carries the configuration pin.

MCG (app.materialscodegraph.com) accepts a ``configuration=<uid>`` query param on
its ``#/new/<node>`` deep link: it resolves the uid against this repo's public
configurations bundle, shows the pinned cell on the define form, and the launched
experiment computes with that exact cell. So when a lineage record pins an atomic
configuration, both Run-URL builders must append ``configuration=<uid>``; when it
does not, the URL must be byte-identical to before (no stray param). The uid is
kept verbatim (a ``sha256:`` prefix is passed through; MCG strips it server-side),
and an unresolvable uid degrades gracefully MCG-side (it simply falls back).

This pins the builders on BOTH pages by extracting ``mcgRunUrl`` and its helper
dependencies from the shipped HTML and running them under Node against crafted
records: a pin produces the param, no pin produces no param, and a
``sha256:``-prefixed pin passes through verbatim. Skipped cleanly when Node is
unavailable; a static wiring check guards the feature regardless.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PLAY = _REPO / "docs" / "play" / "index.html"
_EXPERIMENT = _REPO / "docs" / "experiment" / "index.html"


def _grab_function(html: str, name: str) -> str:
    """Extract a top-level `function name(...) { ... }` body by brace matching."""
    m = re.search(r"function %s\s*\([^)]*\)\s*\{" % re.escape(name), html)
    assert m, f"could not find function {name}"
    i, depth = m.end(), 1
    while i < len(html) and depth:
        c = html[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
    return html[m.start():i]


# The MCG base, declared once per page as `var MCG_BASE = '...';`. Grabbed so the
# extracted builder resolves it standalone under Node.
def _grab_mcg_base(html: str) -> str:
    m = re.search(r"var MCG_BASE\s*=\s*'[^']*'\s*;", html)
    assert m, "could not find the MCG_BASE declaration"
    return m.group(0)


# Each page's mcgRunUrl plus the top-level helpers it calls, in dependency order.
_PLAY_HELPERS = ("baseNode", "materialName", "materialConfigUid", "mcgRunUrl")
_EXPERIMENT_HELPERS = ("baseNode", "condText", "materialConfigUid", "mcgRunUrl")


def _run_builder(page: Path, helpers, call: str) -> str:
    """Evaluate `call` (a mcgRunUrl(...) expression) under Node against the page's
    real builder + helpers, returning the produced URL string."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; builder behavior checked in CI where present")
    html = page.read_text()
    src = _grab_mcg_base(html) + "\n" + "\n".join(_grab_function(html, n) for n in helpers)
    script = src + "\nconsole.log(" + call + ");\n"
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout.strip().splitlines()[-1]


# --------------------------------------------------------------------------
# Static wiring: both builders append the param. Guards the feature even where
# Node is unavailable, and documents the exact query key MCG reads.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", [_PLAY, _EXPERIMENT], ids=["play", "experiment"])
def test_run_url_builder_appends_the_configuration_param(page):
    body = _grab_function(page.read_text(), "mcgRunUrl")
    assert "materialConfigUid(" in body, "the builder does not read the configuration pin"
    assert "'configuration=' + encodeURIComponent(" in body, (
        "the builder does not append configuration=<uid>"
    )


# --------------------------------------------------------------------------
# Behavior, page by page, under Node against the shipped builder.
# --------------------------------------------------------------------------

# A lineage whose material carries a pin. mcgRunUrl reads lineage.material, so both
# pages take the same shape here; the property key differs (node vs variable) but
# neither affects the configuration param under test.
_UID = "a" * 64


def test_play_pin_produces_configuration_param():
    url = _run_builder(
        _PLAY, _PLAY_HELPERS,
        "mcgRunUrl({node:'ThermalConductivity', material:{name:'Si', configuration:'%s'}})" % _UID,
    )
    assert ("configuration=" + _UID) in url, url


def test_play_no_pin_has_no_configuration_param():
    # byte-identical to today: a bare-name material yields no configuration key
    url = _run_builder(
        _PLAY, _PLAY_HELPERS,
        "mcgRunUrl({node:'ThermalConductivity', material:'Si', conditions:{T:300}})",
    )
    assert "configuration=" not in url, url
    assert url == "https://app.materialscodegraph.com/#/new/ThermalConductivity?material=Si&conditions=T%20%3D%20300", url


def test_play_preserves_the_qualified_composite_node():
    url = _run_builder(
        _PLAY, _PLAY_HELPERS,
        "mcgRunUrl({node:'ThermalConductivity[effective_medium=nan,orientation=random]', material:'dgeba_gnp_5pct'})",
    )
    assert url == (
        "https://app.materialscodegraph.com/#/new/"
        "ThermalConductivity%5Beffective_medium%3Dnan%2Corientation%3Drandom%5D"
        "?material=dgeba_gnp_5pct"
    ), url


def test_play_sha256_pin_passes_through_verbatim():
    url = _run_builder(
        _PLAY, _PLAY_HELPERS,
        "mcgRunUrl({node:'ThermalConductivity', material:{configuration:'sha256:%s'}})" % _UID,
    )
    # verbatim: the sha256: prefix survives (URL-encoded, ':' -> %3A); MCG strips it
    assert ("configuration=sha256%3A" + _UID) in url, url


def test_experiment_pin_produces_configuration_param():
    url = _run_builder(
        _EXPERIMENT, _EXPERIMENT_HELPERS,
        "mcgRunUrl({variable:'ThermalConductivity', material:{name:'Si', configuration:'%s'}})" % _UID,
    )
    assert ("configuration=" + _UID) in url, url


def test_experiment_no_pin_has_no_configuration_param():
    url = _run_builder(
        _EXPERIMENT, _EXPERIMENT_HELPERS,
        "mcgRunUrl({variable:'ThermalConductivity', material:'Si'})",
    )
    assert "configuration=" not in url, url
    assert url == "https://app.materialscodegraph.com/#/new/ThermalConductivity?material=Si", url


def test_experiment_preserves_the_qualified_composite_node():
    url = _run_builder(
        _EXPERIMENT, _EXPERIMENT_HELPERS,
        "mcgRunUrl({variable:'ThermalConductivity[effective_medium=nan,orientation=random]', material:'dgeba_gnp_5pct'})",
    )
    assert url == (
        "https://app.materialscodegraph.com/#/new/"
        "ThermalConductivity%5Beffective_medium%3Dnan%2Corientation%3Drandom%5D"
        "?material=dgeba_gnp_5pct"
    ), url


def test_experiment_sha256_pin_passes_through_verbatim():
    url = _run_builder(
        _EXPERIMENT, _EXPERIMENT_HELPERS,
        "mcgRunUrl({variable:'ThermalConductivity', material:{configuration:'sha256:%s'}})" % _UID,
    )
    assert ("configuration=sha256%3A" + _UID) in url, url
