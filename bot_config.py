from datetime import timedelta

HEISTS = ["Casino", "Apartment", "Doomsday", "Cayo Perico"]
MAX_QUEUE_SIZE = 3
MAX_DAILY_HEISTS = 3
HEIST_LIMIT_WINDOW = timedelta(hours=24)
HEIST_COOLDOWN_WINDOW = timedelta(minutes=20)
EMBED_CACHE_TTL = timedelta(minutes=5)

QUEUE_HEADER = "📌 QUEUE STATUS"
QUEUE_MARKER = "(Managed by bot - do not manually edit formatting)"

DEFAULT_QUEUE_LOG_CHANNEL_ID = 1486003037058371847
DEFAULT_HEIST_PANEL_CHANNEL_ID = 1486003037058371847

DB_FILENAME = "heist_data.db"
