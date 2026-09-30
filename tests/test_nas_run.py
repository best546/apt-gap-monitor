import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/nas-run.sh"
FAKE_DOCKER = '''#!/bin/sh
if [ "$1" = compose ]; then shift; fi
echo "$1" >> "$PROJECT_DIR/calls"
if [ "$1" = version ]; then exit "${VERSION_STATUS:-0}"; fi
if [ "$1" = "${BLOCK_STAGE:-none}" ]; then
  touch "$PROJECT_DIR/started"
  while [ ! -f "$PROJECT_DIR/release" ]; do sleep 0.02; done
fi
if [ "$1" = pull ]; then exit "${PULL_STATUS:-0}"; fi
if [ "$1" = run ]; then exit "${RUN_STATUS:-0}"; fi
exit 1
'''


class NasRunTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("docker", "docker-compose"):
            path = self.bin / name
            path.write_text(FAKE_DOCKER)
            path.chmod(0o755)
        self.env = dict(os.environ, PROJECT_DIR=str(self.root), PATH=f"{self.bin}:/usr/bin:/bin")

    def run_script(self, **env):
        return subprocess.run(["sh", str(SCRIPT)], env=dict(self.env, **env), capture_output=True, text=True, timeout=5)

    def start_blocked(self, stage):
        process = subprocess.Popen(["sh", str(SCRIPT)], env=dict(self.env, BLOCK_STAGE=stage),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        def cleanup():
            (self.root / "release").touch()
            if process.poll() is None:
                process.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not (self.root / "started").exists():
            if time.monotonic() > deadline:
                self.fail("fake Docker did not start")
            time.sleep(0.01)
        return process

    def test_lock_covers_pull_and_entire_container_run(self):
        for stage in ("pull", "run"):
            with self.subTest(stage=stage):
                for name in ("release", "started", "calls"):
                    (self.root / name).unlink(missing_ok=True)
                process = self.start_blocked(stage)
                before = (self.root / "calls").read_text()
                result = self.run_script()
                self.assertEqual(result.returncode, 0)
                self.assertIn("skipping", result.stderr)
                self.assertEqual((self.root / "calls").read_text(), before)
                (self.root / "release").touch()
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertFalse((self.root / ".collector.lock").exists())

    def test_failures_propagate_and_unlock(self):
        for env, expected in (({"PULL_STATUS": "7"}, ["version", "pull"]),
                              ({"RUN_STATUS": "7"}, ["version", "pull", "run"])):
            with self.subTest(env=env):
                (self.root / "calls").unlink(missing_ok=True)
                self.assertEqual(self.run_script(**env).returncode, 7)
                self.assertEqual((self.root / "calls").read_text().splitlines(), expected)
                self.assertFalse((self.root / ".collector.lock").exists())

    def test_legacy_compose_fallback(self):
        result = self.run_script(VERSION_STATUS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["version", "pull", "run"])

    def test_termination_leaves_lock_until_operator_checks_container(self):
        process = self.start_blocked("run")
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue((self.root / ".collector.lock").is_dir())
        self.assertIn("skipping", self.run_script().stderr)

    def test_missing_compose_fails_and_unlocks(self):
        (self.bin / "docker-compose").unlink()
        result = self.run_script(VERSION_STATUS="1")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / ".collector.lock").exists())

    def test_lock_path_error_is_not_successful_skip(self):
        (self.root / ".collector.lock").write_text("not a lock directory")
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "calls").exists())


if __name__ == "__main__":
    unittest.main()
