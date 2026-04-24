"""Ex5 — reference solution for tools.py.

This is the educator's reference. Copy ONLY INTO starter/ via
`make educator-apply-solution`. Never commit. The .gitignore at the
repo root excludes this whole solution/ directory.

Pedagogical notes (why each tool is implemented this way):

- venue_search, get_weather, calculate_cost are marked parallel_safe.
  They read fixtures, don't mutate anything. The executor can batch them
  in one turn — important for Decision 5 (parallelism) from the course.

- generate_flyer writes a file, so parallel_safe=False. If you miss
  this, the grader deducts points and the student gets interleaved
  writes in race scenarios.

- Every tool calls record_tool_call() before returning. The integrity
  check compares later outputs (the flyer) against this log to detect
  fabrication.

- Tools return ToolResult, not raw dicts. ToolResult lets the executor
  see success/failure distinctly and surface the summary to the LLM.

- Bad inputs raise ToolError with SA_TOOL_* error_code. Never RuntimeError.
  The executor catches ToolError and feeds it to the LLM as a tool call
  result; RuntimeError would crash the whole session.
"""

from __future__ import annotations

import json
from pathlib import Path

from sovereign_agent.errors import ToolError
from sovereign_agent.session.directory import Session
from sovereign_agent.tools.registry import ToolRegistry, ToolResult, _RegisteredTool

from starter.edinburgh_research.integrity import _TOOL_CALL_LOG, record_tool_call

_SAMPLE_DATA = (
    Path(__file__).parent.parent.parent / "starter" / "edinburgh_research" / "sample_data"
)


# ---------------------------------------------------------------------------
# 1 — venue_search
# ---------------------------------------------------------------------------
def venue_search(near: str, party_size: int, budget_max_gbp: int = 1000) -> ToolResult:
    venues_path = _SAMPLE_DATA / "venues.json"
    if not venues_path.exists():
        raise ToolError("SA_TOOL_DEPENDENCY_MISSING", f"venues.json not found at {venues_path}")

    try:
        venues = json.loads(venues_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ToolError("SA_TOOL_DEPENDENCY_MISSING", f"venues.json malformed: {e}") from e

    near_l = near.lower().strip()
    results = [
        v
        for v in venues
        if v.get("open_now")
        and near_l in v.get("area", "").lower()
        and v.get("seats_available_evening", 0) >= party_size
        and (v.get("hire_fee_gbp", 0) + v.get("min_spend_gbp", 0)) <= budget_max_gbp
    ]

    output = {
        "near": near,
        "party_size": party_size,
        "budget_max_gbp": budget_max_gbp,
        "results": results,
        "count": len(results),
    }
    record_tool_call(
        "venue_search",
        {"near": near, "party_size": party_size, "budget_max_gbp": budget_max_gbp},
        output,
    )

    # Spiral detection — after 3+ calls, tell the LLM to stop.
    # Small models like Qwen3-32B sometimes loop on venue_search when the
    # first call returns 0 results. Returning an error with clear guidance
    # interrupts the loop.
    venue_search_count = sum(1 for rec in _TOOL_CALL_LOG if rec.tool_name == "venue_search")
    if venue_search_count >= 3:
        return ToolResult(
            success=False,
            output={
                "error": "too_many_venue_searches",
                "count": venue_search_count,
                "hint": (
                    "You have called venue_search 3+ times. Stop searching and work "
                    "with the results you have. If your first search returned 0 "
                    "matches, call complete_task with an error — do not try "
                    "different args."
                ),
            },
            summary=(
                f"venue_search: STOP — called {venue_search_count} times; "
                "use previous results and progress to get_weather / generate_flyer"
            ),
        )

    return ToolResult(
        success=True,
        output=output,
        summary=f"venue_search({near!r}, party={party_size}): {len(results)} result(s)",
    )


# ---------------------------------------------------------------------------
# 2 — get_weather
# ---------------------------------------------------------------------------
def get_weather(city: str, date: str) -> ToolResult:
    weather_path = _SAMPLE_DATA / "weather.json"
    if not weather_path.exists():
        raise ToolError("SA_TOOL_DEPENDENCY_MISSING", f"weather.json not found at {weather_path}")

    data = json.loads(weather_path.read_text(encoding="utf-8"))
    city_key = city.lower().strip()

    if city_key not in data:
        return ToolResult(
            success=False,
            output={"error": f"no weather data for city {city!r}"},
            summary=f"get_weather({city!r}, {date}): city not found",
        )

    # Fixture shape: {"edinburgh": {"2026-04-25": {...}, ...}}
    city_forecasts = data[city_key]
    match = city_forecasts.get(date) if isinstance(city_forecasts, dict) else None
    if match is None:
        return ToolResult(
            success=False,
            output={
                "error": f"no forecast for {city} on {date}",
                "available_dates": sorted(city_forecasts.keys())
                if isinstance(city_forecasts, dict)
                else [],
            },
            summary=f"get_weather({city!r}, {date}): date not in fixture",
        )

    output = {"city": city, "date": date, **match}
    record_tool_call("get_weather", {"city": city, "date": date}, output)
    return ToolResult(
        success=True,
        output=output,
        summary=f"get_weather({city!r}, {date}): {match['condition']}, {match['temperature_c']}C",
    )


# ---------------------------------------------------------------------------
# 3 — calculate_cost
# ---------------------------------------------------------------------------
def calculate_cost(
    venue_id: str,
    party_size: int,
    duration_hours: int,
    catering_tier: str = "bar_snacks",
) -> ToolResult:
    catering_path = _SAMPLE_DATA / "catering.json"
    venues_path = _SAMPLE_DATA / "venues.json"

    catering = json.loads(catering_path.read_text(encoding="utf-8"))
    venues = json.loads(venues_path.read_text(encoding="utf-8"))

    if catering_tier not in catering["base_rates_gbp_per_head"]:
        return ToolResult(
            success=False,
            output={"error": f"unknown catering_tier: {catering_tier}"},
            summary=f"calculate_cost: bad tier {catering_tier!r}",
        )

    venue = next((v for v in venues if v.get("id") == venue_id), None)
    if venue is None:
        return ToolResult(
            success=False,
            output={"error": f"unknown venue_id: {venue_id}"},
            summary=f"calculate_cost: venue {venue_id!r} not found",
        )

    base_per_head = catering["base_rates_gbp_per_head"][catering_tier]
    modifier = catering["venue_modifiers"].get(venue_id, 1.0)
    hours = max(1, duration_hours)
    subtotal = int(base_per_head * modifier * party_size * hours)
    service = int(subtotal * catering["service_charge_percent"] / 100)
    venue_floor = venue.get("hire_fee_gbp", 0) + venue.get("min_spend_gbp", 0)
    total = subtotal + service + venue_floor

    # Deposit rules
    if total < 300:
        deposit = 0
    elif total < 1000:
        deposit = int(total * 0.2)
    else:
        deposit = int(total * 0.3)

    output = {
        "venue_id": venue_id,
        "party_size": party_size,
        "duration_hours": hours,
        "catering_tier": catering_tier,
        "subtotal_gbp": subtotal,
        "service_gbp": service,
        "venue_floor_gbp": venue_floor,
        "total_gbp": total,
        "deposit_required_gbp": deposit,
    }
    record_tool_call(
        "calculate_cost",
        {
            "venue_id": venue_id,
            "party_size": party_size,
            "duration_hours": duration_hours,
            "catering_tier": catering_tier,
        },
        output,
    )
    return ToolResult(
        success=True,
        output=output,
        summary=f"calculate_cost({venue_id}, party={party_size}): total £{total}, deposit £{deposit}",
    )


# ---------------------------------------------------------------------------
# 4 — generate_flyer
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 4 — generate_flyer (HTML output)
# ---------------------------------------------------------------------------
def generate_flyer(session: Session, event_details: dict) -> ToolResult:
    """Write a self-contained HTML flyer to workspace/flyer.html.

    HTML (not markdown) so students can open it in a browser and see
    the actual poster their agent produced. The file is self-contained
    (inline CSS) — no external assets. Semantic tags (<article>,
    <section>, <dl>) help screen readers and the integrity check's
    DOM parser.
    """
    required = (
        "venue_name",
        "date",
        "time",
        "party_size",
        "condition",
        "temperature_c",
        "total_gbp",
    )
    missing = [k for k in required if k not in event_details]
    if missing:
        return ToolResult(
            success=False,
            output={"error": f"missing event_details keys: {missing}"},
            summary=f"generate_flyer: missing {missing}",
        )

    # Simple HTML escape for user-controlled text — prevents template
    # injection even though everything here comes from our tool outputs.
    from html import escape

    venue_name = escape(str(event_details["venue_name"]))
    date = escape(str(event_details["date"]))
    time_str = escape(str(event_details["time"]))
    party_size = int(event_details["party_size"])
    condition = escape(str(event_details["condition"]).capitalize())
    temp_c = int(event_details["temperature_c"])
    total_gbp = int(event_details["total_gbp"])
    deposit = int(event_details.get("deposit_required_gbp", 0))
    address = escape(str(event_details.get("venue_address", "")))

    deposit_html = (
        f'<dt>Deposit</dt><dd data-testid="deposit">£{deposit}</dd>'
        if deposit
        else '<dt>Deposit</dt><dd data-testid="deposit">No deposit required</dd>'
    )
    address_html = f'<p class="address" data-testid="address">{address}</p>' if address else ""

    flyer_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{venue_name} — Private Event</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 640px;
    margin: 2rem auto;
    padding: 0 1rem;
    line-height: 1.5;
    color: #222;
  }}
  article {{
    border: 2px solid #444;
    border-radius: 8px;
    padding: 1.5rem 2rem;
    background: #fffef7;
  }}
  h1 {{ margin-top: 0; font-size: 2rem; }}
  h2 {{ font-size: 1.15rem; margin-top: 1.5rem; color: #555; }}
  .address {{ font-style: italic; color: #666; margin-top: -0.5rem; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.3rem 1rem; }}
  dt {{ font-weight: 600; color: #555; }}
  dd {{ margin: 0; }}
  .total {{ font-size: 1.25rem; font-weight: 700; color: #2a5934; }}
</style>
</head>
<body>
<article>
  <h1 data-testid="venue-name">{venue_name}</h1>
  <p class="subtitle">Private Event</p>
  {address_html}

  <h2>When</h2>
  <dl>
    <dt>Date</dt><dd data-testid="date">{date}</dd>
    <dt>Time</dt><dd data-testid="time">{time_str}</dd>
    <dt>Party size</dt><dd data-testid="party-size">{party_size}</dd>
  </dl>

  <h2>Weather forecast</h2>
  <p><span data-testid="condition">{condition}</span>,
     <span data-testid="temperature">{temp_c}°C</span></p>

  <h2>Cost</h2>
  <dl>
    <dt>Total</dt><dd class="total" data-testid="total">£{total_gbp}</dd>
    {deposit_html}
  </dl>
</article>
</body>
</html>
"""

    flyer_path = session.workspace_dir / "flyer.html"
    flyer_path.parent.mkdir(parents=True, exist_ok=True)
    flyer_path.write_text(flyer_html, encoding="utf-8")

    output = {
        "path": "workspace/flyer.html",
        "bytes_written": flyer_path.stat().st_size,
        "venue_name": event_details["venue_name"],
        "total_gbp": total_gbp,
        "deposit_required_gbp": deposit,
    }
    record_tool_call("generate_flyer", {"event_details": event_details}, output)
    return ToolResult(
        success=True,
        output=output,
        summary=f"generate_flyer: wrote workspace/flyer.html ({flyer_path.stat().st_size} bytes)",
    )


# ---------------------------------------------------------------------------
# Registry — same signature as starter scaffold
# ---------------------------------------------------------------------------
def build_tool_registry(session: Session) -> ToolRegistry:
    from sovereign_agent.tools.builtin import make_builtin_registry

    reg = make_builtin_registry(session)

    reg.register(
        _RegisteredTool(
            name="venue_search",
            description="Search Edinburgh venues by area, party size, and max budget.",
            fn=venue_search,
            parameters_schema={
                "type": "object",
                "properties": {
                    "near": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "budget_max_gbp": {"type": "integer", "default": 1000},
                },
                "required": ["near", "party_size"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,
            examples=[
                {
                    "input": {"near": "Haymarket", "party_size": 6, "budget_max_gbp": 800},
                    "output": {"count": 1, "results": [{"id": "haymarket_tap"}]},
                }
            ],
        )
    )

    reg.register(
        _RegisteredTool(
            name="get_weather",
            description="Get scripted weather for a city on a YYYY-MM-DD date.",
            fn=get_weather,
            parameters_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}, "date": {"type": "string"}},
                "required": ["city", "date"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,
            examples=[{"input": {"city": "Edinburgh", "date": "2026-04-25"}, "output": {}}],
        )
    )

    reg.register(
        _RegisteredTool(
            name="calculate_cost",
            description="Compute total cost and deposit for a booking.",
            fn=calculate_cost,
            parameters_schema={
                "type": "object",
                "properties": {
                    "venue_id": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "duration_hours": {"type": "integer"},
                    "catering_tier": {
                        "type": "string",
                        "enum": ["drinks_only", "bar_snacks", "sit_down_meal", "three_course_meal"],
                        "default": "bar_snacks",
                    },
                },
                "required": ["venue_id", "party_size", "duration_hours"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,
            examples=[
                {
                    "input": {"venue_id": "haymarket_tap", "party_size": 6, "duration_hours": 3},
                    "output": {},
                }
            ],
        )
    )

    def _flyer_adapter(event_details: dict) -> ToolResult:
        return generate_flyer(session, event_details)

    reg.register(
        _RegisteredTool(
            name="generate_flyer",
            description="Write an HTML flyer for the event to workspace/flyer.html.",
            fn=_flyer_adapter,
            parameters_schema={
                "type": "object",
                "properties": {"event_details": {"type": "object"}},
                "required": ["event_details"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=False,  # writes a file
            examples=[{"input": {"event_details": {"venue_name": "Haymarket Tap"}}, "output": {}}],
        )
    )

    return reg


__all__ = ["build_tool_registry", "venue_search", "get_weather", "calculate_cost", "generate_flyer"]
