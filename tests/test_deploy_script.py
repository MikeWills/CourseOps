"""The deploy script, checked for the traps that only show up in production.

Nothing here runs the script - it touches a real server. These assert the
shape of it, because the failures they guard against are silent: a deploy that
reports success and changes nothing is worse than one that fails.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"
WORKFLOW = (Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "deploy.yml")


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_ref_is_resolved_before_it_is_checked_out(script):
    """`git checkout main` on the server does NOT deploy origin/main.

    The working copy sits on a detached HEAD, so a local `main` is whatever it
    was when the server was cloned. Checking it out deploys stale code, reports
    success, and leaves someone staring at a version that did not change.
    """
    assert 'refs/remotes/origin/$TAG' in script
    assert "--detach" in script
    # The bare form is the bug. It must not come back.
    assert not re.search(r'checkout --force "\$TAG"', script)


def test_an_unknown_ref_stops_rather_than_deploying_something(script):
    """A typo must not silently deploy whatever HEAD happens to be."""
    assert "No tag or branch called" in script
    assert re.search(r"exit 1", script)


def test_a_tag_wins_over_a_branch_of_the_same_name(script):
    tag_at = script.index("refs/tags/$TAG")
    branch_at = script.index("refs/remotes/origin/$TAG")
    assert tag_at < branch_at, "tags must be tried first"


def test_the_workflow_can_be_run_by_hand_against_a_branch():
    """The escape hatch for being away from a terminal: Actions -> Run
    workflow, which should not require inventing a tag from a phone."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "default: main" in text


def test_the_workflow_does_not_claim_to_wait_for_something_it_does_not():
    """The header used to say it waited for the release build's artefacts. It
    never did, and the server installs from git, so nothing was waiting."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "waits for the release build" not in text


def test_the_install_path_is_not_hardcoded(script):
    """The install is wherever the club put it.

    A VPS with a mounted data volume may well have it under /mnt rather than
    /opt. Hardcoding the default meant the script would cd somewhere else
    entirely and fail on its first line - or, worse, operate on another tree.
    """
    assert 'APP_DIR:-/opt/courseops' not in script
    assert 'BASH_SOURCE' in script, "APP_DIR should derive from the script location"
    # Still overridable, for anyone who needs to.
    assert 'APP_DIR="${APP_DIR:-' in script


def test_the_script_is_committed_executable():
    """A clone must be able to run it.

    Git stores the executable bit, and on Windows `core.filemode` is usually
    false - so a script added there lands as 100644 and nobody notices until a
    Linux box tries to run it and says "Permission denied". That is what
    happened: the documented invocation, and the deploy over SSH, both failed
    on a file that looked perfectly fine in the listing.
    """
    out = subprocess.run(
        ["git", "ls-files", "-s", "deploy/deploy.sh"],
        cwd=SCRIPT.parents[1], capture_output=True, text=True,
    )
    if out.returncode != 0:          # not a git checkout (an sdist, say)
        import pytest
        pytest.skip("not a git working tree")
    mode = out.stdout.split()[0]
    assert mode == "100755", f"deploy.sh is committed as {mode}, not executable"


# --- the forced command ------------------------------------------------------
#
# These DO run a script: ssh-deploy-command.sh is pure validation and exec,
# and the exec target is swapped for a stub that records what it was given.

DEPLOY_DIR = SCRIPT.parent
BASH = "bash"


def _bash_available() -> bool:
    try:
        return subprocess.run([BASH, "-c", "true"], capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.fixture
def forced_command(tmp_path):
    """A copy of the validator whose deploy.sh is a stub echoing its argument."""
    if not _bash_available():
        pytest.skip("bash is not available")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "ssh-deploy-command.sh").write_bytes(
        (DEPLOY_DIR / "ssh-deploy-command.sh").read_bytes())
    (deploy / "deploy.sh").write_text(
        '#!/usr/bin/env bash\nprintf "deploy:%s:%s\n" "$#" "$1"\n', encoding="utf-8")
    (deploy / "deploy.sh").chmod(0o755)   # exec needs the bit; Windows hides that

    def run(original_command: str):
        return subprocess.run(
            [BASH, str(deploy / "ssh-deploy-command.sh")],
            env={"PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": original_command},
            capture_output=True, text=True,
        )
    return run


@pytest.mark.parametrize("cmd, ref", [
    ("/mnt/volume_nyc3_01/opt/courseops/deploy/deploy.sh v2026.9.1", "v2026.9.1"),
    ("/opt/courseops/deploy/deploy.sh main", "main"),
    ("fix/stitch-invents-a-leg", "fix/stitch-invents-a-leg"),
])
def test_the_forced_command_passes_a_ref_through(forced_command, cmd, ref):
    out = forced_command(cmd)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"deploy:1:{ref}"


@pytest.mark.parametrize("cmd", [
    "v1; rm -rf /",
    "v1 && cat .env",
    "$(id)",
    "`id`",
    "v1|sh",
    "--force",                       # must not become an option to git
    "-",
    "",
    "deploy.sh 'v1'",
    "v" * 70,                        # longer than any real ref
])
def test_the_forced_command_refuses_anything_that_is_not_a_ref(forced_command, cmd):
    """A leaked SSH_KEY secret can deploy a ref, and nothing else."""
    out = forced_command(cmd)
    assert out.returncode == 2, out.stdout
    assert "refused" in out.stderr
    assert "deploy:" not in out.stdout


# --- backup.sh -----------------------------------------------------------------


def test_backup_takes_a_consistent_copy_and_rotates_per_label(tmp_path):
    """Between releases the only copy of the event used to be the live file.

    .backup, not cp, because the database is in WAL mode. Rotation is per
    label so a burst of deploys cannot push the nightlies out.
    """
    if not _bash_available():
        pytest.skip("bash is not available")
    if subprocess.run([BASH, "-c", "command -v sqlite3"], capture_output=True).returncode:
        pytest.skip("sqlite3 CLI is not available")
    import sqlite3
    (tmp_path / "deploy").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "deploy" / "backup.sh").write_bytes((DEPLOY_DIR / "backup.sh").read_bytes())
    con = sqlite3.connect(tmp_path / "data" / "courseops.sqlite3")
    con.execute("pragma journal_mode=wal")
    con.execute("create table t(x)")
    con.execute("insert into t values (42)")
    con.commit()

    def run(label, keep="2"):
        return subprocess.run(
            [BASH, str(tmp_path / "deploy" / "backup.sh"), label],
            env={"PATH": "/usr/bin:/bin", "KEEP": keep},
            capture_output=True, text=True,
        )

    for _ in range(3):
        out = run("nightly")
        assert out.returncode == 0, out.stderr
    out = run("pre-deploy")
    assert out.returncode == 0, out.stderr

    backups = tmp_path / "backups"
    nightly = sorted(backups.glob("nightly-*.sqlite3"))
    assert len(nightly) == 2, "the oldest nightly should have been rotated out"
    assert len(list(backups.glob("pre-deploy-*.sqlite3"))) == 1
    copy = sqlite3.connect(nightly[-1])
    assert copy.execute("select x from t").fetchone() == (42,)


def test_backup_with_no_database_is_not_an_error(tmp_path):
    """A fresh install has no database yet; cron must not page about it."""
    if not _bash_available():
        pytest.skip("bash is not available")
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "backup.sh").write_bytes((DEPLOY_DIR / "backup.sh").read_bytes())
    out = subprocess.run([BASH, str(tmp_path / "deploy" / "backup.sh")],
                         env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert out.returncode == 0
    assert "nothing to back up" in out.stdout


def test_deploy_backs_up_through_the_shared_script(script):
    """One backup implementation, not one in deploy.sh and another in cron."""
    assert "deploy/backup.sh\" pre-deploy" in script
    assert "sqlite3" not in script


@pytest.mark.parametrize("name", ["deploy.sh", "backup.sh", "ssh-deploy-command.sh"])
def test_every_script_is_committed_executable(name):
    """Same trap as deploy.sh: added from Windows, lands as 100644."""
    out = subprocess.run(["git", "ls-files", "-s", f"deploy/{name}"],
                         cwd=SCRIPT.parents[1], capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout:
        pytest.skip("not a git working tree")
    assert out.stdout.split()[0] == "100755", f"{name} is not committed executable"


def test_the_workflow_does_not_guess_the_install_path():
    """The install is wherever the club put it; /opt/courseops was a guess
    that failed only on the server."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "'/opt/courseops'" not in text
    assert "DEPLOY_PATH secret is not set" in text


# --- the Apache vhosts ------------------------------------------------------

VHOSTS = ("apache-courseops.conf", "apache-courseops-ssl.conf")


@pytest.mark.parametrize("name", VHOSTS)
def test_the_vhost_caps_request_bodies(name):
    """Apache's default is no limit. Without this line a request of any
    size reaches uvicorn and the app's own check is the only one, on the
    one route that needs no credential."""
    text = (DEPLOY_DIR / name).read_text(encoding="utf-8")
    match = re.search(r"^\s*LimitRequestBody\s+(\d+)", text, re.M)
    assert match, f"{name} has no LimitRequestBody"
    # Room for the 64 MB course file, and not much more.
    assert 64 * 1024 * 1024 < int(match.group(1)) < 100 * 1024 * 1024


@pytest.mark.parametrize("name", VHOSTS)
def test_the_vhost_passes_the_browsers_host_through(name):
    """The app compares Origin against Host to refuse cross-site setup
    writes; without ProxyPreserveHost every save on the setup screen is
    refused as cross-site."""
    text = (DEPLOY_DIR / name).read_text(encoding="utf-8")
    assert re.search(r"^\s*ProxyPreserveHost On", text, re.M), name



@pytest.mark.parametrize("name", VHOSTS)
def test_the_vhost_does_not_log_the_role_token(name):
    """The token is in the URL path, so the stock "combined" format wrote
    every volunteer's credential to the access log on every request, and
    again in the Referer of every request the page then made."""
    text = (DEPLOY_DIR / name).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        if "CustomLog" in line:
            assert "combined" not in line, name
            assert line.rstrip().endswith("courseops"), name
    fmt = re.search(r'^\s*LogFormat\s+"(.*)"\s+courseops\s*$', text, re.M)
    assert fmt, f"{name} defines no courseops LogFormat"
    for directive in ("%r", "%U", "Referer"):
        assert directive not in fmt.group(1), (name, directive)
    assert "%{LOGPATH}e" in fmt.group(1)
    assert "E=LOGPATH:" in text


def test_the_redaction_pattern_replaces_the_token_and_nothing_else():
    """The RewriteCond regex, checked here with Python's engine - the same
    PCRE dialect for what it uses. The setup API has no token in its path
    and must stay readable in the log."""
    text = (DEPLOY_DIR / "apache-courseops-ssl.conf").read_text(encoding="utf-8")
    cond = re.search(r"RewriteCond %\{REQUEST_URI\} (\^/\(e\|api\|ws\)\S+)\n"
                     r"\s*RewriteRule \^ - \[E=LOGPATH:(\S+)\]", text)
    assert cond, "the redaction rule pair is missing"
    pattern = re.compile(cond.group(1))
    template = cond.group(2).replace("%1", r"\1").replace("%2", r"\2").replace("%3", r"\3")

    def logged(uri):
        m = pattern.match(uri)
        return m.expand(template) if m else uri

    assert logged("/e/m2026/AbC123") == "/e/m2026/-token-"
    assert logged("/api/m2026/AbC123/state") == "/api/m2026/-token-/state"
    assert logged("/ws/m2026/AbC123") == "/ws/m2026/-token-"
    assert logged("/api/m2026/AbC123/incidents/4/status") \
        == "/api/m2026/-token-/incidents/4/status"
    assert logged("/api/setup/events/3/links") == "/api/setup/events/3/links"
    assert logged("/static/app.js") == "/static/app.js"
    assert logged("/help/sag") == "/help/sag"



# --- the deploy workflow ----------------------------------------------------

def _run_blocks(text: str) -> list[str]:
    """The `run: |` scripts of the workflow, as the shell sees them."""
    blocks, current, indent = [], None, None
    for line in text.splitlines():
        if current is not None:
            if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                blocks.append("\n".join(current))
                current = None
            else:
                current.append(line)
                continue
        if line.strip().startswith("run: |"):
            current, indent = [], len(line) - len(line.lstrip())
    if current:
        blocks.append("\n".join(current))
    return blocks


def test_no_expression_is_spliced_into_a_shell_line():
    """`${{ }}` is substituted into the script TEXT before the shell runs
    it, so a tag named v1$(cat ~/.ssh/id_deploy) would execute on the runner
    with the deploy key in reach. Everything goes through env: and quoting."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for block in _run_blocks(text):
        assert "${{" not in block, block


def test_the_workflow_checks_the_ref_like_the_forced_command_does():
    text = WORKFLOW.read_text(encoding="utf-8")
    forced = (DEPLOY_DIR / "ssh-deploy-command.sh").read_text(encoding="utf-8")
    pattern = "^[A-Za-z0-9][A-Za-z0-9._/-]{0,60}$"
    assert pattern in forced
    assert pattern in text


def test_the_host_key_can_be_pinned():
    """ssh-keyscan at deploy time trusts whatever answers - on every run.
    A recorded key in a secret is what pinning means."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "SSH_KNOWN_HOSTS" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "StrictHostKeyChecking=no" not in text
