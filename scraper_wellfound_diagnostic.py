import os

import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("SCRAPERAPI_KEY")
url = "https://wellfound.com/role/l/product-manager/bangalore"
response = requests.get(f"http://api.scraperapi.com?api_key={api_key}&url={url}&render=true&ultra_premium=true")
print("Status:", response.status_code)
print("Has NEXT_DATA:", "__NEXT_DATA__" in response.text)
print("First 300 chars:", response.text[:300])
