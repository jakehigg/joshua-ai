"""The documents section of the viewer: the statuses, the kinds, and the frame.

``/documents`` lists the documents of the person who signed in and the
shared ones, one status at a time (Active, Drafts, Final, Archived), with a
chip for each kind, and ``/documents/<owner>/<slug>`` shows one document.
Both are pages of the React app (``src/pages/Documents.tsx`` and
``src/pages/Document.tsx``), which reads the JSON routes of
``viewer_api.documents``. What stays here is what the server decides: which
status and which kind the query names, whose folder a document is in, and
the content policy of an HTML document.

An HTML document shows in an ``<iframe>`` with ``sandbox`` that loads
``/documents/<owner>/<slug>/artifact``: the frame has an opaque origin, so it
cannot read the viewer or its cookies, submit a form, or open a window. That
route sends ``artifact_policy``, which allows the styles of the document and
no network. Scripts stay off unless the frontmatter says ``scripts: true``;
then the sandbox and the policy allow scripts, and nothing more.

The routes live in ``viewer`` and in ``viewer_api.documents``, and the files
in ``viewer_docstore``.
"""

from __future__ import annotations

# The pills of the list, in order: (status, label, the text when empty).
PILLS = (
    ("active", "Active", "No active document."),
    ("draft", "Drafts", "No draft."),
    ("final", "Final", "No final document."),
    ("archived", "Archived", "No archived document."),
)
DEFAULT_STATUS = "active"
_PILL_KEYS = frozenset(key for key, _, _ in PILLS)


def artifact_policy(scripts: bool) -> str:
    """The content policy of ``/documents/<owner>/<slug>/artifact``.

    ``default-src 'none'`` blocks every load, so the document reaches no
    network: no fetch, no font, and no stylesheet of its own. Inline styles
    are allowed, so a ``<style>`` element and a ``style`` attribute work.
    Images come from the viewer only. ``form-action`` and ``frame-src``
    block a form and a frame of the document. The ``sandbox`` directive adds
    the sandbox of the frame to the response itself, so the document is
    sandboxed also when a browser opens the address alone.

    A document with no scripts gets ``allow-same-origin``: no script can run in
    it (the sandbox has no ``allow-scripts``, and ``default-src 'none'`` blocks
    one too), so the only thing the same origin gives is that the page of the
    viewer can read the size of the frame and fit it to its content. A
    document with ``scripts: true`` never gets ``allow-same-origin``: it has an
    opaque origin, because its scripts must not reach the viewer. Scripts run
    only with ``scripts: true``, and only from the document.
    """
    script_src = "script-src 'unsafe-inline'; " if scripts else ""
    sandbox = "sandbox allow-scripts" if scripts else "sandbox allow-same-origin"
    return (
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
        f"{script_src}form-action 'none'; frame-src 'none'; "
        f"frame-ancestors 'self'; {sandbox}"
    )


def parse_status(raw: str | None) -> str:
    return raw if raw in _PILL_KEYS else DEFAULT_STATUS


def parse_kind(raw: str | None) -> str | None:
    value = (raw or "").strip().lower()
    return value if value and len(value) <= 40 and value.replace("-", "").isalnum() else None


def owner_label(owner: str, person_id: str) -> str:
    return "yours" if owner == person_id else "shared"
