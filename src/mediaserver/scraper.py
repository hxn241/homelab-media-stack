"""Torrent scraper: fetches a page (direct ECH request or Cloudflare bypass), extracts
.torrent/magnet links and sends them to qBittorrent. For mejortorrent.eu it also matches
/torrents/… links and falls back to Playwright + bypass cookies when needed."""
import json
import logging
import os
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request

from mediaserver.config import env, env_bool, env_int

app = Flask(__name__)

QB_URL = env("QB_URL", "http://localhost:8080/api/v2")
QB_USER = env("QB_USER", "admin")
QB_PASS = env("QB_PASS")
LISTEN_HOST = env("SCRAPER_HOST", "0.0.0.0")
LISTEN_PORT = env_int("SCRAPER_PORT", 5000)

CF_BYPASS_BASE = env("CF_BYPASS_BASE", "http://127.0.0.1:8000").rstrip("/")
CF_BYPASS_TIMEOUT = env_int("CF_BYPASS_TIMEOUT", 180)
CF_BYPASS_RETRIES = env_int("CF_BYPASS_RETRIES", 5)
CF_BYPASS_BYPASS_CACHE = env_bool("CF_BYPASS_BYPASS_CACHE")
# DoH resolver used to fetch the ECH config (HTTPS record) and get past ISP SNI blocking.
ECH_DOH_URL = env("ECH_DOH_URL", "https://1.1.1.1/dns-query")

_MAGNET_RE = re.compile(r"magnet:\?[^\s\"'<>]+", re.I)
_TORRENT_ABS_RE = re.compile(
    r"https?://[^\s\"'<>]+\.torrent(?:\?[^\s\"'<>]*)?",
    re.I,
)
_TORRENT_PATH_RE = re.compile(
    r"(?<![\w/])/(?:[^\s\"'<>\\]+\.torrent)(?:\?[^\s\"'<>]*)?",
    re.I,
)
# mejortorrent (and clones) usually link the .torrent under /torrents/peliculas|series/
_MEJOR_TORRENT_REL = re.compile(
    r"(/torrents/(?:peliculas|series)/[^\s\"'<>]+\.torrent(?:\?[^\s\"'<>]*)?)",
    re.I,
)
_MEJOR_TORRENT_ABS = re.compile(
    r"https?://[^/\s\"'<>]+/torrents/(?:peliculas|series)/[^\s\"'<>]+\.torrent(?:\?[^\s\"'<>]*)?",
    re.I,
)
_BTIH_URN = re.compile(r"urn:btih:([a-fA-F0-9]{40})\b", re.I)
_BTIH_COLON = re.compile(r"btih:([a-fA-F0-9]{40})\b", re.I)
_BTIH_JSON = re.compile(r'["\']btih["\']\s*:\s*["\']([a-fA-F0-9]{40})["\']', re.I)
_INFO_HASH_JSON = re.compile(
    r'["\']info_?hash["\']\s*:\s*["\']([a-fA-F0-9]{40})["\']', re.I
)


def _block_page_reason(html: str) -> str | None:
    """If the HTML is clearly a Cloudflare challenge/block or another wall, return a short reason."""
    h = html[:24000].lower()
    if "just a moment" in h or "cf-browser-verification" in h:
        return "Cloudflare interstitial challenge"
    if "challenges.cloudflare.com" in h or "cdn-cgi/challenge" in h:
        return "Cloudflare verification page"
    if "checking your browser" in h or "__cf_chl" in h or "cf-challenge" in h:
        return "Cloudflare check (JS/challenge)"
    if "turnstile" in h and "challenges.cloudflare.com" in h:
        return "Cloudflare Turnstile"
    if "sorry, you have been blocked" in h or "you have been blocked" in h:
        return "Cloudflare: IP or client blocked"
    if "error code 1020" in h or "access denied" in h and "cloudflare" in h:
        return "Cloudflare error 1020 / access denied"
    if "attention required" in h and "cloudflare" in h:
        return "Cloudflare (attention required)"
    if "please enable javascript" in h and len(html) < 8000:
        return "requires JavaScript (minimal HTML; usually a challenge or empty shell)"
    return None


def _unwrap_html_if_json_payload(text: str) -> str:
    """Some proxies return JSON with the HTML embedded; the scraper needs the raw HTML."""
    s = text.lstrip()
    if not s.startswith("{"):
        return text
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if isinstance(data, dict):
        for key in ("html", "content", "body", "data", "result"):
            v = data.get(key)
            if isinstance(v, str) and ("<" in v or "magnet:" in v):
                return v
    return text


def fetch_html(url: str, *, force_bypass_cache: bool = False) -> str:
    try:
        params: dict[str, str | int | bool] = {"url": url, "retries": CF_BYPASS_RETRIES}
        if CF_BYPASS_BYPASS_CACHE or force_bypass_cache:
            params["bypassCookieCache"] = True
        r = requests.get(
            f"{CF_BYPASS_BASE}/html",
            params=params,
            timeout=CF_BYPASS_TIMEOUT,
            headers={"Accept": "text/html,*/*;q=0.9"},
        )
    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"Could not connect to the bypass at {CF_BYPASS_BASE}: {e}"
        ) from e

    if r.status_code >= 400:
        raise RuntimeError(f"Bypass HTTP {r.status_code}: {r.text[:400]!r}")

    final_url = r.headers.get("x-cf-bypasser-final-url")
    if final_url:
        logging.info("CF bypass URL final: %s", final_url)

    html = _unwrap_html_if_json_payload(r.text)
    reason = _block_page_reason(html)
    if reason:
        raise RuntimeError(
            f"The bypass returned unusable HTML ({reason}). "
            "Update CloudflareBypassForScraping/Camoufox, try bypassCookieCache "
            "(CF_BYPASS_BYPASS_CACHE=1, or the scraper's automatic retry), "
            "or check whether your IP is blocked by the site."
        )
    logging.info("HTML cf_bypass (%s bytes)", len(html))
    return html


def _cookie_domain_for_host(hostname: str) -> str:
    labels = (hostname or "").lower().split(".")
    if len(labels) >= 2:
        return "." + ".".join(labels[-2:])
    return hostname or ""


def fetch_html_playwright_cf_cookies(page_url: str) -> str:
    """
    mejortorrent loads .torrent links with JS: after the bypass /html we sometimes only get the shell.
    We ask the bypass for cookies, open Chromium with them and wait until the DOM contains /torrents/.
    Disable with: SCRAPE_PLAYWRIGHT_FALLBACK=0
    """
    parsed = urlparse(page_url)
    hostname = parsed.hostname or ""
    cookies_dict: dict[str, str] = {}
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        cr = requests.get(
            f"{CF_BYPASS_BASE}/cookies",
            params={"url": page_url, "retries": CF_BYPASS_RETRIES},
            timeout=min(CF_BYPASS_TIMEOUT, 120),
        )
        if cr.status_code == 200:
            payload = cr.json()
            cookies_dict = payload.get("cookies") or {}
            ua = payload.get("user_agent") or ua
            logging.info(
                "Playwright: %s cookies from the bypass for %s", len(cookies_dict), hostname
            )
    except Exception as e:
        logging.warning("Playwright: no cookies from the bypass (%s)", e)

    cookie_domain = _cookie_domain_for_host(hostname)
    cookie_list: list[dict] = []
    for name, value in cookies_dict.items():
        cookie_list.append(
            {
                "name": name,
                "value": value,
                "domain": cookie_domain,
                "path": "/",
                "secure": page_url.startswith("https"),
            }
        )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=ua)
            if cookie_list:
                context.add_cookies(cookie_list)
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(9000)
            try:
                page.wait_for_selector(
                    'a[href*="/torrents/"], a[href*=".torrent"], a[href^="magnet:"]',
                    timeout=20000,
                )
            except Exception:
                pass
            html = page.content()
            context.close()
        finally:
            browser.close()

    reason = _block_page_reason(html)
    if reason:
        raise RuntimeError(
            f"After Playwright the HTML is still blocked or empty ({reason}). "
            "Try updating Camoufox/the bypass, or set SCRAPE_PLAYWRIGHT_FALLBACK=0 and download manually."
        )
    logging.info("HTML playwright+cookies (%s bytes)", len(html))
    return html


def _mejortorrent_playwright_fallback_enabled(page_url: str) -> bool:
    host = (urlparse(page_url).hostname or "").lower()
    if "mejortorrent.eu" not in host:
        return False
    v = os.environ.get("SCRAPE_PLAYWRIGHT_FALLBACK", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def qb_login():
    try:
        s = requests.Session()
        r = s.post(
            f"{QB_URL}/auth/login",
            data={"username": QB_USER, "password": QB_PASS},
            timeout=10,
        )
        if r.status_code == 200 and r.text == "Ok.":
            return s
        logging.error("qBittorrent login: HTTP %s %s", r.status_code, r.text)
        return None
    except requests.exceptions.RequestException as e:
        logging.error("qBittorrent login: %s", e)
        return None


def _check_torrent_body(body: bytes, source: str) -> None:
    sniff = body[:4000].lower()
    if body.startswith(b"<!") or b"<html" in sniff:
        raise RuntimeError(f"{source} returned HTML instead of the .torrent")


def _direct_ech_get(url: str, accept: str):
    """
    Some ISPs block mejortorrent by SNI (RST during the TLS handshake), so the bypass mirror
    (curl_cffi without ECH) fails with curl (35). Camoufox gets through, and we take its cookies + UA
    (cached, ~0 ms); with DoH + ECH the real SNI is encrypted and the direct request succeeds
    in <1 s, versus ~35 s through /html.
    """
    from curl_cffi import CurlOpt
    from curl_cffi import requests as cffi_requests

    p = urlparse(url)
    origin = f"{p.scheme}://{p.hostname}/"
    cr = requests.get(
        f"{CF_BYPASS_BASE}/cookies",
        params={"url": origin, "retries": CF_BYPASS_RETRIES},
        timeout=min(CF_BYPASS_TIMEOUT, 120),
    )
    cr.raise_for_status()
    payload = cr.json()
    headers = {"Accept": accept, "Referer": origin}
    if payload.get("user_agent"):
        headers["User-Agent"] = payload["user_agent"]
    r = cffi_requests.get(
        url,
        impersonate="firefox",
        cookies=payload.get("cookies") or {},
        headers=headers,
        timeout=30,
        curl_options={CurlOpt.DOH_URL: ECH_DOH_URL.encode(), CurlOpt.ECH: b"hard"},
    )
    r.raise_for_status()
    return r


def fetch_html_direct_ech(url: str) -> str:
    html = _direct_ech_get(url, "text/html,*/*;q=0.9").text
    reason = _block_page_reason(html)
    if reason:
        raise RuntimeError(f"Direct HTML (ECH) blocked: {reason}")
    logging.info("Direct ECH HTML (%s bytes)", len(html))
    return html


def fetch_torrent_file_direct_ech(torrent_url: str) -> bytes:
    body = _direct_ech_get(
        torrent_url, "application/x-bittorrent, application/octet-stream, */*"
    ).content
    _check_torrent_body(body, "The direct download (ECH)")
    return body


def fetch_torrent_file_via_bypass(torrent_url: str) -> tuple[bytes, str]:
    p = urlparse(torrent_url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"Invalid URL: {torrent_url!r}")

    mirror_url = f"{CF_BYPASS_BASE}{p.path or '/'}"
    if p.query:
        mirror_url = f"{mirror_url}?{p.query}"

    try:
        body = fetch_torrent_file_direct_ech(torrent_url)
    except Exception as e:
        logging.warning("Direct ECH download failed (%s); trying the bypass mirror", e)
        r = requests.get(
            mirror_url,
            headers={
                "x-hostname": p.hostname,
                "Accept": "application/x-bittorrent, application/octet-stream, */*",
            },
            timeout=CF_BYPASS_TIMEOUT,
        )
        r.raise_for_status()
        body = r.content
        _check_torrent_body(body, "The mirror")

    name = PurePosixPath(p.path).name or "download.torrent"
    if not name.lower().endswith(".torrent"):
        name = f"{name}.torrent"
    return body, name


def qb_add(session, torrent_url: str) -> bool:
    if not session:
        return False
    try:
        if torrent_url.startswith("magnet:"):
            response = session.post(
                f"{QB_URL}/torrents/add",
                data={"urls": torrent_url},
                timeout=30,
            )
        else:
            data, filename = fetch_torrent_file_via_bypass(torrent_url)
            response = session.post(
                f"{QB_URL}/torrents/add",
                files={"torrents": (filename, data, "application/x-bittorrent")},
                timeout=min(CF_BYPASS_TIMEOUT, 120),
            )

        if response.status_code != 200:
            logging.error("torrents/add HTTP %s %s", response.status_code, response.text)
            return False
        if "fail" in (response.text or "").strip().lower():
            logging.error("torrents/add: %s", response.text)
            return False
        logging.info("Added: %s...", torrent_url[:80])
        return True
    except requests.exceptions.RequestException as e:
        logging.error("torrents/add: %s", e)
        return False
    except Exception as e:
        logging.error("torrent %s: %s", torrent_url[:80], e)
        return False


def _magnets_from_btih_embeds(html: str) -> list[str]:
    """Builds magnet:?xt=urn:btih:… links when the HTML only embeds the hash (e.g. Nuxt JSON)."""
    hashes: list[str] = []
    for rx in (_BTIH_URN, _BTIH_COLON, _BTIH_JSON, _INFO_HASH_JSON):
        for m in rx.finditer(html):
            hashes.append(m.group(1).lower())
    out: list[str] = []
    for h in dict.fromkeys(hashes):
        out.append(f"magnet:?xt=urn:btih:{h}")
    return out


def _regex_torrent_candidates(page_url: str, html: str) -> list[str]:
    found: list[str] = []
    found.extend(_magnets_from_btih_embeds(html))
    for m in _MAGNET_RE.finditer(html):
        found.append(m.group(0).rstrip(").,;]}\""))
    for m in _TORRENT_ABS_RE.finditer(html):
        found.append(m.group(0).rstrip(").,;]}\""))
    for m in _TORRENT_PATH_RE.finditer(html):
        found.append(urljoin(page_url, m.group(0)))
    for m in _MEJOR_TORRENT_ABS.finditer(html):
        found.append(m.group(0).rstrip(").,;]}\""))
    for m in _MEJOR_TORRENT_REL.finditer(html):
        found.append(urljoin(page_url, m.group(1)))
    return found


def _collect_torrent_links(page_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    raw: set[str] = set()
    for cand in _regex_torrent_candidates(page_url, html):
        raw.add(cand)

    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            raw.add(href)
        if "Descargar" in a.get_text() and href:
            raw.add(href)
    for script in soup.find_all("script"):
        if not script.string:
            continue
        if ".torrent" not in script.string and "magnet:" not in script.string:
            continue
        for delim in ('"', "'", "`"):
            for p in script.string.split(delim):
                if ".torrent" in p or "magnet:" in p:
                    raw.add(p)

    out: list[str] = []
    for link in raw:
        link = unquote(link.strip())
        if link.startswith("magnet:"):
            out.append(link)
        elif ".torrent" in link:
            if link.startswith("/"):
                link = urljoin(page_url, link)
            elif link.startswith("//"):
                link = "https:" + link
            out.append(link)
    return list(dict.fromkeys(out))


@app.route("/", methods=["GET"])
def index():
    """Avoids a 404 when opening host:port in a browser; the APIs are /health and /add_page."""
    base = request.url_root.rstrip("/")
    return {
        "service": "mediaserver-scraper",
        "note": "The root / does not process torrents. Use /add_page with the url parameter.",
        "health": f"{base}/health",
        "add_page": f"{base}/add_page?url=<URL_ENCODED_PAGE_URL>",
        "playwright_fallback": "mejortorrent.eu: enabled by default (SCRAPE_PLAYWRIGHT_FALLBACK=0 to disable)",
        "example": (
            f"{base}/add_page?"
            "url=https%3A%2F%2Fwww43.mejortorrent.eu%2Fpelicula%2F30519%2FLa-fuerza-del-deber"
        ),
    }


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return "", 204


@app.route("/add_page", methods=["GET"])
def add_page():
    page_url = request.args.get("url")
    if not page_url:
        return {"status": "error", "message": "Missing 'url' parameter"}, 400

    logging.info("Page: %s", page_url)

    session = qb_login()
    if not session:
        return {"status": "error", "message": "qBittorrent is unreachable"}, 503

    try:
        torrents = []
        try:
            html = fetch_html_direct_ech(page_url)
            torrents = _collect_torrent_links(page_url, html)
        except Exception as e:
            logging.warning("Direct ECH HTML failed (%s)", e)
        if not torrents:
            html = fetch_html(page_url)
            torrents = _collect_torrent_links(page_url, html)
        if (
            not torrents
            and len(html) < 12000
            and "magnet:" not in html
            and ".torrent" not in html.lower()
            and not CF_BYPASS_BYPASS_CACHE
        ):
            logging.info(
                "No links and short HTML; retrying with bypassCookieCache (fresh cookies)"
            )
            html = fetch_html(page_url, force_bypass_cache=True)
            torrents = _collect_torrent_links(page_url, html)

        if not torrents and _mejortorrent_playwright_fallback_enabled(page_url):
            try:
                logging.info("mejortorrent: trying Playwright + bypass cookies…")
                html = fetch_html_playwright_cf_cookies(page_url)
                torrents = _collect_torrent_links(page_url, html)
            except Exception as e:
                logging.error("Playwright fallback: %s", e)
                playwright_note = (
                    f" (Playwright + bypass cookies failed: {e!s}. "
                    "Did you run `playwright install chromium` in the server's venv?)"
                )
            else:
                playwright_note = ""
        else:
            playwright_note = ""

        logging.info("Torrent/magnet links: %s", len(torrents))

        if not torrents:
            hint = ""
            hl = len(html)
            dump_path = os.environ.get("SCRAPE_SAVE_LAST_HTML", "").strip()
            if dump_path:
                try:
                    Path(dump_path).write_text(html, encoding="utf-8", errors="replace")
                    logging.info("Debug HTML saved to %s", dump_path)
                except OSError as oe:
                    logging.warning("Could not write SCRAPE_SAVE_LAST_HTML: %s", oe)
            if hl < 12000 and "magnet:" not in html and ".torrent" not in html.lower():
                hint = (
                    " The HTML is very short and contains no magnet or .torrent: the bypass service "
                    "probably captured the page before the site injected the download links (SPA), "
                    "or it is hitting a block other than the classic challenge. "
                    "Set SCRAPE_SAVE_LAST_HTML=/tmp/last.html to inspect the HTML. "
                    "Update CloudflareBypassForScraping; if the browser's \"View source\" does not "
                    "show btih/magnet/.torrent, the bypass needs to wait for network/JS (not just this scraper)."
                )
            return {
                "status": "ok",
                "found": 0,
                "message": "No torrents found on the page." + hint + playwright_note,
                "torrents": [],
                "html_bytes": hl,
            }

        ok, bad = [], []
        for t in torrents:
            if qb_add(session, t):
                ok.append(t)
            else:
                bad.append(t)

        logging.info("Sent %s, failed %s", len(ok), len(bad))
        return {
            "status": "ok",
            "found": len(torrents),
            "sent": len(ok),
            "failed": len(bad),
            "torrents": torrents,
            "sent_links": ok,
            "failed_links": bad,
        }
    except Exception as e:
        logging.error("%s", e)
        return {"status": "error", "message": str(e)}, 500


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "mediaserver-scraper", "port": LISTEN_PORT, "cf_bypass": CF_BYPASS_BASE}


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    logging.info("http://%s:%s bypass=%s", LISTEN_HOST, LISTEN_PORT, CF_BYPASS_BASE)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
