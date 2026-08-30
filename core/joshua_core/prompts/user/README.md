# User prompt snippets

Put your own system prompt content here. Use it for house rules, guidance for a
custom MCP server you added to `joshua.yaml`, or any other instruction.

The prompt composer loads every `.md` file in this directory. It sorts the files
by name and appends them to each session's system prompt, after the kernel files
and before the identity block. The snippets apply to every profile (dm, group,
guest, event, scheduled). You do not edit any Python code or the kernel files in
`builtin/`.

## How to add a snippet

1. Copy `example.md.sample` to a new file with a `.md` name, for example
   `house-rules.md`.
2. Write your instructions in the file.
3. Restart core, or start a new session, to load the change.

## Rules

- Only `.md` files load. This `README.md` and any dotfile (a name that starts
  with `.`) are ignored.
- The composer sorts files by name. Use a name prefix such as `10-`, `20-` to
  set the order.
- The kernel files in `builtin/` ship with Joshua. Do not edit them. Add your content
  here instead, so an upstream pull does not overwrite it.
