"""Test Shopify API token for both Ordinaire and Bonario stores."""
import requests
from config import Config

def test_store(store_name, store_domain, token):
    print(f"\n--- Testing {store_name} ({store_domain}) ---")
    headers = {"X-Shopify-Access-Token": token}
    
    # Test shop endpoint
    url = f"https://{store_domain}/admin/api/2024-07/shop.json"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        print(f"Shop endpoint: {r.status_code}")
        if r.status_code == 200:
            print("Token is VALID!")
        else:
            print(f"Token is INVALID! Response: {r.text[:200]}")
    except Exception as e:
        print(f"Error connecting: {e}")

if __name__ == "__main__":
    test_store("Ordinaire", Config.SHOPIFY_STORE, Config.SHOPIFY_ACCESS_TOKEN)
    test_store("Bonario", Config.BONARIO_SHOPIFY_STORE, Config.BONARIO_SHOPIFY_ACCESS_TOKEN)

