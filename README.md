# Lookout scraper

Reads company job boards that Lookout can't open on its own (JavaScript-only boards like Workday,
Eightfold, Brassring, Paycom, ADP) and publishes internship postings as one JSON feed.

- **Schedule:** GitHub Actions, about an hour before each Lookout run (see `.github/workflows/scrape.yml`).
- **Output:** `out/postings.json` (the feed) and `out/report.md` (human-readable status), committed after every run,
  and published to a secret gist when the `GIST_TOKEN` secret is set.
- **Add or fix a company:** edit `companies.yaml` on GitHub (pencil icon). Instructions are at the top of that file.
- **Run now:** Actions tab → Lookout scrape → Run workflow (optionally list company names to test just those).
- **Cost:** ~3 runs/day × ~4 min ≈ 400 of the 2,000 free private-repo minutes a month.
