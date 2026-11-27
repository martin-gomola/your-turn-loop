"""Validate portable skill sources, the installer catalog and public content."""
from __future__ import annotations

import hashlib
import itertools
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAX_SKILL_LINES = 200
CLI_SKILLS = {"codex-image", "delegate-code", "second-opinion"}  # skills that run external CLIs


def has_error_recovery_table(text: str) -> bool:
    """True when an `## Error Recovery` section holds one contiguous table with 3+ filled columns and 2+ filled rows."""
    match = re.search(r"(?m)^## Error Recovery\s*$(.*?)(?=^## |\Z)", text, re.S)
    if not match:
        return False
    for is_table, block in itertools.groupby(match.group(1).splitlines(), lambda line: line.strip().startswith("|")):
        if not is_table:
            continue
        table = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in block]
        if len(table) < 4:
            continue
        header, divider, *rows = table
        filled = lambda cells: sum(bool(cell) for cell in cells) >= 3
        if filled(header) and sum(bool(re.fullmatch(r":?-+:?", cell)) for cell in divider) >= 3 and all(map(filled, rows)):
            return True
    return False


def referenced_paths(text: str) -> set[str]:
    """Local `references/` and `scripts/` paths named in backticks or as bare tokens."""
    found = set(re.findall(r"`((?:references|scripts)/[^`\n]+)`", text))
    bare_text = re.sub(r"`(?:references|scripts)/[^`\n]+`", "", text)
    for token in re.findall(r"(?<![\w./-])(?:references|scripts)/[\w./-]+", bare_text):
        found.add(token if token.endswith("..") else token.rstrip(".,;:)"))
    return found


def validate_skill(folder: Path) -> dict:
    if not (folder / "LICENSE").is_file():
        raise ValueError(f"{folder.name}: standalone installations must include a license")
    text = (folder / "SKILL.md").read_text()
    if len(text.splitlines()) > MAX_SKILL_LINES:
        raise ValueError(f"{folder.name}: SKILL.md exceeds the {MAX_SKILL_LINES} line limit")
    for reference in sorted(referenced_paths(text)):
        target = (folder / reference).resolve()
        if not target.is_relative_to(folder.resolve()) or not target.is_file():
            raise ValueError(f"{folder.name}: referenced file {reference} is missing or outside the skill")
    if any(folder.rglob(".mcp.json")):
        raise ValueError(f"{folder.name}: .mcp.json is not allowed; skills must not require MCP")
    if folder.name in CLI_SKILLS and not any(has_error_recovery_table(path.read_text()) for path in [folder / "SKILL.md", *sorted(folder.glob("references/*.md"))]):
        raise ValueError(f"{folder.name}: CLI skill needs an Error Recovery table")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError(f"{folder.name}: missing frontmatter")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict) or not {"name", "description"} <= set(metadata) or set(metadata) - {"name", "description", "metadata"}:
        raise ValueError(f"{folder.name}: expected portable name and description")
    if metadata["name"] != folder.name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", folder.name):
        raise ValueError(f"{folder.name}: invalid skill name")
    if not isinstance(metadata["description"], str) or not 1 <= len(metadata["description"]) <= 1024:
        raise ValueError(f"{folder.name}: invalid description")
    for fixture in ("eval.yaml", "triggers.yaml"):
        data = yaml.safe_load((folder / "tests" / fixture).read_text())
        if data.get("skill") != folder.name:
            raise ValueError(f"{folder.name}: incorrect fixture identity")
    for case in yaml.safe_load((folder / "tests" / "eval.yaml").read_text()).get("cases", []):
        trajectory = case.get("rubric", {}).get("trajectory")
        if trajectory is None:
            continue
        steps = [step for key in ("in_order", "forbidden") for step in trajectory.get(key, [])] if isinstance(trajectory, dict) else []
        if not steps or set(trajectory) - {"in_order", "forbidden"} or not all(isinstance(step, str) and step.strip() for step in steps):
            raise ValueError(f"{folder.name}: invalid trajectory in eval case {case.get('name')}")
    source_manifest = yaml.safe_load((folder / "sources.yaml").read_text()) if (folder / "sources.yaml").exists() else {"sources": []}
    for source in source_manifest["sources"]:
        if re.match(r"https?://", source["locator"]):
            continue  # remote source; only local snapshots are checksummed
        target = (folder / source["locator"]).resolve()
        if not target.is_relative_to(folder.resolve()):
            raise ValueError("source path escapes skill")
        if hashlib.sha256(target.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("source checksum mismatch")
    grounding = yaml.safe_load((folder / "grounding.yaml").read_text()) if (folder / "grounding.yaml").exists() else {"claims": []}
    for claim in grounding["claims"]:
        target = (folder / claim.get("target", "SKILL.md")).resolve()
        if not target.is_relative_to(folder.resolve()) or claim["instruction"] not in target.read_text():
            raise ValueError(f"{folder.name}: stale grounded instruction")
    details = metadata.get("metadata")
    if not isinstance(details, dict) or not re.fullmatch(r"\d+\.\d+\.\d+", str(details.get("version", ""))):
        raise ValueError(f"{folder.name}: missing or invalid skill version")
    return metadata


def validate(root: Path = ROOT) -> None:
    skills = {folder.name: validate_skill(folder) for folder in sorted((root / "skills").iterdir()) if folder.is_dir()}
    catalog = json.loads((root / 'site/catalog.json').read_text())
    ids = [skill['id'] for skill in catalog['skills']]
    if not skills or len(ids) != len(set(ids)) or set(ids) != set(skills):
        raise ValueError('installer catalog must list every skill exactly once')
    for skill in catalog['skills']:
        target = (root / skill['sourcePath']).resolve()
        if skill['sourcePath'] != f"skills/{skill['id']}" or not target.is_relative_to((root / 'skills').resolve()):
            raise ValueError('installer references an invalid skill')
        if skill.get('version') != skills[skill['id']]['metadata']['version']:
            raise ValueError('installer and skill versions differ')
    for path in root.rglob("*"):
        if not path.is_file() or any(part in {".git", "__pycache__", "tmp", "dist", ".venv", ".pytest_cache", ".code-review-graph", ".second-opinion", ".delegate-code"} for part in path.relative_to(root).parts):
            continue
        text = path.read_text(errors="replace")
        if re.search(r"/(?:Users|home)/[A-Za-z0-9_.-]+/|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9]{20,}", text):
            raise ValueError(f"private path or token in {path.relative_to(root)}")
        if path.name == ".env" or path.suffix == ".env":
            raise ValueError("environment files must not be packaged")


if __name__ == "__main__":
    validate()
    print("Skill sources, provenance, installer catalog and public-content scan passed.")
