import requests

urls = [
    "https://www.iimjobs.com/j/product-management-jobs",
    "https://jobs.yourstory.com/jobs?q=product+manager&l=Bengaluru",
    "https://www.hirist.tech/j/product-management-jobs",
    "https://www.naukri.com/product-manager-jobs-in-bengaluru",
    "https://www.foundit.in/srp/results?query=product+manager&location=Bengaluru"
]

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

for url in urls:
    try:
        r = requests.get(url, headers=headers, timeout=10)
        print(f"\n{url}")
        print(f"Status: {r.status_code}")
        print(f"Has NEXT_DATA: {'__NEXT_DATA__' in r.text}")
        print(f"Has job data: {'product manager' in r.text.lower()}")
    except Exception as e:
        print(f"\n{url}")
        print(f"Error: {e}")
