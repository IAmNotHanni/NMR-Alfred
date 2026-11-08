import copy
import glob
import json
import os
import queue
import re
import shutil
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, ttk


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "nmr-alfred-config.json")
CURRENT_VERSION = "0.1.2"
LATEST_RELEASE_API = "https://api.github.com/repos/IAmNotHanni/NMR-Alfred/releases/latest"
RELEASES_PAGE_URL = "https://github.com/IAmNotHanni/NMR-Alfred/releases"

INTERVALS = {
    "5 min": 5 * 60,
    "10 min": 10 * 60,
    "15 min": 15 * 60,
    "20 min": 20 * 60,
    "30 min": 30 * 60,
    "2 h": 2 * 3600,
    "4 h": 4 * 3600,
    "6 h": 6 * 3600,
}
DEFAULT_INTERVAL = "30 min"

MATCH_TYPES = ["contains", "starts with", "ends with", "regex"]

DEFAULT_CONFIG = {
    "folders": [],
    "rules": [],
}


class ScanAborted(Exception):
    pass


def current_copy_log_path(dt=None):
    if dt is None:
        dt = datetime.now()
    iso_year, iso_week, _ = dt.isocalendar()
    return os.path.join(APP_DIR, f"NMR_{iso_year}_W{iso_week:02d}.txt")


def sanitize_log_field(value):
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def serialize_copy_entry(entry):
    return "\t".join([
        sanitize_log_field(entry["timestamp"]),
        sanitize_log_field(entry["sample"]),
        sanitize_log_field(entry.get("nickname", "")),
        sanitize_log_field(entry["program"]),
        sanitize_log_field(entry["source"]),
        sanitize_log_field(entry["destination"]),
    ])


def append_copy_entry_to_log(entry):
    path = current_copy_log_path()
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new_file:
            f.write("timestamp\tsample\tnickname\tprogram\tsource\tdestination\n")
        f.write(serialize_copy_entry(entry) + "\n")


def load_copy_history():
    entries = []
    pattern = os.path.join(APP_DIR, "NMR_????_W??.txt")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("timestamp\t"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 5:
                        continue
                    if len(parts) >= 6:
                        timestamp, sample, nickname, program, source, destination = parts[:6]
                    else:
                        timestamp, sample, program, source, destination = parts[:5]
                        nickname = ""
                    entries.append({
                        "timestamp": timestamp,
                        "sample": sample,
                        "nickname": nickname,
                        "program": program,
                        "source": source,
                        "destination": destination,
                    })
        except Exception:
            continue
    return entries


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

_log_listeners = []


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    for listener in _log_listeners:
        listener(line)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"[ERROR] Could not read config {CONFIG_PATH}: {e}")
        return copy.deepcopy(DEFAULT_CONFIG)
    cfg.setdefault("folders", [])
    cfg.setdefault("rules", [])
    changed = False
    for folder in cfg["folders"]:
        folder["nickname"] = str(folder.get("nickname", "")).strip()
    for rule in cfg["rules"]:
        if "enabled" not in rule:
            rule["enabled"] = True
            changed = True
    if changed:
        try:
            save_config(cfg)
        except Exception as e:
            log(f"[ERROR] Could not save rule enabled defaults to {CONFIG_PATH}: {e}")
    return cfg


def cleanup_missing_destinations(cfg):
    changed = False
    for rule in cfg.get("rules", []):
        destinations = rule.get("destinations", [])
        valid_destinations = []
        for dest in destinations:
            if os.path.isdir(dest):
                valid_destinations.append(dest)
            else:
                log(f"[ERROR] Destination does not exist at startup and was removed: {dest}")
                changed = True
        rule["destinations"] = valid_destinations
        if not valid_destinations and destinations:
            log(f"[ERROR] Rule '{rule.get('expression', '')}' has no valid destinations left.")
    if changed:
        try:
            save_config(cfg)
        except Exception as e:
            log(f"[ERROR] Could not save cleaned config {CONFIG_PATH}: {e}")
    return changed


def cleanup_rules_without_sources(cfg):
    rules = cfg.get("rules", [])
    removed_rules = [rule for rule in rules if not rule.get("sources")]
    if not removed_rules:
        return []
    cfg["rules"] = [rule for rule in rules if rule.get("sources")]
    try:
        save_config(cfg)
    except Exception as e:
        log(f"[ERROR] Could not save config after removing rules without watched folders: {e}")
    return removed_rules


def cleanup_duplicate_nickname_folders(cfg):
    nickname_counts = {}
    for folder in cfg.get("folders", []):
        nickname = str(folder.get("nickname", "")).strip().casefold()
        if nickname:
            nickname_counts[nickname] = nickname_counts.get(nickname, 0) + 1

    duplicate_folders = [
        folder for folder in cfg.get("folders", [])
        if nickname_counts.get(str(folder.get("nickname", "")).strip().casefold(), 0) > 1
    ]
    if not duplicate_folders:
        return [], []

    duplicate_paths = [folder["path"] for folder in duplicate_folders]
    removed_rules = [
        rule for rule in cfg.get("rules", [])
        if any(
            same_path(source, path)
            for source in rule.get("sources", [])
            for path in duplicate_paths
        )
    ]
    cfg["folders"] = [folder for folder in cfg.get("folders", []) if folder not in duplicate_folders]
    cfg["rules"] = [rule for rule in cfg.get("rules", []) if rule not in removed_rules]
    try:
        save_config(cfg)
    except Exception as e:
        log(f"[ERROR] Could not save config after removing duplicate-nickname folders: {e}")
    return duplicate_folders, removed_rules


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def extract_nmr_nickname(path):
    matches = list(re.finditer(r"(?<![A-Za-z0-9])(\d{3}\s*MHz[^/\\]*)", path, re.IGNORECASE))
    return matches[-1].group(1) if matches else ""


def unique_nmr_nickname(nickname, folders, exclude_index=None):
    base = nickname.strip()
    existing = {
        str(folder.get("nickname", "")).strip().casefold()
        for index, folder in enumerate(folders)
        if index != exclude_index
    }
    if base.casefold() not in existing:
        return base
    suffix = 2
    while f"{base} ({suffix})".casefold() in existing:
        suffix += 1
    return f"{base} ({suffix})"


def version_tuple(version):
    match = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_newer_version(candidate, current):
    candidate_version = version_tuple(candidate)
    current_version = version_tuple(current)
    if candidate_version is None or current_version is None:
        return False
    length = max(len(candidate_version), len(current_version))
    return candidate_version + (0,) * (length - len(candidate_version)) > (
        current_version + (0,) * (length - len(current_version))
    )


def paths_overlap(a, b):
    real_a = os.path.realpath(os.path.abspath(a))
    real_b = os.path.realpath(os.path.abspath(b))
    try:
        common_path = os.path.commonpath((real_a, real_b))
    except ValueError:
        return False
    common_path = os.path.normcase(common_path)
    return common_path == os.path.normcase(real_a) or common_path == os.path.normcase(real_b)


def find_overlapping_source_destinations(sources, destinations):
    overlaps = []
    for source in sources:
        for dest in destinations:
            if paths_overlap(source, dest):
                overlaps.append((source, dest))
    return overlaps


def describe_os_error(action, path, error):
    detail = getattr(error, "strerror", None) or str(error)
    return f"[ERROR] {action} for {path}: {detail}"


# ----------------------------------------------------------------------------
# Core scanning / copying
# ----------------------------------------------------------------------------

def sanitize_folder_name(name):
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(" .")
    if safe_name in ("", ".", ".."):
        safe_name = "_"
    reserved_name = safe_name.split(".", 1)[0].rstrip(" .").upper()
    if reserved_name in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        safe_name = f"_{safe_name}"
    return safe_name


def ensure_output_path_within(destination_path, output_path):
    destination_root = os.path.realpath(os.path.abspath(destination_path))
    resolved_output = os.path.realpath(os.path.abspath(output_path))
    try:
        common_path = os.path.commonpath((destination_root, resolved_output))
    except ValueError as e:
        raise ValueError("Output path is on a different drive from its destination.") from e
    if (os.path.normcase(common_path) != os.path.normcase(destination_root)
            or os.path.normcase(resolved_output) == os.path.normcase(destination_root)):
        raise ValueError("Output path must be inside its destination folder.")


def extract_program_name(second_line):
    """
    Take characters from second line until first blank space.
    """
    program = second_line.strip().split(" ")[0]
    return sanitize_folder_name(program)


def title_matches(text, rule):
    expression = rule.get("expression", "")
    if not expression:
        return False
    match_type = rule.get("match_type", "contains")
    if match_type == "starts with":
        return text.startswith(expression)
    if match_type == "ends with":
        return text.endswith(expression)
    if match_type == "regex":
        try:
            return re.search(expression, text) is not None
        except re.error:
            return False
    return expression in text


def folder_created_time(path):
    """Creation timestamp of a folder (st_ctime is the creation time on Windows)."""
    st = os.stat(path)
    return getattr(st, "st_birthtime", st.st_ctime)


def copy_dataset(dataset_folder, destination_path, sample_name, nickname, program_name, copy_record=None, dry_run=False, stop_event=None):
    """Copy one dataset. Returns True if copied or would be copied, False if skipped."""
    sample_name = sanitize_folder_name(sample_name)
    nickname = (nickname or "").strip()
    if not nickname:
        raise ValueError("NMR nickname is required for copied datasets.")
    nickname = sanitize_folder_name(nickname)
    program_name = sanitize_folder_name(program_name)
    sample_folder = os.path.join(destination_path, sample_name)
    nickname_folder = os.path.join(sample_folder, nickname)
    try:
        created = folder_created_time(dataset_folder)
    except OSError as e:
        log(describe_os_error("Could not read creation time", dataset_folder, e))
        created = time.time()
    stamp = datetime.fromtimestamp(created).strftime("%Y-%m-%d_%H%M%S")
    program_folder = os.path.join(nickname_folder, f"{program_name}_{stamp}")
    ensure_output_path_within(destination_path, program_folder)

    if dry_run:
        log(f"[DRY RUN] Would copy dataset from: {dataset_folder}")
        log(f"[DRY RUN] To: {program_folder}")
        return True

    if stop_event is not None and stop_event.is_set():
        raise ScanAborted()

    try:
        os.makedirs(nickname_folder, exist_ok=True)
        ensure_output_path_within(destination_path, program_folder)
    except OSError as e:
        log(describe_os_error("Could not create destination folder", nickname_folder, e))
        raise

    if os.path.exists(program_folder):
        return False

    log(f"[MATCH] {sample_name} / {nickname} / {program_name}")
    log(f"Copying dataset from: {dataset_folder}")
    log(f"To: {program_folder}")
    try:
        def copy_file(src, dst):
            if stop_event is not None and stop_event.is_set():
                raise ScanAborted()
            return shutil.copy2(src, dst)

        shutil.copytree(dataset_folder, program_folder, copy_function=copy_file)
    except ScanAborted:
        if os.path.exists(program_folder):
            try:
                shutil.rmtree(program_folder)
            except OSError:
                pass
        raise
    except OSError as e:
        log(describe_os_error("Could not copy dataset to", program_folder, e))
        raise
    if copy_record is not None:
        copy_record({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sample": sample_name,
            "nickname": nickname,
            "program": program_name,
            "source": dataset_folder,
            "destination": program_folder,
        })
    log("Copy complete.")
    return True


def scan_folder(source_path, source_nickname, rules, stop_event=None, copy_record=None, progress_callback=None, dry_run=False):
    """Scan one source folder and apply all rules associated with it."""
    source_nickname = sanitize_folder_name((source_nickname or "").strip())
    if not source_nickname:
        log(f"[ERROR] Missing NMR nickname for watched folder: {source_path}")
        return
    active_rules = [
        r for r in rules
        if r.get("enabled", True) and r.get("expression") and r.get("destinations")
        and any(same_path(source_path, s) for s in r.get("sources", []))
    ]
    if not active_rules:
        log(f"No expressions/destinations associated with {source_path} - skipping.")
        return
    if not os.path.isdir(source_path):
        log(f"[ERROR] Source folder not reachable: {source_path}")
        return

    if dry_run:
        log(f"Starting dry-run scan in: {source_path}")
    else:
        log(f"Starting scan in: {source_path}")

    total_title_files = 0
    matches_found = 0
    copied_datasets = 0
    skipped_datasets = 0
    iterated_folders = 0
    seen_datasets = set()
    stack = [source_path]
    while stack:
        root = stack.pop()
        iterated_folders += 1
        if stop_event is not None and stop_event.is_set():
            log("Scan aborted.")
            return

        has_title = False
        child_dirs = []
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if stop_event is not None and stop_event.is_set():
                        log("Scan aborted.")
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_dirs.append(entry.path)
                        elif entry.name == "title":
                            has_title = True
                    except OSError:
                        continue
        except OSError as e:
            log(describe_os_error("Could not scan folder", root, e))
            continue

        if progress_callback is not None:
            progress_callback({"type": "update", "current": iterated_folders, "path": root})

        stack.extend(reversed(child_dirs))
        if not has_title:
            continue

        total_title_files += 1
        title_path = os.path.join(root, "title")

        try:
            with open(title_path, "r", encoding="utf-8", errors="ignore") as f:
                first_line = f.readline().strip()
                second_line = f.readline().strip()
        except OSError as e:
            log(describe_os_error("Could not read title file", title_path, e))
            continue

        matching_rules = [r for r in active_rules if title_matches(first_line, r)]
        if not matching_rules:
            continue

        # Go two folders up from .../pdata/1/title
        dataset_folder = os.path.abspath(os.path.join(root, "..", ".."))
        dataset_key = os.path.normcase(os.path.normpath(dataset_folder))
        # Several processings (pdata/1, pdata/2, ...) belong to the same dataset.
        if dataset_key in seen_datasets:
            continue
        seen_datasets.add(dataset_key)
        matches_found += 1

        destinations = []
        for rule in matching_rules:
            for dest in rule["destinations"]:
                if paths_overlap(source_path, dest):
                    log(f"[ERROR] Skipping overlapping source/destination pair: {source_path} -> {dest}")
                    continue
                if not any(same_path(dest, d) for d in destinations):
                    destinations.append(dest)
        for dest in destinations:
            if stop_event is not None and stop_event.is_set():
                log("Scan aborted.")
                return
            try:
                if copy_dataset(dataset_folder, dest, sanitize_folder_name(first_line), source_nickname, extract_program_name(second_line), copy_record=copy_record, dry_run=dry_run, stop_event=stop_event):
                    copied_datasets += 1
                else:
                    skipped_datasets += 1
            except ScanAborted:
                log("Scan aborted.")
                return
            except OSError as e:
                log(describe_os_error("Copying dataset to", dest, e))
            except Exception as e:
                log(f"[ERROR] Copying {dataset_folder} to {dest}: {e}")

    log(f"=== Scan complete: {source_path} ===")
    log(f"Title files scanned: {total_title_files} | Matching: {matches_found} | "
        f"Copied: {copied_datasets} | Skipped (already existing): {skipped_datasets}")


def count_tree_folders(source_path, stop_event=None, progress_callback=None):
    total = 0
    last_report = 0.0
    stack = [source_path]
    while stack:
        root = stack.pop()
        if stop_event is not None and stop_event.is_set():
            return None
        now = time.monotonic()
        if progress_callback is not None and (total == 0 or now - last_report >= 0.2):
            progress_callback(root)
            last_report = now
        total += 1
        try:
            with os.scandir(root) as entries:
                child_dirs = []
                for entry in entries:
                    if stop_event is not None and stop_event.is_set():
                        return None
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_dirs.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
        stack.extend(reversed(child_dirs))
    return total


# ----------------------------------------------------------------------------
# Background monitor
# ----------------------------------------------------------------------------

class Monitor:
    def __init__(self, get_config, record_copy=None):
        self.get_config = get_config
        self.record_copy = record_copy
        self.scan_lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.last_run = {}
        self.folder_total_cache = {}
        self.next_interval_run = {}
        self._stop = threading.Event()
        self._thread = None
        self.active_scan_cancel = None
        self.progress_sink = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.next_interval_run = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log("Monitoring started.")

    def stop(self):
        if not self.running:
            return
        self._stop.set()
        self.abort_active_scan()
        log("Monitoring stopping...")

    def abort_active_scan(self):
        if self.active_scan_cancel is not None:
            self.active_scan_cancel.set()
            log("Abort requested.")

    def next_run(self, path, interval_label):
        return self.next_interval_run.get(interval_label)

    def get_last_run(self, path):
        return self.last_run.get(os.path.normcase(os.path.normpath(path)))

    def scan_now(self, paths, dry_run=False):
        def worker():
            try:
                label = "Manual dry-run" if dry_run else "Manual scan"
                self._scan_paths(paths, label, dry_run=dry_run, respect_monitor_stop=False)
            except Exception as e:
                log(f"[ERROR] Manual scan failed unexpectedly: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _scan(self, path, nickname, rules, stop_event=None, progress_callback=None, dry_run=False):
        with self.scan_lock:
            try:
                scan_folder(
                    path,
                    nickname,
                    rules,
                    stop_event,
                    copy_record=self.record_copy,
                    progress_callback=progress_callback,
                    dry_run=dry_run,
                )
            except Exception as e:
                log(f"[ERROR] Scan of {path} failed: {e}")
            self.last_run[os.path.normcase(os.path.normpath(path))] = time.time()

    def _count_tree_folders_cached(self, path, stop_event=None, progress_callback=None):
        norm_path = os.path.normcase(os.path.normpath(path))
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            return 0
        cached = self.folder_total_cache.get(norm_path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        counted = count_tree_folders(path, stop_event=stop_event, progress_callback=progress_callback)
        if counted is not None:
            self.folder_total_cache[norm_path] = (stamp, counted)
        return counted

    def _interval_seconds(self, interval_label):
        return INTERVALS.get(interval_label, INTERVALS[DEFAULT_INTERVAL])

    def _next_boundary(self, now, interval_seconds):
        whole = int(now)
        return ((whole // interval_seconds) + 1) * interval_seconds

    def _scan_paths(self, paths, label, dry_run=False, respect_monitor_stop=True):
        if not paths:
            return False
        if not self.session_lock.acquire(blocking=False):
            log("A scan is already running.")
            return False

        cancel_event = threading.Event()
        self.active_scan_cancel = cancel_event
        aborted = False
        try:
            cfg = self.get_config()
            folder_nicknames = {}
            for folder in cfg["folders"]:
                folder_nicknames[os.path.normcase(os.path.normpath(folder["path"]))] = folder.get("nickname", "").strip()
            self.progress_sink_send({"type": "start", "mode": "indeterminate", "label": label, "path": label})
            self.progress_sink_send({"type": "nmr_start", "current": 0, "total": len(paths), "label": label})
            folder_totals = []
            overall_total = 0
            for path in paths:
                if cancel_event.is_set() or (respect_monitor_stop and self._stop.is_set()):
                    aborted = True
                    break
                nickname = folder_nicknames.get(os.path.normcase(os.path.normpath(path)), "")
                if not nickname:
                    log(f"[ERROR] Missing NMR nickname for watched folder: {path}")
                    folder_totals.append((path, 0))
                    continue
                folder_total = 0
                if os.path.isdir(path):
                    try:
                        counted = self._count_tree_folders_cached(
                            path,
                            stop_event=cancel_event,
                            progress_callback=lambda current_path, source=path: self.progress_sink_send({
                                "type": "counting",
                                "path": current_path,
                                "source": source,
                            }),
                        )
                        if counted is None:
                            aborted = True
                            break
                        folder_total = counted
                    except Exception as e:
                        log(f"[ERROR] Could not count folders in {path}: {e}")
                folder_totals.append((path, folder_total))
                overall_total += folder_total

            if aborted:
                return

            self.progress_sink_send({"type": "configure", "total": overall_total, "label": label, "path": label})
            completed = 0
            for index, path in enumerate(paths, start=1):
                if cancel_event.is_set() or (respect_monitor_stop and self._stop.is_set()):
                    aborted = True
                    break
                nickname = folder_nicknames.get(os.path.normcase(os.path.normpath(path)), "")
                self.progress_sink_send({"type": "nmr_update", "current": index, "total": len(paths), "label": label, "path": path})
                if not nickname:
                    continue
                folder_total = 0
                for candidate_path, candidate_total in folder_totals:
                    if same_path(candidate_path, path):
                        folder_total = candidate_total
                        break

                def progress_forward(payload, base=completed, total=overall_total):
                    if payload.get("type") != "update":
                        return
                    now = time.monotonic()
                    if now - progress_forward.last_report < 0.1:
                        return
                    progress_forward.last_report = now
                    current = base + payload.get("current", 0)
                    if current > total:
                        current = total
                    self.progress_sink_send({
                        "type": "update",
                        "current": current,
                        "total": total,
                        "path": payload.get("path", path),
                    })

                progress_forward.last_report = 0.0
                self.progress_sink_send({"type": "update", "current": completed, "total": overall_total, "path": path})
                self._scan(path, nickname, cfg["rules"], stop_event=cancel_event, progress_callback=progress_forward, dry_run=dry_run)
                completed += folder_total
                if cancel_event.is_set() or (respect_monitor_stop and self._stop.is_set()):
                    aborted = True
                    break
            self.progress_sink_send({"type": "update", "current": overall_total, "total": overall_total, "path": label})
            self.progress_sink_send({"type": "nmr_update", "current": len(paths), "total": len(paths), "label": label, "path": label})
        finally:
            self.active_scan_cancel = None
            self.progress_sink_send({"type": "done", "path": label})
            self.progress_sink_send({"type": "nmr_done", "path": label})
            if aborted or cancel_event.is_set() or (respect_monitor_stop and self._stop.is_set()):
                log("Scan stopped.")
            self.session_lock.release()
        return True

    def progress_sink_send(self, payload):
        if self.progress_sink is not None:
            self.progress_sink(payload)

    def _run(self):
        while not self._stop.is_set():
            cfg = self.get_config()
            now = time.time()
            grouped = {}
            for folder in cfg["folders"]:
                interval_label = folder.get("interval", DEFAULT_INTERVAL)
                grouped.setdefault(interval_label, []).append(folder["path"])

            for interval_label in list(self.next_interval_run.keys()):
                if interval_label not in grouped:
                    del self.next_interval_run[interval_label]

            for interval_label in grouped:
                if interval_label not in self.next_interval_run:
                    self.next_interval_run[interval_label] = self._next_boundary(now, self._interval_seconds(interval_label))

            due_intervals = [
                interval_label for interval_label, next_run in self.next_interval_run.items()
                if grouped.get(interval_label) and now >= next_run
            ]
            due_intervals.sort(key=lambda interval_label: self.next_interval_run[interval_label])

            if due_intervals:
                interval_label = due_intervals[0]
                paths = grouped.get(interval_label, [])
                if self._scan_paths(paths, f"Automatic scan ({interval_label}): {len(paths)} folder(s)"):
                    self.next_interval_run[interval_label] = self._next_boundary(time.time(), self._interval_seconds(interval_label))
                    continue

            self._stop.wait(1)
        log("Monitoring stopped.")


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"NMR Alfred v{CURRENT_VERSION}")
        self.geometry("1000x720")
        self.minsize(1000, 720)

        self.config_data = load_config()
        cleanup_missing_destinations(self.config_data)
        duplicate_folders, duplicate_nickname_rules = cleanup_duplicate_nickname_folders(self.config_data)
        removed_rules = cleanup_rules_without_sources(self.config_data)
        self.config_lock = threading.Lock()
        self.log_queue = queue.Queue()
        self.copy_queue = queue.Queue()
        self.update_queue = queue.Queue()
        self.scan_progress_queue = queue.Queue()
        self.copy_history = load_copy_history()
        self.copy_log_lock = threading.Lock()
        _log_listeners.append(self.log_queue.put)

        self.monitor = Monitor(self.get_config_snapshot, self.record_copy)
        self.monitor.progress_sink = self.scan_progress_queue.put
        self.selected_rule_index = None
        self.scan_progress_var = tk.IntVar(value=0)
        self.scan_progress_total = 0
        self.scan_progress_label_var = tk.StringVar(value="Idle")
        self.nmr_progress_var = tk.IntVar(value=0)
        self.nmr_progress_total = 0
        self.nmr_progress_label_var = tk.StringVar(value="NMR: 0/0")
        self.next_scan_var = tk.StringVar(value="Next scan: -")
        self.copy_search_var = tk.StringVar(value="")

        self._build_ui()
        self.refresh_folders()
        self.refresh_rules()
        self.refresh_copy_history()
        self.new_rule()
        startup_warnings = []
        if duplicate_folders:
            folder_names = "\n".join(
                f"- {folder.get('nickname', '')}: {folder['path']}" for folder in duplicate_folders
            )
            startup_warnings.append(
                f"{len(duplicate_folders)} folder(s) with duplicate nicknames and "
                f"{len(duplicate_nickname_rules)} associated rule(s) were removed:\n{folder_names}"
            )
        if removed_rules:
            rule_names = "\n".join(
                f"- {rule.get('expression') or '(unnamed rule)'}" for rule in removed_rules
            )
            startup_warnings.append(
                f"{len(removed_rules)} rule(s) without watched NMR folders were removed:\n{rule_names}"
            )
        if startup_warnings:
            self.after_idle(lambda: messagebox.showwarning(
                "Configuration cleaned",
                "\n\n".join(startup_warnings),
                parent=self,
            ))

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self._poll_log)
        self.after(50, self._poll_scan_progress)
        self.after(1000, self._tick)
        threading.Thread(target=self._check_for_updates, daemon=True).start()

    # ---------------------------------------------------------------- config
    def get_config_snapshot(self):
        with self.config_lock:
            return copy.deepcopy(self.config_data)

    def persist(self):
        with self.config_lock:
            try:
                save_config(self.config_data)
            except Exception as e:
                messagebox.showerror("Error", f"Could not save config:\n{e}")

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        tab_folders = ttk.Frame(notebook, padding=8)
        tab_rules = ttk.Frame(notebook, padding=8)
        tab_copies = ttk.Frame(notebook, padding=8)
        tab_about = ttk.Frame(notebook, padding=8)
        notebook.add(tab_folders, text="Watched NMR Folders")
        notebook.add(tab_rules, text="NMR Copy Rules")
        notebook.add(tab_copies, text="Copied Files")
        notebook.add(tab_about, text="About")
        paned = ttk.PanedWindow(tab_folders, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)
        folders_frame = ttk.Frame(paned)
        paned.add(folders_frame, weight=3)
        self._build_folders_tab(folders_frame)
        self._build_rules_tab(tab_rules)
        self._build_copies_tab(tab_copies)
        ttk.Label(tab_about, text="(c) 2026 Johannes Schneider, www.johannes-schneider.de, MIT License").pack(pady=(24, 12))
        ttk.Button(
            tab_about, text="Check for Updates", command=self.open_releases_page
        ).pack()

        log_frame = ttk.LabelFrame(paned, text="Log", padding=4)
        paned.add(log_frame, weight=1)
        self.log_text = tk.Text(log_frame, height=8, state=tk.DISABLED, wrap=tk.WORD)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.no_rules_log_label = ttk.Label(log_frame, text="Specify NMR copy rules")
        self.no_rules_log_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        bar = ttk.Frame(tab_folders, padding=(0, 6, 0, 0))
        bar.pack(fill=tk.X)
        self.start_monitor_btn = ttk.Button(bar, text="Start Monitoring", command=self.start_monitoring)
        self.start_monitor_btn.pack(side=tk.LEFT)
        self.stop_monitor_btn = ttk.Button(bar, text="Stop Monitoring", command=self.stop_monitoring, state=tk.DISABLED)
        self.stop_monitor_btn.pack(side=tk.LEFT, padx=(6, 12))
        self.start_manual_scan_btn = ttk.Button(bar, text="Start Manual Scan", command=self.scan_all_now)
        self.start_manual_scan_btn.pack(side=tk.LEFT)
        self.stop_manual_scan_btn = ttk.Button(bar, text="Stop Manual Scan", command=self.abort_scan, state=tk.DISABLED)
        self.stop_manual_scan_btn.pack(side=tk.LEFT, padx=6)
        self.simulate_scan_all_btn = ttk.Button(bar, text="Simulate Scan", command=self.simulate_scan_all_now)
        self.simulate_scan_all_btn.pack(side=tk.LEFT, padx=6)
        self.status_var = tk.StringVar(value="Monitoring stopped")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.LEFT, padx=12)
        ttk.Label(bar, textvariable=self.next_scan_var).pack(side=tk.LEFT, padx=12)
        ttk.Button(bar, text="Clear Log", command=self.clear_log).pack(side=tk.RIGHT)

    def _build_folders_tab(self, parent):
        columns = ("path", "nickname", "interval", "last", "next")
        self.folder_tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="extended")
        for col, text, width in (("path", "Folder", 460), ("nickname", "Nickname", 140), ("interval", "Check every", 90),
                                 ("last", "Last scan", 140), ("next", "Next scan", 140)):
            self.folder_tree.heading(col, text=text, anchor=tk.W)
            self.folder_tree.column(col, width=width, stretch=(col == "path"))
        self.folder_tree.pack(fill=tk.BOTH, expand=True)
        self.folder_tree.bind("<<TreeviewSelect>>", self.on_folder_select)
        self.folder_tree.bind("<Double-1>", self.open_selected_watch_folder)

        buttons = ttk.Frame(parent, padding=(0, 6, 0, 0))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Add NMR Folder", command=self.open_add_folder_dialog).pack(side=tk.LEFT)
        self.edit_folder_btn = ttk.Button(
            buttons, text="Edit Selected NMR Folder", command=self.open_edit_selected_folder_dialog, state=tk.DISABLED
        )
        self.edit_folder_btn.pack(side=tk.LEFT, padx=4)
        self.remove_folder_btn = ttk.Button(
            buttons, text="Remove Selected NMR Folder", command=self.remove_folders, state=tk.DISABLED
        )
        self.remove_folder_btn.pack(side=tk.LEFT)
        self.scan_selected_btn = ttk.Button(buttons, text="Scan Selected NMR Folder", command=self.scan_selected_now)
        self.scan_selected_btn.pack(side=tk.RIGHT)
        self.simulate_scan_selected_btn = ttk.Button(
            buttons, text="Simulate Selected NMR Folder", command=self.simulate_scan_selected_now
        )
        self.simulate_scan_selected_btn.pack(side=tk.RIGHT, padx=6)

        progress = ttk.LabelFrame(parent, text="Scan progress", padding=8)
        progress.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(progress, textvariable=self.nmr_progress_label_var).pack(anchor="w")
        self.nmr_progress_bar = ttk.Progressbar(progress, mode="determinate", maximum=1, variable=self.nmr_progress_var)
        self.nmr_progress_bar.pack(fill=tk.X, pady=(6, 8))
        ttk.Label(progress, textvariable=self.scan_progress_label_var).pack(anchor="w")
        self.scan_progress_bar = ttk.Progressbar(progress, mode="determinate", maximum=1, variable=self.scan_progress_var)
        self.scan_progress_bar.pack(fill=tk.X, pady=(6, 8))
        self.scan_progress_percent_var = tk.StringVar(value="0%")
        ttk.Label(progress, textvariable=self.scan_progress_percent_var).pack(anchor="w")
        self.scan_abort_btn = ttk.Button(progress, text="Abort scan", command=self.abort_scan, state=tk.DISABLED)
        self.scan_abort_btn.pack(anchor="e")

    def _build_rules_tab(self, parent):
        columns = ("enabled", "expression", "match", "sources", "destinations")
        rule_tree_frame = ttk.Frame(parent)
        rule_tree_frame.pack(fill=tk.BOTH, expand=True)
        self.rule_tree = ttk.Treeview(rule_tree_frame, columns=columns, show="headings", selectmode="browse", height=6)
        for col, text, width in (("enabled", "Enabled", 60), ("expression", "Expression", 160), ("match", "Match", 90),
                                 ("sources", "Watched NMR Folders", 330), ("destinations", "Copy to", 330)):
            self.rule_tree.heading(col, text=text, anchor=tk.W)
            self.rule_tree.column(col, width=width, anchor=tk.CENTER if col == "enabled" else tk.W)
        self.rule_tree.pack(fill=tk.BOTH, expand=True)
        self.no_folders_label = ttk.Label(rule_tree_frame, text="Add NMR folders to watch first")
        self.no_folders_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.rule_tree.bind("<<TreeviewSelect>>", self.on_rule_select)
        self.rule_tree.bind("<Button-1>", self.on_rule_click)
        self.rule_tree.bind("<Delete>", self.delete_rule)

        rule_buttons = ttk.Frame(parent, padding=(0, 8, 0, 0))
        rule_buttons.pack(fill=tk.X)
        self.add_rule_btn = ttk.Button(rule_buttons, text="Add rule", command=self.open_add_rule_dialog)
        self.add_rule_btn.pack(side=tk.LEFT)
        self.edit_rule_btn = ttk.Button(rule_buttons, text="Edit Rule", command=self.open_edit_selected_rule_dialog)
        self.edit_rule_btn.pack(side=tk.LEFT, padx=4)
        self.delete_rule_btn = ttk.Button(rule_buttons, text="Delete Rule", command=self.delete_rule)
        self.delete_rule_btn.pack(side=tk.LEFT)

    def _build_copies_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Current weekly log file:").pack(side=tk.LEFT)
        self.copy_log_path_var = tk.StringVar(value=os.path.basename(current_copy_log_path()))
        ttk.Label(top, textvariable=self.copy_log_path_var).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Reload From Disk", command=self.reload_copy_history).pack(side=tk.RIGHT)

        search_bar = ttk.Frame(parent)
        search_bar.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(search_bar, text="Search copies:").pack(side=tk.LEFT)
        search_entry = ttk.Entry(search_bar, textvariable=self.copy_search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        search_entry.bind("<KeyRelease>", self.on_copy_search_changed)
        ttk.Button(search_bar, text="Clear", command=self.clear_copy_search).pack(side=tk.RIGHT)

        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        columns = ("timestamp", "sample", "nickname", "program", "source", "destination")
        self.copy_tree = ttk.Treeview(frame, columns=columns, show="headings")
        for col, text, width in (
            ("timestamp", "Copied at", 150),
            ("sample", "Sample", 180),
            ("nickname", "Nickname", 140),
            ("program", "Program", 110),
            ("source", "Source folder", 260),
            ("destination", "Copied to", 320),
        ):
            self.copy_tree.heading(col, text=text, anchor=tk.W)
            self.copy_tree.column(col, width=width, stretch=(col in ("source", "destination")))
        scroll = ttk.Scrollbar(frame, command=self.copy_tree.yview)
        self.copy_tree.configure(yscrollcommand=scroll.set)
        self.copy_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.copy_tree.bind("<Double-1>", self.open_selected_copied_folder)

    def _copy_row(self, entry):
        return (
            entry.get("timestamp", ""),
            entry.get("sample", ""),
            entry.get("nickname", ""),
            entry.get("program", ""),
            entry.get("source", ""),
            entry.get("destination", ""),
        )

    def _matches_copy_search(self, entry, query):
        query = query.strip().lower()
        if not query:
            return True
        haystack = " ".join([
            entry.get("timestamp", ""),
            entry.get("sample", ""),
            entry.get("nickname", ""),
            entry.get("program", ""),
            entry.get("source", ""),
            entry.get("destination", ""),
        ]).lower()
        return all(term in haystack for term in query.split())

    def _filtered_copy_history(self):
        return [entry for entry in self.copy_history if self._matches_copy_search(entry, self.copy_search_var.get())]

    # ---------------------------------------------------------- folders tab
    def refresh_folders(self):
        selected = self.folder_tree.selection()
        self.folder_tree.delete(*self.folder_tree.get_children())
        for i, folder in enumerate(self.config_data["folders"]):
            self.folder_tree.insert("", tk.END, iid=str(i), values=self._folder_row(folder))
        for iid in selected:
            if self.folder_tree.exists(iid):
                self.folder_tree.selection_add(iid)
        self.on_folder_select()

    def _folder_row(self, folder):
        fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"
        last = self.monitor.get_last_run(folder["path"])
        if self.monitor.running:
            nxt = self.monitor.next_run(folder["path"], folder.get("interval"))
            nxt_text = fmt(nxt) if nxt else "pending"
        else:
            nxt_text = "-"
        return (folder["path"], folder.get("nickname", ""), folder.get("interval", DEFAULT_INTERVAL), fmt(last), nxt_text)

    def on_folder_select(self, _event=None):
        state = tk.NORMAL if self.folder_tree.selection() else tk.DISABLED
        self.edit_folder_btn.configure(state=state)
        self.remove_folder_btn.configure(state=state)
        self._update_folder_action_states()

    def _update_folder_action_states(self):
        has_folders = bool(self.config_data["folders"])
        has_rules = bool(self.config_data["rules"])
        state = tk.NORMAL if has_folders else tk.DISABLED
        scan_state = tk.NORMAL if has_folders and has_rules else tk.DISABLED
        self.simulate_scan_all_btn.configure(state=scan_state)
        self.scan_selected_btn.configure(state=scan_state)
        self.simulate_scan_selected_btn.configure(state=scan_state)
        self.start_monitor_btn.configure(
            state=tk.NORMAL if has_folders and has_rules and not self.monitor.running else tk.DISABLED
        )
        self.start_manual_scan_btn.configure(
            state=tk.NORMAL if has_folders and has_rules and not self.monitor.session_lock.locked() else tk.DISABLED
        )
        self.add_rule_btn.configure(state=state)
        self.edit_rule_btn.configure(state=state)
        self.delete_rule_btn.configure(state=state)
        if has_folders:
            self.no_folders_label.place_forget()
        else:
            self.no_folders_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        if has_folders and not has_rules:
            self.no_rules_log_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
            self.no_rules_log_label.lift()
        else:
            self.no_rules_log_label.place_forget()

    def open_selected_watch_folder(self, event=None):
        item_id = self.folder_tree.identify_row(event.y) if event is not None else self.folder_tree.selection()
        if isinstance(item_id, tuple):
            item_id = item_id[0] if item_id else ""
        if not item_id:
            return
        folder = self.config_data["folders"][int(item_id)]
        path = folder.get("path", "")
        if not path:
            return
        if not os.path.exists(path):
            messagebox.showerror("Folder not found", f"Cannot open folder because it does not exist:\n{path}")
            return
        try:
            os.startfile(path)
        except OSError as e:
            log(describe_os_error("Could not open watched folder", path, e))

    def open_add_folder_dialog(self):
        self._open_folder_dialog(title="Add NMR folder", add_mode=True)

    def open_edit_selected_folder_dialog(self):
        sel = self.folder_tree.selection()
        if len(sel) != 1:
            messagebox.showinfo("Select folder", "Select exactly one folder to edit.")
            return
        self._open_folder_dialog(title="Edit folder", add_mode=False, folder_index=int(sel[0]))

    def _center_dialog(self, dialog, width, height):
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 2
        dialog.geometry(f"{width}x{height}+{x}+{y}")

    def _open_folder_dialog(self, title, add_mode, folder_index=None):
        dialog = tk.Toplevel(self)
        dialog.withdraw()
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(True, False)

        outer = ttk.Frame(dialog, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="Folder").grid(row=0, column=0, sticky="w")
        path_var = tk.StringVar()
        auto_nickname = {"value": ""}
        ttk.Entry(outer, textvariable=path_var, width=48).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=6
        )

        def browse():
            path = filedialog.askdirectory(parent=dialog, title="Select folder")
            if path:
                path_var.set(os.path.normpath(path))

        ttk.Button(outer, text="Browse", command=browse).grid(row=0, column=4)

        ttk.Label(outer, text="Nickname").grid(row=1, column=0, sticky="w", pady=(6, 0))
        nickname_var = tk.StringVar()
        ttk.Entry(outer, textvariable=nickname_var).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))

        ttk.Label(outer, text="Check every").grid(row=1, column=2, sticky="w", padx=(6, 0), pady=(6, 0))
        interval_var = tk.StringVar(value=DEFAULT_INTERVAL)
        ttk.Combobox(outer, textvariable=interval_var, values=list(INTERVALS), state="readonly", width=12).grid(
            row=1, column=3, columnspan=2, sticky="e", padx=(6, 0), pady=(6, 0)
        )

        if folder_index is not None and 0 <= folder_index < len(self.config_data["folders"]):
            folder = self.config_data["folders"][folder_index]
            path_var.set(folder["path"])
            nickname_var.set(folder.get("nickname", ""))
            interval_var.set(folder.get("interval", DEFAULT_INTERVAL))

        def update_nickname_from_path(*_):
            extracted_nickname = extract_nmr_nickname(path_var.get())
            current_nickname = nickname_var.get()
            if extracted_nickname and (
                not current_nickname or current_nickname == auto_nickname["value"]
            ):
                nickname = unique_nmr_nickname(
                    extracted_nickname, self.config_data["folders"], folder_index
                )
                nickname_var.set(nickname)
                auto_nickname["value"] = nickname

        path_var.trace_add("write", update_nickname_from_path)

        button_row = ttk.Frame(outer)
        button_row.grid(row=2, column=0, columnspan=5, sticky="e", pady=(8, 0))

        def save_folder():
            path = path_var.get().strip()
            if not path:
                messagebox.showwarning("Missing folder", "Please select a folder.", parent=dialog)
                return
            nickname = nickname_var.get().strip()
            if not nickname:
                messagebox.showwarning("Missing nickname", "Please enter an NMR nickname.", parent=dialog)
                return
            if any(
                i != folder_index
                and str(folder.get("nickname", "")).strip().casefold() == nickname.casefold()
                for i, folder in enumerate(self.config_data["folders"])
            ):
                messagebox.showwarning(
                    "Duplicate nickname",
                    "Each watched NMR folder must have a unique nickname.",
                    parent=dialog,
                )
                return
            if add_mode:
                if any(same_path(path, f["path"]) for f in self.config_data["folders"]):
                    messagebox.showwarning("Duplicate", "This folder is already being watched.", parent=dialog)
                    return
                with self.config_lock:
                    self.config_data["folders"].append({"path": path, "nickname": nickname, "interval": interval_var.get()})
            else:
                if folder_index is None:
                    return
                if any(same_path(path, f["path"]) for i, f in enumerate(self.config_data["folders"]) if i != folder_index):
                    messagebox.showwarning("Duplicate", "This folder is already being watched.", parent=dialog)
                    return
                with self.config_lock:
                    folder = self.config_data["folders"][folder_index]
                    old_path = folder["path"]
                    folder["path"] = path
                    folder["nickname"] = nickname
                    folder["interval"] = interval_var.get()
                    for rule in self.config_data["rules"]:
                        rule["sources"] = [path if same_path(s, old_path) else s for s in rule.get("sources", [])]
            self.persist()
            self.refresh_folders()
            self.refresh_rules()
            dialog.destroy()

        ttk.Button(button_row, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(button_row, text="Save", command=save_folder).pack(side=tk.RIGHT, padx=(0, 6))

        dialog.update_idletasks()
        width = int(dialog.winfo_reqwidth() * 1.33)
        height = dialog.winfo_reqheight()
        dialog.minsize(width, height)
        self._center_dialog(dialog, width, height)
        dialog.deiconify()
        dialog.grab_set()

    def remove_folders(self):
        sel = self.folder_tree.selection()
        if not sel:
            return
        indices = sorted((int(i) for i in sel), reverse=True)
        paths_to_remove = [
            self.config_data["folders"][i]["path"]
            for i in indices
            if 0 <= i < len(self.config_data["folders"])
        ]
        affected_rules = [
            rule for rule in self.config_data["rules"]
            if any(
                same_path(source, path)
                for source in rule.get("sources", [])
                for path in paths_to_remove
            )
        ]
        message = f"Remove {len(paths_to_remove)} watched folder(s)?"
        if affected_rules:
            message += (
                f"\n\n{len(affected_rules)} associated NMR rule(s) will also be deleted."
            )
        if not messagebox.askyesno("Remove", message, icon="warning", parent=self):
            return
        with self.config_lock:
            removed = [self.config_data["folders"].pop(i)["path"] for i in indices]
            self.config_data["rules"] = [
                rule for rule in self.config_data["rules"]
                if not any(
                    same_path(source, path)
                    for source in rule.get("sources", [])
                    for path in removed
                )
            ]
        self.persist()
        self.folder_tree.selection_set(())
        self.refresh_folders()
        self.refresh_rules()

    def scan_selected_now(self):
        paths = [self.config_data["folders"][int(i)]["path"] for i in self.folder_tree.selection()]
        if not paths:
            messagebox.showinfo("Select folder", "Select one or more folders to scan.")
            return
        log(f"Manual scan requested for {len(paths)} folder(s).")
        self.monitor.scan_now(paths)

    def simulate_scan_selected_now(self):
        paths = [self.config_data["folders"][int(i)]["path"] for i in self.folder_tree.selection()]
        if not paths:
            messagebox.showinfo("Select folder", "Select one or more folders to scan.")
            return
        log(f"Manual dry-run requested for {len(paths)} folder(s).")
        self.monitor.scan_now(paths, dry_run=True)

    def scan_all_now(self):
        paths = [f["path"] for f in self.config_data["folders"]]
        if paths:
            log(f"Manual scan requested for {len(paths)} watched folder(s).")
            self.monitor.scan_now(paths)

    def simulate_scan_all_now(self):
        paths = [f["path"] for f in self.config_data["folders"]]
        if paths:
            log(f"Manual dry-run requested for {len(paths)} watched folder(s).")
            self.monitor.scan_now(paths, dry_run=True)

    def _show_scan_progress(self, total, label="Scanning"):
        self.scan_progress_total = total if total is not None else 0
        self.scan_progress_var.set(0)
        self.scan_progress_bar.stop()
        self.scan_progress_bar.configure(mode="determinate", maximum=max(total if total is not None else 1, 1), value=0)
        if total is None:
            self.scan_progress_label_var.set(f"{label}: counting folders...")
            self.scan_progress_percent_var.set("0%")
        else:
            self.scan_progress_label_var.set(f"{label}: 0 of {total} folders")
            self.scan_progress_percent_var.set("0%")
        self.scan_abort_btn.configure(state=tk.NORMAL)
        self._update_status()
        self.update_idletasks()

    def _show_nmr_progress(self, current, total):
        self.nmr_progress_total = total if total is not None else 0
        self.nmr_progress_var.set(current if current is not None else 0)
        self.nmr_progress_bar.stop()
        self.nmr_progress_bar.configure(mode="determinate", maximum=max(total if total is not None else 1, 1), value=current if current is not None else 0)
        if current is None or total is None or total <= 0:
            self.nmr_progress_label_var.set("NMR: counting...")
        else:
            self.nmr_progress_label_var.set(f"NMR: {current}/{total}")
        self.update_idletasks()

    def _update_counting_progress(self, path, source):
        if path:
            self.scan_progress_label_var.set(f"Counting folders in {source}: {path}")
        else:
            self.scan_progress_label_var.set(f"Counting folders in {source}...")
        self.scan_progress_percent_var.set("Counting...")
        self.update_idletasks()

    def _update_scan_progress(self, current, total, path):
        self.scan_progress_total = total
        self.scan_progress_var.set(current)
        if total is not None and total > 0:
            self.scan_progress_bar.stop()
            self.scan_progress_bar.configure(mode="determinate", maximum=max(total, 1), value=current)
            self.scan_progress_label_var.set(f"Scanning {current} of {total} folders: {path}")
            self.scan_progress_percent_var.set(f"{int((float(current) / float(total)) * 100)}%")
        else:
            self.scan_progress_bar.configure(mode="determinate", value=current)
            self.scan_progress_label_var.set(f"Scanning: {path}")
            self.scan_progress_percent_var.set(f"{current}")
        self.update_idletasks()

    def _close_scan_progress(self):
        self.scan_progress_total = 0
        self.scan_progress_var.set(0)
        self.scan_progress_label_var.set("Idle")
        self.scan_progress_percent_var.set("0%")
        self.scan_progress_bar.stop()
        self.scan_progress_bar.configure(mode="determinate")
        self.scan_progress_bar.configure(maximum=1, value=0)
        self.nmr_progress_total = 0
        self.nmr_progress_var.set(0)
        self.nmr_progress_label_var.set("NMR: 0/0")
        self.nmr_progress_bar.stop()
        self.nmr_progress_bar.configure(mode="determinate")
        self.nmr_progress_bar.configure(maximum=1, value=0)
        self.scan_abort_btn.configure(state=tk.DISABLED)
        self._update_status()
        self.update_idletasks()

    def abort_scan(self):
        self.monitor.abort_active_scan()

    def record_copy(self, entry):
        try:
            with self.copy_log_lock:
                append_copy_entry_to_log(entry)
        except Exception as e:
            log(f"[ERROR] Could not write copy log entry: {e}")
        self.copy_queue.put(entry)

    def open_selected_copied_folder(self, event=None):
        if not self.copy_tree.get_children():
            return
        item_id = self.copy_tree.identify_row(event.y) if event is not None else self.copy_tree.selection()
        if isinstance(item_id, tuple):
            item_id = item_id[0] if item_id else ""
        if not item_id:
            return
        values = self.copy_tree.item(item_id, "values")
        if len(values) < 6:
            return
        folder_path = values[5]
        if not folder_path:
            return
        if not os.path.exists(folder_path):
            messagebox.showerror("Folder not found", f"Cannot open folder because it does not exist:\n{folder_path}")
            return
        try:
            os.startfile(folder_path)
        except OSError as e:
            log(describe_os_error("Could not open copied folder", folder_path, e))

    def refresh_copy_history(self):
        self.copy_tree.delete(*self.copy_tree.get_children())
        filtered = self._filtered_copy_history()
        for entry in filtered:
            self.copy_tree.insert("", tk.END, values=self._copy_row(entry))
        if filtered:
            self.copy_tree.see(self.copy_tree.get_children()[-1])

    def reload_copy_history(self):
        self.copy_log_path_var.set(os.path.basename(current_copy_log_path()))
        self.copy_history = load_copy_history()
        self.refresh_copy_history()

    def on_copy_search_changed(self, _event=None):
        self.refresh_copy_history()

    def clear_copy_search(self):
        self.copy_search_var.set("")
        self.refresh_copy_history()

    # ------------------------------------------------------------ rules tab
    def refresh_rules(self):
        self.rule_tree.delete(*self.rule_tree.get_children())
        for i, rule in enumerate(self.config_data["rules"]):
            self.rule_tree.insert("", tk.END, iid=str(i), values=(
                "\u2611" if rule.get("enabled", True) else "\u2610",
                rule.get("expression", ""),
                rule.get("match_type", "contains"),
                "; ".join(rule.get("sources", [])),
                "; ".join(rule.get("destinations", [])),
            ))
        if self.selected_rule_index is not None and self.rule_tree.exists(str(self.selected_rule_index)):
            self.rule_tree.selection_set(str(self.selected_rule_index))
        self._update_folder_action_states()

    def on_rule_click(self, event):
        if self.rule_tree.identify_column(event.x) != "#1":
            return
        iid = self.rule_tree.identify_row(event.y)
        if not iid:
            return
        rule_index = int(iid)
        if not 0 <= rule_index < len(self.config_data["rules"]):
            return
        self.selected_rule_index = rule_index
        with self.config_lock:
            rule = self.config_data["rules"][rule_index]
            rule["enabled"] = not rule.get("enabled", True)
        self.persist()
        self.refresh_rules()
        return "break"

    def _refresh_rule_sources_list(self, selected_sources=None):
        if not hasattr(self, "rule_sources_list"):
            return
        if selected_sources is None:
            selected_sources = [self.rule_sources_list.get(i) for i in self.rule_sources_list.curselection()]
        self.rule_sources_list.delete(0, tk.END)
        for i, folder in enumerate(self.config_data["folders"]):
            self.rule_sources_list.insert(tk.END, folder["path"])
            if any(same_path(folder["path"], s) for s in selected_sources):
                self.rule_sources_list.selection_set(i)

    def on_rule_select(self, _event=None):
        sel = self.rule_tree.selection()
        if not sel:
            self.selected_rule_index = None
            return
        self.selected_rule_index = int(sel[0])

    def new_rule(self):
        self.selected_rule_index = None
        self.rule_tree.selection_set(())

    def _create_rule_source_tree(self, parent, selected_sources):
        source_frame = ttk.Frame(parent)
        source_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        source_frame.rowconfigure(0, weight=1)
        source_frame.columnconfigure(0, weight=1)

        source_tree = ttk.Treeview(
            source_frame, columns=("selected", "path"), show="headings", selectmode="none"
        )
        source_tree.heading("selected", text="Use", anchor=tk.W)
        source_tree.heading("path", text="Watched folder", anchor=tk.W)
        source_tree.column("selected", width=42, minwidth=42, stretch=False, anchor=tk.CENTER)
        source_tree.column("path", width=680, minwidth=200, stretch=True)

        vertical_scroll = ttk.Scrollbar(source_frame, orient=tk.VERTICAL, command=source_tree.yview)
        horizontal_scroll = ttk.Scrollbar(source_frame, orient=tk.HORIZONTAL, command=source_tree.xview)
        source_tree.configure(yscrollcommand=vertical_scroll.set, xscrollcommand=horizontal_scroll.set)
        source_tree.grid(row=0, column=0, sticky="nsew")
        vertical_scroll.grid(row=0, column=1, sticky="ns")
        horizontal_scroll.grid(row=1, column=0, sticky="ew")

        for index, folder in enumerate(self.config_data["folders"]):
            path = folder["path"]
            checked = any(same_path(path, selected) for selected in selected_sources)
            source_tree.insert(
                "", tk.END, iid=str(index),
                values=("\u2611" if checked else "\u2610", path),
            )
        source_tree.bind("<Button-1>", lambda event: self._toggle_rule_source(event, source_tree))
        return source_tree

    def _toggle_rule_source(self, event, source_tree):
        if source_tree.identify_column(event.x) != "#1":
            return
        item = source_tree.identify_row(event.y)
        if not item:
            return
        checkbox = source_tree.set(item, "selected")
        source_tree.set(item, "selected", "\u2610" if checkbox == "\u2611" else "\u2611")
        return "break"

    @staticmethod
    def _selected_rule_sources(source_tree):
        return [
            source_tree.set(item, "path")
            for item in source_tree.get_children()
            if source_tree.set(item, "selected") == "\u2611"
        ]

    def open_add_rule_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.withdraw()
        dialog.title("Add NMR Rule")
        dialog.transient(self)
        dialog.minsize(760, 600)

        outer = ttk.Frame(dialog, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        top = ttk.LabelFrame(outer, text="Rule parameters", padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Expression").grid(row=0, column=0, sticky="w")
        expression_var = tk.StringVar()
        ttk.Entry(top, textvariable=expression_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Label(top, text="Match type").grid(row=1, column=0, sticky="w", pady=(6, 0))
        match_type_var = tk.StringVar(value="contains")
        ttk.Combobox(top, textvariable=match_type_var, values=MATCH_TYPES, state="readonly", width=16).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        lists = ttk.Frame(outer)
        lists.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        lists.columnconfigure(0, weight=1)
        lists.rowconfigure(1, weight=1)
        lists.rowconfigure(3, weight=1)

        ttk.Label(lists, text="Watched folders for this rule").grid(row=0, column=0, sticky="w")
        selected_sources = [
            self.config_data["folders"][int(i)]["path"]
            for i in self.folder_tree.selection()
            if 0 <= int(i) < len(self.config_data["folders"])
        ]
        source_tree = self._create_rule_source_tree(lists, selected_sources)

        ttk.Label(lists, text="Copy results to").grid(row=2, column=0, sticky="w", pady=(6, 0))
        dest_list = tk.Listbox(lists, selectmode=tk.EXTENDED, exportselection=False)
        dest_list.grid(row=3, column=0, sticky="nsew", padx=(0, 6))

        button_row = ttk.Frame(lists)
        button_row.grid(row=4, column=0, sticky="w", pady=(4, 0))

        def add_destination():
            path = filedialog.askdirectory(parent=dialog, title="Select destination folder")
            if not path:
                return
            path = os.path.normpath(path)
            if not any(same_path(path, d) for d in dest_list.get(0, tk.END)):
                dest_list.insert(tk.END, path)

        def remove_destination():
            for i in reversed(dest_list.curselection()):
                dest_list.delete(i)

        ttk.Button(button_row, text="Add Destination Folder", command=add_destination).pack(side=tk.LEFT)
        ttk.Button(button_row, text="Remove Destination Folder", command=remove_destination).pack(side=tk.LEFT, padx=4)

        action_bar = ttk.Frame(outer)
        action_bar.grid(row=2, column=0, sticky="e", pady=(10, 0))

        def save_new_rule():
            expression = expression_var.get().strip()
            if not expression:
                messagebox.showwarning("Missing expression", "Please enter an expression.", parent=dialog)
                return
            if match_type_var.get() == "regex":
                try:
                    re.compile(expression)
                except re.error as e:
                    messagebox.showerror("Invalid regex", str(e), parent=dialog)
                    return
            sources = self._selected_rule_sources(source_tree)
            destinations = list(dest_list.get(0, tk.END))
            if not sources:
                messagebox.showwarning("No folders", "Select at least one watched folder.", parent=dialog)
                return
            if not destinations:
                messagebox.showwarning("No destination", "Add at least one destination folder.", parent=dialog)
                return
            overlaps = find_overlapping_source_destinations(sources, destinations)
            if overlaps:
                source, dest = overlaps[0]
                messagebox.showerror(
                    "Overlapping folders",
                    "Source and destination folders must not overlap.\n\n"
                    f"Source: {source}\nDestination: {dest}",
                    parent=dialog,
                )
                return
            with self.config_lock:
                self.config_data["rules"].append({
                    "expression": expression,
                    "match_type": match_type_var.get(),
                    "sources": sources,
                    "destinations": destinations,
                    "enabled": True,
                })
            self.persist()
            self.refresh_rules()
            dialog.destroy()

        ttk.Button(action_bar, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(action_bar, text="Add rule", command=save_new_rule).pack(side=tk.RIGHT, padx=(0, 6))

        dialog.update_idletasks()
        self._center_dialog(dialog, 820, 680)
        dialog.deiconify()
        dialog.grab_set()

    def open_edit_selected_rule_dialog(self):
        sel = self.rule_tree.selection()
        if len(sel) != 1:
            messagebox.showinfo("Select rule", "Select exactly one rule to edit.")
            return
        rule_index = int(sel[0])
        if rule_index < 0 or rule_index >= len(self.config_data["rules"]):
            return
        rule = self.config_data["rules"][rule_index]

        dialog = tk.Toplevel(self)
        dialog.title("Edit rule")
        dialog.transient(self)
        dialog.geometry("820x760")
        dialog.minsize(760, 620)

        outer = ttk.Frame(dialog, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        top = ttk.LabelFrame(outer, text="Rule parameters", padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Expression").grid(row=0, column=0, sticky="w")
        expression_var = tk.StringVar(value=rule.get("expression", ""))
        ttk.Entry(top, textvariable=expression_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Label(top, text="Match type").grid(row=1, column=0, sticky="w", pady=(6, 0))
        match_type_var = tk.StringVar(value=rule.get("match_type", "contains"))
        ttk.Combobox(top, textvariable=match_type_var, values=MATCH_TYPES, state="readonly", width=16).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        enabled_var = tk.BooleanVar(value=rule.get("enabled", True))
        ttk.Checkbutton(top, text="Enabled", variable=enabled_var).grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        lists = ttk.Frame(outer)
        lists.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        lists.columnconfigure(0, weight=1)
        lists.rowconfigure(1, weight=1)
        lists.rowconfigure(3, weight=1)

        ttk.Label(lists, text="Watched folders for this rule").grid(row=0, column=0, sticky="w")
        source_tree = self._create_rule_source_tree(lists, rule.get("sources", []))

        ttk.Label(lists, text="Copy results to").grid(row=2, column=0, sticky="w", pady=(6, 0))
        dest_list = tk.Listbox(lists, selectmode=tk.EXTENDED, exportselection=False)
        dest_list.grid(row=3, column=0, sticky="nsew", padx=(0, 6))

        button_row = ttk.Frame(lists)
        button_row.grid(row=4, column=0, sticky="w", pady=(4, 0))

        def add_destination():
            path = filedialog.askdirectory(parent=dialog, title="Select destination folder")
            if not path:
                return
            path = os.path.normpath(path)
            if not any(same_path(path, d) for d in dest_list.get(0, tk.END)):
                dest_list.insert(tk.END, path)

        def remove_destination():
            for i in reversed(dest_list.curselection()):
                dest_list.delete(i)

        ttk.Button(button_row, text="Add destination...", command=add_destination).pack(side=tk.LEFT)
        ttk.Button(button_row, text="Remove destination", command=remove_destination).pack(side=tk.LEFT, padx=4)

        for dest in rule.get("destinations", []):
            dest_list.insert(tk.END, dest)

        action_bar = ttk.Frame(outer)
        action_bar.grid(row=2, column=0, sticky="e", pady=(10, 0))

        def save_edited_rule():
            expression = expression_var.get().strip()
            if not expression:
                messagebox.showwarning("Missing expression", "Please enter an expression.", parent=dialog)
                return
            if match_type_var.get() == "regex":
                try:
                    re.compile(expression)
                except re.error as e:
                    messagebox.showerror("Invalid regex", str(e), parent=dialog)
                    return
            sources = self._selected_rule_sources(source_tree)
            destinations = list(dest_list.get(0, tk.END))
            if not sources:
                messagebox.showwarning("No folders", "Select at least one watched folder.", parent=dialog)
                return
            if not destinations:
                messagebox.showwarning("No destination", "Add at least one destination folder.", parent=dialog)
                return
            overlaps = find_overlapping_source_destinations(sources, destinations)
            if overlaps:
                source, dest = overlaps[0]
                messagebox.showerror(
                    "Overlapping folders",
                    "Source and destination folders must not overlap.\n\n"
                    f"Source: {source}\nDestination: {dest}",
                    parent=dialog,
                )
                return
            with self.config_lock:
                self.config_data["rules"][rule_index] = {
                    "expression": expression,
                    "match_type": match_type_var.get(),
                    "sources": sources,
                    "destinations": destinations,
                    "enabled": enabled_var.get(),
                }
            self.persist()
            self.refresh_rules()
            dialog.destroy()

        ttk.Button(action_bar, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(action_bar, text="Save changes", command=save_edited_rule).pack(side=tk.RIGHT, padx=(0, 6))

        dialog.update_idletasks()
        self._center_dialog(dialog, 820, 760)
        dialog.grab_set()

    def delete_rule(self):
        sel = self.rule_tree.selection()
        if sel:
            index = int(sel[0])
        elif self.selected_rule_index is not None:
            index = self.selected_rule_index
        else:
            return
        if not messagebox.askyesno("Delete", "Delete the selected rule?"):
            return
        with self.config_lock:
            if 0 <= index < len(self.config_data["rules"]):
                self.config_data["rules"].pop(index)
        self.persist()
        self.new_rule()
        self.refresh_rules()

    # ------------------------------------------------------------- monitor
    def start_monitoring(self):
        self.monitor.start()
        self._update_status()

    def stop_monitoring(self):
        self.monitor.stop()
        self._update_status()

    def _update_status(self):
        monitoring_active = self.monitor.running
        monitoring_stopping = self.monitor.running and self.monitor._stop.is_set()
        scan_active = self.monitor.session_lock.locked()
        if monitoring_active and not monitoring_stopping:
            self.status_var.set("Monitoring active")
        elif monitoring_stopping:
            self.status_var.set("Stopping...")
        else:
            self.status_var.set("Monitoring stopped")
        self._update_folder_action_states()
        self.stop_monitor_btn.configure(state=tk.NORMAL if monitoring_active else tk.DISABLED)
        self.stop_manual_scan_btn.configure(state=tk.NORMAL if scan_active else tk.DISABLED)
        self._update_next_scan_text()

    def _tick(self):
        for iid in self.folder_tree.get_children():
            index = int(iid)
            if index < len(self.config_data["folders"]):
                self.folder_tree.item(iid, values=self._folder_row(self.config_data["folders"][index]))
        self._update_status()
        self.after(1000, self._tick)

    def _format_duration(self, seconds):
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _update_next_scan_text(self):
        if not self.monitor.running or self.monitor._stop.is_set():
            self.next_scan_var.set("Next scan: -")
            return

        now = time.time()
        next_due = None
        next_label = ""
        for folder in self.config_data["folders"]:
            path = folder.get("path", "")
            nxt = self.monitor.next_run(path, folder.get("interval"))
            label = folder.get("nickname") or os.path.basename(path) or path
            if nxt is None:
                self.next_scan_var.set(f"Next scan: starting now ({label})")
                return
            if nxt <= now:
                self.next_scan_var.set(f"Next scan: starting now ({label})")
                return
            if next_due is None or nxt < next_due:
                next_due = nxt
                next_label = label

        if next_due is None:
            self.next_scan_var.set("Next scan: waiting")
            return

        remaining = next_due - now
        self.next_scan_var.set(f"Next scan in {self._format_duration(remaining)} ({next_label})")

    def _check_for_updates(self):
        request = urllib.request.Request(
            LATEST_RELEASE_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "NMR-Alfred",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                release = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            log(f"[WARNING] Could not check for updates: {e}")
            return

        if not isinstance(release, dict):
            log("[WARNING] Could not check for updates: invalid release response.")
            return
        latest_version = release.get("tag_name")
        release_url = release.get("html_url")
        if not isinstance(latest_version, str) or not isinstance(release_url, str):
            log("[WARNING] Could not check for updates: release response is missing version or URL.")
            return
        if is_newer_version(latest_version, CURRENT_VERSION):
            self.update_queue.put((latest_version, release_url))

    def open_releases_page(self):
        try:
            opened = webbrowser.open(RELEASES_PAGE_URL)
        except OSError as e:
            log(f"[ERROR] Could not open releases page {RELEASES_PAGE_URL}: {e}")
            messagebox.showerror(
                "Could not open browser",
                f"Could not open the releases page:\n{RELEASES_PAGE_URL}\n\n{e}",
                parent=self,
            )
        else:
            if not opened:
                log(f"[ERROR] Could not open releases page {RELEASES_PAGE_URL}.")
                messagebox.showerror(
                    "Could not open browser",
                    f"Could not open the releases page:\n{RELEASES_PAGE_URL}",
                    parent=self,
                )

    def _poll_log(self):
        self.copy_log_path_var.set(os.path.basename(current_copy_log_path()))
        while not self.update_queue.empty():
            version, release_url = self.update_queue.get_nowait()
            download_now = messagebox.askyesno(
                "Update available",
                f"An update is available: {version}.\n\n"
                "Would you like to download it now?",
                parent=self,
            )
            if download_now:
                try:
                    opened = webbrowser.open(release_url)
                except OSError as e:
                    log(f"[ERROR] Could not open release page {release_url}: {e}")
                    messagebox.showerror(
                        "Could not open browser",
                        f"Could not open the release page:\n{release_url}\n\n{e}",
                        parent=self,
                    )
                else:
                    if not opened:
                        log(f"[ERROR] Could not open release page {release_url}.")
                        messagebox.showerror(
                            "Could not open browser",
                            f"Could not open the release page:\n{release_url}",
                            parent=self,
                        )
        lines = []
        while not self.log_queue.empty():
            lines.append(self.log_queue.get_nowait())
        copied_entries = []
        while not self.copy_queue.empty():
            copied_entries.append(self.copy_queue.get_nowait())
        if lines:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, "\n".join(lines) + "\n")
            if int(self.log_text.index("end-1c").split(".")[0]) > 5000:
                self.log_text.delete("1.0", "1000.0")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        if copied_entries:
            self.copy_history.extend(copied_entries)
            if self.copy_search_var.get().strip():
                self.refresh_copy_history()
            else:
                for entry in copied_entries:
                    self.copy_tree.insert("", tk.END, values=self._copy_row(entry))
                self.copy_tree.see(self.copy_tree.get_children()[-1])
        self.after(200, self._poll_log)

    def clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_scan_progress(self):
        while not self.scan_progress_queue.empty():
            event = self.scan_progress_queue.get_nowait()
            event_type = event.get("type")
            if event_type == "start":
                total = event.get("total", None)
                if event.get("mode") == "indeterminate" or total is None:
                    self._show_scan_progress(None, event.get("label", "Scanning"))
                else:
                    self._show_scan_progress(total, event.get("label", "Scanning"))
            elif event_type == "counting":
                self._update_counting_progress(event.get("path", ""), event.get("source", ""))
            elif event_type == "configure":
                self._show_scan_progress(event.get("total", 1), event.get("label", "Scanning"))
                self._show_nmr_progress(0, self.nmr_progress_total if self.nmr_progress_total else event.get("nmr_total", 1))
            elif event_type == "update":
                self._update_scan_progress(event.get("current", 0), event.get("total", 1), event.get("path", ""))
            elif event_type == "nmr_start":
                self._show_nmr_progress(None, event.get("total", 1))
            elif event_type == "nmr_update":
                self._show_nmr_progress(event.get("current", 0), event.get("total", 1))
            elif event_type == "nmr_done":
                self._show_nmr_progress(0, 0)
            elif event_type == "done":
                self._close_scan_progress()
        self.after(50, self._poll_scan_progress)

    def on_close(self):
        self.abort_scan()
        self.monitor.stop()
        self.persist()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
