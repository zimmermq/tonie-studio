"""Tests for reading tonie dumps out of a GitHub repository."""

from urllib.parse import unquote

import pytest

from tonie_studio.github import Repo, RepoError, dumps, parse_repo


class Response:
    def __init__(self, status=200, json=None, text="", headers=None):
        self.status_code = status
        self._json = json
        self.text = text
        self.headers = headers or {}
        self.content = text.encode()

    def json(self):
        return self._json


class FakeGitHub:
    """Answers the two API calls and the raw downloads of one repository."""

    def __init__(self, files, default_branch="main", truncated=False):
        self.files = files
        self.default_branch = default_branch
        self.truncated = truncated
        self.asked = []

    def __call__(self, url, headers=None, timeout=None):
        self.asked.append((url, headers or {}))
        api = "https://api.github.com/repos/owner/tonies"
        if url == api:
            return Response(json={"default_branch": self.default_branch})
        if url.startswith(f"{api}/git/trees/"):
            branch = url.split("/git/trees/")[1].split("?")[0]
            if branch != self.default_branch:
                return Response(status=404)
            tree = [{"path": path, "type": "blob"} for path in self.files]
            tree.append({"path": "Hoerspiele", "type": "tree"})
            return Response(json={"tree": tree, "truncated": self.truncated})
        wanted = unquote(url.split("?")[0])
        for path, text in self.files.items():
            if wanted.endswith("/" + path):
                return Response(text=text)
        return Response(status=404)


FILES = {
    "Hoerspiele/Serie A/Folge 1.nfc": "one",
    "Hoerspiele/Serie & Co/Folge 100%.nfc": "two",
    "Musik/Lieder.nfc": "three",
    "README.md": "readme",
}


class TestParseRepo:
    @pytest.mark.parametrize("text", [
        "owner/tonies", " https://github.com/owner/tonies ", "https://github.com/owner/tonies.git",
        "github.com/owner/tonies/",
    ])
    def test_reads_the_usual_ways_of_naming_a_repository(self, text):
        assert parse_repo(text) == Repo("owner", "tonies")

    def test_takes_branch_and_subfolder_from_a_tree_address(self):
        repo = parse_repo("https://github.com/owner/tonies/tree/main/Hoerspiele/Serie%20%26%20Co")

        assert repo == Repo("owner", "tonies", branch="main", subfolder="Hoerspiele/Serie & Co")

    def test_fields_given_separately_win(self):
        repo = parse_repo("owner/tonies/tree/main/Hoerspiele", branch="dev", subfolder="Musik/")

        assert (repo.branch, repo.subfolder) == ("dev", "Musik")

    @pytest.mark.parametrize("text", ["", "owner", "owner/../etc", "https://example.com/owner/tonies"])
    def test_refuses_anything_else(self, text):
        with pytest.raises(RepoError) as raised:
            parse_repo(text)

        assert raised.value.key == "github.bad_repo"


class TestDumps:
    def test_reads_every_dump_below_the_subfolder(self):
        github = FakeGitHub(FILES)

        found = dumps(Repo("owner", "tonies", subfolder="Hoerspiele"), get=github)

        assert found == [("Hoerspiele/Serie & Co/Folge 100%.nfc", "two"),
                         ("Hoerspiele/Serie A/Folge 1.nfc", "one")]

    def test_several_dumps_are_read_at_once(self):
        # Hundreds of dumps one after another took minutes, nearly all of it
        # spent waiting for GitHub to answer each small file.
        import threading
        import time

        class Slow(FakeGitHub):
            def __init__(self, files):
                super().__init__(files)
                self.lock = threading.Lock()
                self.now = self.most = 0

            def __call__(self, url, headers=None, timeout=None):
                if "api.github.com" in url:
                    return super().__call__(url, headers, timeout)
                with self.lock:
                    self.now += 1
                    self.most = max(self.most, self.now)
                time.sleep(0.05)
                with self.lock:
                    self.now -= 1
                return super().__call__(url, headers, timeout)

        files = {f"Serie/Folge {n:02d}.nfc": f"dump {n}" for n in range(1, 21)}
        github = Slow(files)

        found = dumps(Repo("owner", "tonies"), get=github)

        assert github.most > 1
        assert found == sorted(files.items())

    def test_reads_the_whole_repository_without_a_subfolder(self):
        found = dumps(Repo("owner", "tonies"), get=FakeGitHub(FILES))

        assert [path for path, _ in found] == ["Hoerspiele/Serie & Co/Folge 100%.nfc",
                                               "Hoerspiele/Serie A/Folge 1.nfc",
                                               "Musik/Lieder.nfc"]

    def test_a_subfolder_matches_whole_folder_names_only(self):
        with pytest.raises(RepoError) as raised:
            dumps(Repo("owner", "tonies", subfolder="Hoer"), get=FakeGitHub(FILES))

        assert raised.value.key == "github.no_dumps"

    def test_public_files_come_from_raw_addresses_without_a_token(self):
        github = FakeGitHub(FILES)

        dumps(Repo("owner", "tonies", subfolder="Musik"), get=github)

        url, headers = github.asked[-1]
        assert url == "https://raw.githubusercontent.com/owner/tonies/main/Musik/Lieder.nfc"
        assert "Authorization" not in headers

    def test_a_token_goes_with_every_request_to_the_api(self):
        github = FakeGitHub(FILES)

        dumps(Repo("owner", "tonies", subfolder="Musik"), token="secret", get=github)

        assert all(headers.get("Authorization") == "Bearer secret" for _, headers in github.asked)
        assert github.asked[-1][0].startswith("https://api.github.com/repos/owner/tonies/contents/")

    def test_an_unknown_branch_is_reported(self):
        with pytest.raises(RepoError) as raised:
            dumps(Repo("owner", "tonies", branch="nope"), get=FakeGitHub(FILES))

        assert raised.value.key == "github.not_found"

    @pytest.mark.parametrize("answer", [
        Response(text="<html>proxy error</html>"),       # json() raises
        Response(json=["not", "an", "object"]),
        Response(json={"name": "tonies"}),                 # no default_branch
    ])
    def test_an_answer_that_is_not_what_github_sends_is_reported(self, answer):
        # Anything but a RepoError would retry the run every minute, for ever.
        if answer.text:
            def broken():
                raise ValueError("Expecting value")
            answer.json = broken

        with pytest.raises(RepoError) as raised:
            dumps(Repo("owner", "tonies"), get=lambda url, headers=None, timeout=None: answer)

        assert raised.value.key == "github.unreadable"

    def test_a_cut_off_tree_is_reported_rather_than_read_in_part(self):
        with pytest.raises(RepoError) as raised:
            dumps(Repo("owner", "tonies"), get=FakeGitHub(FILES, truncated=True))

        assert raised.value.key == "github.truncated"

    def test_the_rate_limit_says_when_it_ends(self):
        def limited(url, headers=None, timeout=None):
            return Response(status=403, headers={"X-RateLimit-Remaining": "0",
                                                 "X-RateLimit-Reset": "1760000000"})

        with pytest.raises(RepoError) as raised:
            dumps(Repo("owner", "tonies"), get=limited)

        assert raised.value.key == "github.rate_limit"

    def test_an_oversized_file_is_not_a_dump(self):
        github = FakeGitHub({"big.nfc": "x" * 70_000})

        with pytest.raises(RepoError) as raised:
            dumps(Repo("owner", "tonies"), get=github)

        assert raised.value.key == "github.too_large"
