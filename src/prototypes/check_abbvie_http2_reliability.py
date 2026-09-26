from curl_cffi import requests

ok = fail = 0
for i in range(20):
    try:
        s = requests.Session(impersonate="chrome124")
        r = s.get("https://investors.abbvie.com/news-releases", timeout=15)
        ok += 1
    except Exception as e:
        fail += 1
        print(i, "FAIL:", e)
print(f"{ok} ok, {fail} failed out of 20")