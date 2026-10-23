"""Additional details and the medium list from the posting flow (pieces_controller)."""
import pytest

from src.modules.pieces.pieces_controller import _apply_details
from src.shared.utils.app_error import AppError


def test_mediums_set_the_filterable_medium_to_the_first():
    assert _apply_details({"mediums": [" Oil ", "Charcoal"]}) == {
        "mediums": ["Oil", "Charcoal"],
        "medium": "Oil",
    }


def test_an_empty_medium_list_clears_the_list_but_not_medium():
    assert _apply_details({"mediums": []}) == {"mediums": None}


def test_edition_and_signed():
    assert _apply_details({"edition": " 1 of 1 ", "signed": True}) == {
        "edition": "1 of 1",
        "signed": True,
    }
    assert _apply_details({"edition": "", "signed": None}) == {"edition": None, "signed": None}


def test_absent_keys_change_nothing():
    assert _apply_details({"title": "x"}) == {}


@pytest.mark.parametrize("body", [
    {"mediums": "Oil"}, {"mediums": ["Oil", ""]}, {"mediums": [3]},
    {"edition": 5}, {"edition": "x" * 33}, {"signed": "yes"},
])
def test_rejects_bad_values(body):
    with pytest.raises(AppError):
        _apply_details(body)
