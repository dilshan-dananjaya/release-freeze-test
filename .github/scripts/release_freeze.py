#!/usr/bin/env python3
"""Freeze a release branch after sign-off. See jahanVerse#18469."""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
SIGNOFF_LABEL = "release-sign-off"
OVERRIDE_LABEL = "freeze-override"
CHECK_WORKFLOW = "Release freeze check"
SOURCE_BRANCH = "dev"
TARGET_BRANCH = "master"
BRANCH_BOX = "Create release branch"
OWNER_BOX = "Release owner approved"
QA_BOXES = ["QA approved (product)", "QA approved (client)"]
SIGNOFF_BOXES = QA_BOXES + [OWNER_BOX]
OWNER_LINE = "Release Owner"
QA_LINE = "QA Approver"
PR_REVIEWER = os.environ.get("PR_REVIEWER", "").lower()
BLANK_VERSION = "v0.0.0"


def api(method, path, data=None):
    """Call the GitHub API. Returns None for a 404."""
    request = urllib.request.Request(
        f"{API}/repos/{REPO}/{path}",
        method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        print(f"{method} {path} failed: {error.code} {error.read().decode()}")
        raise


def tag_sha(tag):
    ref = api("GET", f"git/ref/tags/{urllib.parse.quote(tag)}")
    if not ref:
        return None
    if ref["object"]["type"] == "tag":
        return api("GET", f"git/tags/{ref['object']['sha']}")["object"]["sha"]
    return ref["object"]["sha"]


def branch_sha(branch):
    found = api("GET", f"branches/{urllib.parse.quote(branch, safe='')}")
    return found["commit"]["sha"] if found else None


def latest_rc(version):
    refs = api("GET", f"git/matching-refs/tags/{version}-rc") or []
    numbers = []
    for ref in refs:
        match = re.fullmatch(rf"refs/tags/{re.escape(version)}-rc(\d+)", ref["ref"])
        if match:
            numbers.append(int(match.group(1)))
    return f"{version}-rc{max(numbers)}" if numbers else None


def read_version(body):
    match = re.search(r"^Release version:[ \t]*`?(v\d+\.\d+\.\d+)`?[ \t\r]*$", body, re.M)
    return match.group(1) if match and match.group(1) != BLANK_VERSION else None


def read_rc(body):
    match = re.search(r"^Release candidate:[ \t]*`([^`]+)`[ \t\r]*$", body, re.M)
    return match.group(1) if match else None


def read_jira(body):
    match = re.search(r"^Jira ticket:[ \t]*(JACM-\d+)[ \t\r]*$", body, re.M)
    return match.group(1) if match else None


def people(body, line):
    """Returns the lower-case GitHub names on a ticket line, like `QA Approver: @a @b`."""
    match = re.search(rf"^{line}:(.*)$", body, re.M)
    return [name.lower() for name in re.findall(r"@([A-Za-z0-9-]+)", match.group(1))] if match else []


def tick_problem(body, box, sender):
    """Returns why this person can't change this box, or None if they can."""
    line = QA_LINE if box in QA_BOXES else OWNER_LINE
    names = people(body, line)
    if not names:
        return f"Add a name to `{line}:` first."
    if box in SIGNOFF_BOXES and set(people(body, OWNER_LINE)) & set(people(body, QA_LINE)):
        return f"`{OWNER_LINE}` and `{QA_LINE}` must be different people."
    if sender.lower() not in names:
        return f"@{sender} isn't the {line}."
    return None


def assign_people(number, issue, body):
    """Assigns the named people to the ticket. Names GitHub can't assign are skipped."""
    current = {person["login"].lower() for person in issue["assignees"]}
    missing = [name for name in people(body, OWNER_LINE) + people(body, QA_LINE) if name not in current]
    if not missing:
        return
    try:
        api("POST", f"issues/{number}/assignees", {"assignees": missing})
    except urllib.error.HTTPError:
        print(f"Couldn't assign {missing}.")


def box_ticked(body, box):
    """Returns True or False, or None if the box isn't in the ticket."""
    match = re.search(rf"^- \[([ xX])\] {re.escape(box)}([ \t].*)?\r?$", body, re.M)
    return match.group(1) != " " if match else None


def boxes(body):
    """Returns {box text: ticked} for the sign-off boxes."""
    found = {box: box_ticked(body, box) for box in SIGNOFF_BOXES}
    return {box: ticked for box, ticked in found.items() if ticked is not None}


def set_box(body, box, ticked):
    mark = "x" if ticked else " "
    return re.sub(
        rf"^- \[[ xX]\] {re.escape(box)}(.*)$",
        lambda match: f"- [{mark}] {box}{match.group(1)}",
        body,
        flags=re.M,
    )


def clear_boxes(body):
    for box in SIGNOFF_BOXES:
        body = set_box(body, box, False)
    return body


def set_branch_mark(body, mark):
    """Writes the mark after the branch box, like `- [x] Create release branch ⏳`."""
    return re.sub(
        rf"^(- \[[ xX]\] {re.escape(BRANCH_BOX)})[^\r\n]*",
        lambda match: f"{match.group(1)} {mark}".rstrip(),
        body,
        flags=re.M,
    )


def set_line(body, name, value):
    return re.sub(rf"^{name}:.*$", f"{name}: {value}", body, flags=re.M)


def fill_ticket(body):
    """Adds the branch and latest RC. Clears the ticks if the RC changed."""
    version = read_version(body)
    if not version:
        return body, False
    rc = latest_rc(version)
    old_rc = read_rc(body)
    branch = f"release/{version}"
    link = f"[`{branch}`](https://github.com/{REPO}/tree/{branch}) ✅"
    body = set_branch_mark(body, link if box_ticked(body, BRANCH_BOX) else "")
    body = set_line(body, "Release candidate", f"`{rc}`" if rc else "waiting for first RC")
    rc_changed = old_rc != rc and any(boxes(body).values())
    if rc_changed:
        body = clear_boxes(body)
    return body, rc_changed


def comment(number, text):
    api("POST", f"issues/{number}/comments", {"body": text})


def rerun_checks(branch):
    """Open PRs that passed before the freeze must be checked again."""
    query = urllib.parse.urlencode({"state": "open", "base": branch, "per_page": 100})
    for pull in api("GET", f"pulls?{query}"):
        sha = pull["head"]["sha"]
        runs = api("GET", f"actions/runs?head_sha={sha}&per_page=100")["workflow_runs"]
        runs = [run for run in runs if run["name"] == CHECK_WORKFLOW]
        if runs:
            api("POST", f"actions/runs/{runs[0]['id']}/rerun", {})


def freeze(number, version, rc):
    branch = f"release/{version}"
    head = branch_sha(branch)
    rc_commit = tag_sha(rc) if rc else None
    if not head or not rc_commit:
        comment(number, f"Not frozen: `{branch}` or its RC doesn't exist yet.")
        return
    if head != rc_commit:
        comment(
            number,
            f"Not frozen: `{branch}` has moved past `{rc}`. "
            "Create a new RC, test it and sign off again.",
        )
        return
    frozen = f"frozen/{version}"
    if tag_sha(frozen):
        api("PATCH", f"git/refs/tags/{frozen}", {"sha": head, "force": True})
    else:
        api("POST", "git/refs", {"ref": f"refs/tags/{frozen}", "sha": head})
    rerun_checks(branch)
    comment(number, f"Frozen on `{rc}`. PRs into `{branch}` are now blocked.")


def create_branch(number, version):
    branch = f"release/{version}"
    if branch_sha(branch):
        comment(number, f"`{branch}` already exists, so it wasn't changed.")
        return
    sha = branch_sha(SOURCE_BRANCH)
    api("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
    comment(number, f"Created `{branch}` from `{SOURCE_BRANCH}` at `{sha[:7]}`.")


def release_title(version, jira):
    return f"{jira or 'JACM-XXX'} Release V{version[1:]}"


def find_release_pr(version):
    owner = REPO.split("/")[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:release/{version}", "base": TARGET_BRANCH})
    found = api("GET", f"pulls?{query}")
    return found[0] if found else None


def update_pr_title(old_body, body):
    """Keeps the release PR title in step with the ticket's Jira number."""
    version = read_version(body)
    if not version or read_jira(old_body) == read_jira(body):
        return
    pull = find_release_pr(version)
    if pull:
        api("PATCH", f"pulls/{pull['number']}", {"title": release_title(version, read_jira(body))})


def create_release_pr(number, version, body):
    """Opens the release PR into master and asks the reviewer."""
    branch = f"release/{version}"
    found = find_release_pr(version)
    if found:
        comment(number, f"Release PR #{found['number']} already exists.")
        return
    try:
        pull = api("POST", "pulls", {
            "title": release_title(version, read_jira(body)),
            "head": branch,
            "base": TARGET_BRANCH,
            "body": f"Release {version}, cut from `{SOURCE_BRANCH}`.\n\nChecklist: #{number}",
        })
    except urllib.error.HTTPError as error:
        comment(number, f"Couldn't create the release PR ({error.code}). Untick and tick `{BRANCH_BOX}` to try again.")
        return
    reviewers = [PR_REVIEWER] if PR_REVIEWER else []
    requested = []
    try:
        if reviewers:
            result = api("POST", f"pulls/{pull['number']}/requested_reviewers", {"reviewers": reviewers})
            requested = [person["login"].lower() for person in (result or {}).get("requested_reviewers", [])]
    except urllib.error.HTTPError:
        pass
    # GitHub skips a name it can't add without returning an error.
    if set(reviewers) <= set(requested):
        comment(number, f"Created release PR #{pull['number']}.")
    else:
        comment(number, f"Created release PR #{pull['number']}, but couldn't request a review from `{PR_REVIEWER}`.")


def branch_box(number, before, after, body, sender):
    """Creates the release branch when its box is newly ticked."""
    if box_ticked(before, BRANCH_BOX) or not box_ticked(after, BRANCH_BOX):
        return body
    version = read_version(body)
    reason = tick_problem(body, BRANCH_BOX, sender)
    if not reason and not version:
        reason = "Add `Release version: vX.Y.Z` first."
    if not reason:
        api("PATCH", f"issues/{number}", {"body": set_branch_mark(body, "⏳")})
        create_branch(number, version)
        create_release_pr(number, version, body)
        return body
    comment(number, f"`{BRANCH_BOX}` was changed back: {reason}")
    return set_box(body, BRANCH_BOX, False)


def signoff(event):
    issue = event["issue"]
    number = issue["number"]
    sender = event["sender"]["login"]
    live = api("GET", f"issues/{number}")["body"] or ""

    changed = []
    if event["action"] == "edited":
        if "body" not in event.get("changes", {}):
            return
        before = boxes(event["changes"]["body"]["from"] or "")
        after = boxes(issue["body"] or "")
        changed = [box for box in after if before.get(box) != after[box]]

    body = live
    allowed = []
    for box in changed:
        problem = tick_problem(body, box, sender)
        if problem:
            body = set_box(body, box, before.get(box, False))
            comment(number, f"`{box}` was changed back: {problem}")
        else:
            allowed.append(box)

    if event["action"] == "edited":
        old_body = event["changes"]["body"]["from"] or ""
        body = branch_box(number, old_body, issue["body"] or "", body, sender)
        update_pr_title(old_body, body)

    body, rc_changed = fill_ticket(body)
    # Read again: the waiting mark may have changed the ticket since the start.
    if body != (api("GET", f"issues/{number}")["body"] or ""):
        api("PATCH", f"issues/{number}", {"body": body})
    assign_people(number, issue, body)
    if rc_changed:
        comment(number, f"The RC is now `{read_rc(body)}`, so the sign-off ticks were cleared.")
        return

    ticks = boxes(body)
    if allowed and len(ticks) == len(SIGNOFF_BOXES) and all(ticks.values()):
        freeze(number, read_version(body), read_rc(body))


def new_rc(event):
    release = event["release"]
    match = re.fullmatch(r"(v\d+\.\d+\.\d+)-rc\d+", release["tag_name"])
    if not release["prerelease"] or not match:
        return
    query = urllib.parse.urlencode({"state": "open", "labels": SIGNOFF_LABEL, "per_page": 100})
    for issue in api("GET", f"issues?{query}"):
        body = issue["body"] or ""
        if read_version(body) != match.group(1):
            continue
        new_body, rc_changed = fill_ticket(body)
        if new_body != body:
            api("PATCH", f"issues/{issue['number']}", {"body": new_body})
        if rc_changed:
            comment(
                issue["number"],
                f"The RC is now `{read_rc(new_body)}`, so the sign-off ticks were cleared.",
            )


def find_ticket(version):
    query = urllib.parse.urlencode({"state": "open", "labels": SIGNOFF_LABEL, "per_page": 100})
    for issue in api("GET", f"issues?{query}"):
        if read_version(issue["body"] or "") == version:
            return issue["number"]
    return None


def publish(event):
    """Publishes the final release after the release PR is merged."""
    pull = event["pull_request"]
    match = re.fullmatch(r"release/(v\d+\.\d+\.\d+)", pull["head"]["ref"])
    if not match:
        return
    version = match.group(1)
    number = find_ticket(version) or pull["number"]
    frozen = tag_sha(f"frozen/{version}")
    if tag_sha(version):
        comment(number, f"`{version}` already exists, so nothing was published.")
    elif not frozen:
        comment(number, f"`{version}` wasn't published: it has no sign-off.")
    elif frozen != pull["head"]["sha"]:
        comment(number, f"`{version}` wasn't published: the PR has commits that aren't in the approved RC.")
    else:
        try:
            release = api("POST", "releases", {
                "tag_name": version,
                "target_commitish": frozen,
                "name": version,
                "make_latest": "true",
                "generate_release_notes": True,
            })
            comment(number, f"Published [`{version}`]({release['html_url']}) as the latest release.")
        except urllib.error.HTTPError as error:
            comment(number, f"Couldn't publish `{version}` ({error.code}).")


def fail(message):
    print(f"::error::{message}")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(f"{message}\n")
    sys.exit(1)


def check(event):
    number = event["pull_request"]["number"]
    pull = api("GET", f"pulls/{number}")
    name = pull["base"]["ref"].removeprefix("release/")
    reason = None
    if tag_sha(name):
        reason = f"`{name}` is already released"
    elif tag_sha(f"frozen/{name}"):
        reason = f"`release/{name}` is frozen"
    if not reason:
        print(f"release/{name} is not frozen.")
        return

    labels = [label["name"] for label in pull["labels"]]
    if OVERRIDE_LABEL not in labels:
        fail(f"{reason}. A late fix needs the `{OVERRIDE_LABEL}` label from a second person.")

    events = api("GET", f"issues/{number}/events?per_page=100")
    added = [
        item["actor"]["login"]
        for item in events
        if item["event"] == "labeled" and item["label"]["name"] == OVERRIDE_LABEL
    ]
    labeller = added[-1]
    reviews = api("GET", f"pulls/{number}/reviews?per_page=100")
    approvers = {r["user"]["login"] for r in reviews if r["state"] == "APPROVED"}
    if labeller == pull["user"]["login"] or labeller in approvers:
        fail(f"{reason}. `{OVERRIDE_LABEL}` must come from someone who didn't write or approve this PR.")
    print(f"{reason}, but {labeller} added {OVERRIDE_LABEL}.")


def gate(version):
    frozen = tag_sha(f"frozen/{version}")
    head = branch_sha(f"release/{version}")
    if not frozen:
        fail(f"`{version}` has no sign-off: `frozen/{version}` doesn't exist.")
    if frozen != head:
        fail(f"`release/{version}` has commits that aren't in the approved RC.")
    print(f"{version} matches the approved RC at {frozen}.")


def main():
    command = sys.argv[1]
    if command == "gate":
        gate(sys.argv[2])
        return
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        event = json.load(f)
    {"signoff": signoff, "new-rc": new_rc, "check": check, "publish": publish}[command](event)


if __name__ == "__main__":
    main()
