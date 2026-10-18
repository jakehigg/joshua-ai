---
destination: family
---
The house is open for fresh air, and the outside conditions or the forecast
have turned bad. Home Assistant says it is time to close up. The event data
below holds every number. Use only those numbers. Never guess or invent a
value, and never fill in a field that is null.

If `house_open` is false, reply with exactly `[IGNORE]`. In all other cases,
send the message, even when many fields are null.

Tell everyone at home to close the windows and doors, and say why. Take the
reason from `reasons`, in plain words, with the number that goes with it:

- rain: rain in about `forecast.next3h.rain_in_min` minutes, or a
  `forecast.next3h.rain_prob` percent chance.
- cold: outside is `outside.effective_f`°F or falling toward it, below the
  comfort floor of `thresholds.comfort_low`°F.
- heat: outside is above `thresholds.comfort_high`°F.
- humid: the dew point is `outside.dew_point_f`°F, at or above
  `thresholds.dew_point_max`°F.
- wind: wind is `outside.wind_mph` mph, above `thresholds.wind_max` mph.

Then add that the HVAC comes back on 5 minutes after the doors are closed.
While `thermostat.upstairs_in_ha` is false, ask someone to set the upstairs
Ecobee back by hand.

Write 1 to 3 short lines in your normal voice. No headers, no lists, no JSON.
Temperatures in °F, rounded to whole degrees. If someone asks, you can check a
sensor or change a thermostat, but this message does not need that.
