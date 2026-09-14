# Brief for fix agents (2026-09-14 audit)

You are fixing one WORKSTREAM from `docs/audit/2026-09-14-tasks.md`. Other
agents are fixing the other workstreams at the same time in their own git
worktrees. Do not touch files outside your workstream's area unless a task
explicitly names them.

## Where things are

- Main checkout (read the audit docs from here, absolute path):
  `C:\Users\mikew\SyncThing\Projects\HamRadio\CourseOps\docs\audit\`
  - `2026-09-14-tasks.md` - your task list (update the checkboxes HERE, in
    the main checkout, not in your worktree; other agents update it too, so
    use a precise string replace of your own task line only).
  - `2026-09-14-report.md` - context.
  - `2026-09-14-raw/*.md` - the agent reports with evidence, exact lines and
    reproductions. Read the Source entries for each task before starting it.
- You are in a git worktree on your own branch. Work there.
- Python: there is no venv in the worktree. Use the main checkout's
  interpreter with PYTHONPATH pointed at YOUR worktree's `src`, otherwise
  the editable install imports the main checkout's code and your tests test
  the wrong tree. PowerShell:
  `$env:PYTHONPATH="src"; & "C:\Users\mikew\SyncThing\Projects\HamRadio\CourseOps\.venv\Scripts\python.exe" -m pytest -q`
  Verify once before trusting a test run:
  `$env:PYTHONPATH="src"; & "...\.venv\Scripts\python.exe" -c "import courseops; print(courseops.__file__)"`
  must print a path inside your worktree.
- Never run `courseops serve`/`ingest` or anything that touches the network.
  Never modify `.env`.

## Method

1. Read `CLAUDE.md` in full first: "Domain rules" and "Working method" are
   binding. Then read your tasks and their Source entries.
2. Work the tasks in the order listed (severity first). For each task:
   - Mark it `[~] <branch>` in the tasks file in the main checkout.
   - Write the failing test first where the task says "Done when".
   - Fix it. Match the surrounding code's style and comment density; comments
     explain WHY, referencing the event-day failure.
   - Run the full test suite. Green before you commit.
   - Update `CHANGELOG.md` under `## [Unreleased]` (what broke and why it
     mattered - not what you edited), and everything else in the
     CLAUDE.md "Documentation discipline" table that the change touches:
     the guides in `src/courseops/guides/` if a volunteer or officer would
     SEE the change, RUNBOOK/DEPLOYMENT for operator-visible behaviour, a new
     CLAUDE.md domain-rule bullet if the fix revealed a trap. Do NOT edit the
     CLAUDE.md "Recent changes" list or the test count - the merger does that.
   - One commit per task: `fix: <what>` / `chore: <what>`, body says why.
     End the message with the attribution lines given in your system
     reminder.
   - Mark it `[x] <branch>` in the tasks file.
3. If a task turns out to be wrong, already fixed, or bigger than its
   estimate, do not silently skip or narrow it: leave it `[ ]`, add a one-line
   `NOTE:` under it in the tasks file saying why, and move on.
4. When all your tasks are done (or noted): push your branch, open ONE pull
   request for the workstream with `gh pr create` - title
   `Audit 2026-09-14: workstream <X> - <area>`, body listing each task ID and
   a sentence on what changed, ending with the PR attribution lines from your
   system reminder. Do not merge. Do not push to main. Do not tag.
   If push or `gh` fails, commit locally and say so in your final report.
5. Final message: the PR URL (or "committed locally, push failed: <error>"),
   the list of task IDs done / noted, the test count before and after, and
   anything you saw that is NOT in the task list.

The branch is one per workstream, not one per task (a deliberate deviation
from "one PR per idea" so seven parallel streams do not become fifty PRs);
keep the commits one per task so the PR reads as a sequence of ideas.
