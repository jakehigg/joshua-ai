"""Chunker tests, ported unchanged from joshua-kb (tests/test_kb.py).

The chunker is the biggest retrieval-quality lever, so its behaviour is pinned:
heading-scoped chunks, breadcrumb prefixes, oversized-section splitting, and the
tiny-section folding rules (including the leading-section case).
"""

from __future__ import annotations

from joshua_core.memory.chunker import chunk_markdown


def test_chunker_sections_and_breadcrumbs() -> None:
    md = (
        "Intro paragraph before any heading, long enough to stand on its own as a "
        "chunk of meaningful documentation text for everyone.\n\n"
        "# Networking\n\n"
        "Overview of the network, also comfortably past the minimum chunk size so "
        "it does not get folded away into a neighbor.\n\n"
        "## VLANs\n\n"
        "Guest wifi is isolated on VLAN 30. The IoT devices live on VLAN 20 and "
        "cannot reach the LAN except through defined firewall rules.\n\n"
        "```\n# not a heading, just a comment in code\necho hi\n```\n"
    )
    chunks = chunk_markdown(md, max_chars=1600)
    assert chunks[0].heading == "" and chunks[0].text.startswith("Intro paragraph")
    assert any(c.heading == "Networking" for c in chunks)
    vlan = [c for c in chunks if c.heading == "Networking > VLANs"]
    assert len(vlan) == 1
    assert "# not a heading" in vlan[0].text
    assert vlan[0].embed_text("Infra").startswith("Infra > Networking > VLANs\n")


def test_chunker_oversized_section_splits() -> None:
    big = "# Big\n\n" + "\n\n".join(f"Paragraph {i} " + "x" * 120 for i in range(20))
    parts = [c for c in chunk_markdown(big, max_chars=400) if c.heading == "Big"]
    assert len(parts) > 1
    assert all(len(c.text) <= 400 for c in parts)


def test_chunker_tiny_trailing_section_folds() -> None:
    tiny = (
        "# A\n\n"
        "Solid section body text that clears the minimum size easily, "
        "with plenty of words to spare for the fold test.\n\n# B\n\nok\n"
    )
    folded = chunk_markdown(tiny, max_chars=1600)
    assert len(folded) == 1 and "ok" in folded[0].text


def test_chunker_tiny_leading_section_not_solo() -> None:
    lead = (
        "[← Back to Home](/en/home)\n\n# Networking\n\n"
        "Overview of the network, comfortably past the minimum chunk size so "
        "it stands on its own as a proper section of documentation.\n"
    )
    led = chunk_markdown(lead, max_chars=1600)
    assert len(led) == 1
    assert led[0].heading == "Networking" and "← Back to Home" in led[0].text
