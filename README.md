# Navimow (Private) — Home Assistant integration

Unofficial Home Assistant custom integration for **Segway Navimow** robot mowers
(i-series, e.g. i108). It talks to the same private mobile-app cloud the official
app uses (`navimow-fra.ninebot.com`), so it works without the vendor's public
Open API / developer program.

> **Disclaimer.** This is an **unofficial**, community, interoperability project.
> It is not affiliated with, endorsed by, or supported by Segway, Ninebot, or
> Willand. It uses the private cloud protocol at your own risk. Names, hosts and
> protocol constants are used only for interoperability. There is no warranty:
> mowing commands move a real machine with spinning blades — use responsibly and
> keep people and pets clear of the lawn.

---

## Features

All entities live under a single Home Assistant device (the mower).

| Platform | Entity | Notes |
|---|---|---|
| `lawn_mower` | Mower | Start / Pause / Dock (+ Resume via Start when paused) |
| `sensor` | Battery, Status, Mowing progress, Coverage, Current zone, Session area, Area this week, Next mow, Error, Wi-Fi signal, Blades life, Chassis life, State code | Areas / progress are best-effort (see below) |
| `binary_sensor` | Problem, Online, Docked | |
| `select` | Mow zone | Stores which zone the `lawn_mower` Start button will mow (or "All zones") — it does **not** start mowing itself |
| `switch` | Night mowing (proven), Rain sensor, Rain detection, Sound, Power saving | Non-proven toggles are opt-in / disabled by default |
| `camera` | Map | App-style SVG map: zones (with mowed %), per-segment boundaries (dashed = virtual boundary, solid = ride-on edge), obstacles / no-mow areas, dock, the live mower, and the reconstructed mowed trail (persisted across restarts) |

Motion commands (start / pause / dock) only ever fire on an explicit user
action. Nothing auto-mows on setup or on a poll.

### Lovelace cards & service

The integration bundles and **auto-registers** two custom cards (no manual
Lovelace resource needed) — just add them from the dashboard card picker:

- **`custom:navimow-scheduler-card`** — edit the weekly mowing plan (per-day
  on/off, time periods, per-period zones).
- **`custom:navimow-mow-card`** — a *Mow now* button that opens a dialog to pick
  a zone (or all) and choose *restart from zero* vs *continue*.

There is also a **`navimow_pro.mow`** service (zones + `reset`) and a
**`navimow_pro.set_schedule`** service for automations.

---

## Installation (HACS custom repository)

1. In Home Assistant open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add the repository URL and choose category **Integration**.
3. Install **Navimow (Private)**, then **restart Home Assistant**.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   **Navimow (Private)**.

(Manual alternative: copy `custom_components/navimow_pro` into your Home
Assistant `config/custom_components/` folder and restart.)

---

## Recommended setup — a dedicated shared account

**Strongly recommended: do not use your primary Navimow account for the
integration.** The Navimow cloud binds an app session to a device id, and a new
login can disturb the session running on your phone. Instead, use a **dedicated
second account that the mower is _shared_ to**:

1. **Create a second Navimow account** — in the Navimow app, log out and sign up
   with a different email (any address you control). This is the account Home
   Assistant will use.
2. **Share the mower to it** — log back in on your phone with your **primary**
   account, open the mower, and use the app's *share device* / *family sharing*
   feature to share the mower with the second account's email.
3. **Add the integration** with the **second account's** email + password
   (Settings → Devices & Services → Add Integration → *Navimow (Private)*).

Home Assistant generates and persists its **own** device id, so it registers as
a distinct, coexisting session — your phone keeps working normally (verified
live with a shared account). Your password is **not** stored; only the
refresh/access tokens are kept and refreshed perpetually. If the session ever
fully expires, Home Assistant raises a re-authentication prompt.

During setup the integration performs: passport login → device registration
(`user/user/login`) → vehicle discovery (`auth-list`).

---

## Multiple mowers

An account can own more than one mower. Add them as **one integration entry per
mower**:

- **Owned mowers** — run **Add Integration → Navimow (Private)** once per mower.
  If the account lists several, a picker appears; mowers you have already added
  are hidden, so just pick the next one (a single remaining mower is added
  automatically).
- **Shared mowers** (an account that owns none — e.g. the recommended shared
  account) — use the manual serial step once per mower.

Each mower becomes its own Home Assistant device with its own entities, cards
and (persisted) map/trail. The `navimow_pro.mow` / `navimow_pro.set_schedule`
services take a `device_id`, so they always target the right mower (required
once more than one is configured).

> **Note — untested with 2+ mowers on one account.** The single-mower path is
> well tested; multiple mowers on the **same** account is supported by design
> but not yet verified live. Each entry logs in with its own device id
> (independent sessions); if the cloud turns out to enforce one session per
> account, the entries could contend for the token. If you hit repeated re-auth
> prompts with several mowers on one account, please open an issue.

---

## Configuration options

Open the integration's **Configure** dialog to set the **zone list** used by the
`Mow zone` selector, as a comma-separated `id:name` list, e.g.:

```
1:Front lawn,5:Back lawn
```

The `id` is the map partition (region) id. This is needed because the full zone
list is not reliably exposed by a documented read endpoint on all firmwares (see
*Assumptions* below). If left blank, the integration tries to auto-discover zones
from the map endpoints and, failing that, falls back to whatever region the mower
currently reports.

Zone encoding is handled for you (little-endian `partitionIds` + the available
`partitionSetup` bitmask), matching the proven protocol.

---

## How it works (high level)

- **Authentication** — it signs in to the vendor's account service (the same one
  the mobile app uses) and keeps a refreshable session. No vendor developer
  program or public API key is required; you only ever provide your own login.
- **Mower cloud** — status and commands use the mobile app's own encrypted
  request format, so the integration talks to the mower the same way the app
  does. Your account identity travels inside the encrypted payload, not in plain
  HTTP headers. The protocol constants involved are app-wide (identical for every
  user of the app), not personal secrets.
- **Performance** — the encryption is CPU-bound and runs entirely in Home
  Assistant's executor, so it never blocks the event loop. Polling is every 30 s,
  and faster (~12 s) while the mower is actively mowing or returning.

---

## Assumptions & limitations (TODO)

These are the parts that are **not** individually proven against the live device
and are parsed/handled best-effort. They degrade gracefully (entity unavailable
or value `unknown`) rather than crashing:

- **Zone discovery.** Only the encoding and the mow commands are proven. The list
  of available zones per map is not proven from a read endpoint, hence the
  Options-based `id:name` fallback. Zone ids `1`/`5` in the reference yard are
  examples, not hardcoded.
- **Best-effort switches.** Only `nightMowSwitch` is a proven reversible write.
  Rain sensor, rain detection, sound and power saving reuse the same zero-padded
  `save-set-data` encoding with camelCase write keys inferred from the settings
  model (`MowerSettingBean`); they are added only when discovered in `set-list`
  and are disabled by default (opt-in).
- **Areas / progress / next-mow.** `mowing_progress` comes from `get-location`
  (`mowing_percentage`). Session area, weekly area, total area and the next-mow
  time are looked up under several candidate field names and may report `unknown`
  on firmwares that name them differently.
- **Maintenance (blades / chassis).** Field names are firmware-specific; parsed
  defensively from `get-component-maintenance`.
- **Map / coverage / trail.** The map geometry (zone boundaries with per-segment
  attributes, obstacles, no-mow areas, dock) **is** decoded from the map-detail
  endpoint, and per-zone mowed coverage comes from `get-path-info-time`. The
  exact per-stripe swept path (`get-path-info-data-compress`) is not accessible
  on this firmware, so the mowed **trail** overlay is reconstructed by sampling
  the mower's position while it cuts — persisted across restarts, best-effort.
- **Region/host.** Fixed to the `fra` region hosts, matching the proven setup.

---

## Credits

Built by porting proven, reverse-engineered protocol scripts into a clean async
Home Assistant component. For interoperability and personal use.
