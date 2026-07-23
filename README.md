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
| `sensor` | Battery, Status, Mowing progress, Current zone, Session area, Area this week, Next mow, Error, Wi-Fi signal, Blades life, Chassis life, State code | Areas / progress are best-effort (see below) |
| `binary_sensor` | Problem, Online, Docked | |
| `select` | Mow zone | Selecting a zone (or "All zones") starts mowing it |
| `switch` | Night mowing (proven), Rain sensor, Rain detection, Sound, Power saving | Non-proven toggles are opt-in / disabled by default |
| `camera` | Map | Lightweight SVG of the mower path + position |

Motion commands (start / pause / dock / zone select) only ever fire on an
explicit user action. Nothing auto-mows on setup or on a poll.

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

## Recommended setup — use a shared account

The Navimow cloud binds an app session to a device id. To avoid disturbing the
session on your phone, **do not** log the integration in with your primary
account. Instead:

1. Create a **second Navimow account** (a dedicated email).
2. In the Navimow app on your phone, **share the mower** to that second account.
3. Add this integration using the **second account's** email + password.

Home Assistant generates and persists its **own** device id, so it registers as
a distinct, coexisting session. Your phone keeps working normally. (This
coexistence was verified live with a shared account.)

During setup the integration performs: passport login → device registration
(`user/user/login`) → vehicle discovery (`auth-list`). Your password is **not**
stored — only the refresh/access tokens are kept and refreshed perpetually. If
the session ever fully expires, Home Assistant raises a re-authentication prompt.

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

## How it works (protocol summary)

- **Passport auth** (`api-passport-fra.willand.com`): signed `/v3/user/login` and
  `/v3/user/refresh`. The request sign is
  `SHA256(sorted "k=v&..." of {app_version, clientKey, os, os_language, os_version,
  timestamp, url} + params)`.
- **Mower cloud** (`navimow-fra.ninebot.com`, protocol `p:101`): each request is
  an envelope `{d,h,k,p,t}` — `k` = RSA-1024 PKCS#1 v1.5 wrap of a per-request
  AES key, `d` = AES-128-CBC of the plaintext, `h` = MD5 of the plaintext. The
  business identity (`uid`, `access_token`, `device_id`) travels **inside** the
  encrypted payload, never in HTTP headers. Responses `{r,s,v}` decrypt with a
  fixed session key. These crypto constants are app-wide (shared by all users),
  not user secrets.
- Polling is every 30 s (12 s while actively mowing / returning). The crypto is
  CPU-bound and synchronous, so all of it runs in Home Assistant's executor and
  never blocks the event loop.

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
- **Camera.** The full perimeter polygon lives in a compressed map blob that this
  integration does not decode, so the map shows path + position only.
- **Region/host.** Fixed to the `fra` region hosts, matching the proven setup.

---

## Credits

Built by porting proven, reverse-engineered protocol scripts into a clean async
Home Assistant component. For interoperability and personal use.
