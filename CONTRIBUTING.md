# Contributing

Thank you for your interest in Joshua. This page says how to send a change.

## Set up

You work in a fork. The main repository is `upstream`. Your fork is `origin`.

1. Fork the repository on GitHub.
2. Clone your fork and add the main repository:

```
git clone git@github.com:<you>/joshua-ai.git
cd joshua-ai
git remote add upstream https://github.com/jakehigg/joshua-ai.git
git fetch upstream
```

3. Install the workspace:

```
make sync
```

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). Docker is needed
only to run the stack.

## Make a change

1. Open an issue first, or find the issue the change belongs to. An issue
   says: Goal, Why, Spec, Acceptance criteria, Tests, Out of scope.
2. Branch from an up-to-date `main`. Name the branch `<issue>-<slug>`:

```
git checkout main
git fetch upstream
git merge --ff-only upstream/main
git checkout -b 42-telegram-privacy-mode
```

3. Make the change. Add or change the tests. Change the docs that the change
   makes wrong.
4. Run the checks:

```
make lint
make test
uv run python scripts/check_test_policy.py
```

5. To try the change in the stack, run your own build of the checkout:

```
make up-dev
```

`make up` runs the released images. `make up-dev` builds the three images from
your working tree.

6. Commit. The subject is one short line. The body says why. Write both in
   Simplified Technical English (below).
7. Push the branch to your fork and open a pull request against
   `upstream/main`:

```
git push -u origin 42-telegram-privacy-mode
```

8. In the pull request, say what changed and why, and end with `Closes #42`.
   CI must be green before review.

One pull request closes one issue. The maintainer squash-merges it. After the
merge, update your `main` from `upstream` and delete the branch:

```
git checkout main
git fetch upstream
git merge --ff-only upstream/main
git push origin main
git branch -d 42-telegram-privacy-mode
```

## Files that never reach a commit

`.env` holds your secrets. `joshua.yaml` holds your people and their handles.
Git ignores both. Before you push, run `git status` and make sure that neither
is in the list.

No hostname, IP address, person, or secret from your own installation belongs
in a diff.

## The commands

```
make sync    # install the workspace
make lint    # ruff check, ruff format --check, chart version
make fmt     # ruff format, ruff check --fix
make test    # pytest for every member
make smoke   # postgres in compose + the integration suite
make up      # the whole stack, from the released images
make up-dev  # the whole stack, from your build of this checkout
```

`CLAUDE.md` holds the conventions: the three goals, the vocabulary, the auth
model, the data volume, the coding rules, and the test rules. Read it before a
change. It is written for people and for coding agents alike.

## The writing standard

Every document, docstring, comment, commit message, pull request, and error
message is in Simplified Technical English (ASD-STE100). Short sentences.
Active voice. One name for one thing. No marketing words.

The rules and a linter are in the repository, in `.claude/skills/ste-writing/`.
A coding agent that supports skills picks it up on its own. Score a page by
hand with:

```
python3 .claude/skills/ste-writing/scripts/ste-lint.py docs/quickstart.md
```

The linter prints violations per 100 words. Aim for less than 2.5 on prose and
near 0 on a procedure. The score measures form, not truth. Check the facts
yourself.

## License of a contribution

Joshua is under the GNU Affero General Public License, version 3 or later
(`LICENSE`). A pull request contributes the change under the same license. There
is no separate contributor agreement.

## What a reviewer looks for

- The change serves the three goals in order. Security first.
- Every path on the data volume goes through `joshua_shared.layout` or the
  files MCP resolver.
- A security claim has a test that tries the denied case.
- The docs that the change makes wrong are changed in the same pull request,
  and `docs/CHANGELOG.md` has a line when a person who runs Joshua can see the
  change.
- No hostname, IP address, person, or secret from your own installation is in
  the diff.
