---
name: cutting-a-release
description: Cut a release of this repository. Write the changelog entry, merge it, tag the commit on main, and check the artifacts. Use when asked to cut, ship, tag, or publish a release or a version. Use it also to write the changelog entry for a release. Use it when a tag gave no GitHub release, no images, or no chart.
---

# cutting-a-release

A pushed tag `v<version>` makes a release. The `release` workflow runs on a
push of a tag that matches `v*`. Nothing else makes a release.

Read `.github/workflows/release.yml` and `docs/CHANGELOG.md` before you start.

## What the workflow does

The workflow has five jobs:

1. `version` takes the version from the tag name, without the leading `v`.
2. `images` builds four images for amd64 and arm64: channels, core, gateway,
   and assignments.
3. `merge` joins the two builds of one image into a manifest list under the
   version tag.
4. `chart` packages the Helm chart at the release version.
5. `publish` creates the GitHub release and attaches the chart.

A build in `images` pushes the image by digest and writes no tag. Only
`merge` writes the version tag.

## A manual run makes no release

`publish` carries `if: github.event_name == 'push'`. A `workflow_dispatch` run
builds the four images and the chart, and it creates no release. Use a manual
run to rebuild artifacts. Do not use it to release.

## What the release holds

The release holds the packaged chart as its one file.

The release is a pre-release whenever the version starts with `0.`.

GitHub writes the release body from the pull requests merged since the
previous tag (`generate_release_notes: true`). The body is not the changelog,
and nothing copies the changelog into it.

## The changelog

The changelog is `docs/CHANGELOG.md`. It is not in the repository root.

Write the entry before the tag, in a pull request of its own, and merge that
pull request first. Match the shape of the entries that are there:

- a heading `## <version> - <date>`.
- a short summary of what changed for the person who runs Joshua.
- a section `### Upgrading from <previous version>` with the steps an operator
  takes, such as a config change or a schema change.

Move the lines under `## Unreleased` into the new entry, and leave
`## Unreleased` empty.

## Tag the right commit

Tag the commit that sits on `main` after the changelog merge. Verify the
commit with `git rev-parse` before you push the tag. Never tag the head of a
branch that nobody merged.

```bash
git fetch origin
git rev-parse origin/main
git log -1 --oneline origin/main
# Read the summary. It must be the changelog merge.
git tag v0.4.0 <commit>
git push origin v0.4.0
```

A tag on the wrong commit ships the wrong code. A tag is hard to take back
once the images carry it, so read the commit twice.

## After the push

Watch the workflow run, and wait for every job. Then confirm:

1. the four image tags at the version, for channels, core, gateway, and
   assignments.
2. each image tag as a manifest list, with amd64 and arm64 in it.
3. the chart asset on the release, at the version.
4. the release itself, with the body that GitHub wrote.
5. the pre-release mark, for a version that starts with `0.`.

A job that fails leaves part of the work done. Fix the cause, delete the tag,
and push it again on the same commit.

## Checklist

1. Pick the version. Read the previous tag and the entries in
   `docs/CHANGELOG.md`.
2. Write the changelog entry in `docs/CHANGELOG.md`, with the summary and the
   "Upgrading from" section.
3. Open the pull request for the changelog alone. Merge it into `main`.
4. Run `git fetch origin`, then `git rev-parse origin/main`.
5. Read the commit. Confirm that it is the changelog merge.
6. Run `git tag v<version> <commit>`.
7. Run `git push origin v<version>`.
8. Watch the workflow run to the end.
9. Confirm the four image tags, the chart asset, and the release.
10. Confirm the pre-release mark for a `0.` version.
