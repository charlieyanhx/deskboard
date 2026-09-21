"""The plugin surface: extra tabs go in front, are refreshed on load / on the header ↻, and one
page's failure does not stop the others."""
import panel as pn

from deskboard.cli import DEMO
from deskboard.ui.app import build, build_desk


def _page(name, log):
    md = pn.pane.Markdown("")
    n = {"k": 0}

    def refresh():
        n["k"] += 1
        md.object = f"{name} refreshed {n['k']}"
        log.append(name)
    return name, pn.Column(md), refresh


def test_extra_tabs_front_and_refresh_on_header_button():
    log = []
    good = _page("Custom", log)

    def boom():
        raise RuntimeError("page broke")
    tmpl, desk, feed = build(DEMO, 1.0, shared=build_desk(DEMO, 1.0), extra_tabs=[good, ("Broken", pn.Column(), boom)])
    tabs = tmpl.main[0]
    assert list(tabs._names)[:2] == ["Custom", "Broken"] and list(tabs._names)[2] == "Risk"
    btn = tmpl.header[0][0]
    assert btn.name == "↻ refresh"
    assert log == ["Custom"]                          # refreshed once at load; the broken page was logged, not raised
    btn.clicks += 1                                   # header refresh: repainted again
    assert log == ["Custom", "Custom"] and "Custom refreshed 2" in good[1][0].object
    assert "refreshed" in tmpl.header[0][1].object


def test_no_extra_tabs_keeps_the_built_in_order():
    tmpl, *_ = build(DEMO, 1.0, shared=build_desk(DEMO, 1.0))
    assert list(tmpl.main[0]._names) == ["Risk", "P&L", "Scenarios", "Execution", "Health", "Alerts", "Legs", "Live", "Feed"]
