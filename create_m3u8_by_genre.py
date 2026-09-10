#!/usr/bin/env python3
"""
Tạo file M3U8 theo từng thể loại, phân loại phim lẻ / phim bộ.
Tối ưu cho GitHub Actions.
Dùng Playwright scraping HTML pages để tránh bị chặn API.
"""

import asyncio
import json
import os
import sys
import re
from urllib.parse import urljoin, quote

GENRES_CONFIG_FILE = "genres.json"
OUTPUT_DIR = "playlists"
CACHE_FILE = ".stream_cache.json"


# ---------- Playwright-based scraping ----------

async def playwright_get_page(url, max_retries=3):
    """
    Dùng Playwright để navigate đến một trang và lấy HTML.
    """
    from playwright.async_api import async_playwright
    
    for attempt in range(max_retries):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ]
                )
                
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                """)
                
                page = await context.new_page()
                
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                    html = await page.content()
                    await browser.close()
                    return html
                    
                except Exception as e:
                    await browser.close()
                    if attempt < max_retries - 1:
                        print(f"    [RETRY] Attempt {attempt + 1} failed: {e}")
                        await asyncio.sleep(3)
                        continue
                    raise
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"    [RETRY] Attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(3)
                continue
            raise
    
    return None


async def playwright_api_get(path, params=None, max_retries=3):
    """
    Dùng Playwright để gọi API từ trong browser context.
    """
    from playwright.async_api import async_playwright
    from urllib.parse import urlencode
    
    query = ""
    if params:
        query = "?" + urlencode(params)
    full_url = f"https://phim.nguonc.com{path}{query}"
    
    for attempt in range(max_retries):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ]
                )
                
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                """)
                
                page = await context.new_page()
                
                try:
                    # First visit the main site to get cookies/session
                    await page.goto("https://phim.nguonc.com/", wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)
                    
                    # Then make API call
                    result = await page.evaluate("""
                        async (args) => {
                            const [url, params] = args;
                            const query = params ? '?' + new URLSearchParams(params).toString() : '';
                            const fullUrl = url + query;
                            try {
                                const resp = await fetch(fullUrl, {
                                    method: 'GET',
                                    headers: {
                                        'Accept': 'application/json, text/plain, */*',
                                        'Referer': 'https://phim.nguonc.com/',
                                        'Origin': 'https://phim.nguonc.com',
                                    },
                                    credentials: 'omit',
                                    cache: 'no-store',
                                });
                                const text = await resp.text();
                                return {
                                    status: resp.status,
                                    body: text,
                                    ok: resp.ok,
                                };
                            } catch (e) {
                                return {
                                    status: 0,
                                    body: e.message,
                                    ok: false,
                                    error: true,
                                };
                            }
                        }
                    """, [full_url, params or {}])
                    
                    await browser.close()
                    
                    status = result.get("status", 0)
                    body = result.get("body", "")
                    
                    if result.get("error"):
                        raise Exception(f"Fetch error: {body}")
                    
                    if status == 403:
                        raise Exception(f"403 Forbidden from {full_url}")
                    
                    if status != 200:
                        raise Exception(f"HTTP {status} for {full_url}: {body[:200]}")
                    
                    return json.loads(body)
                    
                except Exception as e:
                    await browser.close()
                    if attempt < max_retries - 1:
                        print(f"    [RETRY] Attempt {attempt + 1} failed: {e}")
                        await asyncio.sleep(3)
                        continue
                    raise
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"    [RETRY] Attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(3)
                continue
            raise
    
    return None


# ---------- HTML scraping for genre pages ----------

def parse_genre_page(html):
    """
    Parse HTML của trang thể loại để lấy danh sách phim.
    Trả về list của dict: {slug, name, thumb, year, total_episodes, current_episode}
    """
    films = []
    
    # Pattern 1: Tìm links dạng /phim/{slug}
    # HTML có các thẻ <a> với href="/phim/{slug}"
    film_links = re.findall(r'href="/(?:phim|films)/([^"]+)"[^>]*>\s*<[^>]+>([^<]+)</', html)
    
    # Pattern 2: Tìm trong JSON-LD hoặc data attributes
    # Một số trang có thể có data trong script tags
    
    seen_slugs = set()
    
    for slug, name in film_links:
        slug = slug.strip().rstrip('/')
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        
        films.append({
            "slug": slug,
            "name": name.strip(),
        })
    
    # If regex didn't find much, try a broader search
    if len(films) < 3:
        # Look for any links that might be movie links
        all_links = re.findall(r'href="/([^"]+)"[^>]*class="[^"]*film[^"]*"', html, re.IGNORECASE)
        for link in all_links[:20]:
            if link not in seen_slugs:
                seen_slugs.add(link)
                films.append({"slug": link, "name": link})
    
    return films


async def scrape_genre_page(genre_slug, page=1):
    """
    Scrape trang thể loại để lấy danh sách phim.
    URL pattern: https://phim.nguonc.com/the-loai/{slug}?page={page}
    """
    url = f"https://phim.nguonc.com/the-loai/{genre_slug}?page={page}"
    html = await playwright_get_page(url)
    
    if not html:
        return []
    
    return parse_genre_page(html)


# ---------- API helpers (with Playwright fallback) ----------

API_BASE = "https://phim.nguonc.com/api"


async def api_get(path, params=None):
    """Try API first, fallback to HTML scraping if 403."""
    try:
        return await playwright_api_get(path, params)
    except Exception as e:
        if "403" in str(e):
            print(f"    [WARN] API blocked (403), will use HTML scraping instead")
            return None
        raise


async def get_film_detail(slug):
    """Lấy chi tiết phim."""
    data = await api_get(f"/film/{slug}")
    if data and data.get("status") == "success":
        return data["movie"]
    
    # Fallback: scrape the film page
    url = f"https://phim.nguonc.com/phim/{slug}"
    html = await playwright_get_page(url)
    if not html:
        raise Exception(f"Không lấy được thông tin phim: {slug}")
    
    # Try to extract JSON-LD data
    json_ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    if json_ld_match:
        try:
            data = json.loads(json_ld_match.group(1))
            return {
                "name": data.get("name", slug),
                "original_name": data.get("alternateName", ""),
                "description": data.get("description", ""),
                "thumb_url": data.get("image", ""),
                "year": "",
                "episodes": [],
            }
        except Exception:
            pass
    
    # Minimal fallback
    return {
        "name": slug.replace("-", " ").title(),
        "original_name": "",
        "description": "",
        "thumb_url": "",
        "year": "",
        "episodes": [],
    }


# ---------- Cache ----------

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ---------- Stream extraction ----------

async def get_stream_url_with_playwright(embed_url, max_retries=2):
    """Dùng Playwright để lấy stream URL."""
    from playwright.async_api import async_playwright
    
    for attempt in range(max_retries):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ]
                )
                
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                """)
                
                page = await context.new_page()
                stream_url = None
                
                async def handle_response(response):
                    nonlocal stream_url
                    if stream_url:
                        return
                    url = response.url
                    if ".m3u8" in url and response.status == 200:
                        if "key" not in url and "token" not in url:
                            stream_url = url
                
                page.on("response", handle_response)
                
                try:
                    await page.goto(embed_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                    
                    try:
                        stream_url = await page.evaluate("""
                            async () => {
                                try {
                                    if (window.streamAccess && typeof window.streamAccess.ensure === 'function') {
                                        return await window.streamAccess.ensure();
                                    }
                                } catch(e) {}
                                return null;
                            }
                        """)
                    except Exception:
                        pass
                    
                    if not stream_url:
                        try:
                            retry_btn = await page.query_selector("#verification-retry:not([hidden])")
                            if retry_btn:
                                await retry_btn.click()
                                await page.wait_for_timeout(5000)
                                stream_url = await page.evaluate("""
                                    async () => {
                                        try {
                                            if (window.streamAccess && typeof window.streamAccess.ensure === 'function') {
                                                return await window.streamAccess.ensure();
                                            }
                                        } catch(e) {}
                                        return null;
                                    }
                                """)
                        except Exception:
                            pass
                    
                except Exception as e:
                    if attempt == max_retries - 1:
                        print(f"    [ERROR] {e}", file=sys.stderr)
                finally:
                    await browser.close()
            
            if stream_url:
                return stream_url
            
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
                
        except Exception as e:
            print(f"    [ERROR] Attempt {attempt + 1}: {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
    
    return None


# ---------- Classification ----------

def classify_film(film):
    """Phân loại phim lẻ / phim bộ."""
    total_episodes = film.get("total_episodes")
    current_episode = film.get("current_episode", "")
    
    if total_episodes is None:
        return None
    
    if isinstance(total_episodes, str):
        try:
            total_episodes = int(total_episodes)
        except ValueError:
            total_episodes = 0
    
    if total_episodes == 1 or (total_episodes == 0 and current_episode == "FULL"):
        return "phim-le"
    elif total_episodes > 1:
        return "phim-bo"
    else:
        return None


# ---------- Processing ----------

async def process_film_with_cache(slug, film_name, cache, semaphore):
    """Xử lý một phim, dùng cache."""
    async with semaphore:
        cache_key = f"film_{slug}"
        if cache_key in cache:
            print(f"  [CACHE] {film_name}")
            return cache[cache_key]
        
        try:
            detail = await get_film_detail(slug)
            episodes = detail.get("episodes", [])
            
            if not episodes:
                print(f"  [SKIP] {film_name}: Không có tập")
                return None
            
            # Lấy stream từ tập đầu tiên
            first_ep = None
            for server in episodes:
                items = server.get("items", [])
                if items:
                    first_ep = items[0]
                    break
            
            if not first_ep:
                print(f"  [SKIP] {film_name}: Không có embed URL")
                return None
            
            embed_url = first_ep.get("embed", "")
            if not embed_url:
                print(f"  [SKIP] {film_name}: Embed URL trống")
                return None
            
            print(f"  [FETCH] {film_name}")
            stream_url = await get_stream_url_with_playwright(embed_url)
            
            if stream_url:
                result = {
                    "film_name": detail.get("name", film_name),
                    "original_name": detail.get("original_name", ""),
                    "thumb": detail.get("thumb_url", ""),
                    "year": detail.get("year", ""),
                    "stream_url": stream_url,
                }
                cache[cache_key] = result
                return result
            else:
                print(f"  [FAIL] {film_name}: Không lấy được stream")
                return None
                
        except Exception as e:
            print(f"  [ERROR] {film_name}: {e}", file=sys.stderr)
            return None


async def process_genre(genre, cache, semaphore):
    """Xử lý một thể loại."""
    slug = genre["slug"]
    name = genre["name"]
    max_pages = genre.get("max_pages", 2)
    
    print(f"\n{'='*60}")
    print(f"Thể loại: {name} ({slug})")
    print(f"{'='*60}")
    
    # Fetch films using HTML scraping (bypasses API 403)
    all_films = []
    for page in range(1, max_pages + 1):
        print(f"  Scraping page {page}...")
        try:
            films = await scrape_genre_page(slug, page)
            if not films:
                break
            all_films.extend(films)
            print(f"    -> {len(films)} phim")
            await asyncio.sleep(1)  # Be polite
        except Exception as e:
            print(f"    [ERROR] Page {page}: {e}", file=sys.stderr)
            break
    
    if not all_films:
        print(f"  Không có phim nào.")
        return
    
    # If we got films from scraping, try to enrich with API data
    # (for total_episodes classification)
    enriched_films = []
    for film in all_films:
        try:
            detail = await get_film_detail(film["slug"])
            film["total_episodes"] = detail.get("total_episodes", 0)
            film["current_episode"] = detail.get("current_episode", "")
            film["thumb_url"] = detail.get("thumb_url", film.get("thumb_url", ""))
            film["year"] = detail.get("year", "")
            enriched_films.append(film)
            await asyncio.sleep(0.5)
        except Exception:
            # Use minimal data if detail fetch fails
            film["total_episodes"] = 0
            film["current_episode"] = ""
            film["thumb_url"] = film.get("thumb_url", "")
            film["year"] = ""
            enriched_films.append(film)
    
    # Classify
    phim_le = []
    phim_bo = []
    
    for film in enriched_films:
        classification = classify_film(film)
        if classification == "phim-le":
            phim_le.append(film)
        elif classification == "phim-bo":
            phim_bo.append(film)
    
    print(f"  Phân loại: {len(phim_le)} phim lẻ, {len(phim_bo)} phim bộ")
    
    # Get streams
    print(f"  Đang lấy stream URLs...")
    
    tasks_le = [process_film_with_cache(f["slug"], f["name"], cache, semaphore) for f in phim_le]
    tasks_bo = [process_film_with_cache(f["slug"], f["name"], cache, semaphore) for f in phim_bo]
    
    results_le = await asyncio.gather(*tasks_le, return_exceptions=True)
    results_bo = await asyncio.gather(*tasks_bo, return_exceptions=True)
    
    phim_le_results = [r for r in results_le if isinstance(r, dict)]
    phim_bo_results = [r for r in results_bo if isinstance(r, dict)]
    
    # Generate M3U8
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    if phim_le_results:
        path = os.path.join(OUTPUT_DIR, f"{slug}_phim-le.m3u8")
        build_m3u8(phim_le_results, path, f"Phim lẻ - {name}")
        print(f"  ✓ {path} ({len(phim_le_results)} phim)")
    
    if phim_bo_results:
        path = os.path.join(OUTPUT_DIR, f"{slug}_phim-bo.m3u8")
        build_m3u8(phim_bo_results, path, f"Phim bộ - {name}")
        print(f"  ✓ {path} ({len(phim_bo_results)} phim)")


# ---------- M3U8 builder ----------

def build_m3u8(streams, output_path, group_title=""):
    lines = ["#EXTM3U"]
    for s in streams:
        name = s.get("film_name", "Unknown")
        original = s.get("original_name", "")
        stream_url = s.get("stream_url", "")
        thumb = s.get("thumb", "")
        year = s.get("year", "")
        
        label = name
        if original:
            label += f" ({original})"
        
        lines.append(f'#EXTINF:-1 tvg-name="{name}" tvg-year="{year}" tvg-thumb="{thumb}" group-title="{group_title}",{label}')
        lines.append(stream_url)
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------- Config ----------

def load_genres_config():
    if not os.path.exists(GENRES_CONFIG_FILE):
        print(f"Không tìm thấy {GENRES_CONFIG_FILE}", file=sys.stderr)
        sys.exit(1)
    with open(GENRES_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Main ----------

async def main():
    genres = load_genres_config()
    if not genres:
        print("Danh sách thể loại trống.", file=sys.stderr)
        sys.exit(1)
    
    print(f"Tạo playlist M3U8 cho {len(genres)} thể loại...")
    print("Sử dụng HTML scraping để tránh bị chặn API.")
    
    cache = load_cache()
    semaphore = asyncio.Semaphore(2)
    
    for genre in genres:
        await process_genre(genre, cache, semaphore)
    
    save_cache(cache)
    
    print(f"\n{'='*60}")
    print(f"Hoàn tất! Files đã được lưu trong thư mục: {OUTPUT_DIR}/")
    print(f"{'='*60}")
    
    if os.path.exists(OUTPUT_DIR):
        files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.endswith(".m3u8")])
        for f in files:
            size = os.path.getsize(os.path.join(OUTPUT_DIR, f))
            print(f"  {f} ({size} bytes)")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
