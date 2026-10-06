# Security

## Report a vulnerability

Report a security problem privately, through GitHub's private vulnerability
reporting: open the **Security** tab of this repository and choose **Report a
vulnerability**. Do not open a public issue for it.

## What counts

- A way for a guest to act as a member, or for a stranger to make Joshua run.
- A path that reaches outside the data volume, or a person's files from another
  person's session.
- A token or a credential in a log, in an error message, or in a tool result.
- A URL the internet agent fetches that nobody gave it.
- A route that a fleet identity can call when `docs/contracts.md` says it
  cannot.

## What does not count

- A wrong answer from the model.
- A tool that does what its allowlist permits. You choose the allowlist.
- A problem in a package MCP server you added. Report it to that package.

## Response

Best effort, and no promise of a time. The fix lands in a release, with a line
in `docs/CHANGELOG.md`. `docs/security.md` describes the model this project
defends, and "What you accept when you run Joshua" says what it does not
promise.
