# Onyx mirror Worker

A read-only Cloudflare Worker in front of one R2 bucket. The Mac publishes
encrypted blobs into the bucket; the phone fetches them through this Worker with
a bearer token. The Worker never sees a key or a plaintext, only opaque bytes.
The design is in [`docs/plans/phone-mirror.md`](../../docs/plans/phone-mirror.md)
(gate 7 and "Wire format v1").

## What it serves

`GET` or `HEAD` on `/o/<64 lowercase hex>` with `Authorization: Bearer <read token>`:

- `200` with the object, `Content-Type: application/octet-stream`, the R2
  `ETag`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`
  (`HEAD` gets the headers and no body);
- `304` with no body when `If-None-Match` matches the `ETag`.

## What it refuses

Everything else is an empty `404` with `Cache-Control: no-store`, so a caller
learns nothing about which ids exist:

- any method but `GET` and `HEAD`;
- any path but `/o/` plus exactly 64 lowercase hex characters (no trailing slash,
  no uppercase, no encoded characters, no traversal, no listing route);
- a missing, malformed or wrong token, compared in constant time (both sides are
  hashed with SHA-256 and the digests compared byte by byte);
- a missing object.

The query string is ignored; the path decides. If `READ_TOKEN` is unset or empty
the Worker refuses every request. No CORS headers are sent (the phone is a native
app), and the token and `Authorization` header are never logged.

The one answer that is not a `404` is a `500` when R2 itself fails for a caller
who already presented the right token, so an outage never looks like a deletion.

## Deploy

The bucket must have **public access off**: no managed public URL and no custom
domain on it. This Worker is the only way in.

1. Create the bucket: `npx wrangler r2 bucket create <your-bucket>`.
2. `cp wrangler.example.toml wrangler.toml` and fill in the Worker and bucket
   names. `wrangler.toml` is gitignored; never commit it.
3. Set the phone's read token: `npx wrangler secret put READ_TOKEN`. It prompts,
   so the token never lands in shell history.
4. `npx wrangler deploy`. An account with no `workers.dev` subdomain refuses a
   `workers_dev = true` deploy; either register one in the dashboard, or set
   `workers_dev = false` and serve it on a hostname in a zone you own:
   `routes = [{ pattern = "<host>", custom_domain = true }]`. Wrangler prints the Worker's URL; that URL and the read
   token go into the pairing code on the Mac, nowhere else.
5. In the Cloudflare dashboard, create an R2 API token scoped to that one bucket
   with object read and write. The Mac writes with it; the phone never has it.

Rotate the phone's access by running step 3 again with a new token and re-pairing.

## Test

```
npm test
```

Runs `node --test` against an in-memory fake of the R2 bucket. No dependencies,
no network, nothing deployed.
