#!/usr/bin/env python3
"""Fetch Go modules seen for the first time in the module index.

The Go module index at index.golang.org is a feed of module versions, not of
new modules, and it sees several thousand versions per hour. To keep the list
to genuinely new modules, every module path the feed has shown is remembered
in a local SQLite store; only paths that were never seen before are listed.
The first run seeds the store by scanning the feed's recent history without
listing anything, and resumes that scan on later runs if it does not finish.
"""

import argparse
import csv
import datetime as dt
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INDEX_URL = "https://index.golang.org/index"
DEFAULT_USER_AGENT = "new-go-modules/1.0 (https://github.com/GHLists/new-go-modules)"
DEFAULT_STATE_DB = "~/.cache/new-go-modules/seen-modules.sqlite3"

PAGE_SIZE = 2000
MAX_PAGES = 20
BOOTSTRAP_DAYS = 7
BOOTSTRAP_PAGES = 800
CSV_HEADER = ("first_seen_at", "module", "version")

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    http.client.HTTPException,
    OSError,
)


def iso(moment):
    moment = moment.astimezone(dt.timezone.utc)
    if moment.microsecond:
        fraction = f"{moment.microsecond:06d}".rstrip("0")
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def timestamp_filename(moment):
    moment = moment.astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H-%M-%S")
    if moment.microsecond:
        stamp += "-" + f"{moment.microsecond:06d}".rstrip("0")
    return stamp + "Z"


def decode_module(path):
    """Turn the index's case-escaped module path into the canonical one."""
    return re.sub(r"!([a-z])", lambda match: match.group(1).upper(), path)


def fetch_text(url, user_agent, retries=3, backoff=5.0):
    last_error = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except TRANSIENT_ERRORS as error:
            last_error = error
        if attempt < retries:
            print(f"attempt {attempt} failed ({last_error}), retrying", file=sys.stderr)
            time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_page(cursor, user_agent, retries):
    """Return one page of index entries at or after ``cursor``."""
    url = f"{INDEX_URL}?since={urllib.parse.quote(iso(cursor))}&limit={PAGE_SIZE}"
    text = fetch_text(url, user_agent, retries=retries)
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("Go module index returned an invalid line") from error
        if not isinstance(entry, dict):
            raise RuntimeError("Go module index entry is not an object")
        entries.append(entry)
    return entries


def fetch_pages(start, stop, user_agent, retries, max_pages):
    """Walk the feed from ``start`` toward ``stop``.

    Returns the entries, the timestamp of the last entry read and whether the
    walk reached ``stop`` (or the end of the feed) instead of the page limit.
    """
    entries = []
    cursor = start
    for _ in range(max_pages):
        batch = fetch_page(cursor, user_agent, retries)
        if not batch:
            return entries, cursor, True
        entries.extend(batch)
        try:
            last = max(parse_timestamp(entry["Timestamp"]) for entry in batch)
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Go module index contains an invalid Timestamp") from error
        if last <= cursor:
            raise RuntimeError("Go module index cursor did not advance")
        cursor = last
        if len(batch) < PAGE_SIZE or cursor >= stop:
            return entries, cursor, True
    return entries, cursor, False


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy.

    The workflow checks out only ``scripts`` from the repository, so the
    manifest can be missing from the working tree even though it is committed.
    """
    manifest_path = Path(path)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path.as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def load_manifest(path):
    text = read_manifest_text(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"manifest {path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"manifest {path} must contain a JSON object")
    version = data.get("state_version", 1)
    if version != 1:
        raise RuntimeError(f"manifest {path} has an unsupported state version")
    return data


def save_manifest(path, manifest):
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, manifest_path)


def open_seen_store(path):
    """Open the local store of module paths seen by earlier scans."""
    store_path = Path(path).expanduser()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(store_path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS modules (path TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    connection.commit()
    return connection


def select_unseen(connection, names):
    """Return the subset of names that earlier scans have not seen."""
    unseen = []
    for start in range(0, len(names), 500):
        chunk = names[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = connection.execute(
            f"SELECT path FROM modules WHERE path IN ({placeholders})", chunk
        )
        seen = {row[0] for row in rows}
        unseen.extend(name for name in chunk if name not in seen)
    return unseen


def remember_seen(connection, names):
    connection.executemany(
        "INSERT OR IGNORE INTO modules (path) VALUES (?)",
        ((name,) for name in names),
    )
    connection.commit()


def collect_entries(entries):
    """Return (timestamp, path, version) for every valid index entry."""
    collected = []
    for entry in entries:
        path = entry.get("Path")
        version = entry.get("Version")
        if not isinstance(path, str) or not path:
            continue
        if not isinstance(version, str) or not version:
            continue
        try:
            moment = parse_timestamp(entry["Timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        collected.append((moment, path, version))
    return collected


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC start timestamp as ISO 8601 (default: end of the last list)",
    )
    parser.add_argument(
        "--until",
        help="UTC end timestamp as ISO 8601 (default: now)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_PAGES,
        help=f"maximum feed pages per run (default: {MAX_PAGES})",
    )
    parser.add_argument(
        "--bootstrap-days",
        type=float,
        default=BOOTSTRAP_DAYS,
        help=f"index history to seed the seen store with (default: {BOOTSTRAP_DAYS})",
    )
    parser.add_argument(
        "--bootstrap-pages",
        type=int,
        default=BOOTSTRAP_PAGES,
        help=f"maximum feed pages per bootstrap run (default: {BOOTSTRAP_PAGES})",
    )
    parser.add_argument(
        "--state-db",
        default=DEFAULT_STATE_DB,
        help="SQLite store of module paths seen by earlier scans "
        "(default: ~/.cache/new-go-modules/seen-modules.sqlite3)",
    )
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=1.0,
        help="window length when no previous list exists (default: 1)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    until = parse_timestamp(args.until) if args.until else now
    manifest = load_manifest(args.manifest)
    store = open_seen_store(args.state_db)

    try:
        if not manifest.get("bootstrapped"):
            if "bootstrap_cursor" in manifest:
                start = parse_timestamp(manifest["bootstrap_cursor"])
            else:
                start = until - dt.timedelta(days=args.bootstrap_days)
            entries, last, exhausted = fetch_pages(
                start, until, args.user_agent, args.retries, max(1, args.bootstrap_pages)
            )
            paths = [path for _moment, path, _version in collect_entries(entries)]
            remember_seen(store, paths)
            print(
                f"bootstrap scanned {len(entries)} index entries from {iso(start)} "
                f"to {iso(last)}; {len(paths)} module paths remembered"
            )
            if exhausted:
                manifest["bootstrapped"] = True
                manifest.pop("bootstrap_cursor", None)
                manifest["cursor"] = iso(last)
                save_manifest(args.manifest, manifest)
                print("bootstrap complete; the next run will start listing new modules")
                return 0
            manifest["bootstrap_cursor"] = iso(last)
            save_manifest(args.manifest, manifest)
            print(
                "bootstrap reached its page limit; it resumes on the next run",
                file=sys.stderr,
            )
            return 0

        if args.since:
            since = parse_timestamp(args.since)
            if "window" in manifest:
                stored_window = parse_timestamp(manifest["window"])
                if since < stored_window:
                    raise RuntimeError(
                        "backfill would move the window backwards; "
                        f"the manifest window is {iso(stored_window)}"
                    )
        elif "window" in manifest:
            since = parse_timestamp(manifest["window"])
        else:
            since = until - dt.timedelta(hours=args.lookback_hours)

        if "cursor" in manifest:
            cursor = parse_timestamp(manifest["cursor"])
        else:
            cursor = since

        if since >= until:
            print(f"nothing to do ({iso(since)} >= {iso(until)})", file=sys.stderr)
            return 0

        entries, last, exhausted = fetch_pages(
            cursor, until, args.user_agent, args.retries, max(1, args.max_pages)
        )
        collected = collect_entries(entries)
        window_end = last if last < until else until
        if window_end < since:
            window_end = since

        first_seen = {}
        for moment, path, version in collected:
            if path in first_seen:
                continue
            first_seen[path] = (moment, version)

        candidates = sorted(first_seen)
        unseen = set(select_unseen(store, candidates))
        rows = []
        for path in candidates:
            moment, version = first_seen[path]
            if moment <= since or moment > window_end:
                continue
            if path not in unseen:
                continue
            rows.append(
                {
                    "first_seen_at": iso(moment),
                    "module": decode_module(path),
                    "version": version,
                }
            )
        rows.sort(key=lambda row: row["first_seen_at"])
        remember_seen(store, candidates)
        print(
            f"scanned {len(collected)} entries in {iso(cursor)}..{iso(last)}; "
            f"{len(candidates)} module paths, {len(rows)} new"
        )

        manifest["window"] = iso(window_end)
        manifest["cursor"] = iso(window_end)
        manifest["source_truncated"] = not exhausted
        if rows:
            output = (
                Path(args.output_dir) / f"new-modules-{timestamp_filename(window_end)}.csv"
            )
            write_csv(output, rows)
            manifest["list"] = {
                "path": output.as_posix(),
                "from": iso(since),
                "to": iso(window_end),
                "count": len(rows),
            }
            print(
                f"wrote {len(rows)} new modules between {iso(since)} "
                f"and {iso(window_end)} to {output}"
            )
        else:
            print(f"no new modules between {iso(since)} and {iso(window_end)}")
        if not exhausted:
            print(
                "feed reached its page limit; the next run continues from here",
                file=sys.stderr,
            )
        save_manifest(args.manifest, manifest)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
