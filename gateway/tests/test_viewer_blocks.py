"""Tests for the cut of Markdown and of a definition into blocks.

The same cut gives the blocks of the body of a document, which
``viewer_api.documents`` sends with the first line and the last line of each
block, so a comment on a block anchors to a line of the file.

Each definition here is the text that the assignments manager renders for
the spec in its comment, so the line numbers are the line numbers of the
manager.
"""

from __future__ import annotations

from joshua_gateway import viewer_blocks as vb

GOAL = """# Clean up

Read each page
and fix the links.

- flat one
- parent
  - nested one
  - nested two
- last

1. first
2. second

> a quote

```sh
make test
```

| a | b |
|---|---|
| 1 | 2 |"""

# {"title": "Clean up the docs", "goal": GOAL, "model": "sonnet",
#  "budget": {"turns": 40}, "repos": ["example/docs"], "dry_run": False}
FULL = (
    "title: Clean up the docs\ngoal: |-\n  # Clean up\n\n  Read each page\n"
    "  and fix the links.\n\n  - flat one\n  - parent\n    - nested one\n"
    "    - nested two\n  - last\n\n  1. first\n  2. second\n\n  > a quote\n\n"
    "  ```sh\n  make test\n  ```\n\n  | a | b |\n  |---|---|\n  | 1 | 2 |\n"
    "model: sonnet\nbudget:\n  turns: 40\nrepos:\n  - example/docs\ndry_run: false"
)


def _cuts(text: str) -> list[tuple[int, int, str, int]]:
    return [(b.first, b.last, b.kind, b.depth) for b in vb.blocks(text)]


def test_the_blocks_of_markdown_and_their_lines() -> None:
    assert _cuts(GOAL) == [
        (1, 2, "h", 0),
        (3, 5, "p", 0),
        (6, 6, "li", 0),
        (7, 7, "li", 0),
        (8, 8, "li", 1),
        (9, 9, "li", 1),
        (10, 11, "li", 0),
        (12, 12, "li", 0),
        (13, 14, "li", 0),
        (15, 16, "quote", 0),
        (17, 20, "code", 0),
        (21, 23, "table", 0),
    ]


def test_each_block_renders_alone() -> None:
    parts = {b.first: b.html for b in vb.blocks(GOAL)}
    assert parts[1] == "<h1>Clean up</h1>\n"
    assert parts[3] == "<p>Read each page\nand fix the links.</p>\n"
    # A list item is in its own list, without its nested list.
    assert parts[7] == "<ul>\n<li>parent</li>\n</ul>\n"
    assert parts[8] == "<ul>\n<li>nested one</li>\n</ul>\n"
    # An ordered item keeps its number.
    assert parts[12] == "<ol>\n<li>first</li>\n</ol>\n"
    assert parts[13] == '<ol start="2">\n<li>second</li>\n</ol>\n'
    assert "<blockquote>" in parts[15]
    assert '<pre><code class="language-sh">make test\n</code></pre>' in parts[17]
    assert "<table>" in parts[21] and "<td>2</td>" in parts[21]


def test_a_loose_item_and_a_rule() -> None:
    text = "- one\n\n  more of one\n- two\n\n---\n\ntext"
    assert _cuts(text) == [(1, 3, "li", 0), (4, 5, "li", 0), (6, 7, "hr", 0), (8, 8, "p", 0)]
    first = vb.blocks(text)[0].html
    assert "<p>one</p>" in first and "<p>more of one</p>" in first


def test_raw_html_is_text() -> None:
    [block] = vb.blocks("<script>alert(1)</script>")
    assert "<script>" not in block.html and "&lt;script&gt;" in block.html


def test_empty_markdown_has_no_block() -> None:
    assert vb.blocks("") == []
    assert vb.blocks("\n\n") == []


def test_the_fields_of_a_definition_and_their_lines() -> None:
    got = [(f.key, f.first, f.last, f.value) for f in vb.fields(FULL)]
    assert got == [
        ("title", 1, 1, "Clean up the docs"),
        ("goal", 2, 25, GOAL),
        ("model", 26, 26, "sonnet"),
        ("budget", 27, 28, {"turns": 40}),
        ("repos", 29, 30, ["example/docs"]),
        ("dry_run", 31, 31, False),
    ]


def _goal(text: str) -> vb.Goal:
    [goal] = [vb.goal_offset(f) for f in vb.fields(text) if f.key == "goal"]
    assert goal is not None
    return goal


def test_the_goal_offset_with_a_title_before_it() -> None:
    goal = _goal(FULL)
    assert goal.header == 2 and goal.markdown == GOAL
    lines = FULL.split("\n")
    # Line n of the goal is line header + n of the definition.
    for block in vb.blocks(goal.markdown):
        first = goal.header + block.first
        assert lines[first - 1] == "  " + GOAL.split("\n")[block.first - 1]
    assert lines[goal.header + 8 - 1] == "    - nested one"


def test_the_goal_offset_with_no_title() -> None:
    # {"goal": "Fix it.", "model": "opus"}
    goal = _goal("goal: |-\n  Fix it.\nmodel: opus")
    assert goal == vb.Goal(1, 2, "Fix it.")
    [block] = vb.blocks(goal.markdown)
    assert goal.header + block.first == 2


def test_the_goal_offset_with_a_title_of_two_lines() -> None:
    # {"title": "Two\nlines", "goal": "Go."}
    text = "title: |-\n  Two\n  lines\ngoal: |-\n  Go."
    [title, _] = vb.fields(text)
    assert (title.first, title.last, title.value) == (1, 3, "Two\nlines")
    assert vb.goal_offset(title) is not None  # a block scalar, but not the goal
    goal = _goal(text)
    assert goal.header == 4 and goal.markdown == "Go."


def test_the_goal_offset_with_an_indent_indicator() -> None:
    # {"goal": "  indented first line\nsecond"}
    goal = _goal("goal: |2-\n    indented first line\n  second")
    assert goal.markdown == "  indented first line\nsecond"


def test_a_goal_that_is_not_a_block_scalar_has_no_offset() -> None:
    [goal] = vb.fields('goal: ""')
    assert goal.value == "" and vb.goal_offset(goal) is None
    [goal] = vb.fields("goal: 3")
    assert vb.goal_offset(goal) is None


def test_a_definition_with_no_goal() -> None:
    # {"model": "opus", "tools": ["a", "b"]}
    text = "model: opus\ntools:\n  - a\n  - b"
    got = [(f.key, f.first, f.last, f.value) for f in vb.fields(text)]
    assert got == [("model", 1, 1, "opus"), ("tools", 2, 4, ["a", "b"])]


def test_lines_yaml_cannot_read_are_kept() -> None:
    [lead, bad] = vb.fields("  stray\nkey: [unclosed")
    assert (lead.key, lead.first, lead.last, lead.parsed) == ("", 1, 1, False)
    assert (bad.key, bad.parsed, bad.lines) == ("key", False, ("key: [unclosed",))
    assert vb.goal_offset(bad) is None
    assert vb.fields("") == []
