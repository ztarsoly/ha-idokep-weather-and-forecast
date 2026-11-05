"""Constants for idokep."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

MANUFACTURER = "Időkép"
DOMAIN = "idokep_weather_and_forecast"
ATTRIBUTION = "Weather forecast by Időkép"

# Optional weather station (automata) codes of a location entry, e.g.
# ["jozsamet", "olig13"]; set in the config flow (entry data) or the options
STATION = "station"
# How the stations' readings are merged into one value per sensor
MERGE_METHOD = "merge_method"
MERGE_PRIORITY = "priority"  # the first listed station with a fresh reading
MERGE_NEWEST = "newest"  # the station with the most recent fresh reading
MERGE_AVERAGE = "average"  # the mean of the fresh readings
MERGE_METHODS = [MERGE_PRIORITY, MERGE_NEWEST, MERGE_AVERAGE]
# A station reading older than this many minutes is reported as unknown; the
# station files cover about 24 hours, so a longer limit would change nothing
MAX_AGE = "max_age"
DEFAULT_MAX_AGE = 60
MIN_MAX_AGE = 1
MAX_MAX_AGE = 1440
# A location entry's idokep.hu location name (entry data), e.g. "Budapest"
LOCATION = "location"

# Data sources, one per idokep.hu page
CURRENT = "current"
HOURLY = "hourly"
HUMIDITY_MAP = "humidity_map"
PRESSURE_MAP = "pressure_map"
UV_MAP = "uv_map"

# The national Radar service entry, created automatically next to the first
# location entry. Its data is {ENTRY_TYPE: RADAR}; location entries have
# {"location": ...}.
ENTRY_TYPE = "type"
RADAR = "radar"
RADAR_TITLE = "Radar"
RADAR_UNIQUE_ID = "national_radar"
