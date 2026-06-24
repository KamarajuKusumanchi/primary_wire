from playwright.sync_api import sync_playwright


def fetch_aspx(url, wait_selector=None):
    with sync_playwright() as p:
        # Reuses system Chrome and runs headlessly
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()

        # Fast initial navigation bypasses unreliable networkidle waits
        page.goto(url, wait_until="domcontentloaded")

        # Explicitly wait for core content to render in the DOM if a selector is provided
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=10000, state="attached")
            except Exception:
                print(f"Warning: Timed out waiting for selector '{wait_selector}'")

        content = page.content()
        browser.close()
        return content


url = "https://investor.costco.com/news/news-details/2026/Costco-Wholesale-Corporation-Reports-May-Sales-Results/default.aspx"

# Wait specifically for the main article wrapper common to Q4 news layouts
html = fetch_aspx(url, wait_selector="div.q4-details, article, .module_container")
print(html)