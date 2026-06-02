#!/usr/bin/env python3
"""
Seed all council districts from the Raw PDF folder.
Uploads each PDF in order (cd1→cd15), waits for indexing to finish before moving on.

Usage:
    python3 seed_all.py
    python3 seed_all.py cd6 cd7 cd8   # seed specific districts only
"""

import sys
import time
import requests
from pathlib import Path

API_URL = "https://cmem-convo-production.up.railway.app"
PDF_DIR = Path("/Users/rehaananjaria/Visic/CmemFilesReports/Raw")
POLL_INTERVAL = 20  # seconds between status checks
POLL_TIMEOUT  = 90  # seconds to wait for a single status HTTP response
MAX_RESUMES   = 5   # times to re-trigger a seed after a lost job (OOM restart)

MEMBERS = {
    "cd1":  ("Eunisses Hernandez",      "Council District 1"),
    "cd2":  ("Paul Krekorian",          "Council District 2"),
    "cd3":  ("Bob Blumenfield",         "Council District 3"),
    "cd4":  ("Nithya Raman",            "Council District 4"),
    "cd5":  ("Katy Yaroslavsky",        "Council District 5"),
    "cd6":  ("Imelda Padilla",          "Council District 6"),
    "cd7":  ("Monica Rodriguez",        "Council District 7"),
    "cd8":  ("Marqueece Harris-Dawson", "Council District 8"),
    "cd9":  ("Curren D. Price",         "Council District 9"),
    "cd10": ("Heather Hutt",            "Council District 10"),
    "cd11": ("Traci Park",              "Council District 11"),
    "cd12": ("John Lee",                "Council District 12"),
    "cd13": ("Hugo Soto-Martinez",      "Council District 13"),
    "cd14": ("Kevin De Leon",           "Council District 14"),
    "cd15": ("Tim McCosker",            "Council District 15"),
}


def trigger_resume(member_id: str) -> bool:
    """Re-trigger indexing via /reseed after a job was lost (OOM restart).
    Reseed reads the file list from the registry and, since the collection is
    no longer wiped server-side, resumes from the already-indexed files.
    Returns True if a job is running again (newly started or already in flight)."""
    try:
        resp = requests.post(f"{API_URL}/api/members/{member_id}/reseed", timeout=120)
    except requests.RequestException as e:
        print(f"\n  Resume request failed: {e}")
        return False
    if resp.status_code == 409:
        return True  # a job is already running again — just keep polling
    if resp.status_code not in (200, 201):
        print(f"\n  Resume failed: {resp.status_code} {resp.text}")
        return False
    print(f"\n  Resumed via reseed (job {resp.json().get('job_id')})")
    return True


def poll_until_done(member_id: str) -> bool:
    print(f"  Waiting for indexing", end="", flush=True)
    resumes = 0
    while True:
        try:
            resp = requests.get(
                f"{API_URL}/api/members/{member_id}/status", timeout=POLL_TIMEOUT
            )
        except requests.RequestException as e:
            print(f"\n  Poll timeout/error (retrying): {e}")
            time.sleep(POLL_INTERVAL)
            continue

        if resp.status_code == 404:
            # Job state lost — container restarted (likely OOM). Resume via reseed.
            if resumes >= MAX_RESUMES:
                print(f"\n  Gave up after {MAX_RESUMES} resume attempts")
                return False
            resumes += 1
            print(f"\n  No job found (container restarted?) — resuming [{resumes}/{MAX_RESUMES}]")
            if not trigger_resume(member_id):
                return False
            time.sleep(POLL_INTERVAL)
            continue

        if resp.status_code != 200:
            # Transient during restart (502/503) — wait for the container to recover.
            print(f"\n  Status check returned {resp.status_code} (retrying)")
            time.sleep(POLL_INTERVAL)
            continue

        data = resp.json()
        status = data.get("status", "")
        message = data.get("message", "")

        if status == "done":
            print(f"\n  Done: {message}")
            return True
        elif status == "error":
            print(f"\n  Error: {message}")
            return False
        else:
            print(".", end="", flush=True)
            time.sleep(POLL_INTERVAL)


def seed(member_id: str, name: str, district: str, pdf_path: Path) -> bool:
    print(f"\n{'='*60}")
    print(f"[{member_id}] {name} — {district}")
    print(f"  Uploading {pdf_path.name} ({pdf_path.stat().st_size // 1024}KB)...")

    try:
        with open(pdf_path, "rb") as f:
            resp = requests.post(
                f"{API_URL}/api/members",
                data={"member_id": member_id, "name": name, "district": district},
                files={"pdf": (pdf_path.name, f, "application/pdf")},
                timeout=120,
            )
    except requests.RequestException as e:
        print(f"  Upload failed: {e}")
        return False

    if resp.status_code == 409:
        print(f"  Already seeding — waiting for existing job to finish")
        return poll_until_done(member_id)

    if resp.status_code not in (200, 201):
        print(f"  Upload failed: {resp.status_code} {resp.text}")
        return False

    job_id = resp.json().get("job_id")
    print(f"  Job started: {job_id}")
    return poll_until_done(member_id)


def main():
    if sys.argv[1:]:
        targets = [a.lower() for a in sys.argv[1:]]
        unknown = [t for t in targets if t not in MEMBERS]
        if unknown:
            print(f"Unknown district(s): {', '.join(unknown)}")
            sys.exit(1)
    else:
        targets = sorted(MEMBERS.keys(), key=lambda x: int(x[2:]))

    print(f"Seeding {len(targets)} district(s): {', '.join(targets)}")
    print(f"API: {API_URL}")
    print(f"PDFs: {PDF_DIR}")

    failed = []
    for member_id in targets:
        pdf_path = PDF_DIR / f"{member_id}.pdf"
        if not pdf_path.exists():
            print(f"\n[{member_id}] PDF not found at {pdf_path} — skipping")
            failed.append(member_id)
            continue

        name, district = MEMBERS[member_id]
        ok = seed(member_id, name, district, pdf_path)
        if not ok:
            print(f"  [{member_id}] FAILED — continuing to next district")
            failed.append(member_id)

    print(f"\n{'='*60}")
    seeded = len(targets) - len(failed)
    print(f"Complete: {seeded}/{len(targets)} districts seeded successfully")
    if failed:
        print(f"Failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
