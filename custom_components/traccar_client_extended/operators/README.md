# Network operators

One JSON file per mobile country code, mapping mobile network code to the name of
the network. This is what turns the `Operator` sensor's state from `302220` into
`Telus Mobility`.

Most trackers report the cell network as a numeric PLMN identifier rather than a
name — Teltonika's AVL parameter 241, "Active GSM Operator", arrives as `302220`
and Traccar's decoder stores it verbatim under `KEY_OPERATOR`. Nothing upstream of
this integration knows the code means Telus.

## Schema

```json
{
  "mcc": "302",
  "country": "Canada",
  "operators": {
    "220": "Telus Mobility",
    "610": "Bell Mobility",
    "720": "Rogers Wireless"
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `mcc` | yes | Three digits, as a **string**. Must match the file name. |
| `country` | no | For readers; nothing reads it at runtime. |
| `operators` | yes | MNC → name. Non-empty. |

MNC keys are two or three digits **as strings**, with leading zeros kept: MNC `01`
and MNC `1` are different assignments, and an integer key cannot tell them apart.

Files whose name starts with `_` are documentation and are not loaded.
`_TEMPLATE.json` is a starting point.

## The name is the network, not the plan

A PLMN identifies a network, and several brands routinely share one. `302-220`
carries Telus, Koodo and Public Mobile subscribers alike; the code cannot
distinguish them, and neither can this. Where the source lists several brands
against one code, the operating company is used instead of the brand list.

The raw code is always published alongside the name as the `traccar_operator`
state attribute, whether or not a name was found, so nothing is lost and a
template that wants the number does not have to care.

## How lookup works

The value Traccar reports is split by length: MCC is always three digits and never
has a leading zero, so whatever follows is the MNC.

```
302220  ->  MCC 302, MNC 220
 26201  ->  MCC 262, MNC 01
```

A code with no entry is left alone — the sensor keeps showing the number rather
than going `Unknown`. A decoder that already reports a name as text is left alone
for the same reason.

One fallback exists: some modems truncate a three-digit MNC to two, which is why
Telus is reported as both `302220` and `30222`. On a miss, a two-digit MNC is
retried with a trailing zero. It is only ever consulted after an exact match
fails, so it cannot shadow a real two-digit assignment.

## Regenerating

```bash
python tools/generate_operators.py
```

That rewrites every file here, so **hand edits do not survive**. A correction
belongs upstream, or in a pull request that survives the next regeneration.

The source is the [`mcc-mnc-list`](https://www.npmjs.com/package/mcc-mnc-list) npm
package (MIT), which publishes one row per PLMN assignment with a brand, an
operating company and a country.

AOSP's `carrier_list.textpb` was tried first, on the strength of its Apache-2.0
licence, and is the wrong shape for this. It exists to identify a *SIM*, so
wherever MVNOs share a network code the entries are discriminated by `gid1`,
`spn` or IMSI prefix rather than by MCC/MNC — there is no row saying "302220 is
Telus", only four rows saying which GID1 is Telus, Koodo, Public Mobile or PC
mobile. It cannot answer the question this asks, for the exact code that prompted
it. Its names are also SIM-branded (`T-Mobile - US`, `C Spire-US`) and it carries
no country.
