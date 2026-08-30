Closes #

## What changed

<!-- What the code does now. Short sentences. Active voice. -->

## Why

<!-- Which of the three goals this serves, and what was wrong before. -->

## Checks

- [ ] `make lint` and `make test` pass.
- [ ] `uv run python scripts/check_test_policy.py` passes.
- [ ] Tests cover the change, including the denied case for a security change.
- [ ] The docs that this change makes wrong are changed here, and
      `docs/CHANGELOG.md` has a line when a person who runs Joshua can see it.
- [ ] No hostname, IP address, person, or secret from my own installation is in
      the diff.
