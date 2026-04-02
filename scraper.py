from __future__ import annotations

import re
import requests
from bs4 import BeautifulSoup

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── Platform detection ────────────────────────────────────────────────────────
#
# Platform      | Sitemap location(s)                                   | Category URL signal
# --------------|-------------------------------------------------------|--------------------
# WooCommerce   | /sitemap.xml → sub-sitemap with "product_cat"         | /product-category/ or /kategoria-produktu/
# Shoper        | /console/integration/execute/name/GoogleSitemap       | /c/ segment in path
# PrestaShop    | /sitemap.xml or /1_index_sitemap.xml                  | /category/ or /kategoria/ or /\d+-[slug]
# Generic       | /sitemap.xml flat                                     | heuristic path filter

_SHOPER_SITEMAP   = "/console/integration/execute/name/GoogleSitemap"
# Shoper pagination pages: /slug/2, /slug/3 … — skip, keep only /slug (page 1)
_SHOPER_PAGINATION_RE = re.compile(r"/\d+$")

# Patterns that identify a URL as a product category page
_WC_CATEGORY_RE   = re.compile(r"/(product-category|kategoria-produktu|product_cat)/")
_SHOPER_CAT_RE    = re.compile(r"/c/[^/]")                   # /c/nazwa or /c/nazwa/123
_PS_CATEGORY_RE   = re.compile(r"/(categor|kategori)[^/]")   # /category/, /kategorie/, /kategoria-...
_PS_NUMERIC_RE    = re.compile(r"/\d+-[a-z]")                # /123-slug (PrestaShop default)


def _get(url: str, timeout: int = 15):
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r
    except Exception:
        return None


def _normalize(raw: str, base_url: str) -> str | None:
    """Normalize URL to HTTPS and verify it belongs to this domain."""
    u = raw.strip().rstrip("/").replace("http://", "https://")
    base = base_url.replace("http://", "https://")
    return u if u.startswith(base) else None


def _is_xml(r) -> bool:
    ct = r.headers.get("Content-Type", "")
    # Shoper serves sitemaps as application/force-download — check content, not header
    return "xml" in ct or r.text.lstrip().startswith("<?xml")


def _extract_locs(xml_text: str) -> list[str]:
    soup = BeautifulSoup(xml_text, "lxml-xml")
    return [tag.text.strip() for tag in soup.find_all("loc")]


# ── Platform-specific strategies ─────────────────────────────────────────────

def _try_woocommerce(base_url: str) -> list[str] | None:
    """
    WooCommerce/WordPress (Yoast): sitemap index at /sitemap.xml or /sitemap_index.xml
    contains sub-sitemaps; we filter those whose URL contains 'product_cat' or 'categor'.
    Then collect all <loc> URLs from matching sub-sitemaps and filter by WC category path.
    """
    found: set[str] = set()

    for index_url in [f"{base_url}/sitemap.xml", f"{base_url}/sitemap_index.xml"]:
        r = _get(index_url, timeout=10)
        if not r or not _is_xml(r):
            continue

        soup = BeautifulSoup(r.text, "lxml-xml")
        sub_sitemaps = soup.find_all("sitemap")
        if not sub_sitemaps:
            continue  # not an index — handled by _try_generic

        cat_sitemaps = [
            loc.text.strip()
            for sm in sub_sitemaps
            if (loc := sm.find("loc")) and
               any(k in loc.text for k in ("product_cat", "categor", "kategori"))
        ]
        if not cat_sitemaps:
            return None  # looks like WooCommerce index but no category sub-sitemap

        for sm_url in cat_sitemaps:
            inner = _get(sm_url, timeout=10)
            if not inner:
                continue
            for raw in _extract_locs(inner.text):
                u = _normalize(raw, base_url)
                if u and _WC_CATEGORY_RE.search(u):
                    found.add(u)

        return sorted(found) if found else None

    return None


def _try_shoper(base_url: str) -> list[str] | None:
    """
    Shoper: /console/integration/execute/name/GoogleSitemap returns a sitemap index
    with separate sub-sitemaps for products, categories, producers, news, info.
    The categories sub-sitemap contains two URL formats:
      - /pl/c/CategoryName/ID  (with /c/ segment)
      - /slug  +  /slug/2  /slug/3 …  (paginated category — keep only page 1 i.e. no trailing number)
    """
    index_url = base_url + _SHOPER_SITEMAP
    r = _get(index_url, timeout=12)
    if not r or not _is_xml(r):
        return None

    soup = BeautifulSoup(r.text, "lxml-xml")
    sub_sitemaps = soup.find_all("sitemap")

    # Collect URLs from sub-sitemaps whose loc contains "categories"
    # Fall back to scanning the index itself as a flat sitemap if no sub-sitemaps found.
    cat_sitemap_urls: list[str] = []
    if sub_sitemaps:
        for sm in sub_sitemaps:
            loc = sm.find("loc")
            if loc and "categories" in loc.text:
                cat_sitemap_urls.append(loc.text.strip())

    # No sub-sitemaps or none matched "categories" — treat the response as flat
    sources = cat_sitemap_urls if cat_sitemap_urls else [index_url]

    found: set[str] = set()
    for src in sources:
        resp = _get(src, timeout=12) if src != index_url else r
        if not resp:
            continue
        for raw in _extract_locs(resp.text):
            u = _normalize(raw, base_url)
            if not u:
                continue
            # Format 1: /pl/c/Name/ID  or  /c/Name
            # Skip pagination: /pl/c/Name/ID/2, /pl/c/Name/ID/3, …
            # Pattern: after /c/Slug/ID there is an extra numeric page segment
            if _SHOPER_CAT_RE.search(u):
                # Find the segment after the numeric ID; if it's also numeric → pagination
                parts = u.rstrip("/").split("/c/", 1)[-1].split("/")
                # parts = ['Slug', 'ID'] or ['Slug', 'ID', '2']
                is_paginated = len(parts) >= 3 and parts[2].isdigit()
                if not is_paginated:
                    found.add(u)
                continue
            # Format 2: /slug  (skip pagination pages /slug/2, /slug/3, …)
            if not _SHOPER_PAGINATION_RE.search(u):
                # Exclude obviously non-category paths
                path = u.replace(base_url.replace("http://", "https://"), "")
                skip = re.search(
                    r"/(produkt|product|p/|news|blog|info|producent|producer|tag)/",
                    path, re.IGNORECASE,
                )
                if not skip and path.count("/") == 1:  # top-level slug only
                    found.add(u)

    return sorted(found) if found else None


def _try_prestashop(base_url: str) -> list[str] | None:
    """
    PrestaShop: sitemap at /sitemap.xml or /1_index_sitemap.xml.
    Can be a flat file or an index referencing sub-sitemaps.
    Category URLs match /category/, /categor*, /kategoria/ or numeric slug pattern.
    """
    found: set[str] = set()

    for index_url in [
        f"{base_url}/sitemap.xml",
        f"{base_url}/1_index_sitemap.xml",
    ]:
        r = _get(index_url, timeout=10)
        if not r or not _is_xml(r):
            continue

        soup = BeautifulSoup(r.text, "lxml-xml")

        # Index with sub-sitemaps?
        sub_sitemaps = soup.find_all("sitemap")
        if sub_sitemaps:
            # Prefer sub-sitemaps explicitly named for categories
            cat_sub = [
                loc.text.strip()
                for sm in sub_sitemaps
                if (loc := sm.find("loc")) and
                   any(k in loc.text for k in ("categor", "kategori", "category"))
            ]
            targets = cat_sub if cat_sub else [loc.text.strip()
                                               for sm in sub_sitemaps
                                               if (loc := sm.find("loc"))]
            for sm_url in targets:
                inner = _get(sm_url, timeout=10)
                if not inner:
                    continue
                for raw in _extract_locs(inner.text):
                    u = _normalize(raw, base_url)
                    if u and _is_prestashop_category(u):
                        found.add(u)
        else:
            # Flat sitemap
            for raw in _extract_locs(r.text):
                u = _normalize(raw, base_url)
                if u and _is_prestashop_category(u):
                    found.add(u)

        if found:
            return sorted(found)

    return None


def _is_prestashop_category(url: str) -> bool:
    return bool(_PS_CATEGORY_RE.search(url) or _PS_NUMERIC_RE.search(url))


def _try_generic(base_url: str) -> list[str] | None:
    """
    Fallback: parse /sitemap.xml as a flat file and apply broad heuristics
    to identify category-like URLs (non-product, non-blog, has path depth 1-2).
    """
    r = _get(f"{base_url}/sitemap.xml", timeout=10)
    if not r or not _is_xml(r):
        return None

    soup = BeautifulSoup(r.text, "lxml-xml")
    if soup.find_all("sitemap"):
        return None  # is an index — already tried by other strategies

    found: set[str] = set()
    skip = re.compile(
        r"/(produkt|product|blog|post|news|tag|autor|author|"
        r"koszyk|cart|checkout|login|logowanie|rejestracja|"
        r"kontakt|contact|regulamin|polityka|dostawa)[/-]",
        re.IGNORECASE,
    )
    for raw in _extract_locs(r.text):
        u = _normalize(raw, base_url)
        if not u:
            continue
        path = u.replace(base_url.replace("http://", "https://"), "")
        depth = path.strip("/").count("/")
        if depth <= 2 and not skip.search(path):
            found.add(u)

    return sorted(found) if found else None


# ── Platform detector ─────────────────────────────────────────────────────────

def _detect_platform(base_url: str) -> str:
    """
    Detect the e-commerce platform by probing known sitemap URLs and HTML signals.
    Returns one of: 'woocommerce', 'shoper', 'prestashop', 'unknown'.
    """
    # Shoper: unique non-standard sitemap path
    r = _get(base_url + _SHOPER_SITEMAP, timeout=8)
    if r and _is_xml(r):
        return "shoper"

    # Check generator comment in /sitemap.xml
    r = _get(f"{base_url}/sitemap.xml", timeout=8)
    if r and _is_xml(r):
        text = r.text
        if "Yoast SEO" in text or "wordpress" in text.lower():
            return "woocommerce"
        if "PrestaShop" in text or "prestashop" in text.lower():
            return "prestashop"
        soup = BeautifulSoup(text, "lxml-xml")
        if soup.find_all("sitemap"):
            # Index with sub-sitemaps pointing to product_cat → WooCommerce
            locs = [sm.find("loc").text for sm in soup.find_all("sitemap") if sm.find("loc")]
            if any("product_cat" in l for l in locs):
                return "woocommerce"
            return "prestashop"  # PrestaShop also uses sitemap index

    # Check HTML meta generator
    r = _get(base_url, timeout=8)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        gen = soup.find("meta", {"name": "generator"})
        if gen:
            content = gen.get("content", "").lower()
            if "wordpress" in content or "woocommerce" in content:
                return "woocommerce"
            if "prestashop" in content:
                return "prestashop"
            if "shoper" in content:
                return "shoper"

    return "unknown"


# ── Public API ────────────────────────────────────────────────────────────────

def discover_categories(base_url: str) -> list[str]:
    """
    Auto-detects the e-commerce platform and discovers product category URLs.
    Supports WooCommerce, Shoper, PrestaShop, and a generic fallback.
    Returns a sorted list of deduplicated HTTPS URLs.
    """
    base_url = base_url.rstrip("/")
    platform = _detect_platform(base_url)

    strategies = {
        "woocommerce": [_try_woocommerce, _try_generic],
        "shoper":      [_try_shoper, _try_generic],
        "prestashop":  [_try_prestashop, _try_generic],
        "unknown":     [_try_woocommerce, _try_shoper, _try_prestashop, _try_generic],
    }

    for fn in strategies[platform]:
        result = fn(base_url)
        if result:
            return result

    return []


def get_platform(base_url: str) -> str:
    """Returns detected platform name for display purposes."""
    return _detect_platform(base_url.rstrip("/"))


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
