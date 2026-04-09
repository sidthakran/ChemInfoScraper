import csv
import io
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import requests
import streamlit as st
from bs4 import BeautifulSoup


# -----------------------------
# Networking / scraping config
# -----------------------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
PRICE_RE = re.compile(
    r"(?:(USD|EUR|INR|CNY|GBP)\s*[:\-]?\s*)?(\$|€|₹|£)?\s*([\d]{1,3}(?:[,\s][\d]{3})*(?:\.\d+)?|[\d]+(?:\.\d+)?)",
    re.IGNORECASE,
)

REQUEST_TIMEOUT = 25


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def safe_join(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)


def new_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(DEFAULT_HEADERS)
    return sess


SESSION = new_session()


def fetch_html(url: str) -> str:
    r = SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.text


# -----------------------------
# ChemicalBook search (best-effort)
# -----------------------------
def chemicalbook_search_url(chemical_name: str) -> str:
    q = urllib.parse.quote(chemical_name)
    return f"https://www.chemicalbook.com/Search_EN.aspx?keyword={q}"


def find_best_result_url(search_html: str, query_name: str, search_url: str) -> Optional[str]:
    soup = BeautifulSoup(search_html, "html.parser")

    # Collect candidate links
    candidates: List[Tuple[str, str]] = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        title = clean_text(a.get_text())
        if not href:
            continue

        href_l = href.lower()
        # Heuristic: ChemicalBook uses various result URL patterns
        if any(key in href_l for key in ["chemicalproductproperty", "cas", "product", "chemical"]):
            abs_url = safe_join(search_url, href)
            if title:
                candidates.append((title, abs_url))

    if not candidates:
        return None

    # Exact match by visible title if possible
    ql = query_name.strip().lower()
    for title, url in candidates:
        if title.strip().lower() == ql:
            return url

    # Fallback to first candidate
    return candidates[0][1]


# -----------------------------
# Supplier extraction (heuristics)
# -----------------------------
def extract_emails(text: str) -> List[str]:
    return list(dict.fromkeys(EMAIL_RE.findall(text or "")))


def extract_phones(text: str) -> List[str]:
    # Phone regex returns full match strings
    return list(dict.fromkeys(PHONE_RE.findall(text or "")))


def extract_rate(text: str) -> str:
    """
    Very loose 'rate' heuristic: find first price-like token.
    Returns a human-readable snippet.
    """
    if not text:
        return ""
    m = PRICE_RE.search(text)
    if not m:
        return ""
    currency_word = m.group(1) or ""
    currency_symbol = m.group(2) or ""
    number = m.group(3) or ""
    token = clean_text(" ".join([currency_word, currency_symbol + number]).strip())
    return token


def parse_suppliers_from_html(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")

    # Try to find repeated “card-like” sections first
    # These selectors are intentionally broad; we dedupe results later.
    selectors = [
        "div.offer", "div.offers", "div.supplier", "div.company",
        "div.list", "div.result", "table", "tr", "li",
    ]

    blocks = []
    for sel in selectors:
        try:
            blocks.extend(soup.select(sel))
        except Exception:
            pass

    # If nothing, fall back to whole page text
    if not blocks:
        blocks = [soup]

    rows: List[Dict[str, str]] = []
    seen = set()

    for b in blocks:
        bt = clean_text(b.get_text(" "))
        if len(bt) < 60:
            continue

        # Supplier name heuristic: first meaningful heading/strong/anchor text
        supplier_name = ""
        for tag in b.select("h1,h2,h3,h4,strong,b,a"):
            t = clean_text(tag.get_text())
            if 2 <= len(t) <= 80:
                supplier_name = t
                break

        emails = extract_emails(bt)
        phones = extract_phones(bt)
        rate = extract_rate(bt)

        if not (supplier_name or emails or phones or rate):
            continue

        key = (supplier_name, tuple(emails), tuple(phones), rate)
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "supplier_name": supplier_name,
            "email": ", ".join(emails),
            "phone": ", ".join(phones),
            "rate": rate,
        })

    # If we got too many noisy rows from <tr>/<li> etc, keep only the better ones:
    # Prefer rows having at least a supplier name or some contact info.
    filtered = []
    for r in rows:
        score = 0
        if r["supplier_name"]:
            score += 2
        if r["email"]:
            score += 2
        if r["phone"]:
            score += 2
        if r["rate"]:
            score += 1
        if score >= 2:
            filtered.append(r)

    # If filtering removed everything, return the raw rows (something is better than nothing)
    return filtered or rows


@st.cache_data(ttl=3600)
def scrape_chemicalbook(chemical_name: str) -> Tuple[Optional[str], List[Dict[str, str]], str]:
    """
    Returns: (matched_url, supplier_rows, debug_message)
    """
    chemical_name = chemical_name.strip()
    if not chemical_name:
        return None, [], "Empty chemical name."

    search_url = chemicalbook_search_url(chemical_name)

    # Gentle throttle (helps avoid instant blocks)
    time.sleep(0.6)

    try:
        search_html = fetch_html(search_url)
    except Exception as e:
        return None, [], f"Failed to load ChemicalBook search page: {e}"

    matched_url = find_best_result_url(search_html, chemical_name, search_url)
    if not matched_url:
        return None, [], "No search results link found (page layout may have changed or access was blocked)."

    time.sleep(0.6)

    try:
        detail_html = fetch_html(matched_url)
    except Exception as e:
        return matched_url, [], f"Found a candidate page but failed to load it: {e}"

    suppliers = parse_suppliers_from_html(detail_html)
    return matched_url, suppliers, "OK"


def rows_to_csv_bytes(rows: List[Dict[str, str]]) -> bytes:
    buf = io.StringIO()
    fieldnames = ["supplier_name", "email", "phone", "rate"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in fieldnames})
    return buf.getvalue().encode("utf-8")


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="ChemInfoScraper", layout="wide")
st.title("ChemInfoScraper (ChemicalBook)")

st.write(
    "Search **ChemicalBook** by **chemical name** (exact match preference) and extract:\n"
    "- supplier name\n"
    "- email\n"
    "- phone\n"
    "- rate (best-effort)\n\n"
    "Note: Results depend on what ChemicalBook exposes in HTML; some fields may be hidden or blocked."
)

chemical_name = st.text_input("Chemical name", placeholder="e.g., Acetone")

col1, col2, col3 = st.columns([1, 1, 2])
with col1:
    run = st.button("Search", type="primary")
with col2:
    show_debug = st.checkbox("Show debug", value=False)
with col3:
    st.caption("Tip: If you get empty results, paste a ChemicalBook page URL and I can tune the parser.")

if run:
    if not chemical_name.strip():
        st.error("Please enter a chemical name.")
        st.stop()

    with st.spinner("Searching & scraping..."):
        url, rows, debug = scrape_chemicalbook(chemical_name)

    if show_debug:
        st.code(f"debug={debug}\nmatched_url={url}")

    if not url:
        st.warning("Could not find a matching ChemicalBook page for that name.")
        st.stop()

    st.write(f"Matched page: {url}")

    if not rows:
        st.warning("No supplier info extracted from the matched page.")
        st.info(
            "This usually means supplier details are not present in the static HTML, or the page layout is different.\n"
            "If you paste the ChemicalBook URL that shows suppliers in your browser, I can tune the selectors."
        )
        st.stop()

    # Display table
    st.dataframe(rows, use_container_width=True)

    # Download CSV
    csv_bytes = rows_to_csv_bytes(rows)
    st.download_button(
        "Download CSV",
        csv_bytes,
        file_name=f"{chemical_name.strip().replace(' ', '_')}_suppliers.csv",
        mime="text/csv",
    )
