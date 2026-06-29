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
CONFIG_FILE = os.path.expanduser('~/.config/steam-shader-compiler/config.json')

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
    global steamgriddb_api_key, selected_gpu_index
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
            steamgriddb_api_key = cfg.get('steamgriddb_api_key', '') or cfg.get('steam_api_key', '')
            saved_gpu = cfg.get('selected_gpu_index')
            if saved_gpu is not None:
                selected_gpu_index = saved_gpu
    except Exception as e:
        print(f"Config load error: {e}")

def save_config():
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump({
                'steamgriddb_api_key': steamgriddb_api_key,
                'selected_gpu_index': selected_gpu_index,
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
    log("Shader compile job queue finished.")

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
                            'User-Agent': 'steam-shader-compiler/1.0',
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
                        url, headers={'User-Agent': 'steam-shader-compiler/1.0'})
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
