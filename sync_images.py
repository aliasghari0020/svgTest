#!/usr/bin/env python3
"""
Lumera — sync product images from a git folder to Shopify.

Folder layout (same as the lumera repo):
    images/<Category>/<Subcategory...>/<Brand_Folder>/<SKU>-V.jpg      -> variant image
    images/<Category>/<Subcategory...>/<Brand_Folder>/<SKU>-G-01.jpg   -> product gallery image

Matching:  (vendor == brand folder with "_" -> " ")  AND  (variant SKU == SKU from filename)
           Matching is case-insensitive and ignores spaces, because the image pipeline
           stripped spaces from SKUs when it built filenames ("MR16 3W-5K" -> "MR163W-5K").

Default is a DRY RUN: nothing is written to Shopify, only a report is produced.
Pass --apply to actually upload.

Env:
    SHOPIFY_STORE          e.g. lumera-electric.myshopify.com
    Auth, either:
      SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET   app created in the Shopify Dev Dashboard
                                                  (token fetched with the client credentials grant,
                                                  valid ~24h, refreshed automatically)
      SHOPIFY_ADMIN_TOKEN                         shpat_ token of an old admin-created custom app
    App scopes: read_products, write_products, read_files, write_files
    SHOPIFY_API_VERSION    optional, default 2026-10
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-10")
IMAGE_RE = re.compile(r"^(?P<sku>.+?)-(?P<kind>V|G-(?P<pos>\d+))\.(?P<ext>jpe?g|png|webp)$", re.I)


# --------------------------------------------------------------------------- helpers

def norm(s: str) -> str:
    """Normalise SKU / vendor for comparison."""
    return re.sub(r"[\s_]+", "", (s or "")).upper()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ImageFile:
    path: Path
    sku: str
    brand: str
    kind: str              # "V" or "G"
    position: int          # gallery position, 0 for V
    sha: str
    # filled by matching
    product_id: str | None = None
    product_title: str | None = None
    product_handle: str | None = None
    variant_id: str | None = None
    variant_title: str | None = None
    status: str = "pending"
    note: str = ""
    file_id: str | None = None
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- Shopify client

class Shopify:
    def __init__(self, store: str, token: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, url: str | None = None, token_url: str | None = None):
        self.url = url or f"https://{store}/admin/api/{API_VERSION}/graphql.json"
        self.token_url = token_url or f"https://{store}/admin/oauth/access_token"
        self.client_id, self.client_secret = client_id, client_secret
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})
        self._set_token(token or self._fetch_token())

    def _set_token(self, token: str) -> None:
        self.s.headers["X-Shopify-Access-Token"] = token

    def _fetch_token(self) -> str:
        """Client credentials grant (Dev Dashboard apps). Tokens last ~24h."""
        if not (self.client_id and self.client_secret):
            raise RuntimeError("No SHOPIFY_ADMIN_TOKEN and no SHOPIFY_CLIENT_ID/SHOPIFY_CLIENT_SECRET")
        r = requests.post(self.token_url, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id, "client_secret": self.client_secret,
        }, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"token request failed: {r.status_code} {self._oauth_error(r.text)}")
        body = r.json()
        print(f"access token obtained (scopes: {body.get('scope')}, expires in {body.get('expires_in')}s)")
        return body["access_token"]

    @staticmethod
    def _oauth_error(text: str) -> str:
        """Shopify returns an HTML page on OAuth errors; pull out the readable message."""
        found = [re.sub(r"\s+", " ", m).strip() for m in re.findall(r"Oauth error [^<]+", text)]
        if found:
            return max(found, key=len)   # the <title> is short; the body has the full message
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()[:300]

    def gql(self, query: str, variables: dict | None = None, retries: int = 6) -> dict:
        refreshed = False
        for attempt in range(retries):
            r = self.s.post(self.url, json={"query": query, "variables": variables or {}}, timeout=60)
            if r.status_code == 401 and self.client_id and not refreshed:
                self._set_token(self._fetch_token())   # token expired -> get a new one
                refreshed = True
                continue
            if r.status_code == 401:
                raise RuntimeError("401 Unauthorized: the access token is not valid for this store "
                                   "(check SHOPIFY_STORE matches the shop the token was issued for)")
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            body = r.json()
            errs = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs):
                time.sleep(2 ** attempt)
                continue
            if errs:
                raise RuntimeError(f"GraphQL errors: {json.dumps(errs, ensure_ascii=False)}")
            # be polite with the cost-based rate limit
            ts = ((body.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
            if ts and ts.get("currentlyAvailable", 1000) < 200:
                time.sleep(2)
            return body["data"]
        raise RuntimeError("Shopify API: too many retries")

    # ---- reads
    def find_variants_by_sku(self, sku: str) -> list[dict]:
        q = """
        query($q: String!) {
          productVariants(first: 20, query: $q) {
            nodes {
              id sku title
              media(first: 1) { nodes { id } }
              product { id title handle vendor status }
            }
          }
        }"""
        data = self.gql(q, {"q": f'sku:"{sku}"'})
        return data["productVariants"]["nodes"]

    # ---- writes
    def staged_upload(self, path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        q = """
        mutation($input: [StagedUploadInput!]!) {
          stagedUploadsCreate(input: $input) {
            stagedTargets { url resourceUrl parameters { name value } }
            userErrors { field message }
          }
        }"""
        data = self.gql(q, {"input": [{
            "resource": "IMAGE", "filename": path.name, "mimeType": mime,
            "httpMethod": "POST", "fileSize": str(path.stat().st_size),
        }]})["stagedUploadsCreate"]
        if data["userErrors"]:
            raise RuntimeError(f"stagedUploadsCreate: {data['userErrors']}")
        t = data["stagedTargets"][0]
        form = {p["name"]: p["value"] for p in t["parameters"]}
        with path.open("rb") as f:
            r = requests.post(t["url"], data=form, files={"file": (path.name, f, mime)}, timeout=300)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"staged upload POST failed: {r.status_code} {r.text[:300]}")
        return t["resourceUrl"]

    def file_create(self, resource_url: str, alt: str, filename: str) -> str:
        q = """
        mutation($files: [FileCreateInput!]!) {
          fileCreate(files: $files) {
            files { id fileStatus }
            userErrors { field message code }
          }
        }"""
        data = self.gql(q, {"files": [{
            "originalSource": resource_url, "contentType": "IMAGE", "alt": alt, "filename": filename,
        }]})["fileCreate"]
        if data["userErrors"]:
            raise RuntimeError(f"fileCreate: {data['userErrors']}")
        return data["files"][0]["id"]

    def wait_ready(self, file_id: str, timeout: int = 180) -> None:
        q = """query($id: ID!) { node(id: $id) { ... on MediaImage { fileStatus fileErrors { message } } } }"""
        start = time.time()
        while time.time() - start < timeout:
            node = self.gql(q, {"id": file_id})["node"] or {}
            st = node.get("fileStatus")
            if st == "READY":
                return
            if st == "FAILED":
                raise RuntimeError(f"file processing failed: {node.get('fileErrors')}")
            time.sleep(2)
        raise RuntimeError("timed out waiting for file to become READY")

    def attach_to_product(self, file_id: str, product_id: str) -> None:
        q = """
        mutation($files: [FileUpdateInput!]!) {
          fileUpdate(files: $files) { files { id } userErrors { field message code } }
        }"""
        data = self.gql(q, {"files": [{"id": file_id, "referencesToAdd": [product_id]}]})["fileUpdate"]
        if data["userErrors"]:
            raise RuntimeError(f"fileUpdate: {data['userErrors']}")

    def set_variant_media(self, product_id: str, variant_id: str, media_id: str) -> None:
        q = """
        mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id }
            userErrors { field message }
          }
        }"""
        data = self.gql(q, {"productId": product_id,
                            "variants": [{"id": variant_id, "mediaId": media_id}]})["productVariantsBulkUpdate"]
        if data["userErrors"]:
            raise RuntimeError(f"productVariantsBulkUpdate: {data['userErrors']}")


# --------------------------------------------------------------------------- pipeline

def scan(root: Path) -> list[ImageFile]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        m = IMAGE_RE.match(p.name)
        if not m:
            continue
        kind = "V" if m["kind"].upper() == "V" else "G"
        out.append(ImageFile(
            path=p, sku=m["sku"], brand=p.parent.name.replace("_", " "), kind=kind,
            position=int(m["pos"]) if m["pos"] else 0, sha=sha256(p),
        ))
    return out


def match(shop: Shopify, img: ImageFile) -> None:
    candidates = [v for v in shop.find_variants_by_sku(img.sku)
                  if norm(v["sku"]) == norm(img.sku)]
    same_vendor = [v for v in candidates if norm(v["product"]["vendor"]) == norm(img.brand)]
    if not candidates:
        img.status, img.note = "no_match", "SKU not found in Shopify"
        return
    if not same_vendor:
        img.status = "vendor_mismatch"
        img.note = "SKU found but vendor differs: " + ", ".join(v["product"]["vendor"] for v in candidates)
        return
    # Duplicate SKUs usually come from old copies of a product: ignore ARCHIVED ones,
    # and if exactly one ACTIVE product is left, that is the one to use.
    live = [v for v in same_vendor if v["product"]["status"] != "ARCHIVED"] or same_vendor
    active = [v for v in live if v["product"]["status"] == "ACTIVE"]
    if len(live) > 1 and len(active) == 1:
        img.extra["skipped_duplicates"] = ", ".join(
            f'{v["product"]["handle"]} ({v["product"]["status"]})' for v in same_vendor if v is not active[0])
        live = active
    if img.kind == "V" and len(live) > 1:
        img.status = "ambiguous"
        img.note = "same SKU+vendor on: " + "; ".join(
            f'{v["product"]["handle"]} / {v["title"]} ({v["product"]["status"]})' for v in live)
        return
    v = live[0]
    img.product_id, img.product_title = v["product"]["id"], v["product"]["title"]
    img.product_handle = v["product"]["handle"]
    img.variant_id, img.variant_title = v["id"], v["title"]
    img.extra["variant_has_media"] = bool(v["media"]["nodes"])
    img.status = "matched"


def alt_text(img: ImageFile) -> str:
    if img.kind == "V" and img.variant_title and img.variant_title != "Default Title":
        return f"{img.product_title} - {img.variant_title}"
    return img.product_title or img.sku


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", default="images", help="root folder of images")
    ap.add_argument("--state", default="state/uploaded.json", help="idempotency state file (commit it)")
    ap.add_argument("--report", default="reports/sync_report.csv")
    ap.add_argument("--apply", action="store_true", help="really write to Shopify (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="process at most N images")
    ap.add_argument("--skip-variants-with-media", action="store_true",
                    help="do not overwrite a variant that already has an image")
    args = ap.parse_args()

    env = {k: v.strip() for k, v in os.environ.items() if k.startswith("SHOPIFY_")}
    store = env.get("SHOPIFY_STORE")
    token = env.get("SHOPIFY_ADMIN_TOKEN") or None
    cid, csec = env.get("SHOPIFY_CLIENT_ID") or None, env.get("SHOPIFY_CLIENT_SECRET") or None
    print(f"store={store} client_id={'set' if cid else 'MISSING'} ({len(cid or '')} chars) "
          f"client_secret={'set' if csec else 'MISSING'} ({len(csec or '')} chars, "
          f"starts with {(csec or '')[:6]!r}) admin_token={'set' if token else 'not set'}")
    if not store or not (token or (cid and csec)):
        print("Set SHOPIFY_STORE and either SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET "
              "or SHOPIFY_ADMIN_TOKEN", file=sys.stderr)
        return 2
    store = store.replace("https://", "").replace("http://", "").strip("/")
    if token:
        cid = csec = None   # a fixed admin token is used as-is; never fall back to client credentials
    print(f"store host starts with {store[:6]!r}")
    shop = Shopify(store, token=token, client_id=cid, client_secret=csec,
                   url=env.get("SHOPIFY_GRAPHQL_URL"), token_url=env.get("SHOPIFY_TOKEN_URL"))

    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    images = scan(Path(args.images))
    if args.limit:
        images = images[: args.limit]
    print(f"{len(images)} images found | mode: {'APPLY' if args.apply else 'DRY RUN'}")

    for img in images:
        key = f"{img.sha}:{norm(img.sku)}"
        if key in state:
            img.status, img.file_id, img.note = "already_uploaded", state[key]["file_id"], "in state file"
            print(f"  = {img.path.name}: already uploaded")
            continue
        try:
            match(shop, img)
            if img.status != "matched":
                print(f"  ! {img.path.name}: {img.status} ({img.note})")
                continue
            if img.kind == "V" and args.skip_variants_with_media and img.extra.get("variant_has_media"):
                img.status, img.note = "skipped", "variant already has an image"
                print(f"  - {img.path.name}: skipped (variant has image)")
                continue
            dup = img.extra.get("skipped_duplicates")
            if dup:
                img.note = f"ignored duplicate SKU on: {dup}"
            if not args.apply:
                img.status = "would_upload"
                print(f"  ~ {img.path.name} -> {img.product_handle} / {img.variant_title} [{img.kind}]"
                      + (f"   (ignored: {dup})" if dup else ""))
                continue

            resource = shop.staged_upload(img.path)
            img.file_id = shop.file_create(resource, alt_text(img), img.path.name)
            shop.wait_ready(img.file_id)
            shop.attach_to_product(img.file_id, img.product_id)
            if img.kind == "V":
                shop.set_variant_media(img.product_id, img.variant_id, img.file_id)
            img.status = "uploaded"
            state[key] = {"file_id": img.file_id, "product_id": img.product_id,
                          "variant_id": img.variant_id if img.kind == "V" else None,
                          "path": img.path.as_posix(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))   # save after each image
            print(f"  + {img.path.name} -> {img.product_handle} ({img.file_id})")
        except Exception as e:  # keep going; report the failure
            img.status, img.note = "error", str(e)[:500]
            print(f"  x {img.path.name}: {e}")

    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    with rp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "brand", "sku", "kind", "position", "sha256", "status", "note",
                    "product_handle", "product_id", "variant_title", "variant_id", "file_id"])
        for i in images:
            w.writerow([i.path.as_posix(), i.brand, i.sku, i.kind, i.position, i.sha, i.status, i.note,
                        i.product_handle, i.product_id, i.variant_title, i.variant_id, i.file_id])

    counts: dict[str, int] = {}
    for i in images:
        counts[i.status] = counts.get(i.status, 0) + 1
    print("summary:", counts, "| report:", rp)
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
