from playwright.sync_api import sync_playwright

def fetch_aspx(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        content = page.content()
        browser.close()
        return content

url = "https://investor.costco.com/news/news-details/2026/Costco-Wholesale-Corporation-Reports-May-Sales-Results/default.aspx"
html = fetch_aspx(url)
print(html)