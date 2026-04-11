"""Installer for the bundled Claude Code skills.

Cortex ships a small set of `SKILL.md` files under `cortex/skills/<name>/`.
This module copies them into the user's `~/.claude/skills/` directory (or
into a project-local `.claude/skills/` directory) so Claude Code can
auto-activate them based on description matching.

Each bundled skill is copied as `cortex-<name>` so it shows up in the
agent's skill registry namespaced to Cortex without colliding with
unrelated skills the user may have installed.

This is a thin file-copy. Skills are pure markdown plus YAML frontmatter
-- no code is executed during install. The installer is idempotent and
prints what it did.
"""
from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path
from typing import Any


def bundled_skills_root() -> Path:
    """Return the package directory holding bundled skills.

    Uses `importlib.resources` so it works whether the package was
    installed via wheel, sdist, or in editable mode.
    """
    # files() returns a Traversable; for our use case the package is
    # always on a real filesystem (no zipimport), so casting to Path
    # is safe.
    return Path(str(resources.files("cortex").joinpath("skills")))


def list_bundled_skills() -> list[str]:
    """Return sorted names of every bundled skill (directory names)."""
    root = bundled_skills_root()
    if not root.exists():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and (p / "SKILL.md").exists()
    )


def default_user_skills_dir() -> Path:
    """Return `~/.claude/skills/`. Created lazily by the install step."""
    return Path.home() / ".claude" / "skills"


def default_project_skills_dir(project_root: Path | None = None) -> Path:
    """Return `<project>/.claude/skills/`. Created lazily by install."""
    root = project_root or Path.cwd()
    return root / ".claude" / "skills"


def install_skills(
    target_dir: Path,
    *,
    only: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Copy bundled skills into `target_dir`.

    Args:
        target_dir: where to install (e.g. `~/.claude/skills`)
        only: optional list of skill names to install (without prefix);
            None means install all bundled skills.
        force: overwrite existing skill directories. When False, an
            existing directory causes that skill to be reported as
            `skipped` instead of installed.

    Returns a dict report:
        {
            "target": str absolute path,
            "installed": list[str] of skill names that landed,
            "skipped":   list[str] of skill names already present,
            "errors":    list[(skill_name, error_message)],
        }

    The function never raises -- a per-skill error is captured in
    `errors`. Use the report to render output for the user.
    """
    root = bundled_skills_root()
    target_dir = target_dir.expanduser()
    report: dict[str, Any] = {
        "target": str(target_dir.resolve() if target_dir.exists() else target_dir),
        "installed": [],
        "skipped": [],
        "errors": [],
    }

    available = list_bundled_skills()
    if not available:
        report["errors"].append(("(none)", "no bundled skills found in package"))
        return report

    selected = available if only is None else [s for s in available if s in set(only)]
    if only and not selected:
        missing = sorted(set(only) - set(available))
        report["errors"].append(
            ("(selection)", f"unknown skill name(s): {', '.join(missing)}")
        )
        return report

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        report["errors"].append(("(target)", f"cannot create {target_dir}: {e}"))
        return report

    for name in selected:
        src = root / name
        dst = target_dir / name
        try:
            if dst.exists():
                if not force:
                    report["skipped"].append(name)
                    continue
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            report["installed"].append(name)
        except Exception as e:
            report["errors"].append((name, str(e)))

    # Re-resolve target now that the directory definitely exists.
    report["target"] = str(target_dir.resolve())
    return report


def render_install_report(report: dict[str, Any]) -> str:
    """Format an install_skills report for human-readable stdout."""
    lines: list[str] = []
    lines.append(f"Cortex skills install -> {report['target']}")
    lines.append("=" * 60)

    if report["installed"]:
        lines.append(f"Installed ({len(report['installed'])}):")
        for n in report["installed"]:
            lines.append(f"  + {n}")
    if report["skipped"]:
        lines.append(f"Skipped (already present, use --force to overwrite):")
        for n in report["skipped"]:
            lines.append(f"  = {n}")
    if report["errors"]:
        lines.append(f"Errors ({len(report['errors'])}):")
        for name, err in report["errors"]:
            lines.append(f"  ! {name}: {err}")

    if not (report["installed"] or report["skipped"] or report["errors"]):
        lines.append("(no skills processed)")

    if report["installed"]:
        lines.append("")
        lines.append(
            "Claude Code will auto-activate these skills based on their"
        )
        lines.append(
            "description frontmatter. Restart your Claude Code session to"
        )
        lines.append("pick up newly installed skills.")

    return "\n".join(lines)
