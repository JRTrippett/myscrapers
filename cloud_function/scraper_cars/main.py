# cloud_function/main.py
# Per-listing scraper: saves ALL visible text from each NEW car listing page.
# Each hourly run opens ONE search-results page, then downloads up to MAX_ITEMS_PER_RUN
# listings it has never saved before (so no duplicates, and no repeat visits).
import os, io, time, datetime as dt, requests, re, csv
from typing import List
from urllib.parse import urljoin, urlsplit, urlencode, parse_qsl, urlunsplit
from bs4 import BeautifulSoup
from google.cloud import storage
from flask import Request, jsonify

# ---- Config (set with GitHub Variables; deploy.yml passes them to the function) ----
BUCKET_NAME        = os.environ["BUCKET_NAME"]
BASE_SITE          = os.environ.get("BASE_SITE", "").strip()   # your search address (GitHub Variable)
SEARCH_PATH        = os.environ.get("SEARCH_PATH", "").strip() or "/search/cta"   # only used with an old-style site root
MAX_PAGES          = int(os.environ.get("MAX_PAGES", "1"))          # search pages to scan
MAX_ITEMS_PER_RUN  = int(os.environ.get("MAX_ITEMS_PER_RUN", "10")) # NEW listings saved per run (classroom safety)
DELAY_SECS         = float(os.environ.get("DELAY_SECS", "1.0"))     # polite delay between requests
USER_AGENT         = os.environ.get("USER_AGENT", "").strip() or "student-project-scraper/1.0"
SEEN_KEY           = os.environ.get("SEEN_KEY", "state/seen_post_ids.txt")  # every post_id saved so far

HDRS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}

# -- Helpers -------------------------------------------------------------------

def _search_url(base: str, path: str) -> str:
    # New style: BASE_SITE is the whole search address (it already contains /search/).
    # Old style: BASE_SITE is just the site root, so add SEARCH_PATH.
    if "/search" in base:
        return base
    return base.rstrip("/") + path

def _page_url(search_url: str, page: int) -> str:
    # Craigslist uses s=<offset> for later result pages
    if page == 0:
        return search_url
    parts = urlsplit(search_url)
    query = dict(parse_qsl(parts.query))
    query["s"] = str(page * 120)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

# Listing pages look like either:
#   new: https://www.craigslist.org/view/d/<title-slug>/<post_id>
#   old: https://<city>.craigslist.org/cto/d/<title-slug>/<post_id>.html
POST_ID_RE = re.compile(r"/view/d/[^/]+/([A-Za-z0-9]+)/?$|/(\d+)\.html?$")

def _post_id_from_url(url: str) -> str:
    path = urlsplit(url).path
    m = POST_ID_RE.search(path)
    if not m:
        return ""
    return m.group(1) or m.group(2)

def _extract_listing_links(html: str, page_url: str) -> list[str]:
    """Return absolute URLs to individual listings, in page order (newest first)."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select("li.cl-static-search-result a, li.cl-search-result a, a.result-title")
    if not anchors:
        anchors = soup.select("a[href]")
    links = []
    seen = set()
    for a in anchors:
        href = a.get("href")
        if not href:
            continue
        url = urljoin(page_url, href)
        pid = _post_id_from_url(url)
        if pid and pid not in seen:
            seen.add(pid)
            links.append(url)
    return links

def _visible_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    raw = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln and not ln.isspace()]
    dedup = []
    for ln in lines:
        if not dedup or ln != dedup[-1]:
            dedup.append(ln)
    return "\n".join(dedup) + "\n"

def _upload_text(bucket: str, object_name: str, text: str):
    storage.Client().bucket(bucket).blob(object_name)\
        .upload_from_string(text, content_type="text/plain")

def _upload_csv(bucket: str, object_name: str, rows: List[dict], header: List[str]):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header)
    w.writeheader()
    w.writerows(rows)
    storage.Client().bucket(bucket).blob(object_name)\
        .upload_from_string(buf.getvalue(), content_type="text/csv")

def _load_seen_ids(bucket: str) -> set:
    """post_ids already saved. Reads state/seen_post_ids.txt; the first time, rebuilds it from scrapes/."""
    b = storage.Client().bucket(bucket)
    blob = b.blob(SEEN_KEY)
    if blob.exists():
        text = blob.download_as_text()
        return set(ln.strip() for ln in text.splitlines() if ln.strip())
    seen = set()
    for item in b.list_blobs(prefix="scrapes/"):
        name = item.name
        if name.endswith(".txt"):
            seen.add(os.path.splitext(os.path.basename(name))[0])
    return seen

def _save_seen_ids(bucket: str, seen: set):
    text = "\n".join(sorted(seen)) + "\n"
    storage.Client().bucket(bucket).blob(SEEN_KEY)\
        .upload_from_string(text, content_type="text/plain")

# -- Entry point ----------------------------------------------------------------

def entrypoint(request: Request):
    """Called by Cloud Scheduler with a JSON body like {"pages":1,"max":10}.
       You can also test it with query overrides: ?pages=1&max=5
    """
    body = request.get_json(silent=True) or {}
    pages = min(MAX_PAGES, int(request.args.get("pages", body.get("pages", MAX_PAGES))))
    max_items = min(MAX_ITEMS_PER_RUN, int(request.args.get("max", body.get("max", MAX_ITEMS_PER_RUN))))
    base = request.args.get("base", BASE_SITE)
    path = request.args.get("path", SEARCH_PATH)
    if not base:
        return jsonify({"ok": False,
                        "error": "BASE_SITE is not set. Add it as a GitHub Variable, then re-run the Deploy Scraper workflow."}), 500
    search_url = _search_url(base, path)

    # 1) Build run folder: YYYYMMDDHHMMSS (UTC)
    run_id = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    run_prefix = f"scrapes/{run_id}"

    # 2) Collect listing links from search pages
    listing_urls = []
    for p in range(pages):
        url = _page_url(search_url, p)
        r = requests.get(url, headers=HDRS, timeout=25)
        r.raise_for_status()
        listing_urls.extend(_extract_listing_links(r.text, r.url))
        if p < pages - 1:
            time.sleep(DELAY_SECS)

    # 3) Keep only listings we have NEVER saved before, up to max_items (classroom safety)
    already_saved = _load_seen_ids(BUCKET_NAME)
    urls = []
    skipped_seen = 0
    for u in listing_urls:
        pid = _post_id_from_url(u)
        if pid in already_saved:
            skipped_seen += 1
            continue
        urls.append((pid, u))
        if len(urls) >= max_items:
            break

    # 4) Fetch each NEW listing page and write one TXT per listing
    index_rows = []
    for i, (pid, u) in enumerate(urls, start=1):
        try:
            r = requests.get(u, headers=HDRS, timeout=25)
            r.raise_for_status()
            text = _visible_text_from_html(r.text)
            obj = f"{run_prefix}/{pid}.txt"
            _upload_text(BUCKET_NAME, obj, text)
            index_rows.append({"post_id": pid, "url": u, "object": obj, "error": ""})
            already_saved.add(pid)
        except Exception as e:
            # record failure in index for transparency (it will be retried next hour)
            index_rows.append({"post_id": pid, "url": u, "object": "", "error": str(e)})
        if i < len(urls):
            time.sleep(DELAY_SECS)

    # 5) Write an index.csv for the run and remember what we saved
    # (Only when something was saved, so the extractor never picks up an empty run folder.)
    saved = [row for row in index_rows if row["object"]]
    if saved:
        _upload_csv(BUCKET_NAME, f"{run_prefix}/index.csv", index_rows, ["post_id", "url", "object", "error"])
        _save_seen_ids(BUCKET_NAME, already_saved)

    return jsonify({
        "ok": True,
        "run_id": run_id,
        "pages_scanned": pages,
        "candidates_found": len(listing_urls),
        "already_saved_skipped": skipped_seen,
        "new_listings_saved": len(saved),
        "saved_prefix": run_prefix if saved else None,
    })
