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
import http.client
import socket
import ssl
import json
import math
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from collections import deque

BUFFER_SIZE = 256 * 1024
DEFAULT_UPLOAD_SIZE = 50 * 1024 * 1024
MAX_MEASURED_BYTES = 300 * 1024 * 1024
UPLOAD_BOUNDARY = "----YandexInternetometerCLI"
UPLOAD_PREFIX = (
    f"--{UPLOAD_BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="data"; filename="blob"\r\n'
    "Content-Type: application/octet-stream\r\n\r\n"
).encode("ascii")
UPLOAD_SUFFIX = f"\r\n--{UPLOAD_BOUNDARY}--\r\n".encode("ascii")


class UploadController:
    """HTTP upload sizing used by the web client's multiplicative controller."""

    def __init__(self):
        self.size = 30 * 1024
        self.completions = deque()
        self.lock = threading.Lock()
        self.next_tick = 0.0

    def complete(self):
        with self.lock:
            self.completions.append(time.perf_counter())

    def tick(self, now):
        with self.lock:
            if now < self.next_tick:
                return
            self.next_tick = now + 0.25
            while self.completions and self.completions[0] < now - 1.2:
                self.completions.popleft()
            if not self.completions:
                return
            rate = len(self.completions) / 1.2
            if rate > 10:
                factor = min(1.76, max(1.31, rate / 10))
            elif rate < 4:
                factor = min(0.7, max(0.55, rate / 4))
            else:
                return
            candidate = max(8192, min(16 * 1024 * 1024, int(self.size * factor)))
            if abs(candidate - self.size) / self.size >= 0.08:
                self.size = candidate


class SpeedEstimator:
    """Robust sampled result"""

    def __init__(self, mode, start):
        self.mode = mode
        self.start = start
        self.previous = (start, 0)
        self.samples = deque([(start, 0)])
        self.recent = deque(maxlen=10)
        self.values = []
        self.ewma = None
        self.weight = 0.0

    def add(self, now, count):
        before, previous_count = self.previous
        dt = now - before
        if dt <= 0:
            return 0.0
        self.previous = (now, count)
        self.samples.append((now, count))
        while len(self.samples) > 2 and self.samples[1][0] <= now - 3.0:
            self.samples.popleft()
        if self.mode == "ul":
            since, baseline = self.samples[0]
            speed = (count - baseline) * 8 / 1e6 / (now - since)
        else:
            instantaneous = max(0, count - previous_count) * 8 / 1e6 / dt
            weight = 1 - math.exp(-math.log(2) * dt / 2.5)
            self.ewma = (self.ewma or 0.0) * (1 - weight) + instantaneous * weight
            self.weight = self.weight * (1 - weight) + weight
            self.recent.append(self.ewma / self.weight if self.weight else 0.0)
            speed = statistics.median(self.recent)
        self.values.append(speed)
        return speed

    def result(self):
        # The web UI discards edge samples and takes their median, not max().
        trim = 10 if len(self.values) >= 26 else max(0, (len(self.values) - 6 + 1) // 2)
        values = self.values[trim:-trim] if trim else self.values
        return statistics.median(values) if values else 0.0


class UploadGroup:
    def __init__(self, probe):
        self.probe = probe
        self.ready = threading.Event()
        self.failed = False


class TransferStopped(Exception):
    pass


class TransferState:
    """Counters and cancellation belong to one phase, never to the next test."""

    def __init__(self, workers):
        self.stop = threading.Event()
        self.bytes = [0] * workers
        self.external_stats = [False] * workers
        self.multipart = [False] * workers
        self.controller = None
        self.failed = False
        self.errors = [0] * workers
        self.last_error = [""] * workers
        self.connections = set()
        self.lock = threading.Lock()

    def register(self, connection):
        with self.lock:
            if self.stop.is_set():
                raise TransferStopped()
            self.connections.add(connection)

    def unregister(self, connection):
        with self.lock:
            self.connections.discard(connection)

    def cancel(self):
        self.stop.set()
        with self.lock:
            for connection in self.connections:
                # shutdown interrupts another thread blocked in recv/send.
                # The owning worker closes the HTTP response and connection.
                sock = connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass


class UploadBody:
    """Known-length, rewindable body; slices are views."""

    def __init__(self, payload, size, state, multipart=False):
        self.payload = memoryview(payload)
        self.size = size
        self.position = 0
        self.state = state
        self.prefix = UPLOAD_PREFIX if multipart else b""
        self.suffix = UPLOAD_SUFFIX if multipart else b""
        self.content_type = (f"multipart/form-data; boundary={UPLOAD_BOUNDARY}"
                             if multipart else "application/octet-stream")

    def __len__(self):
        return self.size + len(self.prefix) + len(self.suffix)

    def tell(self):
        return self.position

    def seek(self, position, whence=0):
        if whence != 0 or not 0 <= position <= len(self):
            raise ValueError("unsupported upload position")
        self.position = position

    def __iter__(self):
        while self.position < len(self):
            if self.state.stop.is_set():
                raise TransferStopped()
            if self.position < len(self.prefix):
                chunk = self.prefix[self.position:]
            elif self.position < len(self.prefix) + self.size:
                remaining = len(self.prefix) + self.size - self.position
                chunk = self.payload[:min(len(self.payload), remaining)]
            else:
                chunk = self.suffix[self.position - len(self.prefix) - self.size:]
            yield chunk
            self.position += len(chunk)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class YandexSpeedtest:
    def __init__(self):
        self.session = self._make_session()
        self.transport = "auto"
        self.buffer_size = BUFFER_SIZE
        self.payload_chunk = None
        self._ssl_context = None
        self.last_result = None

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

    def get_public_ip(self):
        for host in ("ipv4-internet.yandex.net", "ipv6-internet.yandex.net"):
            try:
                with self.session.get(f"https://{host}/api/v0/ip", timeout=3) as response:
                    response.raise_for_status()
                    value = response.json()
                if isinstance(value, dict):
                    value = value.get("ip")
                if not isinstance(value, str):
                    continue
                address = ipaddress.ip_address(value.strip().strip('"'))
                if address.is_global and not address.is_multicast:
                    return str(address)
            except (requests.RequestException, OSError, ValueError):
                continue
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
            if urlparse(u).netloc == host and marker in u:
                return p.get("url"), p.get("size", 0)
        for p in probes:
            if urlparse(p.get("url", "")).netloc == host:
                return p.get("url"), p.get("size", 0)
        return None, 0

    def _get_all_urls(self, config, section, marker):
        probes = config.get(section, {}).get("probes", [])
        urls = [(p.get("url", ""), p.get("size", 0))
                for p in probes if marker and marker in p.get("url", "")]
        if not urls:
            urls = [(p.get("url", ""), p.get("size", 0)) for p in probes]
        return urls

    def _uses_requests(self, url):
        if self.transport == "requests":
            return True
        settings = self.session.merge_environment_settings(url, {}, None, None, None)
        # Preserve HTTP(S)/SOCKS proxy.
        return bool(requests.utils.select_proxy(url, settings["proxies"]))

    def _make_connection(self, url):
        parsed = urlparse(url)
        if parsed.scheme == "https":
            return http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=5, context=self._ssl_context,
            )
        if parsed.scheme == "http":
            return http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        raise ValueError("Only HTTP and HTTPS probe URLs are supported")

    @staticmethod
    def _read_tls_download(response, connection, buffer, index, state):
        # Only a length-delimited, persistent TLS body can bypass the buffered
        # file layer. Chunked, close-delimited and other transports stay on the
        # HTTPResponse reader, which owns their framing.
        sock = connection.sock
        ssl_object = getattr(sock, "_sslobj", None)
        read = getattr(ssl_object, "read", None)
        if (not isinstance(sock, ssl.SSLSocket) or not callable(read) or
                response.chunked or response.length is None or
                not callable(getattr(response.fp, "peek", None))):
            return False
        remaining = response.length
        counters = state.bytes
        stopped = state.stop.is_set
        view = memoryview(buffer)
        try:
            if remaining and not stopped():
                prefetched = min(remaining, len(response.fp.peek()))
                if prefetched:
                    count = response.fp.readinto(view[:prefetched])
                    if not count:
                        raise http.client.IncompleteRead(b"", remaining)
                    remaining -= count
                    if not stopped():
                        counters[index] += count
            while remaining and not stopped():
                count = read(min(len(buffer), remaining), buffer)
                if not count:
                    raise http.client.IncompleteRead(b"", remaining)
                remaining -= count
                if not stopped():
                    counters[index] += count
        finally:
            response.length = remaining
        return True

    def _worker(self, mode, url, limit, index, state):
        if self._uses_requests(url):
            return self._worker_requests(mode, url, limit, index, state)
        connection = self._make_connection(url)
        headers = dict(self.session.prepare_request(requests.Request("GET", url)).headers)
        parsed = urlparse(url)
        path = parsed.path or "/"
        path += ("?" + parsed.query) if parsed.query else ""
        sep = "&" if "?" in path else "?"
        buffer = bytearray(self.buffer_size)
        view = memoryview(buffer)
        read_size = min(16384, self.buffer_size)
        try:
            state.register(connection)
            while not state.stop.is_set():
                try:
                    size = int(limit) if limit else (
                        state.controller.size if state.controller else DEFAULT_UPLOAD_SIZE)
                    if mode == "ul":
                        body = UploadBody(self.payload_chunk, size, state, state.multipart[index])
                        headers["Content-Length"] = str(len(body))
                        headers["Content-Type"] = body.content_type
                    connection.request(
                        "GET" if mode == "dl" else "POST",
                        f"{path}{sep}rid={self._gen_rid()}",
                        headers=headers,
                    )
                    if connection.sock is not None:
                        connection.sock.settimeout(15)
                    if mode == "ul":
                        for chunk in body:
                            connection.send(chunk)
                    with connection.getresponse() as response:
                        if 300 <= response.status < 400:
                            # Requests handles redirects, cookies and proxy changes.
                            connection.close()
                            return self._worker_requests(mode, url, limit, index, state)
                        if not 200 <= response.status < 300:
                            raise ValueError(f"HTTP {response.status}")
                        if mode == "dl" and self._read_tls_download(
                                response, connection, buffer, index, state):
                            continue
                        while not state.stop.is_set():
                            read_start = time.perf_counter()
                            count = response.readinto(view[:read_size])
                            if not count:
                                if response.length:
                                    raise http.client.IncompleteRead(b"", response.length)
                                break
                            if mode == "dl" and not state.stop.is_set():
                                state.bytes[index] += count
                                elapsed = time.perf_counter() - read_start
                                # Keep progress responsive on slow links; batch on fast links.
                                if elapsed < 0.025:
                                    read_size = min(self.buffer_size, read_size * 2)
                                elif elapsed > 0.1:
                                    read_size = max(16384, read_size // 2)
                        if mode == "ul" and not state.stop.is_set():
                            if not state.external_stats[index]:
                                state.bytes[index] += size
                            if state.controller and not limit:
                                state.controller.complete()
                except TransferStopped:
                    break
                except (OSError, http.client.HTTPException, ValueError) as error:
                    connection.close()
                    if not state.stop.is_set():
                        state.errors[index] += 1
                        state.last_error[index] = str(error)
                        state.stop.wait(0.1)
        except TransferStopped:
            pass
        finally:
            connection.close()
            state.unregister(connection)

    def _worker_requests(self, mode, url, limit, index, state):
        sep = "&" if "?" in url else "?"
        with self._make_session() as session:
            session.cookies.update(self.session.cookies)
            while not state.stop.is_set():
                try:
                    size = int(limit) if limit else (
                        state.controller.size if state.controller else DEFAULT_UPLOAD_SIZE)
                    address = f"{url}{sep}rid={self._gen_rid()}"
                    options = {"stream": True, "timeout": (2, 2)}
                    if mode == "dl":
                        response = session.get(address, **options)
                    else:
                        body = UploadBody(self.payload_chunk, size, state, state.multipart[index])
                        response = session.post(
                            address, data=body,
                            headers={"Content-Type": body.content_type}, **options,
                        )
                    with response:
                        response.raise_for_status()
                        if mode == "ul" and response.request.method != "POST":
                            raise ValueError("Upload redirected to a non-POST request")
                        for chunk in response.iter_content(self.buffer_size):
                            if state.stop.is_set():
                                break
                            if mode == "dl":
                                state.bytes[index] += len(chunk)
                        if mode == "ul" and not state.stop.is_set():
                            if not state.external_stats[index]:
                                state.bytes[index] += size
                            if state.controller and not limit:
                                state.controller.complete()
                except TransferStopped:
                    break
                except (requests.RequestException, OSError, ValueError) as error:
                    if not state.stop.is_set():
                        state.errors[index] += 1
                        state.last_error[index] = str(error)
                        state.stop.wait(0.1)

    def _worker_stats(self, group, index, state):
        """Count bytes received by Yandex."""
        url = group.probe["statsUrl"]
        connection = None
        try:
            if self._uses_requests(url):
                with self._make_session() as session:
                    session.cookies.update(self.session.cookies)
                    with session.get(url, stream=True, timeout=(3, 2),
                                     headers={"Accept": "text/event-stream"}) as response:
                        response.raise_for_status()
                        if not response.headers.get("Content-Type", "").startswith("text/event-stream"):
                            raise ValueError("Upload stats are not an event stream")
                        group.ready.set()
                        lines = response.iter_lines(chunk_size=128)
                        self._read_stats(lines, index, state)
            else:
                connection = self._make_connection(url)
                state.register(connection)
                parsed = urlparse(url)
                headers = dict(self.session.prepare_request(requests.Request("GET", url)).headers)
                headers["Accept"] = "text/event-stream"
                connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                                   headers=headers)
                with connection.getresponse() as response:
                    if response.status != 200:
                        raise ValueError(f"Stats HTTP {response.status}")
                    if not response.getheader("Content-Type", "").startswith("text/event-stream"):
                        raise ValueError("Upload stats are not an event stream")
                    group.ready.set()
                    self._read_stats(iter(response.readline, b""), index, state)
            if not state.stop.is_set():
                raise OSError("Upload stats stream ended")
        except TransferStopped:
            pass
        except (OSError, http.client.HTTPException, requests.RequestException, ValueError) as error:
            group.failed = True
            if not state.stop.is_set():
                state.errors[index] += 1
                state.last_error[index] = str(error)
                # A live stats stream cannot be restarted: that could count bytes twice.
                if group.ready.is_set():
                    state.failed = True
                    state.stop.set()
        finally:
            group.ready.set()
            if connection is not None:
                connection.close()
                state.unregister(connection)

    @staticmethod
    def _read_stats(lines, index, state):
        for line in lines:
            if state.stop.is_set():
                break
            if not line.startswith(b"data:"):
                continue
            try:
                message = json.loads(line[5:])
                if not isinstance(message, dict):
                    continue
                count = message.get("b")
                if message.get("k") == "u" and type(count) is int and count > 0:
                    state.bytes[index] += count
            except (ValueError, TypeError):
                pass

    def _worker_group_upload(self, group, index, state):
        state.multipart[index] = True
        while not group.ready.wait(0.1):
            if state.stop.is_set():
                return
        if state.stop.is_set():
            return
        if group.failed:
            # The same adaptive HTTP fallback exists in the browser client.
            state.external_stats[index] = False
            return self._worker("ul", group.probe["url"], 0, index, state)
        return self._worker("ul", group.probe["postUrl"], DEFAULT_UPLOAD_SIZE, index, state)

    def run_speed_test(self, mode, urls, sizes=None, max_threads=None,
                       duration=12.0, warmup=0.0, probes=None, min_duration=None,
                       max_bytes=MAX_MEASURED_BYTES):
        if mode not in ("dl", "ul") or not urls:
            raise ValueError("A transfer mode and at least one URL are required")
        if not 0 <= warmup < duration:
            raise ValueError("0 <= warmup < duration is required")
        if max_bytes <= 0 or (max_threads is not None and max_threads < 1):
            raise ValueError("Transfer limits must be positive")
        if sizes and any(int(size) < 0 for size in sizes):
            raise ValueError("Upload sizes cannot be negative")
        if any(urlparse(url).scheme not in ("http", "https") for url in urls):
            raise ValueError("Only HTTP and HTTPS probe URLs are supported")
        if self._ssl_context is None:
            ca_file = (os.environ.get("REQUESTS_CA_BUNDLE") or
                       os.environ.get("CURL_CA_BUNDLE") or requests.certs.where())
            self._ssl_context = ssl.create_default_context(
                **({"capath": ca_file} if os.path.isdir(ca_file) else {"cafile": ca_file}))
            self._ssl_context.set_alpn_protocols(["http/1.1"])
        if mode == "ul" and self.payload_chunk is None:
            self.payload_chunk = os.urandom(self.buffer_size)

        groups = []
        if mode == "ul" and probes:
            by_host = {}
            for probe in probes:
                if probe.get("postUrl") and probe.get("statsUrl") and not probe.get("timeout"):
                    by_host.setdefault(urlparse(probe["postUrl"]).netloc, probe)
            groups = [UploadGroup(probe) for probe in list(by_host.values())[:3]]
        hosts = list(dict.fromkeys(urlparse(url).netloc for url in urls))
        # The web client's POST/SSE transport has up to six upload slots,
        # distributed over the API's server set, with five slots at most per host.
        if groups:
            slots_per_host = min(5, math.ceil(6 / len(groups)))
            slots = groups * slots_per_host
            capacity = len(slots)
            initial = len(groups)
        else:
            capacity = max_threads or max(len(urls), 2 * len(hosts))
            initial = capacity if max_threads is not None else min(capacity, len(hosts))
            slots = None
        if capacity < 1:
            raise ValueError("At least one transfer slot is required")
        minimum = min(6.0, duration) if min_duration is None else min_duration
        label = "Входящая " if mode == "dl" else "Исходящая"
        state = TransferState(capacity + len(groups))
        if mode == "ul":
            state.controller = UploadController()
        executor = ThreadPoolExecutor(max_workers=capacity + len(groups))
        futures = []
        active_threads = 0

        def launch_batch(count):
            nonlocal active_threads
            for _ in range(min(count, capacity - active_threads)):
                index = active_threads
                if slots is not None:
                    state.external_stats[index] = True
                    future = executor.submit(self._worker_group_upload, slots[index], index, state)
                else:
                    state.multipart[index] = bool(mode == "ul" and probes)
                    size = sizes[index % len(sizes)] if sizes else 0
                    future = executor.submit(self._worker, mode, urls[index % len(urls)],
                                             size, index, state)
                futures.append(future)
                active_threads += 1

        start = time.perf_counter()
        measure_start = start if warmup == 0 else None
        measure_bytes = 0
        estimator = SpeedEstimator(mode, start)
        previous_rate = None
        next_ramp = None
        ramp_time, ramp_bytes = start, 0
        plateau = False
        interrupted = False
        speed = 0.0

        try:
            for index, group in enumerate(groups, capacity):
                futures.append(executor.submit(self._worker_stats, group, index, state))
            launch_batch(initial)
            while not state.stop.is_set():
                for future in futures:
                    if future.done():
                        future.result()
                now = time.perf_counter()
                elapsed = now - start
                total = sum(state.bytes)
                if elapsed >= duration or (elapsed >= minimum and total >= max_bytes):
                    break
                if next_ramp is None and total:
                    ramp_time, ramp_bytes = now, total
                    next_ramp = now + 1.2
                if next_ramp is not None and now >= next_ramp and not plateau:
                    rate = (total - ramp_bytes) / (now - ramp_time)
                    if previous_rate is not None and rate < previous_rate * 1.05:
                        plateau = True
                    else:
                        launch_batch(max(1, initial))
                    previous_rate = rate
                    ramp_time, ramp_bytes = now, total
                    next_ramp = now + 1.2
                if state.controller:
                    state.controller.tick(now)
                if elapsed >= warmup:
                    if measure_start is None:
                        measure_start, measure_bytes = now, total
                        estimator = SpeedEstimator(mode, now)
                    speed = estimator.add(now, total - measure_bytes)
                # Match the browser's time/volume progress without exposing tuning knobs.
                progress = min(99, max(elapsed / duration, total / max_bytes) * 100,
                               elapsed / max(minimum, 0.001) * 100)
                filled = min(25, int(progress / 4))
                bar = "█" * filled + "░" * (25 - filled)
                sys.stdout.write(f"\r{label} [{bar}]  {speed:>8.2f} Мбит/с")
                sys.stdout.flush()
                state.stop.wait(min(0.1, max(0.0, duration - elapsed)))
        except KeyboardInterrupt:
            interrupted = True
        finally:
            end = time.perf_counter()
            state.stop.set()
            total = sum(state.bytes)
            state.cancel()
            executor.shutdown(wait=True)

        for future in futures:
            future.result()
        if measure_start is None:
            measured_bytes, measured_seconds = 0, 0.0
        else:
            measured_bytes = max(0, total - measure_bytes)
            measured_seconds = end - measure_start
            estimator.add(end, measured_bytes)
        result_speed = estimator.result()
        errors = sum(state.errors)
        valid = measured_bytes > 0 and not state.failed
        if not valid:
            result_speed = 0.0
        self.last_result = {
            "bytes": measured_bytes, "seconds": measured_seconds,
            "speed_mbps": result_speed, "errors": errors, "workers": active_threads,
            "last_errors": [error for error in state.last_error if error],
            "upload_size": state.controller.size if state.controller else None,
            "server_stats": bool(groups) and any(state.external_stats), "valid": valid,
        }
        sys.stdout.write("\r" + " " * 90 + "\r")
        if valid:
            print(f"✅ {label}: {result_speed:.2f} Мбит/с")
        else:
            print(f"❌ {label}: не удалось получить достоверный результат")
        if errors:
            print(f"   ⚠ Ошибок передачи: {errors}. Последняя: {self.last_result['last_errors'][-1]}")
        if interrupted:
            raise KeyboardInterrupt()
        return result_speed

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

        dl_candidates = cfg.get("download", {}).get("probes", [])
        dl_probes = [probe for probe in dl_candidates if "50mb" in probe.get("url", "")]
        if not dl_probes:
            dl_probes = [probe for probe in dl_candidates if not probe.get("timeout")]
        if not dl_probes:
            dl_probes = dl_candidates
        up_probes = cfg.get("upload", {}).get("probes", [])
        up_probes = sorted(up_probes, key=lambda probe: bool(probe.get("timeout")))
        dl_urls = [probe["url"] for probe in dl_probes if probe.get("url")]
        ul_urls = [probe["url"] for probe in up_probes if probe.get("url")]
        if dl_urls:
            nodes = len({urlparse(url).netloc for url in dl_urls})
            print(f"   📡 CDN nodes: {nodes}")
        def options(section):
            settings = cfg.get(section, {})
            return {
                "duration": 6.0 if fast else settings.get("maxMeasurementTime", 12000) / 1000,
                "min_duration": min(6.0, settings.get("minMeasurementTime", 6000) / 1000),
                "max_bytes": settings.get("maxLoadedSize", MAX_MEASURED_BYTES),
            }
        if dl_urls:
            self.run_speed_test("dl", dl_urls, probes=dl_probes, **options("download"))
        if ul_urls:
            self.run_speed_test("ul", ul_urls, probes=up_probes, **options("upload"))

        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Yandex Internetometer CLI")
    parser.add_argument("-f", "--fast", action="store_true", help="Fast mode: 6 seconds per transfer")
    args = parser.parse_args()
    try:
        speedtest = YandexSpeedtest()
        try:
            speedtest.run(fast=args.fast)
        finally:
            speedtest.session.close()
    except KeyboardInterrupt:
        print("\n\nТест остановлен.")
