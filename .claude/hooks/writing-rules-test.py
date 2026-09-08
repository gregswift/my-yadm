#!/usr/bin/env python3
"""Regression suite for writing-rules.py.

Runs the hook as a subprocess against crafted commands, so the entry point, the
exit code and the stderr text are all exercised together. A throwaway git repo
stands in for the working tree, because several checks read the staged diff.

Run: python3 ~/.claude/hooks/writing-rules-test.py
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "writing-rules.py")
BLOCK = 2
PASS = 0

REPO = None
FAILURES = []


def setup():
    """A repo with one commit, so staged_diff and git grep can both answer."""
    global REPO
    REPO = tempfile.mkdtemp(prefix="writing-rules-test-")
    subprocess.run(["git", "init", "-q", REPO], check=True)
    with open(os.path.join(REPO, "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("hello\n")
    subprocess.run(["git", "-C", REPO, "add", "."], check=True)
    subprocess.run(["git", "-C", REPO, "-c", "commit.gpgsign=false",
                    "-c", "user.email=test@example.com", "-c", "user.name=test",
                    "commit", "-qm", "init"], check=True)


def run(command):
    """(exit code, stderr) for one hook invocation."""
    payload = json.dumps({"tool_input": {"command": command}, "cwd": REPO})
    result = subprocess.run([sys.executable, HOOK], input=payload,
                            capture_output=True, text=True)
    return result.returncode, result.stderr.strip()


def gh(subcommand, body):
    """A gh command carrying body. shlex.quote, because json.dumps is not a
    shell quoter and leaves \\n as two characters the hook then reads as text."""
    return "gh %s --body %s" % (subcommand, shlex.quote(body))


def commit(message):
    return "git commit -m %s" % shlex.quote(message)


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name)
    if not condition:
        FAILURES.append(name)
        for line in detail.split("\n")[:6]:
            print("        " + line)


BULLETS = ["validates the branch name", "checks the remote tag",
           "reports the drift", "writes the log", "skips the cache",
           "emits the summary"]
RUN_ON = ("a single bullet that runs on for well over thirty words about "
          "several unrelated things at once which is exactly the shape that "
          "this particular rule is meant to catch on every single run")
NOTES = "Looks good.\n\n## Notes\n\n- we should probably revisit the cache\n"
COINED = "We need a branch-catchup step in the gate.\n"


def test_lists():
    """A list item is its own unit. A run of them is not one long sentence."""
    body = "## What\n\nAdds the gate.\n\n" + "\n".join("- " + b for b in BULLETS)
    code, err = run(gh("pr create --title x", body))
    check("six-bullet body passes", code == PASS, err)

    code, err = run(gh("pr create --title x", "Fix.\n\n- " + RUN_ON + "\n"))
    check("run-on bullet blocks", "sentence-length" in err, err)


def test_dash_location():
    code, err = run(gh("pr create --title x", "Line one.\n\nA real — dash here.\n"))
    check("dash names its line", "line 3" in err and "em dash" in err, err)
    check("dash message clears the hyphen", "ASCII" in err, err)

    code, err = run(commit("feat: x\n\nA real — dash.\n"))
    # The subject is stripped before the checks run, so the body's line 2 is the
    # writer's line 3.
    check("commit dash counts the subject", code == BLOCK and "line 3" in err, err)


def test_artifact_names():
    long_body = "\n".join("Line %d." % n for n in range(45))
    for subcommand, rule, label in (
            ("pr create --title x", "pr-body-length", "pull request body"),
            ("issue create --title x", "issue-body-length", "issue body"),
            ("issue comment 3", "comment-body-length", "this comment")):
        code, err = run(gh(subcommand, long_body))
        check("%s reports %s" % (label.replace("this ", ""), rule),
              rule in err and label in err, err)


def test_limits():
    spaced = "\n\n".join("Line %d." % n for n in range(35))
    code, err = run(gh("pr create --title x", spaced))
    check("blank lines are free", code == PASS, err)

    code, err = run(gh("pr comment 3", "\n".join("Line %d." % n for n in range(29))))
    check("29-line comment passes", code == PASS, err)

    code, err = run(gh("pr comment 3", "\n".join("Line %d." % n for n in range(31))))
    check("31-line comment blocks", "comment-body-length" in err, err)

    fenced = ("Here.\n\n```\n" + "\n".join("x = %d" % n for n in range(40))
              + "\n```\n\nDone.")
    code, err = run(gh("pr comment 3", fenced))
    check("comment skips fenced code", code == PASS, err)


def test_rule_sets():
    """A comment is the discussion, and an issue has no diff behind it."""
    code, err = run(gh("pr comment 3", NOTES))
    check("comment escapes trailing-scope", "trailing-scope" not in err, err)
    code, err = run(gh("pr create --title x", NOTES))
    check("body keeps trailing-scope", "trailing-scope" in err, err)

    code, err = run(gh("issue create --title x", COINED))
    check("issue escapes coined-term", "coined-term" not in err, err)
    code, err = run(gh("pr create --title x", COINED))
    check("body keeps coined-term", "coined-term" in err, err)


def test_commit():
    code, err = run(commit("feat: add gate\n\nThe gate was silent, so a bad "
                           "branch reached CI.\n"))
    check("clean commit passes", code == PASS, err)

    code, err = run(commit("feat: x\n\n" + "y" * 120 + "\n"))
    check("body-line-length blocks", "body-line-length" in err, err)


def test_heredocs():
    """The form an agent actually writes. A heredoc reaches the hook unexpanded."""
    body = "## What\n\nAdds the gate.\n\n" + "\n".join("- " + b for b in BULLETS)
    code, err = run("gh pr create --title 'feat: gate' --body \"$(cat <<'EOF'\n"
                    + body + "\nEOF\n)\"")
    check("heredoc body passes", code == PASS, err)

    code, err = run("git commit -F- <<'EOF'\nfeat: add gate\n\nThe gate was "
                    "silent, so a bad branch reached CI.\nEOF")
    check("heredoc commit passes", code == PASS, err)


def test_ignored():
    check("gh pr view ignored", run("gh pr view 3")[0] == PASS)
    check("gh pr list ignored", run("gh pr list")[0] == PASS)
    check("plain ls ignored", run("ls -la")[0] == PASS)


def main():
    setup()
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print("\n%d failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
