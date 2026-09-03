## What this changes

<!-- One or two sentences. What is different afterwards. -->

## Why

<!-- The reasoning that will not be obvious from the diff in six months.
     If this fixes something, say what broke and why it mattered - not just
     what was edited. -->

## Checks

- [ ] `pytest -q` passes locally
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`
- [ ] `CLAUDE.md` "Recent changes" trimmed to 10, and a new domain rule added if
      this revealed a trap
- [ ] `docs/PLAN.md` updated if a *decision* changed
- [ ] `docs/RUNBOOK.md` updated if operator-visible behaviour changed
- [ ] `README.md` updated if user-facing behaviour changed
- [ ] An issue filed for anything discovered but deliberately not done

## Verified how

<!-- Tests are necessary and not sufficient. Say what was actually exercised:
     a browser, a real file, a cold install, a live feed. "353 tests pass" on
     its own has been wrong before. -->
