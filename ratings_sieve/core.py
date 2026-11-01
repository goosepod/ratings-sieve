"""Pure preference policy. Input order is AudioMuse's similarity order."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    minimum_rating: int = 3
    below_threshold: str = "exclude"
    include_unrated: bool = True
    boost_hearts: bool = True
    candidate_multiplier: int = 4

    def __post_init__(self):
        if type(self.minimum_rating) is not int or not 1 <= self.minimum_rating <= 5:
            raise ValueError("minimum_rating must be 1..5")
        if self.below_threshold != "exclude":
            raise ValueError("only exclude is supported")
        if type(self.candidate_multiplier) is not int or not 1 <= self.candidate_multiplier <= 10:
            raise ValueError("candidate_multiplier must be 1..10")
        if type(self.include_unrated) is not bool or type(self.boost_hearts) is not bool:
            raise ValueError("policy flags must be booleans")


@dataclass(frozen=True)
class Preference:
    rating: int = 0
    heart: bool = False

    @classmethod
    def from_song(cls, song):
        rating = song.get("userRating", 0)
        if type(rating) is not int or not 0 <= rating <= 5:
            raise ValueError("invalid userRating")
        return cls(rating, bool(song.get("starred")))


def unique_candidates(candidates, seed):
    seen = {seed}
    result = []
    for track in candidates:
        item_id = track.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("candidate missing item_id")
        if item_id not in seen:
            seen.add(item_id)
            result.append(track)
    return result


def select(candidates, preferences, count, policy=Policy()):
    eligible = []
    for track in candidates:
        pref = preferences[track["item_id"]]  # Missing lookup is an error, never unrated.
        if pref.rating == 0:
            if not policy.include_unrated:
                continue
        elif pref.rating < policy.minimum_rating:
            continue
        eligible.append(track)
    if policy.boost_hearts:
        eligible.sort(key=lambda track: not preferences[track["item_id"]].heart)
    return eligible[:count]
