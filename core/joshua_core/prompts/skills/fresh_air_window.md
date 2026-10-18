---
destination: family
---
Home Assistant thinks now is a good time to open the whole house: the HVAC is
heating or cooling, or a compressor is running, and the outside air can do the
same work. The event data below holds every number. Use only those numbers.
Never guess or invent a value, and never fill in a field that is null.

First, check the numbers yourself. Reply with exactly `[IGNORE]` if any of
these is true:

- Most of the payload is null, or `outside.effective_f` is null.
- `case` is "none", or `house_open` is already true.
- The margin is thin: `outside.effective_f` is within 1°F of the edge of the
  thermostat band it must beat (`thermostat.downstairs.low` for heating,
  `thermostat.downstairs.high` for cooling), `outside.dew_point_f` is within
  2°F of `thresholds.dew_point_max`, `forecast.next3h.rain_prob` is within 10
  points of `thresholds.rain_prob_max`, or `outside.wind_mph` is close to
  `thresholds.wind_max`.

If the numbers hold up, tell everyone at home it is a good time to open up.
Say:

1. Why, from `case`: for "cooling", the house wants cooling and it is already
   X°F outside; for "heating", the heat is running and it is already Y°F
   outside. Use `outside.effective_f`.
2. About how long it should hold: `forecast.next3h.hold_hours` hours, or rain
   in about `forecast.next3h.rain_in_min` minutes when that is set.
3. One practical note: after the front door is open 15 minutes, the
   downstairs HVAC turns itself off (see `hvac_auto_off_note`). While
   `thermostat.upstairs_in_ha` is false, ask someone to set the upstairs
   Ecobee by hand: turn it off, or widen its band past
   `thermostat.upstairs_band`.

When `trigger` is "arrival", make it a welcome-home note: they just got home,
it is nice out, open up.

Write 1 to 3 short lines in your normal voice. No headers, no lists, no JSON.
Temperatures in °F, rounded to whole degrees. If someone asks, you can check a
sensor or change a thermostat, but this message does not need that.
