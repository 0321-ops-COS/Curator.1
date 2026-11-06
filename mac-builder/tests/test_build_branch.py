"""Build a named branch from origin, in its own worktree, and say which commit.

No Xcode and no real git: ``jobs._exec`` is replaced by a fake that records every
command, so the tests can prove where each one ran.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import jobs  # noqa: E402

ONELINE = "abc1234 Latest commit subject"


def settings(root: Path, **overrides: object) -> SimpleNamespace:
    values = dict(
        authorized_branch="main",
        repo_dir=root / "Hustler",
        worktree_root=root / "mac-builder-worktrees",
        log_dir=root / "logs",
        hustlercore_dir="ios-app/HustlerCore",
        xcode_project="ios-app/Hustler.xcodeproj",
        xcode_scheme="Hustler",
        simulator_name="",
        swift_jobs=2,
        xcodebuild_jobs=2,
        build_timeout_seconds=60.0,
        check_timeout_seconds=60.0,
        test_timeout_seconds=60.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeGit:
    """A pretend git: fetch, refs, worktree and log, plus the script itself."""

    def __init__(self, root: Path) -> None:
        self.repo = root / "Hustler"
        self.fetch_ok = True
        self.map_refs = True
        self.remote = {"main", "dan/x"}
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    async def __call__(
        self,
        argv: object,
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        log_path: Path | None = None,
        **_kwargs: object,
    ) -> "jobs.ExecOutcome":
        argv = tuple(argv)  # type: ignore[arg-type]
        self.calls.append((argv, Path(cwd), dict(env)))
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("$ " + " ".join(argv) + "\n")
        if argv[0] != "git":
            return jobs.ExecOutcome(0, False, ["** BUILD SUCCEEDED **"], [])
        args = list(argv[3:])
        if args[0] == "fetch":
            if not self.fetch_ok:
                return jobs.ExecOutcome(
                    128, False, ["fatal: could not read from remote repository"], []
                )
            refspec = args[-1]
            if refspec.startswith("+"):
                self.remote.add(refspec.split("heads/")[-1].split(":")[0])
                self.map_refs = True
            return jobs.ExecOutcome(0, False, [], [])
        if args[0] == "rev-parse":
            branch = args[-1].split("origin/", 1)[-1]
            ok = self.map_refs and branch in self.remote
            return jobs.ExecOutcome(0 if ok else 1, False, [], [])
        if args[0] == "ls-remote":
            branch = args[-1]
            if branch in self.remote:
                return jobs.ExecOutcome(0, False, [f"abcdef\trefs/heads/{branch}"], [])
            return jobs.ExecOutcome(2, False, [], [])
        if args[0] == "worktree":
            if args[1] == "add":
                tree = Path(args[-1])
                tree.mkdir(parents=True, exist_ok=True)
                (tree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            return jobs.ExecOutcome(0, False, [], [])
        if args[0] == "log":
            return jobs.ExecOutcome(0, False, [ONELINE], [ONELINE])
        return jobs.ExecOutcome(0, False, [], [])

    def subcommands(self) -> list[str]:
        return [args[3] for args, _cwd, _env in self.calls if args[0] == "git"]

    def cwds(self, sub: str) -> list[Path]:
        return [cwd for args, cwd, _env in self.calls if args[0] == "git" and args[3] == sub]


class BranchNameTests(unittest.TestCase):
    def test_the_default_is_the_authorized_branch(self) -> None:
        box = settings(Path("/tmp/x"), authorized_branch="freebuff/base")

        self.assertEqual(jobs.resolve_branch(box, None), "freebuff/base")
        self.assertEqual(jobs.resolve_branch(box, "   "), "freebuff/base")

    def test_the_authorized_branch_and_job_branches_are_accepted(self) -> None:
        box = settings(Path("/tmp/x"))

        for name in ("main", "dan/paste-complete", "freebuff/abc123", "dan/a.b_c-d"):
            self.assertEqual(jobs.resolve_branch(box, name), name)

    def test_other_names_are_refused_with_a_reason(self) -> None:
        box = settings(Path("/tmp/x"))

        for name in (
            "feature/x",
            "main2",
            "Freebuff/abc",
            "-dan/x",
            "dan/../x",
            "dan/",
            "dan//x",
            "dan/x y",
            "dan/x.lock",
            "a" * 101,
        ):
            with self.subTest(name=name), self.assertRaises(ValueError) as raised:
                jobs.resolve_branch(box, name)
            self.assertTrue(str(raised.exception).strip())

    def test_a_refused_name_says_what_is_allowed(self) -> None:
        box = settings(Path("/tmp/x"), authorized_branch="freebuff/base")

        with self.assertRaises(ValueError) as raised:
            jobs.resolve_branch(box, "feature/x")

        self.assertIn("freebuff/ or dan/", str(raised.exception))

    def test_no_branch_at_all_says_so(self) -> None:
        box = settings(Path("/tmp/x"), authorized_branch="")

        with self.assertRaises(ValueError) as raised:
            jobs.resolve_branch(box, None)

        self.assertIn("AUTHORIZED_BRANCH", str(raised.exception))


class BuildTreeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Hustler").mkdir()
        self.box = settings(self.root)
        self.fake = FakeGit(self.root)
        patcher = mock.patch.object(jobs, "_exec", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tree = self.root / "mac-builder-worktrees" / "_build"

    def build_job(self, branch: str = "dan/x") -> "jobs.Job":
        return jobs.Job(
            id="20261102-120000-deadbeef",
            kind="build",
            title="xcodegen + xcodebuild build",
            channel_id=7,
            requester_id=42,
            log_path=self.root / "job.log",
            branch=branch,
        )

    def runner(self) -> "jobs.JobRunner":
        async def post_job(_job: object, _ok: bool, _summary: str) -> None:
            return None

        async def post_review(_outcome: object) -> None:
            return None

        return jobs.JobRunner(
            self.box,
            SimpleNamespace(enabled=False),
            None,
            post_job=post_job,
            post_review=post_review,
        )

    async def test_the_build_runs_in_its_own_worktree_at_the_fetched_commit(
        self,
    ) -> None:
        target = await jobs.prepare_build_tree(self.box, "dan/x")

        self.assertTrue(target.ready, target.problem)
        self.assertEqual(target.tree, self.tree)
        self.assertEqual(target.commit, ONELINE)
        self.assertNotEqual(target.tree, self.box.repo_dir)
        self.assertEqual(self.fake.cwds("fetch"), [self.box.repo_dir])
        self.assertEqual(self.fake.cwds("checkout"), [self.tree])
        self.assertEqual(self.fake.cwds("clean"), [self.tree])

    async def test_a_failed_fetch_is_reported_and_nothing_is_checked_out(self) -> None:
        self.fake.fetch_ok = False

        target = await jobs.prepare_build_tree(self.box, "dan/x")

        self.assertFalse(target.ready)
        self.assertIn("Could not fetch origin", target.problem)
        self.assertIn("Nothing was built", target.problem)
        self.assertEqual(self.fake.subcommands(), ["fetch"])

    async def test_a_branch_missing_from_origin_is_reported(self) -> None:
        self.fake.remote = {"main"}

        target = await jobs.prepare_build_tree(self.box, "dan/nope")

        self.assertFalse(target.ready)
        self.assertIn("origin has no branch 'dan/nope'", target.problem)
        self.assertNotIn("checkout", self.fake.subcommands())
        self.assertFalse(self.tree.exists())

    async def test_a_clone_without_remote_refs_still_fetches_the_tip(self) -> None:
        self.fake.map_refs = False

        target = await jobs.prepare_build_tree(self.box, "dan/x")

        self.assertTrue(target.ready, target.problem)
        self.assertIn("ls-remote", self.fake.subcommands())

    async def test_the_script_runs_in_the_build_worktree_and_the_card_has_its_commit(
        self,
    ) -> None:
        job = self.build_job()

        ok, summary = await self.runner()._run_script(job)

        self.assertTrue(ok)
        self.assertIn("BUILD SUCCEEDED", summary)
        self.assertIn(f"dan/x @ {ONELINE}", summary)
        self.assertEqual(job.branch, "dan/x")
        self.assertEqual(job.commit_label, ONELINE)
        self.assertTrue(job.elapsed_label)
        scripts = [
            (args, cwd, env)
            for args, cwd, env in self.fake.calls
            if args[0] == "/bin/bash"
        ]
        self.assertEqual(len(scripts), 1)
        _args, cwd, env = scripts[0]
        self.assertEqual(cwd, self.tree)
        self.assertEqual(env["REPO_DIR"], str(self.tree))
        self.assertNotIn(self.box.repo_dir, [cwd for _args, cwd, _env in scripts])

    async def test_a_missing_branch_fails_without_running_the_script(self) -> None:
        self.fake.remote = {"main"}
        job = self.build_job("dan/nope")

        ok, summary = await self.runner()._run_script(job)

        self.assertFalse(ok)
        self.assertIn("origin has no branch 'dan/nope'", summary)
        self.assertEqual(job.commit_label, "", "no commit may be claimed")
        self.assertNotIn("/bin/bash", [args[0] for args, _cwd, _env in self.fake.calls])

    async def test_a_refused_branch_never_reaches_git(self) -> None:
        job = self.build_job("main2")

        ok, summary = await self.runner()._run_script(job)

        self.assertFalse(ok)
        self.assertIn("freebuff/ or dan/", summary)
        self.assertEqual(self.fake.calls, [])


class _Channel:
    def __init__(self) -> None:
        self.embeds: list[object] = []

    async def send(self, embed: object) -> None:
        self.embeds.append(embed)


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_card_names_the_branch_commit_and_elapsed_time(self) -> None:
        channel = _Channel()
        fake_self = SimpleNamespace(
            _target_channel=lambda _id: channel,
            _notification_channel=lambda _id: channel,
        )
        job = jobs.Job(
            id="20261102-120000-deadbeef",
            kind="build",
            title="xcodegen + xcodebuild build on dan/x",
            channel_id=7,
            requester_id=42,
            log_path=Path("/tmp/job.log"),
            branch="dan/x",
            commit_label=ONELINE,
            elapsed_label="1m02s",
        )
        job.status = "done"
        summary = f"BUILD SUCCEEDED on dan/x @ {ONELINE} in 1m02s."

        await bot.MacBuilderBot._post_job_result(fake_self, job, True, summary)

        embed = channel.embeds[0]
        fields = {field.name: field.value for field in embed.fields}  # type: ignore[attr-defined]
        self.assertEqual(fields["Branch"], "`dan/x`")
        self.assertIn("abc1234", fields["Commit"])
        self.assertIn("Latest commit subject", fields["Commit"])
        self.assertEqual(fields["Took"], "1m02s")
        self.assertIn("BUILD SUCCEEDED", embed.description)  # type: ignore[attr-defined]

    async def test_a_second_build_while_one_runs_says_so(self) -> None:
        sent: list[str] = []

        class _Response:
            async def send_message(self, text: str, ephemeral: bool = False) -> None:
                sent.append(text)

        running = jobs.Job(
            id="20261102-115959-cafebabe",
            kind="build",
            title="xcodegen + xcodebuild build",
            channel_id=7,
            requester_id=42,
            log_path=Path("/tmp/other.log"),
        )
        owner = SimpleNamespace(
            settings=settings(Path("/tmp/x")),
            queue=SimpleNamespace(running=running),
        )
        owner.require_allowed = mock.AsyncMock(return_value=True)
        interaction = SimpleNamespace(
            channel_id=7,
            user=SimpleNamespace(id=42),
            response=_Response(),
        )

        await bot.MacBuilderBot._queue_script_job(owner, interaction, "build", None)

        self.assertIn("already running", sent[0])
        self.assertIn("20261102-115959-cafebabe", sent[0])


if __name__ == "__main__":
    unittest.main()
