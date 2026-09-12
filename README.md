<div align="center">

<img src="brands/icon.svg" width="112" alt="Traccar Client Extended">

# Traccar Client Extended

**Everything your GPS trackers report, as Home Assistant sensors.**

[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5?style=flat-square)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.2%2B-41BDF5?style=flat-square&logo=homeassistant&logoColor=white)](https://www.home-assistant.io)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

</div>

> [!NOTE]
> This is **not** the webhook-based `traccar` ("Traccar Client") integration that receives posts
> from the phone app. This one connects to a [Traccar **Server**](https://www.traccar.org/).

## What you get

| | |
|---|---|
| **Every device tracked** | Location, speed, bearing, accuracy, and every geofence it is inside |
| **Real sensors** | Battery, temperature, ignition, fuel level, odometer and around ninety more — each with the right unit, so they graph and keep history |
| **Nothing dropped** | Readings with no built-in mapping still become sensors, and new ones appear without a restart |
| **Events as triggers** | Alarms, geofence crossings, ignition on and off, device online and offline |

## Device profiles

Trackers send some readings as raw numbered parameters — `io800` holding `12500`, with nothing to
say it is an external voltage in millivolts. A **device profile** records what those mean for one
model, so anyone with that hardware gets named, typed sensors instead of raw numbers.

Profiles apply automatically once Traccar knows the device's model, or you can pick one per
device. Without one, you can still map readings yourself in the **Attribute overrides** table:
pick the attribute, say what it is, give it a unit.

> [!TIP]
> **No profile for your tracker? Ask for one — you do not have to work it out yourself.**
> [Open an issue](https://github.com/manjotsc/ha-traccar_client_extended/issues) saying which
> device you have and it will be added, for everyone with that model.
>
> Two things make it quick, both optional: the model as Traccar shows it under
> **Settings → Devices**, and a **Download diagnostics** file from the integration's three-dot
> menu — it lists every reading your tracker sends and how each was interpreted.
>
> Prefer to do it yourself? A profile is one JSON file and a pull request, no code — see
> [`device_profiles/`](custom_components/traccar_client_extended/device_profiles/).

## Install

**HACS** → three-dot menu → **Custom repositories** → add
`https://github.com/manjotsc/ha-traccar_client_extended` as an **Integration** → install →
restart Home Assistant.

<details>
<summary>Manual install</summary>

Copy `custom_components/traccar_client_extended` into your `config/custom_components` folder and
restart Home Assistant.

</details>

## Set up

**Settings → Devices & services → Add integration → Traccar Client Extended**

Enter the host and port (default `8082`), then either sign in with your Traccar email and
password — Home Assistant creates its own API token and forgets the password — or paste a token
you made under **Settings → Account** in Traccar.

Your devices appear on their own. The account you use decides what you see: a user who can see
three devices gives you three devices.

> [!IMPORTANT]
> **A readonly Traccar user cannot receive events.**
>
> Traccar only sends an event to Home Assistant when a **Web notification** exists for it, and
> this integration creates those for you at setup. A readonly account is not allowed to create
> them, and Traccar reports that as a generic error, so it is easy to miss.
>
> Everything else still works — devices, location, sensors — but no alarm, geofence, ignition or
> online/offline event will ever arrive.
>
> **Fix:** untick **readonly** for that user in Traccar, open the repair Home Assistant shows you
> and let it create the notifications, then tick readonly again. Readonly blocks *creating*
> things, never receiving them, so events keep arriving afterwards.
>
> It is not about administrator rights — an ordinary Traccar user creates notifications quite
> happily. If that user has no **Settings → Notifications** entry in Traccar, readonly is why.

## Settings

From the integration's **Configure**:

- Ignore attributes you do not want sensors for
- Re-map raw attributes, fleet-wide or for one device
- Filter out imprecise positions, so GPS jitter does not trigger zone automations
- Choose which events to handle, and whether new devices are added automatically

Each device also has its own settings, so one tracker can be treated differently from the rest.

## If something looks wrong

| | |
|---|---|
| **Repairs** | Name the likely cause, and usually offer to fix it |
| **Settings → System → System health** | Shows whether Traccar is reachable *and* whether live updates are flowing — they can fail independently |
| **Diagnostics** | From the three-dot menu: every reading each device sends and how it was interpreted, with tokens and coordinates removed |

Sensors that exist but never change usually mean live updates are not arriving — the repair and
the system health page will say so.

<div align="center">

MIT licensed · icon artwork in [`brands/`](brands/)

</div>
