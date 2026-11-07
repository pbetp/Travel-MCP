"""Data-fetching package behind the Travel MCP server.

Kept separate from server.py so each part can be tested without starting an MCP server:
- geocoding: place name -> coordinates
- weather:   forecasts from Open-Meteo
- osm:       accommodation and places from OpenStreetMap (Overpass API)
- links:     search links to Google Maps / Booking.com so users can check prices themselves
"""

__version__ = "1.0.0"
