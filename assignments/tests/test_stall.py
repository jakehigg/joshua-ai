"""The stall rule: the count of slices with no checkpoint, the error text, the question."""

from __future__ import annotations

from pathlib import Path

from joshua_assignments import stall

WORK = Path("/work/12")


def test_clean_error_keeps_the_scratch_and_hides_every_other_path() -> None:
    text = (
        "fatal: could not create '/work/12/repos/gitops': and /home/joshua/.ssh/id_rsa "
        "and /work/120/x and /work/12"
    )
    cleaned = stall.clean_error(text, WORK)
    assert "/work/12/repos/gitops" in cleaned
    assert "/home/joshua" not in cleaned and "/work/120" not in cleaned
    assert cleaned.count(stall.HIDDEN_PATH) == 2
    assert cleaned.endswith("and /work/12")


def test_clean_error_keeps_a_url_path_and_drops_its_user_part() -> None:
    cleaned = stall.clean_error(
        "unable to access 'https://user:hunter2@gitlab.example.net/infra/gitops.git/': 502",
        WORK,
    )
    assert "hunter2" not in cleaned and "user:" not in cleaned
    assert "https://gitlab.example.net/infra/gitops.git/" in cleaned


def test_clean_error_masks_a_token_and_cuts_the_text_short() -> None:
    cleaned = stall.clean_error(
        "Authorization: Bearer abc123 glpat-0123456789abcdef\n\n" + "x" * 1000, WORK
    )
    assert "abc123" not in cleaned and "glpat-0123" not in cleaned
    assert "\n" not in cleaned
    assert len(cleaned) == stall.MAX_ERROR and cleaned.endswith("...")


def test_the_counter_stops_at_the_limit_and_a_checkpoint_resets_it() -> None:
    counter = stall.StallCounter(3, WORK)
    assert counter.slice_ended(progress=False, errors=["boom"]) is False
    assert counter.slice_ended(progress=False, errors=[]) is False
    assert counter.slice_ended(progress=True, errors=["other"]) is False
    assert (counter.slices, counter.errors) == (0, [])
    assert counter.slice_ended(progress=False, errors=["a"]) is False
    assert counter.slice_ended(progress=False, errors=["b", " ", "a"]) is False
    assert counter.slice_ended(progress=False, errors=["c", "d"]) is True
    # The last distinct errors, at most MAX_ERRORS, the newest last.
    assert counter.errors == ["a", "c", "d"]
    assert stall.StallCounter(0, WORK).limit == 1


def test_the_question_names_the_checkpoint_the_errors_and_the_page() -> None:
    counter = stall.StallCounter(2, WORK)
    counter.slice_ended(progress=False, errors=["git clone: 502"])
    counter.slice_ended(progress=False, errors=["git clone: 502"])
    text = counter.question("cloning.\nNext: " + "y" * 900, "assignments/12-x.md")
    assert text.startswith("I stopped because 2 slices in a row ended with no new checkpoint")
    assert "My last checkpoint: cloning. Next: y" in text
    assert "y" * (stall.MAX_CHECKPOINT) not in text
    assert text.count("- git clone: 502") == 1
    assert "My plan and progress are at assignments/12-x.md." in text
    empty = stall.StallCounter(1, WORK)
    empty.slice_ended(progress=False, errors=[])
    assert "I have no checkpoint." in empty.question(None, "p")
    assert "I saw no tool error." in empty.question(None, "p")
