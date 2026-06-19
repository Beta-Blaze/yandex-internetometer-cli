import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import argparse
import time
import threading
import random
import string
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from collections import deque

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class YandexSpeedtest:
    def __init__(self):
        self.session = self._make_session()
        self.running = False
        self._thread_bytes = []
        self._thread_bytes_lock = threading.Lock()
        self.payload_chunk = os.urandom(4 * 1024 * 1024)

    @staticmethod
    def _make_session():
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://yandex.ru/internet/",
            "Origin": "https://yandex.ru",
            "Cache-Control": "no-cache",
            "Accept-Encoding": "identity",
        })
        adapter = HTTPAdapter(
            pool_connections=30, pool_maxsize=30,
            max_retries=Retry(total=2, backoff_factor=0.1),
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    @staticmethod
    def _gen_rid():
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))

    def _alloc_counter(self):
        with self._thread_bytes_lock:
            idx = len(self._thread_bytes)
            self._thread_bytes.append(0)
            return idx

    def _total_bytes(self):
        return sum(self._thread_bytes)

    def get_public_ip(self):
        try:
            r = self.session.get("https://yandex.ru/internet/api/v0/ip", timeout=3)
            return r.json().strip('"')
        except Exception:
            return "Unknown"

    def get_config(self):
        try:
            url = "https://yandex.ru/internet/api/v0/get-probes"
            r = self.session.get(url, params={"t": int(time.time() * 1000)}, timeout=5)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def measure_latency(self, config, count=15):
        sys.stdout.write("⏳ Задержка: ...\r")
        sys.stdout.flush()

        probes = config.get("latency", {}).get("probes", [])
        best_latency = float('inf')
        best_probe = None

        for probe in probes:
            url = probe.get("url")
            sep = "&" if "?" in url else "?"
            try:
                t0 = time.perf_counter()
                self.session.get(f"{url}{sep}rid={self._gen_rid()}", timeout=1.5)
                lat = (time.perf_counter() - t0) * 1000
                if lat < best_latency:
                    best_latency = lat
                    best_probe = probe
            except Exception:
                pass

        if not best_probe:
            return None, 0, 0

        url = best_probe.get("url")
        results = []
        host = urlparse(url).netloc

        for i in range(1, count + 1):
            sys.stdout.write(f"⏳ Задержка: {best_latency:.0f} мс | {i}/{count}...\r")
            sys.stdout.flush()
            sep = "&" if "?" in url else "?"
            try:
                t0 = time.perf_counter()
                self.session.get(f"{url}{sep}rid={self._gen_rid()}", timeout=2)
                results.append((time.perf_counter() - t0) * 1000)
            except Exception:
                pass

        if not results:
            return None, 0, 0

        best = min(results)
        avg = statistics.mean(results)
        jitter = statistics.stdev(results) if len(results) > 1 else 0

        sys.stdout.write(" " * 70 + "\r")
        print(f"✅ Пинг:     {best:.1f} мс  (avg: {avg:.1f}, jitter: {jitter:.1f})  [{host}]")
        return host, best, jitter

    def _get_url(self, config, section, host, marker):
        probes = config.get(section, {}).get("probes", [])
        for p in probes:
            u = p.get("url", "")
            if host in u and marker in u:
                return p.get("url"), p.get("size", 0)
        for p in probes:
            if host in p.get("url", ""):
                return p.get("url"), p.get("size", 0)
        return None, 0

    def _get_all_urls(self, config, section, marker):
        probes = config.get(section, {}).get("probes", [])
        urls = [(p.get("url", ""), p.get("size", 0))
                for p in probes if marker and marker in p.get("url", "")]
        if not urls:
            urls = [(p.get("url", ""), p.get("size", 0)) for p in probes]
        return urls

    def _worker_dl(self, url, counter_idx):
        sess = self._make_session()
        sess.cookies.update(self.session.cookies)
        while self.running:
            sep = "&" if "?" in url else "?"
            try:
                with sess.get(f"{url}{sep}rid={self._gen_rid()}", stream=True, timeout=8) as r:
                    if r.status_code != 200:
                        continue
                    for chunk in r.iter_content(524_288):
                        if not self.running:
                            break
                        self._thread_bytes[counter_idx] += len(chunk)
            except Exception:
                pass

    def _worker_ul(self, url, limit, counter_idx):
        sess = self._make_session()
        sess.headers["Content-Type"] = "application/octet-stream"
        payload = self.payload_chunk
        sl = int(limit) if limit else 50 * 1024 * 1024

        def gen():
            sent = 0
            while sent < sl and self.running:
                to_send = min(len(payload), sl - sent)
                yield payload[:to_send]
                sent += to_send
                self._thread_bytes[counter_idx] += to_send

        while self.running:
            sep = "&" if "?" in url else "?"
            try:
                sess.post(f"{url}{sep}rid={self._gen_rid()}", data=gen(), timeout=15)
            except Exception:
                pass

    @staticmethod
    def _calc_windowed_speed(samples, window_sec=3.0):
        if len(samples) < 2:
            return 0.0
        t_now, b_now = samples[-1]
        t_target = t_now - window_sec
        best_i = 0
        for i, (t, _) in enumerate(samples):
            if t <= t_target:
                best_i = i
            else:
                break
        t_start, b_start = samples[best_i]
        dt = t_now - t_start
        return (b_now - b_start) * 8 / 1_000_000 / dt if dt > 0 else 0.0

    def run_speed_test(self, mode, urls, sizes=None, max_threads=16, duration=18, warmup=8.0):
        label = "Входящая " if mode == "dl" else "Исходящая"
        self.running = True
        self._thread_bytes = []

        executor = ThreadPoolExecutor(max_workers=max_threads)
        active_threads = 0

        def launch_batch(count):
            nonlocal active_threads
            for _ in range(count):
                if active_threads >= max_threads:
                    break
                idx = self._alloc_counter()
                url = urls[active_threads % len(urls)]
                sz = sizes[active_threads % len(sizes)] if sizes else 0
                if mode == "dl":
                    executor.submit(self._worker_dl, url, idx)
                else:
                    executor.submit(self._worker_ul, url, sz, idx)
                active_threads += 1

        BATCH = 4
        RAMP_INTERVAL = 2.0
        MAX_RAMPS = 3

        launch_batch(BATCH if warmup > 0 else max_threads)

        start = time.perf_counter()
        first_data_time = None
        next_ramp = None
        ramp_count = 0
        prev_spt = 0.0
        win_start_t = start
        win_start_b = 0
        plateau = False

        samples = deque(maxlen=300)
        warmup_bytes = 0
        peak_speed = 0.0

        try:
            while True:
                now = time.perf_counter()
                elapsed = now - start
                if elapsed >= duration:
                    break

                total = self._total_bytes()

                if first_data_time is None and total > 0:
                    first_data_time = now
                    next_ramp = now + RAMP_INTERVAL
                    win_start_t = now
                    win_start_b = total

                if (elapsed < warmup and not plateau
                        and first_data_time and now >= next_ramp
                        and ramp_count < MAX_RAMPS):
                    dt = now - win_start_t
                    if dt > 0.5:
                        ws = (total - win_start_b) * 8 / 1_000_000 / dt
                        spt = ws / active_threads
                        if prev_spt > 0 and spt < prev_spt * 0.80:
                            plateau = True
                        else:
                            launch_batch(BATCH)
                            ramp_count += 1
                        prev_spt = spt
                    else:
                        launch_batch(BATCH)
                        ramp_count += 1
                    win_start_t = now
                    win_start_b = total
                    next_ramp = now + RAMP_INTERVAL

                if elapsed >= warmup:
                    if warmup_bytes == 0:
                        warmup_bytes = total
                    samples.append((now, total - warmup_bytes))
                    speed = self._calc_windowed_speed(samples, window_sec=3.0)
                    if speed > peak_speed:
                        peak_speed = speed
                else:
                    speed = 0.0

                bar_len = 25
                filled = int(elapsed / duration * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)
                phase = f"⏳ прогрев ×{active_threads}" if elapsed < warmup else f"🚀 ×{active_threads}"
                sys.stdout.write(f"\r{phase:16s} {label} [{bar}]  {speed:>8.2f} Мбит/с")
                sys.stdout.flush()
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass

        self.running = False
        executor.shutdown(wait=False)

        total = self._total_bytes()
        eff_dur = max(time.perf_counter() - start - warmup, 0.1)
        avg_speed = ((total - warmup_bytes) * 8) / 1_000_000 / eff_dur

        sys.stdout.write("\r" + " " * 80 + "\r")
        print(f"{'⚡' if warmup == 0 else '✅'} {label}: {avg_speed:.2f} Мбит/с  (peak: {peak_speed:.2f}, потоков: {active_threads})")
        return avg_speed

    def run(self, fast=False):
        print("\n" + "═" * 50)
        print("       YANDEX INTERNETOMETER (CLI)")
        print("═" * 50 + "\n")

        my_ip = self.get_public_ip()
        print(f"🔎 IP:       {my_ip}")

        cfg = self.get_config()
        if not cfg:
            print("❌ Не удалось получить конфигурацию")
            return

        host, *_ = self.measure_latency(cfg, count=5 if fast else 15)
        if not host:
            print("❌ Не удалось измерить задержку")
            return

        dl_urls = self._get_all_urls(cfg, "download", "50mb")
        if not dl_urls:
            dl_url, _ = self._get_url(cfg, "download", host, "50mb")
            if dl_url:
                dl_urls = [(dl_url, 0)]
        urls_dl = [u for u, _ in dl_urls] if dl_urls else []

        up_url, up_size = self._get_url(cfg, "upload", host, "52428800")
        if not up_url:
            up_url, up_size = self._get_url(cfg, "upload", host, "")
        urls_ul = [up_url] if up_url else []

        opts = {"max_threads": 8, "duration": 6, "warmup": 0.0} if fast else {"duration": 18, "warmup": 8.0}
        if fast:
            print(f"   ⚡ Fast mode: 8 потоков, 6 сек")
        elif urls_dl:
            print(f"   📡 CDN nodes: {len(urls_dl)}")

        if urls_dl:
            self.run_speed_test("dl", urls_dl, **opts)
        if urls_ul:
            self.run_speed_test("ul", urls_ul, sizes=[up_size], **opts)

        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Yandex Internetometer CLI")
    parser.add_argument("-f", "--fast", action="store_true", help="Fast mode: 6 sec, no warmup")
    args = parser.parse_args()

    try:
        YandexSpeedtest().run(fast=args.fast)
    except KeyboardInterrupt:
        print("\n\nТест остановлен.")
