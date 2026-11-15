# Travel MCP

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP-capable AI) check weather forecasts, find accommodation and discover places to visit, using real data. It works for cities anywhere in the world, accepts place names in any language (Thai, English, ...) and replies in whatever language you ask in. All data sources are free and need no API key.

The whole server is one readable file, [server.py](server.py). It can run on your own computer, or in the cloud so you can use it from claude.ai on any device, including a phone.

## Example prompts

- "Will it rain in Chiang Mai tomorrow?"
- "Find a hostel near Nimmanhaemin, Chiang Mai for 20-22 Nov"
- "What are the best-known temples in Chiang Mai?"
- "Cafes near Siam Paragon"
- "พรุ่งนี้เชียงใหม่ฝนตกไหม" (Thai works too, and the answer comes back in Thai)
- Pick the **Plan a trip** prompt to have Claude plan weather, accommodation, sights and food together

## What it provides

| Name | Type | What it does | Data source |
|---|---|---|---|
| `get_weather` | tool | Daily forecast (conditions, temperature, chance of rain), up to 16 days ahead | Open-Meteo |
| `search_hotels` | tool | Accommodation around a place, filterable by type and stars, with links to check prices on Google Maps and Booking.com | OpenStreetMap |
| `find_places` | tool | Sights, temples, restaurants, cafes, markets, malls, parks, bars; best-known first or nearest first | OpenStreetMap |
| `plan_trip` | prompt | A ready-made request telling Claude which tools to call, in what order, to plan a trip | - |

## How it works

```
Claude --MCP--> server.py
                  1. geocode():  place name -> coordinates
                                 Open-Meteo Geocoding, then Nominatim as a backup
                  2. ask a free API for data around those coordinates
                                 Open-Meteo (weather) or OpenStreetMap via Overpass (places)
                  3. return a dict that Claude reads and explains to the user
```

Claude never sees the code inside the functions. It only sees each tool's name, docstring and type hints (the `Args:` section of the docstring explains each parameter), and decides from that text when to call it.

The same file runs in two ways:

| | On your computer | In the cloud |
|---|---|---|
| How Claude reaches it | stdio: Claude Code or Claude Desktop starts `server.py` itself | HTTP: Claude connects to a URL |
| Works from | That computer only | Any device signed in to Claude |
| How the server knows | `PORT` is not set | The cloud host sets `PORT` |

## Run it on your computer

Requires Python 3.10 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

**Claude Code**:

```bash
claude mcp add travel-mcp -- /path/to/.venv/bin/python /path/to/server.py
```

**Claude Desktop**: Settings > Developer > Edit Config, then add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "travel-mcp": {
      "command": "C:\\path\\to\\Travel-MCP\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\Travel-MCP\\server.py"]
    }
  }
}
```

After changing the code, restart the MCP server (in Claude Code use `/mcp` and reconnect) so Claude sees the new tool definitions.

## Run it in the cloud

Any host that runs Python and sets a `PORT` variable works. On [Render](https://render.com) (free plan):

1. New > Web Service, and connect this GitHub repository.
2. Build command: `pip install -r requirements.txt`
3. Start command: `python server.py`
4. Instance type: Free. Deploy, and note the address Render gives you.

Then in claude.ai: Settings > Connectors > Add custom connector, and enter the address followed by `/mcp`, for example `https://your-service.onrender.com/mcp`. The tools are then available wherever you are signed in to Claude.

Two things to know:

- On the free plan the server sleeps when nobody uses it, so the first request afterwards takes up to a minute.
- There is no login. Anyone who knows the address can call the tools. They only read public data, but keep the address to yourself.

To try HTTP mode on your own computer first:

```bash
$env:PORT = "8000"; python server.py      # PowerShell
# PORT=8000 python server.py              # macOS / Linux
```

and point the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) (`npx @modelcontextprotocol/inspector`) at `http://localhost:8000/mcp`.

## Limitations

- **No prices, availability or reviews.** There is no free hotel-price API: Agoda and Booking.com require affiliate approval, and Amadeus shut down its Self-Service API in July 2026. Instead the server returns search links so the user can check real prices, and the server instructions tell Claude never to guess prices.
- **OpenStreetMap is volunteer-edited.** Data may be incomplete or out of date, and many accommodation listings have no star rating.
- **The public Overpass servers are slow and unreliable.** In real tests a query took 10-40 seconds on a good day. On a bad day it took 85 seconds, and four attempts in a row failed before one succeeded. When both servers fail the tool returns a clear error, and asking again usually works. This is the biggest weakness of the project.
- **"Popular" is a guess.** It is based on how many languages a place has names in and whether it has a Wikipedia article, so the top result is not always the one tourists would name first.
- **Booking.com links are undocumented.** They use the commonly used URL format; if the site changes it, dates may not carry over. Agoda is left out because its search URLs need internal city IDs that are not public.
- **Ambiguous or abbreviated place names can resolve to the wrong place** (for example the Thai abbreviation "กทม" for Bangkok). Every result includes `location.full_name` so Claude can check.

## Design choices

- **Simple on purpose.** Plain synchronous functions that return dicts, one file, no caching.
- **Everything the model reads is English**, and results always carry an English `name` plus the `local_name` in the local script. The server instructions tell Claude to answer in the user's language.
- **Check the request before using the network.** Bad dates and out-of-range values fail instantly with a message that says how to fix them.
- **Two geocoders, because each misses something** (found by testing): Open-Meteo is fast but only knows cities by their English name. Nominatim is the backup: it finds neighbourhoods and landmarks such as Nimmanhaemin or Khao San Road, and names typed in Thai.
- **Try a second Overpass server** when the first fails or returns an incomplete answer, because the free public servers are often busy.
- **The cloud server keeps no memory between requests** (stateless HTTP), so it can sleep, restart or run as several copies without breaking a conversation.

## Possible improvements

Things deliberately left out to keep the code small, and what would be needed to add them:

- Real prices: an affiliate or paid hotel API (needs an API key), then `check_in` / `check_out` would feed it directly.
- Async requests and caching: worthwhile if many users shared one server.
- Parallel "hedged" requests to several Overpass servers for faster answers when the first is slow.
- A login for the cloud server, before sharing its address with other people.
- Self-hosted Overpass/Nominatim for heavy or commercial use (the public servers are not meant for that).

## Tests

```bash
pip install pytest
pytest
```

24 tests, no network: a fake replaces `httpx.get`/`httpx.post` and returns canned answers, so tests are fast and put no load on the free services. They cover geocoding (including Thai names and both fallbacks), weather, accommodation filtering and sorting, date and size validation, Overpass failover, and what Claude actually sees through the MCP protocol. GitHub Actions runs lint and tests on Python 3.10 and 3.13 on every push.

## Data sources and licences

- Weather and geocoding: [Open-Meteo](https://open-meteo.com) (CC BY 4.0; the free API is for non-commercial use)
- Accommodation and places: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL), via the [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) and [Nominatim](https://operations.osmfoundation.org/policies/nominatim/)
- Results carry a `source` field naming where the data came from, as the licences require.
- This project is not affiliated with Google or Booking.com. The links are ordinary public search pages.

For heavy or commercial use, run your own Overpass/Nominatim or use a paid service instead of the public servers.
