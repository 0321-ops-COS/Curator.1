"""The pane heartbeat: fake panes, a fake clock, no tmux and no Discord."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402

import heartbeat  # noqa: E402
import status_board  # noqa: E402

JOB_ID = "20261102-101500-deadbeef"
# A sanitized pane in the shape Freebuff draws: a timer and spinner that tick on
# their own, real output, a box rule, and the ad box.
BUSY = (
    "⠋ Working… 11m 33s\n"
    "Reading the parser\n"
    "Running tests\n"
    "────────────────────────\n"
    "Upgrade to Pro at freebuff.com\n"
)


def board(stage: str = status_board.FREEBUFF_WORKING, **over) -> status_board.Board:
    return status_board.Board(job_id=JOB_ID, stage=stage, **over)


class HeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = [2_000_000.0]
        self.beat = heartbeat.Heartbeat(
            ping_seconds=600.0, stall_seconds=300.0, now=lambda: self.clock[0]
        )

    def tick(self, pane: str, **over) -> list[heartbeat.Action]:
        return self.beat.tick(board(**over), pane)

    def notices(self, actions: list[heartbeat.Action]) -> list[str]:
        return [item.text for item in actions if item.kind == heartbeat.NOTIFY]

    def test_timers_spinners_and_the_ad_box_are_not_progress(self) -> None:
        first = heartbeat.signature(BUSY)
        later = heartbeat.signature(BUSY.replace("11m 33s", "11m 58s"))
        self.assertEqual(first, later, "a ticking timer is not a change")
        self.assertNotEqual(first, heartbeat.signature(BUSY + "Wrote bot.py\n"))
        self.assertIn("Running tests", heartbeat.stable_lines(BUSY))
        self.assertEqual(
            [line for line in heartbeat.stable_lines(BUSY) if "Upgrade" in line], []
        )

    def test_every_stage_change_is_one_new_short_message(self) -> None:
        started = self.clock[0] - 65
        actions = self.tick(BUSY, started_at=started)
        self.assertEqual([item.kind for item in actions],
                         [heartbeat.NOTIFY, heartbeat.ACTIVITY])
        first = actions[0].text
        self.assertIn(JOB_ID, first)
        self.assertIn("▰▰▰▱▱▱▱ 3/7 Freebuff working", first)
        self.assertIn("1m 05s", first)
        self.assertEqual(self.notices(self.tick(BUSY, started_at=started)), [],
                         "the same stage is not a new message")
        moved = self.notices(self.tick(BUSY, stage=status_board.TESTS, started_at=started))
        self.assertEqual(len(moved), 1)
        self.assertIn("5/7 tests", moved[0])

    def test_a_moving_pane_is_never_stuck_and_pings_while_it_works(self) -> None:
        started = self.clock[0]
        self.tick(BUSY, started_at=started)
        pane, texts = BUSY, []
        for _ in range(59):  # 10s per step, 590s of real output
            self.clock[0] += 10
            pane += f"\nstep {self.clock[0]}"
            texts += self.notices(self.tick(pane, started_at=started))
        self.assertEqual([text for text in texts if "still working" in text], [])
        self.assertEqual([text for text in texts if "stuck" in text], [])
        self.clock[0] += 30
        pane += "\nstep 600"
        text = self.notices(self.tick(pane, started_at=started))[-1]
        self.assertIn("still working ▰▰▰▱▱▱▱", text)
        self.assertIn("last activity 0s ago", text)

    def test_a_frozen_pane_is_possibly_stuck_once_per_stall(self) -> None:
        started = self.clock[0]
        self.tick(BUSY, started_at=started)
        self.clock[0] += 299
        self.assertEqual(self.notices(self.tick(BUSY, started_at=started)), [],
                         "300s is the threshold, not 299s")
        self.clock[0] += 2
        stuck = self.notices(self.tick(BUSY, started_at=started))
        self.assertEqual(len(stuck), 1)
        self.assertIn("possibly stuck at Freebuff working", stuck[0])
        self.assertIn("301s", stuck[0])
        self.clock[0] += 900
        self.assertEqual(self.notices(self.tick(BUSY, started_at=started)), [],
                         "one message per stall, never a stream of them")

    def test_the_board_line_says_how_long_since_the_pane_moved(self) -> None:
        self.tick(BUSY)
        self.clock[0] += 42
        actions = self.tick(BUSY)
        line = [item.text for item in actions if item.kind == heartbeat.ACTIVITY]
        self.assertEqual(line, ["last activity 42s ago"])
        drawn = status_board.render(replace(board(), activity=line[0]))
        self.assertIn("last activity 42s ago", drawn.splitlines()[-1])
