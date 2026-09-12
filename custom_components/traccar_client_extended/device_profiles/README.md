# Device profiles

Traccar's protocol decoders store anything they cannot identify under a generic
key with a raw value. Teltonika's unregistered AVL parameters, for example,
arrive as `io<id>` holding an unscaled integer — so an external voltage shows up
as `12500` with nothing to say it is millivolts, or even a voltage.

A profile records that answer once so nobody else has to work it out.

## Contributing

Add one `.json` file here named after the profile's `name`, then open a pull
request. No code changes are needed. Files starting with `_` are documentation
and are not loaded.

Copy `_TEMPLATE.json` to start.

## Schema

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Machine slug, lower case with underscores, e.g. `teltonika_ftc921`. Must match the filename and be unique. |
| `display_name` | yes | Human label shown in the dropdown, e.g. `Teltonika FTC921`. |
| `vendor` | no | Manufacturer, used to rank suggestions. |
| `description` | no | What the profile covers and where the mappings came from. |
| `protocols` | no | Traccar protocol names, used to rank suggestions, e.g. `["teltonika"]`. |
| `models` | no | Model strings matched against the device's model field in Traccar. |
| `attributes` | yes | The mappings. At least one. |

Each entry under `attributes` is keyed by the Traccar attribute name:

| Field | Meaning |
|---|---|
| `device_class` | A Home Assistant device class, e.g. `voltage`, `temperature`. |
| `unit` | Unit of measurement. Must be valid for the device class — CI enforces this. |
| `scale` | Multiplier applied to the raw value. Use `0.001` to turn millivolts into volts. |
| `precision` | Display precision. |
| `name` | Entity name, instead of the derived one such as "IO 800". |
| `platform` | `sensor` (default), `binary_sensor`, or `ignore` to hide the attribute. |
| `description` | Free text explaining the mapping. Not used at runtime; write it for the next person. |

## Why CI validates units

Home Assistant silently refuses long-term statistics for an invalid
device-class and unit pairing. A profile that pairs `voltage` with `°C` produces
a sensor that looks fine and quietly never records history, so every pairing is
checked against Home Assistant's own table before it can be merged.

## Finding your attribute keys

Download diagnostics from the integration's three-dot menu. It lists every
attribute each of your devices reports, its type, and how the integration
currently classifies it.
