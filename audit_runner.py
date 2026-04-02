from __future__ import annotations

import time
from typing import Callable

import anthropic

from scraper import discover_categories, scrape_page_meta
from gsc_client import get_service, fetch_page_queries
from ai_client import get_recommendations
from excel_writer import create_excel

ProgressFn = Callable[[int, str], None]


def run_audit(
    shop_url: str,
    site_url: str,
    creds_data: dict,
    api_key: str,
    progress: ProgressFn,
) -> str:
    """
    Runs the full SEO audit pipeline:
      1. Discover product categories via sitemap
      2. Scrape H1 / meta title / meta description for each
      3. Fetch GSC query data per page
      4. Generate AI recommendations via Claude
      5. Export to Excel
    Returns the path to the generated Excel temp file.
    """

    # ── Step 1: discover categories ──────────────────────────────────────────
    progress(5, f"Odkrywam kategorie produktów na {shop_url}…")
    category_urls = discover_categories(shop_url)

    if not category_urls:
        raise ValueError(
            f"Nie znaleziono kategorii produktów na {shop_url}. "
            "Sprawdź czy sklep ma sitemap.xml z kategoriami."
        )
    progress(10, f"Znaleziono {len(category_urls)} kategorii.")

    # ── Step 2: scrape meta data ──────────────────────────────────────────────
    categories: dict[str, dict] = {}
    n = len(category_urls)

    for i, url in enumerate(category_urls):
        pct = 10 + int(20 * (i / n))
        slug = url.rstrip("/").split("/")[-1]
        progress(pct, f"Meta [{i + 1}/{n}]: {slug}")
        meta = scrape_page_meta(url)
        categories[url] = {**meta, "queries": []}
        time.sleep(0.3)

    # ── Step 3: GSC data ──────────────────────────────────────────────────────
    progress(30, "Łączę z Google Search Console…")
    service = get_service(creds_data)

    for i, (url, cat) in enumerate(categories.items()):
        pct = 30 + int(20 * (i / n))
        slug = url.rstrip("/").split("/")[-1]
        progress(pct, f"GSC [{i + 1}/{n}]: {slug}")

        queries = fetch_page_queries(service, site_url, url)
        if not queries:
            # Some shops register URLs with trailing slash in GSC
            queries = fetch_page_queries(service, site_url, url + "/")
        cat["queries"] = queries
        time.sleep(0.2)

    total_queries = sum(len(c["queries"]) for c in categories.values())
    progress(50, f"Pobrano {total_queries} fraz GSC. Generuję rekomendacje AI…")

    # ── Step 4: AI recommendations ────────────────────────────────────────────
    client = anthropic.Anthropic(api_key=api_key)
    recommendations: dict[str, dict] = {}

    for i, (url, cat) in enumerate(categories.items()):
        pct = 50 + int(38 * (i / n))
        slug = url.rstrip("/").split("/")[-1]
        progress(pct, f"AI [{i + 1}/{n}]: {slug}")
        recommendations[url] = get_recommendations(client, cat)
        time.sleep(0.5)

    # ── Step 5: Excel ─────────────────────────────────────────────────────────
    progress(90, "Generuję plik Excel…")
    excel_path = create_excel(categories, recommendations)

    progress(100, f"Gotowe! Audyt obejmuje {n} kategorii i {total_queries} fraz GSC.")
    return excel_path
