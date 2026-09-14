from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
FILES = ("docs/data/latest.json", "docs/data/history.json")


def validate_latest_against_remote_config(session: requests.Session, repo: str, branch: str) -> None:
    url = f"https://api.github.com/repos/{repo}/contents/config/complexes.json"
    response = session.get(url, params={"ref": branch}, timeout=30)
    response.raise_for_status()
    config = json.loads(base64.b64decode(response.json()["content"]).decode("utf-8"))
    latest = json.loads((ROOT / "docs/data/latest.json").read_text(encoding="utf-8"))

    expected_ids = [item["id"] for item in config["complexes"]]
    actual_ids = [item["id"] for item in latest["items"]]
    if actual_ids != expected_ids:
        raise RuntimeError(
            f"Refusing stale upload: expected {len(expected_ids)} configured complexes, got {len(actual_ids)}"
        )

    reference_id = config["reference_id"]
    expected_home = next(item for item in config["complexes"] if item["id"] == reference_id)
    actual_home = next(item for item in latest["items"] if item["id"] == reference_id)
    if actual_home.get("area") != expected_home.get("area"):
        raise RuntimeError(
            f"Refusing stale upload: home area {actual_home.get('area')} != {expected_home.get('area')}"
        )
    print(f"Validated latest data against GitHub config: {len(actual_ids)} complexes")



def publish_file(session: requests.Session, repo: str, branch: str, path: str) -> None:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    current = session.get(url, params={"ref": branch}, timeout=30)
    current.raise_for_status()
    sha = current.json()["sha"]
    raw = (ROOT / path).read_bytes()
    payload = {
        "message": f"data: update {Path(path).name} from Synology NAS",
        "content": base64.b64encode(raw).decode("ascii"),
        "sha": sha,
        "branch": branch,
    }
    response = session.put(url, json=payload, timeout=30)
    response.raise_for_status()
    print(f"Published {path}")


def main() -> None:
    token = (os.environ.get("GITHUB_DATA_TOKEN") or "").strip()
    repo = (os.environ.get("GITHUB_REPOSITORY") or "best546/apt-gap-monitor").strip()
    branch = (os.environ.get("GITHUB_BRANCH") or "main").strip()
    if not token:
        raise SystemExit("GITHUB_DATA_TOKEN is required")
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    validate_latest_against_remote_config(session, repo, branch)
    for path in FILES:
        publish_file(session, repo, branch, path)


if __name__ == "__main__":
    main()
