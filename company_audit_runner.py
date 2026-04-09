from __future__ import annotations

import time
from typing import Callable

import anthropic

from scraper import discover_pages_company, scrape_page_meta_company
from gsc_client import get_service, fetch_page_queries
from company_ai_client import get_company_recommendations
from company_excel_writer import create_company_excel

ProgressFn = Callable[[int, str], None]

MAX_PAGES        = 50    # company sites are small
SCRAPE_TIMEOUT_S = 300   # 5-minute safety net


def run_company_audit(
    shop_url: str,
    site_url: str,
    creds_data: dict | None,
    api_key: str,
    progress: ProgressFn,
) -> str:
    """
    Runs the company-site SEO audit pipeline:
      1. Discover pages from nav menu + sitemap (max 50)
      2. Scrape H1 / H2s / meta / form-presence per page
      3. Fetch GSC data if available (optional — no error if missing)
      4. Generate AI recommendations in one batch call
      5. Export to Excel
    Returns path to the generated temp .xlsx file.
    """

    # ── Step 1: discover pages ────────────────────────────────────────────────
    progress(5, f"Odkrywam podstrony na {shop_url}…")
    print(f"[company] Discover: {shop_url}", flush=True)

    page_urls = discover_pages_company(shop_url)

    if not page_urls:
        raise ValueError(
            f"Nie znaleziono podstron na {shop_url}. "
            "Sprawdź czy strona jest dostępna i ma sitemap.xml lub menu nawigacyjne."
        )

    n = len(page_urls)
    progress(10, f"Znaleziono {n} podstron.")
    print(f"[company] Znaleziono {n} podstron.", flush=True)

    # ── Step 2: scrape ────────────────────────────────────────────────────────
    pages: list[dict] = []
    scrape_start = time.monotonic()
    timed_out    = False

    for i, url in enumerate(page_urls):
        elapsed = time.monotonic() - scrape_start
        if elapsed > SCRAPE_TIMEOUT_S:
            msg = (
                f"Scraping przerwany po {int(elapsed)}s (limit {SCRAPE_TIMEOUT_S}s). "
                f"Zebrano {i}/{n} podstron."
            )
            progress(10 + int(20 * (i / n)), msg)
            print(f"[company] TIMEOUT: {msg}", flush=True)
            timed_out = True
            break

        pct  = 10 + int(20 * (i / n))
        slug = url.rstrip("/").split("/")[-1] or "strona-główna"
        progress(pct, f"Meta [{i + 1}/{n}]: {slug}")
        print(f"[company] Scrapuję {i + 1}/{n}: {url}", flush=True)

        meta = scrape_page_meta_company(url)
        meta["queries"] = []
        pages.append(meta)
        time.sleep(0.3)

    if not pages:
        raise ValueError("Scraping nie zwrócił żadnych wyników.")

    n = len(pages)

    # ── Step 3: GSC (optional) ────────────────────────────────────────────────
    gsc_available = False
    if creds_data and site_url:
        try:
            progress(30, "Łączę z Google Search Console…")
            print("[company] GSC: łączenie…", flush=True)
            service = get_service(creds_data)

            for i, page in enumerate(pages):
                pct  = 30 + int(20 * (i / n))
                slug = page["url"].rstrip("/").split("/")[-1] or "główna"
                progress(pct, f"GSC [{i + 1}/{n}]: {slug}")
                print(f"[company] GSC {i + 1}/{n}: {page['url']}", flush=True)

                queries = fetch_page_queries(service, site_url, page["url"])
                if not queries:
                    queries = fetch_page_queries(service, site_url, page["url"] + "/")
                page["queries"] = queries
                time.sleep(0.2)

            total_q = sum(len(p["queries"]) for p in pages)
            progress(50, f"Pobrano {total_q} fraz GSC.")
            print(f"[company] GSC done: {total_q} fraz.", flush=True)
            gsc_available = total_q > 0
        except Exception as e:
            print(f"[company] GSC niedostępne (pomijam): {e}", flush=True)
            progress(50, "Brak danych GSC — analiza na podstawie scrapingu.")
    else:
        progress(50, "Brak danych GSC — analiza na podstawie scrapingu.")

    # ── Step 4: AI ────────────────────────────────────────────────────────────
    progress(55, f"Generuję rekomendacje AI dla {n} podstron…")
    print(f"[company] Claude: batch {n} podstron.", flush=True)

    client    = anthropic.Anthropic(api_key=api_key)
    ai_result = get_company_recommendations(client, pages)

    progress(88, "AI gotowe. Generuję Excel…")
    print("[company] Generuję Excel…", flush=True)

    # ── Step 5: Excel ─────────────────────────────────────────────────────────
    excel_path = create_company_excel(pages, ai_result)

    total_q  = sum(len(p["queries"]) for p in pages)
    summary  = f"Gotowe! {n} podstron"
    summary += f", {total_q} fraz GSC" if gsc_available else " (bez danych GSC)"
    if timed_out:
        summary += " (scraping przerwany po limicie czasu)"

    progress(100, summary + ".")
    print(f"[company] {summary}.", flush=True)
    return excel_path
