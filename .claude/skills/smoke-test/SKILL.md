---
name: smoke-test
description: Run a manual smoke test of a running instance after a deploy. Sign in, send one chat turn, and exercise each MCP addon. Use when asked to smoke test a deploy, or to check a release on a running instance. Use it also to judge whether a failing addon blocks the release.
---

# smoke-test

Check a running instance by hand after a deploy. The test covers the sign-in,
one chat turn, and each MCP addon that the instance has.

The test is a release gate. It answers one question: does this build work on a
real instance?

## What the operator gives you

This skill names no host, no person, and no credential. The operator keeps
those in a runbook of their own, and gives you:

- the base URL of the instance.
- the sign-in credentials of a test person.
- the list of the MCP addons that this instance has.
- the version or the commit under test.
- a way to read the core log.

Ask for anything on that list that you do not have. Do not guess a URL.

## 1. Sign in and send one turn

1. Open the base URL and sign in with the credentials.
2. Open the chat and send one short message.
3. Some browsers do not send on Enter. Use the Send button.
4. Wait for the reply. Confirm that it reads as an answer, not as an error.

A sign-in that fails stops the test. A turn with no reply stops the test too.
Report either one at once, because the rest of the test proves nothing.

## 2. Each addon, one read and one write

Exercise each addon through a real chat turn. Ask Joshua in words. Do not call
the addon over HTTP, because that skips the path the person uses.

For each addon on the list from the operator:

1. Ask for something that the addon reads. Confirm that the answer holds real
   data.
2. Ask for something that the addon writes. Confirm the write with a second
   read.
3. Record pass or fail, under the name of the addon.

An addon with no write path gets a read only. Say so in the record.

## 3. Read a partial failure

The chat can work while one addon does not. When the core log says
`mcp servers ready with failures: <name> failed`, that addon did not answer.

Check three things, in this order:

1. The addon pod. Confirm that it runs and that it is healthy.
2. The addon URL in the core configuration. Compare it with the URL that the
   addon serves.
3. The token that the core sends. Compare it with the token that the addon
   expects.

A missing or empty token on the core side is a configuration gap of that
environment. It is not a defect of the build under test.

## 4. Classify every failure

Give each failure one of two classes:

- **a defect of the build**: the code under test is wrong. This fails the
  release. Stop, and open an issue with what you saw.
- **an environment configuration gap**: the build is fine, and the instance
  carries the wrong settings. This does not fail the release. Fix the
  instance and run the test again.

A failure with no class is not a result. Write the class down, and write the
evidence that gives it.

## 5. Record the result

Write the result where the operator keeps it. Record:

- the date.
- the version or the commit under test.
- who ran the test.
- the sign-in and the chat turn: pass or fail.
- each addon by name: pass or fail.
- each failure: the class, and what you saw.

## Checklist

1. Get the base URL, the credentials, the addon list, and the version.
2. Sign in.
3. Send one chat turn with the Send button, and confirm the reply.
4. For each addon, run one read and one write through the chat.
5. Record pass or fail for each addon by name.
6. For each failure, read the core log and check the pod, the URL, and the
   token.
7. Classify each failure as a build defect or an environment gap.
8. Fail the release for a build defect. Do not fail it for an environment gap.
9. Record the date, the version or the commit, who ran the test, and every
   result.
