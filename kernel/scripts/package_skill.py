#!/usr/bin/env python3
"""Skill Packager - Creates a distributable .skill file of a skill folder."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quick_validate import validate_skill
from backend.services.skill_portability import add_portable_files_to_zip


def _is_within(path_obj: Path, root: Path) -> bool:
    try:
        path_obj.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def package_skill(skill_path, output_dir=None, *, portable: bool = False, skip_validate: bool = False, portable_style: str = "inline"):
    skill_path = Path(skill_path).resolve()
    if not skill_path.exists():
        print(f"❌ Error: Skill folder not found: {skill_path}")
        return None
    if not skill_path.is_dir():
        print(f"❌ Error: Path is not a directory: {skill_path}")
        return None
    if not (skill_path / "SKILL.md").exists():
        print(f"❌ Error: SKILL.md not found in {skill_path}")
        return None

    if not skip_validate and not portable:
        print("🔍 Validating skill...")
        valid, message = validate_skill(skill_path)
        if not valid:
            print(f"❌ Validation failed: {message}")
            print("   Please fix the validation errors before packaging.")
            return None
        print(f"✅ {message}\n")
    elif skip_validate or portable:
        print("⚠️  Skipping quick_validate for portable packaging." if portable else "⚠️  Skipping quick_validate.")

    output_path = Path(output_dir).resolve() if output_dir else Path.cwd()
    output_path.mkdir(parents=True, exist_ok=True)
    skill_filename = output_path / f"{skill_path.name}.skill"

    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            arc_prefix = f"{skill_path.name}/"
            portable_written_scripts: set[str] = set()
            if portable:
                report = add_portable_files_to_zip(zipf, skill_path, arc_prefix=arc_prefix, portable_style=portable_style)
                portable_written_scripts = set(report.patched_scripts)
                # Even unpatched scripts were written by add_portable_files_to_zip.
                portable_written_scripts.update(p.relative_to(skill_path).as_posix() for p in (skill_path / "scripts").rglob("*.py"))
            for file_path in skill_path.rglob("*"):
                if file_path.is_symlink():
                    continue
                if not file_path.is_file():
                    continue
                if not _is_within(file_path, skill_path):
                    raise ValueError(f"Refusing to package path outside skill root: {file_path}")
                if file_path.resolve() == skill_filename.resolve():
                    continue
                rel = file_path.relative_to(skill_path).as_posix()
                if portable and (rel in portable_written_scripts or rel.startswith("_portable_runtime/") or rel in {"requirements-portable.txt", ".env.example", "skill-portability.json"}):
                    continue
                arcname = file_path.relative_to(skill_path.parent)
                zipf.write(file_path, arcname)
                print(f"  Added: {arcname}")
        print(f"\n✅ Successfully packaged skill to: {skill_filename}")
        return skill_filename
    except Exception as e:
        print(f"❌ Error creating .skill file: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Package a skill folder")
    parser.add_argument("skill_path")
    parser.add_argument("output_dir", nargs="?")
    parser.add_argument("--portable", action="store_true")
    parser.add_argument("--skip-validate", action="store_true")
    parser.add_argument("--portable-style", choices=["inline", "package"], default="inline")
    args = parser.parse_args()
    print(f"📦 Packaging skill: {args.skill_path}")
    if args.output_dir:
        print(f"   Output directory: {args.output_dir}")
    result = package_skill(args.skill_path, args.output_dir, portable=args.portable, skip_validate=args.skip_validate, portable_style=args.portable_style)
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
