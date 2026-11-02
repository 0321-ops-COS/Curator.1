import json
import unittest
from unittest import mock

import httpx

import github_client

TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0"  # split: a fixture, not a credential
OWNER, REPO = "0321-ops-COS", "Curator.1"


def client(handler, token: str | None = TOKEN) -> github_client.GitHubClient:
    transport = httpx.MockTransport(handler)
    return github_client.GitHubClient(OWNER, REPO, token, transport=transport)


class TokenTests(unittest.TestCase):
    def test_the_token_is_required_masked_and_never_printed(self) -> None:
        instance = client(lambda request: httpx.Response(200), token=None)
        for call in (lambda: instance.find_pr("dan/x", "main"), lambda: instance.get_pr(1),
                     lambda: instance.ci_states("abc"), lambda: instance.merge_pr(1, "abc")):
            with self.assertRaises(github_client.GitHubError) as raised:
                call()
            self.assertEqual(str(raised.exception), "MANAGER_GITHUB_TOKEN is not set")
        with mock.patch.dict("os.environ", {"MANAGER_GITHUB_TOKEN": TOKEN}):
            self.assertEqual(github_client.GitHubClient.from_env(OWNER, REPO).token, TOKEN)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": f"Bad credentials {TOKEN}"})

        leaky = client(handler)
        self.assertNotIn(TOKEN, repr(leaky))
        with self.assertRaises(github_client.GitHubError) as raised:
            leaky.get_pr(1)
        self.assertIn("401", str(raised.exception))
        self.assertNotIn(TOKEN, str(raised.exception))


class RequestTests(unittest.TestCase):
    def test_requests_use_the_headers_host_and_timeout(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=[])

        instance = client(handler)
        self.assertIsNone(instance.find_pr("dan/x", "main"))  # empty list means no PR
        request = seen[0]
        self.assertEqual(request.url.host, "api.github.com")
        self.assertEqual(request.headers["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(request.headers["accept"], "application/vnd.github+json")
        self.assertEqual(request.headers["x-github-api-version"], "2022-11-28")
        self.assertEqual(request.url.params["head"], f"{OWNER}:dan/x")
        self.assertEqual(instance._client.timeout.read, 20.0)

    def test_open_pr_posts_head_base_title_body_and_hints_at_the_page(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(201, json={"number": 7})

        self.assertEqual(client(handler).open_pr("dan/x", "main", "T", "B")["number"], 7)
        self.assertEqual(bodies[0], {"title": "T", "body": "B", "head": "dan/x", "base": "main"})
        hint = github_client.revert_hint(OWNER, REPO, 7)
        self.assertIn(f"https://github.com/{OWNER}/{REPO}/pull/7", hint)
        self.assertIn("press Revert on that page", hint)

    def test_get_pr_retries_a_null_mergeable_then_gives_up(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={"mergeable": None, "head": {"sha": "abc"}})

        with mock.patch.object(github_client.time, "sleep"):
            pull = client(handler).get_pr(3)
        self.assertEqual(len(calls), 4)
        self.assertIsNone(pull["mergeable"])
        self.assertEqual(pull["head_sha"], "abc")

    def ci(self, runs: list[dict], statuses: list[dict], state: str) -> tuple[str, ...]:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/check-runs"):
                return httpx.Response(200, json={"check_runs": runs})
            return httpx.Response(200, json={"state": state, "statuses": statuses})

        return client(handler).ci_states("abc")

    def test_ci_states_maps_check_runs_and_the_commit_status(self) -> None:
        runs = [{"status": "completed", "conclusion": "neutral"},
                {"status": "completed", "conclusion": "timed_out"},
                {"status": "in_progress", "conclusion": None}]
        self.assertEqual(self.ci(runs, [{}], "failure"),
                         ("success", "failure", "pending", "failure"))
        self.assertEqual(self.ci([], [], "pending"), ())

    def test_merge_pr_sends_the_method_and_the_expected_sha(self) -> None:
        bodies: list[dict] = []
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"merged": True})

        self.assertTrue(client(handler).merge_pr(7, "abc123")["merged"])
        self.assertEqual(methods, ["PUT"])
        self.assertEqual(bodies[0], {"merge_method": "merge", "sha": "abc123"})
        with self.assertRaises(github_client.GitHubError) as raised:
            client(lambda request: httpx.Response(409, json={})).merge_pr(7, "abc")
        self.assertIn("branch head changed", str(raised.exception))
