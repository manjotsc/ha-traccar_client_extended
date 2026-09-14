# Device profiles

Traccar's protocol decoders store anything they cannot identify under a generic
key with a raw value. Teltonika's unregistered AVL parameters, for example,
arrive as `io<id>` holding an unscaled integer — so an external voltage shows up
as `12500` with nothing to say it is millivolts, or even a voltage.

A profile records that answer once so nobody else has to work it out.

> [!IMPORTANT]
> **Check whether Traccar already decodes the parameter before adding an `io<id>`
> entry.** `TeltonikaProtocolDecoder` registers many AVL IDs to named attributes —
> AVL 21 becomes `rssi`, 239 becomes `ignition`, 241 becomes `operator` — and a
> registration marked `any` fires whatever the device model is. Those never arrive
> as `io<id>`, so an `io21` entry would be configuration that can never match.
> Override the name Traccar gives it instead: profiles key on any attribute, not
> just `io<id>`.

A profile also outranks the **denylist** — the handful of keys (`raw`, `event`,
`image`, `dtcs`, …) that never become entities because some protocols put an
unbounded blob in them. Where a protocol puts something small and useful there
instead, a profile is the right scope to say so: `mictrack_mt600.json` labels
`event`, which on that hardware is the reason the tracker reported.

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
| `values` | Raw value → label, for a reading that is an enumeration rather than a measurement. See below. |
| `offset` | Added after `scale`, for encodings of the form `raw × k + c`. |
| `state_class` | `measurement`, `total` or `total_increasing`. Only needed for a reading with no unit and no device class — those infer `measurement` on their own. |
| `entity_category` | `diagnostic`, to file the entity under the device page's diagnostic section. `config` is refused: a sensor carrying it raises on add and never appears. |
| `enabled_by_default` | `false` to create the entity switched off. Independent of `entity_category`. |
| `decode` | `hex_ascii`, for a value Traccar hex-dumped. See below. |
| `fields` | Values packed inside this one, each becoming its own sensor. See below. |
| `description` | Free text explaining the mapping. Not used at runtime; write it for the next person. |

### Overriding an attribute Home Assistant already categorises

An override builds a fresh entity description, so it does not inherit the built-in
mapping's `entity_category`. A profile that renames a curated diagnostic reading —
`rssi`, `sat`, `hdop` — and wants it to stay in the diagnostic section has to say
so:

```json
"rssi": { "name": "Signal level", "state_class": "measurement",
          "entity_category": "diagnostic", "enabled_by_default": false }
```

`entity_category` and `enabled_by_default` are separate things in Home Assistant —
the built-in diagnostic readings set both — so restoring the built-in treatment
means naming both.

`enabled_by_default` only affects entities created **after** it is set. Home
Assistant keeps the enabled state in its registry once an entity exists, so an
entity somebody already has stays as it is; turning one off after the fact is a
job for the entity's own settings.

### Enumerated readings

Plenty of IO parameters are a mode or a status byte rather than a quantity —
Teltonika's "Network Type" and "GNSS Status" among them. `values` gives those
numbers their meanings:

```json
"io69": {
  "name": "GNSS status",
  "values": { "0": "Off", "1": "On, no fix", "2": "On, with fix", "3": "Sleep" }
}
```

Three things follow from it:

* **A value you do not list keeps showing as a number.** Vendors add values in
  firmware releases, and a number is the one thing a reader can always act on.
  This is also why the entity is not a `device_class: enum` sensor — an enum
  logs an error for every state outside its declared options.
* **Keys are strings, and are not normalised.** `"01"` and `"1"` are different
  keys, because the reading is an identifier and not a quantity.
* **`values` cannot be combined with `device_class`, `unit` or `scale`, and
  cannot be used on a `binary_sensor`.** A labelled state is text; those fields
  all say it is a measurement. The loader refuses the combination rather than
  storing something that would not work.

The raw number stays available as the `raw_value` state attribute either way.

### Values Traccar hex-dumped

Traccar decodes only VIN and DTCs out of a variable-length IO element. Everything
else goes through:

```java
position.set(Position.PREFIX_IO + id, ByteBufUtil.hexDump(buf.readSlice(length)));
```

So Teltonika's AVL 641, a 22-byte ASCII ICCID, arrives as 44 hex characters.
`decode` undoes it:

```json
"io641": { "name": "ICCID", "decode": "hex_ascii", "entity_category": "diagnostic" }
```

`3839313033303030303030303331373139333433` then reads as `89103000000031719343`.

Anything that does not decode is passed through unchanged — not hex, an odd
number of characters, bytes that are not ASCII, or a result containing control
characters. Firmware that sends the parameter as plain text keeps working, and a
failed conversion never costs the reading. NUL and space padding is trimmed, and
the untouched value stays available as the `raw_value` state attribute.

Because a decoded value is text, `decode` cannot be combined with
`device_class`, `unit`, `scale`, `offset`, `values` or `fields`, and only applies
to a sensor.

### Packed readings

Vendors routinely pack several readings into one parameter. Teltonika's AVL 1148
carries RSSI, RSRP, SINR and RSRQ in the four bytes of one 32-bit word: published
as a single number it is a sensor reading `272725845`, which is not a value of
anything. `fields` splits it:

```json
"io1148": {
  "platform": "ignore",
  "fields": {
    "rssi": { "byte": 0, "name": "RSSI", "device_class": "signal_strength",
              "unit": "dBm", "scale": -1 },
    "sinr": { "byte": 2, "name": "SINR", "unit": "dB", "scale": 0.2,
              "offset": -20 }
  }
}
```

* **Say where the value sits** with either `byte` (0–7, counting from the least
  significant — the way vendor tables number them) or `bits: [offset, width]`,
  which counts bits from the least significant and can be any run up to 64 bits
  wide. `byte: 2` is exactly `bits: [16, 8]`. Give one or the other, not both.
* **`signed: true`** reads the field as two's complement, for a value that goes
  below zero — a temperature byte reading 251 is −5, not 251.
* **`platform: "binary_sensor"`** turns a field into an on/off entity, which is
  what a single flag bit inside a status word usually is. A binary field takes
  `device_class` but not `unit`, `scale`, `offset`, `precision`, `signed` or
  `values` — none of them mean anything for a flag.
* **`values`** labels a field the same way it labels an attribute, for a few bits
  that spell a mode rather than a quantity.
* The rest — `name`, `device_class`, `unit`, `scale`, `offset`, `precision`,
  `entity_category`, `enabled_by_default` — behaves exactly as it does on a
  normal attribute, including the device-class/unit pairing that CI enforces.

Between them those cover how vendors actually pack things, so decoding a new
device should never need a code change:

```json
"io1234": {
  "platform": "ignore",
  "fields": {
    "rssi":     { "byte": 0, "unit": "dBm", "device_class": "signal_strength",
                  "scale": -1 },
    "coolant":  { "bits": [8, 16], "signed": true, "scale": 0.1, "unit": "°C",
                  "device_class": "temperature" },
    "towing":   { "bits": [3, 1], "platform": "binary_sensor",
                  "device_class": "problem" },
    "mode":     { "bits": [4, 3], "values": { "0": "Idle", "1": "Active" } }
  }
}
```
* The field name becomes part of the entity's unique id, so it must be lower
  case letters, digits and underscores. `io1148` with a `rssi` field produces
  `attr_io1148_rssi`.
* **`platform: "ignore"` on the parent is what hides the packed word itself.**
  `fields` does not imply it — some packed parameters are worth seeing whole —
  and going through `ignore` means the existing clean-up removes the old entity
  rather than leaving it registered and unavailable.
* A parameter that arrives as something other than an integer reads as
  unavailable rather than as byte zero.

## Why CI validates units

Home Assistant silently refuses long-term statistics for an invalid
device-class and unit pairing. A profile that pairs `voltage` with `°C` produces
a sensor that looks fine and quietly never records history, so every pairing is
checked against Home Assistant's own table before it can be merged.

## Finding your attribute keys

Download diagnostics from the integration's three-dot menu. It lists every
attribute each of your devices reports, its type, and how the integration
currently classifies it.
