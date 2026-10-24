"""
Automatic organizer for movies and TV shows.
1. Extracts ZIP/RAR/7z archives found inside download folders
2. Lists the folders in torrents/complete
3. Builds a prompt and sends it to OpenRouter
4. Moves everything into the structure Jellyfin expects
"""

import os
import re
import shutil
import json
import subprocess
import sys
from pathlib import Path

from mediaserver.config import DATA_DIR, env, env_path

# Configuration (see .env.example)
MEDIA_ROOT = env("MEDIA_ROOT")
TORRENTS_COMPLETE = str(env_path("TORRENTS_COMPLETE_DIR", Path(MEDIA_ROOT) / "torrents" / "complete"))
TV_DIR = str(env_path("TV_DIR", Path(MEDIA_ROOT) / "tv"))
MOVIE_DIR = str(env_path("MOVIES_DIR", Path(MEDIA_ROOT) / "movies"))
LAST_RESPONSE_FILE = os.path.join(DATA_DIR, "last_response.json")
LAST_PROMPT_FILE = os.path.join(DATA_DIR, "last_prompt.txt")

OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = env("OPENROUTER_MODEL")
# Last resort: OpenRouter's router that spreads requests across available free models
OPENROUTER_FALLBACK = env("OPENROUTER_FALLBACK", "openrouter/free")

# Automatic discovery of free models
MODELS_CACHE_FILE = os.path.join(DATA_DIR, "free_models.json")
MODEL_STATE_FILE = os.path.join(DATA_DIR, "model_state.json")
CACHE_TTL = 24 * 3600      # refresh the model list once a day
COOLDOWN = 6 * 3600        # a failing model is skipped for 6 h
MAX_CANDIDATES = 4         # how many models to try before the router
MIN_CONTEXT = 32768
NTFY_TOPIC = env("NTFY_TOPIC")

VIDEO_EXTENSIONS = [".mkv", ".mp4", ".avi"]


# ---------------------------------------------------------
# NOTIFICATIONS
# ---------------------------------------------------------
def notify(title, message):
    if not NTFY_TOPIC:
        return
    try:
        import requests
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high"},
            timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------
# 1) EXTRACT ARCHIVES
# ---------------------------------------------------------
def extract_archive(item):
    """Extracts the archive found in a given folder.
       Returns True if something was extracted, False if there was nothing to extract.
    """
    if not item.is_dir():
        return False

    # Look for multi-part archives first
    part_files = list(item.glob("*.part*.rar")) + list(item.glob("*.part*.zip")) + list(item.glob("*.part*.7z"))
    # Look for .r00, .r01, .r02, etc. (old WinRAR format)
    r00_files = [f for f in item.glob("*.r*") if re.match(r'.*\.r\d{2,3}$', f.name, re.I) and f.suffix not in ['.rar', '.mkv', '.mp4', '.avi']]
    # Look for .001, .002, .003, etc. (numbered 7z format) - only if there are several
    num_files = [f for f in item.iterdir() if f.is_file() and re.match(r'.*\.\d{3,4}$', f.name) and f.suffix not in ['.mkv', '.mp4', '.avi', '.srt', '.sub']]
    # Only treat numbered files as multi-part if there are at least 2
    if len(num_files) < 2:
        num_files = []

    # Look for single archives (excluding multi-part ones)
    zip_files = [f for f in item.glob("*.zip") if "part" not in f.name.lower() and not f.name.endswith((".001", ".002", ".003"))]
    rar_files = [f for f in item.glob("*.rar") if "part" not in f.name.lower() and not f.name.endswith((".r00", ".r01", ".r02"))]
    sevenz_files = [f for f in item.glob("*.7z") if "part" not in f.name.lower() and not f.name.endswith((".001", ".002", ".003"))]

    # Decide which kind of archive we have
    archive = None
    is_multipart = False

    # PRIORITY 1: single archives first (they may contain multi-part archives inside)
    all_archives = zip_files + rar_files + sevenz_files
    if len(all_archives) == 1 and not part_files and not r00_files and not num_files:
        archive = all_archives[0]

    # PRIORITY 2: multi-part RAR/ZIP/7Z (part01.rar, part02.rar, etc.)
    elif part_files:
        # Find the first file of the set (part01, part1, part001, etc.)
        sorted_parts = sorted(part_files, key=lambda x: x.name)
        for f in sorted_parts:
            if re.search(r'\.part0*1\.', f.name, re.I) or re.search(r'\.part1\.', f.name, re.I):
                archive = f
                is_multipart = True
                break
        # If part1 was not found, use the first one alphabetically
        if not archive:
            archive = sorted_parts[0]
            is_multipart = True

    # PRIORITY 3: old WinRAR format (.r00, .r01, etc.)
    elif r00_files:
        # Look for the main .rar file (without number)
        main_rar = list(item.glob("*.rar"))
        if main_rar:
            archive = main_rar[0]
            is_multipart = True
        else:
            return False

    # PRIORITY 4: numbered 7z format (.001, .002, etc.)
    elif num_files:
        # Look for the main .7z or .zip file
        main_archive = list(item.glob("*.7z")) + list(item.glob("*.zip"))
        if main_archive:
            archive = main_archive[0]
            is_multipart = True
        else:
            return False

    # No archive found
    if not archive:
        return False

    print(f"📦 Found archive in {item.name}: {archive.name}")
    if is_multipart:
        print("   (multi-part archive detected)")

    try:
        # Determine the archive type from its actual extension
        name_lower = archive.name.lower()
        archive_type = None

        if name_lower.endswith(".zip") or ".zip" in name_lower:
            archive_type = "zip"
        elif name_lower.endswith(".rar") or ".rar" in name_lower:
            archive_type = "rar"
        elif name_lower.endswith(".7z") or ".7z" in name_lower:
            archive_type = "7z"
        elif name_lower.endswith((".001", ".002", ".003")):
            # For .001, .002, etc., infer the type from the main archive
            if list(item.glob("*.7z")):
                archive_type = "7z"
            elif list(item.glob("*.zip")):
                archive_type = "zip"
            else:
                archive_type = "7z"  # default for numbered files
        elif name_lower.endswith((".r00", ".r01", ".r02")):
            archive_type = "rar"

        if archive_type == "zip":
            subprocess.run(["unzip", "-P", "", "-o", str(archive), "-d", TORRENTS_COMPLETE], check=True)
        elif archive_type == "rar":
            subprocess.run(["unrar", "x", "-p-", "-o+", str(archive), TORRENTS_COMPLETE], check=True)
        elif archive_type == "7z":
            subprocess.run(["7z", "x", "-p-", str(archive), f"-o{TORRENTS_COMPLETE}"], check=True)
        else:
            print(f"⚠️  Unrecognized archive type: {archive.name}")
            return False

        print(f"📤 Extracted: {archive.name}")
        return True

    except Exception as e:
        print(f"❌ Error extracting {archive.name} (password-protected or corrupt?): {e}")
        print(f"🗑️ Deleting entire folder: {item.name}")
        shutil.rmtree(item)
        return False


def extract_archives():
    """Finds folders containing archives (single or multi-part), extracts them
       into torrents/complete and deletes the original folder.
       Repeats until no archives remain (to handle nested archives).
       If an archive is password-protected or fails, the whole folder is deleted.
    """
    print("🗜️ Looking for archives...")

    max_iterations = 10  # avoid infinite loops
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        extracted = 0

        # Snapshot the folder list before iterating (the directory changes as we go)
        folders = [item for item in Path(TORRENTS_COMPLETE).iterdir() if item.is_dir()]

        if not folders:
            break

        for item in folders:
            if extract_archive(item):
                extracted += 1
                # After extracting, delete the folder if it is empty or only holds archives
                try:
                    if item.exists():
                        contents = list(item.iterdir())
                        if not contents or all(f.suffix in ['.zip', '.rar', '.7z', '.r00', '.r01', '.r02', '.001', '.002', '.003'] or 'part' in f.name.lower() for f in contents if f.is_file()):
                            shutil.rmtree(item)
                            print(f"🗑️ Folder deleted: {item.name}")
                except Exception as e:
                    print(f"⚠️  Could not delete folder {item.name}: {e}")

        if extracted == 0:
            # Nothing extracted, we are done
            break

        print(f"🔄 Iteration {iteration} done. Looking for more archives...")

    if iteration >= max_iterations:
        print(f"⚠️  Reached the iteration limit ({max_iterations}). There may be deeply nested archives.")


# ---------------------------------------------------------
# 2) LIST FOLDERS
# ---------------------------------------------------------
def list_folders():
    """Lists folders and loose files that contain videos"""
    if not os.path.exists(TORRENTS_COMPLETE):
        print(f"❌ {TORRENTS_COMPLETE} does not exist")
        return []

    folders = []
    loose_files = []

    for item in Path(TORRENTS_COMPLETE).iterdir():
        if item.is_dir():
            files = []
            for ext in ["*.mkv", "*.mp4", "*.avi"]:
                files.extend([f.name for f in item.glob(ext)])
            if files:
                folders.append({
                    "folder_name": item.name,
                    "files": files
                })
        else:
            if item.suffix.lower() in VIDEO_EXTENSIONS:
                loose_files.append(item.name)

    groups = {}
    for file in loose_files:
        base = re.split(r'\.S\d+E\d+|\.\d{3,4}p|\.1080p|\.720p', file, flags=re.I)[0]
        base = base.replace(".", " ").strip()
        groups.setdefault(base, []).append(file)

    for base, files in groups.items():
        folders.append({
            "folder_name": f"__single__{base}",
            "files": files
        })

    return folders


# ---------------------------------------------------------
# 3) BUILD PROMPT
# ---------------------------------------------------------
def build_prompt(folders):
    prompt = """Analyze these folders and return JSON:
[
  {
    "original_folder": "name",
    "type": "series" | "movie",
    "name": "Clean Name",
    "episodes": [{"file": "file.mkv", "season": 1, "episode": 1}]
  }
]

Folders:
"""
    for c in folders:
        prompt += f"\n{c['folder_name']}"
        for a in c['files']:
            prompt += f"\n  - {a}"

    prompt += """

RULES:
- Name in Title Case, keeping the title in its original language as it appears in the folder
- 101 -> Season 1, Episode 1
- Strip tags such as [HDTV 720p], etc.
- Movies have no episodes
"""
    return prompt


# ---------------------------------------------------------
# 3.5) DISCOVER FREE MODELS
# ---------------------------------------------------------
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def discover_free_models():
    """List of free text models, cached for 24 h.

    OpenRouter does not expose the website's "most-popular" ordering through
    its API, so we sort by structured JSON support, context length and recency.
    """
    import time

    cache = _read_json(MODELS_CACHE_FILE, {})
    if cache.get("models") and time.time() - cache.get("ts", 0) < CACHE_TTL:
        return cache["models"]

    try:
        import requests
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
        r.raise_for_status()
        raw = r.json()["data"]
    except Exception as e:
        print(f"⚠️  Could not refresh the model list: {e}")
        return cache.get("models", [])

    usable = []
    for m in raw:
        pricing = m.get("pricing") or {}
        arch = m.get("architecture") or {}
        params = m.get("supported_parameters") or []

        if m.get("id") == OPENROUTER_FALLBACK:
            continue
        # actually free
        if any(float(pricing.get(k, 0) or 0) != 0 for k in ("prompt", "completion", "request")):
            continue
        # text output only (skips audio/image models)
        if not str(arch.get("modality", "")).endswith("->text"):
            continue
        # requiring "tools" skips classifiers such as content-safety models, which are not chat models
        if "tools" not in params:
            continue
        if (m.get("context_length") or 0) < MIN_CONTEXT:
            continue

        usable.append({
            "id": m["id"],
            "ctx": m.get("context_length") or 0,
            "json": "structured_outputs" in params or "response_format" in params,
            "reasoning": "reasoning" in params,
            "created": m.get("created") or 0,
        })

    usable.sort(key=lambda m: (not m["json"], -m["ctx"], -m["created"]))
    models = [m["id"] for m in usable]
    _write_json(MODELS_CACHE_FILE, {"ts": time.time(), "models": models,
                                    "details": usable})
    print(f"🔄 Free model list refreshed ({len(models)} candidates)")
    return models


def build_candidates():
    """Order in which models are tried: the last one that worked, the one
    preferred in .env, the discovered ones, and the router as a last resort."""
    import time

    state = _read_json(MODEL_STATE_FILE, {})
    now = time.time()
    on_cooldown = {
        mid for mid, info in state.items()
        if isinstance(info, dict) and info.get("cooldown_until", 0) > now
    }

    order = []
    for mid in [state.get("_last_ok"), OPENROUTER_MODEL] + discover_free_models():
        if mid and mid not in order and mid not in on_cooldown:
            order.append(mid)

    order = order[:MAX_CANDIDATES]
    if OPENROUTER_FALLBACK and OPENROUTER_FALLBACK not in order:
        order.append(OPENROUTER_FALLBACK)
    return order


def _supports_reasoning(model):
    if model == OPENROUTER_FALLBACK:
        return True
    details = _read_json(MODELS_CACHE_FILE, {}).get("details") or []
    for m in details:
        if m.get("id") == model:
            return bool(m.get("reasoning"))
    return False


def _record_result(model, ok):
    import time

    state = _read_json(MODEL_STATE_FILE, {})
    info = state.get(model) or {}
    if ok:
        info["ok"] = info.get("ok", 0) + 1
        info["cooldown_until"] = 0
        state["_last_ok"] = model
    else:
        info["err"] = info.get("err", 0) + 1
        info["cooldown_until"] = time.time() + COOLDOWN
        if state.get("_last_ok") == model:
            state.pop("_last_ok", None)
    state[model] = info
    _write_json(MODEL_STATE_FILE, state)


# ---------------------------------------------------------
# 4) CALL THE LLM
# ---------------------------------------------------------
def call_llm(prompt, validate=None):
    try:
        import requests

        print("🤖 Querying LLM...")

        candidates = build_candidates()
        failures = []

        for model in candidates:
            body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
            if _supports_reasoning(model):
                body["reasoning"] = {"enabled": True}

            print(f"   → {model}")
            try:
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json=body,
                    timeout=300,
                )
            except Exception as e:
                failures.append(f"{model}: {e}")
                print(f"⚠️  {failures[-1]}")
                _record_result(model, False)
                continue

            if response.status_code == 200:
                data = response.json()
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                if not content:
                    failures.append(f"{model}: empty response")
                elif validate and not validate(content):
                    failures.append(f"{model}: invalid response")
                else:
                    used = data.get("model", model)
                    if used != model:
                        print(f"   (resolved to {used})")
                    _record_result(model, True)
                    return content
            else:
                failures.append(f"{model}: HTTP {response.status_code} {response.text[:200]}")

            print(f"⚠️  {failures[-1]}")
            _record_result(model, False)

        print("❌ No model responded")
        notify(
            "mediaserver organizer error",
            "No model responded:\n" + "\n".join(failures[-3:]),
        )
        return None

    except Exception as e:
        print(f"❌ Error: {e}")
        notify("mediaserver organizer error", f"Exception while calling the LLM:\n{e}")
        return None


# ---------------------------------------------------------
# 5) PARSE JSON
# ---------------------------------------------------------
def parse_json(text):
    try:
        match = re.search(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
        if match:
            return json.loads(match.group(1))

        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            return json.loads(match.group())

        return json.loads(text)
    except Exception as e:
        print(f"❌ Parse error: {e}")
        return None


# ---------------------------------------------------------
# 6) PROCESS RESULTS
# ---------------------------------------------------------
def process_results(results):
    if not results:
        print("❌ No data")
        return

    # Track processed files so none is handled twice
    processed_files = set()

    for item in results:
        kind = item.get('type')
        name = item.get('name', '').title()
        episodes = item.get('episodes', [])

        if not kind or not name:
            continue

        safe_name = re.sub(r'[<>:"/\\|?*]', '', name).strip()

        if kind == "series":
            for episode in episodes:
                file = episode.get('file')
                season = episode.get('season', 1)
                episode_num = episode.get('episode')

                if not file or not episode_num:
                    continue

                if item['original_folder'].startswith("__single__"):
                    folder_path = Path(TORRENTS_COMPLETE)
                else:
                    folder_path = Path(TORRENTS_COMPLETE) / item['original_folder']

                source = None

                for ext in VIDEO_EXTENSIONS:
                    candidate = folder_path / f"{file}"
                    if candidate.exists():
                        source = candidate
                        break
                    for f in folder_path.glob(f"{file}*"):
                        if f.suffix in VIDEO_EXTENSIONS:
                            source = f
                            break

                if not source:
                    print(f"⚠️  Not found: {file}")
                    continue

                dest_dir = Path(TV_DIR) / safe_name / f"Season {season:02d}"
                dest_dir.mkdir(parents=True, exist_ok=True)

                final_name = f"S{season:02d}E{episode_num:02d}{source.suffix}"
                final_path = dest_dir / final_name

                shutil.move(str(source), str(final_path))
                print(f"✓ Series: {source.name} → {final_path}")

        elif kind == "movie":
            if item['original_folder'].startswith("__single__"):
                folder_path = Path(TORRENTS_COMPLETE)
                # Group base name, used to find the matching file
                group_name = item['original_folder'].replace("__single__", "").strip()

                # Find the file that best matches the name returned by the LLM
                # and the group base name
                llm_name = name.lower()
                source = None
                best_score = 0

                for ext in VIDEO_EXTENSIONS:
                    for f in folder_path.glob(f"*{ext}"):
                        # Skip files already processed
                        if str(f) in processed_files:
                            continue

                        file_lower = f.name.lower()
                        # Score by keyword overlap with the LLM name and the group name
                        llm_words = set(p for p in llm_name.split() if len(p) > 2)
                        file_words = set(p for p in file_lower.replace('.', ' ').replace('[', ' ').replace(']', ' ').split() if len(p) > 2)
                        group_words = set(p for p in group_name.lower().split() if len(p) > 2)

                        score = len(llm_words & file_words) + len(group_words & file_words)

                        # Prefer files with more matches
                        if score > best_score:
                            best_score = score
                            source = f

                # Without a good match, fall back to the group name keywords
                if not source or best_score < 2:
                    keywords = [p for p in group_name.split() if len(p) > 2 and p.lower() not in ['vs', 'the', 'and', 'com', 'www', 'newpct']]
                    for ext in VIDEO_EXTENSIONS:
                        for f in folder_path.glob(f"*{ext}"):
                            # Skip files already processed
                            if str(f) in processed_files:
                                continue
                            file_lower = f.name.lower()
                            matches = sum(1 for word in keywords if word.lower() in file_lower)
                            if matches >= max(1, len(keywords) // 2):
                                source = f
                                break
                        if source:
                            break
            else:
                folder_path = Path(TORRENTS_COMPLETE) / item['original_folder']
                # Regular folders: take any video file
                source = None
                for ext in VIDEO_EXTENSIONS:
                    for f in folder_path.glob(f"*{ext}"):
                        source = f
                        break
                    if source:
                        break

            if not source:
                print(f"⚠️  Not found in {item['original_folder']}")
                continue

            # Check whether this file was already processed
            if str(source) in processed_files:
                print(f"⚠️  Already processed: {source.name}")
                continue

            dest_dir = Path(MOVIE_DIR) / safe_name
            dest_dir.mkdir(parents=True, exist_ok=True)

            final_name = f"{safe_name}{source.suffix}"
            final_path = dest_dir / final_name

            shutil.move(str(source), str(final_path))
            processed_files.add(str(source))
            print(f"✓ Movie: {source.name} → {final_path}")


# ---------------------------------------------------------
# 7) CLEAN UP
# ---------------------------------------------------------
def clean_up():
    for item in Path(TORRENTS_COMPLETE).iterdir():
        if item.is_dir():
            for junk in item.glob("*.txt"):
                junk.unlink()
            for junk in item.glob("*.url"):
                junk.unlink()
            if not any(item.iterdir()):
                item.rmdir()
                print(f"🗑️  Empty folder removed: {item.name}")


# ---------------------------------------------------------
# 8) MAIN
# ---------------------------------------------------------
def main():
    if not MEDIA_ROOT and not env("TORRENTS_COMPLETE_DIR"):
        sys.exit("❌ MEDIA_ROOT is not set in .env (see .env.example)")
    if not OPENROUTER_API_KEY:
        sys.exit("❌ OPENROUTER_API_KEY is not set in .env (see .env.example)")
    os.makedirs(DATA_DIR, exist_ok=True)

    print("🔍 Scanning torrents/complete...\n")

    if not os.path.isdir(TORRENTS_COMPLETE):
        print(f"❌ {TORRENTS_COMPLETE} does not exist")
        return

    extract_archives()

    folders = list_folders()
    if not folders:
        print("Nothing to organize")
        return

    print(f"Found {len(folders)} folders")

    prompt = build_prompt(folders)

    with open(LAST_PROMPT_FILE, "w", encoding="utf-8") as f:
        f.write(prompt)
    print(f"📄 Prompt saved to {LAST_PROMPT_FILE}")

    response = call_llm(prompt, validate=parse_json)

    if response:
        results = parse_json(response)

        if results:
            with open(LAST_RESPONSE_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            print(f"💾 Valid JSON saved to {LAST_RESPONSE_FILE}")
            process_results(results)
            clean_up()
            print("\n✅ Organization complete!")
        else:
            with open(LAST_RESPONSE_FILE, "w", encoding="utf-8") as f:
                f.write(response)
            print(f"💾 Raw response saved to {LAST_RESPONSE_FILE} (could not be parsed)")
            print("❌ Could not parse the response")
    else:
        print("❌ Could not get a response from the LLM")


if __name__ == "__main__":
    main()
