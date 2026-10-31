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
MERGE_NEWEST = "newest"  # the most recent fresh reading
MERGE_AVERAGE = "average"  # the mean of the fresh readings
MERGE_METHODS = [MERGE_PRIORITY, MERGE_NEWEST, MERGE_AVERAGE]
# A location entry's idokep.hu location name (entry data), e.g. "Budapest"
LOCATION = "location"

# Data sources, one per idokep.hu page
CURRENT = "current"
HOURLY = "hourly"

# The national Radar service entry, created automatically next to the first
# location entry. Its data is {ENTRY_TYPE: RADAR}; location entries have
# {"location": ...}.
ENTRY_TYPE = "type"
RADAR = "radar"
RADAR_TITLE = "Radar"
RADAR_UNIQUE_ID = "national_radar"
