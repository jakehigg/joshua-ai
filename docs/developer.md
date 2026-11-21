# Developer tasks and their pull requests

A developer task is a coding job on a repository. The `developer` MCP server
runs it, outside this repository, and Joshua starts it, reads what it did, and
publishes the result: a task that finished leaves a pushed branch, and Joshua
opens the pull request for it.

This page is the core side of that flow: who writes the text of the pull
request, when Joshua opens it, and the setting that decides whether it asks
you first. [mcp-servers.md](mcp-servers.md) has how an MCP server is added,
and [settings.md](settings.md) has the settings store.

## Who writes what

| Step | Who does it |
|---|---|
| run the task, commit, push the branch | the `developer` server |
| report what the task did | the `developer` server |
| read the diff and the report | Joshua |
| compose the title and the body | Joshua |
| show the text to you and apply your changes | Joshua |
| open the pull request | Joshua, with the tool of the forge |

The text of a pull request is written by a prompt page of core,
`core/joshua_core/prompts/builtin/skills/developer.md`. The page is in the
system prompt of a session that holds the `developer` server, and of no other
session, and a guest never gets it. So the rules of the text are the same for
every repository, and a repository cannot change them.

The page says that the title is one line, in the imperative, under 72
characters, and that the body says what the change does and why, in the
present tense and in short sentences. It names no person: the person who
asked for a change is "the owner". It carries no identifier of a private
system — no task number, no project slug, no workspace, no document path —
and an issue reference goes in only for an issue of the same repository, and
only when that repository wants a closing reference. For a public repository
the text says nothing of the private deployment side: no internal host,
registry, repository path, or change number. It ends with the test result and
what a reviewer should look at.

## How the pull request is opened

Joshua opens the pull request with the tool of the forge, through the
gateway, so no credential of the forge is in core:

- `create_pull_request` on the `github` server;
- `create_merge_request` on the `gitlab` server.

Both tools belong to servers that an instance already has for its
repositories, so this flow adds no plumbing. Joshua opens no second pull
request for a branch that has one, and a branch with a merge conflict gets
none: Joshua says so and leaves the branch alone.

## The approval

By default Joshua shows you the title and the body, applies the changes you
ask for, and opens the pull request when you approve it. Then it tells you in
one line what is open, with the link.

The approval is a setting of the instance, not a rule of the product:

| Setting | Default | What it does |
|---|---|---|
| `developer.pr_skip_approval` | off | On, Joshua opens the pull request as soon as the text is ready and gives you the text and the link afterwards. |

You write it under Admin → Instance, in the **Developer tasks** section, as
"Open a pull request without asking". It is a switch, so one tap writes it,
and an instance that has never written it asks for the approval. A value that
is neither a yes nor a no is refused at the write.

Core reads the setting when it builds a session that holds the `developer`
server, and no other session reads it. With the switch on, the system prompt
of that session carries one block that replaces the approval step of the
page.
