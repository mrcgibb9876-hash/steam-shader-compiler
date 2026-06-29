# Shader Pre-Cache

**Pre-compile Vulkan pipeline shaders for your Steam games — eliminate stutter before you ever launch a game.**

![Platform](https://img.shields.io/badge/platform-Linux-blue?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![Built with](https://img.shields.io/badge/built%20with-Python%20%2B%20Vulkan-purple?style=flat-square)

---

## Why does this exist?

When you play a game on Linux through Proton, Vulkan needs to compile *pipeline state objects* — essentially GPU programs — the first time each shader combination is encountered. Steam records these encounters in `.foz` pipeline cache files while you play.

**Steam's built-in shader pre-compilation only runs when:**
- You download or update a game
- Valve has uploaded a pre-built shader pack for your exact GPU family
- Your driver version matches what Valve compiled against

In practice this means:
- Freshly installed games stutter badly during the first few hours of play
- After a driver update, the compiled cache is invalidated and stutter returns
- If Valve hasn't uploaded a pack for your GPU, you get nothing
- The pre-compilation Steam does run is against a *generic* target, not your actual hardware

**This tool does something different.** It takes the `.foz` files Steam recorded during *your* real sessions — shaders your GPU actually encountered — and replays them through `fossilize_replay` directly on your hardware, producing a fully compiled pipeline cache tailored to your specific GPU and driver. The next time you launch the game, those pipelines load from the cache instantly: **zero compile stutter**.

---

## How it compares

| | Steam built-in | This tool |
|---|---|---|
| Compiled against your actual GPU | ✗ generic target | ✓ your exact hardware |
| Runs after driver updates | ✗ | ✓ re-run anytime |
| Covers shaders you've actually seen | ✗ Valve's selection | ✓ your recorded sessions |
| Runs while you're at the desktop | ✗ only on game launch | ✓ runs offline, any time |
| Parallel compilation control | ✗ | ✓ choose thread count |
| Per-game or all-games | ✗ all or nothing | ✓ select individual games |

---

## Features

- **Scans all Steam libraries** automatically, finds every game with a shader pipeline cache
- **Compiles on your GPU** using the `fossilize_replay` binary already bundled with Steam — no extra tools needed
- **Multi-threaded** with a slider to control CPU thread count
- **Multi-GPU aware** — select which GPU to compile for (great for hybrid laptop/desktop setups)
- **Per-game selection** — compile one game or all of them at once
- **Live progress** — per-file and cross-game progress bars, real-time fossilize output log
- **Game artwork** — shows header images automatically from Steam's CDN (no API key needed); optionally use [SteamGridDB](https://www.steamgriddb.com) for HD community artwork
- **Compiled / Pending badges** — see at a glance which games have been pre-compiled this session
- **Cache management** — clear a game's shader cache with one click to force a full recompile
- **Self-contained binary** — one file, no Python runtime required after install

---

## Installation

```bash
git clone https://github.com/yourusername/steam-shader-compiler
cd steam-shader-compiler
chmod +x install.sh
./install.sh
```

The installer will:
1. Build a self-contained binary with PyInstaller
2. Install it to `~/.local/share/steam-shader-compiler/`
3. Create a desktop entry so it appears in your app launcher
4. Optionally set up a systemd timer to run automatically every 6 hours

**Requirements:**
- Linux — Arch, CachyOS, SteamOS, Ubuntu, Fedora, or any distro with Steam
- Steam installed at `~/.local/share/Steam` or `~/.steam/steam`
- Python 3.8+ (only needed to build; the installed binary is self-contained)
- A Vulkan-capable GPU

---

## Usage

Launch **Shader Pre-Cache** from your application menu, or run:

```bash
steam-shader-compiler
```

The app opens in your browser at `http://127.0.0.1:8543`.

1. Your games appear automatically — games with shader caches show a **Pending** badge
2. Select the games you want to compile (all are selected by default)
3. Choose your GPU and thread count in the sidebar
4. Click **Compile Selected**
5. Watch the live progress log — each game's pipelines are replayed and compiled
6. Games show a **Compiled** badge when done — launch and play stutter-free

---

## Artwork

Game header images load automatically from Steam's CDN — **no API key required**.

For higher-quality community artwork, you can optionally add a free [SteamGridDB](https://www.steamgriddb.com/profile/preferences/api) API key in the sidebar. SteamGridDB serves 920×430 HD grid images sourced from the community and covers a wider range of titles.

---

## How it works under the hood

Steam's Proton records every unique Vulkan pipeline state encountered during gameplay into `.foz` files inside:

```
~/.local/share/Steam/steamapps/shadercache/<appid>/fozpipelinesv6/
```

This tool finds all those files, then runs Steam's own `fossilize_replay` binary:

```
fossilize_replay --num-threads <N> --device-index <GPU> --progress <file.foz>
```

`fossilize_replay` replays each recorded pipeline against the real Vulkan driver on your machine, populating the on-disk pipeline cache. When the game launches next, the Vulkan driver finds those compiled pipelines in cache and loads them instantly rather than compiling on-demand mid-frame.

The result is the same as if you had played through every scene of the game already, from a shader-compilation perspective — without actually having to do it.

---

## Project files

| File | Purpose |
|------|---------|
| `steam_shader_compiler.py` | Backend HTTP server + fossilize runner |
| `index.html` | Web UI (served by the backend) |
| `steam-shader-compiler.spec` | PyInstaller build spec |
| `install.sh` | Build and install script |
| `uninstall.sh` | Uninstall script |

---

## FAQ

**Do I need to run this more than once?**
Run it again after a GPU driver update (which invalidates old pipeline caches), after installing a new game, or whenever you notice stutter returning.

**Will it hurt performance if the cache is already compiled?**
No. `fossilize_replay` skips pipelines already in the cache. Subsequent runs are very fast.

**What about the shader caches Steam downloads automatically?**
Those are Valve's pre-built packs targeted at popular GPU families. This tool *complements* them — it compiles the shaders you've actually encountered, on your actual hardware, which Steam's packs may not fully cover.

**Is it safe to run while Steam is open?**
Yes. The `.foz` source files are only read, never modified. The compiled pipeline cache is written atomically by the Vulkan driver.

**Why not just use the `shader_pre_caching` option in Steam?**
Steam's pre-caching only runs on game launch and download, compiles against a generic GPU target, and can't be triggered manually. This tool gives you full control — run it on demand, pick your GPU, choose your thread count, and compile only the games you want.

---

## Uninstall

```bash
./uninstall.sh
```

---

## Credits

Built on top of [Fossilize](https://github.com/ValveSoftware/Fossilize) by Valve Software, which is already bundled inside your Steam installation at `~/.local/share/Steam/ubuntu12_64/fossilize_replay`.
