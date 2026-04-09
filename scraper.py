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

# Candidate sitemap paths tried when robots.txt doesn't provide a Sitemap: directive
SITEMAP_CANDIDATES = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap-index.xml",
    "/1_index_sitemap.xml",      # PrestaShop default
    "/sitemap_index.xml.gz",
]

# Patterns that identify a URL as a product category page
_WC_CATEGORY_RE   = re.compile(r"/(product-category|kategoria-produktu|product_cat)/")
_SHOPER_CAT_RE    = re.compile(r"/c/[^/]")                   # /c/nazwa or /c/nazwa/123
_PS_CATEGORY_RE   = re.compile(r"/(categor|kategori)[^/]")   # /category/, /kategorie/, /kategoria-...
_PS_NUMERIC_RE    = re.compile(r"/\d+-[a-z]")                # /123-slug (PrestaShop default)

# WordPress slugs that are never meaningful sections (skip from category-sitemap).
# Match at end-of-string too — _normalize strips trailing slashes.
_WP_SKIP_SLUGS    = re.compile(
    r"/(bez-kategorii|uncategorized|tagi|tag|author|autor)(/|$)",
    re.IGNORECASE,
)


def _find_sitemap_url(base_url: str) -> tuple[str | None, object | None, list[str]]:
    """
    Discovers the main sitemap URL using two strategies (in order):
      1. Parse /robots.txt and extract every 'Sitemap:' directive.
      2. Probe SITEMAP_CANDIDATES one by one.
    Returns (sitemap_url, response, checked_urls).
    If nothing is found, sitemap_url and response are None and checked_urls
    lists every URL that was tried (useful for error messages).
    """
    checked: list[str] = []
    candidates: list[str] = []

    # ── Strategy 1: robots.txt ────────────────────────────────────────────────
    robots_url = f"{base_url}/robots.txt"
    checked.append(robots_url)
    r = _get(robots_url, timeout=8)
    if r and r.status_code == 200:
        for line in r.text.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                url = line.split(":", 1)[1].strip()
                if url not in candidates:
                    candidates.append(url)

    # ── Strategy 2: standard candidates (skip any already found via robots.txt)
    for path in SITEMAP_CANDIDATES:
        url = base_url + path
        if url not in candidates:
            candidates.append(url)

    # ── Try each candidate in order ───────────────────────────────────────────
    for url in candidates:
        checked.append(url)
        resp = _get(url, timeout=10)
        if resp and _is_xml(resp):
            return url, resp, checked

    return None, None, checked


def _get(url: str, timeout: int = 10):
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
    WordPress / WooCommerce (Yoast SEO sitemap index).
    Works for both WooCommerce shops and plain WordPress sites without a shop.

    Priority order — returns the first non-empty result:
      1. WooCommerce product categories  (product_cat sub-sitemap)
      2. WordPress blog categories       (category-sitemap.xml, excl. "bez-kategorii")
      3. WordPress static pages          (page-sitemap.xml, excl. homepage)

    Handles both /sitemap.xml and /sitemap_index.xml (identical on Yoast installs).
    """
    _, r, _ = _find_sitemap_url(base_url)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "lxml-xml")
    sub_sitemaps = soup.find_all("sitemap")
    if not sub_sitemaps:
        return None  # flat sitemap — handled by _try_generic

    # Classify sub-sitemaps by type
    sm_product_cat: list[str] = []
    sm_category:    list[str] = []
    sm_page:        list[str] = []

    for sm in sub_sitemaps:
        loc = sm.find("loc")
        if not loc:
            continue
        url = loc.text.strip()
        if "product_cat" in url:
            sm_product_cat.append(url)
        elif any(k in url for k in ("categor", "kategori")):
            sm_category.append(url)
        elif "page" in url and "page-sitemap" in url:
            sm_page.append(url)

    def _collect(sm_urls: list[str], predicate) -> set[str]:
        found: set[str] = set()
        for sm_url in sm_urls:
            inner = _get(sm_url, timeout=10)
            if not inner:
                continue
            for raw in _extract_locs(inner.text):
                u = _normalize(raw, base_url)
                if u and predicate(u):
                    found.add(u)
        return found

    # Priority 1: WooCommerce product categories
    result = _collect(sm_product_cat, lambda u: bool(_WC_CATEGORY_RE.search(u)))
    if result:
        return sorted(result)

    # Priority 2: WordPress blog categories (skip junk slugs like "bez-kategorii")
    result = _collect(sm_category, lambda u: not _WP_SKIP_SLUGS.search(u))
    if result:
        return sorted(result)

    # Priority 3: WordPress pages — top-level and one-level-deep only (skip homepage
    # and deeply nested pages that are typically plugin-generated product listings).
    base_https = base_url.replace("http://", "https://")

    def _page_ok(u: str) -> bool:
        if u == base_https:
            return False
        # Depth = number of "/" in the path after stripping the domain and leading slash
        depth = u.replace(base_https, "").strip("/").count("/")
        return depth <= 1

    result = _collect(sm_page, _page_ok)
    if result:
        return sorted(result)

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
    PrestaShop: sitemap discovered via _find_sitemap_url (covers /sitemap.xml,
    /1_index_sitemap.xml, robots.txt entries, etc.).
    Can be a flat file or an index referencing sub-sitemaps.
    Category URLs match /category/, /categor*, /kategoria/ or numeric slug pattern.
    """
    _, r, _ = _find_sitemap_url(base_url)
    if not r:
        return None

    found: set[str] = set()
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
        targets = cat_sub if cat_sub else [
            loc.text.strip() for sm in sub_sitemaps if (loc := sm.find("loc"))
        ]
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

    return sorted(found) if found else None


def _is_prestashop_category(url: str) -> bool:
    return bool(_PS_CATEGORY_RE.search(url) or _PS_NUMERIC_RE.search(url))


def _try_generic(base_url: str) -> list[str] | None:
    """
    Fallback: discover sitemap via _find_sitemap_url, parse as a flat file and
    apply broad heuristics to identify category-like URLs (non-product, non-blog,
    path depth 1-2).
    """
    _, r, _ = _find_sitemap_url(base_url)
    if not r:
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

    # Check generator comment in the sitemap (try all known locations)
    _, r, _ = _find_sitemap_url(base_url)
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
    Raises ValueError with a descriptive message if no sitemap or categories found.
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

    # Collect everything that was checked to build a useful error message
    _, _, checked = _find_sitemap_url(base_url)
    checked_str = "\n  ".join(checked) if checked else "(brak)"
    raise ValueError(
        f"Nie znaleziono sitemaps ani kategorii dla: {base_url}\n"
        f"Wykryta platforma: {platform}\n"
        f"Sprawdzone adresy:\n  {checked_str}"
    )


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


# ── Company site discovery ────────────────────────────────────────────────────

MAX_COMPANY_PAGES = 50

_COMPANY_SKIP_RE = re.compile(
    r"/(polityka[_-]prywatnosci|privacy[_-]policy|regulamin|terms|"
    r"login|logowanie|koszyk|cart|checkout|sitemap|"
    r"tag|author|autor|feed)(/|$)|"
    r"[?&]s=",
    re.IGNORECASE,
)


def discover_pages_company(base_url: str) -> list[str]:
    """
    Discovers company website pages for SEO audit via two strategies:
      1. Internal links from <nav> / <header> / elements with menu/nav class
      2. All URLs from sitemap (flat or index) not matching the skip pattern
    Returns a deduplicated sorted list, max MAX_COMPANY_PAGES entries.
    """
    base_url   = base_url.rstrip("/")
    base_https = base_url.replace("http://", "https://")
    found: set[str] = set()

    def _intern(href: str) -> str | None:
        """Normalize href; return HTTPS URL if internal & not skipped, else None."""
        href = href.strip()
        if href.startswith("/"):
            href = base_https + href
        elif href.startswith("http"):
            href = href.replace("http://", "https://")
        else:
            return None
        href = href.rstrip("/")
        if not href.startswith(base_https):
            return None
        path = href[len(base_https):]
        if _COMPANY_SKIP_RE.search(path):
            return None
        return href

    # Strategy 1: nav / header / menu links
    r = _get(base_url, timeout=10)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        containers = (
            soup.find_all("nav") +
            soup.find_all("header") +
            soup.find_all(class_=re.compile(r"\b(menu|nav)\b", re.IGNORECASE))
        )
        seen_ids: set[int] = set()
        for el in containers:
            eid = id(el)
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            for a in el.find_all("a", href=True):
                u = _intern(a["href"])
                if u:
                    found.add(u)

    # Strategy 2: sitemap (flat or index)
    _, r_sitemap, _ = _find_sitemap_url(base_url)
    if r_sitemap:
        sitemap_soup = BeautifulSoup(r_sitemap.text, "lxml-xml")
        sub_sitemaps = sitemap_soup.find_all("sitemap")

        def _add_from_xml(xml_text: str) -> None:
            for raw in _extract_locs(xml_text):
                u = _intern(raw)
                if u:
                    found.add(u)

        if sub_sitemaps:
            for sm in sub_sitemaps:
                loc = sm.find("loc")
                if not loc:
                    continue
                inner = _get(loc.text.strip(), timeout=10)
                if inner:
                    _add_from_xml(inner.text)
        else:
            _add_from_xml(r_sitemap.text)

    return sorted(found)[:MAX_COMPANY_PAGES]


def scrape_page_meta_company(url: str) -> dict:
    """
    Scrapes a company-site page: H1, first 3 H2s, meta title, meta description,
    and whether the page contains a <form> element.
    """
    r = _get(url)
    if not r:
        return {
            "url": url, "h1": "", "h2s": [],
            "meta_title": "", "meta_description": "", "has_form": False,
        }

    soup = BeautifulSoup(r.text, "html.parser")

    h1_tag = soup.find("h1")
    h1     = h1_tag.get_text(" ", strip=True) if h1_tag else ""

    h2s = [tag.get_text(" ", strip=True) for tag in soup.find_all("h2")[:3]]

    title_tag  = soup.find("title")
    meta_title = title_tag.get_text(strip=True) if title_tag else ""

    desc_tag         = soup.find("meta", attrs={"name": "description"})
    meta_description = desc_tag.get("content", "").strip() if desc_tag else ""

    has_form = bool(soup.find("form"))

    return {
        "url": url, "h1": h1, "h2s": h2s,
        "meta_title": meta_title, "meta_description": meta_description,
        "has_form": has_form,
    }
