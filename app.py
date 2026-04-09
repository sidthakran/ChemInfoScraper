import re
import time
import urllib.parse
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def clean_text(x: str) -> str:
    return re.sub(r"\s+", " ", (x or "").strip())


def fetch(url: str, timeout=25) -> str:
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def chemicalbook_search_exact(chemical_name: str):
    """
    Best-effort: use a general search query and pick the first result whose title matches exactly.
    This may need adjustment once we see your exact ChemicalBook target pages.
    """
    q = urllib.parse.quote(chemical_name)
    # ChemicalBook has multiple search entrypoints; this is a common pattern.
    search_url = f"https://www.chemicalbook.com/Search_EN.aspx?keyword={q}"
    html = fetch(search_url)
    soup = BeautifulSoup(html, "html.parser")

    # Heuristic: find candidate result links
    candidates = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = clean_text(a.get_text())
        if not href:
            continue
        if "chemicalproductproperty_en" in href.lower() or "product" in href.lower():
            candidates.append((text, urllib.parse.urljoin(search_url, href)))

    # Exact title match first; else fallback to first candidate
    for text, url in candidates:
        if text.lower() == chemical_name.lower():
            return url
    return candidates[0][1] if candidates else None


def parse_suppliers_from_page(html: str):
    """
    Supplier extraction is site-structure dependent.
    We'll implement multiple heuristics:
      - scan for blocks that look like supplier cards
      - extract email/phone via regex from visible text
      - attempt to detect price/rate patterns
    """
    soup = BeautifulSoup(html, "html.parser")
    text_all = soup.get_text("\n")
    # Common regexes
    email_re = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
    phone_re = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")

    # Heuristic: supplier "cards" — you will likely tweak these selectors after sharing an example page
    possible_blocks = []
    for sel in [
        "div.company", "div.supplier", "div.offer", "div.list", "div#company", "div#supplier",
        "table", "div:has(a[href*='Company'])"
    ]:
        try:
            possible_blocks.extend(soup.select(sel))
        except Exception:
            pass

    # If we failed to find blocks, fallback to whole page as one block (still tries to find at least something)
    if not possible_blocks:
        possible_blocks = [soup]

    rows = []
    seen = set()

    for block in possible_blocks:
        block_text = clean_text(block.get_text(" "))
        if len(block_text) < 40:
            continue

        # Supplier name heuristic: first strong/heading-ish text
        supplier = ""
        for tag in block.select("h1,h2,h3,strong,b,a"):
            t = clean_text(tag.get_text())
            if 2 <= len(t) <= 80:
                supplier = t
                break

        emails = list(dict.fromkeys(email_re.findall(block_text)))
        phones = list(dict.fromkeys(phone_re.findall(block_text)))

        # Rate/price heuristic examples: "$123", "USD 123", "Price: 123", "₹", "€"
        rate = ""
        m = re.search(r"(USD|EUR|INR|CNY|\$|€|₹)\s*[:\-]?\s*[\d,.]+", block_text, flags=re.IGNORECASE)
        if m:
            rate = m.group(0)

        # Only include rows that have at least a supplier name or contact info
        if not (supplier or emails or phones or rate):
            continue

        key = (supplier, tuple(emails), tuple(phones), rate)
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "supplier_name": supplier,
            "email": ", ".join(emails),
            "phone": ", ".join(phones),
            "rate": rate,
        })

    # If nothing was extracted from blocks but there are emails/phones on the page, return at least one row
    if not rows:
        emails = list(dict.fromkeys(email_re.findall(text_all)))
        phones = list(dict.fromkeys(phone_re.findall(text_all)))
        if emails or phones:
            rows.append({
                "supplier_name": "",
                "email": ", ".join(emails),
                "phone": ", ".join(phones),
                "rate": "",
            })

    return rows


@st.cache_data(ttl=3600)
def scrape_chemicalbook_suppliers(chemical_name: str):
    detail_url = chemicalbook_search_exact(chemical_name)
    if not detail_url:
        return None, []

    html = fetch(detail_url)
    rows = parse_suppliers_from_page(html)
    return detail_url, rows


st.set_page_config(page_title="Chemical Supplier Scraper", layout="wide")
st.title("Chemical Supplier Scraper (ChemicalBook)")

st.write("Enter a **chemical name** (exact match). The app will try to find supplier name, email, phone, and rate.")

chemical_name = st.text_input("Chemical name", placeholder="e.g., Acetone")
col1, col2 = st.columns([1, 1])
with col1:
    run = st.button("Search", type="primary")
with col2:
    st.caption("Note: Some data may be hidden by the site; results depend on what is visible in HTML.")

if run:
    if not chemical_name.strip():
        st.error("Please enter a chemical name.")
        st.stop()

    with st.spinner("Searching ChemicalBook and scraping suppliers..."):
        try:
            url, rows = scrape_chemicalbook_suppliers(chemical_name.strip())
            if not url:
                st.warning("No ChemicalBook result found for that exact name.")
                st.stop()

            st.write(f"Matched page: {url}")

            if not rows:
                st.warning("Found the chemical page, but couldn't extract supplier info with current heuristics.")
                st.info("If you paste a sample ChemicalBook page URL that has suppliers, I can tune the selectors.")
                st.stop()

            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True)

            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, file_name=f"{chemical_name}_suppliers.csv", mime="text/csv")

        except requests.HTTPError as e:
            st.error(f"HTTP error while scraping: {e}")
        except Exception as e:
            st.error(f"Error: {e}")
