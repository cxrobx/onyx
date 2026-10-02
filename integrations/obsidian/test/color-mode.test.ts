import assert from "node:assert/strict";
import test from "node:test";

import { bodySignature, inOtherMode } from "../src/color-mode.ts";

/** Just enough of <body>: a real-order class list and a style attribute. */
function body(className: string, style: string | null = null) {
  let classes = className.split(" ").filter(Boolean);
  const classList = {
    contains: (name: string) => classes.includes(name),
    replace: (from: string, to: string) => {
      const at = classes.indexOf(from);
      if (at < 0) return false;
      classes[at] = to;
      return true;
    },
  } as unknown as DOMTokenList;
  return {
    classList,
    get className() { return classes.join(" "); },
    getAttribute: (name: string) => (name === "style" ? style : null),
  };
}

test("reads in the other mode and puts the class back where it was", () => {
  const dark = body("mod-macos theme-dark is-focused");
  const before = bodySignature(dark);
  assert.equal(inOtherMode(dark, () => dark.className), "mod-macos theme-light is-focused");
  assert.equal(bodySignature(dark), before, "an observer comparing signatures sees no change");

  const light = body("theme-light");
  assert.equal(inOtherMode(light, () => light.classList.contains("theme-dark")), true);
  assert.equal(light.className, "theme-light");
});

test("puts the class back even when the read throws", () => {
  const dark = body("theme-dark");
  assert.throws(() => inOtherMode(dark, () => { throw new Error("boom"); }));
  assert.equal(dark.className, "theme-dark");
});

test("a body with no mode class has no other mode", () => {
  let read = false;
  assert.equal(inOtherMode(body("mod-macos"), () => { read = true; return 1; }), undefined);
  assert.equal(read, false);
});

test("the signature changes with the class or the inline style", () => {
  assert.notEqual(bodySignature(body("theme-dark")), bodySignature(body("theme-light")));
  assert.notEqual(bodySignature(body("theme-dark", "--x: 1")), bodySignature(body("theme-dark")));
});
