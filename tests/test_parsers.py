import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("dnfshelf", os.path.join(HERE, "..", "dnfshelf.py"))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

PLAN = """Package   Arch   Version   Repository   Size
Upgrading:
 bash x86_64 5.2.37-2.fc42 updates 1.8 MiB
   replacing bash x86_64 5.2.37-1.fc42 @System 1.8 MiB
Installing dependencies:
 foo noarch 1.0-1.fc42 fedora 12.0 KiB

Transaction Summary:
 Installing:   1 packages
 Upgrading:    1 packages
 Replacing:    1 packages

Need to download 1.8 MiB.
After this operation, 12 KiB extra will be used (install 12 KiB, remove 0 B).
"""

HISTORY = """Transaction ID : 42
Begin time     : Mon 24 Nov 2026 10:00:00
End time       : Mon 24 Nov 2026 10:00:09
User           : Foo Bar (1000)
Status         : Ok
Description    : dnf upgrade
Packages altered:
 Action    Package                         Reason     Repository
 Upgrade   bash-0:5.2.37-2.fc42.x86_64     User       updates
 Replaced  bash-0:5.2.37-1.fc42.x86_64     User       @System
 Install   foo-1.0-1.fc42.noarch           Dependency updates
 Remove    bar-2.0-1.fc42.x86_64           User       @System
"""


class PlanTests(unittest.TestCase):
    def test_sections_and_stats(self):
        sections, stats = d.parse_plan(PLAN)
        self.assertEqual(dict(sections)["Upgrading"], [["bash", "5.2.37-2.fc42", "1.8 MiB", "5.2.37-1.fc42"]])
        self.assertEqual(stats, [("Download size", "1.8 MiB"), ("Disk space", "+12 KiB")])

    def test_count_mismatch_is_rejected(self):
        self.assertIsNone(d.parse_plan(PLAN.replace("Upgrading:    1", "Upgrading:    2")))

    def test_problem_lines(self):
        text = "Failed to resolve the transaction:\nProblem: conflict\n\nYou can try to add --allowerasing"
        self.assertEqual(d.plan_problem(text), "Problem: conflict")


class TransactionTests(unittest.TestCase):
    def test_grouped_with_previous_version(self):
        sections, stats = d.parse_transaction(HISTORY)
        got = dict(sections)
        self.assertEqual(got["Upgraded"], [["bash", "5.2.37-2.fc42", "User", "5.2.37-1.fc42"]])
        self.assertEqual(got["Installed"][0][:3], ["foo", "1.0-1.fc42", "Dependency"])
        self.assertIn("Removed", got)
        self.assertNotIn("Replaced", got)
        self.assertIn(("Command", "dnf upgrade"), stats)

    def test_no_packages(self):
        self.assertIsNone(d.parse_transaction("Transaction ID : 1\nStatus : Ok\n"))


class InfoTests(unittest.TestCase):
    def test_second_block_is_dropped(self):
        text = "Name : bash\nVersion : 1\nRelease : 1\nName : bash\nVersion : 2\nRelease : 1\n"
        intro, rows = d.parse_info(text)
        self.assertEqual(rows, [["Name", "bash"], ["Version", "1"], ["Release", "1"]])

    def test_plain_text_is_not_info(self):
        self.assertIsNone(d.parse_info("No information available."))


class DetailTabTests(unittest.TestCase):
    def test_deps(self):
        text = "Depends on (packages) (2):\n  glibc\n  zlib\n\nRequired by (packages) (0):"
        self.assertEqual(d.parse_deps(text), [("Depends on (packages) (2)", ["glibc", "zlib"]),
                                              ("Required by (packages) (0)", [])])
        self.assertIsNone(d.parse_deps("Error: boom"))

    def test_files(self):
        self.assertEqual(d.parse_files("/usr/bin/a\n/usr/bin/b"), ["/usr/bin/a", "/usr/bin/b"])
        self.assertIsNone(d.parse_files("No file list available."))

    def test_changelog(self):
        text = "Changelogs for bash-5.2\n* Mon Jun 03 2024 A <a@b> - 5.2-1\n- One\n- Two\n"
        heading, entries = d.parse_changelog(text)
        self.assertEqual(heading, "Changelogs for bash-5.2")
        self.assertEqual(entries, [("Mon Jun 03 2024 A <a@b> - 5.2-1", ["- One", "- Two"])])
        self.assertIsNone(d.parse_changelog("No changelog available."))


class RepoTests(unittest.TestCase):
    def test_repos(self):
        text = "repo id   repo name   status\nfedora   Fedora 42 - x86_64   enabled\nrpmfusion   RPM Fusion   disabled\n"
        self.assertEqual(d.parse_repos(text), [("fedora", "Fedora 42 - x86_64", True),
                                               ("rpmfusion", "RPM Fusion", False)])

    def test_rpm_install_plan(self):
        argv, preview, dry = d.rpm_install_plan(["/tmp/a b.rpm", "/tmp/c.rpm"])
        self.assertEqual(argv, ["pkexec", "dnf5", "-y", "install", "/tmp/a b.rpm", "/tmp/c.rpm"])
        self.assertEqual(dry, [["dnf5", "install", "/tmp/a b.rpm", "/tmp/c.rpm", "--assumeno"]])
        self.assertIn("'/tmp/a b.rpm'", preview)

    def test_classify_repo_location(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "good", "repodata"))
            open(os.path.join(root, "good", "repodata", "repomd.xml"), "w").close()
            os.mkdir(os.path.join(root, "empty"))
            open(os.path.join(root, "x.repo"), "w").close()
            for loc, want in (("https://dl.example.org/x.repo", "repofile"),
                              (os.path.join(root, "x.repo"), "repofile"),
                              (os.path.join(root, "good"), "folder"),
                              (os.path.join(root, "empty"), "norepodata"),
                              ("https://example.org/page", ""), ("", ""), ("/no/such/x.repo", "")):
                self.assertEqual(d.classify_repo_location(loc), want, loc)
        self.assertEqual(d.local_repo_id("/srv/my repo/"), "local-my-repo")

    def test_repo_id_taken(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "local-a.repo"), "w").close()
            self.assertTrue(d.repo_id_taken("local-a", {}, root))
            self.assertTrue(d.repo_id_taken("fedora", {"fedora": 1}, root))
            self.assertFalse(d.repo_id_taken("local-b", {"fedora": 1}, root))


if __name__ == "__main__":
    unittest.main()
