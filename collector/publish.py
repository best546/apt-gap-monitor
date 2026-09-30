from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
FILES = ("docs/data/latest.json", "docs/data/history.json")
HISTORY_LIMIT = 104


def timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    return stamp


def snapshot(latest: dict) -> dict:
    timestamp(latest["generated_at"])
    items = latest["items"]
    ids = [item["id"] for item in items]
    if not ids or len(ids) != len(set(ids)) or latest["reference_id"] not in ids:
        raise ValueError("latest must contain unique IDs and its reference_id")
    return {
        "generated_at": latest["generated_at"],
        "values": {item["id"]: {"price": item["representative_manwon"], "gap": item["gap_manwon"]}
                   for item in items},
    }


def validate_history(history: list) -> None:
    if not isinstance(history, list):
        raise ValueError("history must be a list")
    previous = None
    for entry in history:
        stamp = timestamp(entry["generated_at"])
        if previous is not None and stamp <= previous:
            raise ValueError("history timestamps must be unique and increasing")
        if not isinstance(entry["values"], dict) or not entry["values"]:
            raise ValueError("history values must be a nonempty object")
        for value in entry["values"].values():
            if set(value) != {"price", "gap"}:
                raise ValueError("history values must contain price and gap")
        previous = stamp


def validate_pair(latest: dict, history: list) -> dict:
    current = snapshot(latest)
    validate_history(history)
    if not history or history[-1] != current:
        raise ValueError("latest and the final history snapshot do not match")
    return current


def validate_config(latest: dict, config: dict) -> None:
    expected_ids = [item["id"] for item in config["complexes"]]
    actual_ids = [item["id"] for item in latest["items"]]
    if actual_ids != expected_ids or latest["reference_id"] != config["reference_id"]:
        raise RuntimeError("Refusing stale upload: configured IDs/reference differ")
    reference_id = config["reference_id"]
    expected_home = next(item for item in config["complexes"] if item["id"] == reference_id)
    actual_home = next(item for item in latest["items"] if item["id"] == reference_id)
    if actual_home.get("area") != expected_home.get("area"):
        raise RuntimeError("Refusing stale upload: home area differs")


def read_remote(session: requests.Session, base: str, path: str, head: str):
    # latest.json already exceeds 1 MiB; the default Contents response omits
    # base64 content at that size. Raw media supports files up to 100 MiB.
    response = session.get(f"{base}/contents/{path}", params={"ref": head},
                           headers={"Accept": "application/vnd.github.raw+json"}, timeout=30)
    response.raise_for_status()
    return response.json()


def publish(session: requests.Session, repo: str, branch: str, root: Path = ROOT,
            max_attempts: int = 3) -> bool:
    latest, local_history = [json.loads((root / path).read_text(encoding="utf-8")) for path in FILES]
    current = validate_pair(latest, local_history)
    base = f"https://api.github.com/repos/{repo}"
    ref = f"heads/{quote(branch, safe='/')}"

    for _ in range(max_attempts):
        response = session.get(f"{base}/git/ref/{ref}", timeout=30)
        response.raise_for_status()
        head = response.json()["object"]["sha"]
        # Pin all reads to one commit, including every retry after a competing update.
        config = read_remote(session, base, "config/complexes.json", head)
        validate_config(latest, config)
        remote_latest = read_remote(session, base, FILES[0], head)
        history = read_remote(session, base, FILES[1], head)
        validate_pair(remote_latest, history)
        remote_stamp = timestamp(remote_latest["generated_at"])
        local_stamp = timestamp(latest["generated_at"])
        if local_stamp < remote_stamp:
            raise RuntimeError("Refusing stale upload: remote data is newer")
        if local_stamp == remote_stamp:
            if latest == remote_latest:
                print("Data already published; no commit needed")
                return False
            raise RuntimeError("Refusing conflicting data with the same generated_at")

        # The remote history is authoritative. Never replace it with a NAS cache.
        merged_history = (history + [current])[-HISTORY_LIMIT:]
        validate_pair(latest, merged_history)
        response = session.get(f"{base}/git/commits/{head}", timeout=30)
        response.raise_for_status()
        base_tree = response.json()["tree"]["sha"]
        entries = [
            {"path": path, "mode": "100644", "type": "blob",
             "content": json.dumps(data, ensure_ascii=False, indent=2)}
            for path, data in zip(FILES, (latest, merged_history))
        ]
        response = session.post(f"{base}/git/trees", json={"base_tree": base_tree, "tree": entries}, timeout=30)
        response.raise_for_status()
        tree = response.json()["sha"]
        response = session.post(f"{base}/git/commits", json={
            "message": "data: update apartment trades from Synology NAS",
            "tree": tree, "parents": [head],
        }, timeout=30)
        response.raise_for_status()
        commit = response.json()["sha"]
        # Only this operation makes either file visible on the branch. A competing
        # commit makes this a non-fast-forward update, which GitHub must reject.
        response = session.patch(f"{base}/git/refs/{ref}", json={"sha": commit, "force": False}, timeout=30)
        if response.status_code in (409, 422):
            check = session.get(f"{base}/git/ref/{ref}", timeout=30)
            check.raise_for_status()
            if check.json()["object"]["sha"] != head:
                continue
        response.raise_for_status()
        print(f"Published latest.json and history.json in one commit: {commit}")
        return True
    raise RuntimeError(f"Branch kept changing; publication aborted after {max_attempts} attempts")


def main() -> None:
    token = (os.environ.get("GITHUB_DATA_TOKEN") or "").strip()
    repo = (os.environ.get("GITHUB_REPOSITORY") or "best546/apt-gap-monitor").strip()
    branch = (os.environ.get("GITHUB_BRANCH") or "main").strip()
    if not token:
        raise SystemExit("GITHUB_DATA_TOKEN is required")
    with requests.Session() as session:
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        publish(session, repo, branch)


if __name__ == "__main__":
    main()
