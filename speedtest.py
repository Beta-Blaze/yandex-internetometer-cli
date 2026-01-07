import requests
import time
import threading
import random
import string
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

class YandexSpeedtest:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://yandex.ru/internet/",
            "Origin": "https://yandex.ru",
            "Cache-Control": "no-cache"
        })
        self.running = False
        self.total_bytes = 0
        self.lock = threading.Lock()
        # Буфер 1 MB рандомных данных
        self.payload_chunk = os.urandom(1024 * 1024)

    def _gen_rid(self):
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))

    def get_public_ip(self):
        try:
            r = self.session.get("https://yandex.ru/internet/api/v0/ip", timeout=3)
            return r.json().strip('"')
        except Exception as e:
            return "Unknown"

    def get_config(self):
        try:
            url = "https://yandex.ru/internet/api/v0/get-probes"
            r = self.session.get(url, params={"t": int(time.time() * 1000)}, timeout=5)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def measure_latency(self, config):
        sys.stdout.write("⏳ Задержка: ...\r")
        sys.stdout.flush()

        probes = config.get("latency", {}).get("probes", [])
        best_latency = float('inf')
        best_probe = None

        # 1. Быстрый поиск
        for probe in probes:
            url = probe.get("url")
            target = f"{url}&rid={self._gen_rid()}" if "?" in url else f"{url}?rid={self._gen_rid()}"
            try:
                t0 = time.perf_counter()
                self.session.get(target, timeout=1.0)
                lat = (time.perf_counter() - t0) * 1000
                if lat < best_latency:
                    best_latency = lat
                    best_probe = probe
            except Exception:
                pass

        if not best_probe: return None, 0, 0

        # 2. Точная серия
        url = best_probe.get("url")
        results = []
        host = urlparse(url).netloc

        # Визуализация процесса пинга
        for i in range(1, 11):
            sys.stdout.write(f"⏳ Задержка: {best_latency:.0f} мс | Запрос {i}/10...\r")
            sys.stdout.flush()

            target = f"{url}&rid={self._gen_rid()}" if "?" in url else f"{url}?rid={self._gen_rid()}"
            try:
                t0 = time.perf_counter()
                self.session.get(target, timeout=2)
                results.append((time.perf_counter() - t0) * 1000)
                time.sleep(0.05)
            except Exception:
                pass

        if not results: return None, 0, 0

        avg = statistics.mean(results)
        jitter = statistics.stdev(results) if len(results) > 1 else 0

        # Очистка строки
        sys.stdout.write(" " * 60 + "\r")
        print(f"✅ Пинг:     {avg:.1f} мс  (jitter: {jitter:.1f})  [{host}]")
        return host, avg, jitter

    def _get_url(self, config, section, host, marker):
        probes = config.get(section, {}).get("probes", [])
        for p in probes:
            if host in p.get("url") and marker in p.get("url"):
                return p.get("url"), p.get("size", 0)
        for p in probes:
            if host in p.get("url"):
                return p.get("url"), p.get("size", 0)
        return None, 0

    def _worker_dl(self, url):
        while self.running:
            target = f"{url}&rid={self._gen_rid()}" if "?" in url else f"{url}?rid={self._gen_rid()}"
            try:
                with self.session.get(target, stream=True, timeout=5) as r:
                    if r.status_code != 200: continue
                    for chunk in r.iter_content(65536):
                        if not self.running: break
                        with self.lock:
                            self.total_bytes += len(chunk)
            except Exception as e:
                time.sleep(0.1)

    def _worker_ul(self, url, limit):
        def gen():
            sent = 0
            sl = int(limit) if limit else 10 * 1024 * 1024
            while sent < sl and self.running:
                chunk = self.payload_chunk
                to_send = min(len(chunk), sl - sent)
                yield chunk[:to_send]
                sent += to_send
                with self.lock: self.total_bytes += to_send

        while self.running:
            target = f"{url}&rid={self._gen_rid()}" if "?" in url else f"{url}?rid={self._gen_rid()}"
            try:
                self.session.post(target, data=gen(), timeout=10)
            except Exception:
                time.sleep(0.1)

    def run_speed_test(self, mode, worker, url, size_val=0, threads=4, duration=10):
        label = "Входящая " if mode == "dl" else "Исходящая "
        self.running = True
        self.total_bytes = 0

        executor = ThreadPoolExecutor(max_workers=threads)
        for _ in range(threads):
            if mode == "dl":
                executor.submit(worker, url)
            else:
                executor.submit(worker, url, size_val)

        start = time.time()
        try:
            while True:
                now = time.time()
                elapsed = now - start
                if elapsed >= duration: break

                # Считаем скорость
                speed = (self.total_bytes * 8) / 1_000_000 / elapsed if elapsed > 0 else 0

                # Рисуем прогресс бар
                bar_len = 20
                filled = int(elapsed / duration * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)

                sys.stdout.write(f"\r🚀 {label} [{bar}]  {speed:>7.2f} Мбит/с")
                sys.stdout.flush()

                time.sleep(0.1)
        except KeyboardInterrupt:
            pass

        self.running = False
        executor.shutdown(wait=True)

        # Финальный расчет
        final_speed = (self.total_bytes * 8) / 1_000_000 / (time.time() - start)

        # Перетираем строку прогресса финальным результатом
        sys.stdout.write("\r" + " " * 70 + "\r")  # Очистить строку
        print(f"✅ {label}: {final_speed:.2f} Мбит/с")
        return final_speed

    def run(self):
        print("\n" + "═" * 50)
        print("         YANDEX INTERNETOMETER (CLI)         ")
        print("═" * 50 + "\n")

        my_ip = self.get_public_ip()
        print(f"🔎 IP:       {my_ip}")

        cfg = self.get_config()
        if not cfg: return

        host, ping, jitter = self.measure_latency(cfg)
        if not host: return

        dl_url, _ = self._get_url(cfg, "download", host, "50mb")
        if dl_url:
            self.run_speed_test("dl", self._worker_dl, dl_url)

        up_url, up_size = self._get_url(cfg, "upload", host, "52428800")
        if not up_url: up_url, up_size = self._get_url(cfg, "upload", host, "")

        if up_url:
            self.run_speed_test("ul", self._worker_ul, up_url, up_size)

if __name__ == "__main__":
    try:
        YandexSpeedtest().run()
    except KeyboardInterrupt:
        print("\n\nТест остановлен.")
