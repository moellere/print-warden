import pytest

from print_warden import outcome, stats


def test_prompt_pages():
    assert [a for a, _ in outcome.first("j1", completed=True)] == [
        "PRINT_WARDEN_OUTCOME_j1:good",
        "PRINT_WARDEN_OUTCOME_j1:failed",
    ]
    cancelled = [a for a, _ in outcome.first("j1", completed=False)]
    assert cancelled == [a for a, _ in outcome.reasons("j1", 1)]
    every = [
        a.split(":")[1] for a, _ in outcome.reasons("j1", 1) + outcome.reasons("j1", 2)
    ]
    assert set(outcome.REASONS) <= set(every)
    assert all(len(outcome.reasons("j1", p)) <= 3 for p in (1, 2))


def test_parse_and_label():
    assert outcome.parse("abc123:layer_shift") == ("abc123", "layer_shift")
    with pytest.raises(ValueError):
        outcome.parse("abc123:explode")
    assert outcome.label("failed", "spaghetti")["outcome_reason"] == "spaghetti"
    with pytest.raises(ValueError):
        outcome.label("good", "adhesion")


def test_labels_win_in_the_stats():
    jobs = [
        {
            "state": "done",
            "material": "PLA",
            "outcome": "failed",
            "outcome_reason": "adhesion",
        },
        {
            "state": "failed",
            "material": "PLA",
            "error": "print_cancelled",
            "outcome": "good",
        },
        {"state": "failed", "material": "PLA", "error": "Klipper shutdown: heater"},
        {"state": "done", "material": "PLA"},
    ]
    (row,) = stats.outcomes(jobs, "material")
    assert (row["done"], row["failed"], row["labelled"]) == (2, 2, 2)
    assert {r["reason"] for r in row["top_failures"]} == {
        "adhesion",
        "Klipper shutdown",
    }
