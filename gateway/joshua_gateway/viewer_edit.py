"""The one editor of the viewer: the form, and how a save composes the file.

A wiki page and a journal entry are edited through the same form and the
same save. The path decides the shape:

- A journal entry, under a day folder, edits its text and its people. Its
  frontmatter keys stay, its ``people`` list is replaced, and a title
  heading the form did not show is written back.
- Any other wiki page edits its body. Frontmatter, when the file has one, is
  kept byte for byte and shown above the form, not in it.
- A new page is a wiki page: the journal has one writer, the agent's
  ``write_journal_entry`` tool, and this editor never creates an entry.

The routes, the auth, and the write live in ``viewer``. This module builds
HTML and text only.
"""

from __future__ import annotations

from datetime import date
from html import escape
from urllib.parse import quote

import yaml

from joshua_gateway import viewer_journal as journal
from joshua_gateway import viewer_wiki as wiki

# The form field for a page name accepts a path under the folder; every
# segment must be a plain file or folder name.
_NAME_MAX = 200


class EditError(ValueError):
    """A save the person can fix: a bad person id, a bad page name, bad
    frontmatter. The message is shown on the form."""


# -- text -------------------------------------------------------------------


def split_raw_frontmatter(text: str) -> tuple[str, str]:
    """``(raw block, body)``: the leading ``---`` block with its fences and the
    blank line after it, exactly as written, and the rest. ``("", text)`` when
    there is no block or it does not close."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index + 1
            while end < len(lines) and lines[end].strip() == "":
                end += 1
            return "".join(lines[:end]), "".join(lines[end:])
    return "", text


def normalize_body(body: str) -> str:
    """The text from the form: unix line ends, one trailing newline."""
    return body.replace("\r\n", "\n").strip("\n") + "\n"


def compose_wiki(text: str, body: str) -> str:
    """A wiki page after an edit: the file's own frontmatter, then the new
    body. A body that opens its own frontmatter block must parse."""
    front, _ = split_raw_frontmatter(text)
    body = normalize_body(body)
    if body.startswith("---"):
        _check_frontmatter(body)
    return front + body


def compose_new(body: str) -> str:
    """A new wiki page: the body alone. A leading frontmatter block must parse."""
    body = normalize_body(body)
    if body.startswith("---"):
        _check_frontmatter(body)
    return body


def compose_journal(text: str, rel: str, day: date, people_field: str, body: str) -> str:
    """A journal entry after an edit: the frontmatter keys as they were with
    ``people`` replaced, the title heading the form did not show, then the
    new text."""
    try:
        people = journal.parse_people_field(people_field)
    except ValueError as exc:
        raise EditError("A person id is letters, digits and hyphens.") from exc
    front, raw_body = journal.split_frontmatter(text)
    front["people"] = list(people)
    entry = journal.parse_entry(rel, day, text)
    heading, _ = journal._first_heading(raw_body)
    lead = f"# {entry.title}\n\n" if heading and not entry.is_day_page else ""
    return journal.render_frontmatter(front) + lead + normalize_body(body)


def _check_frontmatter(body: str) -> None:
    lines = body.splitlines()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError as exc:
                raise EditError("The frontmatter is not valid YAML.") from exc
            return


def new_page_rel(folder: str, name: str) -> str:
    """The wiki-relative path of a new page from the folder and the name the
    form gave. ``.md`` is added when absent. Raises :class:`EditError` for
    an empty, dotted, absolute, or escaping name."""
    name = name.strip().replace("\\", "/")
    if not name or len(name) > _NAME_MAX:
        raise EditError("Give the page a name.")
    if not name.endswith(".md"):
        name += ".md"
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise EditError("A page name is a file name, or a path of plain names, under the folder.")
    folder = folder.strip("/")
    return f"{folder}/{name}" if folder else name


# -- html -------------------------------------------------------------------


def edit_url(rel: str) -> str:
    return "/edit/" + quote(rel)


def new_url(folder: str) -> str:
    return "/new?folder=" + quote(folder) if folder else "/new"


def form_html(
    *,
    title: str,
    subtitle: str,
    csrf: str,
    body: str,
    cancel_url: str,
    path: str = "",
    people: tuple[str, ...] | None = None,
    folder: str | None = None,
    name: str = "",
    kept_front: str = "",
    error: str = "",
) -> str:
    """The edit form. ``people`` adds the people field (a journal entry).
    ``folder`` makes it a new-page form with a name field. ``kept_front`` is
    the frontmatter the save keeps, shown above the text."""
    parts = [f"<h1>{escape(title)}</h1>", f"<p class=snippet>{escape(subtitle)}</p>"]
    if error:
        parts.append(f'<p class="error">{escape(error)}</p>')
    parts.append('<form class="edit" action="/save" method="post">')
    parts.append(f'<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">')
    if folder is not None:
        parts.append(f'<input type="hidden" name="folder" value="{escape(folder, quote=True)}">')
        parts.append(
            "<label>name (a file name; folders with a slash)<br>"
            f'<input name="name" value="{escape(name, quote=True)}" '
            'placeholder="my-page.md" required></label>'
        )
    else:
        parts.append(f'<input type="hidden" name="path" value="{escape(path, quote=True)}">')
    if people is not None:
        parts.append(
            '<label>people (ids, comma separated)<br><input name="people" '
            f'value="{escape(", ".join(people), quote=True)}"></label>'
        )
    if kept_front:
        parts.append(
            '<details class="kept"><summary>frontmatter, kept as it is</summary>'
            f"<pre>{escape(kept_front.strip())}</pre></details>"
        )
    parts.append(
        f'<label>text<br><textarea name="body" rows="22">{escape(body)}</textarea></label>'
    )
    cancel = escape(cancel_url, quote=True)
    parts.append(f'<p><button type=submit>save</button> <a href="{cancel}">cancel</a></p>')
    parts.append("</form>")
    return "".join(parts)


def edit_link(rel: str) -> str:
    return f'<a href="{edit_url(rel)}">edit this page</a>'


def new_link(folder: str) -> str:
    where = wiki.stem_title(folder.rsplit("/", 1)[-1]) if folder else "the wiki"
    return f'<a href="{new_url(folder)}">new page in {escape(where)}</a>'
