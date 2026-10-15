"""Check all declared Python pins and locked npm versions against the release-age policy."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
NPM_REGISTRY = "https://packagefeedproxy.microsoft.io/npm/"
CUTOFF = date(2026, 10, 2)


def release_date(metadata: dict[str, Any], version: str, ecosystem: str) -> date:
    if not re.fullmatch(r"\d+(?:\.\d+)+", version):
        raise ValueError(f"non-stable or unsupported pin: {version}")
    if ecosystem == "python":
        files = [f for f in metadata.get("releases", {}).get(version, []) if not f.get("yanked")]
        if not files:
            raise ValueError(f"missing or entirely yanked release: {version}")
        timestamps = [f.get("upload_time_iso_8601", f.get("upload_time")) for f in files]
        if not all(timestamps):
            raise ValueError(f"missing upload time: {version}")
        timestamp = min(timestamps)
    else:
        if version not in metadata.get("versions", {}):
            raise ValueError(f"version unavailable through configured proxy: {version}")
        timestamp = metadata.get("time", {}).get(version)
        if not timestamp:
            raise ValueError(f"missing publish time: {version}")
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).date()


def check_age(published: date, today: date, min_age_days: int, cutoff: date) -> None:
    eligible = min(cutoff, today - timedelta(days=min_age_days))
    if published > eligible:
        raise ValueError(f"published {published}; required on/before {eligible}")


def inventory(root: Path) -> list[tuple[str, str, str, str]]:
    pins = []
    for path in [root / "requirements.txt", *sorted((root / "src").glob("*/requirements.txt"))]:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([\w.-]+)==([^\s;]+)", line)
            if not match:
                raise ValueError(f"{path}:{number}: policy requires an exact, stable pin")
            name, version = match.groups()
            pins.append(("python", re.sub(r"[-_.]+", "-", name.lower()), version, str(path.relative_to(root))))
    frontend = root / "src/hypothesis_ag_ui/frontend"
    manifest = json.loads((frontend / "package.json").read_text())
    lock = json.loads((frontend / "package-lock.json").read_text())
    packages = lock.get("packages", {})
    for category in ("dependencies", "devDependencies"):
        if manifest.get(category, {}) != packages.get("", {}).get(category, {}):
            raise ValueError(f"package.json {category} does not match lockfile")
        for name in manifest.get(category, {}):
            if f"node_modules/{name}" not in packages:
                raise ValueError(f"unlocked npm dependency: {name}")
    for path, metadata in packages.items():
        if path:
            if "node_modules/" not in path or not metadata.get("version"):
                raise ValueError(f"unsupported lock entry: {path}")
            name = path.rsplit("node_modules/", 1)[1]
            pins.append(("npm", name, metadata["version"], path))
    return pins


def fetch_metadata(url: str, cache: Path, *, max_cache_age: int = 3600) -> dict[str, Any]:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    if path.exists() and time.time() - path.stat().st_mtime <= max_cache_age:
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    error = "registry unavailable"
    for attempt in range(3):
        result = subprocess.run(
            ["curl", "--compressed", "--fail", "--silent", "--show-error", "--max-time", "90", "--output", str(path), url],
            text=True, capture_output=True,
        )
        if result.returncode == 0:
            try:
                return json.loads(path.read_text())
            except ValueError:
                error = "invalid registry JSON"
        else:
            error = result.stderr.strip()
        path.unlink(missing_ok=True)
        time.sleep(attempt + 1)
    raise RuntimeError(f"{url}: {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    parser.add_argument("--cutoff", type=date.fromisoformat, default=CUTOFF)
    parser.add_argument("--min-age-days", type=int, default=7)
    parser.add_argument("--npm-registry", default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print full per-pin evidence")
    args = parser.parse_args(argv)
    if args.min_age_days < 7 or args.cutoff > CUTOFF:
        parser.error("policy cannot relax seven-day age or the 2026-10-02 cutoff")
    if args.npm_registry is None:
        args.npm_registry = subprocess.check_output(["npm", "config", "get", "registry"], text=True).strip()
    if args.npm_registry.rstrip("/") != NPM_REGISTRY.rstrip("/"):
        parser.error(f"npm registry must be {NPM_REGISTRY}")
    try:
        pins = inventory(ROOT)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 2
    jobs = sorted({(ecosystem, name) for ecosystem, name, _, _ in pins})

    def query(job):
        ecosystem, name = job
        url = f"https://pypi.org/pypi/{name}/json" if ecosystem == "python" else NPM_REGISTRY + name.replace("/", "%2F")
        try:
            return job, fetch_metadata(url, ROOT / ".cache/package-age", max_cache_age=0 if args.no_cache else 3600)
        except (RuntimeError, OSError) as exc:
            return job, exc

    with ThreadPoolExecutor(max_workers=8) as executor:
        metadata = dict(executor.map(query, jobs))
    results = []
    failures = 0
    registry_failed = False
    for ecosystem, name, version, source in pins:
        entry = {"ecosystem": ecosystem, "package": name, "version": version, "source": source}
        try:
            package = metadata[ecosystem, name]
            if isinstance(package, Exception):
                registry_failed = True
                raise package
            published = release_date(package, version, ecosystem)
            entry["published"] = published.isoformat()
            check_age(published, args.today, args.min_age_days, args.cutoff)
            entry["eligible"] = True
        except (ValueError, RuntimeError) as exc:
            failures += 1
            entry.update(eligible=False, error=str(exc))
            if not args.json:
                print(f"FAIL {ecosystem}:{name}=={version} ({source}): {exc}")
        results.append(entry)
    if args.json:
        print(json.dumps({"today": str(args.today), "cutoff": str(min(args.cutoff, args.today - timedelta(days=args.min_age_days))),
                          "npm_registry": NPM_REGISTRY, "results": results}, indent=2))
    else:
        print(f"Checked {len(pins)} pins/lock entries ({len(jobs)} packages) via PyPI and {NPM_REGISTRY}: {failures} failures.")
    return 2 if registry_failed else 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
