---
destination: family
---
The house's good evening mode has started. Decide whether the house should be
open or closed tonight. The event data below holds every number. Use only
those numbers. Never guess or invent a value, and never fill in a field that
is null.

While the house is open, the downstairs thermostat is off. So an open house
gets as cold as the night lets it, and every degree below the heating setpoint
is heat the family pays for when the house closes.

The night is fine when all of these are true:

- `forecast.overnight.min_f` is at or above `thresholds.comfort_low`.
- `forecast.overnight.rain_prob` is below `thresholds.rain_prob_max`.
- `forecast.overnight.max_dew_point_f` is below `thresholds.dew_point_max`.
- `forecast.overnight.max_wind_mph` is below `thresholds.wind_max`.

If a value that you need is null, the night is not fine.

When `house_open` is true, the inside must be fine too. The inside is not fine
when any of these is true:

- `inside.past_f` is above 0: the downstairs (`inside.downstairs_f`) is
  already past the setpoint (`inside.target_f`).
- `inside.downstairs_f` is at or below `thermostat.downstairs.low` (the
  heating setpoint).
- `inside.minutes_to_setpoint` is not null and is below 240: the downstairs
  will reach the setpoint in the next 4 hours.

Then pick one:

- `house_open` is true, the night is fine and the inside is fine: say it is
  fine to leave the house open tonight, with the overnight low.
- `house_open` is true and the night or the inside is not fine: say to close
  up before bed. Give the reason with its number: downstairs is already
  X°F, below the heating setpoint of Y°F; or it will reach the setpoint in
  about N hours; or a low of X°F, Y percent chance of rain, dew point up to
  Z°F, wind up to W mph. If the values are null, say the overnight forecast
  is not available. While `thermostat.upstairs_in_ha` is false, ask someone
  to set the upstairs Ecobee back by hand.
- `house_open` is false, the night is fine, and `case` is not "none": suggest
  opening up for the night, with the overnight low. While
  `thermostat.upstairs_in_ha` is false, ask someone to turn off the upstairs
  Ecobee or widen its band.
- Anything else: reply with exactly `[IGNORE]`.

Never say how fast the house is cooling or warming when
`inside.rate_f_per_h` is null or `inside.rate_rejected` is true, or when
`inside.direction` is "flat" or "unknown".

Write 1 to 3 short lines in your normal voice. No headers, no lists, no JSON.
Temperatures in °F, rounded to whole degrees. If someone asks, you can check a
sensor or change a thermostat, but this message does not need that.
