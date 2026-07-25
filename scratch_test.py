from app import get_media_items

url = 'https://gimyplus.com/ep/432837-7-1.html'
try:
    items = get_media_items(url)
    print("Parsed Items:")
    for item in items:
        print(item)
except Exception as e:
    print("Error:", e)
