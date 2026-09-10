"""Turn `docs/wiki/` into a GitHub Wiki checkout.

The wiki is a SEPARATE git repository (`<repo>.wiki.git`) with its own
conventions, so the pages cannot simply be copied across:

  * Pages are addressed without the `.md` suffix, and the landing page must be
    called `Home`. A link that works on github.com (`net-control.md`) 404s in
    the wiki.
  * A wiki page cannot reach a file in the code repository by a relative path -
    they are different repositories - so `../RUNBOOK.md` has to become an
    absolute URL.
  * Images are served from the wiki's own raw host. Relative image paths do
    sometimes resolve, but the raw URL is the documented form and does not
    depend on which view the reader arrived through.

The sync is deliberately ONE WAY. The wiki working copy is emptied and
rewritten from `docs/wiki/` on every run, so anything typed into the wiki's
editor is overwritten: the repository stays the source of truth, and every
change to these pages still goes through a pull request. `_Footer.md` says so
on every page, because a reader who has just lost an edit deserves to have been
warned before making it.

Usage:
    python tools/build_wiki.py <source dir> <wiki checkout> [--repo owner/name]
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

# The landing page of a GitHub wiki is always called Home.
HOME = "Home"

# Where the wiki's own files are served from. This is the wiki repository, not
# the code repository: /wiki/ in the path is what distinguishes them.
RAW = "https://raw.githubusercontent.com/wiki/{repo}/{path}"
BLOB = "https://github.com/{repo}/blob/main/{path}"

# Markdown links and images: ![alt](target) and [text](target).
LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")


def page_name(filename: str) -> str:
    """The wiki page a source file becomes."""
    stem = Path(filename).stem
    return HOME if stem.lower() == "readme" else stem


def rewrite(text: str, repo: str) -> str:
    """Point every link and image at where it lives in the wiki."""

    def one(match: re.Match[str]) -> str:
        bang, label, target = match.groups()

        if bang:
            # An image. Anything already absolute is left alone.
            if target.startswith(("http://", "https://")):
                return match.group(0)
            return f"![{label}]({RAW.format(repo=repo, path=target.lstrip('./'))})"

        if target.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)

        # A file in the code repository, reached from docs/wiki/ - the wiki
        # cannot follow that relatively, so name it absolutely.
        if target.startswith("../"):
            return (f"[{label}]("
                    f"{BLOB.format(repo=repo, path='docs/' + target[3:])})")

        # A sibling page. Keep any #anchor; drop the .md.
        path, _, anchor = target.partition("#")
        if path.endswith(".md"):
            name = page_name(path)
            return f"[{label}]({name}{'#' + anchor if anchor else ''})"
        return match.group(0)

    return LINK.sub(one, text)


def sidebar(pages: list[str]) -> str:
    """A fixed running order, not alphabetical.

    The basics come first because every other page tells the reader to start
    there; the roles then run in the order the runbook introduces them.
    """
    order = ["Home", "everyone", "net-control", "sag", "liaison", "logistics",
             "staff", "setup", "setup-people", "setup-race-week", "setup-ideas"]
    titles = {
        "Home": "All the guides",
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
    known = [p for p in order if p in pages]
    rest = sorted(p for p in pages if p not in order)
    lines = ["### Course Ops", ""]
    lines += [f"- [{titles.get(p, p)}]({p})" for p in known + rest]
    return "\n".join(lines) + "\n"


def footer(repo: str) -> str:
    """Appended by the wiki to every page.

    Kept short: it is on the bottom of all seven pages.
    """
    source = f"https://github.com/{repo}/tree/main/docs/wiki"
    return (
        f"---\n\n*These pages are generated from [`docs/wiki/`]({source}) in "
        "the repository and are replaced on every change there. Edits made "
        "here will be overwritten - open a pull request instead.*\n"
    )


def build(source: Path, target: Path, repo: str) -> list[str]:
    if not source.is_dir():
        raise SystemExit(f"No such directory: {source}")
    if not (target / ".git").is_dir():
        raise SystemExit(
            f"{target} is not a git checkout. The wiki repository must exist "
            "before it can be written to: enable Wikis on the repository and "
            "create one page in the browser, which is what initialises it."
        )

    # Emptied and rewritten, so a page deleted in the repository disappears
    # from the wiki rather than lingering with nothing pointing at it.
    for path in target.iterdir():
        if path.name == ".git":
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()

    pages: list[str] = []
    for md in sorted(source.glob("*.md")):
        name = page_name(md.name)
        pages.append(name)
        (target / f"{name}.md").write_text(
            rewrite(md.read_text(encoding="utf-8"), repo), encoding="utf-8")

    images = source / "images"
    if images.is_dir():
        shutil.copytree(images, target / "images")

    (target / "_Sidebar.md").write_text(sidebar(pages), encoding="utf-8")
    (target / "_Footer.md").write_text(footer(repo), encoding="utf-8")
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="docs/wiki")
    parser.add_argument("target", type=Path, help="the wiki checkout")
    parser.add_argument("--repo", default="MikeWills/CourseOps",
                        help="owner/name, for absolute links")
    args = parser.parse_args()

    pages = build(args.source, args.target, args.repo)
    print(f"Wrote {len(pages)} page(s): {', '.join(pages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
