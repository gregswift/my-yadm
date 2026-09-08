#!/usr/bin/env python3
"""PreToolUse gate for commit messages, GitHub bodies and GitHub comments.

Reads a Claude Code hook payload on stdin. When the Bash command writes a
commit message, a pull request or issue body, or a pull request or issue
comment, the text is checked against the writing rules in
~/.claude/skills/writing-standard/. Blocking findings exit 2 and
return the findings to Claude. Warning findings exit 0 and print to stderr.
Any internal error exits 0, because a broken linter must not stop work.

WRITING_RULES_ALLOW is not honored. Setting it blocks the command and is
recorded, because an agent can set an environment variable and a gate that any
caller can switch off is not a gate. A skip is taken from
~/.claude/hooks/writing-rules.override, which Greg writes by hand and which is
consumed on first use.
"""
import json
import os
import re
import sys

MAX_WORDS = 20
BLOCK_WORDS = 30
MAX_BODY_LINES = 40
MAX_COMMENT_LINES = 30
MAX_BODY_LINE = 100
MAX_CODE_COMMENT_SENTENCES = 3
LOG_FILE = os.path.expanduser("~/.claude/hooks/writing-rules.log")
OVERRIDE_FILE = os.path.expanduser("~/.claude/hooks/writing-rules.override")

# Rules not listed here block. A warning prints and exits 0, so a heuristic
# check can be observed for a week before it is allowed to stop a commit.
WARN_ONLY = frozenset({
    "code-comment-rationale",
    "code-comment-length",
    "coined-term",
    "verbed-noun",
    "trailing-scope",
    "out-of-scope-pointer",
    "ellipsis",
    "sentence-long",
})

RULES = """Writing rules (~/.claude/skills/writing-standard/):
- One idea per sentence, 20 words or fewer.
- No em dashes. Use a comma, a colon, or a second sentence.
- Wrap commit bodies at 100 characters (commitlint body-max-line-length).
- A GitHub body is not a commit: never wrapped, one line per paragraph.
- A pull request or issue body is under 40 non-blank lines. A comment is under
  30, and its fenced code does not count.
- One rationale per tier. Code says what, comments say why, the commit carries
  rationale, docs hold paragraphs. Do not repeat one explanation across tiers.
- A code comment is 3 sentences at most. Longer belongs in a doc with a pointer.
- Never coin a name. A real name appears in the diff, not only in the prose.
- No idioms, no figures of speech, no meta-narration about your own writing.
- Never close on work that should have been settled before writing."""

HEREDOC = re.compile(r"(?:>\s*(\S+)\s*)?<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2\s*\n"
                     r"(.*?)\n\s*\3\b", re.S)
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
# A comment is the discussion, a body is its outcome, and an issue has no diff
# standing behind it. The three take different rule sets, so the noun and the
# subcommand are both kept instead of collapsing into one GitHub kind.
GH_NOUNS = ("pr", "issue")
GH_WRITES = ("create", "edit", "comment")

LABELS = {
    "commit": "commit message",
    "pr": "pull request body",
    "issue": "issue body",
    "comment": "comment",
}
LENGTH_RULE = {
    "pr": "pr-body-length",
    "issue": "issue-body-length",
    "comment": "comment-body-length",
}


def github_kind(words):
    """'pr', 'issue' or 'comment' for a gh invocation that writes prose."""
    if words[:2] and words[0] in GH_NOUNS and words[1] in GH_WRITES:
        return "comment" if words[1] == "comment" else words[0]
    return None


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
    """(redirect target, body) for each heredoc. Target is None when unredirected."""
    return [(m.group(1), m.group(4)) for m in HEREDOC.finditer(command)]


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


# yadm wraps git for dotfiles, so a commit it makes has to be read against
# yadm's index rather than the one in the current directory.
VCS = ("git", "yadm")


def extract(command, cwd):
    """Return (kind, body, program).

    kind is 'commit', 'pr', 'issue', 'comment', 'unreadable' or None."""
    import shlex

    docs = heredocs(command)
    stripped = HEREDOC.sub(" ", command)
    try:
        tokens = shlex.split(stripped, comments=False)
    except ValueError:
        tokens = stripped.split()

    program = next((p for p in VCS
                    if any(w[:1] == ["commit"] for w in positionals(tokens, p))), None)
    gh_kind = next((k for k in map(github_kind, positionals(tokens, "gh")) if k), None)
    if not program and not gh_kind:
        return None, None, None

    inline = flag_values(tokens, ["-m", "--message"] if program else ["-b", "--body"])
    files = flag_values(tokens, ["-F", "--file"] if program else ["-F", "--body-file"])

    named = [p for p in files if p and p != "-"]
    # A command can carry heredocs that are not the message, such as a script it
    # writes first. Checking those as prose reports sentences nobody wrote.
    if named:
        wanted = {os.path.basename(p) for p in named}
        bodies = [b for target, b in docs
                  if target and os.path.basename(target) in wanted]
    elif inline:
        bodies = []
    else:
        bodies = [b for _, b in docs]

    read = 0
    for path in named:
        content = read_file(path, cwd)
        if content:
            bodies.append(content)
            read += 1

    text = "\n\n".join(list(inline) + bodies).strip()
    # An unreadable message file is a hole, not an absence. A shell variable in
    # the path reaches this hook unexpanded, and passing would skip every rule.
    # A heredoc in the same command writes that file after this hook runs, so
    # its text is already in hand and the path not existing yet is expected.
    if named and not read and not text:
        return "unreadable", named[0], program or "git"
    if not text:
        return None, None, None
    return ("commit" if program else gh_kind), text, program or "git"


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


def plain(body):
    """Body with fenced code, code spans and URLs removed."""
    text = re.sub(r"(?s)```.*?```", " ", body)
    return URL.sub("URL", CODE_SPAN.sub("CODE", text))


DASH_NAMES = {"—": "em dash", "–": "en dash"}


def check_dashes(body, offset=0):
    """Locate the dash. A bare count sends the reader hunting, and the character
    they wrongly suspect is the ASCII hyphen opening a list item."""
    hits = []
    for number, line in enumerate(body.split("\n"), start=1 + offset):
        for column, char in enumerate(line):
            if char in DASH_NAMES:
                hits.append((number, DASH_NAMES[char],
                             line[max(0, column - 30):column + 30].strip()))
    if not hits:
        return []
    number, name, snippet = hits[0]
    return [("em-dash",
             "%d en or em dash. The first is an %s on line %d: \"%s\". An ASCII "
             "hyphen is never matched, so a list item opening with \"-\" is not "
             "this finding. Replace the dash with a comma, a colon, or a second "
             "sentence." % (len(hits), name, number, snippet))]


def check_wrapping(body):
    lines = dict(prose_lines(body))
    for number, line in sorted(lines.items()):
        text = line.rstrip()
        following = lines.get(number + 1, "").strip()
        if not (50 <= len(text) <= 95):
            continue
        if text[-1:] in ".!?:" or not following:
            continue
        return [("wrapping", "line %d is wrapped mid-sentence at %d characters. "
                 "Write one line per paragraph." % (number, len(text)))]
    return []


BLOCK_START = re.compile(r"^\s*(?:[-*+>|]|#{1,6}|\d+[.)])\s")


def text_units(body):
    """Paragraphs and list items, one unit each, markers stripped.

    A list item carries no terminal punctuation, so a run of them reaching the
    sentence splitter joins into a single sentence as long as the list."""
    units = []
    current = []
    for line in plain(body).split("\n"):
        if not line.strip():
            if current:
                units.append(" ".join(current))
                current = []
            continue
        if BLOCK_START.match(line):
            if current:
                units.append(" ".join(current))
            current = [BLOCK_START.sub("", line.strip(), count=1)]
            continue
        current.append(line.strip())
    if current:
        units.append(" ".join(current))
    return units


def check_sentences(body):
    findings = []
    for unit in text_units(body):
        for sentence in SENTENCE_SPLIT.split(unit):
            words = sentence.split()
            if len(words) > MAX_WORDS:
                # Greg's own prose averages 19-21 words, so 20 is the target and
                # not a wall. Only a genuine run-on stops the commit.
                rule = "sentence-length" if len(words) > BLOCK_WORDS else "sentence-long"
                findings.append((rule,
                                 "%d words: %s" % (len(words), " ".join(words[:9]) + " ...")))
    return findings[:5]


COMMENT_LINE = re.compile(r"^\+\s*(?:#|//|--|;|\*)\s?(.*\S)\s*$")
DUP_RUN = 4
DUP_SKIP = frozenset(
    "a an and are as at be been but by for from had has have in into is it its "
    "not of on or so that the their then there these they this to was were "
    "which while with would".split()
)


def _stem(word):
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _tokens(text):
    """Content words, stemmed. Paraphrase is still duplication."""
    raw = re.findall(r"[a-z0-9_/.'-]+", text.replace("`", " ").lower())
    return [_stem(w) for w in raw if w not in DUP_SKIP]


def git(args, cwd, program="git"):
    """Run a git command. Returns stdout, or None when git cannot answer."""
    import subprocess

    try:
        result = subprocess.run([program] + args, cwd=cwd or None,
                                capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return result.stdout if result.returncode in (0, 1) else None


def staged_diff(cwd, program="git"):
    """Staged changes, falling back to the working tree.

    A command that stages and commits in one line reaches this hook before the
    add runs, so the index is still empty and every diff check sees nothing."""
    staged = git(["diff", "--cached", "-U0"], cwd, program) or ""
    if staged.strip():
        return staged
    return git(["diff", "HEAD", "-U0"], cwd, program) or ""


def comment_runs(diff):
    """Comment text the staged diff adds, grouped into adjacent runs."""
    runs = []
    current = []
    for line in diff.split("\n"):
        if line.startswith("+++"):
            continue
        match = COMMENT_LINE.match(line)
        if match:
            current.append(match.group(1))
            continue
        if current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


# A doc comment states a contract, so it is not a why-comment and the
# three-sentence ceiling does not apply to it.
DOC_MARKER = re.compile(r"@param|@return|@throws|@type|:param|:return|:rtype"
                        r"|\bArgs:|\bReturns:|\bRaises:|\bAttributes:", re.I)


def check_tier_duplication(diff, body):
    """A comment repeating the commit body is one rationale in two tiers."""
    body_words = _tokens(body)
    if len(body_words) < DUP_RUN:
        return []
    grams = {
        tuple(body_words[i : i + DUP_RUN])
        for i in range(len(body_words) - DUP_RUN + 1)
    }
    flat = " ".join(line for run in comment_runs(diff) for line in run)
    comment_words = _tokens(flat)
    for i in range(len(comment_words) - DUP_RUN + 1):
        gram = tuple(comment_words[i : i + DUP_RUN])
        if gram in grams:
            return [("tier-duplication",
                     'a comment this commit adds repeats the commit body: "%s ...". '
                     "One rationale per tier: the commit carries why the change was "
                     "made, the comment carries only what the code cannot say."
                     % " ".join(gram))]
    return []


RATIONALE = re.compile(
    r"\bused to\b|\bno longer\b|\bpreviously\b|\bwas considered\b"
    r"|\bnow .{1,30}\binstead\b|\bthis (?:change|commit|patch)\b"
    r"|\bwe (?:switched|moved|changed|replaced)\b|\bused to be\b",
    re.I,
)


def check_code_comment_rationale(diff):
    """Before-and-after contrast in a comment is commit-body content."""
    for run in comment_runs(diff):
        text = " ".join(run)
        if DOC_MARKER.search(text):
            continue
        match = RATIONALE.search(text)
        if match:
            return [("code-comment-rationale",
                     'a comment this commit adds says "%s". A comment that contrasts '
                     "before and after is change rationale. Move it to the commit body."
                     % match.group(0))]
    return []


def check_code_comment_length(diff):
    for run in comment_runs(diff):
        text = " ".join(run).strip()
        if DOC_MARKER.search(text) or len(text) < 120:
            continue
        count = len([s for s in SENTENCE_SPLIT.split(text) if s.strip()])
        if count > MAX_CODE_COMMENT_SENTENCES:
            return [("code-comment-length",
                     'a comment this commit adds runs %d sentences: "%s ...". The ceiling '
                     "is %d. Move it to a doc and leave a one-line pointer."
                     % (count, text[:60], MAX_CODE_COMMENT_SENTENCES))]
    return []


BANNED = [
    "i wanted to reach out", "just wanted to", "just checking in", "reaching out",
    "circle back", "touch base", "sync up", "at the end of the day",
    "great question", "hope this finds you well", "delve", "tapestry",
    "in today's fast-paced", "best-in-class", "seamless", "unlock",
    "leverage", "empower", "catch-all", "unbounded",
]
NOT_X_IT_Y = re.compile(r"\b(?:it'?s|this is|that'?s)\s+not\s+[^,.]{1,40},\s*"
                        r"(?:it'?s|this is|that'?s)\b", re.I)


def check_banned(body):
    text = plain(body).lower()
    hits = [phrase for phrase in BANNED if phrase in text]
    findings = []
    if hits:
        findings.append(("banned-phrase",
                         "banned phrase: %s. If the phrase is banned because the sentence "
                         "does no work, delete the sentence rather than reword it."
                         % ", ".join('"%s"' % h for h in hits[:4])))
    if NOT_X_IT_Y.search(plain(body)):
        findings.append(("banned-phrase",
                         'an "It\'s not X, it\'s Y" construction. State the claim directly.'))
    return findings


def check_ellipsis(body):
    if re.search(r"\.\.\.|…", plain(body)):
        return [("ellipsis", "an ellipsis. It is Greg's keyboard habit when he writes, "
                 "never one to generate.")]
    return []


VERBED = re.compile(
    r"\b(?:that|which|to|will|can|should|would|may|must|and|or)\s+"
    r"(names?|gates?|actions?|surfaces?|impacts?|leverages?|architects?)\b",
    re.I,
)


def check_verbed_noun(body):
    match = VERBED.search(plain(body))
    if match:
        return [("verbed-noun",
                 'the noun "%s" is used as a verb ("%s"). Simplified Technical English '
                 "fixes one part of speech per word. Point at the literal artifact instead."
                 % (match.group(1), match.group(0)))]
    return []


HYPHENATED = re.compile(r"\b[a-z]{3,}(?:-[a-z]{2,}){1,2}\b")
CAMEL = re.compile(r"\b[A-Z][a-z]{2,}[A-Z][A-Za-z]{2,}\b")
COMMON_COMPOUND = frozenset("""
follow-up followup out-of-scope well-known read-only read-write up-to-date
end-to-end long-running short-lived left-over per-user per-repo per-branch
non-zero non-empty pull-request first-party third-party built-in opt-in opt-out
day-to-day one-off round-trip side-effect trade-off drop-in run-time
false-positive false-negative
re-run re-use set-up check-in hard-coded well-formed self-hosted multi-tenant
GitHub GitLab PostgreSQL MySQL JavaScript TypeScript Kubernetes CloudFormation
OpenTofu DataDog CloudFlare PagerDuty ClickHouse LinkedIn WireGuard MacOS
""".split())


def check_coined_term(cwd, body, diff, program="git"):
    """A name being introduced appears in the diff. One only in prose is invented."""
    text = plain(body)
    skip = COMMON_COMPOUND
    candidates = []
    for term in HYPHENATED.findall(text) + CAMEL.findall(text):
        if term.lower() not in skip and term not in candidates:
            candidates.append(term)
    lower_diff = diff.lower()
    for term in candidates[:12]:
        if term.lower() in lower_diff:
            continue
        found = git(["grep", "--cached", "-liF", term], cwd, program)
        # None means git could not answer, so the term cannot be judged.
        if found is None or found.strip():
            continue
        return [("coined-term",
                 '"%s" appears in the prose but not in the staged diff or the repo. '
                 "A name you are genuinely introducing shows up in the code you are "
                 "committing. Either use plain description, or, if the term is real "
                 "and will be reused, define it in the repo (CONTEXT.md, a glossary, "
                 "or an ADR) so it stops being invented." % term)]
    return []


DEFERRAL = re.compile(
    r"should (?:probably |also |eventually )*(?:address|consider|revisit|look at|fix|handle)"
    r"|additional (?:things|items|work|changes|cleanup)"
    r"|(?<!among )other (?:things|items|cleanup)"
    r"|might want to|follow-?ups?\b|in a (?:future|later) (?:pr|commit)"
    r"|left for later|further (?:work|investigation)",
    re.I,
)
VAGUE = re.compile(r"\bprobably\b|\bsome\b|\ba few\b|\badditional\b|\bother\b"
                   r"|\bmight\b|\bvarious\b|\betc\b", re.I)
TICKET = re.compile(r"#\d+|[A-Z]{2,}-\d+")


TRAILING_HEADER = re.compile(
    r"^\s*(?:#{1,4}\s*)?\**\s*(worth noting|notes?|additional notes?|other notes?"
    r"|follow[- ]?ups?|remaining|next steps?|open (?:items|questions)|to do)\b",
    re.I,
)
UNDECIDED = re.compile(
    r"needs? to be decided|needs? a decision|still (?:to be|needs?|open)"
    r"|\bTBD\b|to be determined|open question|should we\b|do we\b"
    r"|we (?:should|could) (?:probably|also|maybe)|might want to"
    r"|unclear (?:if|whether|how)|not sure (?:if|whether)|up for discussion"
    r"|worth (?:discussing|considering)|\bTODO\b|(?:one|two|a few|a couple) more",
    re.I,
)
ITEM_START = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def trailing_items(lines):
    """Items in a trailing section, each joined with its continuation lines."""
    items = []
    for line in lines:
        if ITEM_START.match(line):
            items.append(line)
        elif items and line.strip():
            items[-1] += " " + line.strip()
    return items


def check_trailing_scope(body):
    """An undecided item at the end is work that belonged in the discussion.

    Matching the section header alone would fire on every notes list, so only an
    item carrying no decision and no ticket is reported."""
    lines = plain(body).strip().split("\n")
    start = None
    for index, line in enumerate(lines):
        if TRAILING_HEADER.match(line):
            start = index
    if start is not None:
        for item in trailing_items(lines[start:]):
            if UNDECIDED.search(item) and not TICKET.search(item):
                return [("trailing-scope",
                         'a trailing item is still undecided: "%s". That section carries '
                         "settled consequences of the change. An open decision belonged in "
                         "the discussion before this was written. Decide it, give it a "
                         "ticket, or cut it." % item.strip()[:70])]
        return []

    body_lines = [line for line in lines if line.strip()]
    tail = "\n".join(body_lines[-4:])
    if not DEFERRAL.search(tail):
        return []
    if TICKET.search(tail) or not VAGUE.search(tail):
        return []
    return [("trailing-scope",
             "the closing lines defer work vaguely. A deferral names the specific thing "
             "and either carries a ticket number or says why it was deferred. Resolve it, "
             "track it, or cut it.")]


OOS_HEADER = re.compile(r"^\s*(?:#{1,4}\s*)?\**\s*out[- ]of[- ]scope\b", re.I)
OOS_NEXT = re.compile(r"^\s*(?:#{1,4}\s|\*\*[A-Z])")
OOS_POINTER = re.compile(
    r"separate (?:issue|finding|pr|ticket)|file (?:a|an|it|them|these)"
    r"|follow[- ]?up|another (?:pr|issue)|tracked (?:in|by)|will be (?:done|handled)"
    r"|deferred to|left (?:to|for)", re.I)


def check_out_of_scope(body):
    """Out of Scope names what this artifact does not do, not where other work lives.

    Only content is judged here. The short noun phrase shape is evidenced in
    convention documents, which never reach this hook, so enforcing it on an
    issue would apply one artifact class to another."""
    lines = plain(body).split("\n")
    start = next((i for i, line in enumerate(lines) if OOS_HEADER.match(line)), None)
    if start is None:
        return []
    section = []
    for line in lines[start + 1:]:
        if OOS_NEXT.match(line):
            break
        section.append(line)

    # A bare paragraph under the header carries no bullet, so it would escape a
    # list walk entirely, and that is the shape of the entries Greg deletes.
    items = trailing_items(section) or [" ".join(section).strip()]
    for item in items:
        text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", item).strip()
        if not text:
            continue
        if TICKET.search(text) or OOS_POINTER.search(text):
            return [("out-of-scope-pointer",
                     'the Out of Scope list points at other work: "%s". That section names an '
                     "expectation a reader would bring to this artifact, so they stop expecting "
                     "it. Other work gets its own issue, not a pointer stapled here."
                     % text[:70])]
    return []


def check_body_line_length(body, offset=0):
    """commitlint's body-max-line-length. Commit bodies wrap at 100."""
    for number, line in enumerate(body.split("\n"), start=1 + offset):
        if len(line) > MAX_BODY_LINE:
            return [("body-line-length",
                     "line %d is %d characters. Wrap commit bodies at %d, "
                     "matching Conventional Commits." % (number, len(line), MAX_BODY_LINE))]
    return []


def outside_fences(lines):
    """Lines that sit outside a fenced code block, the fences themselves dropped."""
    fenced = False
    for line in lines:
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield line


def check_length(kind, body):
    limit = MAX_COMMENT_LINES if kind == "comment" else MAX_BODY_LINES
    lines = body.split("\n")
    if kind == "comment":
        lines = outside_fences(lines)
    count = len([line for line in lines if line.strip()])
    if count <= limit:
        return []
    exempt = "Blank lines do not count"
    if kind == "comment":
        exempt += ", and neither does fenced code"
    return [(LENGTH_RULE[kind], "this %s is %d lines. The limit is %d. %s."
             % (LABELS[kind], count, limit, exempt))]


def collect(kind, body, text, cwd, program="git"):
    # A commit subject is stripped before the checks run, so a line number taken
    # from the body is one short of the line the writer sees.
    offset = 1 if kind == "commit" else 0
    findings = (check_dashes(body, offset) + check_sentences(body)
                + check_banned(body) + check_ellipsis(body)
                + check_verbed_noun(body))
    diff = staged_diff(cwd, program)
    if kind == "commit":
        return (findings + check_coined_term(cwd, body, diff, program)
                + check_body_line_length(body, offset) + check_tier_duplication(diff, body)
                + check_code_comment_rationale(diff) + check_code_comment_length(diff)
                + check_trailing_scope(body))

    # A GitHub body is not a commit, so it is never wrapped.
    findings += check_wrapping(body) + check_length(kind, body)
    # An issue proposes work that has no diff yet, so a name it introduces
    # cannot appear in one.
    if kind != "issue":
        findings += check_coined_term(cwd, body, diff, program)
    # A comment is the discussion. Raising an open question in one is its point,
    # and Out of Scope is a section a body carries, not a comment.
    if kind != "comment":
        findings += check_trailing_scope(body) + check_out_of_scope(body)
    return findings


def take_override():
    """Read the hand-written skip list and delete it, so one file is one use."""
    try:
        with open(OVERRIDE_FILE, encoding="utf-8") as handle:
            rules = {line.strip() for line in handle
                     if line.strip() and not line.startswith("#")}
    except OSError:
        return set()
    try:
        os.remove(OVERRIDE_FILE)
    except OSError:
        pass
    return rules


def record(kind, cwd, findings, bypassed, attempted=()):
    """Append one line per invocation. A silent log is worth more than a warning
    nobody rereads, and a bypass has to leave a trace to be reviewable."""
    import datetime

    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "cwd": cwd,
        "blocked": sorted({r for r, _ in findings if r not in WARN_ONLY}),
        "warned": sorted({r for r, _ in findings if r in WARN_ONLY}),
        "bypassed": sorted(bypassed),
        "attempted": sorted(attempted),
    }
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def main():
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    cwd = payload.get("cwd") or os.getcwd()

    kind, text, program = extract(command, cwd)
    if not kind:
        return 0
    if kind == "unreadable":
        record("unreadable", cwd, [("unreadable-message", text)], set())
        sys.stderr.write(
            "Blocked: the message file %r could not be read, so the text was not "
            "checked.\n\nA path holding a shell variable arrives here unexpanded. "
            "Pass an absolute path, or pass the message with -m.\n" % text)
        return 2

    body = body_of(kind, text)
    if not body.strip():
        return 0

    attempted = {r.strip() for r in os.environ.get("WRITING_RULES_ALLOW", "").split(",") if r.strip()}
    allowed = take_override()
    all_findings = collect(kind, body, text, cwd, program)
    findings = [f for f in all_findings if f[0] not in allowed]
    record(kind, cwd, all_findings, {r for r, _ in all_findings if r in allowed}, attempted)

    if attempted:
        sys.stderr.write(
            "Blocked: WRITING_RULES_ALLOW was set to %s on this command.\n\n"
            "That variable is not honored and never skips a rule. It is recorded in "
            "%s.\n\nStop. Tell Greg that a bypass was attempted, name the rule, and "
            "say why the text could not satisfy it. Do not retry the command, with or "
            "without the variable.\n" % (",".join(sorted(attempted)), LOG_FILE))
        return 2

    blocking = [f for f in findings if f[0] not in WARN_ONLY]
    warnings = [f for f in findings if f[0] in WARN_ONLY]

    label = LABELS[kind]
    if warnings:
        sys.stderr.write("Writing warnings on this %s (not blocking):\n" % label)
        for rule, message in warnings:
            sys.stderr.write("  - [%s] %s\n" % (rule, message))
        sys.stderr.write("\n")
    if not blocking:
        return 0

    sys.stderr.write("Blocked: this %s breaks the writing rules.\n\n" % label)
    for rule, message in blocking:
        sys.stderr.write("  - [%s] %s\n" % (rule, message))
    sys.stderr.write("\n%s\n\nRewrite the text and run the command again.\n" % RULES)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
