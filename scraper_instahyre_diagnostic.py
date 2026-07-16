import requests

response = requests.get("https://www.instahyre.com/jobs/product-manager/")
print("Status:", response.status_code)
print("Has NEXT_DATA:", "__NEXT_DATA__" in response.text)
print("First 300 chars:", response.text[:300])
