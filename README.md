# homelab-media-stack

A self-hosted home media server built on Docker and Linux. The project bundles several independent parts that share the same machine and media drive. Some form a pipeline, while others are standalone services. Use only the parts you need.

## Overview

### 1. Automated media pipeline

The core of the project: from a web page to a playable title in Jellyfin, with no manual file handling.

```
 MejorTorrent page URL ──► Scraper ──► qBittorrent ──► torrents/complete/
                                                               │
                                             AI organizer      │ every 30 min
                                                               ▼
                                          movies/  and  tv/ ──► Jellyfin
```

- **Scraper** (Python, Flask) — takes the URL of a movie or TV show page on *MejorTorrent*, extracts its `.torrent` files and magnet links, and sends them to qBittorrent, even when the site is behind Cloudflare. It is designed and tested for MejorTorrent, and also attempts to extract links from similar sites. It relies on the `cloudflare-bypass` container.
- **qBittorrent** — downloads the torrents into `torrents/complete/`.
- **AI organizer** (Python) — every 30 minutes it scans completed downloads, extracts archives where needed, asks a free LLM on [OpenRouter](https://openrouter.ai) to identify each movie or show, and moves the files into the folder structure expected by Jellyfin.
- **[Jellyfin](https://jellyfin.org)** — serves the organized library to any device: browser, phone app, smart TV.

### 2. aMule (eD2K / Kad network)

A standalone aMule daemon that runs on the server around the clock, so the eMule network can be used without leaving a desktop PC switched on. It listens for remote connections. From any other computer on your local network, open the aMule GUI (*aMule Remote GUI*, `amulegui`) and point it at the server's LAN IP on port **4712** with `AMULE_GUI_PASSWORD`, or use the web interface at `http://SERVER_IP:4711`. Searches and downloads are managed as if aMule were running locally, but the work is done on the server.

Finished aMule downloads land in the same `torrents/complete/` folder, so the AI organizer also files them into the Jellyfin library.

### 3. Remote access from your phone

Lets you queue downloads and watch your library while away from home.

- **WireGuard** — a VPN that places your phone on the home network wherever you are. Once connected, you can send a MejorTorrent page to the scraper (`http://SERVER_IP:5000/add_page?url=...`) or add torrents directly in qBittorrent's web UI, and the download starts on the server at home. Services that should never be exposed to the internet, such as the scraper, are reachable this way.
- **Caddy** — an HTTPS reverse proxy that publishes the qBittorrent and Jellyfin web interfaces on your own domain (`https://DOMAIN/qbittorrent/`, `https://DOMAIN/jellyfin/`) with automatic Let's Encrypt certificates. No VPN is required for these.
- **ddclient** and `scripts/dynv6.sh` — keep your dynamic DNS hostname pointed at your home's public IP, which may change, so that both WireGuard and Caddy remain reachable.

### 4. Network file share

- **Samba** — exposes the entire media drive as a network folder (`\\SERVER_IP\share`) so it can be browsed, copied to or cleaned up from any Windows, macOS or Linux computer on the local network.

### 5. Host maintenance

- **`scripts/start.sh`** — at boot, waits for the media drive to be mounted before starting the containers, so services never start against an empty mount point.
- **`scripts/optimize.sh`** — optional tuning for a dedicated low-power server (CPU governor, network buffers, NIC power management, hard-drive spin-down). Not needed on most systems; see [System tuning script](#system-tuning-script-scriptsoptimizesh).
- **cron** — schedules all of the above (see [step 9](#9-schedule-everything-with-cron)).

## Services and ports

| Service | Purpose | Port(s) |
|---|---|---|
| **Jellyfin** | Media server for movies and TV shows (web, mobile, TV) | 8096 |
| **qBittorrent** | BitTorrent client with web UI | 8080, 6881 |
| **aMule** | eD2K/Kademlia client | 4711 (web), 4662, 4672/udp |
| **Samba** | Exposes the media drive as a network share for Windows/macOS/Linux | 445 |
| **Caddy** | Reverse proxy with automatic HTTPS for remote access to qBittorrent and Jellyfin | 80, 443 |
| **WireGuard** | VPN for secure access to your home network | `WG_PORT`/udp |
| **ddclient** | Keeps your dynamic DNS hostname pointed at your public IP | — |
| **cloudflare-bypass** | [CloudflareBypassForScraping](https://github.com/sarperavci/CloudflareBypassForScraping), used by the scraper | 8000 (localhost only) |

Services are independent of each other; remove any you don't need from `docker-compose.yml`.

## Repository layout

```
.
├── docker-compose.yml        # All containers (configured through .env)
├── .env.example              # Configuration template -> copy to .env
├── docker/
│   ├── caddy/Caddyfile       # HTTPS reverse proxy
│   ├── ddclient/             # ddclient.conf template
│   └── config/               # (auto-created) container data, not tracked by git
├── src/mediaserver/          # Python package
│   ├── config.py             #   loads .env and resolves paths
│   ├── scraper.py            #   Flask API: page -> torrents -> qBittorrent
│   ├── organizer.py          #   AI media organizer
│   └── client.py             #   command-line client for the scraper
├── scripts/
│   ├── start.sh              # Boot script: waits for the drive, runs docker compose up
│   ├── optimize.sh           # CPU, network and disk tuning (optional, requires sudo)
│   └── dynv6.sh              # Fallback dynv6 DNS updater
├── deploy/crontab.example    # Scheduled jobs
├── tests/                    # Test suite (pytest)
├── pyproject.toml
├── requirements.txt
└── requirements-dev.txt
```

The `data/` (organizer state) and `logs/` directories are created at runtime and are not tracked by git.

---

## Requirements

- A **Linux** machine running 24/7 (tested on Ubuntu; a mini PC or an old laptop is sufficient).
- A drive (internal or USB) for media storage.
- **Docker** with the **Docker Compose v2** plugin — see the [official installation guide](https://docs.docker.com/engine/install/).
- **Python 3.10 or later** with `venv`.
- System tools used by the scripts:

  ```bash
  sudo apt install git python3-venv unrar unzip p7zip-full curl dnsutils
  # optional, only for hard-disk tuning (see "Optional: keeping the server awake and tuned"):
  sudo apt install hdparm
  ```

- For remote access: the ability to forward ports on your router, and a free dynamic DNS hostname (this project uses [dynv6](https://dynv6.com), but any provider will work).
- For the organizer: a free [OpenRouter](https://openrouter.ai) account.

Add your user to the `docker` group so that `sudo` is not required (log out and back in afterwards):

```bash
sudo usermod -aG docker $USER
```

---

## Installation

The steps below are meant to be followed in order. Each one only depends on the ones before it.

### 1. Clone the repository

```bash
git clone https://github.com/hxn241/homelab-media-stack.git
cd homelab-media-stack
```

All commands in this guide are run **from the repository root**.

### 2. Prepare the media drive

**Mount the drive at boot.** Reference it by UUID rather than by device name (`/dev/sdb1` may change between boots, especially with USB drives). Find the UUID and filesystem type:

```bash
lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINT
```

Add a line to `/etc/fstab` (adjust the UUID, mount path and filesystem type):

```
UUID=1234abcd-...  /mnt/media  ext4  defaults,nofail  0  2
```

`nofail` lets the system boot normally if the drive is disconnected; `scripts/start.sh` waits for the mount point before starting the containers. Mount it and verify:

```bash
sudo mkdir -p /mnt/media
sudo mount -a && findmnt /mnt/media
```

**Create the folder structure** and make sure your user owns it:

```bash
MEDIA_ROOT=/mnt/media          # replace with your mount path
mkdir -p $MEDIA_ROOT/{movies,tv,torrents/complete,torrents/incomplete}
ls -ld $MEDIA_ROOT             # if your user is not the owner: sudo chown -R $USER:$USER $MEDIA_ROOT
```

| Folder | Contents |
|---|---|
| `torrents/incomplete/` | Downloads in progress |
| `torrents/complete/` | Finished downloads awaiting organization |
| `movies/` | Organized movies (`Title/Title.mkv`) |
| `tv/` | Organized TV shows (`Title/Season 01/S01E01.mkv`) |

> If the media drive is a mechanical hard disk (HDD), see [Keep the drive from spinning down](#keep-the-drive-from-spinning-down-hdd-only) once the installation is complete.

### 3. Set up external accounts and the router

Gather the values the `.env` file will need in the next step.

**Dynamic DNS (remote access).** Skip this if you only need access from your local network, and remove `caddy`, `wireguard` and `ddclient` from `docker-compose.yml`.

1. Create an account at [dynv6.com](https://dynv6.com) and a zone, e.g. `myserver.dynv6.net`.
2. Copy your token from *Keys → HTTP Tokens*.
3. Create the ddclient configuration from the template and fill in your token and hostname:

   ```bash
   mkdir -p docker/config/ddclient
   cp docker/ddclient/ddclient.conf.example docker/config/ddclient/ddclient.conf
   chmod 600 docker/config/ddclient/ddclient.conf
   nano docker/config/ddclient/ddclient.conf
   ```

If you use a different dynamic DNS provider (DuckDNS, No-IP, etc.), adapt `ddclient.conf` according to the [ddclient documentation](https://ddclient.net) and do not use `scripts/dynv6.sh`.

**OpenRouter (AI organizer).** Create an account at [openrouter.ai](https://openrouter.ai) and an API key at [openrouter.ai/keys](https://openrouter.ai/keys). The free models used by the organizer do not require credit.

**ntfy (optional).** To receive organizer failure alerts on your phone, install the [ntfy](https://ntfy.sh) app and subscribe to a long, hard-to-guess topic name.

**Router.** Give the server a fixed LAN IP (a DHCP reservation on your router) and forward these ports to it:

| Port | Protocol | Used by | Needed for |
|---|---|---|---|
| 80, 443 | TCP | Caddy | Remote HTTPS access and certificate issuance |
| `WG_PORT` (default 51820) | UDP | WireGuard | VPN from your phone |
| 6881 | TCP + UDP | qBittorrent | Better download speed (optional) |
| 4662 / 4672 | TCP / UDP | aMule | High ID on the eD2K network (optional) |

### 4. Create the `.env` file

All site-specific configuration (paths, domain, passwords, API keys) lives in a single `.env` file, which is **never committed to git**:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

At a minimum, set the following:

| Variable | Value |
|---|---|
| `PUID` / `PGID` | Your user and group IDs. Run `id` and copy the `uid=` and `gid=` values |
| `TZ` | Your time zone, e.g. `Europe/Madrid` |
| `MEDIA_ROOT` | The mount path from step 2 |
| `SERVER_IP` | The server's fixed LAN IP from step 3 (`hostname -I`) |
| `DOMAIN`, `DYNV6_ZONE` | Your dynamic DNS hostname from step 3 |
| `DYNV6_TOKEN` | Your dynv6 token from step 3 |
| `OPENROUTER_API_KEY` | Your OpenRouter API key from step 3 |
| `NTFY_TOPIC` | Your ntfy topic from step 3 (optional) |
| `AMULE_WEBUI_PASSWORD`, `AMULE_GUI_PASSWORD` | Choose passwords for aMule |
| `SAMBA_USER`, `SAMBA_PASSWORD` | Choose credentials for the network share |

Leave `QB_USER` / `QB_PASS` for now; they are set in step 5. Leave `OPENROUTER_MODEL` empty (see [How the model is chosen](#how-the-model-is-chosen)). All other variables are documented in `.env.example` and have sensible defaults; a complete reference is provided at the end of this document.

> **Format:** use `KEY=value` with no spaces around `=` and no trailing comments. Wrap values containing spaces or characters such as `$`, `;` or `#` in single quotes.

### 5. Start the containers

```bash
docker compose up -d
docker compose ps
```

If a required variable is missing from `.env`, `docker compose` will report which one. The first run takes a few minutes while the images are downloaded.

Initial setup for each service:

- **qBittorrent** — `http://SERVER_IP:8080`
  1. The temporary `admin` password is printed in `docker logs qbittorrent`.
  2. Under *Tools → Options → Web UI*, set your username and password, then copy them to `QB_USER` / `QB_PASS` in `.env`.
  3. Under *Downloads*, set the default save path to `/downloads` and the incomplete downloads path to `/incomplete`.
- **Jellyfin** — `http://SERVER_IP:8096`. Complete the setup wizard and create two libraries: *Movies* → `/media/movies` and *Shows* → `/media/tv`.
- **aMule** — web interface at `http://SERVER_IP:4711` (`AMULE_WEBUI_PASSWORD`), or *aMule Remote GUI* from another computer pointed at `SERVER_IP`, port 4712 (`AMULE_GUI_PASSWORD`).
- **Samba** — connect to `\\SERVER_IP\<SAMBA_SHARE>` (Windows) or `smb://SERVER_IP/<SAMBA_SHARE>` (macOS/Linux) using `SAMBA_USER` / `SAMBA_PASSWORD`. The entire `MEDIA_ROOT` is shared.
- **Caddy** — obtains a TLS certificate automatically once your hostname resolves to your public IP and ports 80/443 are reachable. Routes: `https://DOMAIN/qbittorrent/` and `https://DOMAIN/jellyfin/`. To change them, edit `docker/caddy/Caddyfile` and run `docker compose restart caddy`.
- **WireGuard** — generates `WG_PEERS` client configurations. On a phone, install the official WireGuard app and scan the QR code:

  ```bash
  docker exec wireguard /app/show-peer 1
  ```

### 6. Install the Python tools

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
```

`requirements.txt` installs the pinned dependencies along with the package itself, which provides three commands in the virtual environment:

- `mediaserver-scraper` — starts the scraper API
- `mediaserver-organize` — runs a single organizer pass
- `mediaserver-add` — submits a URL to the scraper from the command line

Chromium (via Playwright) is only required for the scraper's last-resort fallback on certain sites; everything else works without it.

### 7. Test the AI organizer

```bash
.venv/bin/mediaserver-organize
```

With nothing in `torrents/complete/` it simply reports that there is nothing to organize. Each pass performs the following steps:

1. Extracts `.zip` / `.rar` / `.7z` archives (including multi-part archives) found in `torrents/complete/`.
2. Lists folders and loose video files (`.mkv`, `.mp4`, `.avi`) and sends them to the LLM for identification.
3. Moves each movie to `movies/Title/Title.ext` and each episode to `tv/Title/Season NN/SNNENN.ext`.
4. Deletes leftover `.txt` / `.url` files and any empty folders.

> ⚠️ **Files are moved, not copied.** If an archive is password-protected or corrupt, **its entire folder is deleted**. If you continue seeding torrents, be aware that they will lose access to their files once organized.

State (model cache, last prompt and last response) is stored in `data/`.

#### How the model is chosen

OpenRouter's free models come and go frequently: models are added, rate-limited, or have their free tier retired without notice. Rather than depending on a single model, the organizer builds a list of candidates on every run and uses the first one that returns a valid answer:

1. **The last model that succeeded** (stored in `data/model_state.json`).
2. **`OPENROUTER_MODEL`**, if set.
3. **Free models discovered automatically.** Once a day the organizer downloads OpenRouter's model catalogue and keeps only models that are completely free, produce text, support chat/tool use and have at least 32K tokens of context. They are ranked by structured JSON support first, then context length, then release date. The list is cached in `data/free_models.json`.
4. **`OPENROUTER_FALLBACK`** (`openrouter/free` by default) as a last resort: an OpenRouter router that forwards the request to any free model currently available.

At most four candidates from steps 1–3 are tried before the fallback. A model that fails (HTTP error, empty or unparseable answer) is put on a **6-hour cooldown** and skipped until it expires. A model whose free tier is retired drops out of the discovered list at the next daily refresh.

**Recommendation: leave `OPENROUTER_MODEL` empty.** Pinning a model only makes sense if you have verified that it identifies your titles better than the rest. A pinned model is retried every 6 hours even after it stops working, and it never drops out on its own: if its free tier ends, the organizer still works (it moves on to the next candidate), but it wastes an attempt on every cycle until you clear the variable.

### 8. Test the scraper

```bash
.venv/bin/mediaserver-scraper
```

It listens on `http://0.0.0.0:5000` (configurable via `SCRAPER_HOST` / `SCRAPER_PORT`).

Movies and TV shows are sourced from **MejorTorrent**: search for the title on the site, open its page, and submit that URL to the scraper. MejorTorrent changes domains frequently (`www43.mejortorrent.eu`, `www47.mejortorrent.eu`, etc.); use whichever is currently active. The scraper recognizes its download links (`/torrents/peliculas/...` and `/torrents/series/...`) and falls back to Playwright when the page loads them via JavaScript.

Leave the scraper running and, from another terminal, submit a page:

```bash
.venv/bin/mediaserver-add "https://www47.mejortorrent.eu/pelicula/31079/El-Visitante"
# or from any machine on your network (or over WireGuard):
curl "http://SERVER_IP:5000/add_page?url=<URL-encoded page URL>"
```

The response is a JSON object listing the links found and those successfully sent to qBittorrent. `GET /health` can be used as a liveness check. Stop the scraper with `Ctrl+C` once you have checked it works; cron starts it at boot (step 9).

The page HTML is retrieved using the following strategies, in order:

1. **Direct request with ECH** (`curl_cffi` + DNS over HTTPS): Encrypted Client Hello hides the target hostname in the TLS handshake, which circumvents ISP blocking based on SNI. Cloudflare cookies are obtained from the bypass container.
2. **`cloudflare-bypass` container** (`/html` endpoint), which loads the page in a real browser to solve the Cloudflare challenge. If the result appears empty, it retries with fresh cookies.
3. **Playwright + Chromium** using those cookies (MejorTorrent only; disable with `SCRAPE_PLAYWRIGHT_FALLBACK=0`), for pages that inject download links via JavaScript.

`.torrent` files are downloaded on the server and uploaded to qBittorrent; magnet links are passed through directly.

> **Security:** the scraper has no authentication. Do not expose port 5000 to the internet; access it from your local network or over WireGuard.

### 9. Schedule everything with cron

To start everything at boot and run the organizer every 30 minutes:

```bash
mkdir -p logs
sed "s#/path/to/repo#$PWD#g" deploy/crontab.example   # review the output
crontab -e                                            # and paste the lines
```

Scheduled jobs:

| When | What |
|---|---|
| At boot | `scripts/start.sh`: waits for the drive to be mounted, then runs `docker compose up -d` |
| At boot | `mediaserver-scraper` |
| Every 30 min | `mediaserver-organize` |
| Every 30 min | `scripts/dynv6.sh` (optional, fallback for ddclient) |

Logs are written to `logs/`. Reboot once (`sudo reboot`) and check that everything comes back up with `docker compose ps` and `curl http://localhost:5000/health`.

The installation is complete.

---

## Optional: keeping the server awake and tuned

None of this is required. Check each item against your hardware before applying it.

### Prevent the system from suspending

A desktop installation of Linux may suspend the machine after a period of inactivity, which takes every service offline and cannot be undone remotely. To disable suspend and hibernation system-wide (shutdown and reboot are unaffected; undo with `unmask`):

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

### Keep the drive from spinning down (HDD only)

Check the drive type first:

```bash
lsblk -d -o NAME,ROTA,TRAN,MODEL      # ROTA=1 → hard disk, ROTA=0 → SSD
```

**SSD (`ROTA=0`):** nothing to do. SSDs have no moving parts and do not spin down.

**HDD (`ROTA=1`):** most hard disks, and especially USB enclosures, spin down after a few minutes of inactivity to save power. On a media server this means:

- a delay of several seconds when you start playback in Jellyfin, open the Samba share, or the organizer runs, while the disk spins back up;
- frequent spin-up/spin-down cycles, which add wear to the drive over the years.

Whether to prevent it is a trade-off: an always-spinning disk uses a few more watts and makes some noise, but responds instantly. If you want the disk always ready, follow these steps.

1. **Check whether the drive accepts power commands** (install `hdparm` and replace `sdX` with your disk):

   ```bash
   sudo hdparm -C /dev/sdX      # prints "active/idle" or "standby"
   ```

   If it prints an error such as `SG_IO: bad/missing sense data`, the USB enclosure does not pass these commands through; go to step 4.

2. **Disable spin-down permanently.** On Debian/Ubuntu, add the disk to `/etc/hdparm.conf`, using its stable ID from `ls -l /dev/disk/by-id/`:

   ```
   /dev/disk/by-id/ata-WDC_WD40EFRX-68N32N0_WD-XXXXXXXX {
       apm = 254
       spindown_time = 0
   }
   ```

   `apm = 254` selects maximum performance without the drive's own power-saving spin-down, and `spindown_time = 0` disables the standby timer. The settings are applied automatically when the disk is detected, including at boot. To test them immediately without rebooting: `sudo hdparm -B 254 -S 0 /dev/sdX`.

3. **USB drives: disable USB autosuspend** so the kernel does not power down the port. Check the current value:

   ```bash
   cat /sys/block/sdX/device/../../../../power/control    # "on" = autosuspend already disabled
   ```

   If it shows `auto`, create a udev rule with your drive's vendor and product IDs (from `lsusb`, shown as `ID vvvv:pppp`):

   ```bash
   echo 'ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="vvvv", ATTR{idProduct}=="pppp", TEST=="power/control", ATTR{power/control}="on"' \
     | sudo tee /etc/udev/rules.d/50-media-disk-no-autosuspend.rules
   sudo udevadm control --reload && sudo udevadm trigger
   ```

4. **If the enclosure ignores `hdparm`**, it has its own sleep timer in firmware that the operating system cannot change. The usual workaround is to touch the disk periodically so the timer never expires. Add this to your crontab (shorter than the enclosure's timeout, typically 10–30 minutes):

   ```
   */5 * * * * touch /mnt/media/.keepalive
   ```

5. **Verify** after leaving the server idle for longer than the previous spin-down time: `sudo hdparm -C /dev/sdX` should still report `active/idle`.

### System tuning script (`scripts/optimize.sh`)

A script for a dedicated, always-on server. It applies four settings at runtime:

| Step | Setting | Purpose | When it matters |
|---|---|---|---|
| 1 | CPU governor → `schedutil` (or `performance` if unavailable) | Keeps the CPU responsive instead of throttling aggressively | CPUs that default to `powersave`. Many Intel CPUs (`intel_pstate`) only offer `performance` and `powersave` |
| 2 | TCP keepalive 600 s and 16 MB socket buffers | Detects dead connections sooner and improves throughput on large transfers (Samba, Jellyfin streaming) | Mostly a minor gain on a home LAN |
| 3 | Network interface power management off | Prevents the network card from entering a low-power state and dropping connections | NICs that enable runtime power management by default |
| 4 | Disk standby disabled (`hdparm -B 254 -S 0`) | Stops hard drives from spinning down | Mechanical hard drives only, and only until the next reboot. Prefer the permanent steps in the previous section |

Most systems don't need it. Check what your machine is already doing before enabling it:

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor   # current CPU governor
sysctl net.ipv4.tcp_keepalive_time net.core.rmem_max         # current network settings
cat /sys/class/net/*/device/power/control                    # "on" = NIC power management already off
lsblk -d -o NAME,ROTA,MODEL                                   # ROTA=0 means SSD (step 4 not needed)
```

All changes are lost on reboot. To run it at every boot from `start.sh`, set `RUN_OPTIMIZE=true` in `.env`. Because it calls `sudo`, cron can only run it if those commands are allowed without a password in sudoers; otherwise it fails silently. A cleaner way to make the network settings permanent is a sysctl file, which needs no sudo at boot:

```bash
sudo tee /etc/sysctl.d/99-mediaserver.conf <<'EOF'
net.ipv4.tcp_keepalive_time=600
net.ipv4.tcp_keepalive_intvl=60
net.ipv4.tcp_keepalive_probes=5
net.core.rmem_max=16777216
net.core.wmem_max=16777216
net.ipv4.tcp_rmem=4096 87380 16777216
net.ipv4.tcp_wmem=4096 65536 16777216
EOF
sudo sysctl --system
```

---

## Configuration reference

| Variable | Required | Default | Used by | Description |
|---|---|---|---|---|
| `PUID`, `PGID` | | `1000` | Docker | User and group that own the files |
| `TZ` | | `Europe/Madrid` | Docker | Time zone |
| `MEDIA_ROOT` | ✔ | — | Docker, organizer, `start.sh` | Root of the media drive |
| `CONFIG_DIR` | | `./docker/config` | Docker | Persistent container data |
| `DOMAIN` | ✔ | — | Caddy, WireGuard | Public hostname |
| `SERVER_IP` | ✔ | — | Caddy | Server LAN IP |
| `DYNV6_ZONE`, `DYNV6_TOKEN` | For `dynv6.sh` | — | `dynv6.sh` | dynv6 zone and token |
| `WG_PORT` | | `51820` | WireGuard | Public UDP port |
| `WG_PEERS` | | `1` | WireGuard | Number of VPN clients (or a list of names) |
| `AMULE_WEBUI_PASSWORD`, `AMULE_GUI_PASSWORD` | ✔ | — | aMule | Passwords |
| `SAMBA_USER`, `SAMBA_PASSWORD` | ✔ | — | Samba | Network share credentials |
| `SAMBA_SHARE` | | `media` | Samba | Share name |
| `SAMBA_NETBIOS_NAME` | | `MEDIASERVER` | Samba | Hostname shown on the network |
| `QB_URL` | | `http://localhost:8080/api/v2` | Scraper | qBittorrent API URL |
| `QB_USER`, `QB_PASS` | ✔ | `admin` / — | Scraper | qBittorrent credentials |
| `OPENROUTER_API_KEY` | ✔ | — | Organizer | OpenRouter API key |
| `OPENROUTER_MODEL` | | — | Organizer | Model to try first (recommended: leave empty) |
| `OPENROUTER_FALLBACK` | | `openrouter/free` | Organizer | Last-resort model |
| `NTFY_TOPIC` | | — | Organizer | ntfy.sh topic for failure alerts |
| `WAIT_FOR_MOUNTS` | | `MEDIA_ROOT` | `start.sh` | Mount points to wait for (space-separated) |
| `RUN_OPTIMIZE` | | `false` | `start.sh` | Run `optimize.sh` at boot |
| `DATA_DIR` | | `data` | Organizer | State directory |
| `TORRENTS_COMPLETE_DIR`, `TV_DIR`, `MOVIES_DIR` | | under `MEDIA_ROOT` | Organizer | Custom paths if not using the standard layout |
| `SCRAPER_HOST`, `SCRAPER_PORT` | | `0.0.0.0`, `5000` | Scraper | Listen address |
| `CF_BYPASS_BASE` | | `http://127.0.0.1:8000` | Scraper | Bypass container URL |
| `CF_BYPASS_TIMEOUT`, `CF_BYPASS_RETRIES` | | `180`, `5` | Scraper | Bypass timeout (seconds) and retry count |
| `CF_BYPASS_BYPASS_CACHE` | | `false` | Scraper | Always request fresh cookies |
| `ECH_DOH_URL` | | `https://1.1.1.1/dns-query` | Scraper | DoH resolver used for ECH |
| `SCRAPE_PLAYWRIGHT_FALLBACK` | | `1` | Scraper | Enable the Playwright fallback |
| `SCRAPE_SAVE_LAST_HTML` | | — | Scraper | Save the HTML when no links are found (debugging) |

## Troubleshooting

- **Viewing logs:** `tail -f logs/*.log` for the scripts and `docker compose logs -f <service>` for the containers.
- **`docker compose` reports a missing variable:** it is not set in `.env`; compare against `.env.example`.
- **The organizer reports "No model responded":** OpenRouter's free models change frequently. Delete `data/free_models.json` to force a refresh of the model list. If `OPENROUTER_MODEL` is set, check that the model still has a free tier, or clear it.
- **The scraper finds no torrents:** set `SCRAPE_SAVE_LAST_HTML=/tmp/last.html` and inspect the HTML it received. If it is a Cloudflare challenge page, update the bypass image: `docker compose pull cloudflare-bypass && docker compose up -d cloudflare-bypass`.
- **Caddy fails to obtain a certificate:** verify that `DOMAIN` resolves to your public IP (`dig +short YOUR_DOMAIN`) and that TCP ports 80 and 443 reach the server.
- **Folder permission errors:** check that `PUID`/`PGID` match the owner of `MEDIA_ROOT`.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## License

[MIT](LICENSE)

## Disclaimer

This project is intended for personal use. Downloading or distributing copyrighted material without permission may be illegal in your jurisdiction; you are solely responsible for how you use this software.
