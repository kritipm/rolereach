import requests, json

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

for name, url in [
    ("iimjobs", "https://www.iimjobs.com/j/product-management-jobs"),
    ("hirist", "https://www.hirist.tech/j/product-management-jobs"),
]:
    r = requests.get(url, headers=headers, timeout=10)
    start = r.text.find('__NEXT_DATA__')
    chunk = r.text[start:start+3000]
    print(f"\n=== {name} ===")
    print(chunk[:2000])
