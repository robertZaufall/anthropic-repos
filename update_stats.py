#!/usr/bin/env python3
"""
Build and refresh the Anthropic GitHub repository catalog.

The script fetches public GitHub metadata for Anthropic repositories that have
more than 200 stars and at least one commit in the last three months, groups
them into purpose clusters, and rewrites index.html.

Requirements:
  - Python 3.10+
  - A GitHub token from GITHUB_TOKEN, GH_TOKEN, or an authenticated gh CLI

Usage:
  python3 update_stats.py
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ORG = "anthropics"
MIN_STARS = 201
TOP_PER_CLUSTER = 25
TRACTION_DAYS = 30
HISTORY_DAYS = 140
GITHUB_API = "https://api.github.com"
USER_AGENT = "anthropic-repos-catalog"
AUTO_KEYWORD_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Claude", ("claude",)),
    ("Claude Code", ("claude code", "claude-code")),
    ("Claude API", ("claude api", "anthropic api", "api")),
    ("Agent SDK", ("agent sdk", "agent-sdk")),
    ("SDK", ("sdk",)),
    ("Skills", ("skills", "skill")),
    ("Plugins", ("plugins", "plugin")),
    ("Marketplace", ("marketplace",)),
    ("Cowork", ("cowork", "cwc")),
    ("Cookbooks", ("cookbooks", "recipes")),
    ("Tutorial", ("tutorial", "workshop")),
    ("Quickstart", ("quickstart", "quickstarts")),
    ("MCP", ("mcp", "model context protocol")),
    ("Legal", ("legal",)),
    ("Healthcare", ("healthcare", "health")),
    ("Financial Services", ("financial services", "finance")),
    ("Life Sciences", ("life sciences",)),
    ("Python", ("python",)),
    ("TypeScript", ("typescript", "ts")),
    ("Go", ("go", "golang")),
    ("Rust", ("rust",)),
    ("Jupyter", ("jupyter", "notebook")),
)
SUBSTRING_KEYWORDS = {
    "Claude",
    "Claude Code",
    "Agent SDK",
    "SDK",
    "Skills",
    "Plugins",
    "Cowork",
    "Python",
    "TypeScript",
    "Jupyter",
}


@dataclass(frozen=True)
class Cluster:
    key: str
    name: str
    summary: str
    accent: str
    keywords: tuple[str, ...]


CLUSTERS: tuple[Cluster, ...] = (
    Cluster(
        "claude-code-agents",
        "Claude Code & Agent Workflows",
        "Claude Code, GitHub Actions, agent SDKs, demos, and long-running automation patterns.",
        "clay",
        (
            "action",
            "agent",
            "agent sdk",
            "agent-sdk",
            "automation",
            "base-action",
            "claude code",
            "claude-code",
            "coding",
            "long-running",
            "terminal",
            "workflow",
        ),
    ),
    Cluster(
        "sdks-api-cli",
        "SDKs, APIs & CLIs",
        "Official API clients and command-line tooling for building directly on Anthropic APIs.",
        "amber",
        (
            "api",
            "anthropic cli",
            "anthropic sdk",
            "anthropic-cli",
            "anthropic-sdk",
            "cli",
            "client",
            "go",
            "java",
            "kotlin",
            "python",
            "ruby",
            "sdk",
            "typescript",
        ),
    ),
    Cluster(
        "skills-plugins",
        "Skills, Plugins & Marketplaces",
        "Reusable Skills, Claude Code plugins, marketplace directories, and packaged workflows.",
        "teal",
        (
            "cowork",
            "cwc",
            "directory",
            "knowledge work",
            "knowledge-work",
            "marketplace",
            "plugin",
            "plugins",
            "skill",
            "skills",
        ),
    ),
    Cluster(
        "learning-quickstarts",
        "Cookbooks, Quickstarts & Learning",
        "Tutorials, notebooks, quickstarts, workshops, and recipes for learning Claude patterns.",
        "blue",
        (
            "cookbook",
            "cookbooks",
            "demo",
            "demos",
            "education",
            "getting started",
            "jupyter",
            "notebook",
            "quickstart",
            "quickstarts",
            "recipe",
            "recipes",
            "tutorial",
            "workshop",
            "workshops",
        ),
    ),
    Cluster(
        "domain-solutions",
        "Industry & Domain Solutions",
        "Applied repositories for legal, healthcare, financial services, life sciences, and device workflows.",
        "violet",
        (
            "bluetooth",
            "desktop",
            "domain",
            "financial",
            "financial services",
            "healthcare",
            "industry",
            "legal",
            "life sciences",
            "makers",
        ),
    ),
    Cluster(
        "systems-protocols",
        "Systems & Protocol Libraries",
        "Lower-level Rust and protocol libraries maintained in the Anthropic organization.",
        "green",
        (
            "connect",
            "connectrpc",
            "protobuf",
            "protocol",
            "rust",
            "serialization",
            "systems",
            "zero-copy",
        ),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Anthropic GitHub repository catalog.")
    parser.add_argument("--file", default="index.html", help="HTML file to write")
    parser.add_argument("--history", default="stats_history.json", help="history JSON file")
    parser.add_argument("--org", default=ORG, help="GitHub organization")
    parser.add_argument("--min-stars", type=int, default=MIN_STARS, help="minimum stars")
    parser.add_argument("--top-per-cluster", type=int, default=TOP_PER_CLUSTER)
    parser.add_argument("--traction-days", type=int, default=TRACTION_DAYS)
    parser.add_argument("--months", type=int, default=3, help="recency window in calendar months")
    parser.add_argument("--skip-commit-counts", action="store_true", help="skip per-repo commit counts")
    return parser.parse_args()


def subtract_months(dt: datetime, months: int) -> datetime:
    month = dt.month - months
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return dt.replace(year=year, month=month, day=min(dt.day, days_in_month[month - 1]))


def get_token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        token = result.stdout.strip()
        return token or None
    return None


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def request(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(url, headers=headers)
        for attempt in range(4):
            try:
                with urlopen(req, timeout=60) as response:
                    body = response.read().decode("utf-8")
                    response_headers = {k.lower(): v for k, v in response.headers.items()}
                    return json.loads(body), response_headers
            except HTTPError as exc:
                if exc.code in (403, 429) and attempt < 3:
                    reset = exc.headers.get("X-RateLimit-Reset")
                    if reset and reset.isdigit():
                        delay = max(2, min(60, int(reset) - int(time.time()) + 2))
                    else:
                        delay = 3 * (attempt + 1)
                    time.sleep(delay)
                    continue
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"GitHub API failed for {url}: HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"GitHub API failed for {url}: {exc}") from exc
        raise RuntimeError(f"GitHub API failed for {url}")


def parse_link_header(link: str | None) -> dict[str, str]:
    links: dict[str, str] = {}
    if not link:
        return links
    for part in link.split(","):
        m = re.search(r'<([^>]+)>;\s*rel="([^"]+)"', part.strip())
        if m:
            links[m.group(2)] = m.group(1)
    return links


def link_last_page(link: str | None, fallback_len: int) -> int:
    links = parse_link_header(link)
    last = links.get("last")
    if not last:
        return fallback_len
    m = re.search(r"[?&]page=(\d+)", last)
    return int(m.group(1)) if m else fallback_len


def iso_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fmt_number(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    value = int(value)
    if value >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M"
        return text.replace(".0M", "M")
    if value >= 100_000:
        return f"{value / 1000:.0f}k"
    if value >= 1_000:
        text = f"{value / 1000:.1f}k"
        return text.replace(".0k", "k")
    return str(value)


def fmt_date(value: str) -> str:
    if not value:
        return "n/a"
    return iso_to_datetime(value).strftime("%b %d, %Y")


def read_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"snapshots": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"snapshots": []}
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        return {"snapshots": []}
    return data


def write_history(path: Path, history: dict[str, Any], repos: list[dict[str, Any]], now: datetime) -> None:
    cutoff = now.date() - timedelta(days=HISTORY_DAYS)
    snapshots = []
    for snap in history.get("snapshots", []):
        try:
            snap_date = datetime.strptime(snap["date"], "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            continue
        if snap_date >= cutoff:
            snapshots.append(snap)

    today = now.strftime("%Y-%m-%d")
    snapshots = [snap for snap in snapshots if snap.get("date") != today]
    snapshots.append(
        {
            "date": today,
            "repos": {
                repo["full_name"]: {
                    "stars": repo["stars"],
                    "forks": repo["forks"],
                    "pushed_at": repo["pushed_at"],
                }
                for repo in repos
            },
        }
    )
    snapshots.sort(key=lambda snap: snap["date"])
    path.write_text(json.dumps({"snapshots": snapshots}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def baseline_snapshot(history: dict[str, Any], now: datetime, days: int) -> dict[str, Any] | None:
    target = now.date() - timedelta(days=days)
    candidates = []
    for snap in history.get("snapshots", []):
        try:
            snap_date = datetime.strptime(snap["date"], "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            continue
        if snap_date <= target:
            candidates.append((snap_date, snap))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def fetch_repositories(client: GitHubClient, org: str, min_stars: int, pushed_cutoff: datetime) -> list[dict[str, Any]]:
    query = f"org:{org} stars:>={min_stars} pushed:>={pushed_cutoff.date().isoformat()} fork:false archived:false"
    repos = fetch_search_repositories(client, query, pushed_cutoff)
    repos = [repo for repo in repos if repo["stars"] >= min_stars]
    repos.sort(key=lambda repo: repo["stars"], reverse=True)
    return repos


def fetch_search_repositories(client: GitHubClient, query: str, pushed_cutoff: datetime) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        data, _ = client.request(
            "/search/repositories",
            {
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            if iso_to_datetime(item["pushed_at"]) < pushed_cutoff:
                continue
            repos.append(normalize_repo(item))
        if len(items) < 100 or page >= 10:
            break
        page += 1
    return repos


def normalize_repo(item: dict[str, Any]) -> dict[str, Any]:
    license_info = item.get("license") or {}
    license_spdx = license_info.get("spdx_id") if isinstance(license_info, dict) else None
    if not license_spdx or license_spdx == "NOASSERTION":
        license_spdx = "n/a"
    return {
        "name": item.get("name", ""),
        "full_name": item.get("full_name", ""),
        "url": item.get("html_url", ""),
        "description": item.get("description") or "",
        "language": item.get("language") or "Mixed",
        "topics": item.get("topics") or [],
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "open_issues": int(item.get("open_issues_count") or 0),
        "pushed_at": item.get("pushed_at") or "",
        "updated_at": item.get("updated_at") or "",
        "created_at": item.get("created_at") or "",
        "license": license_spdx,
        "commits": None,
        "commits_30d": None,
        "commits_window": None,
    }


def enrich_commit_counts(client: GitHubClient, repos: list[dict[str, Any]], since_30d: datetime, since_window: datetime) -> None:
    for index, repo in enumerate(repos, start=1):
        full_name = repo["full_name"]
        print(f"[{index:03d}/{len(repos):03d}] commits {full_name}")
        repo["commits"] = commit_count(client, full_name)
        repo["commits_30d"] = commit_count(client, full_name, since=since_30d)
        repo["commits_window"] = commit_count(client, full_name, since=since_window)


def commit_count(client: GitHubClient, full_name: str, since: datetime | None = None) -> int:
    params: dict[str, Any] = {"per_page": 1}
    if since:
        params["since"] = since.isoformat().replace("+00:00", "Z")
    try:
        data, headers = client.request(f"/repos/{full_name}/commits", params)
    except RuntimeError as exc:
        print(f"  warning: {exc}", file=sys.stderr)
        return 0
    if not isinstance(data, list) or not data:
        return 0
    return link_last_page(headers.get("link"), len(data))


def cluster_repo(repo: dict[str, Any]) -> Cluster:
    haystack = " ".join(
        [
            repo["name"],
            repo["description"],
            repo["language"],
            " ".join(repo.get("topics") or []),
        ]
    ).lower()
    clusters_by_key = {cluster.key: cluster for cluster in CLUSTERS}
    override_terms = {
        "domain-solutions": (
            "financial-services",
            "healthcare",
            "life-sciences",
            "claude-for-legal",
            "legal",
            "desktop-buddy",
        ),
        "systems-protocols": (
            "buffa",
            "connect-rust",
            "connectrpc",
            "protobuf",
            "zero-copy",
        ),
        "sdks-api-cli": (
            "anthropic-sdk",
            "anthropic-cli",
        ),
        "skills-plugins": (
            "skills",
            "plugins",
            "marketplace",
            "knowledge-work",
        ),
        "learning-quickstarts": (
            "cookbooks",
            "quickstarts",
            "tutorial",
            "workshops",
        ),
        "claude-code-agents": (
            "claude-code",
            "agent-sdk",
            "long-running-agents",
            "base-action",
        ),
    }
    for key, terms in override_terms.items():
        if any(term_matches(haystack, term) for term in terms):
            return clusters_by_key[key]

    scores: list[tuple[int, int, Cluster]] = []
    for order, cluster in enumerate(CLUSTERS):
        score = 0
        for keyword in cluster.keywords:
            if keyword in haystack:
                score += 3 if keyword in repo["name"].lower() else 1
        scores.append((score, -order, cluster))
    best = max(scores, key=lambda item: (item[0], item[1]))
    if best[0] == 0:
        return CLUSTERS[0]
    return best[2]


def term_matches(haystack: str, term: str) -> bool:
    if " " in term or "-" in term:
        return term in haystack
    pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def add_derived_fields(repos: list[dict[str, Any]], history: dict[str, Any], now: datetime, traction_days: int) -> None:
    baseline = baseline_snapshot(history, now, traction_days)
    baseline_repos = baseline.get("repos", {}) if baseline else {}
    for repo in repos:
        cluster = cluster_repo(repo)
        repo["cluster_key"] = cluster.key
        repo["cluster_name"] = cluster.name
        old = baseline_repos.get(repo["full_name"])
        if old:
            repo["stars_delta_30d"] = max(0, repo["stars"] - int(old.get("stars", repo["stars"])))
            repo["forks_delta_30d"] = max(0, repo["forks"] - int(old.get("forks", repo["forks"])))
            repo["has_history_baseline"] = True
        else:
            repo["stars_delta_30d"] = None
            repo["forks_delta_30d"] = None
            repo["has_history_baseline"] = False
        repo["traction_score"] = traction_score(repo, now)


def traction_score(repo: dict[str, Any], now: datetime) -> float:
    commits = int(repo.get("commits_30d") or 0)
    audience = (math.log10(max(repo["stars"], 10)) * 24) + (math.log10(max(repo["forks"], 1) + 1) * 9)
    established = math.sqrt(max(repo["stars"], 1)) * 2.25
    if repo.get("has_history_baseline"):
        star_delta = int(repo.get("stars_delta_30d") or 0)
        fork_delta = int(repo.get("forks_delta_30d") or 0)
        return (star_delta * 10) + (fork_delta * 4) + min(commits, 350) + established + audience

    last_push = iso_to_datetime(repo["pushed_at"])
    age_days = max(0, (now - last_push).days)
    freshness = max(0.0, 1.0 - (age_days / TRACTION_DAYS))
    return min(commits, 350) + established + (freshness * audience)


def grouped_repos(repos: list[dict[str, Any]], top_n: int) -> dict[str, list[dict[str, Any]]]:
    groups = {cluster.key: [] for cluster in CLUSTERS}
    for repo in repos:
        groups[repo["cluster_key"]].append(repo)
    for key in groups:
        groups[key].sort(key=lambda repo: repo["stars"], reverse=True)
        groups[key] = groups[key][:top_n]
    return groups


def language_class(language: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", language.lower()).strip("-")
    known = {
        "c": "lang-c",
        "c#": "lang-csharp",
        "c++": "lang-cpp",
        "go": "lang-go",
        "jupyter notebook": "lang-jupyter",
        "kotlin": "lang-kotlin",
        "python": "lang-python",
        "ruby": "lang-ruby",
        "rust": "lang-rust",
        "shell": "lang-shell",
        "typescript": "lang-typescript",
    }
    return known.get(language.lower(), f"lang-{normalized or 'generic'}")


def detected_keywords(repo: dict[str, Any]) -> list[str]:
    haystack = f"{repo['name']} {repo['description']} {' '.join(repo.get('topics') or [])}".lower()
    found = []
    for label, aliases in AUTO_KEYWORD_TERMS:
        if label in SUBSTRING_KEYWORDS:
            matched = any(alias in haystack for alias in aliases)
        else:
            matched = any(term_matches(haystack, alias) for alias in aliases)
        if matched:
            found.append(label)
    return found


def keyword_buttons(repo: dict[str, Any]) -> str:
    keywords: list[str] = []
    seen: set[str] = set()
    for keyword in [*detected_keywords(repo), *(repo.get("topics") or [])]:
        key = keyword.lower()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)

    return "".join(
        (
            f'<button class="keyword-button" type="button" '
            f'data-filter-keyword="{escape(keyword)}" title="Filter by {escape(keyword)}">'
            f'{escape(keyword)}</button>'
        )
        for keyword in keywords[:12]
    )


def repo_row(repo: dict[str, Any], rank: int | None = None, include_cluster: bool = False, include_traction: bool = False) -> str:
    pushed_ts = int(iso_to_datetime(repo["pushed_at"]).timestamp()) if repo.get("pushed_at") else 0
    score = int(repo.get("traction_score") or 0)
    desc = escape(repo["description"] or "No description provided.")
    topic_html = keyword_buttons(repo)
    owner = repo["full_name"].split("/", 1)[0]
    avatar = f"https://github.com/{escape(owner)}.png?size=32"
    name_cell = (
        f'<div class="repo-name"><img src="{avatar}" alt="" class="avatar">'
        f'<a href="{escape(repo["url"])}" target="_blank" rel="noreferrer">{escape(repo["name"])}</a></div>'
        f'<a class="repo-slug" href="{escape(repo["url"])}" target="_blank" rel="noreferrer">{escape(repo["full_name"])}</a>'
    )
    language = escape(repo["language"])
    cells = []
    if rank is not None:
        cells.append(f'<td class="rank-cell">{rank}</td>')
    cells.append(f"<td>{name_cell}</td>")
    cells.append(f'<td><span class="tag {language_class(repo["language"])}">{language}</span></td>')
    cells.append(description_cell(desc, topic_html))
    if include_cluster:
        cells.append(cluster_cell(repo, linked=include_traction))
    cells.append(github_cell(repo, include_activity=include_traction))
    return (
        f'<tr data-stars="{repo["stars"]}" data-pushed="{pushed_ts}" data-score="{score}" '
        f'data-name="{escape(repo["name"].lower())}" data-cluster="{escape(repo["cluster_key"])}">\n'
        + "\n".join(f"  {cell}" for cell in cells)
        + "\n</tr>"
    )


def description_cell(desc: str, topic_html: str) -> str:
    return f'<td class="description-cell">{desc}<div class="topic-row">{topic_html}</div></td>'


def cluster_cell(repo: dict[str, Any], linked: bool = False) -> str:
    name = escape(repo["cluster_name"])
    if linked:
        return f'<td><a class="cluster-pill cluster-link" href="#cluster-{escape(repo["cluster_key"])}">{name}</a></td>'
    return f'<td><span class="cluster-pill">{name}</span></td>'


def github_cell(repo: dict[str, Any], include_activity: bool = False) -> str:
    activity = ""
    if include_activity:
        pct = max(4, min(100, int(repo.get("traction_pct", 0))))
        activity = f"""
    <div class="activity-block" aria-label="Recent activity score {int(repo["traction_score"])}">
      <div class="activity-bar"><span style="width: {pct}%"></span></div>
      <div class="activity-meta">
        <span>{fmt_number(repo.get("commits_30d"))} commits / 30d</span>
        <span>score {int(repo["traction_score"])}</span>
      </div>
    </div>"""
    return f"""<td class="github-cell">
    <div class="gh-stats">
      <span class="star-count">stars {fmt_number(repo["stars"])}</span>
      <span class="fork-count">forks {fmt_number(repo["forks"])}</span>
      <span class="commit-count">commits {fmt_number(repo.get("commits"))}</span>
    </div>
    <span class="last-updated">pushed {fmt_date(repo["pushed_at"])}</span>
    {activity}
  </td>"""


def section_table(cluster: Cluster, repos: list[dict[str, Any]], top_n: int) -> str:
    rows = "\n".join(repo_row(repo) for repo in repos)
    return f"""
<section class="repo-section" id="cluster-{cluster.key}" data-section>
  <div class="section-header">
    <div>
      <h2><span class="section-mark section-mark-{cluster.accent}"></span>{escape(cluster.name)}</h2>
      <p>{escape(cluster.summary)} Showing up to {top_n} repositories by stars.</p>
    </div>
    <span class="count-pill">{len(repos)} repos</span>
  </div>
  <div class="table-wrap">
    <table>
      <colgroup>
        <col class="col-repository">
        <col class="col-language">
        <col class="col-description">
        <col class="col-github">
      </colgroup>
      <thead>
        <tr>
          <th>Repository</th>
          <th>Language</th>
          <th>Description</th>
          <th>GitHub</th>
        </tr>
      </thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
</section>
"""


def traction_table(repos: list[dict[str, Any]]) -> str:
    top = sorted(repos, key=lambda repo: repo["traction_score"], reverse=True)[:25]
    max_score = max((repo["traction_score"] for repo in top), default=1) or 1
    for repo in top:
        repo["traction_pct"] = round((repo["traction_score"] / max_score) * 100)
    rows = "\n".join(
        repo_row(repo, rank=rank, include_cluster=True, include_traction=True)
        for rank, repo in enumerate(top, start=1)
    )
    return f"""
<section class="repo-section traction-section" id="section-traction" data-section>
  <div class="section-header">
    <div>
      <h2><span class="section-mark section-mark-clay"></span>Wide Audience With Fresh Traction</h2>
      <p>Ranked by recent commits, freshness, stars, and forks so active projects with broad audiences rise first.</p>
    </div>
    <span class="count-pill">Top {len(top)}</span>
  </div>
  <div class="table-wrap">
    <table>
      <colgroup>
        <col class="col-rank">
        <col class="col-traction-repository">
        <col class="col-traction-language">
        <col class="col-traction-description">
        <col class="col-traction-cluster">
        <col class="col-traction-github">
      </colgroup>
      <thead>
        <tr>
          <th>#</th>
          <th>Repository</th>
          <th>Language</th>
          <th>Description</th>
          <th>Cluster</th>
          <th>GitHub</th>
        </tr>
      </thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
</section>
"""


def jump_links() -> str:
    cluster_items = [
        ("section-traction", "Fresh Traction"),
        *[(f"cluster-{cluster.key}", cluster.name) for cluster in CLUSTERS],
    ]
    links = "\n".join(
        f'<a href="#{escape(anchor)}">{escape(label)}</a>'
        for anchor, label in cluster_items
    )
    return f'<div class="jump-group"><span class="jump-group-label">Clusters</span>{links}</div>'


def render_html(
    repos: list[dict[str, Any]],
    now: datetime,
    pushed_cutoff: datetime,
    args: argparse.Namespace,
) -> str:
    groups = grouped_repos(repos, args.top_per_cluster)
    total_stars = sum(repo["stars"] for repo in repos)
    total_commits_30d = sum(int(repo.get("commits_30d") or 0) for repo in repos)
    total_window_commits = sum(int(repo.get("commits_window") or 0) for repo in repos)
    sections = "\n".join(section_table(cluster, groups[cluster.key], args.top_per_cluster) for cluster in CLUSTERS)
    traction = traction_table(repos)
    updated = now.strftime("%Y-%m-%d %H:%M UTC")
    cutoff = pushed_cutoff.strftime("%Y-%m-%d")
    nav_links = jump_links()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script>
  var p = location.pathname;
  if (p.charAt(p.length - 1) !== '/') p += '/';
  document.write('<base href="' + p + '">');
</script>
<title>Anthropic GitHub Repository Atlas</title>
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {{
    theme: {{
      extend: {{
        colors: {{
          anthropic: {{
            ink: '#14110f',
            panel: '#1d1814',
            clay: '#d97757',
            cream: '#f4efe7',
            muted: '#b8a99d',
          }},
        }},
        fontFamily: {{
          sans: ['Inter', 'system-ui', 'sans-serif'],
          mono: ['JetBrains Mono', 'monospace'],
        }},
      }},
    }},
  }};
</script>
<style>
  :root {{
    --bg: #14110f;
    --surface: #1b1714;
    --surface2: #241e19;
    --surface3: #302821;
    --border: #46382f;
    --text: #f4efe7;
    --text-muted: #b8a99d;
    --anthropic: #d97757;
    --clay: #d97757;
    --amber: #f1c27b;
    --teal: #7ccfc3;
    --blue: #83b5ff;
    --violet: #c5a7ff;
    --green: #9ad47c;
    --red: #ff947d;
  }}

  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    margin: 0;
    min-height: 100vh;
    background:
      linear-gradient(rgba(244, 239, 231, 0.028) 1px, transparent 1px),
      linear-gradient(90deg, rgba(244, 239, 231, 0.022) 1px, transparent 1px),
      radial-gradient(circle at 84% -8%, rgba(217, 119, 87, 0.24), transparent 30rem),
      radial-gradient(circle at 9% 8%, rgba(124, 207, 195, 0.12), transparent 28rem),
      linear-gradient(180deg, #18120f 0%, var(--bg) 42rem);
    background-size: 56px 56px, 56px 56px, auto, auto, auto;
    color: var(--text);
    font-family: 'Inter', system-ui, sans-serif;
  }}
  a {{ color: inherit; }}
  .shell {{ width: min(1480px, calc(100vw - 32px)); margin: 0 auto; }}
  .logo-box {{
    width: 40px;
    height: 40px;
    display: inline-grid;
    place-items: center;
    border-radius: 8px;
    background: linear-gradient(135deg, #f1d7c7, var(--anthropic) 55%, #9a4b37);
    color: #14110f;
    font: 900 15px 'JetBrains Mono', monospace;
    box-shadow: 0 0 28px rgba(217, 119, 87, 0.32);
  }}
  .metric-card {{
    min-width: 148px;
    padding: 13px 15px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(29, 24, 20, 0.82);
    box-shadow: inset 0 1px 0 rgba(244, 239, 231, 0.045);
  }}
  .metric-card strong {{
    display: block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 18px;
    color: var(--text);
  }}
  .metric-card span {{
    display: block;
    margin-top: 3px;
    color: var(--text-muted);
    font: 700 11px 'JetBrains Mono', monospace;
  }}
  .jump-group {{
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    align-items: center;
    padding: 8px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: rgba(29, 24, 20, 0.56);
  }}
  .jump-group-label {{
    padding: 0 4px 0 2px;
    color: var(--text-muted);
    font: 700 10px 'JetBrains Mono', monospace;
    letter-spacing: 0.7px;
    text-transform: uppercase;
  }}
  .jump-group a {{
    display: inline-flex;
    align-items: center;
    border: 1px solid rgba(217, 119, 87, 0.32);
    border-radius: 7px;
    padding: 8px 10px;
    background: rgba(217, 119, 87, 0.07);
    color: var(--text);
    text-decoration: none;
    font: 700 11px 'JetBrains Mono', monospace;
    transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
  }}
  .jump-group a:hover {{
    border-color: var(--anthropic);
    color: #ffd8c5;
    background: rgba(217, 119, 87, 0.13);
  }}
  .search-panel {{
    display: grid;
    grid-template-columns: auto minmax(240px, 1fr) auto auto auto auto auto;
    gap: 10px;
    align-items: center;
    margin-bottom: 30px;
    padding: 14px 16px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background:
      radial-gradient(circle at top right, rgba(217, 119, 87, 0.16), transparent 24rem),
      linear-gradient(180deg, rgba(36, 30, 25, 0.98), rgba(25, 21, 18, 0.98));
  }}
  .search-label,
  .sort-label {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-muted);
    white-space: nowrap;
  }}
  .search-input {{
    width: 100%;
    min-width: 0;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(16, 13, 11, 0.95);
    color: var(--text);
    outline: none;
    padding: 12px 14px;
    font: 500 14px 'Inter', system-ui, sans-serif;
  }}
  .search-input:focus {{
    border-color: rgba(217, 119, 87, 0.82);
    box-shadow: 0 0 0 3px rgba(217, 119, 87, 0.14);
  }}
  .button {{
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(16, 13, 11, 0.95);
    color: var(--text);
    padding: 12px 13px;
    font: 700 12px 'JetBrains Mono', monospace;
    cursor: pointer;
    white-space: nowrap;
  }}
  .button:hover,
  .button.active {{
    border-color: var(--anthropic);
    color: #ffd8c5;
  }}
  .button:disabled {{
    opacity: 0.55;
    cursor: default;
  }}
  .repo-section {{ margin-top: 34px; }}
  .section-header {{
    display: flex;
    justify-content: space-between;
    gap: 18px;
    align-items: end;
    margin-bottom: 14px;
  }}
  .section-header h2 {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    font-family: 'JetBrains Mono', monospace;
    font-size: 20px;
    line-height: 1.25;
    letter-spacing: 0;
  }}
  .section-header p {{
    margin: 6px 0 0;
    color: var(--text-muted);
    font-size: 13px;
    line-height: 1.5;
  }}
  .section-mark {{
    width: 10px;
    height: 20px;
    border-radius: 2px;
    background: var(--anthropic);
    display: inline-block;
  }}
  .section-mark-clay {{ background: var(--clay); }}
  .section-mark-amber {{ background: var(--amber); }}
  .section-mark-teal {{ background: var(--teal); }}
  .section-mark-blue {{ background: var(--blue); }}
  .section-mark-violet {{ background: var(--violet); }}
  .section-mark-green {{ background: var(--green); }}
  .count-pill,
  .cluster-pill {{
    display: inline-block;
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 4px 8px;
    color: var(--text-muted);
    background: rgba(16, 13, 11, 0.48);
    font: 600 11px 'JetBrains Mono', monospace;
  }}
  .count-pill {{ white-space: nowrap; }}
  .cluster-pill {{
    white-space: normal;
    line-height: 1.35;
  }}
  .cluster-link {{
    text-decoration: none;
    transition: border-color 0.15s ease, color 0.15s ease;
  }}
  .cluster-link:hover {{
    color: #ffd8c5;
    border-color: var(--anthropic);
  }}
  .table-wrap {{
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    scrollbar-color: #5b463a #17120f;
    scrollbar-width: thin;
  }}
  table {{
    width: 100%;
    min-width: 1120px;
    border-collapse: collapse;
    font-size: 13px;
    table-layout: fixed;
  }}
  .traction-section table {{ min-width: 1240px; }}
  .col-rank {{ width: 4%; }}
  .col-repository {{ width: 22%; }}
  .col-language {{ width: 11%; }}
  .col-description {{ width: 47%; }}
  .col-github {{ width: 20%; }}
  .col-traction-repository {{ width: 17%; }}
  .col-traction-language {{ width: 9%; }}
  .col-traction-description {{ width: 42%; }}
  .col-traction-cluster {{ width: 13%; }}
  .col-traction-github {{ width: 15%; }}
  th {{
    position: sticky;
    top: 0;
    z-index: 1;
    padding: 13px 14px;
    text-align: left;
    background: var(--surface2);
    color: var(--text-muted);
    border-bottom: 1px solid var(--border);
    font: 700 11px 'JetBrains Mono', monospace;
    text-transform: uppercase;
    letter-spacing: 0.7px;
  }}
  td {{
    padding: 13px 14px;
    vertical-align: top;
    border-bottom: 1px solid var(--border);
    line-height: 1.5;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: rgba(217, 119, 87, 0.055); }}
  .repo-name {{
    display: flex;
    align-items: center;
    gap: 8px;
    font: 700 14px 'JetBrains Mono', monospace;
    white-space: nowrap;
  }}
  .repo-name a {{
    color: var(--text);
    text-decoration: none;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .repo-name a:hover,
  .repo-slug:hover {{
    color: #ffd8c5;
    text-decoration: underline;
  }}
  .repo-slug {{
    display: block;
    margin-top: 4px;
    color: var(--text-muted);
    text-decoration: none;
    font: 500 11px 'JetBrains Mono', monospace;
    overflow-wrap: anywhere;
  }}
  .avatar {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    background: var(--surface3);
    flex: 0 0 auto;
  }}
  .tag,
  .keyword-button {{
    display: inline-block;
    border-radius: 4px;
    padding: 2px 7px;
    font: 700 10px 'JetBrains Mono', monospace;
    background: rgba(244, 239, 231, 0.1);
    color: var(--text-muted);
  }}
  .tag {{
    font-size: 11px;
    white-space: normal;
    line-height: 1.35;
  }}
  .lang-python {{ background: rgba(154, 212, 124, 0.14); color: var(--green); }}
  .lang-typescript {{ background: rgba(131, 181, 255, 0.14); color: var(--blue); }}
  .lang-jupyter {{ background: rgba(241, 194, 123, 0.14); color: var(--amber); }}
  .lang-shell {{ background: rgba(217, 119, 87, 0.16); color: var(--clay); }}
  .lang-go {{ background: rgba(124, 207, 195, 0.14); color: var(--teal); }}
  .lang-rust {{ background: rgba(255, 148, 125, 0.14); color: var(--red); }}
  .lang-kotlin, .lang-ruby, .lang-csharp, .lang-cpp {{ background: rgba(197, 167, 255, 0.15); color: var(--violet); }}
  .topic-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 7px;
  }}
  .keyword-button {{
    border: 0;
    cursor: pointer;
    text-align: left;
    appearance: none;
    -webkit-appearance: none;
    transition: color 0.15s ease, background 0.15s ease;
  }}
  .keyword-button:hover,
  .keyword-button:focus-visible {{
    color: #ffd8c5;
    background: rgba(217, 119, 87, 0.16);
    outline: none;
  }}
  .description-cell {{
    color: var(--text);
    max-width: 54rem;
  }}
  .github-cell {{ min-width: 210px; }}
  .gh-stats {{
    display: flex;
    flex-wrap: wrap;
    gap: 7px 10px;
    align-items: center;
  }}
  .star-count,
  .fork-count,
  .commit-count,
  .rank-cell {{
    font-family: 'JetBrains Mono', monospace;
  }}
  .star-count {{
    color: var(--amber);
    font-weight: 800;
    font-size: 13px;
  }}
  .fork-count,
  .commit-count {{
    color: var(--text-muted);
    font-size: 11px;
    font-weight: 600;
  }}
  .rank-cell {{
    color: var(--anthropic);
    font-weight: 800;
    white-space: nowrap;
  }}
  .last-updated {{
    display: block;
    margin-top: 6px;
    color: var(--text-muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
  }}
  .activity-block {{ margin-top: 8px; }}
  .activity-bar {{
    height: 6px;
    border-radius: 999px;
    background: rgba(244, 239, 231, 0.14);
    overflow: hidden;
  }}
  .activity-bar span {{
    display: block;
    height: 100%;
    border-radius: inherit;
    background: linear-gradient(90deg, var(--anthropic), var(--amber), var(--teal));
  }}
  .activity-meta {{
    display: flex;
    justify-content: space-between;
    gap: 8px;
    margin-top: 5px;
    color: var(--text-muted);
    font: 600 10px 'JetBrains Mono', monospace;
  }}
  .hidden {{ display: none !important; }}
  .footer {{
    margin-top: 30px;
    color: var(--text-muted);
    font: 500 11px 'JetBrains Mono', monospace;
    line-height: 1.7;
  }}
  .footer a {{ color: #ffd8c5; text-decoration: none; }}
  .footer a:hover {{ text-decoration: underline; }}
  @media (max-width: 900px) {{
    .shell {{ width: min(100vw - 28px, 1480px); }}
    .search-panel {{ grid-template-columns: 1fr; }}
    .button, .search-label, .sort-label {{ width: 100%; }}
    .section-header {{ display: block; }}
    .count-pill {{ margin-top: 10px; }}
  }}
</style>
</head>
<body class="min-h-screen py-8 md:py-10">
  <header class="shell mb-6">
    <h1 class="m-0 flex items-center gap-3 font-mono text-[26px] font-bold leading-tight text-anthropic-cream md:text-[30px]">
      <span class="logo-box">AN</span>Anthropic GitHub Repository Atlas
    </h1>
    <p class="mt-3 max-w-4xl text-sm leading-6 text-anthropic-muted">
      Searchable snapshot of public Anthropic repositories from
      <a class="text-[#ffd8c5] underline decoration-[#d97757]/40 underline-offset-4" href="https://github.com/orgs/anthropics/repositories" target="_blank" rel="noreferrer">github.com/anthropics</a>.
      Included repositories have more than 200 stars and at least one commit within the last three months, since {cutoff}.
    </p>
    <div class="mt-5 flex flex-wrap gap-2.5" aria-label="Catalog summary">
      <div class="metric-card"><strong>{len(repos)}</strong><span>qualified repos</span></div>
      <div class="metric-card"><strong>{fmt_number(total_stars)}</strong><span>combined stars</span></div>
      <div class="metric-card"><strong>{fmt_number(total_commits_30d)}</strong><span>commits in {args.traction_days}d</span></div>
      <div class="metric-card"><strong>{fmt_number(total_window_commits)}</strong><span>commits since {cutoff}</span></div>
      <div class="metric-card"><strong>{len(CLUSTERS)}</strong><span>purpose clusters</span></div>
    </div>
    <nav class="mt-5 flex flex-wrap gap-3" aria-label="Section links">
      {nav_links}
    </nav>
  </header>

  <div class="shell search-panel" role="search">
    <div class="search-label">Search</div>
    <input id="global-table-search" class="search-input" type="search" autocomplete="off" spellcheck="false" placeholder="repo, topic, language, description">
    <button id="global-search-clear" class="button" type="button" disabled>Clear</button>
    <div class="sort-label">Sort by</div>
    <button id="sort-score" class="button active" type="button">Traction</button>
    <button id="sort-stars" class="button" type="button">Stars</button>
    <button id="sort-fresh" class="button" type="button">Freshness</button>
  </div>

  <main class="shell">
{traction}
{sections}
  </main>

  <footer class="shell footer">
    <p style="margin-bottom: 10px;">Useful links:
      <a class="gh-link" href="https://www.anthropic.com/" target="_blank" rel="noreferrer" style="display: inline; margin-left: 6px;">Anthropic</a>
      <span style="margin: 0 4px;">/</span>
      <a class="gh-link" href="https://claude.ai/" target="_blank" rel="noreferrer" style="display: inline;">Claude</a>
      <span style="margin: 0 4px;">/</span>
      <a class="gh-link" href="https://docs.claude.com/" target="_blank" rel="noreferrer" style="display: inline;">Claude Docs</a>
      <span style="margin: 0 4px;">/</span>
      <a class="gh-link" href="https://www.anthropic.com/engineering" target="_blank" rel="noreferrer" style="display: inline;">Engineering</a>
    </p>
    <p style="text-align: center;">Generated from the GitHub API. Last updated: <span id="last-updated-date">{updated}</span></p>
  </footer>

<script>
  const searchInput = document.getElementById('global-table-search');
  const clearButton = document.getElementById('global-search-clear');
  const sortScore = document.getElementById('sort-score');
  const sortStars = document.getElementById('sort-stars');
  const sortFresh = document.getElementById('sort-fresh');

  function applySearch() {{
    const query = searchInput.value.trim().toLowerCase();
    let visibleRows = 0;
    document.querySelectorAll('[data-section]').forEach(section => {{
      let sectionVisible = false;
      section.querySelectorAll('tbody tr').forEach(row => {{
        const match = !query || row.textContent.toLowerCase().includes(query);
        row.classList.toggle('hidden', !match);
        if (match) {{
          sectionVisible = true;
          visibleRows += 1;
        }}
      }});
      section.classList.toggle('hidden', !sectionVisible);
    }});
    clearButton.disabled = !query;
    if (query && visibleRows === 0) {{
      document.querySelectorAll('[data-section], tbody tr').forEach(el => el.classList.remove('hidden'));
    }}
  }}

  function sortTables(kind) {{
    const attr = kind === 'fresh' ? 'pushed' : kind === 'score' ? 'score' : 'stars';
    document.querySelectorAll('tbody').forEach(tbody => {{
      if (kind === 'score' && !tbody.closest('.traction-section')) return;
      const rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort((a, b) => Number(b.dataset[attr] || 0) - Number(a.dataset[attr] || 0));
      rows.forEach(row => tbody.appendChild(row));
    }});
    sortScore.classList.toggle('active', kind === 'score');
    sortStars.classList.toggle('active', kind === 'stars');
    sortFresh.classList.toggle('active', kind === 'fresh');
    applySearch();
  }}

  searchInput.addEventListener('input', applySearch);
  clearButton.addEventListener('click', () => {{
    searchInput.value = '';
    applySearch();
    searchInput.focus();
  }});
  document.addEventListener('click', event => {{
    const keyword = event.target.closest('[data-filter-keyword]');
    if (!keyword) return;
    const value = keyword.dataset.filterKeyword || keyword.textContent.trim();
    searchInput.value = searchInput.value.trim().toLowerCase() === value.trim().toLowerCase() ? '' : value;
    applySearch();
    searchInput.focus({{ preventScroll: true }});
  }});
  sortScore.addEventListener('click', () => sortTables('score'));
  sortStars.addEventListener('click', () => sortTables('stars'));
  sortFresh.addEventListener('click', () => sortTables('fresh'));
  sortTables('score');
</script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    now = datetime.now(timezone.utc)
    pushed_cutoff = subtract_months(now, args.months)
    traction_cutoff = now - timedelta(days=args.traction_days)

    token = get_token()
    if not token:
        print("warning: no GitHub token found; unauthenticated API rate limits may apply", file=sys.stderr)
    client = GitHubClient(token)
    repos = fetch_repositories(client, args.org, args.min_stars, pushed_cutoff)
    if not args.skip_commit_counts:
        enrich_commit_counts(client, repos, traction_cutoff, pushed_cutoff)
        repos = [repo for repo in repos if int(repo.get("commits_window") or 0) > 0]
    else:
        for repo in repos:
            repo["commits_window"] = None

    history_path = Path(args.history)
    history = read_history(history_path)
    add_derived_fields(repos, history, now, args.traction_days)
    html = render_html(repos, now, pushed_cutoff, args)
    Path(args.file).write_text(html, encoding="utf-8")
    write_history(history_path, history, repos, now)
    print(f"Wrote {args.file} with {len(repos)} Anthropic repositories")
    print(f"Wrote {args.history}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
