#!/usr/bin/env python3
"""
Convert Tesla Tessie exported CSV data to TeslaMate import format.
- Timestamps in UTC (select Etc/UTC at import)
- charge_energy_added = cumulative based on Energy Remaining (battery_states)
- AC/DC classification BY SESSION (not by line): peak > 15 kW => DC
  => charger_phases = NULL for ALL lines in DC session,
     charger_phases = 1 for ALL lines in AC session.
  (TeslaMate distinguishes AC/DC via mode(charger_phases), and mode() ignores NULLs)
"""

import csv
import os
import argparse
from datetime import datetime, timezone
from collections import defaultdict

def parse_ts(ts_str):
    """Parse timestamp string to UTC datetime object."""
    ts_str = ts_str.strip().strip('\"')
    try:
        return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

def format_ts(dt):
    """Format datetime object to string."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def read_csv(filename, data_dir):
    """Read CSV file and return list of rows with parsed timestamps."""
    rows = []
    path = os.path.join(data_dir, filename)
    if not os.path.exists(path):
        print(f"  File not found: {path}")
        return rows
    with open(path, 'r') as f:
        reader = csv.reader(f)
        header = [h.strip().strip('\"') for h in next(reader)]
        for row in reader:
            if not row or not row[0].strip():
                continue
            try:
                ts = parse_ts(row[0])
                d = {'_ts': ts}
                for i, val in enumerate(row):
                    if i < len(header):
                        d[header[i]] = val.strip().strip('\"')
                rows.append(d)
            except Exception:
                pass
    print(f"  {filename}: {len(rows)} lines")
    return rows

def to_float(v):
    """Convert string to float, return None if invalid."""
    v = (v or '').strip()
    if not v:
        return None
    try:
        return float(v)
    except Exception:
        return None

MILE_KM = 1.609344

# Max age of the last driving sample before the car is treated as parked.
# Tessie logs driving_states roughly every 30-60s while moving, so a gap wider
# than this means the car stopped/slept (or the export ended mid-drive). Keeping
# a stale shift="D" forward-filled across such a gap makes TeslaMate build one
# giant drive whose duration_min overflows the smallint column on import.
STALE_DRIVE_SECONDS = 600  # 10 minutes

def to_miles_value(row, mi_key, km_key):
    """Return a distance/speed as miles-string for the TeslaFi format.
    Tessie may export either imperial (mi/mph) or metric (km/km/h) columns
    depending on the account's unit setting. TeslaMate's TeslaFi importer
    expects miles and reconverts to km, so metric inputs are converted here."""
    v = (row.get(mi_key, '') or '').strip()
    if v:
        return v  # already miles/mph -> passthrough
    f = to_float(row.get(km_key, ''))
    return '' if f is None else f'{f / MILE_KM:.6f}'

def build_session_dc_map(charging):
    """Group Starting/Charging states into contiguous sessions, classify by peak power, return {ts: is_dc}."""
    sessions = []
    cur = None
    for r in charging:
        ts = r['_ts']
        st = r.get('Charging State', '').strip()
        p = to_float(r.get('Charger Power (kW)', '')) or 0.0
        if st in ('Starting', 'Charging'):
            if cur is None:
                cur = {'start': ts, 'rows': []}
            cur['rows'].append((ts, p))
        else:
            if cur is not None:
                sessions.append(cur)
                cur = None
    if cur is not None:
        sessions.append(cur)

    ts_dc = {}
    dc_count = ac_count = 0
    for s in sessions:
        peak = max(p for _, p in s['rows']) if s['rows'] else 0.0
        is_dc = peak > 15.0  # DC threshold > 15 kW
        if is_dc:
            dc_count += 1
        else:
            ac_count += 1
        for ts, _ in s['rows']:
            ts_dc[ts] = is_dc
    print(f"  Sessions: {dc_count} DC, {ac_count} AC")
    return ts_dc

def main(input_dir, output_dir, vin=None, display_name=None):
    """Main conversion function."""
    vin = vin or "TESLA_DEFAULT_VIN"
    display_name = display_name or "My Tesla"
    print("Reading Tessie files...")
    os.makedirs(output_dir, exist_ok=True)

    charging = read_csv("charging_states.csv", input_dir)
    driving = read_csv("driving_states.csv", input_dir)
    battery = read_csv("battery_states.csv", input_dir)
    vehicle = read_csv("vehicle_states.csv", input_dir)
    climate = read_csv("climate_states.csv", input_dir)

    driving_by_ts = {r['_ts']: r for r in driving}
    battery_by_ts = {r['_ts']: r for r in battery}
    vehicle_by_ts = {r['_ts']: r for r in vehicle}
    climate_by_ts = {r['_ts']: r for r in climate}
    charging_by_ts = {r['_ts']: r for r in charging}

    print("Classifying AC/DC by session...")
    session_dc = build_session_dc_map(charging)

    all_ts = set(charging_by_ts.keys()) | set(driving_by_ts.keys())
    all_ts = sorted(all_ts)
    print(f"Unique timestamps: {len(all_ts)}")

    # Rows are only emitted at charging timestamps, so those are what TeslaMate
    # sees. A drive stays OPEN until a non-driving row closes it, and TeslaMate
    # dates the drive end at that closing row. If the last row before a data gap
    # is still shift="D", the drive is only closed by the first row AFTER the
    # gap, so its duration spans the whole gap -> drives.duration_min overflows.
    # Precompute the gap to the next emitted (charging) row for each timestamp.
    charging_ts_sorted = sorted(charging_by_ts.keys())
    next_gap = {}
    for i, cts in enumerate(charging_ts_sorted):
        nxt = charging_ts_sorted[i + 1] if i + 1 < len(charging_ts_sorted) else None
        next_gap[cts] = None if nxt is None else (nxt - cts).total_seconds()

    months = defaultdict(list)
    last_driving = {}
    last_battery = {}
    last_vehicle = {}
    last_climate = {}
    energy_at_start = None

    for ts in all_ts:
        if ts in driving_by_ts:
            last_driving = driving_by_ts[ts]
        if ts in battery_by_ts:
            last_battery = battery_by_ts[ts]
        if ts in vehicle_by_ts:
            last_vehicle = vehicle_by_ts[ts]
        if ts in climate_by_ts:
            last_climate = climate_by_ts[ts]

        charging_row = charging_by_ts.get(ts)
        if charging_row is None:
            continue

        row = {}
        row['Date'] = format_ts(ts)
        row['id'] = '1'
        lat = (last_driving.get('Latitude', '') or '').strip()
        row['state'] = 'online' if (lat and float(lat) != 0) else 'offline'

        # --- Charge state ---
        row['charging_state'] = charging_row.get('Charging State', '')
        bl = charging_row.get('Usable Battery Level (%)', '')
        row['battery_level'] = bl
        row['usable_battery_level'] = bl

        battery_range = to_miles_value(charging_row, 'Battery Range (mi)', 'Battery Range (km)')
        ideal_range = to_miles_value(charging_row, 'Ideal Battery Range (mi)', 'Ideal Battery Range (km)')
        if not ideal_range.strip():
            ideal_range = battery_range
        row['battery_range'] = battery_range
        row['ideal_battery_range'] = ideal_range

        row['charge_rate'] = to_miles_value(charging_row, 'Charge Rate (mph)', 'Charge Rate (km/h)')
        row['charger_actual_current'] = charging_row.get('Charger Current (A)', '')
        charger_power_str = charging_row.get('Charger Power (kW)', '')
        row['charger_power'] = charger_power_str
        row['charger_voltage'] = charging_row.get('Charger Voltage (V)', '')

        # --- AC/DC BY SESSION (consistent across all lines in session) ---
        is_dc = session_dc.get(ts, False)
        if is_dc:
            row['fast_charger_present'] = 'True'
            row['fast_charger_type'] = 'Combo'
            row['charger_phases'] = ''  # NULL => DC
            row['conn_charge_cable'] = 'SAE'
        else:
            row['fast_charger_present'] = 'False'
            row['fast_charger_type'] = 'AC'
            row['charger_phases'] = '1'  # 1 phase => AC
            row['conn_charge_cable'] = 'IEC'

        # --- charge_energy_added = cumulative Energy Remaining (kWh) ---
        charge_energy_added = ''
        energy_now = to_float(last_battery.get('Energy Remaining (kWh)', ''))
        if row['charging_state'] == 'Disconnected':
            energy_at_start = None
        if row['charging_state'] == 'Charging' and energy_at_start is None and energy_now is not None:
            energy_at_start = energy_now
        if row['charging_state'] == 'Charging' and energy_at_start is not None and energy_now is not None:
            cumulative_added = max(0, energy_now - energy_at_start)
            charge_energy_added = f'{cumulative_added:.6f}'
        row['charge_energy_added'] = charge_energy_added

        # --- Drive state (forward-fill) ---
        # Guard against stale drive state: Tessie stops logging driving_states
        # when the car parks/sleeps, and an export can end mid-drive (last
        # sample still shift="D"). Forward-filling that across a data gap makes
        # TeslaMate treat everything up to the next sample as ONE drive, so
        # drives.duration_min (smallint, minutes) overflows past 32767 and the
        # import crashes with a Postgrex EncodeError. If the last driving sample
        # is older than STALE_DRIVE_SECONDS, consider the car parked.
        driving_ts = last_driving.get('_ts')
        driving_stale = driving_ts is None or (ts - driving_ts).total_seconds() > STALE_DRIVE_SECONDS
        # forward_gap: the next emitted row is far away (or this is the last row),
        # so any open drive must be closed HERE to avoid it spanning the gap.
        fg = next_gap.get(ts)
        forward_gap = fg is None or fg > STALE_DRIVE_SECONDS
        if driving_stale or forward_gap:
            row['shift_state'] = 'P'
            row['speed'] = ''
        else:
            row['shift_state'] = last_driving.get('Shift State', '')
            row['speed'] = to_miles_value(last_driving, 'Speed (mph)', 'Speed (km/h)')
        row['latitude'] = last_driving.get('Latitude', '')
        row['longitude'] = last_driving.get('Longitude', '')
        row['odometer'] = to_miles_value(last_driving, 'Odometer (mi)', 'Odometer (km)')
        row['battery_current'] = last_battery.get('Pack Current (A)', '')

        # --- Power (kW) = pack_current × pack_voltage / 1000 ---
        pc = to_float(last_battery.get('Pack Current (A)', ''))
        pv = to_float(last_battery.get('Pack Voltage (V)', ''))
        if pc is not None and pv is not None:
            row['power'] = str(int(round(pc * pv / 1000.0)))
        else:
            row['power'] = '0'

        # Vehicle/Climate
        row['locked'] = last_vehicle.get('Locked', '')
        row['inside_temp'] = last_climate.get('Inside Temp (°C)', '')
        ot = last_climate.get('Outside Temp (°C)', '').strip()
        row['outside_temp'] = ot if ot else '0'
        row['is_climate_on'] = last_climate.get('Climate Enabled', '')

        # Battery heater (non-null required by API)
        row['battery_heater'] = 'False'
        row['battery_heater_on'] = 'False'
        row['battery_heater_no_power'] = 'False'

        # Fixed metadata
        row['display_name'] = display_name
        row['vin'] = vin
        row['vehicle_id'] = '1'
        row['vehicle_name'] = display_name

        # Normalize
        if bl.strip():
            try:
                row['battery_level'] = str(int(round(float(bl))))
                row['usable_battery_level'] = str(int(round(float(bl))))
            except Exception:
                pass
        locked = row['locked'].strip()
        if locked == '1':
            row['locked'] = 'True'
        elif locked == '0':
            row['locked'] = 'False'

        month_key = ts.strftime("%Y-%m")
        months[month_key].append(row)

    column_order = [
        'Date', 'id', 'state', 'shift_state', 'charging_state',
        'battery_level', 'usable_battery_level', 'battery_range', 'ideal_battery_range',
        'charge_energy_added', 'latitude', 'longitude', 'speed', 'odometer',
        'charge_rate', 'charger_power', 'charger_voltage', 'charger_phases',
        'charger_actual_current', 'fast_charger_present', 'fast_charger_type',
        'conn_charge_cable', 'battery_current', 'power', 'battery_heater',
        'battery_heater_on', 'battery_heater_no_power', 'inside_temp',
        'outside_temp', 'is_climate_on', 'locked', 'display_name',
        'vehicle_name', 'vin', 'vehicle_id',
    ]

    all_columns = set()
    for month_rows in months.values():
        for row in month_rows:
            all_columns.update(row.keys())
    remaining = sorted(all_columns - set(column_order))
    final_columns = [c for c in column_order if c in all_columns] + remaining

    for month_key in sorted(months.keys()):
        rows = months[month_key]
        year, month = month_key.split('-')
        filename = f"TeslaFi{int(month)}{year}.csv"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(final_columns)
            for row in rows:
                writer.writerow([row.get(c, '') for c in final_columns])
        print(f"  {filename}: {len(rows)} lines")

    print(f"\nFiles generated in: {output_dir}")
    print("IMPORTANT: select 'Etc/UTC' at import.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Convert Tesla Tessie CSV data to TeslaMate format')
    parser.add_argument('--input-dir', default='./tessie', 
                       help='Directory containing input CSV files')
    parser.add_argument('--output-dir', default='./output',
                       help='Directory for output CSV files')
    parser.add_argument('--vin', default=None,
                       help='VIN of the vehicle (optional, default: TESLA_DEFAULT_VIN)')
    parser.add_argument('--display-name', default=None,
                       help='Display name of the vehicle (optional, default: "My Tesla")')
    args = parser.parse_args()
    main(args.input_dir, args.output_dir, vin=args.vin, display_name=args.display_name)