I tried to hack EV fast chargers to stress the power grid in simulation. Setting every charger to full power did nothing. Flipping them on and off did.

The setup: three fast-charging plazas (18 × 150 kW) on the CIGRE 20-kV benchmark feeder, with charging demand drawn from 35,000 real sessions. An attacker who controls the charging management system tries to stress the feeder, and a guard at each site tries to stop them. Power flow from pandapower.

What I found:
- "Everything to 150 kW" changed nothing. Plugged-in cars only take what their batteries accept, and the site caps never bound. The attacker's leverage is set by how many cars are plugged in, not by charger nameplate.
- Toggling chargers on and off every 2 minutes swung load by 1,177 kW in one minute, three times anything seen in normal operation. The guard caught it in 4 minutes and switched the sites to local control.
- Fake battery readings and under-reported power were caught in 0-3 minutes, using battery physics and the site's own revenue meter.
- Zero false alarms in 7 normal days.

The lesson I didn't expect: my first mitigation made things worse. It rate-limited the attacker's commands instead of ignoring them, and the deferred charging pushed a feeder line from 92% to 101%. The fix was simple: when under attack, stop listening.

Repo: https://github.com/faysalchowdhoury/evcs-grid-surge-guard

Utility folks: are load-altering attacks on EV charging part of your planning studies yet?

#EVCharging #GridResilience #Cybersecurity #PowerSystems #Electrification #Transportation #Python
