"""Tiny fake Shopify Admin GraphQL + staged-upload endpoint, for local testing only."""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
VARIANTS = {
    "ELWB": ("gid://shopify/Product/1", "elco-lighting-elwb-low-voltage-transformer", "ELCO Lighting", "Default Title"),
    "ELWW": ("gid://shopify/Product/2", "elco-lighting-elww-low-voltage-transformer", "ELCO Lighting", "Default Title"),
    "EP2025B": ("gid://shopify/Product/3", "elco-lighting-ep2025b-low-voltage-transformer-multi", "ELCO Lighting", "All Black"),
    # EP2028B intentionally missing -> should be reported as no_match
}
DUPLICATES = {"EP2025B": "DRAFT", "ELWW": "ARCHIVED"}
# SKUs that contain "/" (written as "-" in filenames)
SLASH_SKUS = {
    "NLCBC2-45127DW/10": ("gid://shopify/Product/10", "nora-lighting-nlcbc2-45127dw-10", "Nora Lighting", "Default Title"),
    "NLCBC2-45127DW/A": ("gid://shopify/Product/11", "nora-lighting-nlcbc2-45127dw-a", "Nora Lighting", "Default Title"),
}
# two different SKUs that become identical after normalisation -> ambiguous
CLASH = {"XY-1/2": "Brand A", "XY-1-2": "Brand B"}


def all_variants():
    out = []
    for sku, (pid, handle, vendor, title) in list(VARIANTS.items()) + list(SLASH_SKUS.items()):
        out.append({"id": f"gid://shopify/ProductVariant/{sku}", "sku": sku, "title": title,
                    "product": {"id": pid, "title": f"{vendor} {sku}", "handle": handle,
                                "vendor": vendor, "status": "ACTIVE"}})
    for sku, status in DUPLICATES.items():
        out.append({"id": f"gid://shopify/ProductVariant/{sku}-old", "sku": sku, "title": "Default Title",
                    "product": {"id": f"gid://shopify/Product/old-{sku}", "title": f"ELCO {sku} (old)",
                                "handle": f"{sku.lower()}-old", "vendor": "ELCO Lighting", "status": status}})
    for i, (sku, vendor) in enumerate(CLASH.items()):
        out.append({"id": f"gid://shopify/ProductVariant/clash{i}", "sku": sku, "title": "Default Title",
                    "product": {"id": f"gid://shopify/Product/clash{i}", "title": sku, "handle": f"clash-{i}",
                                "vendor": vendor, "status": "ACTIVE"}})
    return out
CALLS = []
files = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/token":
            CALLS.append("token")
            return self._send({"access_token": "shpat_test", "scope": "write_products,write_files", "expires_in": 86399})
        if self.path == "/upload":
            CALLS.append("upload")
            return self._send({}, 201)
        req = json.loads(body)
        q, v = req["query"], req["variables"]
        ext = {"cost": {"throttleStatus": {"currentlyAvailable": 1000}}}
        if "productVariants(" in q:
            sku = re.search(r'sku:"(.+)"', v["q"]).group(1)
            nodes = []
            if sku in VARIANTS:
                pid, handle, vendor, title = VARIANTS[sku]
                nodes.append({"id": f"gid://shopify/ProductVariant/{sku}", "sku": sku, "title": title,
                              "media": {"nodes": []},
                              "product": {"id": pid, "title": f"ELCO {sku}", "handle": handle,
                                          "vendor": vendor, "status": "ACTIVE"}})
            if sku in DUPLICATES:   # an old copy of the same product with the same SKU
                nodes.append({"id": f"gid://shopify/ProductVariant/{sku}-old", "sku": sku, "title": "Default Title",
                              "media": {"nodes": []},
                              "product": {"id": f"gid://shopify/Product/old-{sku}", "title": f"ELCO {sku} (old)",
                                          "handle": f"{sku.lower()}-old", "vendor": "ELCO Lighting",
                                          "status": DUPLICATES[sku]}})
            CALLS.append(f"find:{sku}")
            return self._send({"data": {"productVariants": {"nodes": nodes}}, "extensions": ext})
        if "bulkOperationRunQuery" in q:
            CALLS.append("bulk")
            return self._send({"data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/1", "status": "CREATED"}, "userErrors": []}}})
        if "node(id" in q and v.get("id", "").startswith("gid://shopify/BulkOperation/"):
            return self._send({"data": {"node": {"status": "COMPLETED", "errorCode": None, "objectCount": "9",
                                                 "url": f"http://127.0.0.1:{PORT}/bulk.jsonl", "partialDataUrl": None}}})
        if "stagedUploadsCreate" in q:
            CALLS.append("staged")
            return self._send({"data": {"stagedUploadsCreate": {"stagedTargets": [{
                "url": f"http://127.0.0.1:{PORT}/upload", "resourceUrl": "https://staged/x",
                "parameters": [{"name": "key", "value": "k"}]}], "userErrors": []}}})
        if "fileCreate" in q:
            fid = f"gid://shopify/MediaImage/{len(files) + 1}"
            files[fid] = 0
            CALLS.append("fileCreate:" + v["files"][0]["alt"])
            return self._send({"data": {"fileCreate": {"files": [{"id": fid, "fileStatus": "UPLOADED"}], "userErrors": []}}})
        if "node(id" in q:
            files[v["id"]] += 1
            st = "READY" if files[v["id"]] >= 2 else "PROCESSING"
            return self._send({"data": {"node": {"fileStatus": st, "fileErrors": []}}})
        if "fileUpdate" in q:
            CALLS.append("attach:" + v["files"][0]["referencesToAdd"][0])
            return self._send({"data": {"fileUpdate": {"files": [{"id": v["files"][0]["id"]}], "userErrors": []}}})
        if "productVariantsBulkUpdate" in q:
            CALLS.append("variant:" + v["variants"][0]["id"])
            return self._send({"data": {"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}}})
        self._send({"errors": [{"message": "unknown query"}]})

    def do_GET(self):
        if self.path == "/bulk.jsonl":
            lines = [json.dumps(v) for v in all_variants()]
            lines.append(json.dumps({"id": "gid://shopify/MediaImage/9", "__parentId": "gid://shopify/ProductVariant/ELWB"}))
            b = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/jsonl")
            self.end_headers()
            self.wfile.write(b)
            return
        self._send(CALLS)


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
