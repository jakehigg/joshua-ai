# Contributing

This page is for two kinds of people: someone who runs their own Joshua and
wants to keep their own changes, and someone who wants to send a change back.
Both start the same way.

## Set up the repository

You keep your code in your own fork. The main repository is `upstream`. Your
fork is `origin`.

1. On GitHub, fork the repository.
2. Clone your fork:

```
git clone git@github.com:<you>/joshua-ai.git
cd joshua-ai
```

3. Add the main repository as `upstream`:

```
git remote add upstream https://github.com/jakehigg/joshua-ai.git
git fetch upstream
```

4. Make sure the two remotes are right:

```
git remote -v
```

`origin` is your fork. `upstream` is the main repository. You push to
`origin`. You pull from `upstream`.

## Keep your own changes

Keep `main` clean. It tracks `upstream/main` and nothing else. Put your own
changes on a branch of your own, for example `mine`:

```
git checkout -b mine main
```

Commit your changes there. Push the branch to your fork:

```
git push -u origin mine
```

Run Joshua from that branch. When you want a change from the main repository,
update `main` and put your branch on top of it:

```
git checkout main
git fetch upstream
git merge --ff-only upstream/main
git push origin main
git checkout mine
git rebase main
```

If the rebase stops on a conflict, fix the file, run `git add <file>`, then
`git rebase --continue`. After a rebase, push with `git push --force-with-lease
origin mine`. The flag refuses to overwrite a commit you have not seen.

### Files that never leave your machine

`.env` holds your secrets. `joshua.yaml` holds your people and their handles.
Git ignores both. Git also ignores `local/`. Put anything else that is yours
alone under `local/`: a compose override, a script, notes. Nothing under
`local/` can reach a commit.

Before you push a branch, run:

```
git status
```

Make sure that `.env` and `joshua.yaml` are not in the list.

### A private remote

If you want a second copy of your branch somewhere private, add a second
remote and push there too:

```
git remote add private git@your.git.host:you/joshua-ai.git
git push private mine
```

Push without `-u` here. The branch keeps its tracking on `origin`, and the
push to `private` is an extra copy.

`main` still comes from `upstream`. Your branch goes to `origin`, `private`,
or both. The main repository never sees a push from you. It sees pull requests
only.

## Send a change back

A change for the main repository starts from `main`, not from your own branch.

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

5. Commit. The subject is one short line. The body says why. Write both in
   Simplified Technical English (below).
6. Push the branch to your fork and open a pull request against
   `upstream/main`:

```
git push -u origin 42-telegram-privacy-mode
```

7. In the pull request, say what changed and why, and end with
   `Closes #42`. CI must be green before review.

One pull request closes one issue. The maintainer squash-merges it. After the
merge, update your `main` from `upstream` and delete the branch.

If you run Joshua from your own branch `mine`, and you also want the change
there before the merge, cherry-pick it: `git checkout mine && git cherry-pick
<commit>`. After the merge, a rebase of `mine` onto `main` drops the duplicate.

## Develop

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). Docker is needed
only to run the stack.

```
make sync    # install the workspace
make lint    # ruff check, ruff format --check
make fmt     # ruff format, ruff check --fix
make test    # pytest for every member
make smoke   # postgres in compose + the integration suite
make up      # the whole stack
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
