from __future__ import annotations

import time
from typing import Callable

import anthropic

from scraper import discover_categories, scrape_page_meta
from gsc_client import get_service, fetch_page_queries
from ai_client import get_recommendations
from excel_writer import create_excel

ProgressFn = Callable[[int, str], None]

MAX_CATEGORIES    = 200   # Claude context fits comfortably up to this many
SCRAPE_TIMEOUT_S  = 300   # 5 minutes max for the scraping phase


def run_audit(
    shop_url: str,
    site_url: str,
    creds_data: dict,
    api_key: str,
    progress: ProgressFn,
) -> str:
    """
    Runs the full SEO audit pipeline:
      1. Discover sections / product categories via sitemap
      2. Scrape H1 / meta title / meta description for each (max 200, 5-min timeout)
      3. Fetch GSC query data per page
      4. Generate AI recommendations via Claude
      5. Export to Excel
    Returns the path to the generated Excel temp file.
    """

    # ── Step 1: discover categories ──────────────────────────────────────────
    progress(5, f"Odkrywam kategorie na {shop_url}…")
    print(f"[audit] Discover: {shop_url}", flush=True)

    category_urls = discover_categories(shop_url)

    if not category_urls:
        raise ValueError(
            f"Nie znaleziono kategorii na {shop_url}. "
            "Sprawdź czy strona ma sitemap.xml z kategoriami."
        )

    total_found = len(category_urls)
    if total_found > MAX_CATEGORIES:
        category_urls = category_urls[:MAX_CATEGORIES]
        progress(10, f"Znaleziono {total_found} kategorii — audyt obejmie pierwsze {MAX_CATEGORIES}.")
        print(f"[audit] Znaleziono {total_found} kategorii, przycinamy do {MAX_CATEGORIES}.", flush=True)
    else:
        progress(10, f"Znaleziono {total_found} kategorii.")
        print(f"[audit] Znaleziono {total_found} kategorii.", flush=True)

    # ── Step 2: scrape meta data (with global timeout) ────────────────────────
    categories: dict[str, dict] = {}
    n = len(category_urls)
    scrape_start = time.monotonic()
    timed_out = False

    for i, url in enumerate(category_urls):
        elapsed = time.monotonic() - scrape_start
        if elapsed > SCRAPE_TIMEOUT_S:
            msg = (
                f"Scraping przerwany po {int(elapsed)}s (limit {SCRAPE_TIMEOUT_S}s). "
                f"Zebrano {i}/{n} kategorii."
            )
            progress(10 + int(20 * (i / n)), msg)
            print(f"[audit] TIMEOUT scraping: {msg}", flush=True)
            timed_out = True
            break

        pct = 10 + int(20 * (i / n))
        slug = url.rstrip("/").split("/")[-1]
        progress(pct, f"Meta [{i + 1}/{n}]: {slug}")
        print(f"[audit] Scrapuję {i + 1}/{n}: {url}", flush=True)

        meta = scrape_page_meta(url)
        categories[url] = {**meta, "queries": []}
        time.sleep(0.3)

    if not categories:
        raise ValueError("Scraping nie zwrócił żadnych wyników.")

    n = len(categories)  # actual count after possible timeout cut
    if timed_out:
        progress(30, f"Kontynuuję z {n} zebranymi kategoriami…")

    # ── Step 3: GSC data ──────────────────────────────────────────────────────
    progress(30, "Łączę z Google Search Console…")
    print("[audit] GSC: łączenie…", flush=True)
    service = get_service(creds_data)

    for i, (url, cat) in enumerate(categories.items()):
        pct = 30 + int(20 * (i / n))
        slug = url.rstrip("/").split("/")[-1]
        progress(pct, f"GSC [{i + 1}/{n}]: {slug}")
        print(f"[audit] GSC {i + 1}/{n}: {url}", flush=True)

        queries = fetch_page_queries(service, site_url, url)
        if not queries:
            queries = fetch_page_queries(service, site_url, url + "/")
        cat["queries"] = queries
        time.sleep(0.2)

    total_queries = sum(len(c["queries"]) for c in categories.values())
    progress(50, f"Pobrano {total_queries} fraz GSC. Generuję rekomendacje AI…")
    print(f"[audit] GSC done: {total_queries} fraz łącznie.", flush=True)

    # ── Step 4: AI recommendations ────────────────────────────────────────────
    client = anthropic.Anthropic(api_key=api_key)
    recommendations: dict[str, dict] = {}

    for i, (url, cat) in enumerate(categories.items()):
        pct = 50 + int(38 * (i / n))
        slug = url.rstrip("/").split("/")[-1]
        progress(pct, f"AI [{i + 1}/{n}]: {slug}")
        print(f"[audit] Claude {i + 1}/{n}: {url}", flush=True)

        recommendations[url] = get_recommendations(client, cat)
        time.sleep(0.5)

    # ── Step 5: Excel ─────────────────────────────────────────────────────────
    progress(90, "Generuję plik Excel…")
    print("[audit] Generuję Excel…", flush=True)
    excel_path = create_excel(categories, recommendations)

    summary = f"Gotowe! {n} kategorii, {total_queries} fraz GSC"
    if timed_out:
        summary += f" (scraping przerwany po limicie czasu, z {total_found} znalezionych)"
    progress(100, summary + ".")
    print(f"[audit] {summary}.", flush=True)
    return excel_path
