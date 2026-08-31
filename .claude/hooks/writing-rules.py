#!/usr/bin/env python3
"""PreToolUse gate for commit messages and pull request bodies.

Reads a Claude Code hook payload on stdin. When the Bash command writes a
commit message or a pull request body, the text is checked against the writing
rules in ~/.claude/CLAUDE.md. Exit 2 blocks the command and returns the
findings. Any internal error exits 0, because a broken linter must not stop
work.
"""
import json
import os
import re
import sys

MAX_WORDS = 20
MAX_PR_LINES = 20

RULES = """Writing rules (~/.claude/CLAUDE.md):
- One idea per sentence, 20 words or fewer.
- No em dashes. Use a comma, a colon, or a second sentence.
- Never wrap a commit body or PR body to a shell width. One line per paragraph.
- A PR body is under 20 lines.
- One rationale per tier. Code says what, comments say why, the commit carries
  rationale, docs hold paragraphs. Do not repeat one explanation across tiers.
- No idioms, no figures of speech, no meta-narration about your own writing."""

HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1\s*\n(.*?)\n\s*\2\b", re.S)
FENCE = re.compile(r"^\s*```")
LIST_ITEM = re.compile(r"^\s*(?:[-*+>|#]|\d+[.)])\s")
URL = re.compile(r"https?://\S+")
CODE_SPAN = re.compile(r"`[^`]*`")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"'(])")


# git accepts global options before the subcommand, and some of them take a
# separate value token. Matching "git commit" as adjacent words misses the
# common `git -c key=value commit` and `git -C path commit` forms.
GIT_VALUE_OPTS = {"-c", "-C", "--exec-path", "--git-dir", "--work-tree",
                  "--namespace", "--config-env", "--super-prefix"}
SEPARATORS = {"&&", "||", ";", "|", "(", ")"}
PR_WRITES = [[a, b] for a in ("pr", "issue") for b in ("create", "edit", "comment")]


def positionals(tokens, program, limit=3):
    """Non-option tokens after each `program` token, one list per invocation."""
    found = []
    for index, token in enumerate(tokens):
        if token != program:
            continue
        words = []
        cursor = index + 1
        while cursor < len(tokens) and len(words) < limit:
            current = tokens[cursor]
            if current in SEPARATORS:
                break
            if current in GIT_VALUE_OPTS:
                cursor += 2
                continue
            if current.startswith("-"):
                cursor += 1
                continue
            words.append(current)
            cursor += 1
        found.append(words)
    return found


def heredocs(command):
    return [m.group(3) for m in HEREDOC.finditer(command)]


def read_file(path, cwd):
    if not path or path == "-":
        return None
    full = path if os.path.isabs(path) else os.path.join(cwd or "", path)
    try:
        with open(full, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def flag_values(tokens, names):
    """Collect values for `--name value`, `--name=value` and `-n value`."""
    found = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        for name in names:
            if token == name and index + 1 < len(tokens):
                found.append(tokens[index + 1])
                index += 1
                break
            if token.startswith(name + "="):
                found.append(token[len(name) + 1:])
                break
        index += 1
    return found


def extract(command, cwd):
    """Return (kind, body) where kind is 'commit', 'pr' or None."""
    import shlex

    bodies = heredocs(command)
    stripped = HEREDOC.sub(" ", command)
    try:
        tokens = shlex.split(stripped, comments=False)
    except ValueError:
        tokens = stripped.split()

    is_commit = any(w[:1] == ["commit"] for w in positionals(tokens, "git"))
    is_pr = any(w[:2] in PR_WRITES for w in positionals(tokens, "gh"))
    if not is_commit and not is_pr:
        return None, None

    inline = flag_values(tokens, ["-m", "--message"] if is_commit else ["-b", "--body"])
    files = flag_values(tokens, ["-F", "--file"] if is_commit else ["-F", "--body-file"])
    for path in files:
        content = read_file(path, cwd)
        if content:
            bodies.append(content)

    text = "\n\n".join(list(inline) + bodies).strip()
    if not text:
        return None, None
    return ("commit" if is_commit else "pr"), text


def body_of(kind, text):
    """A commit's first line is its subject and is exempt from the checks."""
    if kind != "commit":
        return text
    parts = text.split("\n", 1)
    return parts[1] if len(parts) > 1 else ""


def prose_lines(body):
    """Line numbers and text for lines that are prose, not code or lists."""
    out = []
    fenced = False
    for number, line in enumerate(body.split("\n"), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or line.startswith("    ") or LIST_ITEM.match(line):
            continue
        out.append((number, line))
    return out


def check_dashes(body):
    hits = [c for c in body if c in "—–"]
    if hits:
        return ["%d em or en dash. Use a comma, a colon, or a second sentence." % len(hits)]
    return []


def check_wrapping(body):
    lines = dict(prose_lines(body))
    for number, line in sorted(lines.items()):
        text = line.rstrip()
        following = lines.get(number + 1, "").strip()
        if not (50 <= len(text) <= 95):
            continue
        if text[-1:] in ".!?:" or not following:
            continue
        return ["line %d is wrapped mid-sentence at %d characters. "
                "Write one line per paragraph." % (number, len(text))]
    return []


def check_sentences(body):
    text = re.sub(r"(?s)```.*?```", " ", body)
    text = URL.sub("URL", CODE_SPAN.sub("CODE", text))
    findings = []
    for chunk in text.split("\n\n"):
        for sentence in SENTENCE_SPLIT.split(chunk.strip()):
            words = sentence.split()
            if len(words) > MAX_WORDS:
                findings.append("%d words: %s" % (len(words), " ".join(words[:9]) + " ..."))
    return findings[:5]


def check_length(body):
    count = len(body.strip().split("\n"))
    if count > MAX_PR_LINES:
        return ["PR body is %d lines. The limit is %d." % (count, MAX_PR_LINES)]
    return []


def main():
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    cwd = payload.get("cwd") or os.getcwd()

    kind, text = extract(command, cwd)
    if not kind:
        return 0

    body = body_of(kind, text)
    if not body.strip():
        return 0

    findings = check_dashes(body) + check_wrapping(body) + check_sentences(body)
    if kind == "pr":
        findings += check_length(text)
    if not findings:
        return 0

    label = "commit message" if kind == "commit" else "pull request body"
    sys.stderr.write("Blocked: this %s breaks the writing rules.\n\n" % label)
    for finding in findings:
        sys.stderr.write("  - %s\n" % finding)
    sys.stderr.write("\n%s\n\nRewrite the text and run the command again.\n" % RULES)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
