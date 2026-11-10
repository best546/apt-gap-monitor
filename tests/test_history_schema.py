"""Schema regression fixtures must not be built with publish.snapshot.

The collector and publisher used to disagree about the six added history fields
while all publisher tests passed because their inputs used that same helper.
All data and GitHub operations below are synthetic and isolated in temp files.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector import main as collector
from collector.publish import FILES, publish, snapshot, validate_history, validate_pair
from test_publish import GitHub


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"history_{name}_pair.json").read_text(encoding="utf-8"))


class HistorySchemaTest(unittest.TestCase):
    def setUp(self):
        self.legacy = fixture("legacy")
        self.enriched = fixture("enriched")

    def test_legacy_fixture_remains_unchanged(self):
        before = deepcopy(self.legacy)
        self.assertEqual(snapshot(self.legacy["latest"]), self.legacy["history"][-1])
        validate_pair(self.legacy["latest"], self.legacy["history"])
        self.assertEqual(self.legacy, before)

    def test_enriched_snapshot_preserves_all_metadata(self):
        before = deepcopy(self.enriched)
        self.assertEqual(snapshot(self.enriched["latest"]), self.enriched["history"][-1])
        self.assertEqual(self.enriched, before)

    def test_mixed_history_is_accepted_without_rewriting_old_entries(self):
        history = self.legacy["history"] + self.enriched["history"]
        before = deepcopy(history)
        self.assertEqual(validate_pair(self.enriched["latest"], history), history[-1])
        self.assertEqual(history, before)

    def test_missing_or_non_object_prices_are_rejected(self):
        for value in ({"price": 100}, {"gap": 0}, {}, [], ["price", "gap"], "price", None):
            with self.subTest(value=value):
                history = deepcopy(self.legacy["history"])
                history[0]["values"]["home"] = value
                with self.assertRaisesRegex(ValueError, "price and gap"):
                    validate_history(history)

    def test_all_enriched_values_are_still_compared(self):
        replacements = {
            "price": 999, "gap": 999, "representative_count": 4,
            "representative_months": 12, "confidence": "LOW",
            "gap_confidence": "LOW", "price_change_reason": "WINDOW_SHIFT",
            "significant_gap_change": True,
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                history = deepcopy(self.enriched["history"])
                history[-1]["values"]["target"][field] = replacement
                with self.assertRaisesRegex(ValueError, "do not match"):
                    validate_pair(self.enriched["latest"], history)

    def test_enriched_metadata_cannot_be_silently_dropped(self):
        fields = set(self.enriched["history"][-1]["values"]["target"]) - {"price", "gap"}
        for field in fields:
            with self.subTest(field=field):
                history = deepcopy(self.enriched["history"])
                del history[-1]["values"]["target"][field]
                with self.assertRaisesRegex(ValueError, "do not match"):
                    validate_pair(self.enriched["latest"], history)

    def test_unmapped_final_metadata_is_rejected_instead_of_discarded(self):
        self.enriched["history"][-1]["values"]["target"]["future_field"] = "keep me"
        with self.assertRaisesRegex(ValueError, "do not match"):
            validate_pair(self.enriched["latest"], self.enriched["history"])

    def test_null_prices_and_zero_sample_metadata_are_preserved(self):
        item = self.enriched["latest"]["items"][1]
        value = self.enriched["history"][-1]["values"]["target"]
        item.update(representative_manwon=None, gap_manwon=None)
        value.update(price=None, gap=None)
        missing = {"representative_count": 0, "representative_months": 12,
                   "confidence": "NONE", "gap_confidence": "NONE"}
        item.update(missing)
        value.update(missing)
        self.assertEqual(validate_pair(self.enriched["latest"], self.enriched["history"]),
                         self.enriched["history"][-1])

    def test_history_timestamps_remain_strictly_increasing(self):
        for history in (self.enriched["history"] * 2,
                        self.enriched["history"] + self.legacy["history"]):
            with self.subTest(history=history):
                with self.assertRaisesRegex(ValueError, "unique and increasing"):
                    validate_history(history)


class EnrichedPublishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs/data").mkdir(parents=True)
        self.api = GitHub()
        self.legacy = fixture("legacy")
        self.enriched = fixture("enriched")
        self.set_remote(self.legacy)
        self.write_local(self.enriched)

    def set_remote(self, pair):
        self.api.files[FILES[0]] = deepcopy(pair["latest"])
        self.api.files[FILES[1]] = deepcopy(pair["history"])

    def write_local(self, pair):
        for path, key in zip(FILES, ("latest", "history")):
            (self.root / path).write_text(json.dumps(pair[key]), encoding="utf-8")

    def publish(self):
        return publish(self.api, "test/repo", "main", self.root)

    def previous_enriched_pair(self):
        pair = deepcopy(self.enriched)
        pair["latest"]["generated_at"] = "2026-11-09T00:00:00+00:00"
        pair["history"][-1]["generated_at"] = pair["latest"]["generated_at"]
        pair["history"] = self.legacy["history"] + pair["history"]
        return pair

    def test_upgrade_preserves_legacy_history_and_publishes_metadata_atomically(self):
        self.assertTrue(self.publish())
        self.assertEqual(self.api.files[FILES[0]], self.enriched["latest"])
        self.assertEqual(self.api.files[FILES[1]], self.legacy["history"] + self.enriched["history"])
        self.assertEqual(len([c for c in self.api.calls if c[0] == "PATCH"]), 1)
        self.assertEqual(self.api.files["unrelated.txt"], "keep me")

    def test_remote_metadata_is_preserved_and_local_unpublished_history_is_ignored(self):
        previous = self.previous_enriched_pair()
        self.set_remote(previous)
        # A failed, older local collection must never replace remote history.
        unpublished = deepcopy(previous["history"][-1])
        unpublished["generated_at"] = "2026-11-09T12:00:00+00:00"
        unpublished["values"]["target"]["price"] = 999999
        self.enriched["history"].insert(0, unpublished)
        self.write_local(self.enriched)
        self.assertTrue(self.publish())
        self.assertEqual(self.api.files[FILES[1]], previous["history"] + self.enriched["history"][-1:])

    def test_enriched_repeat_publish_is_noop(self):
        self.assertTrue(self.publish())
        head, count = self.api.head, len(self.api.commits)
        self.assertFalse(self.publish())
        self.assertEqual((self.api.head, len(self.api.commits)), (head, count))

    def test_same_timestamp_metadata_conflict_is_rejected(self):
        self.set_remote(self.enriched)
        self.enriched["latest"]["items"][1]["confidence"] = "LOW"
        self.enriched["history"][-1]["values"]["target"]["confidence"] = "LOW"
        self.write_local(self.enriched)
        with self.assertRaisesRegex(RuntimeError, "same generated_at"):
            self.publish()
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))

    def test_invalid_local_metadata_never_contacts_github(self):
        self.enriched["history"][-1]["values"]["target"]["confidence"] = "LOW"
        self.write_local(self.enriched)
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.publish()
        self.assertEqual(self.api.calls, [])

    def test_invalid_remote_metadata_is_not_replaced(self):
        self.set_remote(self.previous_enriched_pair())
        self.api.files[FILES[1]][-1]["values"]["target"]["confidence"] = "LOW"
        before = deepcopy(self.api.files)
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.publish()
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))
        self.assertEqual(self.api.files, before)

    def test_concurrent_enriched_publish_is_preserved_on_retry(self):
        previous = self.previous_enriched_pair()
        def race(api):
            api.before_patch = None
            api.concurrent(lambda files: files.update({FILES[0]: previous["latest"],
                                                       FILES[1]: previous["history"]}))
        self.api.before_patch = race
        self.assertTrue(self.publish())
        self.assertEqual(self.api.files[FILES[1]], previous["history"] + self.enriched["history"])
        self.assertEqual(len([c for c in self.api.calls if c[0] == "PATCH"]), 2)

    def test_retention_preserves_enriched_entries(self):
        previous = self.previous_enriched_pair()
        end = datetime.fromisoformat(previous["latest"]["generated_at"])
        history = []
        for offset in range(103, -1, -1):
            entry = deepcopy(previous["history"][-1])
            entry["generated_at"] = (end - timedelta(days=offset)).isoformat()
            history.append(entry)
        previous["history"] = history
        self.set_remote(previous)
        self.assertTrue(self.publish())
        self.assertEqual(self.api.files[FILES[1]], history[1:] + self.enriched["history"])
        self.assertEqual(len(self.api.files[FILES[1]]), 104)

    def test_actual_collector_output_matches_fixture_and_can_be_published(self):
        # Run the real collector path with fake data, fixed time, temp outputs,
        # and no live credentials or network. Do not build history via snapshot.
        config = {"reference_id": "home", "months": 6, "history_months": 1,
                  "area_tolerance": 1.2, "complexes": [
                      {"id": "home", "name": "Home", "area": 84, "lawd_cd": "00000"},
                      {"id": "target", "name": "Target", "area": 84, "lawd_cd": "00000"},
                  ]}
        rows = [{"aptNm": name, "excluUseAr": "84", "dealAmount": price,
                 "dealYear": "2026", "dealMonth": "11", "dealDay": str(day),
                 "floor": "10", "dealingGbn": "중개거래"}
                for name, price in (("Home", "100,000"), ("Target", "200,000"))
                for day in range(1, 6)]
        (self.root / FILES[0]).unlink()
        (self.root / FILES[1]).write_text(json.dumps(self.legacy["history"]), encoding="utf-8")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"MOLIT_API_KEY": "fixture-only"}, clear=True))
            stack.enter_context(patch.object(collector, "OUTPUT", self.root / FILES[0]))
            stack.enter_context(patch.object(collector, "HISTORY", self.root / FILES[1]))
            stack.enter_context(patch.object(collector, "load_config", side_effect=[config, {"prices": {}}]))
            stack.enter_context(patch.object(collector, "month_keys", return_value=["202611"]))
            stack.enter_context(patch.object(collector, "fetch_month", return_value=rows))
            stack.enter_context(patch.object(collector.time, "sleep"))
            stack.enter_context(patch.object(collector.requests, "get", side_effect=AssertionError("network forbidden")))
            clock = stack.enter_context(patch.object(collector, "datetime"))
            clock.now.return_value.astimezone.return_value = datetime(2026, 11, 10, tzinfo=timezone.utc)
            collector.main()
        generated_history = json.loads((self.root / FILES[1]).read_text())
        self.assertEqual(generated_history, self.legacy["history"] + self.enriched["history"])
        generated_latest = json.loads((self.root / FILES[0]).read_text())
        for expected, actual in zip(self.enriched["latest"]["items"], generated_latest["items"]):
            self.assertEqual({key: actual[key] for key in expected}, expected)
        self.assertTrue(self.publish())
        self.assertEqual(self.api.files[FILES[1]], generated_history)
        self.assertEqual(self.api.files[FILES[0]], generated_latest)


if __name__ == "__main__":
    unittest.main()
