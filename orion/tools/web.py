"""Internet tools: web search, news, web pages and the weather, free and without API keys.

Only the search words, the page address or the place name leave this PC. A search asks several engines in one round
(``web.search_backends``) on the tool's worker thread, each thread keeping its own search session; the same query
within ``web.cache_s`` is answered from memory. A page is read up to ``web.page_max_bytes`` under one deadline and its
main text is extracted off the event loop. The weather comes from Open-Meteo through ``WeatherService``, which the
HUD's widget in Orion's own process shares: places and forecasts are kept in data/cache.db, so both processes ask
the internet once. ``ddgs`` and ``trafilatura`` load with the first search or page (the tools process warms them up).
"""

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, TypedDict, cast

import httpx
from pydantic import Field

from orion.base import JSON, TRUNCATED, TTLCache, clip
from orion.tools import GROUPS, Group, ctx, publish

if TYPE_CHECKING:
    from orion.config import Config

log = logging.getLogger("orion.tools.web")
group = Group(GROUPS["web"])

CACHE_ENTRIES: Final = 64  # searches and pages kept in memory
GEOCODE_CACHE_DAYS: Final = 30  # a place found is kept this long
FETCH_DAYS: Final = 7  # a forecast is always fetched for this many days, so the widget and the tool share it
CACHE_BUSY_S: Final = 2.0  # the cache database waits this long for the other process
WMO: Final = {  # Open-Meteo's weather codes
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "severe thunderstorm with hail",
}


class Place(TypedDict):
    """A place Open-Meteo found."""

    name: str
    country: str
    latitude: float
    longitude: float


class WeatherDay(TypedDict):
    """One day of a forecast."""

    date: str
    condition: str
    code: int
    max: int
    min: int
    rain_chance: int
    sunrise: str
    sunset: str


class Weather(TypedDict):
    """The weather now and the days ahead, as the HUD's widget shows it."""

    place: str
    temp: int
    feels_like: int
    humidity: int
    wind: int
    condition: str
    code: int
    days: list[WeatherDay]
    lat: float  # where it is, for the widget's globe
    lon: float


# ---------------------------------------------------------------------------------------------------- plumbing
class _Shared:
    """This process's HTTP client, search sessions and in-memory caches, made on first use."""

    def __init__(self) -> None:
        """Start with nothing made."""
        self.lock = threading.Lock()
        self.client: httpx.AsyncClient | None = None
        self.local = threading.local()
        self.searches: TTLCache[tuple[str, int], str] = TTLCache(CACHE_ENTRIES)
        self.news: TTLCache[tuple[str, int], tuple[str, JSON]] = TTLCache(CACHE_ENTRIES)  # the text, the HUD's items
        self.pages: TTLCache[str, str] = TTLCache(CACHE_ENTRIES)
        self.weather: WeatherService | None = None


_shared = _Shared()


def http() -> httpx.AsyncClient:
    """This process's HTTP client (one connection pool; requests set their own User-Agent and limits).

    Returns:
        The client.
    """
    with _shared.lock:
        if _shared.client is None:
            _shared.client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, connect=10))
        return _shared.client


async def aclose() -> None:
    """Close this process's HTTP client and the weather cache."""
    client, _shared.client = _shared.client, None
    if client is not None:
        await client.aclose()
    if _shared.weather is not None:
        _shared.weather.cache.close()


def _ddgs() -> Any:
    """This worker thread's search session (anyio's worker threads are pooled, so sessions are reused), made again
    when ``web.search_timeout_s`` changed."""
    timeout = int(ctx.config.web.search_timeout_s)
    made = getattr(_shared.local, "ddgs", None)
    if made is None or made[0] != timeout:
        from ddgs import DDGS

        made = _shared.local.ddgs = (timeout, DDGS(timeout=timeout))
    return made[1]


def _search(kind: str, query: str, count: int, backend: str) -> list[dict[str, Any]]:
    """Search results, asking the "auto" backends once more when the named ones found nothing."""
    from ddgs.exceptions import DDGSException

    session = _ddgs()
    find = session.text if kind == "text" else session.news
    for engines in dict.fromkeys((backend, "auto")):
        try:
            if results := find(query, max_results=count, backend=engines):
                return results
        except DDGSException as e:
            log.debug("%s search (%s) found nothing: %s", kind, engines, e)
    return []


@group.tool(read_only=True, open_world=True, title="Search the web",
            examples=("Search for the Raspberry Pi 5 release date", "Who won the race yesterday?"))
def web_search(
    query: Annotated[str, Field(description="The search query.")],
    max_results: Annotated[int, Field(ge=0, le=10, description="Number of results (0: the usual number).")] = 0,
) -> str:
    """Search the internet for current information, facts, prices, people, events or anything you don't know. Not for
    what a more specific tool does (the time, the weather, the news, the PC)."""
    cfg = ctx.config.web
    count = max_results or cfg.search_results
    key = (" ".join(query.casefold().split()), count)
    if (cached := _shared.searches.get(key)) is not None:
        return cached
    results = _search("text", query, count, ",".join(cfg.search_backends))
    if not results:
        return "No results."
    text = "\n\n".join(f"[{i}] {r.get('title', '')}\n{r.get('href', '')}\n"
                       f"{clip(str(r.get('body', '')), cfg.snippet_chars)}" for i, r in enumerate(results, 1))
    _shared.searches.put(key, text, cfg.cache_s)
    return text


@group.tool(read_only=True, open_world=True, title="Get the news headlines",
            examples=("Any Formula 1 news?", "What is in the news today?"))
def get_news(topic: Annotated[str, Field(description='Optional topic, e.g. "technology" or "Formula 1". Empty for the '
                                                     'top world news.')] = "") -> str:
    """Get the latest news headlines, optionally about a topic."""
    cfg = ctx.config.web
    query = " ".join(topic.split()) or cfg.news_default_query
    key = (query.casefold(), cfg.news_results)
    if (found := _shared.news.get(key)) is None:
        results = _search("news", query, cfg.news_results, ",".join(cfg.news_backends))
        if not results:
            return "No news found."
        text = "\n\n".join(f"- {r.get('title', '')} ({r.get('source', '')}, {str(r.get('date', ''))[:10]})\n  "
                           f"{clip(str(r.get('body', '')), cfg.snippet_chars)}" for r in results)
        items: JSON = [{field: str(r.get(field) or "") for field in ("title", "source", "date", "url")}
                       for r in results]
        found = (text, items)
        _shared.news.put(key, found, cfg.cache_s)
    if not topic.strip():  # the top headlines: the HUD's NEWS panel shows them too, from memory as well
        publish("news", items=found[1])
    return found[0]


def _extract(page: bytes) -> str:
    """A page's main text (trafilatura; run in a worker thread)."""
    import trafilatura

    return trafilatura.extract(page, fast=True, include_comments=False, include_tables=True) or ""


@group.tool(read_only=True, open_world=True, title="Read a web page",
            examples=("Read that article", "Summarise this page for me"))
async def read_webpage(url: Annotated[str, Field(description="The full address, starting with http:// or https://")]
                       ) -> str:
    """Fetch a web page and return its main text, e.g. to read an article found with web_search."""
    cfg = ctx.config.web
    address = url.strip()
    if not re.match(r"https?://", address):
        return "Give the full address, starting with http:// or https://."
    if (cached := _shared.pages.get(address)) is not None:
        return cached
    page = bytearray()
    try:
        async with asyncio.timeout(cfg.page_timeout_s):
            async with http().stream("GET", address, headers={"User-Agent": cfg.user_agent}) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    page += chunk
                    if len(page) >= cfg.page_max_bytes:
                        break
    except TimeoutError:
        return f"The page didn't load within {cfg.page_timeout_s:g} seconds."
    except httpx.HTTPStatusError as e:
        return f"The page answered with error {e.response.status_code}."
    except httpx.HTTPError as e:
        return f"The page couldn't be read: {e}"
    text = await asyncio.to_thread(_extract, bytes(page[:cfg.page_max_bytes]))
    if not text:
        return "Could not extract readable text from that page."
    shown = text[:cfg.page_chars] + (TRUNCATED if len(text) > cfg.page_chars else "")
    _shared.pages.put(address, shown, cfg.cache_s)
    return shown


# ---------------------------------------------------------------------------------------------------- weather
class DiskCache:
    """A small JSON cache in data/cache.db, shared by both processes (write-ahead logging), with expiry."""

    def __init__(self, path: Path) -> None:
        """Remember the file; it opens (and drops what expired) on first use.

        Args:
            path: the cache database.
        """
        self.path = path
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

    def get(self, key: str) -> JSON | None:
        """The value of a key that hasn't expired.

        Args:
            key: the key.

        Returns:
            The value, or None when it is missing, expired or unreadable.
        """
        with self._lock:
            row = self._open().execute("SELECT value FROM cache WHERE key = ? AND expires_at > ?",
                                       (key, time.time())).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def put(self, key: str, value: JSON, ttl_s: float) -> None:
        """Store a value for ``ttl_s`` seconds.

        Args:
            key: the key.
            value: any JSON value.
            ttl_s: its lifetime.
        """
        with self._lock:
            db = self._open()
            db.execute("INSERT INTO cache (key, value, expires_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                       "value = excluded.value, expires_at = excluded.expires_at",
                       (key, json.dumps(value, ensure_ascii=False), time.time() + ttl_s))
            db.commit()

    def close(self) -> None:
        """Close the database (it opens again when next used)."""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _open(self) -> sqlite3.Connection:
        """The connection, opened on first use (under the lock): table made, expired rows dropped."""
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=CACHE_BUSY_S, check_same_thread=False)
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                       "expires_at REAL NOT NULL) WITHOUT ROWID")
            db.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
            db.commit()
            self._db = db
        return self._db


class WeatherService:
    """Places and forecasts from Open-Meteo, through the shared cache (get_weather and the HUD's widget)."""

    def __init__(self, cache: DiskCache, config: Callable[[], "Config"]) -> None:
        """Hold the cache and a way to read the live settings.

        Args:
            cache: the shared cache.
            config: returns the live settings (``weather.*``, ``web.user_agent``).
        """
        self.cache = cache
        self._config = config

    async def geocode(self, place: str) -> Place | None:
        """Where a place is (Open-Meteo's best match; "Paris, France" picks the French one), kept a month.

        Args:
            place: a place name, optionally with its country or region after a comma.

        Returns:
            The place, or None when Open-Meteo knows none by that name.

        Raises:
            httpx.HTTPError: Open-Meteo couldn't be reached.
        """
        key = "geo:" + " ".join(place.casefold().split())
        if isinstance(cached := await asyncio.to_thread(self.cache.get, key), dict):
            return cast(Place, cached)
        cfg = self._config()
        name, _, qualifier = place.partition(",")
        response = await http().get(cfg.weather.geocode_url, timeout=cfg.weather.timeout_s,
                                    params={"name": name.strip(), "count": 10, "language": "en"},
                                    headers={"User-Agent": cfg.web.user_agent})
        response.raise_for_status()
        results = response.json().get("results") or []
        wanted = qualifier.strip().casefold()
        chosen = next((r for r in results if wanted and wanted in (str(r.get("country", "")).casefold(),
                                                                   str(r.get("admin1", "")).casefold(),
                                                                   str(r.get("country_code", "")).casefold())),
                      results[0] if results else None)
        if chosen is None:
            return None
        found = Place(name=str(chosen.get("name", place)), country=str(chosen.get("country", "")),
                      latitude=float(chosen["latitude"]), longitude=float(chosen["longitude"]))
        await asyncio.to_thread(self.cache.put, key, dict(found), GEOCODE_CACHE_DAYS * 86400)
        return found

    async def forecast(self, place: str, days: int) -> Weather | None:
        """The weather now and for the coming days (one fetch of FETCH_DAYS days, shared for a while).

        Args:
            place: a place name.
            days: how many days, today included (1-7).

        Returns:
            The weather, or None when the place is unknown.

        Raises:
            httpx.HTTPError: Open-Meteo couldn't be reached.
        """
        found = await self.geocode(place)
        if found is None:
            return None
        cfg = self._config()
        key = f"wx:{found['latitude']:.2f},{found['longitude']:.2f}"
        weather = await asyncio.to_thread(self.cache.get, key)
        if not isinstance(weather, dict):
            weather = await self._fetch(found, cfg)
            await asyncio.to_thread(self.cache.put, key, weather, cfg.weather.forecast_cache_s)
        shown = dict(weather)
        shown["days"] = list(weather["days"])[:max(1, min(int(days), FETCH_DAYS))]
        return cast(Weather, shown)

    @staticmethod
    async def _fetch(found: Place, cfg: "Config") -> dict[str, JSON]:
        """Fetch and shape a forecast (metric units: degrees Celsius, km/h, mm)."""
        response = await http().get(cfg.weather.forecast_url, timeout=cfg.weather.timeout_s, headers={
            "User-Agent": cfg.web.user_agent}, params={
            "latitude": found["latitude"], "longitude": found["longitude"], "timezone": "auto",
            "forecast_days": FETCH_DAYS,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"})
        response.raise_for_status()
        data = response.json()
        now, daily = data["current"], data["daily"]
        return {
            "place": ", ".join(part for part in (found["name"], found["country"]) if part),
            "temp": round(now["temperature_2m"]), "feels_like": round(now["apparent_temperature"]),
            "humidity": round(now["relative_humidity_2m"]), "wind": round(now["wind_speed_10m"]),
            "condition": WMO.get(now["weather_code"], "unknown"), "code": int(now["weather_code"]),
            "lat": round(found["latitude"], 2), "lon": round(found["longitude"], 2),
            "days": [{"date": daily["time"][i], "condition": WMO.get(daily["weather_code"][i], "unknown"),
                      "code": int(daily["weather_code"][i]), "max": round(daily["temperature_2m_max"][i]),
                      "min": round(daily["temperature_2m_min"][i]),
                      "rain_chance": int(daily["precipitation_probability_max"][i] or 0),
                      "sunrise": str(daily["sunrise"][i])[-5:], "sunset": str(daily["sunset"][i])[-5:]}
                     for i in range(len(daily["time"]))],
        }


def _weather() -> WeatherService:
    """This process's weather service (the tools process's; Orion's own makes its own for the widget)."""
    with _shared.lock:
        if _shared.weather is None:
            _shared.weather = WeatherService(DiskCache(ctx.config.data_path / "cache.db"), lambda: ctx.config)
        return _shared.weather


def _places(location: str, is_home: bool) -> tuple[str, str]:
    """(the place to report, the place the HUD's widget shows), remembering a new home or the last place asked."""
    cfg, store = ctx.config, ctx.memory.store
    home = cfg.user.city or store.get_setting("home_city")
    if is_home and location:
        store.set_setting("home_city", location)
        home = cfg.user.city or location
    elif location and not home:
        store.set_setting("last_weather_city", location)
    return location or home, home or store.get_setting("last_weather_city")


def _day_name(index: int, iso: str) -> str:
    """"Today", "Tomorrow", then the weekday."""
    if index < 2:
        return ("Today", "Tomorrow")[index]
    with contextlib.suppress(ValueError):
        return f"{date.fromisoformat(iso):%A}"
    return iso


@group.tool(open_world=True, taints=False, title="Weather and forecast",
            examples=("What's the weather tomorrow?", "Will it rain in Paris on Friday?"))
async def get_weather(
    location: Annotated[str, Field(description='City name, optionally with the country ("Paris, France"). Empty for '
                                               "the user's home city.")] = "",
    days: Annotated[int, Field(ge=0, le=FETCH_DAYS, description="Forecast days including today (0: the usual "
                                                               "number).")] = 0,
    is_home: Annotated[bool, Field(description="True if the user just said this is where they live, so it becomes "
                                               "their home city.")] = False,
) -> str:
    """Current weather and forecast."""
    place, widget = await asyncio.to_thread(_places, " ".join(location.split()), is_home)
    if not place:
        return "The user's home city is unknown. Ask which city they are in, then remember it and try again."
    shown = days or ctx.config.weather.default_days
    try:
        weather = await _weather().forecast(place, max(shown, ctx.config.weather.widget_days))
    except httpx.HTTPError as e:
        return f"The weather service couldn't be reached: {e}"
    if weather is None:
        return f"Could not find a place called '{place}'."
    if place.casefold() == widget.casefold():
        publish("weather", weather=cast(JSON, weather))
    now = (f"{weather['place']}: {weather['condition']}, {weather['temp']}°C (feels like {weather['feels_like']}°C), "
           f"humidity {weather['humidity']}%, wind {weather['wind']} km/h.")
    lines = [now]
    for i, d in enumerate(weather["days"][:max(shown, 2)]):
        lines.append(f"{_day_name(i, d['date'])} ({d['date']}): {d['condition']}, {d['min']} to {d['max']}°C, rain "
                     f"chance {d['rain_chance']}%, sunrise {d['sunrise']}, sunset {d['sunset']}.")
    return "\n".join(lines)
