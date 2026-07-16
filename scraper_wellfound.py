import re

import requests

WELLFOUND_URL = "https://wellfound.com/role/r/product-manager"

NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


def diagnose():
    resp = requests.get(
        WELLFOUND_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    print(f"Status code: {resp.status_code}")

    match = NEXT_DATA_PATTERN.search(resp.text)
    if match:
        print("__NEXT_DATA__ found. First 500 chars:")
        print(match.group(1)[:500])
    else:
        print("__NEXT_DATA__ NOT found in response HTML.")
        print("First 500 chars of response body:")
        print(resp.text[:500])


if __name__ == "__main__":
    diagnose()
