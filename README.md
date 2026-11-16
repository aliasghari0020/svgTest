# Shopify image sync (test)

Uploads images from `images/` to Shopify and links each one to its product and variant.

## File naming
```
images/<Category>/<Subcategory>/<Brand_Folder>/<SKU>-V.jpg      -> variant image
images/<Category>/<Subcategory>/<Brand_Folder>/<SKU>-G-01.jpg   -> product gallery image
```
The brand folder (`ELCO_Lighting` -> `ELCO Lighting`) must match the product **Vendor** in Shopify,
and `<SKU>` must match the variant **SKU** (case and spaces are ignored).

## SKUs with "/" and other special characters
A filename cannot contain "/", so write it as "-" (NTH-104W/A -> NTH-104W-A-V.jpg); spaces and quotes are removed.
The script downloads all store variants once and converts their SKUs the same way, so the image still finds NTH-104W/A.
If two SKUs become identical after the conversion, the image is reported as "ambiguous" and skipped;
add a row to sku_overrides.csv (filename,sku) with the exact SKU to resolve it.

## Folders
Folders are optional. images/NTH-104W-A-V.jpg works. If the folder that holds an image has the name of a store vendor
(Nora_Lighting -> Nora Lighting), the vendor must match, otherwise the image is reported as vendor_mismatch.

## Setup (once)
1. Shopify admin -> Settings -> Apps and sales channels -> Develop apps -> Create an app.
   Admin API scopes: `read_products`, `write_products`, `read_files`, `write_files`. Install it and copy the Admin API token.
2. GitHub repo -> Settings -> Secrets and variables -> Actions:
   - `SHOPIFY_STORE` = `your-store.myshopify.com`
   - `SHOPIFY_ADMIN_TOKEN` = the token

## Run
GitHub -> Actions -> **Sync images to Shopify** -> Run workflow.
- `apply = false` (default): dry run. Nothing changes in Shopify; download the `sync-report` artifact.
- `apply = true`: uploads, then commits `state/uploaded.json` so a re-run never uploads the same image twice.

Locally:
```
pip install -r requirements.txt
export SHOPIFY_STORE=your-store.myshopify.com SHOPIFY_ADMIN_TOKEN=...
python sync_images.py            # dry run
python sync_images.py --apply    # real upload
```

## Report statuses
| status | meaning |
|---|---|
| would_upload | dry run: matched, would be uploaded |
| uploaded | uploaded and linked |
| already_uploaded | same image + SKU is in `state/uploaded.json` |
| no_match | SKU not in Shopify |
| vendor_mismatch | SKU exists but under another vendor |
| ambiguous | more than one variant with this SKU + vendor |
| skipped | variant already has an image and `--skip-variants-with-media` was given |
| error | API error; see `note` |

## Local test without Shopify
```
python tests/mock_shopify.py 8765 &
SHOPIFY_STORE=x SHOPIFY_ADMIN_TOKEN=x SHOPIFY_GRAPHQL_URL=http://127.0.0.1:8765/graphql python sync_images.py --apply
```
