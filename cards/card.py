#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["tyro", "fonttools[woff]"]
# ///
"""Render the activity card under the profile README, as one SVG.

Reads max's contribution history through `gh` (GraphQL, read-only; `GH_TOKEN`
decides what it can see) into a JSON cache, then draws three sections: activity
per month since the account began, a summary, and the last 90 days.

A repo is named on the card only if it is public and lives in max's own
namespaces, or is a public project outside them. Work, school and private repos
are counted, never named, and the cache holds only their totals.

Examples:

    uv run cards/card.py --refresh     # fetch, then render cards/activity.svg
    uv run cards/card.py               # render from the cache
"""

import base64
import collections
import datetime as dt
import fnmatch
import html
import io
import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import tyro
from fontTools import subset
from fontTools.ttLib import TTFont

HERE = Path(__file__).parent
LOGIN = "MaxWolf-01"
OWN = {"MaxWolf-01", "yapit-tts", "neuro-soup"}
WORK = {"HELFERLINE", "ondewo"}
SCHOOL = {"Die-Spengergasse", "tms-organization", "necoder27", "schletz"}
# Counted with the private work, never named.
PRIVATE = {"funke-dev"}
# Closed as completed, but by a duplicate, a workaround or a "works as intended",
# not by a change in the project: read by hand from each thread.
NOT_FIXED = {
    "https://github.com/hexclave/hexclave/issues/1228",
    "https://github.com/Hannah-Sten/TeXiFy-IDEA/issues/3678",
    "https://github.com/jackyzha0/quartz/issues/1628",
    "https://github.com/jackyzha0/quartz/issues/1640",
    "https://github.com/xRyul/obsidian-image-converter/issues/44",
    "https://github.com/tth05/obsidian-completr/issues/64",
    "https://github.com/kepano/defuddle/issues/240",
}
# Max's own repos by the README tree's folders; one not listed counts as a tool.
FOLDER = {
    "research": {"grax", "LNDP", "neat", "multi-agent-transformer", "sentinel2-landcover-classification",
                 "es-torch", "evochi", "x-transformers", "SuperSuit", "csv_qa", "x-evolution", "LAPO",
                 "hls-foundation-os", "evolving_ideas"},
    # The README's fun/ projects count as apps: small apps, one layer fewer to tell apart.
    "apps": {"yapit", "aleator", "higgs-audio", "markxiv", "TruthTabler", "2048JavaFx-Bot"},
}
# Max's own repos made for school, counted with the school accounts' work.
SCHOOL_REPOS = {"TinfProject-2nd-Semester", "DBI-Mockdata", "dbi-vergleichsprojekt"}
# Notebooks are mostly stored output.
NOT_WRITTEN = {"Jupyter Notebook"}


@dataclass
class Args:
    refresh: bool = False
    """Fetch from GitHub even if the cache exists."""
    cache: Path = HERE / "data.json"
    """Fetched data, kept between renders."""
    out: Path = HERE / "activity.svg"
    """The SVG to write."""
    today: str = ""
    """Treat this date (YYYY-MM-DD) as today; defaults to the real date."""


# ---------------------------------------------------------------- fetch

class QueryFailed(Exception):
    pass


def gql(query: str, attempts: int = 3, **variables) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            cmd += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    for attempt in range(attempts):
        run = subprocess.run(cmd, capture_output=True, text=True)
        try:
            out = json.loads(run.stdout)
        except json.JSONDecodeError:
            out = {"errors": [run.stderr]}
        if not (run.returncode or out.get("errors")):
            return out["data"]
        time.sleep(5 * (attempt + 1))  # GitHub answers 502 on queries that time out; often a retry passes.
    if os.environ.get("CARD_DEBUG"):
        print(query[:300], run.stderr[:500], out.get("errors"))
    raise QueryFailed(len(out.get("errors") or []))


def gql_or_exit(query: str, **variables) -> dict:
    try:
        return gql(query, **variables)
    except QueryFailed as e:
        # The error text can quote a private repo's name, and Action logs are public.
        raise SystemExit(f"GraphQL query failed ({e} errors); check that GH_TOKEN is set and can read the "
                         "viewer's repos. CARD_DEBUG=1 prints the error, which can name a private repo.")


def month_starts(first: dt.date, today: dt.date) -> list[dt.date]:
    months, m = [], first.replace(day=1)
    while m <= today:
        months.append(m)
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return months


def stamp(day: dt.date, end: bool = False) -> str:
    return f"{day}T23:59:59Z" if end else f"{day}T00:00:00Z"


BY_REPO = """
  commitContributionsByRepository(maxRepositories: 100) {
    repository { name owner { login } isPrivate }
    contributions(first: 31) { nodes { occurredAt commitCount } }
  }
  pullRequestContributionsByRepository(maxRepositories: 100) {
    repository { name owner { login } isPrivate } contributions { totalCount }
  }
  issueContributionsByRepository(maxRepositories: 100) {
    repository { name owner { login } isPrivate } contributions { totalCount }
  }
"""


def public_contributions(c: dict, month: str, out: list):
    """Append one row per public repo and day (commits) or month (PRs, issues)."""
    for r in c["commitContributionsByRepository"]:
        if not r["repository"]["isPrivate"]:
            for n in r["contributions"]["nodes"]:
                out.append({"owner": r["repository"]["owner"]["login"], "name": r["repository"]["name"],
                            "date": n["occurredAt"][:10], "commits": n["commitCount"], "other": 0})
    for key in ("pullRequestContributionsByRepository", "issueContributionsByRepository"):
        for r in c[key]:
            if not r["repository"]["isPrivate"]:
                out.append({"owner": r["repository"]["owner"]["login"], "name": r["repository"]["name"],
                            "date": f"{month}-01", "commits": 0, "other": r["contributions"]["totalCount"]})


def work_commits(viewer_id: str) -> tuple[collections.Counter, set[str]]:
    """Max's commits per month on the default branches of the work orgs' private repos, and those repos.

    A commit counts once it is released to the default branch, as on GitHub's
    own calendar; merged work waiting on an integration branch shows up with
    the next release, dated by when it was committed.
    """
    months, repos_seen = collections.Counter(), set()
    for org in sorted(WORK):
        repos = gql_or_exit('query($org:String!){organization(login:$org){repositories(first:100,privacy:PRIVATE)'
                    '{nodes{name}}}}', org=org)["organization"]
        for repo in (repos or {"repositories": {"nodes": []}})["repositories"]["nodes"]:
            repos_seen.add(f"{org}/{repo['name']}")
            after = None
            while True:
                ref = gql_or_exit("query($o:String!,$n:String!,$id:ID!,$after:String){repository(owner:$o,name:$n){"
                          "defaultBranchRef{target{... on Commit{history(author:{id:$id},first:100,after:$after)"
                          "{pageInfo{hasNextPage endCursor} nodes{committedDate}}}}}}}",
                          o=org, n=repo["name"], id=viewer_id, after=after)["repository"]["defaultBranchRef"]
                if not ref:
                    break
                hist = ref["target"]["history"]
                months.update(n["committedDate"][:7] for n in hist["nodes"])
                if not hist["pageInfo"]["hasNextPage"]:
                    break
                after = hist["pageInfo"]["endCursor"]
    return months, repos_seen


REPO_SHARE = """
  nameWithOwner isPrivate
  defaultBranchRef { name target { ... on Commit { mine: history(author: {id: $id}) { totalCount } } } }
  languages(first: 20) { edges { size node { name } } }
"""


def markdown_bytes(full: str, branch: str) -> int:
    """Bytes of Markdown in a repo's tree, which GitHub's language statistics leave out as prose.

    Paths the repo's .gitattributes marks linguist-generated or linguist-vendored
    don't count, as GitHub's own statistics skip them: a copy of notes kept for a
    site build is not writing.
    """
    def get(path):
        run = subprocess.run(["gh", "api", "-X", "GET", f"repos/{full}/{path}"], capture_output=True, text=True)
        return json.loads(run.stdout) if run.returncode == 0 else None

    tree = get(f"git/trees/{branch}?recursive=1")
    if not tree:
        return 0
    attrs = get(f"contents/.gitattributes?ref={branch}")
    skip = []
    for line in base64.b64decode(attrs["content"]).decode(errors="replace").splitlines() if attrs else ():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pattern, *flags = line.split()
        if any(f in ("linguist-generated", "linguist-generated=true", "linguist-vendored", "linguist-vendored=true")
               for f in flags):
            skip.append(pattern.lstrip("/"))
    return sum(e.get("size", 0) for e in tree["tree"]
               if e["type"] == "blob" and e["path"].endswith(".md") and not any(fnmatch.fnmatch(e["path"], p) for p in skip))


def weighted_languages(viewer_id: str, extra_repos: set[str]) -> tuple[dict[str, float], dict[str, float]]:
    """Max's commits in every repo he committed to, split by each repo's language mix.

    Weighting bytes by his share of the commits instead lets a repo of vendored
    plugin bundles outweigh everything he wrote. GitHub measures no Markdown, so
    each repo's Markdown is measured from its files: that share of the commits
    is writing, returned apart, and only the rest is split among the languages.
    A notes vault then adds little HTML for its few exported pages. Only the
    totals leave this function, so no private repo is named in the cache.
    """
    langs, writing, seen, after = collections.Counter(), collections.Counter(), set(), None

    def add(repo):
        seen.add(repo["nameWithOwner"])
        ref = repo["defaultBranchRef"]
        if not ref:
            return
        mix = {e["node"]["name"]: e["size"] for e in repo["languages"]["edges"] if e["node"]["name"] not in NOT_WRITTEN}
        size = sum(mix.values())
        mine = ref["target"]["mine"]["totalCount"]
        md = markdown_bytes(repo["nameWithOwner"], ref["name"]) if mine else 0
        if not md + size:
            return
        # The commits split by bytes: the Markdown share is writing, the rest goes to the code languages.
        writing["markdown"] += mine * md / (md + size)
        writing["code"] += mine * size / (md + size)
        for name, n in mix.items():
            langs[name] += mine * n / (md + size)

    while True:
        page = gql_or_exit("query($id:ID!,$after:String){viewer{repositories(first:50,after:$after,"
                   "ownerAffiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER]){pageInfo{hasNextPage endCursor} "
                   f"nodes{{{REPO_SHARE}}}}}}}}}", id=viewer_id, after=after)["viewer"]["repositories"]
        for repo in page["nodes"]:
            add(repo)
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    # Repos the listing above leaves out: public projects max committed to
    # without being a member (upstream work), and work repos reached through an
    # org team rather than membership. A query on a project the size of nixpkgs
    # can time out; max's few commits there don't move the bar, so it is left out.
    for full in sorted(extra_repos - seen):
        owner, name = full.split("/")
        try:
            add(gql(f"query($o:String!,$n:String!,$id:ID!){{repository(owner:$o,name:$n){{{REPO_SHARE}}}}}",
                    o=owner, n=name, id=viewer_id)["repository"])
        except QueryFailed:
            continue
    return dict(langs), dict(writing)


def upstream(orgs: set[str]) -> tuple[list, list]:
    """Merged PRs and fixed issues in public projects outside max's namespaces, orgs, work and school."""
    excluded = OWN | orgs | WORK | SCHOOL | PRIVATE

    def search(q):
        hits, after = [], None
        while True:
            page = gql_or_exit("query($q:String!,$after:String){search(query:$q,type:ISSUE,first:100,after:$after){"
                       "pageInfo{hasNextPage endCursor} nodes{"
                       "... on PullRequest{url closedAt repository{nameWithOwner owner{login}}}"
                       "... on Issue{url closedAt repository{nameWithOwner owner{login}}}}}}",
                       q=q, after=after)["search"]
            hits += [{"url": n["url"], "closed": n["closedAt"][:10], "repo": n["repository"]["nameWithOwner"]}
                     for n in page["nodes"] if n and n["repository"]["owner"]["login"] not in excluded]
            if not page["pageInfo"]["hasNextPage"]:
                return hits
            after = page["pageInfo"]["endCursor"]

    merged = search(f"author:{LOGIN} is:pr is:merged -user:{LOGIN}")
    fixed = [n for n in search(f"author:{LOGIN} is:issue is:closed reason:completed -user:{LOGIN}")
             if n["url"] not in NOT_FIXED]
    return merged, fixed


def fetch(today: dt.date) -> dict:
    viewer = gql_or_exit("query{viewer{id createdAt organizations(first:50){nodes{login}}}}")["viewer"]
    orgs = {n["login"] for n in viewer["organizations"]["nodes"]}
    created = dt.date.fromisoformat(viewer["createdAt"][:10])

    # Every contribution per day, private ones included, from the contribution calendar.
    days = {}
    for year in range(created.year, today.year + 1):
        cal = gql_or_exit('query($from:DateTime!,$to:DateTime!){viewer{contributionsCollection(from:$from,to:$to)'
                  '{contributionCalendar{weeks{contributionDays{date contributionCount}}}}}}',
                  **{"from": f"{year}-01-01T00:00:00Z", "to": f"{year}-12-31T23:59:59Z"})
        for w in cal["viewer"]["contributionsCollection"]["contributionCalendar"]["weeks"]:
            days.update({d["date"]: d["contributionCount"] for d in w["contributionDays"]})

    # Public contributions per repo, a month per alias, twelve months per query.
    public, months = [], month_starts(created, today)
    for i in range(0, len(months), 12):
        chunk = months[i:i + 12]
        parts = []
        for j, m in enumerate(chunk):
            last = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            parts.append(f'm{j}: contributionsCollection(from:"{stamp(m)}", to:"{stamp(last, end=True)}"){{{BY_REPO}}}')
        data = gql_or_exit("query{viewer{" + "\n".join(parts) + "}}")["viewer"]
        for j, m in enumerate(chunk):
            public_contributions(data[f"m{j}"], m.strftime("%Y-%m"), public)

    work_months, work_repos = work_commits(viewer["id"])
    merged, fixed = upstream(orgs)
    public_repos = {f"{r['owner']}/{r['name']}" for r in public if r["commits"] and r["owner"] not in WORK | SCHOOL | PRIVATE}
    langs, writing = weighted_languages(viewer["id"], public_repos | work_repos)

    stars, repo_count = 0, 0
    for owner in sorted(OWN):
        kind = "user" if owner == LOGIN else "organization"
        res = gql_or_exit(f'query($o:String!){{{kind}(login:$o){{repositories(first:100,privacy:PUBLIC,isFork:false'
                  f'{",ownerAffiliations:OWNER" if kind == "user" else ""}){{nodes{{stargazerCount}}}}}}}}', o=owner)
        nodes = res[kind]["repositories"]["nodes"]
        repo_count += len(nodes)
        stars += sum(n["stargazerCount"] for n in nodes)

    return {
        "fetched": today.isoformat(),
        "created": created.isoformat(),
        "days": days,
        "public": public,
        "work_months": dict(work_months),
        "langs": langs,
        "writing": writing,
        "merged": merged,
        "fixed": fixed,
        "repos": repo_count,
        "stars": stars,
    }


# ---------------------------------------------------------------- shape

def category(owner: str, name: str) -> str:
    if owner in WORK:
        return "work"
    if owner in SCHOOL:
        return "school"
    if owner in PRIVATE:
        return "private"
    if owner in OWN:
        if name in SCHOOL_REPOS:
            return "school"
        return next((f for f, names in FOLDER.items() if name in names), "tools")
    return "open source"


def nameable(owner: str) -> bool:
    return owner not in WORK | SCHOOL | PRIVATE


# ---------------------------------------------------------------- svg

LAYERS = ["research", "apps", "tools", "open source", "work", "school", "private"]
# House inks for GitHub's light and dark grounds. Research takes the accent,
# open source the second accent; the rest are sand and grey between them.
LAYER_INK = {  # hues of the ghibli terminal palette the README's prompts use, one per layer
    "research": ("#4b7a2c", "#7fa05a"),
    "apps": ("#2b7a8c", "#5ba5b7"),
    "tools": ("#b08c3c", "#d4b77a"),
    "open source": ("#a0603a", "#c8875a"),
    "work": ("#7a5f8a", "#9c86ab"),
    "school": ("#857245", "#8a7a52"),
    "private": ("#e6dccb", "#3a3530"),
}
# The same hues, muted, for the activity chart: a large area in a full hue drowns the rest.
AREA_INK = {
    "research": ("#8aa46c", "#6c8a4f"),
    "apps": ("#86a9b1", "#4f7a84"),
    "tools": ("#cfc09f", "#8f8264"),
    "open source": ("#c99a78", "#a2714f"),
    "work": ("#ab9fb8", "#76698a"),
    "school": ("#a39a74", "#635a42"),
    "private": ("#ebe4d8", "#332f2a"),
}
HATCH = ("#8a7f5c", "#a0906a")  # the school layer's stripes, light and dark
INK = {  # role: (light, dark)
    "muted": ("#5e5650", "#b0a89e"),
    "body": ("#37261d", "#ede3d2"),
    "strong": ("#22140b", "#faf2dc"),
    "accent": ("#426724", "#6ea444"),
    "edge": ("#cfbca3", "#4a433b"),
    # The Markdown/code bar, in neutrals so it can't be read as two of the languages above.
    "prose": ("#d9cdb6", "#d6cbb5"),
    "codeink": ("#8d8781", "#5f5a55"),
}


def slug(name: str) -> str:
    return name.replace(" ", "-")


def stylesheet(text: str) -> str:
    def rules(i):
        fills = [f".{k} {{ fill: {v[i]} }}" for k, v in INK.items()]
        fills += [f".s-{k} {{ stroke: {v[i]} }}" for k, v in INK.items()]
        fills += [f".l-{slug(k)} {{ fill: {v[i]} }}" for k, v in LAYER_INK.items()]
        fills += [f".a-{slug(k)} {{ fill: {v[i]} }}" for k, v in AREA_INK.items()]
        return " ".join(fills)

    faces = "".join(font_face(HERE / "fonts" / f"IBMPlexMono-{w}.woff2", wt, text)
                    for w, wt in (("Regular", 400), ("Medium", 500)))
    return (f"{faces} text {{ font-family: 'Plex', ui-monospace, monospace; font-variant-numeric: tabular-nums }} "
            f".h {{ font-weight: 500 }} {rules(0)} @media (prefers-color-scheme: dark) {{ {rules(1)} }}"
            # School is hatched over its own colour, so it can't be read as the solid private layer beside it.
            f" .hb {{ fill: {AREA_INK['school'][0]} }} .hs {{ stroke: {HATCH[0]} }}"
            f" @media (prefers-color-scheme: dark) {{ .hb {{ fill: {AREA_INK['school'][1]} }} .hs {{ stroke: {HATCH[1]} }} }}"
            " .a-school { fill: url(#school) }")


def font_face(path: Path, weight: int, text: str) -> str:
    """A @font-face holding only the glyphs the card uses, as inline woff2."""
    font = TTFont(path)
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["kern", "tnum"]
    sub = subset.Subsetter(opts)
    sub.populate(text=text + " 0123456789")
    sub.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    data = base64.b64encode(buf.getvalue()).decode()
    return f"@font-face {{ font-family: 'Plex'; font-weight: {weight}; src: url(data:font/woff2;base64,{data}) format('woff2') }} "


class Canvas:
    def __init__(self):
        self.parts = []

    def add(self, s: str):
        self.parts.append(s)

    def text(self, x, y, s, ink="body", size=12, anchor="start", cls=""):
        classes = f"{ink} {cls}".strip()
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" class="{classes}" font-size="{size}" '
                 f'text-anchor="{anchor}">{escape(str(s))}</text>')

    def rect(self, x, y, w, h, cls, rx=2):
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" class="{cls}"/>')

    def hline(self, x1, x2, y, cls="s-edge", dash=""):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" x2="{x2:.1f}" y1="{y:.1f}" y2="{y:.1f}" class="{cls}" stroke-width="1"{d}/>')

    def svg(self, w: int, h: int, title: str) -> str:
        body = "\n".join(self.parts)
        text = html.unescape("".join(re.findall(r">([^<]*)</text>", body)))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
                f'role="img" aria-label="{escape(title)}"><title>{escape(title)}</title>'
                f"<style>{stylesheet(text)}</style>\n{body}\n</svg>\n")


# The card sits inside the README's <pre>, which pads its content; a wider card is scaled down to fit.
W = 800
X0, X1 = 0, W
VAL = 176  # the value column of the summary and the bars of the last 90 days
SECTION_GAP = 48
FIRST_ROW = 30  # from a section's heading to its first row


def heading(c: Canvas, y: float, title: str, note: str):
    c.text(X0, y, title, "strong", 13, cls="h")
    c.text(X1, y, note, "muted", 11, "end")


def activity(c: Canvas, d: dict, today: dt.date, y: float) -> float:
    """Contributions per month, stacked by the README's folders plus work, school and private."""
    created = dt.date.fromisoformat(d["created"])
    months = [m.strftime("%Y-%m") for m in month_starts(created, today)]
    per = {m: collections.Counter() for m in months}
    for r in d["public"]:
        per[r["date"][:7]][category(r["owner"], r["name"])] += r["commits"] + r["other"]
    for m, n in d["work_months"].items():
        if m in per:
            per[m]["work"] += n
    totals = collections.Counter()
    for day, n in d["days"].items():
        totals[day[:7]] += n
    for m in months:
        # Whatever the calendar counts beyond the named layers is private work.
        per[m]["private"] += max(totals[m] - sum(per[m].values()), 0)

    c.add('<defs><pattern id="school" patternUnits="userSpaceOnUse" width="4" height="4" patternTransform="rotate(45)">'
          '<rect width="4" height="4" class="hb"/><line x1="0" y1="0" x2="0" y2="4" class="hs" stroke-width="1.5"/>'
          '</pattern></defs>')
    heading(c, y, "activity", f"contributions per month since {created:%B %Y}".lower())
    # Legend.
    x, ly = X0, y + FIRST_ROW
    for layer in LAYERS:
        c.rect(x, ly - 8, 9, 9, f"a-{slug(layer)}")
        c.text(x + 14, ly, layer, "muted", 11)
        x += 14 + len(layer) * 6.6 + 18

    top_y, ph = y + FIRST_ROW + 22, 150
    base_y = top_y + ph
    pw = X1 - X0
    xs = [X0 + i * pw / (len(months) - 1) for i in range(len(months))]
    peak = math.sqrt(max(sum(per[m].values()) for m in months) or 1)
    scale = lambda v: base_y - math.sqrt(v) / peak * ph
    below = [0.0] * len(months)
    for layer in LAYERS:
        above = [b + per[m][layer] for b, m in zip(below, months)]
        if any(a > b for a, b in zip(above, below)):
            upper = " ".join(f"{x:.1f},{scale(v):.1f}" for x, v in zip(xs, above))
            lower = " ".join(f"{x:.1f},{scale(v):.1f}" for x, v in reversed(list(zip(xs, below))))
            c.add(f'<polygon class="a-{slug(layer)}" points="{upper} {lower}"/>')
        below = above
    for i, m in enumerate(months):
        if m.endswith("-01"):
            c.add(f'<line x1="{xs[i]:.1f}" x2="{xs[i]:.1f}" y1="{top_y - 4:.1f}" y2="{base_y:.1f}" '
                  f'class="s-edge" stroke-width="0.8" stroke-dasharray="2 3"/>')
            c.text(xs[i] + 5, top_y + 6, m[:4], "muted", 11)
    c.hline(X0, X1, base_y)

    # One dot per upstream PR merged or bug fixed, by the day it closed.
    span = (today - dt.date.fromisoformat(months[0] + "-01")).days
    # A busy week spills into the nearest columns with room, so the dots stay a low band.
    pitch, depth = 4.5, 4
    stacks = collections.Counter()
    for item in sorted(d["merged"] + d["fixed"], key=lambda n: n["closed"]):
        day = dt.date.fromisoformat(item["closed"])
        home = round((X0 + (day - created.replace(day=1)).days / span * pw) / pitch)
        slot = min((s for s in range(home - 40, home + 41) if stacks[s] < depth), key=lambda s: (abs(s - home), s))
        c.rect(slot * pitch - 1.5, base_y + 5 + stacks[slot] * pitch, 3, 3, "l-open-source", rx=0.8)
        stacks[slot] += 1
    dots_end = base_y + 5 + max(stacks.values(), default=0) * pitch
    c.text(X1, dots_end + 16, "height grows with the square root of the count  ·  "
           "a dot is a merged pr or a fixed bug report upstream", "muted", 10.5, "end")
    return dots_end + 16


def summary(c: Canvas, d: dict, today: dt.date, y: float) -> float:
    year = str(today.year)
    created = dt.date.fromisoformat(d["created"])
    days = d["days"]
    all_time = sum(days.values())
    this_year = sum(n for day, n in days.items() if day.startswith(year))
    busiest = collections.Counter()
    for r in d["public"]:
        if r["date"].startswith(year) and r["owner"] in OWN:
            busiest[r["name"]] += r["commits"]
    upstream_repos = {n["repo"] for n in d["merged"] + d["fixed"]}

    rows = [
        ("contributions", f"{all_time:,} in total  ·  {this_year:,} in {year}"),
        ("on github", f"{(today - created).days / 365.25:.1f} years, since {created:%B %Y}".lower()),
        ("upstream", f"{len(d['merged'])} prs merged  ·  {len(d['fixed'])} bug reports fixed  ·  "
                     f"{len(upstream_repos)} projects"),
        ("repos", f"{d['repos']} public  ·  {d['stars']} stars"),
        (f"busiest {year}", "  ·  ".join(f"{n} {k:,}" for n, k in busiest.most_common(4))),
    ]
    heading(c, y, "summary", f"as of {today:%d %B %Y}".lower())
    y += FIRST_ROW
    for key, value in rows:
        c.text(X0, y, key, "muted", 12)
        c.text(VAL, y, value, "body", 12)
        y += 22

    # Languages: commits split by each repo's language mix, private and work repos included.
    langs = collections.Counter(d["langs"])
    total = sum(langs.values())
    top = langs.most_common(5)
    shown = top + [("other", total - sum(v for _, v in top))]
    inks = ["l-research", "l-open-source", "l-apps", "l-tools", "l-work", "l-school"]
    c.text(X0, y, "languages", "muted", 12)
    bar_w, x = 420, VAL
    for (name, v), ink in zip(shown, inks):
        w = bar_w * v / total
        c.rect(x, y - 9, max(w - 2, 1), 10, ink)
        x += w
    y += 20
    x = VAL
    for (name, v), ink in zip(shown, inks):
        label = f"{name.lower()} {100 * v / total:.0f}%"
        c.rect(x, y - 8, 8, 8, ink)
        c.text(x + 12, y, label, "muted", 11)
        x += 12 + len(label) * 6.6 + 16
    y += 26

    # The same commits split by what they changed: Markdown (notes, docs, prompts) or code.
    md, code = d["writing"]["markdown"], d["writing"]["code"]
    c.text(X0, y, "markdown · code", "muted", 12)
    c.rect(VAL, y - 6, max(bar_w * md / (md + code) - 2, 1), 5, "prose", rx=1.5)
    c.rect(VAL + bar_w * md / (md + code), y - 6, bar_w * code / (md + code), 5, "codeink", rx=1.5)
    c.text(VAL + bar_w + 14, y, f"{100 * md / (md + code):.0f}% markdown  ·  {100 * code / (md + code):.0f}% code", "muted", 11)
    y += 30

    # Contributions per week since the account began, height by square root like the chart above.
    n = (today - created).days // 7 + 1
    start = today - dt.timedelta(days=n * 7 - 1)
    weeks = [sum(days.get((start + dt.timedelta(days=w * 7 + i)).isoformat(), 0) for i in range(7))
             for w in range(n)]
    c.text(X0, y, "contributions a week", "muted", 12)
    peak = max(weeks) or 1
    step = bar_w / (len(weeks) - 1)
    pts = " ".join(f"{VAL + i * step:.1f},{y - 22 * math.sqrt(v / peak):.1f}" for i, v in enumerate(weeks))
    for year in range(start.year + 1, today.year + 1):
        x = VAL + (dt.date(year, 1, 1) - start).days / 7 * step
        c.add(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y - 24:.1f}" y2="{y + 0.5:.1f}" '
              'class="s-edge" stroke-width="0.8" stroke-dasharray="2 2"/>')
    c.hline(VAL, VAL + bar_w, y + 0.5, dash="2 3")
    c.add(f'<polyline points="{pts}" fill="none" class="s-accent" stroke-width="1.5" '
          'stroke-linejoin="round" stroke-linecap="round"/>')
    c.text(VAL + bar_w + 14, y, f"since {created.year}  ·  max {peak:,}", "muted", 11)
    return y


def recent(c: Canvas, d: dict, today: dt.date, y: float) -> float:
    since = today - dt.timedelta(days=90)
    weekly = collections.defaultdict(lambda: [0] * 13)
    named = collections.Counter()
    named_total = 0
    for r in d["public"]:
        day = dt.date.fromisoformat(r["date"])
        if day <= since:
            continue
        named_total += r["commits"] + r["other"]
        if nameable(r["owner"]) and r["commits"]:
            key = (r["owner"], r["name"])
            weekly[key][min((day - since).days // 7, 12)] += r["commits"]
            named[key] += r["commits"]
    window = sum(n for day, n in d["days"].items() if dt.date.fromisoformat(day) > since)
    unnamed = max(window - named_total, 0)
    rows = [(k, n) for k, n in named.most_common(6) if n >= 5]

    heading(c, y, "last 90 days", "commits per week, public repos")
    y += FIRST_ROW
    top = max((max(v) for v in weekly.values()), default=1)
    for (owner, name), n in rows:
        c.text(X0, y, name, "body", 12)
        c.text(VAL - 16, y, f"{n:,}", "muted", 12, "end")
        for w, v in enumerate(weekly[(owner, name)]):
            h = max(16 * math.sqrt(v / top), 1.5)
            c.rect(VAL + w * 14, y - h, 10, h, "accent" if v else "edge", rx=1.5)
        c.text(VAL + 13 * 14 + 12, y, category(owner, name), "muted", 11)
        y += 26
    c.text(X0, y, "private & work", "muted", 12)
    c.text(VAL - 16, y, f"{unnamed:,}", "muted", 12, "end")
    c.text(VAL, y, "contributions, not named", "muted", 11)
    return y


def render(d: dict, today: dt.date) -> str:
    c = Canvas()
    y = activity(c, d, today, 18)
    y = summary(c, d, today, y + SECTION_GAP)
    y = recent(c, d, today, y + SECTION_GAP)
    return c.svg(W, math.ceil(y + 12), "Max's GitHub activity: per month since 2020, a summary, and the last 90 days")


def main(args: Args):
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    if args.refresh or not args.cache.exists():
        args.cache.write_text(json.dumps(fetch(today)))
    args.out.write_text(render(json.loads(args.cache.read_text()), today))
    print(args.out)


if __name__ == "__main__":
    main(tyro.cli(Args, description=__doc__))
