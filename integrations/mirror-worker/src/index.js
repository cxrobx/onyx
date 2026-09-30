// Onyx phone mirror: a read-only window onto one R2 bucket.
//
// The Worker serves `GET`/`HEAD /o/<64 lowercase hex>` with a bearer token and
// nothing else. Every other request, a wrong token included, gets the same empty
// 404, so the Worker never confirms which ids exist. The objects are already
// encrypted by the Mac; the Worker only moves opaque bytes. See
// docs/plans/phone-mirror.md, gate 7 and "Wire format v1".

const OBJECT_PATH = /^\/o\/([0-9a-f]{64})$/;

const NO_STORE = "no-store";

function notFound() {
  return new Response(null, {
    status: 404,
    headers: { "Cache-Control": NO_STORE },
  });
}

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return new Uint8Array(digest);
}

// Both sides are hashed first, so the compared values always have the same
// length and the loop never exits early on a mismatch.
async function tokenMatches(presented, expected) {
  const [a, b] = await Promise.all([sha256(presented), sha256(expected)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

function bearerToken(request) {
  const header = request.headers.get("Authorization") ?? "";
  return header.slice(0, 7).toLowerCase() === "bearer " ? header.slice(7) : "";
}

// Weak comparison, as RFC 9110 prescribes for If-None-Match: `W/` is ignored and
// `*` matches any stored object.
function etagMatches(ifNoneMatch, etag) {
  const bare = (tag) => tag.trim().replace(/^W\//, "");
  return ifNoneMatch
    .split(",")
    .some((tag) => tag.trim() === "*" || bare(tag) === bare(etag));
}

export async function handle(request, env) {
  // The header a client sends is trimmed by the runtime, so a secret saved with
  // a trailing newline would never match; trimming it here removes that trap.
  const expected = typeof env.READ_TOKEN === "string" ? env.READ_TOKEN.trim() : "";
  if (expected === "") return notFound();

  if (request.method !== "GET" && request.method !== "HEAD") return notFound();

  let pathname;
  try {
    pathname = new URL(request.url).pathname;
  } catch {
    return notFound();
  }
  const match = OBJECT_PATH.exec(pathname);
  if (match === null) return notFound();

  if (!(await tokenMatches(bearerToken(request), expected))) return notFound();

  let object;
  try {
    object = await env.BUCKET.get(`o/${match[1]}`);
  } catch {
    // Only an authenticated caller gets here, and a storage outage must not
    // read as "object deleted", so this is the one answer that is not a 404.
    return new Response(null, {
      status: 500,
      headers: { "Cache-Control": NO_STORE },
    });
  }
  if (object === null) return notFound();

  const headers = {
    ETag: object.httpEtag,
    "Cache-Control": NO_STORE,
    "X-Content-Type-Options": "nosniff",
  };

  const ifNoneMatch = request.headers.get("If-None-Match");
  if (ifNoneMatch !== null && etagMatches(ifNoneMatch, object.httpEtag)) {
    await object.body.cancel();
    return new Response(null, { status: 304, headers });
  }

  headers["Content-Type"] = "application/octet-stream";
  if (request.method === "HEAD") {
    await object.body.cancel();
    if (typeof object.size === "number") headers["Content-Length"] = String(object.size);
    return new Response(null, { status: 200, headers });
  }
  return new Response(object.body, { status: 200, headers });
}

export default {
  fetch(request, env) {
    return handle(request, env);
  },
};
