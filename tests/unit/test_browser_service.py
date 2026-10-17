"""Unit tests for the service layer's pure pieces and the CLI grammar (TDD).

Pins the port resolution (`DevToolsActivePort` first line), the launch shell,
the Chromium-family check, the ephemeral forward's argv, and every browser
subcommand's argparse grammar — without a VM.
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from microvm_control import sshx  # noqa: E402
from microvm_control.browser import service  # noqa: E402

PROFILE = "/persist/chromium"
LOG = "/persist/work/browser-chromium.log"


class ActivePortTests(unittest.TestCase):
    def test_first_line_is_the_port(self):
        self.assertEqual(service.parse_active_port("9222\n/devtools/browser/abc\n"), 9222)

    def test_empty_and_garbage_are_zero(self):
        self.assertEqual(service.parse_active_port(""), 0)
        self.assertEqual(service.parse_active_port("not a port"), 0)
        self.assertEqual(service.parse_active_port("0"), 0)
        self.assertEqual(service.parse_active_port("99999"), 0)

    def test_whitespace_is_tolerated(self):
        self.assertEqual(service.parse_active_port("  9223  \n/x"), 9223)


class UserAgentTests(unittest.TestCase):
    def test_reduced_form_from_version(self):
        ua = service.ua_from_version("Chromium 152.0.7977.82 (Developer Build)",
                                     "x86_64")
        self.assertEqual(
            ua,
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

    def test_unreadable_version_is_empty(self):
        self.assertEqual(service.ua_from_version("no digits here"), "")
        self.assertEqual(service.ua_from_version(""), "")
        self.assertEqual(service.ua_from_version("Chromium 152"), "")


class StartShellTests(unittest.TestCase):
    def test_shell_names_every_required_flag(self):
        shell = service.start_shell(port=9222, profile=PROFILE, log_path=LOG)
        self.assertIn("setsid", shell)
        self.assertIn("chromium", shell)
        self.assertIn("--headless", shell)
        self.assertIn("--no-sandbox", shell)
        self.assertIn("--remote-debugging-port=9222", shell)
        self.assertIn(f"--user-data-dir={PROFILE}", shell)
        self.assertIn("--no-first-run", shell)
        self.assertIn("--disable-extensions", shell)
        self.assertIn("--disable-blink-features=AutomationControlled", shell)
        self.assertIn(f">{LOG} 2>&1", shell)
        self.assertIn("echo $!", shell)

    def test_user_agent_rides_only_when_the_binary_backs_it(self):
        shell = service.start_shell(port=9222, profile=PROFILE, log_path=LOG)
        self.assertNotIn("--user-agent=", shell)
        user_agent = service.ua_from_version("Chromium 152.0.7977.82", "x86_64")
        shell = service.start_shell(port=9222, profile=PROFILE, log_path=LOG,
                                    user_agent=user_agent)
        self.assertIn("--user-agent=", shell)
        self.assertIn(user_agent, shell)
        self.assertNotIn("HeadlessChrome", shell)

    def test_dynamic_port_is_zero(self):
        shell = service.start_shell(port=0, profile=PROFILE, log_path=LOG)
        self.assertIn("--remote-debugging-port=0", shell)

    def test_url_is_quoted(self):
        shell = service.start_shell(port=9222, profile=PROFILE, log_path=LOG,
                                    url="https://example.com/a b")
        self.assertIn("'https://example.com/a b'", shell)


class FamilyTests(unittest.TestCase):
    def test_chromium_family_is_accepted(self):
        self.assertTrue(service.is_chromium({
            "Browser": "HeadlessChrome/151.0.0.0", "Protocol-Version": "1.3"}))
        self.assertTrue(service.is_chromium({
            "Browser": "Chromium/151.0.0.0", "Protocol-Version": "1.3"}))

    def test_other_product_is_refused(self):
        self.assertFalse(service.is_chromium({
            "Browser": "Firefox/140", "Protocol-Version": "1.3"}))


class AdoptTests(unittest.TestCase):
    PS = "\n".join([
        "1660 /usr/lib/chromium/chromium --headless --no-sandbox "
        "--remote-debugging-port=9222 --user-data-dir=/persist/chromium about:blank",
        "1724 /usr/lib/chromium/chromium --type=renderer "
        "--user-data-dir=/persist/chromium --remote-debugging-port=9222",
        "999 /usr/lib/chromium/chromium --type=zygote "
        "--user-data-dir=/persist/chromium",
    ])

    def test_main_process_is_adopted(self):
        self.assertEqual(service.parse_running(self.PS), {"pid": 1660, "port": 9222})

    def test_other_profile_is_ignored(self):
        self.assertIsNone(service.parse_running(
            "1 chromium --user-data-dir=/other --remote-debugging-port=9222"))

    def test_empty_is_none(self):
        self.assertIsNone(service.parse_running(""))


class ForwardTests(unittest.TestCase):
    def test_forward_argv_shape(self):
        argv = sshx.forward_argv(Path("/k/id"), Path("/k/known"), "172.16.0.2",
                                 12345, 9222)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-N", argv)
        self.assertIn("127.0.0.1:12345:127.0.0.1:9222", argv)
        self.assertEqual(argv[-1], "root@172.16.0.2")

    def test_free_port_is_bindable(self):
        port = sshx.free_port()
        self.assertTrue(1024 < port < 65536)
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
        finally:
            sock.close()


class ParserTests(unittest.TestCase):
    def parser(self):
        from microvm_control.__main__ import build_parser
        return build_parser()

    def test_type_delay_parses(self):
        ns = self.parser().parse_args(
            ["browser", "demo", "type", "hello", "--delay", "0.08"])
        self.assertEqual((ns.browser_verb, ns.value, ns.delay),
                         ("type", "hello", 0.08))

    def test_start_and_lifecycle(self):
        ns = self.parser().parse_args(["browser", "demo", "start", "--port", "0"])
        self.assertEqual((ns.id, ns.browser_verb, ns.port), ("demo", "start", 0))
        ns = self.parser().parse_args(["browser", "demo", "stop"])
        self.assertEqual(ns.browser_verb, "stop")

    def test_actions_and_flags(self):
        ns = self.parser().parse_args(
            ["browser", "demo", "text", "--tab", "id:ab", "--chars", "100"])
        self.assertEqual(ns.browser_verb, "text")
        self.assertEqual((ns.tab, ns.chars), ("id:ab", 100))
        ns = self.parser().parse_args(["browser", "demo", "click", "--selector", "#buy"])
        self.assertEqual((ns.browser_verb, ns.selector), ("click", "#buy"))
        ns = self.parser().parse_args(
            ["browser", "demo", "extract", "--each", "article",
             "--field", "a=h2", "--field", "b=a@href"])
        self.assertEqual(ns.field, ["a=h2", "b=a@href"])
        ns = self.parser().parse_args(
            ["browser", "demo", "wait", "--for", "js", "--expr", "window.ok"])
        self.assertEqual((ns.for_, ns.expr), ("js", "window.ok"))
        ns = self.parser().parse_args(["browser", "demo", "open", "https://a/", "https://b/"])
        self.assertEqual(ns.urls, ["https://a/", "https://b/"])
        ns = self.parser().parse_args(["browser", "demo", "screenshot", "--full"])
        self.assertTrue(ns.full)
        ns = self.parser().parse_args(["browser", "demo", "scroll", "--edge", "top"])
        self.assertEqual(ns.edge, "top")
        ns = self.parser().parse_args(["browser", "demo", "dialog", "accept", "--text", "y"])
        self.assertEqual((ns.mode, ns.text), ("accept", "y"))

    def test_legacy_flags_still_parse(self):
        ns = self.parser().parse_args(["browser", "demo", "--dom", "https://x/"])
        self.assertIsNone(ns.browser_verb)
        self.assertEqual(ns.dom, "https://x/")
        ns = self.parser().parse_args(
            ["browser", "demo", "--screenshot", "https://x/", "--out", "/tmp/x.png"])
        self.assertEqual(ns.screenshot, "https://x/")

    def test_no_action_dispatches_bad_args(self):
        ns = self.parser().parse_args(["browser", "demo"])
        self.assertIsNone(ns.browser_verb)
        self.assertIsNone(ns.dom)


if __name__ == "__main__":
    unittest.main()
