"""A link that leaves Onyx opens in the user's browser, and never inside the app.

The failure class: Onyx is a reader, and a page from another site loaded into it
arrives with no address bar, no tab of its own and the shell's chrome around it —
and a Markdown note's link, whose target left the reader frame, took the whole
window with it. Three layers answer for it, so these check all three agree:

* the app cancels the navigation and hands the URL to Launch Services — every
  link activation, and every window a page asks for (``Onyx.swift``);
* served to a browser, where there is no such layer, the shell catches the click
  in itself and in the page it shows and asks for a window of its own instead
  (``extClick``, ``tabs_ui``), and steps aside in the app so the two never both
  act on one click;
* a note read straight from the service, with no shell around it, carries
  ``target=_blank`` (``viewer``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from onyx.tabs_ui import TABS_JS

LAUNCHER_SWIFT = Path(__file__).resolve().parent.parent / "launcher" / "Onyx.swift"

# Where each of these goes, by the one rule both layers state. "out" is the browser's.
WHERE = {
    # Onyx's own service: its pages stay in the reader.
    "http://127.0.0.1:8899/": "in",
    "http://127.0.0.1:8899/view?src=/notes/A.md": "in",
    "http://127.0.0.1:8899/quick?text=hi": "in",
    # A page's own machinery never travels.
    "javascript:void(0)": "in",
    "about:blank": "in",
    "data:text/html,hello": "in",
    "blob:http://127.0.0.1:8899/4ec0-0a24": "in",
    # Another site, another scheme, another port, another host spelling: all the browser's.
    "https://example.com/a": "out",
    "http://example.com/a": "out",
    "https://127.0.0.1:8899/view": "out",
    "http://127.0.0.1:8900/view": "out",
    "http://localhost:8899/view": "out",
    # Not the web at all: Launch Services knows what to do with each, and the reader does not.
    "mailto:someone@example.com": "out",
    "tel:+15551234567": "out",
    "file:///Users/someone/Downloads/pack.zip": "out",
    "obsidian://open?vault=CX": "out",
}


class ExternalLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.swift = LAUNCHER_SWIFT.read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("swiftc"), "swiftc is only on a developer Mac")
    def test_the_launcher_sends_exactly_the_links_that_leave_onyx_to_the_browser(self) -> None:
        # The app's own rule, compiled out of the shipped source and run: a table check, because the
        # branch that matters (scheme, host and port all three) is the one a rewrite gets wrong.
        port = re.search(r"^private let port = \d+$", self.swift, re.MULTILINE)
        base = re.search(r"^private let baseURL = .*$", self.swift, re.MULTILINE)
        rule = re.search(r"^private func leavesOnyx\(.*?^\}$", self.swift, re.MULTILINE | re.DOTALL)
        for name, found in (("port", port), ("baseURL", base), ("leavesOnyx", rule)):
            self.assertIsNotNone(found, f"the launcher no longer states {name}")
        harness = "\n".join([
            "import Foundation",
            port.group(0),
            base.group(0),
            rule.group(0),
            'for raw in CommandLine.arguments.dropFirst() {',
            '    guard let url = URL(string: raw) else { print("unparsed"); continue }',
            '    print(leavesOnyx(url) ? "out" : "in")',
            "}",
        ])
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            (work / "rule.swift").write_text(harness, encoding="utf-8")
            build = subprocess.run(
                ["swiftc", "-O", str(work / "rule.swift"), "-o", str(work / "rule")],
                capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            ran = subprocess.run([str(work / "rule"), *WHERE], capture_output=True, text=True, check=True)
        self.assertEqual(dict(zip(WHERE, ran.stdout.split())), WHERE)

    def test_the_shell_states_the_same_rule_as_the_app(self) -> None:
        # Two implementations of one rule drift. The shell's is the one that runs first, so it has to
        # say the same thing: the same schemes left alone, and origin — not host, not hostname — for
        # the web, so a link to the same host on another port or scheme still leaves.
        rule = re.search(r"function leavesOnyx\(u\)\{(.*?)\n(?=function |document\.)", TABS_JS, re.DOTALL)
        self.assertIsNotNone(rule, "the shell no longer states where a link opens")
        shell_schemes = set(re.findall(r"'(\w+):'", rule.group(1)))
        app_schemes = set(re.findall(r'"(\w+)"', re.search(r'if \[(.*?)\]\.contains\(scheme\)', self.swift).group(1)))
        self.assertEqual(shell_schemes, app_schemes)
        self.assertIn("u.origin!==location.origin", rule.group(1))
        # Caught in the shell and in the page it shows, on a plain click and a middle one, and on the way
        # up (no capture flag): a page that handles its own link has already said so, and is left alone.
        # In the app it steps aside entirely — the app's own policy takes the click, and two layers acting
        # on one click would open the browser twice.
        self.assertTrue(re.search(r"function extClick\(e\)\{if\(native\|\|", TABS_JS),
                        "the shell no longer leaves the click to the app")
        self.assertIn("document.addEventListener('click',extClick);", TABS_JS)
        self.assertIn("document.addEventListener('auxclick',extClick);", TABS_JS)
        reader = re.search(r"onReaderLoad\(\(\)=>\{try\{const w=reader\.contentWindow;(.*?)\}catch", TABS_JS)
        self.assertIn("w.addEventListener('click',extClick);", reader.group(1))
        self.assertIn("w.addEventListener('auxclick',extClick);", reader.group(1))
        self.assertIn("e.defaultPrevented", re.search(r"function extClick\(e\)\{(.*?)\ndocument\.", TABS_JS, re.DOTALL).group(1))

    def test_the_app_loads_nothing_that_leaves_onyx_into_its_own_window(self) -> None:
        # Both of the app's two doors. A navigation it decides on is cancelled and handed over, and
        # only a link the reader clicked is taken — an embedded frame (a video, a map) and a Back
        # through a page from before this rule still load in place.
        policy = re.search(r"decidePolicyFor navigationAction.*?^    \}$", self.swift, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(policy, "the launcher no longer decides where a navigation goes")
        self.assertIn("navigationAction.navigationType == .linkActivated", policy.group(0))
        self.assertIn("leavesOnyx(url)", policy.group(0))
        self.assertIn("decisionHandler(.cancel)", policy.group(0))
        self.assertIn("NSWorkspace.shared.open(url)", policy.group(0))
        window = re.search(r"createWebViewWith configuration.*?^    \}$", self.swift, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(window, "the launcher no longer answers a page asking for a window")
        # The load that used to take the shell is still there for Onyx's own pages, behind the test.
        self.assertIn("} else if leavesOnyx(url) {\n            NSWorkspace.shared.open(url)\n        } else {\n"
                      "            webView.load(URLRequest(url: url))", window.group(0))


if __name__ == "__main__":
    unittest.main()
