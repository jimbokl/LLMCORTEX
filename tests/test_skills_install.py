"""Tests for the bundled-skills installer."""
from cortex.skills_install import (
    bundled_skills_root,
    install_skills,
    list_bundled_skills,
    render_install_report,
)


def test_bundled_skills_root_exists():
    """The package directory must exist after install."""
    root = bundled_skills_root()
    assert root.exists(), f"missing skills root: {root}"
    assert root.is_dir()


def test_list_bundled_skills_returns_known_set():
    """v1 ships exactly these five skills."""
    expected = {
        "cortex-bootstrap",
        "cortex-capture-lesson",
        "cortex-search",
        "cortex-tune",
        "cortex-status",
    }
    found = set(list_bundled_skills())
    assert expected.issubset(found), f"missing skills: {expected - found}"


def test_each_bundled_skill_has_frontmatter(tmp_path):
    """Every shipped SKILL.md must start with frontmatter (name + description)."""
    for name in list_bundled_skills():
        path = bundled_skills_root() / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---"), f"{name}: missing frontmatter"
        head = text.split("---", 2)[1]
        assert "name:" in head, f"{name}: missing name field"
        assert "description:" in head, f"{name}: missing description field"


def test_install_skills_copies_all(tmp_path):
    target = tmp_path / "skills"
    report = install_skills(target)
    assert report["errors"] == []
    expected = set(list_bundled_skills())
    assert set(report["installed"]) == expected
    for name in expected:
        assert (target / name / "SKILL.md").exists()


def test_install_skills_skip_when_present(tmp_path):
    target = tmp_path / "skills"
    install_skills(target)
    report2 = install_skills(target)
    assert report2["installed"] == []
    expected = set(list_bundled_skills())
    assert set(report2["skipped"]) == expected


def test_install_skills_force_overwrites(tmp_path):
    target = tmp_path / "skills"
    install_skills(target)
    # Mutate one of the installed files so we can detect overwrite.
    sample = target / "cortex-status" / "SKILL.md"
    sample.write_text("MODIFIED", encoding="utf-8")
    report = install_skills(target, force=True)
    assert "cortex-status" in report["installed"]
    assert sample.read_text(encoding="utf-8").startswith("---")


def test_install_skills_only_filter(tmp_path):
    target = tmp_path / "skills"
    report = install_skills(target, only=["cortex-bootstrap"])
    assert report["installed"] == ["cortex-bootstrap"]
    assert (target / "cortex-bootstrap" / "SKILL.md").exists()
    assert not (target / "cortex-search").exists()


def test_install_skills_unknown_name_reports_error(tmp_path):
    target = tmp_path / "skills"
    report = install_skills(target, only=["nonexistent-skill"])
    assert report["installed"] == []
    assert any("unknown skill" in err for _, err in report["errors"])


def test_render_install_report_includes_target_and_counts(tmp_path):
    target = tmp_path / "skills"
    report = install_skills(target)
    text = render_install_report(report)
    assert "Cortex skills install" in text
    assert "Installed" in text
    assert "cortex-bootstrap" in text


def test_render_install_report_skipped_section(tmp_path):
    target = tmp_path / "skills"
    install_skills(target)
    report = install_skills(target)  # second run = all skipped
    text = render_install_report(report)
    assert "Skipped" in text


def test_install_creates_target_dir(tmp_path):
    target = tmp_path / "deeply" / "nested" / "skills"
    assert not target.exists()
    report = install_skills(target)
    assert target.exists()
    assert report["installed"]
