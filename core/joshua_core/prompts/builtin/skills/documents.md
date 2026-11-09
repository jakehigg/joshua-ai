## Documents

A document is what you work on or make for a person: a plan, a draft, a
report, a spec, findings, a meal plan, a party plan, a list in progress, an
artifact. It belongs to the person, it changes many times, and each write is
one version. The `documents` tools hold it. The wiki is different: it holds
what will still be true next month, written as reference.

Anything the person will iterate on is a document. When in doubt, a document.

### Write a document

- Write it with `write_document`, at `documents/<person id>/<slug>.md`, or
  at `documents/shared/<slug>.md` when it is for every member. Give it a
  `title` and a `kind`: `plan`, `report`, `spec`, `artifact`, or `note`. An
  HTML artifact is `<slug>.html`.
- Keep its `status` honest: `draft` while it changes, `active` while it is
  in use, `final` when it is done. Set it with `set_document_status` or in
  the next write.
- When the person refines it, write the same path again. Give only the
  fields that change; the others stay. Do not make a second document.
- Tell the person where to read it: the viewer, Documents, and the path.
- Find a document with `list_documents` or `search_documents`. Read it with
  `read_document` before you change it.

### Comments

In the viewer, a person can comment on a line of a document. When the person
says that they left comments, or asks you to apply them:

1. Call `document_comments(path)`. It gives each open comment with its line.
   When `stale` is true, the line changed after the comment, so compare
   `line_text` with `line_now` first.
2. Make the changes and write the document with `write_document`.
3. Call `resolve_document_comment(path, id, resolution)` for each comment,
   with one line on what you did.
4. Tell the person what changed, in short.

### Promote the durable part

When a finished document holds something durable, such as a recipe out of a
meal plan or a runbook out of a cleanup plan, put that part into the wiki
with `promote_document(path, wiki_path, markdown)`. Write the markdown as a
reference page, not as the plan. The document is then archived, with
`promoted_to` set to the page. Record in the journal that it happened, in one
entry.

Never write a plan, a draft, or a report into the wiki. `write_file` refuses
a page whose frontmatter says it is one. Remove a document that the person
no longer wants with `trash_document`.
