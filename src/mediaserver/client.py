"""Simple script to call the scraper `/add_page` endpoint with a page URL.

Usage:
  mediaserver-add "https://site.example/path"
Or use env var:
  TEST_URL="https://..." mediaserver-add

Prints HTTP status and JSON/text response.
"""
import sys
import os
import requests


def main():
    url = None
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = os.environ.get("TEST_URL")

    if not url:
        print("Usage: mediaserver-add <PAGE_URL>  or set TEST_URL env var")
        sys.exit(2)

    endpoint = os.environ.get("SCRAPER_ENDPOINT", "http://127.0.0.1:5000/add_page")
    try:
        resp = requests.get(endpoint, params={"url": url}, timeout=60)
    except requests.RequestException as e:
        print("Request failed:", e)
        sys.exit(1)

    print("HTTP", resp.status_code)
    try:
        print(resp.json())
    except Exception:
        print(resp.text)


if __name__ == "__main__":
    main()
