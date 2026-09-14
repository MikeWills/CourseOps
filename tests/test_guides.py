"""The volunteer guides served from inside the app.

Two kinds of test. The converter is checked construct by construct, because
it is ours and a Markdown subset is exactly the kind of thing that quietly
drops a line. Then every SHIPPED page is rendered and every link and image in
it resolved, so a renamed screenshot or a page split in two fails here rather
than as a broken picture on a phone on race morning.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from courseops import guides
from courseops.config import Settings
from courseops.web import create_app

GUIDES = Path(guides.__file__).with_name("guides")


# --- the converter ------------------------------------------------------------

def test_headings_paragraphs_and_emphasis():
    html = guides.render("# Title\n\nOne **bold** and *soft* line\nthat continues.\n\n## Next\n")
    assert '<h1 id="title">Title</h1>' in html
    assert "<p>One <strong>bold</strong> and <em>soft</em> line that continues.</p>" in html
    assert '<h2 id="next">Next</h2>' in html


def test_text_is_escaped_but_code_spans_are_literal():
    html = guides.render("Use `<b>` & `a*b*c`, not <i>tags</i>")
    assert "<code>&lt;b&gt;</code>" in html
    assert "<code>a*b*c</code>" in html
    assert "&amp; " in html
    assert "&lt;i&gt;tags&lt;/i&gt;" in html


def test_lists_keep_their_kind_and_continuation_lines():
    text = "1. Press **Go**.\n2. Tap where the\n   runner is.\n\n- one\n- two\n"
    html = guides.render(text)
    assert "<ol><li>Press <strong>Go</strong>.</li><li>Tap where the runner is.</li></ol>" in html
    assert "<ul><li>one</li><li>two</li></ul>" in html


def test_tables_and_rules():
    html = guides.render("| A | B |\n|---|---|\n| **x** | [y](sag.md) |\n\n---\n")
    assert "<table><thead><tr><th>A</th><th>B</th></tr></thead>" in html
    assert '<tbody><tr><td><strong>x</strong></td><td><a href="/help/sag">y</a></td></tr></tbody>' in html
    assert "<hr>" in html


def test_blockquote_body_is_markdown_again():
    html = guides.render("> ## The radio comes first\n>\n> **Supplemental.** Call it in.\n")
    assert html.startswith("<blockquote><h2")
    assert "<p><strong>Supplemental.</strong> Call it in.</p></blockquote>" in html


def test_an_image_on_its_own_line_is_a_captioned_figure():
    html = guides.render("![The top bar](images/shared-topbar.png)")
    assert html == ('<figure><img src="/help/images/shared-topbar.png" alt="The top bar" '
                    'loading="lazy"><figcaption>The top bar</figcaption></figure>')


def test_links_are_rewritten_for_where_the_pages_are_served():
    """`x.md` becomes `/help/x`; the index becomes `/help/`; `../` reaches the
    repository because docs/ is not shipped; anything leaving the app opens
    in a new tab."""
    html = guides.render("[a](net-control.md#status) [b](README.md) [c](../../../docs/RUNBOOK.md) [d](https://x.example/?a=1&b=2)")
    assert '<a href="/help/net-control#status">a</a>' in html
    assert '<a href="/help/">b</a>' in html
    assert ('<a href="https://github.com/MikeWills/CourseOps/blob/main/docs/RUNBOOK.md"'
            ' target="_blank" rel="noopener noreferrer">c</a>') in html
    assert '<a href="https://x.example/?a=1&amp;b=2" target="_blank"' in html


def test_unknown_constructs_survive_as_text():
    """Never a hole: whatever the converter does not know is still readable."""
    html = guides.render("~~struck~~ and a footnote[^1]")
    assert "~~struck~~ and a footnote[^1]" in html


# --- the shipped pages ------------------------------------------------------

def test_page_names_lists_every_shipped_page_index_first():
    names = guides.page_names()
    assert names[0] == guides.INDEX
    assert set(names) == {p.stem for p in GUIDES.glob("*.md")}
    # The five role pages the ? links point at, and the four setup pages.
    for role_page in ("net-control", "sag", "liaison", "logistics", "staff",
                      "setup", "setup-people", "setup-race-week", "setup-ideas"):
        assert role_page in names


@pytest.mark.parametrize("name", guides.page_names())
def test_every_link_and_image_in_every_page_resolves(name):
    page = guides.load(name)
    assert page is not None and page.title
    for href in re.findall(r'href="([^"]+)"', page.html):
        if href.startswith(("http://", "https://", "#")):
            continue
        assert href.startswith("/help/"), (name, href)
        target = href[len("/help/"):].split("#", 1)[0]
        assert guides.load(target or guides.INDEX) is not None, (name, href)
    for src in re.findall(r'<img src="([^"]+)"', page.html):
        assert src.startswith("/help/images/"), (name, src)
        assert (GUIDES / "images" / src[len("/help/images/"):]).is_file(), (name, src)
    # Nothing Markdown got through unrendered.
    assert "](" not in page.html, name
    assert "**" not in page.html, name


def test_load_refuses_anything_that_is_not_a_page_name():
    for bad in ("../schema", "images/x.png", "net-control.md", "Net-Control", "", "."):
        assert guides.load(bad) is None, bad


# --- served ------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    settings = Settings(callsign="KI4TST", passcode="-1", host="h", port=1,
                        db_path=tmp_path / "t.sqlite3", log_level="WARNING")
    with TestClient(create_app(settings)) as c:
        yield c


def test_guides_are_served_without_a_token(client):
    """The ? on a role page opens without asking anyone for anything, and
    a page carries the navigation to every other guide."""
    r = client.get("/help/")
    assert r.status_code == 200
    assert "Course Ops - volunteer guides" in r.text
    assert 'href="/help/net-control"' in r.text
    assert "/static/guide.css?v=" in r.text  # versioned like the app's own

    r = client.get("/help/sag")
    assert r.status_code == 200
    assert 'aria-current="page"' in r.text
    assert "<h1" in r.text

    r = client.get("/help", follow_redirects=False)
    assert r.status_code == 200


def test_unknown_guide_is_404(client):
    assert client.get("/help/nothing-here").status_code == 404
    assert client.get("/help/..%2Fschema.sql").status_code == 404


def test_screenshots_are_served_beside_the_pages(client):
    r = client.get("/help/images/shared-topbar.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_the_help_rings_point_into_the_app():
    """Both `?` rings used to point at the GitHub Wiki; a club running its
    own copy was sending volunteers to ours."""
    static = Path(guides.__file__).with_name("static")
    for f in ("app.js", "setup.js", "setup.html", "index.html"):
        assert "github.com/MikeWills/CourseOps/wiki" not in (static / f).read_text(encoding="utf-8"), f
    assert "const HELP_BASE = '/help/';" in (static / "app.js").read_text(encoding="utf-8")
    assert "const HELP_BASE = '/help/';" in (static / "setup.js").read_text(encoding="utf-8")


def test_guides_ship_in_the_wheel_and_the_frozen_build():
    """`pip install -e .` reads the source tree and hides a missing
    package-data entry; the .exe unpacks only what the spec names."""
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"guides/*.md"' in pyproject and '"guides/images/*"' in pyproject
    spec = (root / "packaging" / "courseops.spec").read_text(encoding="utf-8")
    assert '"courseops/guides"' in spec
