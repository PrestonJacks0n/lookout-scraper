# Lookout scraper

Reads company job boards that Lookout can't open on its own (JavaScript-only boards like Workday,
Eightfold, Brassring, Paycom, ADP) and publishes internship postings as one JSON feed.

- **Schedule:** GitHub Actions cron every 3 hours (see `.github/workflows/scrape.yml`). GitHub has been starting
  scheduled runs 5 to 8 hours late, so the runs aren't timed to Lookout; the steady cadence keeps the feed a few hours
  old at most. A manual or API trigger (`workflow_dispatch`) starts within seconds and takes about 3 minutes.
- **Output:** `out/postings.json` (the feed) and `out/report.md` (human-readable status), committed after every run,
  and published to a secret gist when the `GIST_TOKEN` secret is set.
- **Add or fix a company:** edit `companies.yaml` on GitHub (pencil icon). Instructions are at the top of that file.
- **Run now:** Actions tab → Lookout scrape → Run workflow (optionally list company names to test just those).
- **Cost:** ~8 runs/day × ~3 min ≈ 750 of the 2,000 free private-repo minutes a month.
