"""Load input safety and complete accounting when the arrival generator stalls."""

from contextlib import redirect_stderr
from io import StringIO
import unittest
from unittest.mock import patch

from scripts.load_test import main, run_paced_workload


class LoadToolTests(unittest.TestCase):
    def test_generator_stall_counts_all_remaining_arrivals_without_docker(self) -> None:
        current_time = [100.0]

        def pause_with_stall(delay: float) -> None:
            current_time[0] += delay + 2.0

        with patch("scripts.load_test.execute") as execute:
            result = run_paced_workload(
                lambda sequence, scheduled: {"sequence": sequence},
                duration=1.0,
                rate=10.0,
                concurrency=2,
                clock=lambda: current_time[0],
                pause=pause_with_stall,
            )
        execute.assert_not_called()
        self.assertEqual(result.planned_requests, 10)
        self.assertEqual(result.offered_requests, 1)
        self.assertEqual(result.unoffered_requests, 9)
        self.assertEqual(result.capacity_dropped_requests, 0)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(
            len(result.records) + result.dropped_requests, result.planned_requests
        )

    def test_fractional_window_preserves_each_in_window_arrival(self) -> None:
        current_time = [100.0]

        def advance(delay: float) -> None:
            current_time[0] += delay

        result = run_paced_workload(
            lambda sequence, scheduled: {"sequence": sequence, "scheduled": scheduled},
            duration=1.25,
            rate=2.0,
            concurrency=2,
            clock=lambda: current_time[0],
            pause=advance,
        )
        self.assertEqual(result.planned_requests, 3)
        self.assertEqual(result.offered_requests, 3)
        self.assertEqual(result.unoffered_requests, 0)
        self.assertEqual(result.dropped_requests, 0)
        self.assertEqual(
            sorted(row["scheduled"] for row in result.records), [100.0, 100.5, 101.0]
        )
        self.assertEqual(
            len(result.records) + result.dropped_requests, result.planned_requests
        )

    def test_invalid_dataset_bounds_fail_before_provisioning(self) -> None:
        for arguments in (
            ["--users", "1"],
            ["--users", "101"],
            ["--users", "8", "--notes", "7"],
            ["--notes", "100001"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch("sys.argv", ["load_test", *arguments]),
                patch("scripts.load_test.execute") as execute,
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main()
            self.assertEqual(raised.exception.code, 2)
            execute.assert_not_called()
