#!/usr/bin/env python3
import os
import sys
import time
import json
import re
import socket
import subprocess
import threading
import shutil
import webbrowser
import http.server
import socketserver

# Global Configuration
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    PROJECT_DIR = sys._MEIPASS
else:
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STEAM_PATH = os.path.expanduser('~/.local/share/Steam')
if not os.path.exists(STEAM_PATH):
    STEAM_PATH = os.path.expanduser('~/.steam/steam')

# CLI flags: --headless runs a compile of all pending games with no browser/UI.
HEADLESS = '--headless' in sys.argv
COMPILE_ALL_FLAG = '--all' in sys.argv

# Global State
is_compiling = False
max_threads = os.cpu_count() or 1
thread_count = max_threads
games_data = {}
current_game_appid = None
current_file_path = None
current_progress = 0.0
overall_progress = 0.0
progress_detail = ""
total_files = 0
remaining_files = 0
total_games_in_queue = 0
completed_games_count = 0
cross_game_progress = 0.0
log_buffer = []
last_ping_time = time.time()
running_process = None
cancel_requested = False
current_cpu_usage = 0.0

# GPU State
gpu_list = []       # [{'index': 0, 'name': 'AMD Radeon ...', 'vendor': 'amd'}, ...]
selected_gpu_index = None   # None = not yet chosen, will default to first discrete NVIDIA or index 0

# SteamGridDB API key (optional, for higher-quality game artwork)
steamgriddb_api_key = ''
CONFIG_FILE = os.path.expanduser('~/.config/stutterless/config.json')

# Auto-update (systemd timer) state
auto_update_enabled = False

# Cache-size stats for the current/last run (the headline "X GB compiled" number)
last_run_stats = {
    'before_bytes': 0,
    'after_bytes': 0,
    'gained_bytes': 0,
    'games_compiled': 0,
    'finished_at': 0,
}
# Total compiled cache size across all games, refreshed on scan
total_cache_bytes = 0

# Benchmark (MangoHud) support
BENCHMARK_DIR = os.path.expanduser('~/.config/stutterless/benchmarks')
# appid -> unix timestamp of last compile, used to split before/after logs
last_compile_time = {}

# Mutex for state updates
state_lock = threading.Lock()

# GPU Detection via /sys/bus/pci/devices
VENDOR_MAP = {
    '10de': 'nvidia',
    '1002': 'amd',
    '8086': 'intel',
}

def detect_gpus():
    """Enumerate Vulkan-capable GPUs by parsing /sys/bus/pci/devices.
    Returns a list of dicts with index, name, vendor, and pci_id.
    Index matches the order fossilize_replay --gpu-index expects (Vulkan physical device order).
    Falls back to a single unknown entry if detection fails.
    """
    global gpu_list, selected_gpu_index
    found = []

    try:
        pci_base = '/sys/bus/pci/devices'
        entries = sorted(os.listdir(pci_base))
        for entry in entries:
            dev_path = os.path.join(pci_base, entry)
            class_path = os.path.join(dev_path, 'class')
            vendor_path = os.path.join(dev_path, 'vendor')
            device_path = os.path.join(dev_path, 'device')
            label_path = os.path.join(dev_path, 'label')

            if not os.path.exists(class_path):
                continue
            try:
                with open(class_path, 'r') as f:
                    pci_class = f.read().strip()
                # 0x0300 = VGA, 0x0302 = 3D, 0x0380 = display
                if not pci_class.startswith('0x03'):
                    continue

                vendor_id = ''
                device_id = ''
                if os.path.exists(vendor_path):
                    with open(vendor_path, 'r') as f:
                        vendor_id = f.read().strip().lower().replace('0x', '')
                if os.path.exists(device_path):
                    with open(device_path, 'r') as f:
                        device_id = f.read().strip().lower().replace('0x', '')

                vendor_name = VENDOR_MAP.get(vendor_id, 'unknown')

                # Try to get a human-readable name from drm or modalias
                name = None
                drm_dir = os.path.join(dev_path, 'drm')
                if os.path.exists(drm_dir):
                    cards = [d for d in os.listdir(drm_dir) if d.startswith('card')]
                    if cards:
                        card_name_path = os.path.join(drm_dir, cards[0], 'device', 'product_name')
                        if os.path.exists(card_name_path):
                            with open(card_name_path, 'r') as f:
                                name = f.read().strip()

                if not name:
                    # Compose a name from vendor + device IDs
                    vendor_label = vendor_name.upper() if vendor_name != 'unknown' else 'GPU'
                    name = f"{vendor_label} [{vendor_id}:{device_id}]"

                found.append({
                    'name': name,
                    'vendor': vendor_name,
                    'pci_id': entry,
                    'vendor_id': vendor_id,
                    'device_id': device_id,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"GPU detection error: {e}")

    # Assign Vulkan-style indices (sorted by PCI address, matching driver enumeration order)
    if not found:
        found = [{'name': 'Unknown GPU', 'vendor': 'unknown', 'pci_id': '', 'vendor_id': '', 'device_id': ''}]

    gpu_list = [dict(index=i, **g) for i, g in enumerate(found)]

    # Auto-select: prefer first NVIDIA, then first AMD discrete, then index 0
    auto = 0
    for g in gpu_list:
        if g['vendor'] == 'nvidia':
            auto = g['index']
            break
    else:
        for g in gpu_list:
            if g['vendor'] == 'amd':
                auto = g['index']
                break

    if selected_gpu_index is None:
        selected_gpu_index = auto

    print(f"Detected GPUs: {[g['name'] for g in gpu_list]}")
    print(f"Auto-selected GPU index: {selected_gpu_index} ({gpu_list[selected_gpu_index]['name']})")

def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}"
    print(line, flush=True)
    with state_lock:
        log_buffer.append(line)
        if len(log_buffer) > 1000:
            log_buffer.pop(0)

# CPU Usage tracking via /proc/stat
_last_cpu_time = 0
_last_idle_time = 0

def get_cpu_usage():
    global _last_cpu_time, _last_idle_time
    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
        parts = line.split()
        fields = [float(x) for x in parts[1:8]]
        idle = fields[3] + fields[4]
        total = sum(fields)
        
        if _last_cpu_time == 0:
            _last_cpu_time = total
            _last_idle_time = idle
            return 0.0
        
        diff_total = total - _last_cpu_time
        diff_idle = idle - _last_idle_time
        
        _last_cpu_time = total
        _last_idle_time = idle
        
        if diff_total == 0:
            return 0.0
        return 100.0 * (1.0 - (diff_idle / diff_total))
    except Exception:
        return 0.0

def cpu_monitor_loop():
    global current_cpu_usage
    while True:
        current_cpu_usage = get_cpu_usage()
        time.sleep(1.0)

# Keep-alive checker
def check_keep_alive():
    global last_ping_time
    start_time = time.time()
    log("check_keep_alive thread started.")
    while True:
        time.sleep(2)
        elapsed = time.time() - start_time
        ping_diff = time.time() - last_ping_time
        if elapsed > 25:
            if ping_diff > 15:
                log("No active browser connection detected for 15 seconds. Exiting...")
                # Cleanup process
                if running_process:
                    try:
                        running_process.terminate()
                    except Exception:
                        pass
                os._exit(0)

# Directory size helper
def get_dir_size(path):
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += get_dir_size(entry.path)
    except Exception:
        pass
    return total

# AC power detection — used to gate auto-update runs on laptops.
def on_ac_power():
    """Return True if on AC power or if power state can't be determined
    (desktops have no battery, so absence of battery == always 'plugged in')."""
    ps_base = '/sys/class/power_supply'
    try:
        if not os.path.isdir(ps_base):
            return True
        supplies = os.listdir(ps_base)
        # Look for a mains adapter first.
        for s in supplies:
            type_path = os.path.join(ps_base, s, 'type')
            online_path = os.path.join(ps_base, s, 'online')
            if os.path.exists(type_path) and os.path.exists(online_path):
                with open(type_path) as f:
                    if f.read().strip().lower() == 'mains':
                        with open(online_path) as o:
                            return o.read().strip() == '1'
        # No mains adapter found — check if there's any battery at all.
        has_battery = False
        for s in supplies:
            type_path = os.path.join(ps_base, s, 'type')
            if os.path.exists(type_path):
                with open(type_path) as f:
                    if f.read().strip().lower() == 'battery':
                        has_battery = True
        # No battery => desktop => treat as always on AC.
        return not has_battery
    except Exception:
        return True

# systemd user timer control for the auto-update feature.
SYSTEMD_USER_DIR = os.path.expanduser('~/.config/systemd/user')
TIMER_UNIT = 'stutterless.timer'
SERVICE_UNIT = 'stutterless.service'

def _self_exec_path():
    """Path to invoke for the headless run inside the service unit."""
    if getattr(sys, 'frozen', False):
        return sys.executable  # the PyInstaller binary itself
    return f"{sys.executable} {os.path.abspath(__file__)}"

def write_systemd_units():
    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
    exec_start = f"{_self_exec_path()} --headless --all"
    service = f"""[Unit]
Description=Stutterless - pre-compile Vulkan pipeline shaders for Steam games
After=graphical-session.target

[Service]
Type=oneshot
ExecStart={exec_start}
Nice=19
IOSchedulingClass=idle
CPUSchedulingPolicy=idle
"""
    timer = """[Unit]
Description=Run Stutterless shader pre-compilation periodically

[Timer]
OnStartupSec=15min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
"""
    with open(os.path.join(SYSTEMD_USER_DIR, SERVICE_UNIT), 'w') as f:
        f.write(service)
    with open(os.path.join(SYSTEMD_USER_DIR, TIMER_UNIT), 'w') as f:
        f.write(timer)

def set_auto_update(enabled):
    """Enable/disable the systemd user timer. Returns (ok, message)."""
    global auto_update_enabled
    if not shutil.which('systemctl'):
        return False, "systemctl not available on this system."
    try:
        if enabled:
            write_systemd_units()
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=False)
            subprocess.run(['systemctl', '--user', 'enable', '--now', TIMER_UNIT], check=True)
            auto_update_enabled = True
            log("Auto-update enabled (systemd timer armed; runs only on AC power).")
            save_config()
            return True, "Auto-update enabled."
        else:
            subprocess.run(['systemctl', '--user', 'disable', '--now', TIMER_UNIT], check=False)
            auto_update_enabled = False
            log("Auto-update disabled.")
            save_config()
            return True, "Auto-update disabled."
    except Exception as e:
        log(f"[ERROR] Failed to set auto-update: {e}")
        return False, str(e)

def timer_is_active():
    if not shutil.which('systemctl'):
        return False
    try:
        r = subprocess.run(['systemctl', '--user', 'is-enabled', TIMER_UNIT],
                           capture_output=True, text=True)
        return r.stdout.strip() == 'enabled'
    except Exception:
        return False

# ── MangoHud benchmark support ───────────────────────────────
def mangohud_available():
    return shutil.which('mangohud') is not None

def benchmark_launch_string(appid):
    """Return the Steam launch-options string that enables MangoHud logging
    for this game, writing CSVs into the per-game benchmark folder."""
    folder = os.path.join(BENCHMARK_DIR, str(appid))
    # MangoHud will not create a missing output folder — make it now.
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    # log_duration caps each log at 300s (5 min) so files stay comparable —
    # long enough to cover game loading plus a few minutes of real gameplay.
    cfg = (f"output_folder={folder},autostart_log=1,log_duration=300,"
           f"benchmark_percentiles=AVG+1+0.1")
    return f"MANGOHUD=1 MANGOHUD_CONFIG={cfg} %command%"

def _parse_mangohud_csv(path):
    """Parse a MangoHud CSV log. Returns dict with frametimes_ms (list),
    or None if unparseable.

    MangoHud format has TWO header sections:
      Line 1:  os,cpu,gpu,ram,kernel,driver        (system header)
      Line 2:  <actual system values>
      Line 3:  fps,frametime,cpu_load,gpu_load,...  (data column header)
      Line 4+: per-frame numeric rows

    We locate the data-column header row (the one containing 'frametime'),
    read which column index holds the frametime, then parse rows after it.
    Frametime is logged in microseconds; we normalise to milliseconds.
    Falls back to scanning for any plausible frametime column if the
    header can't be found."""
    try:
        with open(path, 'r', errors='ignore') as f:
            raw_lines = [ln.rstrip('\n') for ln in f]
    except Exception:
        return None
    lines = [ln for ln in raw_lines if ln.strip()]
    if len(lines) < 4:
        return None

    ft_col = None
    fps_col = None
    data_start = 0

    # Find the data-column header row (contains 'frametime' or 'fps').
    for i, ln in enumerate(lines):
        low = ln.lower()
        if 'frametime' in low:
            cols = [c.strip().lower() for c in ln.split(',')]
            for idx, c in enumerate(cols):
                if c == 'frametime' or c.startswith('frametime'):
                    ft_col = idx
                if c == 'fps':
                    fps_col = idx
            data_start = i + 1
            break

    frametimes_ms = []

    def push_ft(val_us):
        # Normalise to ms. MangoHud logs frametime in microseconds.
        if val_us <= 0:
            return
        if val_us > 1e5:      # nanoseconds
            ms = val_us / 1e6
        elif val_us > 200:    # microseconds
            ms = val_us / 1e3
        else:                 # already ms
            ms = val_us
        if 0.1 <= ms <= 1000:
            frametimes_ms.append(ms)

    if ft_col is not None:
        # Parse using the known frametime column.
        for ln in lines[data_start:]:
            parts = ln.split(',')
            if len(parts) <= ft_col:
                continue
            try:
                push_ft(float(parts[ft_col]))
            except ValueError:
                continue
    else:
        # Fallback: no frametime header found. Older MangoHud logs only
        # had fps in column 0. Derive frametime from fps where possible.
        for ln in lines:
            parts = ln.split(',')
            if len(parts) < 1:
                continue
            try:
                fps = float(parts[0])
            except ValueError:
                continue
            if fps > 0:
                ms = 1000.0 / fps
                if 0.1 <= ms <= 1000:
                    frametimes_ms.append(ms)

    if len(frametimes_ms) < 20:
        return None
    return {'frametimes_ms': frametimes_ms}

def _analyse_frametimes(frametimes_ms):
    """Compute avg FPS, 1% low FPS, 0.1% low FPS, and a stutter count.
    1% low FPS is derived from the 99th-percentile (worst) frametimes,
    which is the metric that actually captures shader stutter."""
    n = len(frametimes_ms)
    if n == 0:
        return None
    s = sorted(frametimes_ms)
    total = sum(frametimes_ms)
    avg_ft = total / n
    avg_fps = 1000.0 / avg_ft if avg_ft > 0 else 0

    # 1% low: average of the slowest 1% of frames (worst frametimes),
    # expressed as FPS. This is the widely-used "1% low" definition.
    k1 = max(1, n // 100)
    worst1 = s[-k1:]
    low1_fps = 1000.0 / (sum(worst1) / len(worst1))

    k01 = max(1, n // 1000)
    worst01 = s[-k01:]
    low01_fps = 1000.0 / (sum(worst01) / len(worst01))

    # Stutter count: frames taking >2x the median frametime.
    median = s[n // 2]
    stutters = sum(1 for ft in frametimes_ms if ft > 2.0 * median)

    return {
        'frames': n,
        'avg_fps': round(avg_fps, 1),
        'low1_fps': round(low1_fps, 1),
        'low01_fps': round(low01_fps, 1),
        'stutter_count': stutters,
        'stutter_pct': round(100.0 * stutters / n, 2),
    }

def _downsample(series, target=200):
    """Reduce a frametime series to ~target points for graphing,
    taking the max in each bucket so stutter spikes are preserved."""
    n = len(series)
    if n <= target:
        return [round(x, 2) for x in series]
    bucket = n / target
    out = []
    i = 0.0
    while i < n:
        chunk = series[int(i):int(i + bucket) or int(i) + 1]
        if chunk:
            out.append(round(max(chunk), 2))
        i += bucket
    return out

def get_benchmark_data(appid):
    """Find before/after MangoHud logs for a game and return analysis.
    Logs older than the last compile = 'before'; newer = 'after'."""
    folder = os.path.join(BENCHMARK_DIR, str(appid))
    result = {
        'has_mangohud': mangohud_available(),
        'launch_string': benchmark_launch_string(appid),
        'folder': folder,
        'before': None,
        'after': None,
        'before_graph': None,
        'after_graph': None,
        'improvement_pct': None,
        'log_count': 0,
        'diag': [],          # human-readable notes about what happened
    }
    diag = result['diag']

    if not os.path.isdir(folder):
        diag.append(f"No benchmark folder yet at {folder}. No logs have been written.")
        return result

    # Collect ALL csv files (recursively, in case MangoHud nested them).
    logs = []
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if fn.lower().endswith('.csv'):
                fp = os.path.join(root, fn)
                try:
                    logs.append((fp, os.path.getmtime(fp)))
                except Exception:
                    continue
    result['log_count'] = len(logs)
    if not logs:
        diag.append(f"Folder exists but contains no .csv logs. "
                    f"Check the game actually launched with the MangoHud launch option, "
                    f"and that MangoHud is installed.")
        return result

    # Parse every log up front so we can report which ones are usable.
    parsed_logs = []   # (path, mtime, frametimes or None)
    for fp, mt in sorted(logs, key=lambda x: x[1]):
        p = _parse_mangohud_csv(fp)
        parsed_logs.append((fp, mt, p['frametimes_ms'] if p else None))
        if p is None:
            diag.append(f"Couldn't parse {os.path.basename(fp)} "
                        f"(too short, or no frametime data — play longer).")

    usable = [(fp, mt, ft) for (fp, mt, ft) in parsed_logs if ft]
    if not usable:
        diag.append("Found logs but none had enough frametime data to analyse. "
                    "Play at least ~30 seconds with the overlay active.")
        return result

    split = last_compile_time.get(str(appid), 0)

    if split:
        before = [u for u in usable if u[1] < split]
        after  = [u for u in usable if u[1] >= split]
        diag.append(f"Using compile time to split: "
                    f"{len(before)} before, {len(after)} after.")
    else:
        before, after = [], []
        diag.append("No compile timestamp recorded for this game yet.")

    # Fallbacks: if the split left one side empty but we have >=2 usable logs,
    # treat oldest as before and newest as after.
    if (not before or not after) and len(usable) >= 2:
        before = [usable[0]]
        after = [usable[-1]]
        diag.append("Split incomplete — using oldest log as 'before' and newest as 'after'.")
    elif len(usable) == 1:
        # Only one run so far. Assign it to the side implied by the compile time.
        only = usable[0]
        if split and only[1] >= split:
            after = [only]; before = []
            diag.append("Only an 'after' run so far — play once more BEFORE compiling for a comparison.")
        else:
            before = [only]; after = []
            diag.append("Only a 'before' run so far — compile, then play again to capture 'after'.")

    def analyse(u):
        if not u:
            return None, None
        fp, _mt, ft = u[-1]
        return _analyse_frametimes(ft), _downsample(ft)

    b_an, b_graph = analyse(before)
    a_an, a_graph = analyse(after)
    result['before'] = b_an
    result['after'] = a_an
    result['before_graph'] = b_graph
    result['after_graph'] = a_graph

    if b_an and a_an and b_an['low1_fps'] > 0:
        imp = 100.0 * (a_an['low1_fps'] - b_an['low1_fps']) / b_an['low1_fps']
        result['improvement_pct'] = round(imp, 1)
        diag.append("Comparison ready.")

    return result

# Parsing Steam appmanifest file
# Keywords that identify non-game Steam entries (Proton, runtimes, tools, etc.)
# Hardcoded appids that are always non-game Steam tools/runtimes
_SKIP_APPIDS = {
    '228980',  # Steamworks Common Redistributables
    '1070560', # Steam Linux Runtime
    '1391110', # Steam Linux Runtime - Soldier
    '1628350', # Steam Linux Runtime - Sniper
    '2180100', # Steam Linux Runtime - Medic
    '4183110', # Steam Linux Runtime - Scout
    '1493710', # Proton Experimental
    '2348590', # Proton 8.0
    '2805730', # Proton 9.0
    '3175060', # Proton 10.0
    '3658110', # Proton 10.0 (beta)
    '4628710', # Proton (latest)
    '1887720', # Proton 7.0
    '961940',  # Proton 3.16
    '1054830', # Proton 3.7
    '1113280', # Proton 4.2
    '1245040', # Proton 5.0
    '1420170', # Proton 6.3
    '2456610', # Proton 7.0-6
    '1161040', # Proton EAC Runtime
    '1games',  # placeholder never matched
}

_SKIP_NAME_PATTERNS = re.compile(
    r'^(proton|steam linux runtime|steamworks|pressure vessel|'
    r'steam client|steam sdk|dedicated server)',
    re.IGNORECASE
)

# Secondary pattern for anything containing these anywhere in the name
_SKIP_NAME_CONTAINS = re.compile(
    r'(linux runtime|sniper runtime|soldier runtime|scout runtime|'
    r'medic runtime|eac runtime|battleye runtime|anti-cheat)',
    re.IGNORECASE
)

def parse_acf(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        appid_match = re.search(r'"appid"\s+"(\d+)"', content)
        name_match  = re.search(r'"name"\s+"([^"]+)"', content)
        type_match  = re.search(r'"type"\s+"([^"]+)"', content)
        if appid_match and name_match:
            name     = name_match.group(1)
            app_type = (type_match.group(1) if type_match else 'game').lower()
            # Skip tools, config entries, and anything matching non-game patterns
            if app_type not in ('game', 'application'):
                return None
            if _SKIP_NAME_PATTERNS.search(name):
                return None
            if _SKIP_NAME_CONTAINS.search(name):
                return None
            return {
                'appid': appid_match.group(1),
                'name':  name,
            }
    except Exception as e:
        print(f"Error parsing {path}: {e}")
    return None

# Find fossilize_replay binary
def find_fossilize_replay():
    candidates = [
        os.path.join(STEAM_PATH, 'ubuntu12_64', 'fossilize_replay'),
        os.path.join(STEAM_PATH, 'steamrt64', 'fossilize_replay'),
        os.path.join(STEAM_PATH, 'ubuntu12_32', 'fossilize_replay'),
        os.path.join(STEAM_PATH, 'steamrt32', 'fossilize_replay'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def load_config():
    global steamgriddb_api_key, selected_gpu_index, auto_update_enabled, last_compile_time
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
            steamgriddb_api_key = cfg.get('steamgriddb_api_key', '') or cfg.get('steam_api_key', '')
            saved_gpu = cfg.get('selected_gpu_index')
            if saved_gpu is not None:
                selected_gpu_index = saved_gpu
            auto_update_enabled = bool(cfg.get('auto_update_enabled', False))
            last_compile_time = cfg.get('last_compile_time', {}) or {}
    except Exception as e:
        print(f"Config load error: {e}")

def save_config():
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump({
                'steamgriddb_api_key': steamgriddb_api_key,
                'selected_gpu_index': selected_gpu_index,
                'auto_update_enabled': auto_update_enabled,
                'last_compile_time': last_compile_time,
            }, f, indent=2)
    except Exception as e:
        print(f"Config save error: {e}")

# Scan Steam directories
def scan_steam():
    global games_data
    log(f"Scanning Steam directory: {STEAM_PATH}")
    library_folders = [STEAM_PATH]
    
    lib_vdf = os.path.join(STEAM_PATH, 'steamapps', 'libraryfolders.vdf')
    if os.path.exists(lib_vdf):
        try:
            with open(lib_vdf, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            paths = re.findall(r'"path"\s+"([^"]+)"', content)
            for p in paths:
                if os.path.exists(p) and p not in library_folders:
                    library_folders.append(p)
        except Exception as e:
            log(f"Error reading libraryfolders.vdf: {e}")
            
    scanned_games = {}
    for lib in library_folders:
        steamapps_dir = os.path.join(lib, 'steamapps')
        if not os.path.exists(steamapps_dir):
            continue
            
        for file in os.listdir(steamapps_dir):
            if file.endswith('.acf') and file.startswith('appmanifest_'):
                acf_path = os.path.join(steamapps_dir, file)
                game_info = parse_acf(acf_path)
                if game_info:
                    appid = game_info['appid']
                    # Skip known non-game appids
                    if appid in _SKIP_APPIDS:
                        continue

                    shader_dir = os.path.join(lib, 'steamapps', 'shadercache', appid)
                    foz_files = []
                    cache_size = 0
                    
                    if os.path.exists(shader_dir):
                        cache_size = get_dir_size(shader_dir)
                        for root, dirs, files in os.walk(shader_dir):
                            if 'fozpipelines' in root:
                                for f in files:
                                    if f.endswith('.foz') and 'whitelist' not in f.lower():
                                        foz_files.append(os.path.join(root, f))
                                        
                    scanned_games[appid] = {
                        'appid': appid,
                        'name': game_info['name'],
                        'library': lib,
                        'foz_files': foz_files,
                        'cache_size': cache_size,
                        'status': 'idle' if len(foz_files) > 0 else 'no_shaders'
                    }
                    
    with state_lock:
        games_data = scanned_games

    global total_cache_bytes
    total_cache_bytes = sum(g['cache_size'] for g in scanned_games.values())

# Clear specific game shader cache
def clear_game_cache(appid):
    global is_compiling, current_game_appid
    if is_compiling and current_game_appid == appid:
        return False, "Cannot clear cache of currently compiling game."
    
    game = games_data.get(appid)
    if not game:
        return False, "Game not found."
        
    shader_dir = os.path.join(game['library'], 'steamapps', 'shadercache', appid)
    if os.path.exists(shader_dir):
        try:
            shutil.rmtree(shader_dir)
            os.makedirs(shader_dir, exist_ok=True)
            log(f"Cleared shader cache directory for {game['name']} ({appid})")
            
            # Refresh this game's entry in our dictionary
            game['cache_size'] = 0
            game['foz_files'] = []
            game['status'] = 'no_shaders'
            return True, f"Successfully cleared shader cache for {game['name']}."
        except Exception as e:
            log(f"Error clearing cache for {appid}: {e}")
            return False, f"Error clearing cache: {e}"
    else:
        return True, "No shader cache folder existed."

# Run a shader compilation job (Background Thread)
def run_compile_job(target_appids=None):
    global is_compiling, current_game_appid, current_file_path, current_progress
    global overall_progress, progress_detail, total_files, remaining_files, running_process
    global cancel_requested, total_games_in_queue, completed_games_count, cross_game_progress
    global last_run_stats, total_cache_bytes

    fossilize_bin = find_fossilize_replay()
    if not fossilize_bin:
        log("[ERROR] fossilize_replay binary not found in Steam folders.")
        return

    log(f"Using fossilize_replay binary: {fossilize_bin}")

    # Select which games to compile
    compile_queue = []
    if target_appids:
        for appid in target_appids:
            if appid in games_data and len(games_data[appid]['foz_files']) > 0:
                compile_queue.append(appid)
    else:
        # Compile all games that have shaders
        for appid, game in games_data.items():
            if len(game['foz_files']) > 0:
                compile_queue.append(appid)

    if not compile_queue:
        log("No games with shader cache files found in queue.")
        return

    is_compiling = True
    cancel_requested = False
    total_games_in_queue = len(compile_queue)
    completed_games_count = 0
    cross_game_progress = 0.0

    # Capture total cache size before this run, for the before/after stat.
    before_bytes = 0
    for appid in compile_queue:
        g = games_data[appid]
        sd = os.path.join(g['library'], 'steamapps', 'shadercache', appid)
        before_bytes += get_dir_size(sd) if os.path.exists(sd) else 0
    last_run_stats = {
        'before_bytes': before_bytes,
        'after_bytes': 0,
        'gained_bytes': 0,
        'games_compiled': 0,
        'finished_at': 0,
        'in_progress': True,
    }

    # Mark queued games as pending
    for appid in compile_queue:
        games_data[appid]['status'] = 'pending'

    for index, appid in enumerate(compile_queue):
        if cancel_requested:
            break
            
        game = games_data[appid]
        game['status'] = 'compiling'
        current_game_appid = appid
        
        log(f"Starting compile for game: {game['name']} ({appid})")
        gpu_idx = selected_gpu_index if selected_gpu_index is not None else 0
        gpu_name = gpu_list[gpu_idx]['name'] if gpu_list and gpu_idx < len(gpu_list) else f"GPU {gpu_idx}"
        log(f"Using GPU index {gpu_idx}: {gpu_name}")
        
        # Configure env variables for NVIDIA/AMD shader output path
        shader_dir = os.path.join(game['library'], 'steamapps', 'shadercache', appid)
        os.makedirs(shader_dir, exist_ok=True)
        
        env = os.environ.copy()
        env['__GL_SHADER_DISK_CACHE'] = '1'
        env['__GL_SHADER_DISK_CACHE_PATH'] = shader_dir
        env['__GL_SHADER_DISK_CACHE_SKIP_CLEANUP'] = '1'
        env['MESA_GLSL_CACHE_DIR'] = shader_dir
        
        foz_files = game['foz_files']
        total_files = len(foz_files)
        remaining_files = total_files
        
        for file_idx, foz_file in enumerate(foz_files):
            if cancel_requested:
                break
                
            current_file_path = foz_file
            current_progress = 0.0
            progress_detail = "Starting..."
            overall_progress = ((file_idx + (current_progress / 100.0)) / total_files) * 100.0
            
            filename = os.path.basename(foz_file)
            log(f"Replaying database [{file_idx+1}/{total_files}]: {filename}")
            
            # Compile command
            gpu_idx = selected_gpu_index if selected_gpu_index is not None else 0
            cmd = [fossilize_bin, '--num-threads', str(thread_count), '--device-index', str(gpu_idx), '--progress', foz_file]
            
            try:
                running_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env
                )
                
                # Parse output logs
                for line in running_process.stdout:
                    line = line.strip()
                    # Check for progress messages
                    match = re.search(r"Overall\s+(\d+)\s+/\s+(\d+)", line)
                    if match:
                        x = int(match.group(1))
                        y = int(match.group(2))
                        if y > 0:
                            current_progress = (x / y) * 100.0
                            progress_detail = f"Replayed: {x} / {y} shaders"
                            overall_progress = ((file_idx + (current_progress / 100.0)) / total_files) * 100.0
                    
                    # Print interesting milestones to frontend console log
                    if "Replayed" in line or "succeeded" in line or "failed" in line or "evicted" in line:
                        log(f"[{game['name']}] {line}")
                        
                running_process.wait()
                
                # Check exit status
                if running_process.returncode == 0:
                    log(f"Database {filename} replayed successfully.")
                else:
                    log(f"[WARNING] Database {filename} exited with code {running_process.returncode}")
                    
            except Exception as e:
                log(f"[ERROR] Failed running fossilize_replay for {filename}: {e}")
            finally:
                running_process = None
                remaining_files = total_files - (file_idx + 1)
                
        # Finalize game status
        if cancel_requested:
            game['status'] = 'idle'
            log(f"Compilation for {game['name']} was cancelled.")
        else:
            game['status'] = 'completed'
            # Refresh directory size to show compiled size
            game['cache_size'] = get_dir_size(shader_dir)
            completed_games_count += 1
            cross_game_progress = (completed_games_count / total_games_in_queue) * 100.0
            last_compile_time[str(appid)] = time.time()
            log(f"[SUCCESS] Shader pre-compilation completed for {game['name']}")

    # Finalize global state
    for appid in compile_queue:
        if games_data[appid]['status'] == 'pending':
            games_data[appid]['status'] = 'idle'

    is_compiling = False
    current_game_appid = None
    current_file_path = None
    current_progress = 0.0
    overall_progress = 0.0
    cross_game_progress = 0.0
    progress_detail = ""
    total_files = 0
    remaining_files = 0
    total_games_in_queue = 0
    completed_games_count = 0

    # Compute before/after cache stats for the headline number.
    after_bytes = 0
    for appid in compile_queue:
        g = games_data[appid]
        sd = os.path.join(g['library'], 'steamapps', 'shadercache', appid)
        after_bytes += get_dir_size(sd) if os.path.exists(sd) else 0
    gained = max(0, after_bytes - last_run_stats.get('before_bytes', 0))
    last_run_stats = {
        'before_bytes': last_run_stats.get('before_bytes', 0),
        'after_bytes': after_bytes,
        'gained_bytes': gained,
        'games_compiled': len(compile_queue),
        'finished_at': time.time(),
        'in_progress': False,
    }
    total_cache_bytes = sum(g['cache_size'] for g in games_data.values())

    def _fmt(b):
        gb = b / (1024**3)
        if gb >= 1:
            return f"{gb:.1f} GB"
        return f"{b / (1024**2):.0f} MB"
    log(f"[SUCCESS] Compiled {_fmt(gained)} of new shader pipelines "
        f"({_fmt(last_run_stats['before_bytes'])} -> {_fmt(after_bytes)}).")
    log("Shader compile job queue finished.")
    save_config()  # persist last_compile_time for benchmark before/after

# HTTP Request Handler
class ShaderCompilerHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silence standard requests output to prevent cluttering console

    def do_GET(self):
        global last_ping_time
        
        # Route path
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            with open(os.path.join(PROJECT_DIR, 'index.html'), 'rb') as f:
                self.wfile.write(f.read())
                
        elif self.path == '/index.css':
            self.send_response(200)
            self.send_header('Content-Type', 'text/css')
            self.end_headers()
            css_path = os.path.join(PROJECT_DIR, 'index.css')
            if os.path.exists(css_path):
                with open(css_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b'/* CSS is inline in index.html */')
                
        elif self.path == '/icon.jpg':
            icon_path = os.path.join(PROJECT_DIR, 'icon.jpg')
            if os.path.exists(icon_path):
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.end_headers()
                with open(icon_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)
                

        elif self.path.startswith('/api/get_art'):
            import urllib.request
            import urllib.parse
            qs = urllib.parse.urlparse(self.path).query
            params_qs = dict(urllib.parse.parse_qsl(qs))
            appid = params_qs.get('appid', '')
            if not appid:
                self.send_response(204)
                self.end_headers()
                return
            try:
                art_url = ''
                if steamgriddb_api_key:
                    try:
                        url = (f"https://www.steamgriddb.com/api/v2/grids/steam/{appid}"
                               f"?dimensions=920x430&nsfw=false")
                        req = urllib.request.Request(url, headers={
                            'Authorization': f'Bearer {steamgriddb_api_key}',
                            'User-Agent': 'stutterless/1.0',
                        })
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            data = json.loads(resp.read())
                        if data.get('success') and data.get('data'):
                            art_url = data['data'][0].get('url', '')
                    except Exception:
                        pass  # not in SteamGridDB — fall through to store API
                if not art_url:
                    # Steam store API — free, no key, covers betas and all appids
                    url = (f"https://store.steampowered.com/api/appdetails"
                           f"?appids={appid}&filters=basic")
                    req = urllib.request.Request(
                        url, headers={'User-Agent': 'stutterless/1.0'})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read())
                    appid_str = str(appid)
                    if data.get(appid_str, {}).get('success'):
                        art_url = data[appid_str]['data'].get('header_image', '')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'art_url': art_url}).encode())
            except Exception:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'art_url': ''}).encode())

        elif self.path.startswith('/api/benchmark'):
            import urllib.parse
            qs = urllib.parse.urlparse(self.path).query
            params_qs = dict(urllib.parse.parse_qsl(qs))
            appid = params_qs.get('appid', '')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if not appid:
                self.wfile.write(json.dumps({'error': 'no appid'}).encode())
                return
            try:
                data = get_benchmark_data(appid)
            except Exception as e:
                data = {'error': str(e)}
            self.wfile.write(json.dumps(data).encode())

        elif self.path == '/api/ping':
            last_ping_time = time.time()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            
        elif self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            with state_lock:
                games_list = []
                for appid, game in games_data.items():
                    games_list.append({
                        'appid': appid,
                        'name': game['name'],
                        'foz_files': game['foz_files'],
                        'status': game['status'],
                        'cache_size': game['cache_size']
                    })
                    
                response = {
                    'is_compiling': is_compiling,
                    'thread_count': thread_count,
                    'max_threads': max_threads,
                    'games': games_list,
                    'current_game': current_game_appid,
                    'current_file': current_file_path,
                    'current_progress': current_progress,
                    'overall_progress': overall_progress,
                    'cross_game_progress': cross_game_progress,
                    'total_games_in_queue': total_games_in_queue,
                    'completed_games_count': completed_games_count,
                    'progress_detail': progress_detail,
                    'total_files': total_files,
                    'remaining_files': remaining_files,
                    'cpu_usage': current_cpu_usage,
                    'log_buffer': list(log_buffer),
                    'gpu_list': gpu_list,
                    'selected_gpu_index': selected_gpu_index,
                    'has_api_key': bool(steamgriddb_api_key),
                    'auto_update_enabled': auto_update_enabled,
                    'on_ac_power': on_ac_power(),
                    'total_cache_bytes': total_cache_bytes,
                    'last_run_stats': last_run_stats,
                }
            self.wfile.write(json.dumps(response).encode('utf-8'))
        else:
            self.send_error(404)

    def do_POST(self):
        global thread_count, cancel_requested
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ""
        
        try:
            params = json.loads(body) if body else {}
        except Exception:
            params = {}
            
        if self.path == '/api/start':
            if is_compiling:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error": "Compilation already running"}')
                return

            # Accept appids array (from UI selection) or legacy single appid
            appids = params.get('appids')
            if not appids:
                single = params.get('appid')
                appids = [single] if single else None

            # Run compile job in background thread
            threading.Thread(target=run_compile_job, args=(appids,)).start()
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "started"}')
            
        elif self.path == '/api/stop':
            cancel_requested = True
            if running_process:
                try:
                    running_process.terminate()
                except Exception as e:
                    log(f"Error terminating process: {e}")
            log("Stop requested by user. Terminating active compile...")
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "stopping"}')
            
        elif self.path == '/api/set_threads':
            threads = params.get('threads', thread_count)
            threads = max(1, min(max_threads, int(threads)))
            thread_count = threads
            log(f"Thread count updated to: {thread_count}")
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "updated"}')

        elif self.path == '/api/set_gpu':
            global selected_gpu_index
            idx = params.get('gpu_index')
            if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(gpu_list):
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error": "Invalid gpu_index"}')
                return
            selected_gpu_index = idx
            gpu_name = gpu_list[idx]['name'] if idx < len(gpu_list) else str(idx)
            log(f"GPU selection updated to index {idx}: {gpu_name}")
            save_config()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'updated', 'gpu_index': idx, 'gpu_name': gpu_name}).encode())
            

        elif self.path == '/api/set_steam_key' or self.path == '/api/set_api_key':
            global steamgriddb_api_key
            key = params.get('key', '').strip()
            steamgriddb_api_key = key
            save_config()
            log(f"SteamGridDB API key {'set' if key else 'cleared'}")
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'has_key': bool(key)}).encode())

        elif self.path == '/api/set_auto_update':
            enabled = bool(params.get('enabled', False))
            ok, msg = set_auto_update(enabled)
            self.send_response(200 if ok else 500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'success': ok,
                'message': msg,
                'auto_update_enabled': auto_update_enabled,
            }).encode())

        elif self.path == '/api/clear_cache':
            appid = params.get('appid')
            if not appid or appid not in games_data:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error": "Invalid appid"}')
                return
                
            success, msg = clear_game_cache(appid)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': success, 'message': msg}).encode('utf-8'))
            
        else:
            self.send_error(404)

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def find_free_port(start_port=8543):
    port = start_port
    while port < start_port + 100:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except socket.error:
                port += 1
    return 0

if __name__ == '__main__':
    # Load saved config
    load_config()
    # Detect GPUs first
    detect_gpus()

    # Initial scan of steam library
    scan_steam()

    # Reconcile saved auto-update flag with the actual systemd timer state.
    auto_update_enabled = timer_is_active()

    # ── Headless mode (used by the systemd timer / auto-update) ──
    if HEADLESS:
        # Only run on AC power — never drain a laptop battery on a big compile.
        if not on_ac_power():
            log("Headless run skipped: not on AC power.")
            sys.exit(0)
        log("Headless run starting (AC power confirmed).")
        # Compile all games that have recorded shaders; run synchronously.
        run_compile_job(None)
        log("Headless run complete.")
        sys.exit(0)

    # Start CPU monitor
    threading.Thread(target=cpu_monitor_loop, daemon=True).start()
    
    # Start keep alive monitor
    threading.Thread(target=check_keep_alive, daemon=True).start()
    
    # Find free port
    port = find_free_port()
    if port == 0:
        print("[ERROR] Could not find a free port for the server.")
        sys.exit(1)
        
    url = f"http://127.0.0.1:{port}"
    log(f"Server starting on {url}")
    
    # Launch browser
    webbrowser.open(url)
    
    # Run server
    server_address = ('127.0.0.1', port)
    httpd = ThreadingHTTPServer(server_address, ShaderCompilerHTTPHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("Shutting down server...")
        if running_process:
            try:
                running_process.terminate()
            except Exception:
                pass
