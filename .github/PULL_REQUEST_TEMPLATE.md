Closes #

## What changed

<!-- What the code does now. Short sentences. Active voice. -->

## Why

<!-- Which of the three goals this serves, and what was wrong before. -->

## Checks

- [ ] `make lint`, `make test`, and `make test-dates` pass.
- [ ] `uv run python scripts/check_test_policy.py` passes.
- [ ] Tests cover the change, including the denied case for a security change.
- [ ] The docs that this change makes wrong are changed here, and
      `docs/CHANGELOG.md` has a line under `## Unreleased` when a person who
      runs Joshua can see the change.
- [ ] This text, the commit messages, and the docs are in Simplified Technical
      English.
- [ ] A version change touches every file `scripts/check_chart_version.py`
      names, and nothing else changes a version.
- [ ] No hostname, IP address, person, or secret from my own installation is in
      the diff.
- [ ] When a coding agent wrote the change: the commits carry the
      `Co-Authored-By:` trailer, and the agent pushed no tag.
