from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import requests

from collector.publish import FILES, publish, snapshot, validate_pair


def latest(day):
    return {"generated_at": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)).isoformat(),
            "reference_id": "home", "items": [
                {"id": "home", "area": 84, "representative_manwon": 100 + day, "gap_manwon": 0},
                {"id": "target", "area": 84, "representative_manwon": 200 + day, "gap_manwon": 100},
            ]}


class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return deepcopy(self.data)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class GitHub:
    """In-memory Git objects; only a successful fast-forward changes visible files."""
    def __init__(self):
        self.head = "initial"
        self.trees = {"initial-tree": {
            "config/complexes.json": {"reference_id": "home", "complexes": latest(0)["items"]},
            FILES[0]: latest(0), FILES[1]: [snapshot(latest(0))], "unrelated.txt": "keep me",
        }}
        self.commits = {self.head: {"tree": {"sha": "initial-tree"}, "parents": []}}
        self.calls = []
        self.fail = None
        self.before_patch = None
        self.lose_patch_response = False
        self.conflict_status = 422

    @property
    def files(self):
        return self.trees[self.commits[self.head]["tree"]["sha"]]

    def concurrent(self, change):
        files = deepcopy(self.files)
        change(files)
        tree = f"external-tree-{len(self.trees)}"
        sha = f"external-{len(self.commits)}"
        self.trees[tree] = files
        self.commits[sha] = {"tree": {"sha": tree}, "parents": [self.head]}
        self.head = sha

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("/repos/test/repo/")[1]
        self.calls.append(("GET", path, params))
        if path.startswith("git/ref/"):
            return Response({"object": {"sha": self.head}})
        if path.startswith("git/commits/"):
            return Response(self.commits[path.removeprefix("git/commits/")])
        if path.startswith("contents/"):
            assert headers == {"Accept": "application/vnd.github.raw+json"}
            head = params["ref"]
            files = self.trees[self.commits[head]["tree"]["sha"]]
            data = files[path.removeprefix("contents/")]
            return Response(data)
        raise AssertionError(path)

    def post(self, url, json, timeout=None):
        kind = url.rsplit("/", 1)[1]
        self.calls.append(("POST", kind, deepcopy(json)))
        if self.fail == kind:
            return Response(status=500)
        if kind == "trees":
            files = deepcopy(self.trees[json["base_tree"]])
            for entry in json["tree"]:
                files[entry["path"]] = __import__("json").loads(entry["content"])
            sha = f"tree-{len(self.trees)}"
            self.trees[sha] = files
        elif kind == "commits":
            sha = f"commit-{len(self.commits)}"
            self.commits[sha] = {"tree": {"sha": json["tree"]}, "parents": json["parents"]}
        else:
            raise AssertionError(kind)
        return Response({"sha": sha})

    def patch(self, url, json, timeout=None):
        self.calls.append(("PATCH", url, deepcopy(json)))
        assert json["force"] is False
        if self.before_patch:
            self.before_patch(self)
        if self.fail == "ref":
            return Response(status=403)
        if self.fail == "validation":
            return Response(status=422)
        if self.commits[json["sha"]]["parents"] != [self.head]:
            return Response(status=self.conflict_status)
        self.head = json["sha"]
        if self.lose_patch_response:
            self.lose_patch_response = False
            raise requests.Timeout("response lost after commit")
        return Response({"object": {"sha": self.head}})


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs/data").mkdir(parents=True)
        self.api = GitHub()
        self.write_local(latest(2))

    def write_local(self, data, history=None):
        for path, value in zip(FILES, (data, history if history is not None else [snapshot(data)])):
            (self.root / path).write_text(json.dumps(value))

    def publish(self):
        return publish(self.api, "test/repo", "main", self.root)

    def test_atomic_commit_preserves_remote_history_despite_stale_local_history(self):
        self.assertTrue(self.publish())
        self.assertEqual(self.api.files[FILES[0]], latest(2))
        self.assertEqual(self.api.files[FILES[1]], [snapshot(latest(0)), snapshot(latest(2))])
        self.assertEqual(self.api.files["unrelated.txt"], "keep me")
        self.assertEqual(len(self.api.commits), 2)
        self.assertEqual(len([c for c in self.api.calls if c[0] == "PATCH"]), 1)
        self.assertTrue(all(c[2]["ref"] == "initial" for c in self.api.calls if c[1].startswith("contents/")))

    def test_large_latest_uses_raw_content_and_round_trips(self):
        data = latest(2)
        data["padding"] = "x" * (1024 * 1024)
        self.write_local(data)
        self.publish()
        self.assertEqual(self.api.files[FILES[0]], data)

    def test_failure_at_each_write_leaves_both_remote_files_unchanged(self):
        for stage in ("trees", "commits", "ref", "validation"):
            with self.subTest(stage=stage):
                self.api = GitHub()
                before = deepcopy(self.api.files)
                self.api.fail = stage
                with self.assertRaises(requests.HTTPError):
                    self.publish()
                self.assertEqual(self.api.head, "initial")
                self.assertEqual(self.api.files, before)

    def test_repeat_publish_is_noop(self):
        self.publish()
        head, count = self.api.head, len(self.api.commits)
        self.assertFalse(self.publish())
        self.assertEqual((self.api.head, len(self.api.commits)), (head, count))

    def test_lost_success_response_is_safe_to_retry(self):
        self.api.lose_patch_response = True
        with self.assertRaises(requests.Timeout):
            self.publish()
        validate_pair(self.api.files[FILES[0]], self.api.files[FILES[1]])
        self.assertFalse(self.publish())
        self.assertEqual(len(self.api.commits), 2)

    def test_concurrent_update_rebases_on_remote_history(self):
        def race(api):
            api.before_patch = None
            def change(files):
                files[FILES[0]] = latest(1)
                files[FILES[1]].append(snapshot(latest(1)))
                files["unrelated.txt"] = "concurrent edit"
            api.concurrent(change)
        self.api.before_patch = race
        self.publish()
        self.assertEqual(self.api.files[FILES[1]], [snapshot(latest(i)) for i in range(3)])
        self.assertEqual(self.api.files["unrelated.txt"], "concurrent edit")

    def test_409_conflict_retries(self):
        self.api.conflict_status = 409
        def race(api):
            api.before_patch = None
            api.concurrent(lambda files: files.update({"unrelated.txt": "edit"}))
        self.api.before_patch = race
        self.assertTrue(self.publish())
        self.assertEqual(self.api.files["unrelated.txt"], "edit")

    def test_concurrent_newer_collection_blocks_stale_retry(self):
        def race(api):
            api.before_patch = None
            api.concurrent(lambda files: files.update({FILES[0]: latest(3), FILES[1]: [snapshot(latest(3))]}))
        self.api.before_patch = race
        with self.assertRaisesRegex(RuntimeError, "remote data is newer"):
            self.publish()
        self.assertEqual(self.api.files[FILES[0]], latest(3))

    def test_concurrent_identical_publish_does_not_duplicate_history(self):
        def race(api):
            api.before_patch = None
            api.concurrent(lambda files: files.update({FILES[0]: latest(2),
                FILES[1]: [snapshot(latest(0)), snapshot(latest(2))]}))
        self.api.before_patch = race
        self.assertFalse(self.publish())
        self.assertEqual(len(self.api.files[FILES[1]]), 2)

    def test_remote_read_failure_does_not_write(self):
        original_get = self.api.get
        def get(url, **kwargs):
            if url.endswith(FILES[1]):
                return Response(status=503)
            return original_get(url, **kwargs)
        self.api.get = get
        with self.assertRaises(requests.HTTPError):
            self.publish()
        self.assertEqual(self.api.head, "initial")
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))

    def test_concurrent_config_change_is_revalidated(self):
        def race(api):
            api.before_patch = None
            api.concurrent(lambda files: files["config/complexes.json"]["complexes"].pop())
        self.api.before_patch = race
        with self.assertRaisesRegex(RuntimeError, "configured IDs"):
            self.publish()
        self.assertEqual(self.api.files[FILES[0]], latest(0))

    def test_continuous_contention_is_bounded(self):
        self.api.before_patch = lambda api: api.concurrent(lambda files: files.update({"unrelated.txt": "edit"}))
        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            self.publish()
        self.assertEqual(len([c for c in self.api.calls if c[0] == "PATCH"]), 3)
        self.assertEqual(self.api.files[FILES[0]], latest(0))

    def test_newer_remote_snapshot_is_not_overwritten(self):
        self.api.concurrent(lambda files: files.update({FILES[0]: latest(3), FILES[1]: [snapshot(latest(3))]}))
        with self.assertRaisesRegex(RuntimeError, "remote data is newer"):
            self.publish()
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))

    def test_same_timestamp_different_content_is_rejected(self):
        data = latest(0)
        data["items"][0]["representative_manwon"] = 99
        self.write_local(data)
        with self.assertRaisesRegex(RuntimeError, "same generated_at"):
            self.publish()

    def test_invalid_local_pair_never_contacts_github(self):
        for mutation in (lambda h: h[-1].update(generated_at=latest(1)["generated_at"]),
                         lambda h: h[-1]["values"].pop("target"),
                         lambda h: h[-1]["values"]["target"].update(gap=999)):
            with self.subTest(mutation=mutation):
                history = [snapshot(latest(2))]
                mutation(history)
                self.write_local(latest(2), history)
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.api.calls, [])

    def test_invalid_remote_pair_is_not_replaced(self):
        self.api.files[FILES[1]][-1]["values"].pop("target")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))

    def test_retention_and_local_unpublished_history(self):
        self.api.files[FILES[0]] = latest(105)
        self.api.files[FILES[1]] = [snapshot(latest(i)) for i in range(2, 106)]
        self.write_local(latest(107), [snapshot(latest(106)), snapshot(latest(107))])
        self.publish()
        self.assertEqual(self.api.files[FILES[1]], [snapshot(latest(i)) for i in range(3, 106)] + [snapshot(latest(107))])

    def test_checked_in_pair_is_consistent(self):
        root = Path(__file__).resolve().parents[1]
        validate_pair(*(json.loads((root / path).read_text()) for path in FILES))


if __name__ == "__main__":
    unittest.main()
