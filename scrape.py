"""Lookout scraper: reads company job boards (including JavaScript-only ones) and publishes
internship postings as one JSON feed that the Lookout scheduled tasks read.

Run: python scrape.py            (writes out/postings.json and out/report.md)
Env: GIST_TOKEN (optional)       publishes the feed to a secret gist (id kept in gist_id.txt)
     ONLY="Dexcom,Illumina"      limits a run to some companies (for testing)
Schedule and setup: see README.md.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import traceback
from urllib.parse import urljoin, urlparse

import requests
import yaml

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"
GIST_ID_FILE = ROOT / "gist_id.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}

# Internship-like titles: intern / internship / co-op / early interest / summer 2027.
# Excludes "internal", "international", "internet".
INTERN_RE = re.compile(r"\bintern(?!al|ation|et)\w*|\bco-?op\b|early[ -]?(interest|talent)|summer\s*'?(20)?27", re.I)
BLOCK_RE = re.compile(r"access denied|request blocked|are you a robot|captcha|just a moment|verify you are human|403 forbidden", re.I)
MAX_POSTINGS = 150
# postings.txt (what Lookout reads) leaves out roles located only outside the US; postings.json keeps everything.
FOREIGN_RE = re.compile(r"canada|ontario|british columbia|quebec|sweden|france|italy|germany|bavaria|ireland|colombia|china|beijing|shanghai|india|bangalore|hyderabad|singapore|malaysia|penang|kuala lumpur|brazil|sao paulo|\bBR\b|japan|korea|taiwan|united kingdom|\bUK\b|england|london|australia|mexico|philippines|poland|spain|netherlands|israel|costa rica|bulgaria|romania|vietnam|hong kong|switzerland|denmark|belgium|austria|czech|hungary|portugal|argentina|chile|peru|egypt|dubai|saudi|thailand|indonesia|new zealand", re.I)
US_RE = re.compile(r"united states|\bUSA?\b|california|\b(CA|CO|TX|NY|WA|AZ|IL|MA|VA|GA|NC|FL|UT|OR|NV)\b|remote", re.I)


def us_relevant(loc: str) -> bool:
    return not loc or not FOREIGN_RE.search(loc) or bool(US_RE.search(loc))
BROWSER_CONCURRENCY = 4


def is_intern(title: str) -> bool:
    return bool(title) and bool(INTERN_RE.search(title))


def clean(s, n=200):
    if s is None:
        return ""
    if isinstance(s, (list, tuple)):
        s = "; ".join(clean(x, n) for x in s if x)
    elif isinstance(s, dict):
        parts = [s.get(k) for k in ("name", "fullLocation", "city", "region", "state", "country", "Name", "LocationName") if s.get(k)]
        s = ", ".join(str(p) for p in parts)
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:n]


def posting(title, location="", url="", posted=""):
    return {"title": clean(title), "location": clean(location), "url": url or "", "posted": clean(posted, 40)}


def dedupe(items):
    seen, out = {}, []
    for p in items:
        if not p["title"]:
            continue
        key = (p["title"].lower(), p["location"].lower())
        if key in seen:
            if not seen[key]["url"] and p["url"]:
                seen[key]["url"] = p["url"]
            continue
        seen[key] = p
        out.append(p)
    return out[:MAX_POSTINGS]


def epoch_to_date(v):
    try:
        v = float(v)
        if v > 1e12:
            v /= 1000
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).date().isoformat()
    except Exception:
        return str(v or "")


# ---------------------------------------------------------------- API boards (no browser needed)

def get_json(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(c):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs")
    jobs = data.get("jobs", [])
    return len(jobs), [posting(j.get("title"), (j.get("location") or {}).get("name"), j.get("absolute_url"), (j.get("updated_at") or "")[:10])
                       for j in jobs if is_intern(j.get("title", ""))]


def fetch_lever(c):
    jobs = get_json(f"https://api.lever.co/v0/postings/{c['board']}?mode=json")
    return len(jobs), [posting(j.get("text"), (j.get("categories") or {}).get("location"), j.get("hostedUrl"), epoch_to_date(j.get("createdAt")))
                       for j in jobs if is_intern(j.get("text", ""))]


def fetch_ashby(c):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['board']}")
    jobs = data.get("jobs", [])
    return len(jobs), [posting(j.get("title"), j.get("location"), j.get("jobUrl"), (j.get("publishedAt") or "")[:10])
                       for j in jobs if is_intern(j.get("title", ""))]


def fetch_smartrecruiters(c):
    b, out, total, offset = c["board"], [], 0, 0
    while offset < 500:
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{b}/postings", params={"q": "intern", "limit": 100, "offset": offset})
        content = data.get("content", [])
        total = data.get("totalFound", len(content))
        for j in content:
            loc = j.get("location") or {}
            loc_s = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            if loc.get("remote"):
                loc_s = (loc_s + " (remote)").strip()
            if is_intern(j.get("name", "")):
                out.append(posting(j.get("name"), loc_s, f"https://jobs.smartrecruiters.com/{b}/{j.get('id')}", (j.get("releasedDate") or "")[:10]))
        offset += 100
        if offset >= total or not content:
            break
    return total, out


def fetch_workday(c):
    u = urlparse(c["url"])
    host = u.netloc
    tenant = host.split(".")[0]
    site = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)][0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    hdr = {**HEADERS, "Content-Type": "application/json", "Origin": f"https://{host}", "Referer": c["url"]}
    sess = requests.Session()
    sess.get(c["url"], headers={"User-Agent": UA, "Accept": "text/html"}, timeout=30)
    out, total = [], 0
    for q in ("intern", "co-op"):
        offset = 0
        while offset < 400:
            for attempt in range(3):
                r = sess.post(api, headers=hdr, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q}, timeout=30)
                if r.status_code != 429:
                    break
                time.sleep(6 * (attempt + 1))
            r.raise_for_status()
            data = r.json()
            posts = data.get("jobPostings", [])
            if offset == 0:
                total += data.get("total", 0)
            for j in posts:
                if is_intern(j.get("title", "")):
                    out.append(posting(j.get("title"), j.get("locationsText"), f"https://{host}/{site}{j.get('externalPath', '')}", j.get("postedOn")))
            offset += 20
            if offset >= data.get("total", 0) or not posts:
                break
    return total, out


def fetch_eightfold(c):
    host = urlparse(c["url"]).netloc
    out, total, start = [], 0, 0
    while start < 500:
        data = get_json(f"https://{host}/api/apply/v2/jobs", params={"domain": c["domain"], "query": "intern", "start": start, "num": 100})
        pos = data.get("positions", [])
        total = data.get("count", len(pos))
        for j in pos:
            if is_intern(j.get("name", "")):
                url = j.get("canonicalPositionUrl") or f"https://{host}/careers/job/{j.get('id')}"
                out.append(posting(j.get("name"), j.get("location") or j.get("locations"), url, epoch_to_date(j.get("t_create"))))
        start += 100
        if start >= total or not pos:
            break
    return total, out


API = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
       "smartrecruiters": fetch_smartrecruiters, "workday": fetch_workday, "eightfold": fetch_eightfold}


# ---------------------------------------------------------------- browser boards

TITLE_KEYS = ("title", "jobTitle", "Title", "postingTitle", "job_title", "positionTitle", "externalName",
              "requisitionTitle", "JobTitle", "name", "Name", "text")
LOC_KEYS = ("locationsText", "location", "locationName", "primaryLocation", "PrimaryLocation", "jobLocation",
            "city_state", "cityState", "locations", "city", "address", "Location", "workLocation")
URL_KEYS = ("absolute_url", "applyUrl", "jobUrl", "hostedUrl", "canonicalPositionUrl", "externalPath", "url", "Url",
            "link", "href", "detailUrl", "externalUrl", "jobDetailUrl", "applyURL")
DATE_KEYS = ("postedOn", "postedDate", "PostedDate", "datePosted", "publishedAt", "releasedDate", "created_at",
             "postingDate", "dateCreated", "t_create", "posted_date")


def walk_json(obj, base, found, depth=0):
    if depth > 12:
        return
    if isinstance(obj, dict):
        title = next((obj[k] for k in TITLE_KEYS if isinstance(obj.get(k), str)), None)
        if title and len(title) < 200 and is_intern(title):
            loc = next((obj[k] for k in LOC_KEYS if obj.get(k)), "")
            url = next((obj[k] for k in URL_KEYS if isinstance(obj.get(k), str) and obj.get(k)), "")
            date = next((obj[k] for k in DATE_KEYS if obj.get(k)), "")
            if date and isinstance(date, (int, float)):
                date = epoch_to_date(date)
            if url and not url.startswith("http"):
                url = urljoin(base, url)
            found.append(posting(title, loc, url, date))
        for v in obj.values():
            walk_json(v, base, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:2000]:
            walk_json(v, base, found, depth + 1)


DOM_JS = r"""
(re) => {
  const rx = new RegExp(re, 'i');
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const t = (a.innerText || a.getAttribute('aria-label') || '').trim();
    if (!t || t.length > 200 || !rx.test(t)) continue;
    const card = a.closest('li, article, tr, [class*="job" i], [class*="card" i], [class*="result" i], [class*="posting" i]');
    out.push({title: t.split('\n')[0], url: a.href, context: card ? card.innerText.replace(/\s+/g, ' ').slice(0, 300) : ''});
  }
  return {links: out, nLinks: document.querySelectorAll('a[href]').length, text: (document.body ? document.body.innerText : '').slice(0, 4000), title: document.title};
}
"""


async def browse(browser, c):
    ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="en-US")
    page = await ctx.new_page()
    jsons, tasks = [], []

    async def grab(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct and resp.request.resource_type in ("xhr", "fetch"):
                body = await resp.body()
                if len(body) < 8_000_000:
                    jsons.append((resp.url, json.loads(body)))
        except Exception:
            pass

    page.on("response", lambda r: tasks.append(asyncio.ensure_future(grab(r))))
    status = None
    try:
        resp = await page.goto(c["url"], wait_until="domcontentloaded", timeout=45_000)
        status = resp.status if resp else None
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        for _ in range(4):
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(1200)
        if tasks:
            await asyncio.wait(tasks, timeout=10)
        dom = await page.evaluate(DOM_JS, INTERN_RE.pattern)
    finally:
        await ctx.close()

    from_json = []
    for u, data in jsons:
        walk_json(data, u, from_json)
    from_dom = []
    for l in dom["links"]:
        # Skip share buttons and dead "#" links (Brassring and others repeat each job as a "Share ..." link).
        if l["url"].rstrip("/").endswith(("#0", "#")) or l["title"].lower().startswith("share "):
            continue
        ctxt = l["context"].replace(l["title"], "").strip()
        from_dom.append(posting(l["title"], ctxt[:150], l["url"]))
    # Merge: JSON gives clean fields, the page gives the link. Same title = same job.
    dom_url = {}
    for d in from_dom:
        dom_url.setdefault(d["title"].lower(), d["url"])
    for j in from_json:
        if not j["url"]:
            j["url"] = dom_url.get(j["title"].lower(), "")
    json_titles = {j["title"].lower() for j in from_json}
    found = dedupe(from_json + [d for d in from_dom if d["title"].lower() not in json_titles])
    # Workday JSON gives paths like /job/...; the working link needs the board's site name in front.
    pu = urlparse(c["url"])
    if "myworkdayjobs.com" in pu.netloc:
        site = [x for x in pu.path.split("/") if x and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", x)][0]
        for f in found:
            if f["url"].startswith(f"https://{pu.netloc}/job/"):
                f["url"] = f["url"].replace(f"https://{pu.netloc}/job/", f"https://{pu.netloc}/{site}/job/", 1)
    text = dom["text"]
    blocked = (status and status >= 400) or (BLOCK_RE.search(text[:1500] + dom["title"]) and not found)
    empty = dom["nLinks"] < 5 and not jsons and not found
    if re.search(r"\b(log ?in|sign ?in)\b", dom["title"], re.I) and not found:
        raise RuntimeError(f"page asks for a login (title: {dom['title'][:60]})")
    debug = f"http {status}, {dom['nLinks']} links, {len(jsons)} json responses, title '{dom['title'][:60]}'"
    if blocked:
        raise RuntimeError(f"blocked or error page ({debug})")
    if empty:
        raise RuntimeError(f"page rendered almost nothing ({debug})")
    return debug, found


# ---------------------------------------------------------------- run

async def run(companies):
    from playwright.async_api import async_playwright

    results = []
    browser_jobs = []
    for c in companies:
        r = {"name": c["name"], "method": c["type"], "source": c.get("url") or c.get("board"), "status": "ok",
             "error": "", "detail": "", "postings": []}
        t0 = time.time()
        if c["type"] in API:
            try:
                total, posts = API[c["type"]](c)
                r["postings"] = dedupe(posts)
                r["detail"] = f"api: {total} jobs checked"
            except Exception as e:
                r["error"] = f"{c['type']} api failed: {e.__class__.__name__}: {str(e)[:150]}"
                if c.get("url"):
                    r["method"] = "browser (api fallback)"
                    if c["type"] == "workday" and "?" not in c["url"]:
                        c = {**c, "url": c["url"].rstrip("/") + "?q=intern"}
                    browser_jobs.append((c, r))
                else:
                    r["status"] = "failed"
        else:
            browser_jobs.append((c, r))
        r["seconds"] = round(time.time() - t0, 1)
        results.append(r)

    if browser_jobs:
        sem = asyncio.Semaphore(BROWSER_CONCURRENCY)
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--disable-blink-features=AutomationControlled"],
                                              executable_path=os.environ.get("CHROME_PATH") or None)

            async def one(c, r):
                async with sem:
                    t0 = time.time()
                    try:
                        debug, posts = await asyncio.wait_for(browse(browser, c), timeout=120)
                        r["postings"], r["detail"], r["status"] = posts, f"browser: {debug}", "ok"
                    except Exception as e:
                        r["status"] = "failed"
                        r["error"] = (r["error"] + " | " if r["error"] else "") + f"browser: {str(e)[:200]}"
                    r["seconds"] = round(r.get("seconds", 0) + time.time() - t0, 1)

            await asyncio.gather(*(one(c, r) for c, r in browser_jobs))
            await browser.close()
    return results


def write_outputs(results):
    now = dt.datetime.now(dt.timezone.utc)
    pt = now.astimezone(dt.timezone(dt.timedelta(hours=-8)))
    feed = {
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "generated_at_pacific_approx": pt.strftime("%Y-%m-%d %H:%M"),
        "run_url": os.environ.get("RUN_URL", ""),
        "about": "Internship-like postings (intern, internship, co-op, early interest, summer 2027) read directly from each company's job board. status=ok means the board was read; zero postings then means none were listed. status=failed means the board could not be read this run.",
        "summary": {
            "companies": len(results),
            "ok": sum(r["status"] == "ok" for r in results),
            "failed": sum(r["status"] == "failed" for r in results),
            "postings": sum(len(r["postings"]) for r in results),
        },
        "companies": results,
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "postings.json").write_text(json.dumps(feed, indent=1, ensure_ascii=False))
    lines = [f"# Lookout scraper report", "", f"Run: {feed['generated_at_pacific_approx']} PT (approx) · "
             f"{feed['summary']['ok']} ok · {feed['summary']['failed']} failed · {feed['summary']['postings']} postings", "",
             "| Company | Method | Status | Postings | Detail / error | Secs |", "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['method']} | {r['status']} | {len(r['postings'])} | {(r['error'] or r['detail']).replace('|', '/')[:180]} | {r['seconds']} |")
    lines.append("")
    for r in results:
        if r["postings"]:
            lines.append(f"## {r['name']}")
            lines += [f"- {p['title']} · {p['location']} · {p['posted']} · {p['url']}" for p in r["postings"][:40]]
            lines.append("")
    (OUT / "report.md").write_text("\n".join(lines))
    txt = [f"LOOKOUT SCRAPER FEED · generated {feed['generated_at_pacific_approx']} Pacific ({feed['generated_at_utc']})",
           f"{feed['summary']['ok']} boards read, {feed['summary']['failed']} failed, {feed['summary']['postings']} internship postings.",
           "STATUS lines: company | ok or failed | how it was read | error. POSTING lines: company | title | location | posted | url.", ""]
    for r in results:
        txt.append(f"STATUS | {r['name']} | {r['status']} | {r['method']} | {r['error'][:120] if r['status'] == 'failed' else ''}")
    txt.append("")
    dropped = 0
    for r in results:
        for p in r["postings"]:
            if not us_relevant(p["location"]):
                dropped += 1
                continue
            txt.append(f"POSTING | {r['name']} | {p['title']} | {p['location'][:80]} | {p['posted']} | {p['url']}")
    txt.append("")
    txt.append(f"({dropped} postings located only outside the US are left out of this file; they are in postings.json.)")
    (OUT / "postings.txt").write_text("\n".join(txt) + "\n")
    return feed


def publish_gist(feed):
    token = os.environ.get("GIST_TOKEN")
    if not token:
        print("No GIST_TOKEN set: skipping gist publish (results are still committed to the repo).")
        return
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    files = {"postings.txt": {"content": (OUT / "postings.txt").read_text()},
             "postings.json": {"content": (OUT / "postings.json").read_text()},
             "report.md": {"content": (OUT / "report.md").read_text()}}
    gid = GIST_ID_FILE.read_text().strip() if GIST_ID_FILE.exists() else ""
    if gid:
        r = requests.patch(f"https://api.github.com/gists/{gid}", headers=hdr, json={"files": files}, timeout=30)
    else:
        r = requests.post("https://api.github.com/gists", headers=hdr, timeout=30,
                          json={"description": "Lookout scraper feed", "public": False, "files": files})
    r.raise_for_status()
    g = r.json()
    if not gid:
        GIST_ID_FILE.write_text(g["id"] + "\n")
    owner = g.get("owner", {}).get("login", "")
    print(f"Gist updated: https://gist.githubusercontent.com/{owner}/{g['id']}/raw/postings.json")


def main():
    cfg = yaml.safe_load((ROOT / "companies.yaml").read_text())
    companies = cfg["companies"]
    only = [s.strip().lower() for s in os.environ.get("ONLY", "").split(",") if s.strip()]
    if only:
        companies = [c for c in companies if c["name"].lower() in only]
    results = asyncio.run(run(companies))
    feed = write_outputs(results)
    print((OUT / "report.md").read_text()[:6000])
    try:
        publish_gist(feed)
    except Exception:
        traceback.print_exc()
        print("Gist publish failed; results are still committed to the repo.")
    # Fail the workflow (so GitHub emails Preston) only if almost everything broke.
    if feed["summary"]["ok"] < max(1, len(results) // 3):
        print("Most boards failed this run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
