import assert from "node:assert/strict";
import test from "node:test";

import worker, { handle } from "../src/index.js";

// Synthetic values only: nothing here is a real token, id or endpoint.
const TOKEN = "unit-test-read-token-0123456789";
const ID = "0123456789abcdef".repeat(4);
const OTHER_ID = "fedcba9876543210".repeat(4);
const BLOB = new Uint8Array([1, 2, 3, 4, 5, 250, 251, 252]);
const ORIGIN = "https://worker.test";

// The slice of the R2 bucket API the Worker uses: get(key) resolves to an
// object with a body stream, httpEtag and size, or to null when absent.
function fakeBucket(objects) {
  const calls = [];
  const cancelled = [];
  return {
    calls,
    cancelled,
    async get(key) {
      calls.push(key);
      const bytes = objects[key];
      if (bytes === undefined) return null;
      return {
        key,
        size: bytes.byteLength,
        httpEtag: `"etag-${key.slice(-8)}"`,
        body: new ReadableStream({
          start(controller) {
            controller.enqueue(bytes);
            controller.close();
          },
          cancel() {
            cancelled.push(key);
          },
        }),
      };
    },
  };
}

function setup(envOverrides = {}) {
  const bucket = fakeBucket({ [`o/${ID}`]: BLOB, [`o/${OTHER_ID}`]: new Uint8Array([9]) });
  return { bucket, env: { BUCKET: bucket, READ_TOKEN: TOKEN, ...envOverrides } };
}

async function call(env, path, { method = "GET", token = TOKEN, headers = {} } = {}) {
  const init = { method, headers: { ...headers } };
  if (token !== null) init.headers.Authorization = `Bearer ${token}`;
  return handle(new Request(ORIGIN + path, init), env);
}

function assertRefused(response, bucket) {
  assert.equal(response.status, 404);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("ETag"), null);
  assert.equal(response.headers.get("Content-Type"), null);
  for (const name of response.headers.keys()) {
    assert.ok(!name.startsWith("access-control-"), `unexpected CORS header ${name}`);
  }
  if (bucket) assert.deepEqual(bucket.calls, [], "a refused request must not touch the bucket");
}

async function assertRefusedFully(response, bucket) {
  assertRefused(response, bucket);
  assert.equal(await response.text(), "");
}

test("GET serves the blob with the required headers", async () => {
  const { bucket, env } = setup();
  const response = await call(env, `/o/${ID}`);
  assert.equal(response.status, 200);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), BLOB);
  assert.equal(response.headers.get("Content-Type"), "application/octet-stream");
  assert.equal(response.headers.get("ETag"), `"etag-${ID.slice(-8)}"`);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
  assert.deepEqual(bucket.calls, [`o/${ID}`]);
});

test("no response carries a CORS header", async () => {
  const { env } = setup();
  const responses = [
    await call(env, `/o/${ID}`),
    await call(env, `/o/${ID}`, { method: "HEAD" }),
    await call(env, `/o/${ID}`, { headers: { Origin: "https://elsewhere.test" } }),
    await call(env, `/o/${ID}`, { method: "OPTIONS", headers: { Origin: "https://elsewhere.test" } }),
    await call(env, "/nope"),
  ];
  for (const response of responses) {
    for (const name of response.headers.keys()) {
      assert.ok(!name.startsWith("access-control-"), `unexpected CORS header ${name}`);
    }
  }
});

test("HEAD returns the headers and no body", async () => {
  const { bucket, env } = setup();
  const response = await call(env, `/o/${ID}`, { method: "HEAD" });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
  assert.equal(response.headers.get("Content-Type"), "application/octet-stream");
  assert.equal(response.headers.get("ETag"), `"etag-${ID.slice(-8)}"`);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
  assert.equal(response.headers.get("Content-Length"), String(BLOB.byteLength));
  assert.deepEqual(bucket.cancelled, [`o/${ID}`], "the unused body stream is released");
});

test("a matching If-None-Match answers 304 with no body", async () => {
  const etag = `"etag-${ID.slice(-8)}"`;
  for (const method of ["GET", "HEAD"]) {
    for (const header of [etag, `W/${etag}`, `"other", ${etag}`, "*"]) {
      const { bucket, env } = setup();
      const response = await call(env, `/o/${ID}`, {
        method,
        headers: { "If-None-Match": header },
      });
      assert.equal(response.status, 304, `${method} ${header}`);
      assert.equal(await response.text(), "");
      assert.equal(response.headers.get("ETag"), etag);
      assert.equal(response.headers.get("Cache-Control"), "no-store");
      assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
      assert.deepEqual(bucket.cancelled, [`o/${ID}`]);
    }
  }
});

test("a stale If-None-Match still serves the blob", async () => {
  const { env } = setup();
  const response = await call(env, `/o/${ID}`, {
    headers: { "If-None-Match": '"some-older-etag"' },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), BLOB);
});

test("If-None-Match never turns a refusal into a 304", async () => {
  const { bucket, env } = setup();
  const etag = `"etag-${ID.slice(-8)}"`;
  const response = await call(env, `/o/${ID}`, {
    token: "wrong-token",
    headers: { "If-None-Match": etag },
  });
  await assertRefusedFully(response, bucket);
});

test("only GET and HEAD are served", async () => {
  for (const method of ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"]) {
    const { bucket, env } = setup();
    await assertRefusedFully(await call(env, `/o/${ID}`, { method }), bucket);
  }
});

test("only /o/<64 lowercase hex> is served", async () => {
  const paths = [
    "/",
    "",
    "/o",
    "/o/",
    "/o//",
    `/o/${ID}/`,
    `/o/${ID}/extra`,
    `/o/${ID}.bin`,
    `/o/${ID}0`,
    `/o/${ID.slice(1)}`,
    `/o/${ID.toUpperCase()}`,
    `/o/${ID.slice(0, 63)}F`,
    `/o/${ID.slice(0, 63)}g`,
    `/o/ ${ID.slice(1)}`,
    `/O/${ID}`,
    `/x/${ID}`,
    `/${ID}`,
    `/objects/${ID}`,
    `//o/${ID}`,
    `/o//${ID}`,
    `/o/%30${ID.slice(1)}`,
    `/o/${ID.slice(0, 62)}%36%34`,
    `/o/${ID}%00`,
    `/o/${ID}%2f`,
    `/o/${ID}%2f..`,
    `/o/%2e%2e%2f${ID}`,
    `/o/..%2f${ID}`,
    `/o/..%5c${ID}`,
    `/o/${ID}/..`,
    `/o/${ID}/../..`,
    `/o/../${ID}`,
    `/o/%2e%2e/${ID}`,
    `/o/../x/${ID}`,
    `/o/${ID}/../../secret`,
    "/list",
    "/o?list",
    "/o/?prefix=",
    "/o/?list-type=2&prefix=o/",
    "/?prefix=o/",
  ];
  for (const path of paths) {
    const { bucket, env } = setup();
    await assertRefusedFully(await call(env, path), bucket);
    await assertRefusedFully(await call(env, path, { method: "HEAD" }), bucket);
  }
});

test("a query string is ignored and the path decides", async () => {
  const { bucket, env } = setup();
  const served = await call(env, `/o/${ID}?list=1&prefix=o/&x=${OTHER_ID}`);
  assert.equal(served.status, 200);
  assert.deepEqual(new Uint8Array(await served.arrayBuffer()), BLOB);
  assert.deepEqual(bucket.calls, [`o/${ID}`]);

  const other = setup();
  await assertRefusedFully(await call(other.env, `/o/nothex?o/${ID}`), other.bucket);
});

test("dot segments cannot reach a key outside o/", async () => {
  // The URL parser resolves dot segments first, so this lands on o/<OTHER_ID>:
  // another well-formed id, still behind the token and still only under o/.
  const { bucket, env } = setup();
  const response = await call(env, `/o/${ID}/../${OTHER_ID}`);
  assert.equal(response.status, 200);
  assert.deepEqual(bucket.calls, [`o/${OTHER_ID}`]);
});

test("a missing object is a plain 404", async () => {
  const { bucket, env } = setup();
  const missing = "a".repeat(64);
  const response = await call(env, `/o/${missing}`);
  await assertRefusedFully(response, null);
  assert.deepEqual(bucket.calls, [`o/${missing}`]);
});

test("a missing or malformed token is refused", async () => {
  const tokens = [
    null, // no Authorization header
    "not-the-token",
    "", // "Bearer " with nothing after it
    TOKEN.slice(0, -1),
    `${TOKEN}x`,
    TOKEN + TOKEN,
    TOKEN.toUpperCase(),
    ` ${TOKEN}`,
  ];
  for (const token of tokens) {
    const { bucket, env } = setup();
    await assertRefusedFully(await call(env, `/o/${ID}`, { token }), bucket);
  }
});

test("a token differing only in the last character is refused", async () => {
  const { bucket, env } = setup();
  const flipped = TOKEN.slice(0, -1) + (TOKEN.endsWith("9") ? "8" : "9");
  assert.notEqual(flipped, TOKEN);
  assert.equal(flipped.length, TOKEN.length);
  await assertRefusedFully(await call(env, `/o/${ID}`, { token: flipped }), bucket);
  assert.equal((await call(env, `/o/${ID}`)).status, 200, "the real token still works");
});

test("the token is only accepted as a Bearer Authorization header", async () => {
  const cases = [
    { path: `/o/${ID}`, headers: { Authorization: `Basic ${TOKEN}` } },
    { path: `/o/${ID}`, headers: { Authorization: TOKEN } },
    { path: `/o/${ID}`, headers: { Authorization: `Bearer${TOKEN}` } },
    { path: `/o/${ID}`, headers: { "X-Api-Key": TOKEN } },
    { path: `/o/${ID}`, headers: { Cookie: `token=${TOKEN}` } },
    { path: `/o/${ID}?token=${TOKEN}`, headers: {} },
    { path: `/o/${ID}?access_token=${TOKEN}`, headers: {} },
  ];
  for (const { path, headers } of cases) {
    const { bucket, env } = setup();
    await assertRefusedFully(await call(env, path, { token: null, headers }), bucket);
  }
});

test("the Bearer scheme name is case-insensitive", async () => {
  const { env } = setup();
  const response = await call(env, `/o/${ID}`, {
    token: null,
    headers: { Authorization: `bearer ${TOKEN}` },
  });
  assert.equal(response.status, 200);
});

test("an unset or empty READ_TOKEN refuses everything", async () => {
  const unset = [undefined, null, "", "   ", "\n", 12345, {}];
  for (const value of unset) {
    const { bucket, env } = setup({ READ_TOKEN: value });
    const presented = [
      null,
      "",
      "undefined",
      "null",
      "[object Object]",
      String(value),
      TOKEN,
    ];
    for (const token of presented) {
      await assertRefusedFully(await call(env, `/o/${ID}`, { token }), bucket);
      await assertRefusedFully(await call(env, `/o/${ID}`, { token, method: "HEAD" }), bucket);
    }
  }
  const noBinding = { BUCKET: fakeBucket({}) };
  await assertRefusedFully(await call(noBinding, `/o/${ID}`), noBinding.BUCKET);
});

test("a secret saved with surrounding whitespace still matches", async () => {
  // Headers trim their values, so without trimming the secret it could never match.
  const { env } = setup({ READ_TOKEN: `\n${TOKEN}\n` });
  assert.equal((await call(env, `/o/${ID}`)).status, 200);
  assert.equal((await call(env, `/o/${ID}`, { token: "not-the-token" })).status, 404);
});

test("a storage failure is a 500, never a 404", async () => {
  const env = {
    READ_TOKEN: TOKEN,
    BUCKET: {
      async get() {
        throw new Error("R2 unavailable");
      },
    },
  };
  const response = await call(env, `/o/${ID}`);
  assert.equal(response.status, 500);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(await response.text(), "");
  // Unauthenticated callers never reach storage, so they still get a 404.
  await assertRefusedFully(await call(env, `/o/${ID}`, { token: "wrong-token" }), null);
});

test("the token and Authorization header are never logged", async () => {
  const logged = [];
  const methods = ["log", "info", "warn", "error", "debug", "trace"];
  const originals = methods.map((name) => console[name]);
  for (const name of methods) console[name] = (...args) => logged.push(args.join(" "));
  try {
    const { env } = setup();
    await call(env, `/o/${ID}`);
    await call(env, `/o/${ID}`, { method: "HEAD" });
    await call(env, `/o/${ID}`, { token: "wrong-token" });
    await call(env, `/o/${"a".repeat(64)}`);
    await call(env, "/nope", { method: "POST" });
    await call({ READ_TOKEN: TOKEN, BUCKET: { get: async () => { throw new Error("boom"); } } }, `/o/${ID}`);
  } finally {
    methods.forEach((name, i) => {
      console[name] = originals[i];
    });
  }
  assert.deepEqual(logged, []);
});

test("the default export delegates to handle", async () => {
  const { env } = setup();
  const request = new Request(`${ORIGIN}/o/${ID}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  const response = await worker.fetch(request, env);
  assert.equal(response.status, 200);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), BLOB);
  await assertRefusedFully(await worker.fetch(new Request(`${ORIGIN}/o/${ID}`), env), null);
});
