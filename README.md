# Яндекс.Интернетометр CLI

Консольная версия сервиса **Яндекс.Интернетометр**. Показывает ваш IP, замеряет пинг, джиттер и скорость интернет-соединения (входящую и исходящую) в реальном времени, используя API Яндекса.

### Быстрый запуск (Linux)

Требуется Python 3 и библиотека `requests`.

```bash
curl -sL https://raw.githubusercontent.com/Beta-Blaze/yandex-internetometer-cli/refs/heads/main/speedtest.py | python3

```

#### Быстрый режим (Fast mode)
Для проведения ускоренного замера (8 потоков, без фазы прогрева) используйте флаг `-f` или `--fast`:

```bash
curl -sL https://raw.githubusercontent.com/Beta-Blaze/yandex-internetometer-cli/refs/heads/main/speedtest.py | python3 - -f
```

---
# Yandex Internetometer CLI 

A lightweight command-line interface for **yandex.ru/internet**. It measures your public IP, latency (ping + jitter), download, and upload speeds in real-time using the Yandex API.

### Quick Start (Linux)

Requires Python 3 and `requests`.

```bash
curl -sL https://raw.githubusercontent.com/Beta-Blaze/yandex-internetometer-cli/refs/heads/main/speedtest.py | python3

```

#### Fast mode
To run a faster test (8 threads, no warmup phase), use the `-f` or `--fast` flag:

```bash
curl -sL https://raw.githubusercontent.com/Beta-Blaze/yandex-internetometer-cli/refs/heads/main/speedtest.py | python3 - -f
```
