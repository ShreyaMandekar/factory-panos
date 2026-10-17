#!/usr/bin/env python3
"""
Generate 5 World Labs worlds, one per CC0 Poly Haven panorama.

Usage:
  pip install requests pillow
  export WLT_API_KEY=your_key
  python generate_worlds.py prepare                       # download + resize panos to 2560x1280
  python generate_worlds.py generate --draft --base-url https://your-host/panos
  python generate_worlds.py generate --base-url https://your-host/panos
  python generate_worlds.py credits

Before running, check these against https://docs.worldlabs.ai (I did not test them):
  - API_BASE and the auth header name (AUTH_HEADER)
  - the exact fields of the operation/world responses (parsing below is defensive)
  - Image delivery: the API needs a public, no-auth URL, OR a media asset id.
    Host ./panos somewhere public and pass --base-url, or swap image_prompt()
    to use the media-asset upload described in the API FAQ.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
import dotenv

import requests
from PIL import Image

dotenv.load_dotenv()

API_BASE = "https://api.worldlabs.ai"
AUTH_HEADER = "WLT-Api-Key"  # verify in docs
PANO_DIR = Path("panos")
OUT_DIR = Path("worlds")

# Order = order along the corridor. Replace IDs if your panoramas differ
# (browse https://polyhaven.com/hdris/indoor and copy the id from the URL).
ROOMS = [
    {"name": "boiler",      "polyhaven_id": "boiler_room"},
    {"name": "foundry",     "polyhaven_id": "industrial_workshop_foundry"},
    {"name": "machine_shop", "polyhaven_id": "machine_shop_02"},   # swap for 01/03 if that's the crowded one
    {"name": "pump_hall",   "polyhaven_id": "peppermint_powerplant_2"},  # verify this is the teal turbine hall
    {"name": "electrical_lab", "polyhaven_id": "vintage_measuring_lab"},  # verify
]

MODEL_FULL = "marble-1.1-plus"
MODEL_DRAFT = "marble-1.0-draft"
POLL_SECONDS = 15
MAX_WAIT_SECONDS = 20 * 60
START_GAP_SECONDS = 25  # stay under ~3 starts/minute


def headers():
    key = os.environ.get("WLT_API_KEY")
    if not key:
        sys.exit("Set WLT_API_KEY first.")
    return {AUTH_HEADER: key, "Content-Type": "application/json"}


def prepare():
    PANO_DIR.mkdir(exist_ok=True)
    for room in ROOMS:
        out = PANO_DIR / f"{room['name']}.jpg"
        if out.exists():
            print(f"skip {out}")
            continue
        url = (
            "https://dl.polyhaven.org/file/ph-assets/HDRIs/extra/"
            f"Tonemapped%20JPG/{room['polyhaven_id']}.jpg"
        )
        print(f"download {url}")
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        tmp = PANO_DIR / f"{room['name']}_full.jpg"
        tmp.write_bytes(r.content)
        img = Image.open(tmp).convert("RGB")
        img = img.resize((2560, 1280), Image.LANCZOS)  # exactly 2:1
        img.save(out, quality=92)
        tmp.unlink()
        print(f"saved {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print(f"\nUpload the files in ./{PANO_DIR} to a public host, then run generate.")


def image_prompt(room, base_url):
    return {
        "type": "image",
        "is_pano": True,
        "image_prompt": {
            "source": "uri",
            "uri": f"{base_url.rstrip('/')}/{room['name']}.jpg",
        },
    }


def start_world(room, base_url, model):
    body = {
        "display_name": f"factory-{room['name']}",
        "model": model,  # always set explicitly (docs disagree on the default)
        "world_prompt": image_prompt(room, base_url),
    }
    r = requests.post(f"{API_BASE}/marble/v1/worlds:generate", headers=headers(), json=body, timeout=60)
    if not r.ok:
        sys.exit(f"{room['name']}: start failed {r.status_code}: {r.text}")
    return r.json()


def wait_for(op):
    op_id = op.get("operation_id") or op.get("id") or op.get("name", "").split("/")[-1]
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        r = requests.get(f"{API_BASE}/marble/v1/operations/{op_id}", headers=headers(), timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"operation {op_id} failed: {data['error']}")
        if data.get("done"):
            return data
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"operation {op_id} still running after {MAX_WAIT_SECONDS}s")


def find_world_id(done_op):
    resp = done_op.get("response") or {}
    return resp.get("world_id") or resp.get("id") or (resp.get("world") or {}).get("id")


def download(url, dest):
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    dest.write_bytes(r.content)


def generate(base_url, draft):
    model = MODEL_DRAFT if draft else MODEL_FULL
    OUT_DIR.mkdir(exist_ok=True)
    print(f"model: {model}  rooms: {len(ROOMS)}")

    pending = []
    for i, room in enumerate(ROOMS):
        print(f"start {room['name']}")
        pending.append((room, start_world(room, base_url, model)))
        if i < len(ROOMS) - 1:
            time.sleep(START_GAP_SECONDS)

    manifest = []
    for room, op in pending:
        print(f"wait {room['name']} ...")
        done = wait_for(op)
        world_id = find_world_id(done)
        if not world_id:
            print(f"  could not find world id; raw: {json.dumps(done)[:400]}")
            continue
        w = requests.get(f"{API_BASE}/marble/v1/worlds/{world_id}", headers=headers(), timeout=60)
        w.raise_for_status()
        world = w.json()
        room_dir = OUT_DIR / f"{room['name']}_{'draft' if draft else 'full'}"
        room_dir.mkdir(exist_ok=True)
        (room_dir / "world.json").write_text(json.dumps(world, indent=2))

        assets = world.get("assets", {})
        spz = (assets.get("splats") or {}).get("spz_urls", {})
        for key in ("full_res", "500k", "100k"):
            if spz.get(key):
                download(spz[key], room_dir / f"{key}.spz")
        collider = (assets.get("mesh") or {}).get("collider_mesh_url")
        if collider:
            download(collider, room_dir / "collider.glb")

        meta = (assets.get("splats") or {}).get("semantics_metadata", {})
        manifest.append({
            "name": room["name"],
            "world_id": world_id,
            "dir": str(room_dir),
            "metric_scale_factor": meta.get("metric_scale_factor"),
            "ground_plane_offset": meta.get("ground_plane_offset"),
        })
        print(f"  saved {room_dir}  scale={meta.get('metric_scale_factor')}")

    (OUT_DIR / f"manifest_{'draft' if draft else 'full'}.json").write_text(json.dumps(manifest, indent=2))
    print("done. Manifest written to", OUT_DIR)


def credits():
    r = requests.get(f"{API_BASE}/marble/v1/credits", headers=headers(), timeout=30)
    print(r.status_code, r.text)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    sub.add_parser("credits")
    g = sub.add_parser("generate")
    g.add_argument("--base-url", required=True, help="public URL folder hosting the resized panos")
    g.add_argument("--draft", action="store_true", help="use marble-1.0-draft (150 credits each)")
    a = p.parse_args()
    if a.cmd == "prepare":
        prepare()
    elif a.cmd == "credits":
        credits()
    else:
        generate(a.base_url, a.draft)