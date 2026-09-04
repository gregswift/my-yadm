#!/usr/bin/env python3
"""SessionStart hook. Surfaces writing-rules bypass attempts in a new session.

A bypass attempt blocks the session that makes it, but nothing tells the other
sessions or the next one. This reads the log and reports attempts that have not
been reported yet, then moves the marker. Any internal error exits 0.
"""
import json
import os
import sys

LOG_FILE = os.path.expanduser("~/.claude/hooks/writing-rules.log")
MARK_FILE = os.path.expanduser("~/.claude/hooks/writing-rules.reported")


def main():
    try:
        with open(MARK_FILE, encoding="utf-8") as handle:
            since = handle.read().strip()
    except OSError:
        since = ""

    rows = []
    try:
        with open(LOG_FILE, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("attempted") and row.get("ts", "") > since:
                    rows.append(row)
    except OSError:
        return 0

    if rows:
        rules = sorted({r for row in rows for r in row["attempted"]})
        places = sorted({row["cwd"] for row in rows})
        print("WRITING-RULES ALERT: %d bypass attempt(s) since the last report."
              % len(rows))
        print("Rules named: %s" % ", ".join(rules))
        for place in places[:5]:
            print("  %s" % place)
        print("WRITING_RULES_ALLOW is not honoured, so no rule was actually skipped.")
        print("Report this to Greg before doing anything else in this session.")

    latest = max((row.get("ts", "") for row in rows), default="")
    if latest:
        try:
            with open(MARK_FILE, "w", encoding="utf-8") as handle:
                handle.write(latest)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
