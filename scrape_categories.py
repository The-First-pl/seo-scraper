from __future__ import annotations

import csv
import json
import os
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Konfiguracja ─────────────────────────────────────────────────────────────

BASE_URL = "https://kawa-amann.pl"
SITE_URL = "https://kawa-amann.pl/"          # URL zarejestrowany w GSC (ze slash)

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"                    # zapisywany automatycznie po 1. logowaniu

OUTPUT_CSV = "categories_with_gsc.csv"

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# Zakres dat dla GSC — ostatnie 90 dni
GSC_END_DATE = date.today() - timedelta(days=3)       # GSC ma ~3 dni opóźnienia
GSC_START_DATE = GSC_END_DATE - timedelta(days=89)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── Scraping kategorii ────────────────────────────────────────────────────────

def get_soup(url: str):
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"  ERROR {url}: {e}")
        return None


def is_product_category(url: str) -> bool:
    """Akceptuje tylko URL-e z /kategoria-produktu/."""
    return "/kategoria-produktu/" in urlparse(url).path


def discover_categories() -> set[str]:
    found = set()

    # Pobierz sitemap index i znajdź wszystkie sub-sitemaps z kategoriami produktów
    for sitemap_index in [
        f"{BASE_URL}/sitemap.xml",
        f"{BASE_URL}/sitemap_index.xml",
    ]:
        try:
            resp = requests.get(sitemap_index, headers=HTTP_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml-xml")
            sitemaps = soup.find_all("sitemap")
            if not sitemaps:
                continue

            for sm in sitemaps:
                loc = sm.find("loc")
                if not loc:
                    continue
                loc_url = loc.text.strip()

                # WooCommerce product categories: product_cat-sitemap.xml
                # WordPress categories: category-sitemap.xml
                if "product_cat" not in loc_url and "kategori" not in loc_url:
                    continue

                print(f"  Pobieram sitemap: {loc_url}")
                inner = requests.get(loc_url, headers=HTTP_HEADERS, timeout=10)
                if inner.status_code != 200:
                    continue

                inner_soup = BeautifulSoup(inner.text, "lxml-xml")
                for url_tag in inner_soup.find_all("url"):
                    loc2 = url_tag.find("loc")
                    if loc2:
                        u = loc2.text.strip().rstrip("/")
                        # Normalizuj do https
                        u = u.replace("http://", "https://")
                        if is_product_category(u):
                            found.add(u)
            break  # wystarczy pierwsza działająca sitemap
        except Exception as e:
            print(f"  Sitemap pominięta ({e})")

    return found


def scrape_page_meta(url: str) -> dict:
    soup = get_soup(url)
    if not soup:
        return {"url": url, "h1": "", "meta_title": "", "meta_description": ""}

    h1_tag = soup.find("h1")
    h1 = h1_tag.get_text(" ", strip=True) if h1_tag else ""

    title_tag = soup.find("title")
    meta_title = title_tag.get_text(strip=True) if title_tag else ""

    desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = desc_tag.get("content", "").strip() if desc_tag else ""

    return {
        "url": url,
        "h1": h1,
        "meta_title": meta_title,
        "meta_description": meta_description,
    }

# ── Google Search Console API ─────────────────────────────────────────────────

def get_gsc_service():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Odświeżam token GSC...")
            creds.refresh(Request())
        else:
            print("Logowanie do Google Search Console (otworzy się przeglądarka)...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print(f"Token zapisany do {TOKEN_FILE}")

    return build("searchconsole", "v1", credentials=creds)


def fetch_gsc_data(service, page_url: str) -> list[dict]:
    """
    Pobiera frazy, kliknięcia, wyświetlenia i pozycję dla danego URL-a.
    Zwraca listę dict-ów posortowanych po kliknięciach (malejąco).
    """
    body = {
        "startDate": GSC_START_DATE.isoformat(),
        "endDate": GSC_END_DATE.isoformat(),
        "dimensions": ["query"],
        "dimensionFilterGroups": [
            {
                "filters": [
                    {
                        "dimension": "page",
                        "operator": "equals",
                        "expression": page_url,
                    }
                ]
            }
        ],
        "rowLimit": 50,
        "startRow": 0,
    }

    try:
        resp = service.searchanalytics().query(siteUrl=SITE_URL, body=body).execute()
        rows = resp.get("rows", [])
        results = []
        for row in rows:
            results.append(
                {
                    "query": row["keys"][0],
                    "clicks": int(row.get("clicks", 0)),
                    "impressions": int(row.get("impressions", 0)),
                    "position": round(row.get("position", 0), 1),
                }
            )
        results.sort(key=lambda x: x["clicks"], reverse=True)
        return results
    except Exception as e:
        print(f"  GSC ERROR dla {page_url}: {e}")
        return []

# ── Zapis do CSV ──────────────────────────────────────────────────────────────

def save_csv(rows: list[dict]):
    """
    Każda fraza GSC to osobny wiersz. Kolumny SEO kategorii są powtórzone.
    """
    fieldnames = [
        "url",
        "h1",
        "meta_title",
        "meta_description",
        "gsc_query",
        "gsc_clicks",
        "gsc_impressions",
        "gsc_position",
    ]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nZapisano {len(rows)} wierszy do {OUTPUT_CSV}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # 1. Odkryj kategorie produktów
    print("=== KROK 1: Odkrywanie kategorii produktów ===")
    categories = discover_categories()

    if not categories:
        print("Nie znaleziono kategorii produktów w sitemapie.")
        return

    print(f"Znaleziono {len(categories)} kategorii produktów\n")

    # 2. Scrapuj meta dane
    print("=== KROK 2: Scraping meta danych ===")
    meta_data = {}
    for i, url in enumerate(sorted(categories), 1):
        print(f"[{i}/{len(categories)}] {url}")
        meta_data[url] = scrape_page_meta(url)
        time.sleep(0.5)

    # 3. Połącz z GSC
    print("\n=== KROK 3: Pobieranie danych z Google Search Console ===")
    print(f"Zakres dat: {GSC_START_DATE} → {GSC_END_DATE}\n")
    service = get_gsc_service()

    all_rows = []
    for i, url in enumerate(sorted(categories), 1):
        meta = meta_data[url]
        print(f"[{i}/{len(categories)}] GSC: {url}")

        # GSC wymaga URL-a dokładnie tak jak zarejestrowano — sprawdzamy obie wersje
        gsc_rows = fetch_gsc_data(service, url)
        if not gsc_rows:
            # Spróbuj z trailing slash
            gsc_rows = fetch_gsc_data(service, url + "/")

        if gsc_rows:
            for gsc in gsc_rows:
                all_rows.append(
                    {
                        "url": meta["url"],
                        "h1": meta["h1"],
                        "meta_title": meta["meta_title"],
                        "meta_description": meta["meta_description"],
                        "gsc_query": gsc["query"],
                        "gsc_clicks": gsc["clicks"],
                        "gsc_impressions": gsc["impressions"],
                        "gsc_position": gsc["position"],
                    }
                )
            print(f"  → {len(gsc_rows)} fraz")
        else:
            # Brak danych GSC — zapisz sam URL z meta
            all_rows.append(
                {
                    "url": meta["url"],
                    "h1": meta["h1"],
                    "meta_title": meta["meta_title"],
                    "meta_description": meta["meta_description"],
                    "gsc_query": "",
                    "gsc_clicks": 0,
                    "gsc_impressions": 0,
                    "gsc_position": "",
                }
            )
            print("  → brak danych GSC")

        time.sleep(0.3)

    # 4. Zapisz CSV
    print("\n=== KROK 4: Zapis do CSV ===")
    save_csv(all_rows)


if __name__ == "__main__":
    main()
