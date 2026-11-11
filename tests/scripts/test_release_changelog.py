"""--no-changelog keeps the release body's frame and drops the commit sections."""


def test_no_changelog_keeps_the_frame_without_commit_sections():
    from scripts import release

    commits = [{
        "sha": "a" * 40, "short_sha": "a" * 8, "author_name": "Dev", "author_email": "dev@example.test",
        "subject": "feat: something (#5)", "category": "features", "github_author": "@dev", "coauthors": [],
    }]

    body = release.generate_changelog(commits, "rc.1-v1.2.4", "1.2.4", repo_url="https://github.com/o/r",
                                      prev_tag="v1.2.3", no_changelog=True)

    assert "Something" not in body and "@dev" not in body
    assert "<!-- HERMES_BUILDS_TABLE -->" in body
    assert "https://github.com/o/r/compare/v1.2.3...rc.1-v1.2.4" in body


def test_an_oversized_changelog_shrinks_to_githubs_body_limit():
    """A fork's first canary after a big sync lists thousands of commits; GitHub
    refuses the draft at 125k characters, so the bullets shrink and the
    complete comparison stays one link away."""
    from scripts import release
    from scripts.releases.entrypoint import GITHUB_BODY_LIMIT

    commits = [{
        "sha": f"{i:040x}", "short_sha": f"{i:08x}", "author_name": "Dev",
        "author_email": "dev@example.test",
        "subject": f"fix: a long upstream subject line {i} " + "x" * 80,
        "category": "fixes", "github_author": "@dev", "coauthors": [],
    } for i in range(3000)]

    body = release.generate_changelog(
        commits, "v0.21.5+canary.X", "0.21.5+canary.X",
        repo_url="https://github.com/o/r", prev_tag="v0.21.4",
        max_body_chars=GITHUB_BODY_LIMIT,
    )

    assert len(body) <= GITHUB_BODY_LIMIT
    assert "more — see the full changelog link below" in body
    assert body.startswith("# Hermes Agent")
    assert "https://github.com/o/r/compare/v0.21.4...v0.21.5+canary.X" in body
    # The frame and the contributors section survive the shrink.
    assert "## 🐛 Bug Fixes" in body and "## 👥 Contributors" in body


def test_the_canary_budget_leaves_headroom_for_the_builds_table_swap():
    """desktop-bundled-release.yml later rewrites the HERMES_BUILDS_TABLE
    marker into a run link and then the download tables, growing the body
    after creation. cmd_canary must cap below the hard limit, not at it."""
    from scripts import release
    from scripts.releases.entrypoint import GITHUB_BODY_LIMIT

    assert release.RELEASE_BODY_BUDGET < GITHUB_BODY_LIMIT
    commits = [{
        "sha": f"{i:040x}", "short_sha": f"{i:08x}", "author_name": "Dev",
        "author_email": "dev@example.test",
        "subject": f"fix: a long upstream subject line {i} " + "x" * 80,
        "category": "fixes", "github_author": "@dev", "coauthors": [],
    } for i in range(3000)]

    body = release.generate_changelog(
        commits, "v0.21.5+canary.X", "0.21.5+canary.X",
        repo_url="https://github.com/o/r", prev_tag="v0.21.4",
        max_body_chars=release.RELEASE_BODY_BUDGET,
    )
    assert len(body) <= release.RELEASE_BODY_BUDGET
    assert GITHUB_BODY_LIMIT - len(body) >= 20_000
