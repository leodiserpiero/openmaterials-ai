"""Responsive containment contracts for the playground data view."""

import re
from pathlib import Path


PLAY = Path(__file__).resolve().parents[1] / "docs" / "play" / "index.html"


def _css() -> str:
    html = PLAY.read_text()
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    assert style, "playground inline styles are missing"
    return re.sub(r"\s+", "", style.group(1))


def test_data_view_header_reserves_readable_metadata_width():
    css = _css()
    assert ".rec-head{display:grid;grid-template-columns:minmax(0,1fr)minmax(24rem,34rem)" in css
    assert ".rec-head>.rec-lede{white-space:normal;}" in css
    assert ".rec-head-meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert ".rec-head-meta>div{display:grid;grid-template-columns:max-contentminmax(0,1fr)" in css
    assert ".rec-head-meta" in css and "overflow-wrap:anywhere" in css


def test_mobile_navigation_and_actions_are_locally_contained():
    css = _css()
    assert ".pg-top{" in css and "overflow:hidden" in css
    assert ".pg-nav{" in css and "overflow-x:auto" in css
    assert ".lin-stage{" in css and "max-width:100vw" in css and "overflow-x:hidden" in css
    assert ".lin-bar{display:grid;grid-template-columns:minmax(0,1fr)" in css
    assert ".lin-actionsa{min-width:0;overflow-wrap:anywhere;}" in css
