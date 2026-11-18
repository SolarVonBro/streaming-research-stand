"""Настройки сервиса из переменных окружения (12-factor)."""
import os

# mono — все модули в одном процессе; auth | catalog | delivery | telemetry — отдельный сервис
ROLE = os.environ.get("ROLE", "mono")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
AUTH_URL = os.environ.get("AUTH_URL", "http://auth:8000")  # нужен только delivery в режиме micro
SECRET = os.environ.get("TOKEN_SECRET", "stand-demo-secret").encode()  # не секрет: демо-подпись
MEDIA_SIZES = os.environ.get("MEDIA_SIZES", "/data/media/envivio_sizes.csv")
SEGMENT_SECONDS = float(os.environ.get("SEGMENT_SECONDS", "4.0"))
VIDEO_ID = os.environ.get("VIDEO_ID", "envivio")
# Калибровка стенда: искусственная задержка выдачи сегмента (известный эффект для проверки)
INJECT_DELAY_MS = float(os.environ.get("INJECT_DELAY_MS", "0"))
