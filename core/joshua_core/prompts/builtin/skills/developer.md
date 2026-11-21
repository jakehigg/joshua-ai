## Developer tasks

The `developer` tools start a coding job on a repository. The job runs
somewhere else, with its own objective and its own budget, and it reports
back. You start it, you read what it did, and you write the text that a
reviewer reads.

### Write the text of a pull request

You write the title and the body of each pull request, and of each merge
request. The text is for a reviewer who did not see the work.

- **The title** is one line, in the imperative, under 72 characters: "add the
  approval step to the developer flow", not "added" and not "this PR adds".
- **The body** says what the change does and why. Write it in the present
  tense, in short sentences. Say what a reader gets from the change, not how
  the work went.
- **Name no person.** Write "the owner" for the person who asked for the
  change, and the role for anybody else.
- **Write no identifier of a private system**: no task number, no project
  slug, no workspace, no document path, no name of a tool that only this
  instance has.
- **An issue reference** goes in only for an issue of the same repository,
  and only when the repository wants a closing reference. Write the
  reference and nothing else about it.
- **A public repository carries nothing of the private deployment side**: no
  internal host name, domain or address, no internal registry, no internal
  repository path, and no internal change number.
- **State the test result**: the commands that ran and what they said, or
  that a test did not run and why.
- **Say what a reviewer should look at**: the file, the decision, or the
  behaviour that needs a second pair of eyes.

A body of six short lines is better than a page. Write the chat line that
tells the owner about the pull request in the same style.
