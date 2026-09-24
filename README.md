# Navimow (Private) — Home Assistant integration

<p align="center">
  <a href="https://www.paypal.com/donate/?hosted_button_id=M325UXAK93XHL"><img src="https://www.paypalobjects.com/en_US/IT/i/btn/btn_donateCC_LG.gif" alt="Donate with PayPal" /></a>
  <br />
  <em>If this integration is useful to you, a donation to support development is welcome — thank you! 🙏</em>
</p>

Unofficial Home Assistant custom integration for **Segway Navimow** robot mowers
(i- and H-series, and some X-series). It works with your own Navimow account,
without the vendor's public Open API or developer programme.

> **Disclaimer.** This is an **unofficial**, community, interoperability project.
> It is not affiliated with, endorsed by, or supported by Segway, Ninebot, or
> Willand. Use it at your own risk. Names are used only to say which product it
> works with. There is no warranty:
> mowing commands move a real machine with spinning blades — use responsibly and
> keep people and pets clear of the lawn.

---

## Features

All entities live under a single Home Assistant device (the mower).

| Platform | Entity | Notes |
|---|---|---|
| `lawn_mower` | Mower | Start / Pause / Dock (+ Resume via Start when paused) |
| `sensor` | Battery, Status, Mowing progress, Coverage, Current zone, Session area, Area this week, Next mow, Error, Wi-Fi signal, Blades life, Chassis life, State code | Areas / progress are best-effort (see below) |
| `binary_sensor` | Problem, Online, Docked | A fault is held briefly after the cloud stops reporting it, so the sensor does not flicker |
| `number` | Cutting height, battery thresholds | Created only on mowers that report the setting; bounded by the values the machine accepts |
| `calendar` | Mowing schedule | Read-only view of the weekly plan |
| `device_tracker` | Mower position | The mower on Home Assistant's map, for zone and proximity automations (see below) |
| `select` | Mow zone | Stores which zone the `lawn_mower` Start button will mow (or "All zones") — it does **not** start mowing itself |
| `switch` | Night mowing, rain handling, sound, power saving, and the mower's other settings | Settings whose behaviour is unconfirmed are opt-in / disabled by default |
| `camera` | Map | App-style SVG map: zones (with mowed %), per-segment boundaries (dashed = virtual boundary, solid = ride-on edge), channels between zones, off-limit areas and VisionFence-off areas, dock, the live mower, and the reconstructed mowed trail (persisted across restarts) |

Motion commands (start / pause / dock) only ever fire on an explicit user
action. Nothing auto-mows on setup or on a poll.

### Lovelace cards & service

The integration bundles and **auto-registers** two custom cards (no manual
Lovelace resource needed) — just add them from the dashboard card picker:

- **`custom:navimow-scheduler-card`** — edit the weekly mowing plan (per-day
  on/off, time periods, per-period zones).
- **`custom:navimow-mow-card`** — a *Mow now* button that opens a dialog to pick
  a zone (or all) and choose *restart from zero* vs *continue*.

There are three services for automations: **`navimow_pro.mow`** (zones +
`reset`), **`navimow_pro.set_schedule`**, and **`navimow_pro.resume`**, which
resumes whatever job the mower already has without choosing zones — unlike *mow*,
it cannot discard the progress made so far.

> **"Custom element not found: navimow-mow-card"?** The loader is injected into
> the page when Home Assistant starts, so a browser still serving the cached
> frontend won't see it. **Hard-refresh** the page (Ctrl/Cmd+Shift+R, or clear the
> cache in the companion app) and it should appear.
>
> If it still doesn't, add the two resources by hand under **Settings → Dashboards
> → ⋮ → Resources**, as *JavaScript module*:
> `/local/navimow_pro/navimow-scheduler-card.js` and
> `/local/navimow_pro/navimow-mow-card.js`. That is safe to do even if the
> automatic loading later works — the cards refuse to register themselves twice —
> though you may then see each card listed twice in the picker.

---

## The mower on the Home Assistant map

`device_tracker` publishes the mower's real-world position, which is what makes
*"tell me if he leaves the property"* a single zone automation instead of a
template over the position sensors.

Worth knowing before you rely on it:

- The coordinates are **entity attributes**, so they land in your recorder
  database and therefore in your backups. On a dashboard the entity normally
  reads `Home`, not a number — only a map card or Developer Tools shows the
  position itself. If you would rather keep only the local `position_x` /
  `position_y` sensors, disable `device_tracker` in the entity list.
- A mower that has never had a GPS fix reports 0/0, which is a real place in the
  Gulf of Guinea. Those coordinates are refused, so the entity reads unknown
  instead of putting the marker off the coast of Africa.
- Nothing extra is fetched for this. It is the position the cloud already sends
  on every poll.

---

## What the map looks like

The `camera` entity draws a live map in the style of the mobile app: each zone
with the share of it already cut, the perimeter drawn dashed where it is a
virtual boundary and solid where it is a physical edge, the channels that link
zones, off-limit areas in orange and VisionFence-off areas in blue (as the app
colours them), the charging station, the mower's current
position, and the mowed trail built up as it works. It survives restarts.

Zone labels follow the app: a label is drawn only where it fits entirely inside
its zone -- the name and percentage on a large zone, the percentage alone on a
smaller one, nothing on a zone too small to hold either.

There are no screenshots here: the ones that used to be were of a real garden.

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
a distinct, coexisting session — your phone keeps working normally. Your
password is **not** stored; only the
refresh/access tokens are kept and refreshed perpetually. If the session ever
fully expires, Home Assistant raises a re-authentication prompt.

---

## Multiple mowers

An account can own more than one mower. Add them as **one integration entry per
mower**:

- **Owned mowers** — run **Add Integration → Navimow (Private)** once per mower.
  If the account lists several, a picker appears; mowers you have already added
  are hidden, so just pick the next one (a single remaining mower is added
  automatically).
- **Shared mowers** — once you have **accepted the share invitation**, the mowers
  are listed just like owned ones, so the picker above applies. The manual serial
  step is the fallback for when nothing is listed (typically an invitation that
  has not been accepted yet).

Each mower becomes its own Home Assistant device with its own entities, cards
and (persisted) map/trail. The `navimow_pro.mow` / `navimow_pro.set_schedule`
services take a `device_id`, so they always target the right mower (required
once more than one is configured).

> **Note — several mowers on one account.** The cloud binds **one device identity
> per account**, so two entries logging in with their own identity used to kick
> each other out (reported live, and fixed in 0.2.2): entries belonging to the
> same account now share that identity, which keeps them all signed in. Entries
> created before 0.2.2 converge onto a shared identity the next time you
> re-authenticate one of them. If you still see repeated re-auth prompts with
> several mowers on one account, please open an issue.

---

## Configuration options

Open the integration's **Configure** dialog to set the **zone list** used by the
`Mow zone` selector, as a comma-separated `id:name` list, e.g.:

```
1:Front lawn,5:Back lawn
```

The `id` is the zone number as the mower knows it. This is only needed because
the zone list is not reported reliably on every firmware. Leave it blank and the
integration works the zones out for itself; fill it in if yours does not, or if
you want your own names.

Everything else about zones is handled for you, including the order they are
mowed in.

The same dialog sets the **mower's size on the map**, in percent of the default
(10–300 %) — handy when the lawn is large and the icon gets lost in it.

---

## How it works (high level)

- **Authentication** — you sign in with your own Navimow account and the
  integration keeps a refreshable session. No developer programme or API key is
  needed; the only credentials involved are yours.
- **Performance** — the work is CPU-bound and runs entirely in Home Assistant's
  executor, so it never blocks the event loop. All calls share **one kept-alive
  HTTPS connection**, so a refresh costs no repeated TCP/TLS handshakes.
- **Polling** — deliberately uneven, since there is no push. Every **3 s while
  cutting** (that density is what reconstructs the mowed path on the map),
  **12 s while returning to the dock**, **30 s** when something is wrong or a
  scheduled mow is due within 15 minutes, and **120 s sitting idle in the dock**,
  where nothing changes but the battery. A command sent from Home Assistant
  refreshes immediately, so the slow idle rate is never felt; a mow started from
  the *phone app* can take up to 2 minutes to show up.

---

## Limitations

Some things are handled best-effort and degrade gracefully — an entity goes
unavailable or reads `unknown` rather than the integration crashing:

- **Zone discovery.** The zone list is not reliably reported on every firmware,
  hence the Options-based `id:name` fallback described above.
- **Some switches are opt-in.** Where a setting's behaviour could not be
  confirmed on a real machine, the entity is created only when the mower reports
  the setting, and is disabled by default. Enable it if you want it.
- **Areas, progress and next mow.** These fields are named differently across
  firmwares, so they are looked up under several candidates and may read
  `unknown` on a model that names them another way.
- **Maintenance (blades / chassis).** Same story — parsed defensively.
- **Mowed trail.** Per-zone coverage percentages come from the mower. The drawn
  trail is reconstructed by sampling the mower's position while it cuts, since
  the exact swept path is not available; it is persisted across restarts and is
  an approximation.
- **Cutting height** is read on every model that reports one. It is offered as
  a slider on models that list the heights they accept, except families known to
  have a manual knob (the i1). Every change is read back half a minute later, and
  a notification says so if the mower did not apply it.
- **Features a model family lacks.** Every model reports much the same settings,
  so a field being present is not proof the feature exists. A short list of
  field-confirmed exceptions hides what a family does not have -- currently no
  remote cutting height on the i1, and no charge limit on the X3.
- **Zone ids in services.** `mow` and `set_schedule` refuse zone ids the decoded
  map does not contain — but only once the map has actually decoded. On a
  firmware whose map never arrives, nothing is checked, because refusing a
  command there would turn our gap into your error.
- **GPS position.** Whether the cloud reports a live fix or a fixed reference
  point is not something the integration can tell apart. If the marker never
  moves while the mower is cutting, that is the cloud, not the entity.

---

## Credits

Built and maintained by one person, for interoperability and personal use, with
bug reports, diagnostics and fixes from the people in the issue tracker — several
of whom found things that would otherwise still be broken.
