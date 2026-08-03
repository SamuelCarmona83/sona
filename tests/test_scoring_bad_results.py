"""YouTube candidate scoring prefers studio releases over play-throughs."""
from __future__ import annotations

import unittest

from src.scoring import _rank_candidates, _score_candidate


class ScoringBadResultsTests(unittest.TestCase):
    def test_play_through_loses_to_lyrics_or_official(self):
        query = "Deftones - My Own Summer (Shove It)"
        candidates = [
            {
                "title": "Deftones – My Own Summer (Shove It) [Stephen Carpenter Play-Through]",
                "uploader": "Technofacist",
                "duration": 240,
            },
            {
                "title": "Deftones - My Own Summer (Shove It) / Lyrics",
                "uploader": "Some Channel",
                "duration": 230,
            },
            {
                "title": "Deftones - My Own Summer (Official Music Video) [HD Remaster]",
                "uploader": "Deftones",
                "duration": 230,
            },
        ]
        ranked = _rank_candidates(query, candidates)
        self.assertNotIn("Play-Through", ranked[0]["title"])
        play_score = _score_candidate(query, candidates[0])
        official_score = _score_candidate(query, candidates[2])
        self.assertLess(play_score, official_score)

    def test_bad_pattern_applies_penalty(self):
        query = "Green Day - Basket Case"
        good = {
            "title": "Green Day - Basket Case [Official Music Video]",
            "uploader": "Green Day",
            "duration": 180,
        }
        bad = {
            "title": "Basket Case guitar lesson tutorial",
            "uploader": "GuitarLessons",
            "duration": 400,
        }
        self.assertGreater(_score_candidate(query, good), _score_candidate(query, bad))


if __name__ == "__main__":
    unittest.main()
