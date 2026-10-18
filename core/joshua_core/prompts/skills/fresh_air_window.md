---
destination: family
---
Home Assistant checks every 30 minutes whether it is a good time to open the
whole house, and sends this event when the outside air is in a good range for
the downstairs thermostat band and the weather, forecast, humidity and wind all
pass. It waits at least 3 hours between these events, except on arrival home
and at the start of the evening. The event data below holds every number. Use
only those numbers. Never guess or invent a value, and never fill in a field
that is null. Check everything yourself and tell the people at home what to
do.

First, check the numbers yourself. Reply with exactly `[IGNORE]` if any of
these is true:

- Most of the payload is null, or `outside.effective_f` is null.
- `direction_ok` is false, or `house_open` is already true.
- The margin is thin: `outside.effective_f` is within 1°F of the edge of the
  thermostat band it must beat (`thermostat.downstairs.low` for heating,
  `thermostat.downstairs.high` for cooling), `outside.dew_point_f` is within
  2°F of `thresholds.dew_point_max`, `forecast.next3h.rain_prob` is within 10
  points of `thresholds.rain_prob_max`, or `outside.wind_mph` is close to
  `thresholds.wind_max`.

If the numbers hold up, tell everyone at home it is a good time to open up.
Say:

1. Why, from `case`: for "cooling", the house is above its band and it is
   cooler outside at X°F; for "heating", the house is below its band and it is
   already Y°F outside; for "fresh", the house is comfortable and it is a nice
   Z°F outside, so open up for fresh air. Use `outside.effective_f`. When
   `compressors_w.downstairs` or `compressors_w.upstairs` is above 300, say
   that the heat pump or AC is running right now and opening up lets it stop.
   For "heating" and "fresh", how cold outside may be is wind-adjusted:
   `thresholds.heating_margin_effective` is how many °F below
   `thermostat.downstairs.low` still counts, at the current
   `thresholds.wind_mph`. Calm air (about `thresholds.calm_wind_mph` or less)
   cools the house slowly, so you can say that cool air is fine because it is
   calm, or that a breeze will cool the house faster when the wind is up.
2. About how long it should hold: `forecast.next3h.hold_hours` hours, or rain
   in about `forecast.next3h.rain_in_min` minutes when that is set.
3. One practical note: after the front door is open 15 minutes, the
   downstairs HVAC turns itself off (see `hvac_auto_off_note`). While
   `thermostat.upstairs_in_ha` is false, ask someone to set the upstairs
   Ecobee by hand: turn it off, or widen its band past
   `thermostat.upstairs_band`.

`inside.rate_f_per_h` (°F per hour, negative = falling) and
`inside.minutes_to_setpoint` say how fast the downstairs is moving and how
soon it reaches the setpoint on that side; you may use them for a short "it
will stay comfortable for about N hours", but they are not a reason to stay
silent. Never state a rate when `inside.rate_rejected` is true or
`inside.rate_f_per_h` is null.

When `trigger` is "arrival", make it a welcome-home note: they just got home,
it is nice out, open up.

Write 1 to 3 short lines in your normal voice. No headers, no lists, no JSON.
Temperatures in °F, rounded to whole degrees. If someone asks, you can check a
sensor or change a thermostat, but this message does not need that.
