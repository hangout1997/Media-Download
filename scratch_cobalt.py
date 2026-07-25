import requests

url = "https://api.cobalt.tools/api/json"
headers = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}
payload = {
    "url": "https://x.com/Astro_Petit/status/1709489505085350280"
}
try:
    resp = requests.post(url, json=payload, headers=headers)
    print(resp.json())
except Exception as e:
    print(e)
