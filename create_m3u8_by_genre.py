#!/usr/bin/env python3
"""
Tạo file M3U8 theo từng thể loại, phân loại phim lẻ / phim bộ.
Tối ưu cho GitHub Actions.
Dùng Playwright cho TẤT CẢ requests để tránh bị chặn IP.
"""

import asyncio
import json
import os
import sys
import time
from urllib.parse import quote

GENRES_CONFIG_FILE = "genres.json"
OUTPUT_DIR = "playlists"
CACHE_FILE = ".stream_cache.json"


# ---------- Playwright-based HTTP client ----------

async def playwright_get(url, params=None, max_retries=3):
    """
    Dùng Playwright để gọi HTTP GET từ trong browser context.
    Tránh bị chặn bởi Cloudflare/WAF vì requests xuất phát từ trình duyệt thật.
    """
    from playwright.async_api import async_playwright
    
    query = ""
    if params:
        from urllib.parse import urlencode
        query = "?" + urlencode(params)
    full_url = url + query
    
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
                    extra_http_headers={
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://phim.nguonc.com/",
                        "Origin": "https://phim.nguonc.com",
                    }
                )
                
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                """)
                
                page = await context.new_page()
                
                try:
                    # Navigate to main site first to establish session
                    await page.goto("https://phim.nguonc.com/", wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)
                    
                    # Now make the API request from within the browser context
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
                    """, [url, params or {}])
                    
                    await browser.close()
                    
                    status = result.get("status", 0)
                    body = result.get("body", "")
                    
                    if result.get("error"):
                        raise Exception(f"Fetch error: {body}")
                    
                    if status == 403:
                        raise Exception(f"403 Forbidden from {url}")
                    
                    if status == 429:
                        wait = int(result.get("retry_after", 10))
                        print(f"    [RATE LIMIT] Waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    
                    if status != 200:
                        raise Exception(f"HTTP {status} for {url}: {body[:200]}")
                    
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


# ---------- Data fetching ----------

API_BASE = "https://phim.nguonc.com/api"


async def api_get(path, params=None):
    url = f"{API_BASE}{path}"
    return await playwright_get(url, params)


async def get_film_detail(slug):
    data = await api_get(f"/film/{slug}")
    if data.get("status") != "success":
        raise Exception(f"Không lấy được thông tin phim: {data}")
    return data["movie"]


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
        if slug in cache:
            print(f"  [CACHE] {film_name}")
            return cache[slug]
        
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
                cache[slug] = result
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
    
    # Fetch films
    all_films = []
    for page in range(1, max_pages + 1):
        print(f"  Fetching page {page}...")
        try:
            data = await api_get(f"/films/the-loai/{slug}", params={"page": page})
            items = data.get("items", [])
            if not items:
                break
            all_films.extend(items)
            print(f"    -> {len(items)} phim")
        except Exception as e:
            print(f"    [ERROR] Page {page}: {e}", file=sys.stderr)
            break
    
    if not all_films:
        print(f"  Không có phim nào.")
        return
    
    # Classify
    phim_le = []
    phim_bo = []
    
    for film in all_films:
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
