---
destination: family
---
The house is open for fresh air, and the downstairs is drifting toward the
thermostat setpoint. The family opens the house to save energy. If the house
gets colder than the heating setpoint (in the fall) or warmer than the cooling
setpoint (in the spring), the heat or the AC has to buy those degrees back
when the house closes. Home Assistant sends this event at most once per stage
while the house is open. The event data below holds every number. Use only
those numbers. Never guess or invent a value, and never fill in a field that
is null.

Reply with exactly `[IGNORE]` if `house_open` is false, or if `stage` is
missing or is not `approaching`, `hit` or `past`.

`direction` says which setpoint the house is heading for: "heating" means it
is cooling down toward the heating setpoint, "cooling" means it is warming up
toward the cooling setpoint. Call it the heating setpoint or the cooling
setpoint to match. `inside.target_f` is that setpoint, `inside.downstairs_f`
is the downstairs now, and `inside.rate_f_per_h` is how fast it is moving
(°F per hour, negative = falling). Never state a rate, or say how fast the
house is cooling or warming, when `inside.rate_rejected` is true,
`inside.rate_f_per_h` is null, or `direction` is "flat" or "unknown": the
rate is not trusted then (for example right after a restart).

Name what is open, from `open_contacts` ("the breakfast nook window and the
bedroom side window"). If the list is empty, say "the house".

Then say one thing, from `stage`:

- `approaching`: downstairs is `inside.downstairs_f`°F and will reach the
  heating (or cooling) setpoint of `inside.target_f`°F in about
  `inside.minutes_to_setpoint` minutes. Close up soon, unless they want to
  keep enjoying it a little longer.
- `hit`: downstairs is now at the setpoint (`inside.downstairs_f`°F against
  `inside.target_f`°F). If they close up now, the heat (or AC) will not need
  to run yet.
- `past`: downstairs is `inside.past_f`°F past the setpoint
  (`inside.downstairs_f`°F against `inside.target_f`°F). Every extra degree is
  heating (or cooling) they pay for later. Close up now.

Mention the outside or the forecast only when it changes the advice. Example:
the house is cooling toward the heating setpoint, but `forecast.next3h.max_f`
is clearly above `outside.effective_f` and above `inside.downstairs_f`, so the
air will warm the house back up soon: "or wait, it warms to 62 in the next few
hours". Do not invent a time that the data does not give.

Write 1 to 3 short lines in your normal voice. No headers, no lists, no JSON.
Temperatures in °F, rounded to whole degrees, except `inside.past_f`, which
keeps one decimal when it is below 2. If someone asks, you can check a sensor
or change a thermostat, but this message does not need that.
