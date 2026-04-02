from __future__ import annotations

import time
import requests
from bs4 import BeautifulSoup

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _get(url: str, timeout: int = 15):
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception:
        return None


def discover_categories(base_url: str) -> list[str]:
    """
    Discovers product category URLs from the shop's XML sitemap.
    Works with WooCommerce (product_cat-sitemap.xml) and generic category sitemaps.
    Returns a sorted list of HTTPS URLs.
    """
    base_url = base_url.rstrip("/")
    found: set[str] = set()

    for index_url in [f"{base_url}/sitemap.xml", f"{base_url}/sitemap_index.xml"]:
        r = _get(index_url, timeout=10)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "lxml-xml")
        sub_sitemaps = soup.find_all("sitemap")

        if not sub_sitemaps:
            # Single sitemap — scan all URLs
            for tag in soup.find_all("url"):
                loc = tag.find("loc")
                if loc:
                    u = _normalize(loc.text, base_url)
                    if u:
                        found.add(u)
            break

        for sm in sub_sitemaps:
            loc = sm.find("loc")
            if not loc:
                continue
            loc_url = loc.text.strip()
            # Accept product_cat or category sitemaps
            if not any(k in loc_url for k in ("product_cat", "categor", "kategori")):
                continue

            inner = _get(loc_url, timeout=10)
            if not inner:
                continue
            inner_soup = BeautifulSoup(inner.text, "lxml-xml")
            for tag in inner_soup.find_all("url"):
                loc2 = tag.find("loc")
                if loc2:
                    u = _normalize(loc2.text, base_url)
                    if u:
                        found.add(u)
        break  # stop after first working sitemap index

    return sorted(found)


def _normalize(raw_url: str, base_url: str) -> str | None:
    """Normalize URL to HTTPS and verify it belongs to this domain."""
    u = raw_url.strip().rstrip("/").replace("http://", "https://")
    base_https = base_url.replace("http://", "https://")
    if u.startswith(base_https):
        return u
    return None


def scrape_page_meta(url: str) -> dict:
    r = _get(url)
    if not r:
        return {"url": url, "h1": "", "meta_title": "", "meta_description": ""}

    soup = BeautifulSoup(r.text, "html.parser")

    h1_tag = soup.find("h1")
    h1 = h1_tag.get_text(" ", strip=True) if h1_tag else ""

    title_tag = soup.find("title")
    meta_title = title_tag.get_text(strip=True) if title_tag else ""

    desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = desc_tag.get("content", "").strip() if desc_tag else ""

    return {"url": url, "h1": h1, "meta_title": meta_title, "meta_description": meta_description}
