#!/usr/bin/env python3
"""Generate the profile stats cards (stats, languages, repositories) as SVG files.

Reads every repository the token can see, public and private. Private repository names are
never written to the output: only counts, language shares and line totals are.

Environment:
  GH_TOKEN              token for the GitHub API (public data only if it lacks the repo scope)
  STATS_USER            GitHub login to report on
  STATS_EXCLUDE_OWNERS  comma-separated owners (users or orgs) whose repositories are skipped
  STATS_EXCLUDE_REPOS   comma-separated repositories (name or owner/name) that are skipped
  OUT_DIR               output directory (default: profile)
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape

API = "https://api.github.com"
TOKEN = os.environ["GH_TOKEN"]
USER = os.environ["STATS_USER"]
OUT = os.environ.get("OUT_DIR", "profile")


def csv_env(name):
    return {v.strip().lower() for v in os.environ.get(name, "").split(",") if v.strip()}


EXCLUDE_OWNERS = csv_env("STATS_EXCLUDE_OWNERS")
EXCLUDE_REPOS = csv_env("STATS_EXCLUDE_REPOS")
BYTES_PER_LINE = 40  # fallback when per-author line stats are unavailable


def request(method, url, body=None):
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "profile-stats",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        return err.code, None


def gql(query, **variables):
    status, data = request("POST", f"{API}/graphql", {"query": query, "variables": variables})
    if status != 200 or not data or not data.get("data"):
        # Workflow logs of a public repo are public: report error types only, never messages
        # (messages can name private repositories or organizations).
        kinds = sorted({str(e.get("type", "unknown")) for e in (data or {}).get("errors", [])})
        sys.exit(f"GraphQL request failed (HTTP {status}, error types: {', '.join(kinds) or 'none'})")
    return data["data"]


LANGS = "languages(first: 20, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }"
REPO = "nameWithOwner isPrivate isFork stargazerCount " + LANGS


def owned_repos():
    repos, cursor = [], None
    while True:
        page = gql(
            """query($login: String!, $after: String) { user(login: $login) {
              repositories(first: 50, after: $after, ownerAffiliations: OWNER, isFork: false) {
                pageInfo { hasNextPage endCursor } nodes { %s } } } }""" % REPO,
            login=USER,
            after=cursor,
        )["user"]["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        cursor = page["pageInfo"]["endCursor"]


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def contributions():
    """Repos with commits by USER, plus commit and PR/issue totals."""
    head = gql(
        "query($l: String!) { user(login: $l) { createdAt pullRequests { totalCount } issues { totalCount } } }",
        l=USER,
    )["user"]
    query = """query($l: String!, $frm: DateTime!, $to: DateTime!) { user(login: $l) {
      contributionsCollection(from: $frm, to: $to) {
        totalCommitContributions
        commitContributionsByRepository(maxRepositories: 100) { repository { %s } contributions { totalCount } } } } }""" % REPO
    now = datetime.now(timezone.utc)
    first_year = int(head["createdAt"][:4])
    repos, all_time = {}, 0
    for year in range(first_year, now.year + 1):
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = min(datetime(year + 1, 1, 1, tzinfo=timezone.utc), now)
        part = gql(query, l=USER, frm=iso(start), to=iso(end))["user"]["contributionsCollection"]
        all_time += part["totalCommitContributions"]
        for item in part["commitContributionsByRepository"]:
            repos[item["repository"]["nameWithOwner"]] = item["repository"]
    year_ago = datetime.fromtimestamp(now.timestamp() - 365 * 86400, timezone.utc)
    recent = gql(query, l=USER, frm=iso(year_ago), to=iso(now))["user"]["contributionsCollection"]
    return {
        "repos": list(repos.values()),
        "commits_all": all_time,
        "commits_year": recent["totalCommitContributions"],
        "prs": head["pullRequests"]["totalCount"],
        "issues": head["issues"]["totalCount"],
    }


def contributor_stats(names):
    """GitHub computes these lazily (HTTP 202), so poll until each repo answers."""
    result, pending = {}, list(names)
    for _ in range(8):
        waiting = []
        for name in pending:
            status, data = request("GET", f"{API}/repos/{name}/stats/contributors")
            if status == 200 and isinstance(data, list):
                result[name] = data
            elif status == 202:
                waiting.append(name)
            else:
                result[name] = None
        if not waiting:
            return result
        pending = waiting
        time.sleep(5)
    for name in pending:
        result[name] = None
    return result


def my_additions(entries):
    total = 0
    for entry in entries or []:
        if (entry.get("author") or {}).get("login", "").lower() == USER.lower():
            total += sum(week["a"] for week in entry["weeks"])
    return total


def is_flutter(name):
    status, data = request("GET", f"{API}/repos/{name}/contents/pubspec.yaml")
    if status != 200 or not data:
        return False
    text = base64.b64decode(data["content"]).decode("utf-8", "ignore")
    return "sdk: flutter" in text or "\nflutter:" in text


def keep(repo):
    owner, _, short = repo["nameWithOwner"].partition("/")
    if repo["isFork"] or owner.lower() in EXCLUDE_OWNERS:
        return False
    return repo["nameWithOwner"].lower() not in EXCLUDE_REPOS and short.lower() not in EXCLUDE_REPOS


def compact(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def card(width, height, title, body):
    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape(title)}">
<title>{escape(title)}</title>
<style>
.title {{ font: 600 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: #2f80ed }}
.label {{ font: 600 14px 'Segoe UI', Ubuntu, Sans-Serif; fill: #434d58 }}
.text {{ font: 400 13px 'Segoe UI', Ubuntu, Sans-Serif; fill: #434d58 }}
.muted {{ font: 400 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: #6b7683 }}
</style>
<rect x="0.5" y="0.5" rx="4.5" width="{width - 1}" height="{height - 1}" fill="#fffefe" stroke="#e4e2e2"/>
<text x="25" y="35" class="title">{escape(title)}</text>
{body}
</svg>
"""


def stats_card(s):
    rows = [
        ("Repositories", f"{s['n_public'] + s['n_private']}  ({s['n_public']} public · {s['n_private']} private)"),
        ("Lines of code written", f"{s['lines']:,}"),
        ("Commits (all time)", f"{s['commits_all']:,}"),
        ("Commits (last 12 months)", f"{s['commits_year']:,}"),
        ("Pull requests", f"{s['prs']:,}"),
        ("Issues", f"{s['issues']:,}"),
        ("Stars earned", f"{s['stars']:,}"),
    ]
    body = "".join(
        f'<text x="25" y="{68 + i * 24}" class="label">{escape(k)}:</text>'
        f'<text x="215" y="{68 + i * 24}" class="label">{escape(v)}</text>'
        for i, (k, v) in enumerate(rows)
    )
    return card(450, 245, f"{s['display']}'s GitHub Stats", body)


def langs_card(s):
    langs = s["langs"][:8]
    total = sum(w for _, w, _ in langs) or 1
    bar, x = "", 25.0
    for _, weight, color in langs:
        seg = 290 * weight / total
        bar += f'<rect x="{x:.2f}" y="52" width="{seg:.2f}" height="8" fill="{color}"/>'
        x += seg
    clip = '<clipPath id="bar"><rect x="25" y="52" width="290" height="8" rx="4"/></clipPath>'
    legend = ""
    for i, (name, weight, color) in enumerate(langs):
        col, row = i % 2, i // 2
        px, py = 25 + col * 150, 90 + row * 26
        legend += (
            f'<circle cx="{px + 5}" cy="{py - 4}" r="5" fill="{color}"/>'
            f'<text x="{px + 18}" y="{py}" class="text">{escape(name)} {100 * weight / total:.1f}%</text>'
        )
    body = f'{clip}<g clip-path="url(#bar)">{bar}</g>{legend}'
    return card(340, 245, "Most Used Languages", body)


def repos_card(s):
    shown = s["public_rows"][:6]
    body = ""
    for i, (name, lang, color, lines) in enumerate(shown):
        y = 68 + i * 24
        label = name if len(name) <= 30 else name[:29] + "…"
        body += (
            f'<circle cx="30" cy="{y - 4}" r="5" fill="{color}"/>'
            f'<text x="44" y="{y}" class="label">{escape(label)}</text>'
            f'<text x="285" y="{y}" class="text">{escape(lang)}</text>'
            f'<text x="425" y="{y}" class="muted" text-anchor="end">{compact(lines)} lines</text>'
        )
    if not shown:
        body += '<text x="25" y="68" class="text">No public repositories yet</text>'
    if s["n_private"]:
        body += (
            f'<text x="25" y="228" class="muted">+ {s["n_private"]} private repositories '
            f'· {compact(s["private_lines"])} lines · names hidden</text>'
        )
    return card(450, 245, "Repositories", body)


def main():
    contrib = contributions()
    candidates = {}
    for repo in owned_repos() + contrib["repos"]:
        if keep(repo):
            candidates[repo["nameWithOwner"]] = repo
    repos = list(candidates.values())
    stats = contributor_stats(candidates)

    weights, colors = {}, {}
    flutter = any(
        is_flutter(r["nameWithOwner"])
        for r in repos
        if any(e["node"]["name"] == "Dart" for e in r["languages"]["edges"])
    )
    lines_total = private_lines = 0
    public_rows = []
    for repo in repos:
        name = repo["nameWithOwner"]
        mine = my_additions(stats.get(name))
        if not mine and name.split("/")[0].lower() == USER.lower():
            mine = sum(e["size"] for e in repo["languages"]["edges"]) // BYTES_PER_LINE
        edges = repo["languages"]["edges"]
        size = sum(e["size"] for e in edges)
        lines_total += mine
        if repo["isPrivate"]:
            private_lines += mine
        elif edges:
            top = edges[0]["node"]
            public_rows.append((name.split("/")[1], top["name"], top["color"] or "#8b949e", mine))
        for edge in edges:
            if size and mine:
                lang = edge["node"]["name"]
                label = "Dart / Flutter" if lang == "Dart" and flutter else lang
                weights[label] = weights.get(label, 0) + mine * edge["size"] / size
                colors[label] = edge["node"]["color"] or "#8b949e"
    public_rows.sort(key=lambda row: -row[3])

    owned = [r for r in repos if r["nameWithOwner"].split("/")[0].lower() == USER.lower()]
    summary = {
        "display": os.environ.get("STATS_DISPLAY_NAME", USER),
        "n_public": sum(1 for r in repos if not r["isPrivate"]),
        "n_private": sum(1 for r in repos if r["isPrivate"]),
        "lines": lines_total,
        "private_lines": private_lines,
        "stars": sum(r["stargazerCount"] for r in owned),
        "langs": sorted(((k, v, colors[k]) for k, v in weights.items()), key=lambda t: -t[1]),
        "public_rows": public_rows,
        **{k: contrib[k] for k in ("commits_all", "commits_year", "prs", "issues")},
    }
    os.makedirs(OUT, exist_ok=True)
    for filename, render in (("stats.svg", stats_card), ("top-langs.svg", langs_card), ("repos.svg", repos_card)):
        with open(os.path.join(OUT, filename), "w", encoding="utf-8") as handle:
            handle.write(render(summary))
    print(
        f"repos: {summary['n_public']} public, {summary['n_private']} private | "
        f"lines: {lines_total:,} | flutter: {flutter} | languages: {len(summary['langs'])} | "
        f"commits: {contrib['commits_all']} all-time, {contrib['commits_year']} last 12 months"
    )


if __name__ == "__main__":
    main()
