"""The profile schema rejects rules the code cannot apply."""
import copy

import pytest
from pydantic import ValidationError

from app.profile.schema import Profile, ProfileRules, RangeLabels


def rules_dict(profile: Profile) -> dict:
    return profile.model_dump(include=set(ProfileRules.model_fields))


def test_committed_profile_is_valid(fema_profile):
    assert fema_profile.streams and fema_profile.segmented_pages()
    assert fema_profile.validation is not None


@pytest.mark.parametrize("path, value", [
    (("streams", 0, "number_regex"), r"^(\d+(?:\.\d+)*)"),  # no named group num
    (("table_caption", "regex"), r"^Table (?P<num>[0-9]+"),  # does not compile
    (("footnote", "line_regex"), r"^\d+\."),  # no marker group
    (("where_block", "item_regex"), r"^(?P<symbol>\S+) = .+"),  # no definition group
])
def test_bad_regexes_are_rejected(fema_profile, path, value):
    data = copy.deepcopy(rules_dict(fema_profile))
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ProfileRules.model_validate(data)


def test_example_must_match_its_stream(fema_profile):
    data = copy.deepcopy(rules_dict(fema_profile))
    data["streams"][0]["examples"] = ["not a number"]
    with pytest.raises(ValidationError):
        ProfileRules.model_validate(data)


def test_commentary_must_point_at_a_stream(fema_profile):
    data = copy.deepcopy(rules_dict(fema_profile))
    commentary = next(s for s in data["streams"] if s["commentary_of"])
    commentary["commentary_of"] = "missing"
    with pytest.raises(ValidationError):
        ProfileRules.model_validate(data)


def test_range_labels_checks():
    labels = RangeLabels(ranges=[{"start": 1, "end": 2, "kind": "front_matter"}, {"start": 3, "end": 9, "kind": "body"}],
                         sample_pages=[3, 4, 5, 6], title_pages=[1], notes="")
    assert labels.errors_for(9) == []
    assert labels.errors_for(5)  # a range ends after the last page
    with pytest.raises(ValidationError):
        RangeLabels(ranges=[{"start": 1, "end": 5, "kind": "body"}, {"start": 4, "end": 9, "kind": "body"}],
                    sample_pages=[1, 2, 3, 4], title_pages=[], notes="")
