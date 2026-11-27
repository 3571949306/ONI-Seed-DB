#!/usr/bin/env python3
"""
ONI-Seed-DB Community Submission Validator & Merger.

Pure Python 3 stdlib script executed by GitHub Actions (or locally for verification)
to parse JSON seed submissions, validate schema/bounds/paths, deduplicate and merge
into data/<CLUSTER_PREFIX>.json, and atomically rebuild manifest.json with SHA-256 hashes.
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

SCHEMA_VERSION = 1
MAX_SEED = 2147483000
MAX_SEEDS_PER_SUBMISSION = 25000
GZIP_ONLY_THRESHOLD_BYTES = 19 * 1024 * 1024
CLUSTER_PREFIX_RE = re.compile(r"^[A-Z0-9-]{2,24}$")
GRADE_SET = {"S", "A", "B", "C", ""}


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_str(val, max_len=160):
    if val is None:
        return ""
    if not isinstance(val, str):
        val = str(val)
    val = val.replace("\r", " ").replace("\n", " ").strip()
    if len(val) > max_len:
        val = val[:max_len]
    return val


def safe_int(val, default=0):
    if isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return default
    return default


def safe_float(val, default=0.0):
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip())
        except ValueError:
            return default
    return default


def extract_json_payload(raw_text):
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty submission body")

    gz_marker = re.search(
        r"\[seed-db-gzip\]\s*(.*?)\s*\[/seed-db-gzip\]",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if gz_marker:
        b64 = re.sub(r"\s+", "", gz_marker.group(1))
        raw_bytes = gzip.decompress(base64.b64decode(b64))
        return json.loads(raw_bytes.decode("utf-8"))

    marker = re.search(
        r"\[seed-db-json\]\s*(.*?)\s*\[/seed-db-json\]",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if marker:
        return json.loads(marker.group(1).strip())

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence:
        return json.loads(fence.group(1).strip())

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        return json.loads(text[first_brace : last_brace + 1])

    raise ValueError("No JSON object found in submission body")


def derive_cluster_prefix(raw_prefix, coord):
    p = clean_str(raw_prefix, 24).upper()
    if p and CLUSTER_PREFIX_RE.match(p):
        return p
    c = clean_str(coord, 80).upper()
    m = re.match(r"^([A-Z0-9-]{2,24})-(\d+)(?:-.*)?$", c)
    if m and CLUSTER_PREFIX_RE.match(m.group(1)):
        return m.group(1)
    return ""


def normalize_poi(raw):
    if not isinstance(raw, dict):
        return None
    prefab_id = clean_str(raw.get("prefabId"), 80)
    display_name = clean_str(raw.get("displayName"), 80)
    if not prefab_id and not display_name:
        return None
    grade = clean_str(raw.get("grade"), 4).upper()
    if grade not in GRADE_SET:
        grade = ""
    px = max(0, min(5000, safe_int(raw.get("x"), 0)))
    py = max(0, min(5000, safe_int(raw.get("y"), 0)))
    dist = max(0, min(5000, safe_int(raw.get("dist"), 0)))
    avg = round(max(0.0, min(1000000.0, safe_float(raw.get("avgYieldGps"), 0.0))), 2)
    pct = round(max(0.0, min(100.0, safe_float(raw.get("percentile"), 0.0))), 1)
    return {
        "prefabId": prefab_id,
        "displayName": display_name,
        "category": clean_str(raw.get("category") or "geyser", 24).lower(),
        "x": px,
        "y": py,
        "dist": dist,
        "avgYieldGps": avg,
        "grade": grade,
        "percentile": pct,
    }


def normalize_planet(raw):
    if not isinstance(raw, dict):
        return None
    world_idx = safe_int(raw.get("worldIndex"), -1)
    if world_idx < 0 or world_idx > 64:
        return None
    traits = []
    if isinstance(raw.get("traits"), list):
        for t in raw["traits"][:12]:
            ts = clean_str(t, 64)
            if ts and ts not in traits:
                traits.append(ts)
    trait_ids = []
    if isinstance(raw.get("traitIds"), list):
        for t in raw["traitIds"][:12]:
            ts = clean_str(t, 80)
            if ts and ts not in trait_ids:
                trait_ids.append(ts)
    pois = []
    if isinstance(raw.get("pois"), list):
        for rp in raw["pois"][:80]:
            np = normalize_poi(rp)
            if np is not None:
                pois.append(np)
    pois_scanned = bool(raw.get("poisScanned")) or (len(pois) > 0)
    return {
        "worldIndex": world_idx,
        "worldId": clean_str(raw.get("worldId"), 80),
        "worldName": clean_str(raw.get("worldName"), 64),
        "poisScanned": pois_scanned,
        "traits": traits,
        "traitIds": trait_ids,
        "pois": pois,
    }


def normalize_seed_entry(raw, author_override=""):
    if not isinstance(raw, dict):
        return None, "Seed entry must be a JSON object"
    seed = safe_int(raw.get("seed"), 0)
    if seed <= 0 or seed > MAX_SEED:
        return None, f"Seed out of valid range [1, {MAX_SEED}]: {raw.get('seed')}"

    coord = clean_str(raw.get("coordinate"), 80)
    prefix = derive_cluster_prefix(raw.get("clusterPrefix"), coord)
    if not prefix or not CLUSTER_PREFIX_RE.match(prefix):
        return None, f"Invalid or disallowed clusterPrefix: {raw.get('clusterPrefix')!r}"

    story_code = clean_str(raw.get("storyCode") or "0", 24) or "0"
    mixing_code = clean_str(raw.get("mixingCode") or "0", 24) or "0"
    if not coord:
        coord = f"{prefix}-{seed}-0-{story_code}-{mixing_code}"

    planets = []
    seen_pidx = set()
    if isinstance(raw.get("planets"), list):
        for rp in raw["planets"][:24]:
            np = normalize_planet(rp)
            if np is not None and np["worldIndex"] not in seen_pidx:
                seen_pidx.add(np["worldIndex"])
                planets.append(np)
    planets.sort(key=lambda p: p["worldIndex"])

    author = clean_str(author_override or raw.get("author") or "anonymous", 48) or "anonymous"
    entry = {
        "schemaVersion": SCHEMA_VERSION,
        "clusterPrefix": prefix,
        "clusterPath": clean_str(raw.get("clusterPath"), 120),
        "clusterName": clean_str(raw.get("clusterName"), 64),
        "seed": seed,
        "coordinate": coord,
        "storyCode": story_code,
        "mixingCode": mixing_code,
        "dlcFlags": clean_str(raw.get("dlcFlags"), 160),
        "author": author,
        "submittedAt": clean_str(raw.get("submittedAt"), 40) or utc_now_iso(),
        "traitSummary": clean_str(raw.get("traitSummary"), 500),
        "poiSummary": clean_str(raw.get("poiSummary"), 600),
        "note": clean_str(raw.get("note"), 200),
        "planets": planets,
    }
    return entry, None


def normalize_dlc_key(raw_dlc):
    if not raw_dlc:
        return ""
    parts = [p.strip().upper() for p in re.split(r"[,;\s]+", str(raw_dlc)) if p.strip()]
    return "EXPANSION1_ID" if "EXPANSION1_ID" in parts else ""


def dedup_key(entry):
    return (
        safe_int(entry.get("seed"), 0),
        normalize_dlc_key(entry.get("dlcFlags")),
        clean_str(entry.get("storyCode") or "0", 24) or "0",
        clean_str(entry.get("mixingCode") or "0", 24) or "0",
    )


def merge_two_entries(existing, incoming):
    # Preserve richer planet POI metadata while blending notes and contributors.
    by_idx = {}
    for p in existing.get("planets") or []:
        by_idx[p["worldIndex"]] = p
    for p in incoming.get("planets") or []:
        idx = p["worldIndex"]
        cur = by_idx.get(idx)
        if cur is None:
            by_idx[idx] = p
        elif (not cur.get("poisScanned") and p.get("poisScanned")) or len(p.get("pois") or []) > len(cur.get("pois") or []):
            by_idx[idx] = p
        elif len(p.get("traits") or []) > len(cur.get("traits") or []):
            cur["traits"] = p["traits"]
    merged_planets = [by_idx[k] for k in sorted(by_idx.keys())]

    for field in ("clusterPath", "clusterName", "dlcFlags", "traitSummary", "poiSummary"):
        if not existing.get(field) and incoming.get(field):
            existing[field] = incoming[field]
        elif field == "poiSummary" and len(incoming.get(field, "")) > len(existing.get(field, "")):
            existing[field] = incoming[field]

    in_note = clean_str(incoming.get("note"), 200)
    ex_note = clean_str(existing.get("note"), 200)
    if in_note and in_note not in ex_note:
        existing["note"] = clean_str(f"{ex_note}; {in_note}" if ex_note else in_note, 200)

    in_author = clean_str(incoming.get("author"), 48)
    ex_author = clean_str(existing.get("author"), 64)
    if in_author and in_author not in ex_author:
        existing["author"] = clean_str(f"{ex_author}, {in_author}" if ex_author else in_author, 64)

    existing["planets"] = merged_planets
    existing["submittedAt"] = incoming.get("submittedAt") or existing.get("submittedAt") or utc_now_iso()
    return existing


def safe_cluster_file(data_dir, prefix, ext=".json"):
    if not CLUSTER_PREFIX_RE.match(prefix):
        raise ValueError(f"Unsafe cluster prefix: {prefix!r}")
    real_data = os.path.realpath(data_dir)
    target = os.path.realpath(os.path.join(real_data, f"{prefix}{ext}"))
    if os.path.dirname(target) != real_data:
        raise ValueError(f"Path traversal blocked for {prefix!r}")
    return target


def safe_bad_file(repo_root, prefix):
    bad_dir = os.path.join(repo_root, "data", "bad")
    return safe_cluster_file(bad_dir, prefix, ".json")


def load_bad_map(repo_root, prefix):
    bpath = safe_bad_file(repo_root, prefix)
    bad_map = {}
    if not os.path.isfile(bpath):
        return bad_map
    try:
        with open(bpath, "r", encoding="utf-8") as f:
            doc = json.load(f)
        by_dlc = doc.get("byDlc") if isinstance(doc, dict) else None
        if isinstance(by_dlc, dict):
            for raw_k, arr in by_dlc.items():
                if not isinstance(arr, list):
                    continue
                dk = normalize_dlc_key(raw_k)
                s = bad_map.setdefault(dk, set())
                for v in arr:
                    sv = safe_int(v, 0)
                    if 0 < sv <= MAX_SEED:
                        s.add(sv)
    except Exception:
        pass
    return bad_map


def count_bad_map(bad_map):
    return sum(len(s) for s in bad_map.values())


def write_bad_doc_atomic(path, prefix, bad_map):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    by_dlc_out = {}
    total_cnt = 0
    for dk in sorted(bad_map.keys()):
        vals = sorted(s for s in bad_map[dk] if 0 < s <= MAX_SEED)
        if vals:
            by_dlc_out[dk] = vals
            total_cnt += len(vals)
    doc = {
        "schemaVersion": SCHEMA_VERSION,
        "clusterPrefix": prefix,
        "updatedAt": utc_now_iso(),
        "count": total_cnt,
        "byDlc": by_dlc_out,
    }
    return write_json_atomic(path, doc)


def is_seed_blacklisted(bad_map, seed, raw_dlc):
    sv = safe_int(seed, 0)
    if sv <= 0:
        return False
    dk = normalize_dlc_key(raw_dlc)
    return sv in bad_map.get(dk, ())


def load_cluster_seeds_from_disk(data_dir, prefix):
    jpath = safe_cluster_file(data_dir, prefix, ".json")
    gpath = safe_cluster_file(data_dir, prefix, ".json.gz")
    raw_bytes = None
    if os.path.isfile(jpath):
        with open(jpath, "rb") as f:
            raw_bytes = f.read()
    elif os.path.isfile(gpath):
        with open(gpath, "rb") as f:
            raw_bytes = gzip.decompress(f.read())
    if not raw_bytes:
        return [], False
    cdoc = json.loads(raw_bytes.decode("utf-8"))
    if isinstance(cdoc, dict) and isinstance(cdoc.get("seeds"), list):
        return [s for s in cdoc["seeds"] if isinstance(s, dict)], True
    return [], True


def format_cluster_doc_bytes(cluster_doc):
    seeds = cluster_doc.get("seeds") or []
    lines = [
        "{",
        f'  "schemaVersion": {cluster_doc["schemaVersion"]},',
        f'  "clusterPrefix": {json.dumps(cluster_doc["clusterPrefix"], ensure_ascii=False)},',
        f'  "updatedAt": {json.dumps(cluster_doc["updatedAt"], ensure_ascii=False)},',
        f'  "count": {len(seeds)},',
        '  "seeds": [',
    ]
    for i, s in enumerate(seeds):
        comma = "," if i < len(seeds) - 1 else ""
        lines.append("    " + json.dumps(s, ensure_ascii=False, separators=(",", ":")) + comma)
    lines.append("  ]")
    lines.append("}")
    raw = "\n".join(lines) + "\n"
    return raw.encode("utf-8")


def write_bytes_atomic(path, raw_bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw_bytes)
    os.replace(tmp, path)


def write_cluster_doc_atomic(path, cluster_doc, gzip_only=False):
    raw_bytes = format_cluster_doc_bytes(cluster_doc)
    data_dir = os.path.dirname(path)
    prefix = cluster_doc["clusterPrefix"]
    jpath = safe_cluster_file(data_dir, prefix, ".json")
    gpath = safe_cluster_file(data_dir, prefix, ".json.gz")

    gz_bytes = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
    write_bytes_atomic(gpath, gz_bytes)

    use_gz_only = bool(gzip_only) or (len(raw_bytes) > GZIP_ONLY_THRESHOLD_BYTES)
    if use_gz_only:
        if os.path.isfile(jpath):
            os.remove(jpath)
    else:
        write_bytes_atomic(jpath, raw_bytes)
    return raw_bytes


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
    os.replace(tmp, path)
    return raw.encode("utf-8")


def extract_bad_items_by_prefix(payload):
    by_prefix = {}
    errors = []
    if not isinstance(payload, dict):
        return by_prefix, ["Root JSON must be an object"]

    # Format A: {"clusterPrefix": "V-SNDST-C", "byDlc": {"EXPANSION1_ID": [123, 456]}}
    if isinstance(payload.get("byDlc"), dict):
        pref = derive_cluster_prefix(payload.get("clusterPrefix"), payload.get("coordinate"))
        if not pref:
            errors.append(f"Invalid clusterPrefix for byDlc: {payload.get('clusterPrefix')!r}")
        else:
            pdict = by_prefix.setdefault(pref, {})
            for raw_k, arr in payload["byDlc"].items():
                if not isinstance(arr, list):
                    continue
                dk = normalize_dlc_key(raw_k)
                s = pdict.setdefault(dk, set())
                for v in arr:
                    sv = safe_int(v, 0)
                    if 0 < sv <= MAX_SEED:
                        s.add(sv)

    # Format B: {"badSeeds": [{"clusterPrefix": "...", "seed": 123, "dlcFlags": "..."}]}
    raw_bad = payload.get("badSeeds")
    if isinstance(raw_bad, list):
        for idx, item in enumerate(raw_bad):
            if not isinstance(item, dict):
                errors.append(f"Bad item #{idx + 1}: must be an object")
                continue
            sv = safe_int(item.get("seed"), 0)
            if sv <= 0 or sv > MAX_SEED:
                errors.append(f"Bad item #{idx + 1}: invalid seed {item.get('seed')}")
                continue
            pref = derive_cluster_prefix(item.get("clusterPrefix"), item.get("coordinate"))
            if not pref:
                errors.append(f"Bad item #{idx + 1}: invalid clusterPrefix")
                continue
            dk = normalize_dlc_key(item.get("dlcFlags"))
            by_prefix.setdefault(pref, {}).setdefault(dk, set()).add(sv)

    return by_prefix, errors


def apply_bad_items_to_repo(repo_root, by_prefix, gzip_only=False):
    data_dir = os.path.join(repo_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    added_bad = 0
    purged_good = 0
    touched = []

    for prefix, inc_map in sorted(by_prefix.items()):
        bad_map = load_bad_map(repo_root, prefix)
        changed_bad = False
        for dk, seeds_set in inc_map.items():
            cur = bad_map.setdefault(dk, set())
            for sv in seeds_set:
                if sv not in cur:
                    cur.add(sv)
                    added_bad += 1
                    changed_bad = True
        if changed_bad or not os.path.isfile(safe_bad_file(repo_root, prefix)):
            write_bad_doc_atomic(safe_bad_file(repo_root, prefix), prefix, bad_map)

        existing_list, exists = load_cluster_seeds_from_disk(data_dir, prefix)
        if exists:
            filtered = [
                ex for ex in existing_list
                if not is_seed_blacklisted(bad_map, ex.get("seed"), ex.get("dlcFlags"))
            ]
            removed_cnt = len(existing_list) - len(filtered)
            if removed_cnt > 0:
                purged_good += removed_cnt
                cluster_doc = {
                    "schemaVersion": SCHEMA_VERSION,
                    "clusterPrefix": prefix,
                    "updatedAt": utc_now_iso(),
                    "count": len(filtered),
                    "seeds": filtered,
                }
                write_cluster_doc_atomic(safe_cluster_file(data_dir, prefix, ".json"), cluster_doc, gzip_only=gzip_only)
        touched.append(prefix)

    return added_bad, purged_good, touched


def merge_bad_seeds(repo_root, payload, gzip_only=False):
    if not isinstance(payload, dict):
        return {"ok": False, "message": "Root JSON must be an object", "addedBad": 0, "purgedGood": 0, "errors": []}
    ver = safe_int(payload.get("schemaVersion", SCHEMA_VERSION), 0)
    if ver != SCHEMA_VERSION:
        return {"ok": False, "message": f"Unsupported schemaVersion: {ver}", "addedBad": 0, "purgedGood": 0, "errors": []}

    by_prefix, errors = extract_bad_items_by_prefix(payload)
    if not by_prefix:
        return {
            "ok": False,
            "message": "No valid bad seeds in payload",
            "addedBad": 0,
            "purgedGood": 0,
            "errors": errors,
        }

    added_bad, purged_good, touched = apply_bad_items_to_repo(repo_root, by_prefix, gzip_only=gzip_only)
    manifest = rebuild_manifest(repo_root)
    return {
        "ok": True,
        "message": "Bad seeds merged",
        "addedBad": added_bad,
        "purgedGood": purged_good,
        "clusters": touched,
        "totalSeeds": manifest["totalSeeds"],
        "errors": errors,
    }


def rebuild_manifest(repo_root):
    data_dir = os.path.join(repo_root, "data")
    bad_dir = os.path.join(repo_root, "data", "bad")
    os.makedirs(data_dir, exist_ok=True)

    prefixes = set()
    for fn in os.listdir(data_dir):
        if fn.endswith(".json"):
            p = fn[:-5]
            if CLUSTER_PREFIX_RE.match(p):
                prefixes.add(p)
        elif fn.endswith(".json.gz"):
            p = fn[:-8]
            if CLUSTER_PREFIX_RE.match(p):
                prefixes.add(p)
    if os.path.isdir(bad_dir):
        for fn in os.listdir(bad_dir):
            if fn.endswith(".json"):
                p = fn[:-5]
                if CLUSTER_PREFIX_RE.match(p):
                    prefixes.add(p)

    clusters = []
    total = 0
    for prefix in sorted(prefixes):
        jpath = safe_cluster_file(data_dir, prefix, ".json")
        gpath = safe_cluster_file(data_dir, prefix, ".json.gz")
        bpath = safe_bad_file(repo_root, prefix)

        item = None
        if os.path.isfile(jpath) or os.path.isfile(gpath):
            raw_json = None
            gz_bytes = None
            if os.path.isfile(jpath):
                with open(jpath, "rb") as f:
                    raw_json = f.read()
            if os.path.isfile(gpath):
                with open(gpath, "rb") as f:
                    gz_bytes = f.read()
            if raw_json is None and gz_bytes is not None:
                try:
                    raw_json = gzip.decompress(gz_bytes)
                except Exception:
                    raw_json = None
            if raw_json is not None:
                try:
                    doc = json.loads(raw_json.decode("utf-8"))
                    seeds = doc.get("seeds") if isinstance(doc, dict) else None
                except Exception:
                    seeds = None
                if isinstance(seeds, list):
                    cnt = len(seeds)
                    total += cnt
                    if os.path.isfile(jpath):
                        main_rel = f"data/{prefix}.json"
                        main_sha = hashlib.sha256(raw_json).hexdigest()
                    else:
                        main_rel = f"data/{prefix}.json.gz"
                        main_sha = hashlib.sha256(gz_bytes).hexdigest()
                    item = {
                        "clusterPrefix": prefix,
                        "file": main_rel,
                        "count": cnt,
                        "sha256": main_sha,
                    }
                    if gz_bytes is not None:
                        item["gzipFile"] = f"data/{prefix}.json.gz"
                        item["gzipSha256"] = hashlib.sha256(gz_bytes).hexdigest()
                        item["gzipBytes"] = len(gz_bytes)

        if os.path.isfile(bpath):
            with open(bpath, "rb") as f:
                bad_bytes = f.read()
            bad_map = load_bad_map(repo_root, prefix)
            bad_cnt = count_bad_map(bad_map)
            if bad_cnt > 0:
                if item is None:
                    item = {
                        "clusterPrefix": prefix,
                        "file": "",
                        "count": 0,
                        "sha256": "",
                    }
                item["badFile"] = f"data/bad/{prefix}.json"
                item["badCount"] = bad_cnt
                item["badSha256"] = hashlib.sha256(bad_bytes).hexdigest()

        if item is not None:
            clusters.append(item)

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": utc_now_iso(),
        "totalSeeds": total,
        "clusters": clusters,
    }
    write_json_atomic(os.path.join(repo_root, "manifest.json"), manifest)
    return manifest


def merge_submission(repo_root, payload, author_override="", gzip_only=False):
    if not isinstance(payload, dict):
        return {"ok": False, "message": "Root JSON must be an object", "added": 0, "merged": 0, "rejected": 0, "errors": []}

    ver = safe_int(payload.get("schemaVersion"), 0)
    if ver != SCHEMA_VERSION:
        return {"ok": False, "message": f"Unsupported schemaVersion: {ver}", "added": 0, "merged": 0, "rejected": 0, "errors": []}

    bad_by_prefix, bad_errors = extract_bad_items_by_prefix(payload)
    added_bad = 0
    purged_good = 0
    bad_touched = []
    if bad_by_prefix:
        added_bad, purged_good, bad_touched = apply_bad_items_to_repo(repo_root, bad_by_prefix, gzip_only=gzip_only)

    raw_seeds = payload.get("seeds")
    if raw_seeds is None and "seed" in payload:
        raw_seeds = [payload]
    if (not isinstance(raw_seeds, list) or len(raw_seeds) == 0):
        if bad_by_prefix:
            manifest = rebuild_manifest(repo_root)
            return {
                "ok": True,
                "message": "Bad seeds merged",
                "added": 0,
                "merged": 0,
                "rejected": len(bad_errors),
                "addedBad": added_bad,
                "purgedGood": purged_good,
                "clusters": bad_touched,
                "totalSeeds": manifest["totalSeeds"],
                "errors": bad_errors,
            }
        return {"ok": False, "message": "Submission contains no seeds array", "added": 0, "merged": 0, "rejected": 0, "errors": []}
    if len(raw_seeds) > MAX_SEEDS_PER_SUBMISSION:
        return {
            "ok": False,
            "message": f"Too many seeds in one submission ({len(raw_seeds)} > {MAX_SEEDS_PER_SUBMISSION})",
            "added": 0,
            "merged": 0,
            "rejected": 0,
            "errors": [],
        }

    errors = list(bad_errors)
    by_prefix = {}
    for idx, rs in enumerate(raw_seeds):
        entry, err = normalize_seed_entry(rs, author_override=author_override)
        if err:
            errors.append(f"Item #{idx + 1}: {err}")
            continue
        by_prefix.setdefault(entry["clusterPrefix"], []).append(entry)

    if not by_prefix:
        if bad_by_prefix:
            manifest = rebuild_manifest(repo_root)
            return {
                "ok": True,
                "message": "Bad seeds merged (no valid good seeds)",
                "added": 0,
                "merged": 0,
                "rejected": len(errors),
                "addedBad": added_bad,
                "purgedGood": purged_good,
                "clusters": bad_touched,
                "totalSeeds": manifest["totalSeeds"],
                "errors": errors,
            }
        return {
            "ok": False,
            "message": "All submitted seed items failed validation",
            "added": 0,
            "merged": 0,
            "rejected": len(errors),
            "errors": errors,
        }

    data_dir = os.path.join(repo_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    added = 0
    merged = 0
    touched_clusters = list(bad_touched)

    for prefix, entries in sorted(by_prefix.items()):
        bad_map = load_bad_map(repo_root, prefix)
        existing_list, _ = load_cluster_seeds_from_disk(data_dir, prefix)
        clean_existing = []
        for ex in existing_list:
            if is_seed_blacklisted(bad_map, ex.get("seed"), ex.get("dlcFlags")):
                purged_good += 1
            else:
                clean_existing.append(ex)
        existing_list = clean_existing

        index_map = {}
        for i, ex in enumerate(existing_list):
            index_map[dedup_key(ex)] = i

        for ent in entries:
            if is_seed_blacklisted(bad_map, ent.get("seed"), ent.get("dlcFlags")):
                errors.append(f"Seed {ent['seed']} ({prefix}) rejected: Blacklisted bad seed")
                continue
            k = dedup_key(ent)
            if k in index_map:
                pos = index_map[k]
                existing_list[pos] = merge_two_entries(existing_list[pos], ent)
                merged += 1
            else:
                index_map[k] = len(existing_list)
                existing_list.append(ent)
                added += 1

        existing_list.sort(key=lambda e: (safe_int(e.get("seed"), 0), clean_str(e.get("storyCode"), 24)))
        cluster_doc = {
            "schemaVersion": SCHEMA_VERSION,
            "clusterPrefix": prefix,
            "updatedAt": utc_now_iso(),
            "count": len(existing_list),
            "seeds": existing_list,
        }
        write_cluster_doc_atomic(safe_cluster_file(data_dir, prefix, ".json"), cluster_doc, gzip_only=gzip_only)
        if prefix not in touched_clusters:
            touched_clusters.append(prefix)

    manifest = rebuild_manifest(repo_root)
    return {
        "ok": True,
        "message": "Merged successfully",
        "added": added,
        "merged": merged,
        "rejected": len(errors),
        "addedBad": added_bad,
        "purgedGood": purged_good,
        "clusters": touched_clusters,
        "totalSeeds": manifest["totalSeeds"],
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="Validate & merge ONI community seed submission.")
    parser.add_argument("--repo-root", default=".", help="Path to ONI-Seed-DB root")
    parser.add_argument("--issue-body-file", help="Path to GitHub Issue body markdown/text")
    parser.add_argument("--test-json", help="Path to raw JSON submission file")
    parser.add_argument("--bad-seeds-json", help="Path to JSON bad-seed blacklist file")
    parser.add_argument("--author", default="", help="Override author name")
    parser.add_argument("--gzip-only", action="store_true", help="Store shards as .json.gz only (omit uncompressed .json)")
    parser.add_argument("--rebuild-manifest", action="store_true", help="Only rebuild manifest.json")
    parser.add_argument("--result-out", help="Write structured result JSON to path")
    args = parser.parse_args()

    repo_root = os.path.abspath(args.repo_root)
    result = {"ok": False, "message": "", "added": 0, "merged": 0, "rejected": 0, "errors": []}

    try:
        if args.rebuild_manifest:
            manifest = rebuild_manifest(repo_root)
            result = {
                "ok": True,
                "message": "Manifest rebuilt",
                "added": 0,
                "merged": 0,
                "rejected": 0,
                "clusters": [c["clusterPrefix"] for c in manifest["clusters"]],
                "totalSeeds": manifest["totalSeeds"],
                "errors": [],
            }
        elif args.bad_seeds_json:
            with open(args.bad_seeds_json, "r", encoding="utf-8") as f:
                payload = json.load(f)
            result = merge_bad_seeds(repo_root, payload, gzip_only=args.gzip_only)
        else:
            if args.test_json:
                with open(args.test_json, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            elif args.issue_body_file:
                with open(args.issue_body_file, "r", encoding="utf-8") as f:
                    payload = extract_json_payload(f.read())
            else:
                raise ValueError("Specify --issue-body-file, --test-json, --bad-seeds-json, or --rebuild-manifest")
            result = merge_submission(repo_root, payload, author_override=args.author, gzip_only=args.gzip_only)
    except Exception as exc:
        result = {"ok": False, "message": str(exc), "added": 0, "merged": 0, "rejected": 0, "errors": [str(exc)]}

    if args.result_out:
        write_json_atomic(args.result_out, result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
