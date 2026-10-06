#!/usr/bin/env python3
"""
Daily provenance run for www.locafy.com.

1. Fetch https://www.locafy.com/okf/manifest.json (SHA-256 + size of every
   file in Locafy's OKF knowledge bundle, hashed over the exact served bytes).
2. Save the full manifest as manifests/YYYY-MM-DD.json and append what changed
   since the last run to ledger/YYYY-MM-DD.jsonl (added / changed / removed).
3. Re-fetch a sample of files and confirm their bytes still hash to the
   manifest value (catches a manifest that disagrees with what is served).
4. OpenTimestamps: stamp today's manifest and ledger; try to upgrade every
   still-pending proof from earlier days.
5. Internet Archive: queue changed URLs and submit up to ARCHIVE_LIMIT per run
   to Save Page Now, logging each result in archive/YYYY-MM-DD.jsonl.

Stdlib only, plus the `ots` CLI (opentimestamps-client). Safe to re-run: a
second run on the same day replaces that day's manifest snapshot and appends
to (never replaces) that day's ledger.
"""
import datetime as dt
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

MANIFEST_URL = "https://www.locafy.com/okf/manifest.json"
ARCHIVE_LIMIT = int(os.environ.get("ARCHIVE_LIMIT", "20"))
# Save Page Now can take minutes per URL; stop archiving after this many seconds
# so the run always reaches its commit step well inside the job timeout.
ARCHIVE_BUDGET_S = int(os.environ.get("ARCHIVE_BUDGET_S", "600"))
SAMPLE = int(os.environ.get("VERIFY_SAMPLE", "10"))
UA = "locafy-provenance/1 (+https://github.com/Locafy/locafy-provenance)"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def path(*parts):
    p = os.path.join(ROOT, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def write_json(p, value):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")


def main():
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    day = now.date().isoformat()
    stamp_at = now.isoformat().replace("+00:00", "Z")

    status, raw = fetch(MANIFEST_URL)
    if status != 200:
        sys.exit(f"manifest fetch failed: HTTP {status}")
    manifest = json.loads(raw)
    files = {f["path"]: f for f in manifest["files"]}

    # 3. spot-check served bytes against the manifest
    mismatches = []
    for f in random.sample(list(files.values()), min(SAMPLE, len(files))):
        _, body = fetch(f["url"])
        if hashlib.sha256(body).hexdigest() != f["sha256"]:
            mismatches.append(f["path"])
    # The manifest is regenerated per request; a post can publish between the two
    # fetches, so a mismatch is recorded rather than fatal.

    # 2. diff against the previous state
    state_p = path("state", "latest.json")
    prev = json.load(open(state_p)) if os.path.exists(state_p) else {}
    entries = []
    for p, f in sorted(files.items()):
        old = prev.get(p)
        if old is None or old != f["sha256"]:
            entries.append({"change": "added" if old is None else "changed", "path": p, "url": f["url"],
                            "sha256": f["sha256"], "bytes": f["bytes"], "observed_at": stamp_at})
    for p in sorted(set(prev) - set(files)):
        entries.append({"change": "removed", "path": p, "previous_sha256": prev[p], "observed_at": stamp_at})

    manifest_p = path("manifests", f"{day}.json")
    write_json(manifest_p, {"observed_at": stamp_at, "source": MANIFEST_URL, "spot_check_mismatches": mismatches, "manifest": manifest})
    # The ledger is append-only: a second run on the same day adds its changes
    # to that day's file instead of replacing what an earlier run recorded.
    ledger_p = path("ledger", f"{day}.jsonl")
    with open(ledger_p, "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    write_json(state_p, {p: f["sha256"] for p, f in files.items()})

    # 4. OpenTimestamps
    # Restamp only files whose bytes changed this run; an unchanged ledger keeps
    # its existing (possibly already Bitcoin-confirmed) proof.
    to_stamp = [manifest_p] + ([ledger_p] if entries or not os.path.exists(ledger_p + ".ots") else [])
    for target in to_stamp:
        if os.path.exists(target + ".ots"):
            os.remove(target + ".ots")
        subprocess.run(["ots", "stamp", target], check=True)
    pending = [os.path.relpath(t, ROOT) + ".ots" for t in to_stamp]  # fresh proofs always start pending
    for d in ("manifests", "ledger"):
        for name in sorted(os.listdir(os.path.join(ROOT, d))):
            if name.endswith(".ots") and not name.startswith(day):
                r = subprocess.run(["ots", "upgrade", os.path.join(ROOT, d, name)], capture_output=True, text=True)
                if r.returncode != 0:
                    pending.append(f"{d}/{name}")

    # 5. Internet Archive
    queue_p = path("archive", "queue.txt")
    queue = [l.strip() for l in open(queue_p)] if os.path.exists(queue_p) else []
    queue += [e["url"] for e in entries if e["change"] != "removed" and e["url"] not in queue]
    results, done, started = [], 0, time.monotonic()
    for url in list(queue):
        if done >= ARCHIVE_LIMIT or time.monotonic() - started > ARCHIVE_BUDGET_S:
            break
        try:
            req = urllib.request.Request("https://web.archive.org/save/" + url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                snapshot = r.headers.get("Content-Location") or r.geturl()
                results.append({"url": url, "status": r.status, "snapshot": snapshot, "at": stamp_at})
            queue.remove(url)
        except Exception as exc:  # keep it queued; retry tomorrow
            results.append({"url": url, "error": str(exc)[:200], "at": stamp_at})
        done += 1
        time.sleep(12)
    with open(queue_p, "w") as fh:
        fh.write("\n".join(queue) + ("\n" if queue else ""))
    with open(path("archive", f"{day}.jsonl"), "a", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, sort_keys=True) + "\n")

    summary = {"day": day, "files": len(files), "changes": len(entries), "spot_check_mismatches": mismatches,
               "pending_proofs": pending, "archived": sum(1 for r in results if "error" not in r),
               "archive_errors": sum(1 for r in results if "error" in r), "archive_queue": len(queue)}
    write_json(path("state", "last-run.json"), summary)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
