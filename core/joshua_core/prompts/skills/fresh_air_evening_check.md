---
destination: family
---
The house's good evening mode has started. Decide whether the house should be
open or closed tonight. The event data below holds every number. Use only
those numbers. Never guess or invent a value, and never fill in a field that
is null.

The night is fine when all of these are true:

- `forecast.overnight.min_f` is at or above `thresholds.comfort_low`.
- `forecast.overnight.rain_prob` is below `thresholds.rain_prob_max`.
- `forecast.overnight.max_dew_point_f` is below `thresholds.dew_point_max`.
- `forecast.overnight.max_wind_mph` is below `thresholds.wind_max`.

If a value that you need is null, the night is not fine.

Then pick one:

- `house_open` is true and the night is fine: say it is fine to leave the
  house open tonight, with the overnight low.
- `house_open` is true and the night is not fine: say to close up before bed,
  and give the reason with its number (low of X°F, Y percent chance of rain,
  dew point up to Z°F, wind up to W mph). If the values are null, say the
  overnight forecast is not available. While `thermostat.upstairs_in_ha` is
  false, ask someone to set the upstairs Ecobee back by hand.
- `house_open` is false, the night is fine, and `case` is not "none": suggest
  opening up for the night, with the overnight low. While
  `thermostat.upstairs_in_ha` is false, ask someone to turn off the upstairs
  Ecobee or widen its band.
- Anything else: reply with exactly `[IGNORE]`.

When the house is open, `inside.rate_f_per_h` (°F per hour, negative =
falling) and `inside.minutes_to_setpoint` say how soon the downstairs reaches
the heating or cooling setpoint; if that is before bedtime, you may say so in
the "close up" advice.

Write 1 to 3 short lines in your normal voice. No headers, no lists, no JSON.
Temperatures in °F, rounded to whole degrees. If someone asks, you can check a
sensor or change a thermostat, but this message does not need that.
