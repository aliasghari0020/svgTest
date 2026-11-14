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
            CALLS.append(f"find:{sku}")
            return self._send({"data": {"productVariants": {"nodes": nodes}}, "extensions": ext})
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
        self._send(CALLS)


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
