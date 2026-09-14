"""The volunteer guides, served from inside the app at `/help/<page>`.

They were published to the GitHub Wiki. That put the one thing a volunteer
reads on race morning on a different site, behind a different look, at a URL
that named a code-hosting service - and a club running its own copy had to
point the `?` at its own fork's wiki or ship links to ours. The guides now
ship WITH the app: `guides/*.md` in the package, rendered here, so the `?` on
every screen opens the page for that role on the same server the app came
from, and the guide a club sees is the one for the version it is running.

The pages stay Markdown. GitHub renders them in the repository, they diff in
a pull request, and a screenshot is a file beside them. What renders them is
the small converter below, not a dependency: the pages use a dozen constructs
(headings, paragraphs, quotes, two kinds of list, tables, bold, italic, code,
links, images, a rule) and a full Markdown library is another package a club
has to install for the sake of nothing on these pages. Anything the converter
does not know is left as text, never dropped, so an unsupported construct
shows up as odd-looking words rather than a hole. `tests/test_guides.py`
renders every shipped page and checks every link and image resolves.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path

from .resources import package_file

GUIDES_DIR = package_file("guides")

# The landing page. `README.md` so GitHub shows it when someone browses the
# directory; served here as `/help/`.
INDEX = "README"

# Running order for the navigation, not alphabetical: the basics first because
# every other page tells the reader to start there, then the roles in the
# order the runbook introduces them, then setup. Anything not named lands at
# the end in file order, so a new page appears without editing this.
ORDER = ["everyone", "net-control", "sag", "liaison", "logistics", "staff",
         "setup", "setup-people", "setup-race-week", "setup-ideas"]

# Short names for the navigation. The page's own H1 is the title on the page;
# these are what fit in a list down the side of a phone screen.
NAV_TITLES = {
    INDEX: "All the guides",
    "everyone": "The basics",
    "net-control": "Net Control",
    "sag": "SAG",
    "liaison": "Liaison",
    "logistics": "Logistics",
    "staff": "Staff",
    "setup": "Setup: the course",
    "setup-people": "Setup: roster and links",
    "setup-race-week": "Setup: race week and after",
    "setup-ideas": "Making it yours",
}

# A page name is a bare slug. Anything else - a dot, a slash, an encoded
# anything - is refused before it reaches the filesystem.
PAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass
class Page:
    name: str
    title: str
    html: str


def page_names(root: Path = GUIDES_DIR) -> list[str]:
    """Every page, index first, then `ORDER`, then the rest by filename."""
    found = {p.stem for p in root.glob("*.md")}
    names = [INDEX] if INDEX in found else []
    names += [n for n in ORDER if n in found]
    names += sorted(n for n in found if n not in names)
    return names


def nav_title(name: str, root: Path = GUIDES_DIR) -> str:
    if name in NAV_TITLES:
        return NAV_TITLES[name]
    return _h1(_source(name, root)) or name


def load(name: str, root: Path = GUIDES_DIR) -> Page | None:
    """The rendered page, or None for anything that is not a page."""
    if name != INDEX and not PAGE_NAME.match(name):
        return None
    path = root / f"{name}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    return Page(name=name, title=_h1(text) or nav_title(name, root), html=render(text))


def _source(name: str, root: Path) -> str:
    try:
        return (root / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _h1(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# --- Markdown, the subset these pages use ------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_OL_ITEM = re.compile(r"^\d+\.\s+(.*)$")
_UL_ITEM = re.compile(r"^[-*]\s+(.*)$")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})\s*$")

# Inline, in the order they are tried. Code first so nothing inside a span is
# touched; images before links because `![` contains `[`.
_CODE = re.compile(r"`([^`]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_STRONG = re.compile(r"\*\*(.+?)\*\*")
_EM = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?![*\w])")


def render(text: str) -> str:
    """Markdown (the subset above) to an HTML fragment."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if _HR.match(stripped):
            out.append("<hr>")
            i += 1
            continue

        m = _HEADING.match(stripped)
        if m:
            level = len(m.group(1))
            title = m.group(2)
            out.append(f'<h{level} id="{_slug(title)}">{_inline(title)}</h{level}>')
            i += 1
            continue

        if stripped.startswith(">"):
            # A quote runs until a blank line that is not itself quoted, and
            # its body is Markdown again - the pages put a heading and bold
            # paragraphs inside the "radio comes first" box.
            body: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                inner = lines[i].strip()[1:]
                body.append(inner[1:] if inner.startswith(" ") else inner)
                i += 1
            out.append(f"<blockquote>{render(chr(10).join(body))}</blockquote>")
            continue

        if _UL_ITEM.match(stripped) or _OL_ITEM.match(stripped):
            pattern = _UL_ITEM if _UL_ITEM.match(stripped) else _OL_ITEM
            tag = "ul" if pattern is _UL_ITEM else "ol"
            items: list[str] = []
            while i < n:
                m = pattern.match(lines[i].strip())
                if not m:
                    break
                item = [m.group(1)]
                i += 1
                # Continuation lines: anything indented that is not the next
                # item. A blank line ends the list.
                while i < n and lines[i].strip() and not pattern.match(lines[i].strip()) \
                        and lines[i][:1] in (" ", "\t"):
                    item.append(lines[i].strip())
                    i += 1
                items.append(f"<li>{_inline(' '.join(item))}</li>")
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        if stripped.startswith("|") and i + 1 < n and _TABLE_RULE.match(lines[i + 1].strip()):
            head = _cells(stripped)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i].strip()))
                i += 1
            thead = "".join(f"<th>{_inline(c)}</th>" for c in head)
            tbody = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                for row in rows
            )
            out.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            continue

        if _IMAGE.fullmatch(stripped):
            # An image on a line of its own is a figure, not a word in a
            # paragraph; the alt text becomes its caption, because on these
            # pages it is written as one.
            m = _IMAGE.fullmatch(stripped)
            assert m is not None
            alt, src = m.groups()
            out.append(f'<figure><img src="{_href(src)}" alt="{escape(alt, quote=True)}" '
                       f'loading="lazy"><figcaption>{_inline(alt)}</figcaption></figure>')
            i += 1
            continue

        # A paragraph: consecutive non-blank lines that start nothing else.
        para = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if not nxt or _starts_block(nxt, lines[i + 1].strip() if i + 1 < n else ""):
                break
            para.append(nxt)
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    return "\n".join(out)


def _starts_block(stripped: str, following: str) -> bool:
    return bool(
        _HEADING.match(stripped) or _HR.match(stripped) or stripped.startswith(">")
        or _UL_ITEM.match(stripped) or _OL_ITEM.match(stripped)
        or (stripped.startswith("|") and _TABLE_RULE.match(following))
        or _IMAGE.fullmatch(stripped)
    )


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def _slug(title: str) -> str:
    plain = re.sub(r"[`*_\[\]()!]", "", title).lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def _href(target: str) -> str:
    """Where a link in a page points once it is served from `/help/`.

    Sibling pages are `name.md` in the source so GitHub can follow them; here
    they become `/help/name`. A `../` path reaches out of the package - the
    runbook in `docs/`, say - which is not shipped, so it is resolved against
    where these pages sit in the repository and sent there. Images are mounted
    beside the pages and made absolute, so they resolve from `/help` and
    `/help/` alike.
    """
    if target.startswith(("http://", "https://", "#", "mailto:")):
        return escape(target, quote=True)
    if target.startswith("../"):
        path = posixpath.normpath(posixpath.join(GUIDES_IN_REPO, target))
        return escape(REPO_BLOB + path, quote=True)
    path, _, anchor = target.partition("#")
    if path.endswith(".md"):
        name = Path(path).stem
        name = "" if name == INDEX else name
        return escape(f"/help/{name}" + (f"#{anchor}" if anchor else ""), quote=True)
    return escape("/help/" + target.lstrip("./"), quote=True)


# The one place a guide may point outside the app: the runbook and the plan
# are for the club's technical person and are not shipped with the app.
REPO_BLOB = "https://github.com/MikeWills/CourseOps/blob/main/"
GUIDES_IN_REPO = "src/courseops/guides"


def _inline(text: str) -> str:
    """Spans. Escaped first; every construct then emits its own markup."""
    # Pull code spans out before anything else so their contents are literal.
    codes: list[str] = []

    def stash(m: re.Match[str]) -> str:
        codes.append(f"<code>{escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE.sub(stash, text)
    text = escape(text, quote=False)

    # Targets were escaped along with the rest of the line; `_href` escapes
    # what it emits, so an `&` in a URL would otherwise arrive doubled.
    def image(m: re.Match[str]) -> str:
        return (f'<img src="{_href(unescape(m.group(2)))}" '
                f'alt="{escape(unescape(m.group(1)), quote=True)}">')

    def link(m: re.Match[str]) -> str:
        label, href = m.group(1), _href(unescape(m.group(2)))
        # Anything leaving the app opens in a new tab: a volunteer reading
        # this with the map in the tab behind should not lose the page.
        external = href.startswith(("http://", "https://"))
        rel = ' target="_blank" rel="noopener noreferrer"' if external else ""
        return f'<a href="{href}"{rel}>{label}</a>'

    text = _IMAGE.sub(image, text)
    text = _LINK.sub(link, text)
    text = _STRONG.sub(r"<strong>\1</strong>", text)
    text = _EM.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


# --- the page shell -----------------------------------------------------------

def page_html(page: Page, names: list[str], root: Path = GUIDES_DIR) -> str:
    """The guide inside the app's own chrome: navy bar, the lockup, a list of
    every guide, and the page. Self-contained apart from the shared fonts and
    logo, which the app has already cached."""
    e = escape
    items = []
    for n in names:
        href = "/help/" if n == INDEX else f"/help/{n}"
        current = ' aria-current="page"' if n == page.name else ""
        items.append(f'<li><a href="{href}"{current}>{e(nav_title(n, root))}</a></li>')
    nav = "".join(items)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(page.title)} - Course Ops guide</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/guide.css">
</head>
<body>
<header id="bar">
  <a href="/help/" class="brand"><img src="/static/logo-lockup-reversed.svg"
     alt="Course Ops" height="30"></a>
  <span class="crumb">Guides</span>
</header>
<div id="frame">
  <nav id="pages" aria-label="All the guides">
    <ul>{nav}</ul>
  </nav>
  <main id="guide">
{page.html}
  </main>
</div>
</body></html>
"""
