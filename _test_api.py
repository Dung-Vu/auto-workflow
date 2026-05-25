"""Test Shopify API token with different methods."""
import requests

store = "ordinaire-vietnam.myshopify.com"
token = "YOUR_SHOPIFY_TOKEN"
headers = {"X-Shopify-Access-Token": token}

# Test 1: REST API metafields with customer ID from logs
cid = "9222947668211"
url = f"https://{store}/admin/api/2024-07/customers/{cid}/metafields.json"
r = requests.get(url, headers=headers, timeout=10)
print(f"REST metafields (2024-07): {r.status_code}")

# Test 2: REST API with older version
url2 = f"https://{store}/admin/api/2024-01/customers/{cid}/metafields.json"
r2 = requests.get(url2, headers=headers, timeout=10)
print(f"REST metafields (2024-01): {r2.status_code}")

# Test 3: GraphQL
gql_url = f"https://{store}/admin/api/2024-07/graphql.json"
gql_headers = {
    "X-Shopify-Access-Token": token,
    "Content-Type": "application/json",
}
query = """query($id: ID!) {
  customer(id: $id) {
    metafields(first: 20) {
      edges {
        node { namespace key value }
      }
    }
  }
}"""
r3 = requests.post(gql_url, json={"query": query, "variables": {"id": f"gid://shopify/Customer/{cid}"}}, headers=gql_headers, timeout=10)
print(f"GraphQL: {r3.status_code}")
if r3.status_code == 200:
    import json as j
    data = r3.json()
    mfs = data.get("data", {}).get("customer", {}).get("metafields", {}).get("edges", [])
    print(f"  Metafields found: {len(mfs)}")
    for edge in mfs:
        n = edge["node"]
        print(f"  ns={n['namespace']} key={n['key']} val={str(n['value'])[:100]}")
else:
    print(r3.text[:300])

# Test 4: Simple shop endpoint to verify token works at all
url4 = f"https://{store}/admin/api/2024-07/shop.json"
r4 = requests.get(url4, headers=headers, timeout=10)
print(f"Shop endpoint: {r4.status_code}")
