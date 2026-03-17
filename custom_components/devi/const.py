DOMAIN = "devi"
PLATFORMS = ["climate", "sensor", "binary_sensor"]
SERVICE_GET_SCHEDULE = "get_schedule"
SERVICE_SET_WEEKLY_SCHEDULE = "set_weekly_schedule"
SERVICE_SET_AWAY_INTERVAL = "set_away_interval"
CONF_CLIENT_NAME = "client_name"
CONF_PRIVATE_KEY = "private_key"
CONF_HOUSE_ID = "house_id"
CONF_HOUSE_NAME = "house_name"
CONF_THERMOSTATS = "thermostats"
CONF_USER_NAME = "user_name"
CONF_PEER_ID = "peer_id"
CONF_ROOM_NAME = "room_name"
CONF_SERIAL_NUMBER = "serial_number"
DEFAULT_SCAN_INTERVAL_SECONDS = 30
MIN_SCAN_INTERVAL_SECONDS = 10
MAX_SCAN_INTERVAL_SECONDS = 300

OPT_ENABLE_ADVANCED_ENTITIES = "enable_advanced_entities"
OPT_ENABLE_DIAGNOSTIC_ENTITIES = "enable_diagnostic_entities"
OPT_SCAN_INTERVAL_SECONDS = "scan_interval_seconds"

DEFAULT_ENABLE_ADVANCED_ENTITIES = False
DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES = False


def option_enabled(options: dict, key: str, default: bool) -> bool:
    value = options.get(key, default)
    return value if isinstance(value, bool) else default


def option_scan_interval_seconds(options: dict) -> int:
    value = options.get(OPT_SCAN_INTERVAL_SECONDS, DEFAULT_SCAN_INTERVAL_SECONDS)
    if not isinstance(value, int):
        return DEFAULT_SCAN_INTERVAL_SECONDS
    if value < MIN_SCAN_INTERVAL_SECONDS:
        return MIN_SCAN_INTERVAL_SECONDS
    if value > MAX_SCAN_INTERVAL_SECONDS:
        return MAX_SCAN_INTERVAL_SECONDS
    return value
