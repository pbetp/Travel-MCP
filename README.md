# Travel MCP (thai-travel)

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP-capable AI) check weather forecasts, find accommodation and discover places to visit, using real data. It works for cities anywhere in the world, accepts place names in any language (Thai, English, ...) and replies in whatever language you ask in. All data sources are free and need no API key.

It started as a Thailand-only project, which is why the folder and server are still called `thai-travel`. Thai place names work especially well because they were the first test cases.

## Example prompts

- "Will it rain in Chiang Mai tomorrow?"
- "Find a hostel near Nimmanhaemin, Chiang Mai for 20-22 Nov"
- "What are the best-known temples in Chiang Mai?"
- "Cafes near Siam Paragon"
- "What's the weather like in Tokyo next week?"
- "พรุ่งนี้เชียงใหม่ฝนตกไหม" (Thai works too, and the answer comes back in Thai)
- Pick the **Plan a trip** prompt (`plan_trip`) to have Claude plan weather, accommodation, sights and food together

## What it provides

| Name | Type | What it does | Data source |
|---|---|---|---|
| `get_weather` | tool | Daily forecast (conditions, temperature, chance of rain), up to 16 days ahead | Open-Meteo |
| `search_hotels` | tool | Accommodation around a place, filterable by type and stars, with links to check prices on Google Maps and Booking.com | OpenStreetMap |
| `find_places` | tool | Sights, temples, restaurants, cafes, markets, malls, parks, bars, ranked by how well known they are or by distance | OpenStreetMap |
| `plan_trip` | prompt | A prompt template that tells Claude which tools to call, in what order, to plan a trip | - |

## Limitations

- **No prices, availability or reviews.** There is no free hotel-price API: Agoda and Booking.com require affiliate approval, and Amadeus shut down its Self-Service API in July 2026. Instead the server returns search links so the user can check real prices themselves, and the server instructions tell Claude never to guess prices.
- **OpenStreetMap is volunteer-edited.** Data may be incomplete or out of date, and many accommodation listings have no star rating.
- **The public Overpass API is slow.** When its servers are busy, real tests took 10-40 seconds per query. Asking again is served from the cache instantly.
- **Booking.com links are undocumented.** They use the commonly used URL format; if the site changes it, dates may not be carried over. Agoda is left out because its search URLs need internal city IDs that are not public.
- **Ambiguous place names can resolve to the wrong place.** Every result includes `location.full_name` so Claude can check.

## Architecture

```
Claude --MCP (stdio)--> server.py          defines the tools, validates input, assembles results
                           |
                           +- thai_travel/geocoding.py --> Open-Meteo Geocoding --(not found)--> Nominatim
                           +- thai_travel/weather.py   --> Open-Meteo Forecast
                           +- thai_travel/osm.py       --> Overpass API (3 servers backing each other up)
                           +- thai_travel/links.py         builds Google Maps / Booking.com links
```

Every tool works in two steps: turn the place name into coordinates, then fetch data around those coordinates.

## Design decisions

1. **Structured output.** Tools return Pydantic models that FastMCP turns into an `outputSchema`. Claude knows the result shape in advance, and tests check fields instead of comparing text.
2. **The schema is documentation for the model.** Parameter descriptions live in `Field(description=...)` and have constraints (`ge`/`le`/enums), so invalid values are rejected before any code runs. Everything the model reads is in English so the server is usable from any language.
3. **Language handling.** Results are always English with the local-script name alongside (`name` + `local_name`). The server instructions tell Claude to reply in the user's language, so a Thai question gets a Thai answer and an English question gets an English one.
4. **Tool annotations.** Every tool declares `readOnlyHint` / `idempotentHint` / `openWorldHint` so clients know it is safe to call.
5. **Useful errors.** Messages say how to fix the problem. Input that can be checked without the network (such as a check-in date in the past) is rejected immediately.
6. **Two geocoders.** Testing showed Open-Meteo is good at city names but misses neighbourhoods (Patong, Doi Suthep), while Nominatim finds them. They are chained, with an alias table for nicknames and a rejection of whole countries.
7. **Resilience against a flaky Overpass.** Hedged requests (if the first server is not answering within 6 seconds, also ask a backup in parallel) plus exponential backoff when the queue is full, all capped at 50 seconds, below the 60 seconds after which some MCP clients abandon a tool call.
8. **Ranking fame without extra API calls.** Count the languages a place has names in on OpenStreetMap, plus a Wikipedia article. On the 92 temples of Chiang Mai, the top results are Wat Phra Singh, Wat Chedi Luang and Wat Phan Tao.
9. **Being a good citizen of free services.** Results are cached (coordinates 24 h, weather 30 min, map data 6 h), Nominatim is called at most once per second as its policy requires, and the app name is sent in the User-Agent.

## Install

Requires Python 3.10 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

## Connect to Claude

**Claude Code**: copy `.mcp.json.example` to `.mcp.json` and fix the two paths (the real `.mcp.json` is git-ignored because it holds machine-specific paths), or add the server with:

```bash
claude mcp add thai-travel -- /path/to/.venv/bin/python /path/to/server.py
```

**Claude Desktop**: Settings > Developer > Edit Config, then add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "thai-travel": {
      "command": "C:\\path\\to\\thai-travel-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\thai-travel-mcp\\server.py"]
    }
  }
}
```

After changing the code, restart the MCP server (in Claude Code use `/mcp` and reconnect) so Claude sees the new tool definitions.

To try the tools without Claude, use the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector .venv/Scripts/python.exe server.py
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest            # 76 tests, no network (every API is faked), finishes in about a second
pytest -m live    # tests against the real APIs, to check the services still answer as expected
```

The tests cover:
- Parsing each API's answers, missing data, and OSM data quirks
- Failover between servers: retry on a full queue, hedging a slow server, all servers down, deadline
- Validation of dates and arguments
- The whole system through the real MCP protocol (`tests/test_server.py`), seeing what Claude sees, including a check that every description Claude reads is in English

GitHub Actions runs the lint and tests on Python 3.10 and 3.13 on every push (`.github/workflows/tests.yml`).

## Project layout

```
server.py               MCP tools and prompt (the entry point)
thai_travel/
  geocoding.py          place name -> coordinates
  weather.py            weather forecasts
  osm.py                accommodation and places from OpenStreetMap
  links.py              Google Maps / Booking.com links and stay-date checks
  models.py             result structures (outputSchema)
  net.py                HTTP client and cache
  errors.py             errors that are shown to the user
tests/                  tests (FakeWeb in conftest.py fakes the APIs)
```

## Data sources and licences

- Weather and geocoding: [Open-Meteo](https://open-meteo.com) (CC BY 4.0; the free API is for non-commercial use)
- Accommodation and places: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL), via the [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) and [Nominatim](https://operations.osmfoundation.org/policies/nominatim/)
- Every result carries an `attribution` field naming its source, as the licences require.
- This project is not affiliated with Google or Booking.com. The links are ordinary public search pages.

For heavy or commercial use, run your own Overpass/Nominatim or use a paid service instead of the public servers.
