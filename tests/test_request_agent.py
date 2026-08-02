"""Unit tests for request agent parse / validate / heuristic."""
from __future__ import annotations

import unittest

from src.request_agent import (
    heuristic_plan,
    parse_plan_dict,
    planned_track_labels,
    summarize_actions,
    validate_and_cap_plan,
)


class RequestAgentTests(unittest.TestCase):
    def test_parse_plan_filters_unknown_actions(self):
        plan = parse_plan_dict({
            "reply": "ok",
            "actions": [
                {"type": "enqueue", "queries": ["a"], "position": "end"},
                {"type": "radio_off"},
                {"type": "SKIP"},
            ],
        })
        types = [a["type"] for a in plan.actions]
        self.assertEqual(types, ["enqueue", "skip"])
        self.assertEqual(plan.reply, "ok")

    def test_validate_caps_tracks_to_ten(self):
        plan = parse_plan_dict({
            "reply": "big",
            "actions": [{
                "type": "enqueue",
                "queries": [f"song {i}" for i in range(20)],
                "position": "end",
            }],
        })
        capped = validate_and_cap_plan(plan, user_slots_left=50, max_tracks=10)
        self.assertEqual(len(capped.actions[0]["queries"]), 10)
        self.assertEqual(capped.track_budget_used(), 10)

    def test_validate_respects_user_slots(self):
        plan = parse_plan_dict({
            "reply": "x",
            "actions": [
                {"type": "enqueue", "queries": ["a", "b", "c"], "position": "end"},
            ],
        })
        capped = validate_and_cap_plan(plan, user_slots_left=2, max_tracks=10)
        self.assertEqual(capped.actions[0]["queries"], ["a", "b"])

    def test_validate_zero_slots_drops_enqueue_keeps_skip(self):
        plan = parse_plan_dict({
            "reply": "full",
            "actions": [
                {"type": "skip"},
                {"type": "enqueue", "queries": ["x"], "position": "end"},
            ],
        })
        capped = validate_and_cap_plan(plan, user_slots_left=0, max_tracks=10)
        self.assertEqual([a["type"] for a in capped.actions], ["skip"])

    def test_set_auto_and_genre_cap(self):
        plan = parse_plan_dict({
            "reply": "g",
            "actions": [
                {"type": "set_auto", "enabled": True},
                {"type": "genre_playlist", "genre": "jazz", "count": 99, "hints": ""},
            ],
        })
        capped = validate_and_cap_plan(plan, user_slots_left=5, max_tracks=10)
        self.assertEqual(capped.actions[0], {"type": "set_auto", "enabled": True})
        self.assertEqual(capped.actions[1]["count"], 5)
        self.assertEqual(capped.track_budget_used(), 5)

    def test_heuristic_enqueue(self):
        plan = heuristic_plan("pon never gonna give you up", auto_enabled=False)
        self.assertEqual(plan.source, "heuristic")
        self.assertEqual(plan.actions[0]["type"], "enqueue")
        self.assertIn("never gonna", plan.actions[0]["queries"][0].lower())

    def test_heuristic_move_remove(self):
        move = heuristic_plan("mueve la 3 a 1", auto_enabled=False)
        self.assertEqual(move.actions[0], {"type": "move", "from_pos": 3, "to_pos": 1})
        rm = heuristic_plan("quita la 2", auto_enabled=False)
        self.assertEqual(rm.actions[0], {"type": "remove", "pos": 2})

    def test_heuristic_set_auto(self):
        on = heuristic_plan("activa el modo auto", auto_enabled=False)
        self.assertEqual(on.actions[0], {"type": "set_auto", "enabled": True})
        off = heuristic_plan("desactiva el auto", auto_enabled=True)
        self.assertEqual(off.actions[0], {"type": "set_auto", "enabled": False})

    def test_heuristic_genre(self):
        plan = heuristic_plan("dame 5 de synthwave", auto_enabled=False)
        self.assertEqual(plan.actions[0]["type"], "genre_playlist")
        self.assertEqual(plan.actions[0]["count"], 5)
        self.assertIn("synthwave", plan.actions[0]["genre"])

    def test_planned_labels_and_summary(self):
        plan = parse_plan_dict({
            "reply": "r",
            "actions": [
                {"type": "skip"},
                {"type": "enqueue", "queries": ["a", "b"], "position": "front"},
                {"type": "genre_playlist", "genre": "rock", "count": 2, "hints": ""},
            ],
        })
        labels = planned_track_labels(plan)
        self.assertEqual(labels[:2], ["a", "b"])
        self.assertTrue(any("rock" in x for x in labels))
        summary = summarize_actions(plan)
        self.assertIn("skip", summary)
        self.assertIn("+2", summary)


if __name__ == "__main__":
    unittest.main()
