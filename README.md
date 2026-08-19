[comment]: # (Readme section)
# Tesla Tessie to TeslaMate Converter

Converts Tesla Tessie exported CSV data to the TeslaFi format for import into TeslaMate.

## Why this tool?

TeslaMate supports importing data from TeslaFi exports, but **not directly from Tessie**. Tessie exports data as 5 separate CSV files that TeslaMate cannot ingest. This script merges and transforms them into the format TeslaMate expects.

## Input files

Place your Tessie export CSVs in a single directory. The converter looks for these 5 files:

| File | Content |
|------|---------|
| `charging_states.csv` | Charging sessions |
| `driving_states.csv` | Driving/geolocation data |
| `battery_states.csv` | Battery and power data |
| `vehicle_states.csv` | Vehicle state (lock, etc.) |
| `climate_states.csv` | Climate/temperature data |

## Usage

```bash
pip install -r requirements.txt

python converter.py --input-dir ./tessie_data --output-dir ./output
```

The output directory will contain one file per month: `TeslaFiMMYYYY.csv`.

## Importing into TeslaMate

1. Open your TeslaMate instance
2. Go to **"Import from TeslaFi"** in the main menu
3. Upload all generated `TeslaFi*.csv` files
4. Set timezone to **"Etc/UTC"** (Tessie data is UTC)
5. Click **Import**

## How it works

The script handles all TeslaMate's import requirements:

- **Required `id` column** — TeslaMate fails with "No complete vehicle data was found" if missing
- **AC/DC classification** — detected per charging session (peak power > 15 kW = DC, else AC)
- **`charger_phases` consistency** — NULL for DC sessions, 1 for AC (PostgreSQL `mode()` ignores NULLs)
- **`charge_energy_added`** — cumulative calculation from Energy Remaining (kWh) within each charging session
- **Monthly file splitting** — one CSV per month to keep files manageable
- **Forward-filled state** — driving, battery, vehicle, and climate data are forward-filled between samples

## Known limitations

| Limitation | Reason |
|------------|--------|
| **Elevation stays NULL** | TeslaMate's USGS elevation-filling job only processes ~10 recent days |
| **No regen energy** | Tessie's ~30-second sampling is too coarse for energy recovery calculation |
| **Short-trip efficiency anomalies** | Tessie's "Battery Range" is a smoothed estimate that barely changes on trips under 10 km |
| **Disconnected events** | Not supported — these add no data and can break TeslaMate import |

## Requirements

- Python 3.8+
- Standard library only (no external dependencies; pandas is listed but the script uses only `csv` and `argparse` from stdlib)

## License

MIT

---

*This is an unofficial community tool. Not affiliated with Tesla, Inc., TeslaFi, or Tessie.*