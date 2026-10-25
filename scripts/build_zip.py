"""Build a clean, root-layout MaiBot plugin ZIP under dist/."""

from __future__ import annotations

import json
import re
from pathlib import Path
from posixpath import isabs
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo



PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_FILES = (
    Path("_manifest.json"),
    Path("plugin.py"),
    Path("webui.json"),
    Path("docs/plugin-implementation.md"),
)
CONFIG_FILE = Path("config.toml")
PACKAGE_ROOT = Path("maiwill")
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def collect_package_files(project_root: Path = PROJECT_ROOT) -> tuple[str, list[tuple[Path, str]]]:
    """Validate required plugin files and return stable ZIP member names."""
    project_root = project_root.resolve()
    required_files = (*FIXED_FILES[:3], CONFIG_FILE, FIXED_FILES[3])
    missing = [path.as_posix() for path in required_files if not (project_root / path).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing required plugin package files: {', '.join(missing)}")
    for relative_path in required_files:
        path = project_root / relative_path
        if path.is_symlink() or not path.resolve().is_relative_to(project_root):
            raise ValueError(f"Plugin package file must remain within the project and cannot be a symlink: {relative_path}")

    manifest = json.loads((project_root / "_manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("_manifest.json must declare a three-part numeric version")

    package_files = sorted(
        path.relative_to(project_root)
        for path in (project_root / PACKAGE_ROOT).rglob("*.py")
        if path.is_file()
    )
    if not package_files or Path("maiwill/__init__.py") not in package_files:
        raise FileNotFoundError("maiwill package must contain Python modules and __init__.py")
    package_root = project_root / PACKAGE_ROOT
    if package_root.is_symlink() or not package_root.resolve().is_relative_to(project_root):
        raise ValueError("MaiWill package directory must remain within the project and cannot be a symlink")
    for relative_path in package_files:
        path = project_root / relative_path
        if path.is_symlink() or not path.resolve().is_relative_to(project_root):
            raise ValueError(f"MaiWill module must remain within the project and cannot be a symlink: {relative_path}")

    members = [(project_root / path, path.as_posix()) for path in FIXED_FILES[:3]]
    members.append((project_root / Path("config.toml"), "config.toml"))
    members.append((project_root / FIXED_FILES[3], "README.md"))
    members.extend((project_root / path, path.as_posix()) for path in package_files)
    webui = json.loads((project_root / "webui.json").read_text(encoding="utf-8"))
    for entry in sorted({page["entry"] for page in webui.get("pages", []) if page.get("mode") == "custom"}):
        relative = Path(entry)
        source = project_root / relative
        if (
            relative.is_absolute() or relative.drive or ".." in relative.parts
            or chr(92) in entry or relative.suffix.lower() != ".html"
            or not source.resolve().is_relative_to(project_root)
            or any(path.is_symlink() for path in (source, *source.parents) if path != project_root and path.is_relative_to(project_root))
        ):
            raise ValueError("Custom WebUI entry must be an HTML file inside the plugin, without symlinks")
        if not source.is_file():
            raise FileNotFoundError(f"Missing custom WebUI entry: {entry}")
        source.read_text(encoding="utf-8")
        if source.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Custom WebUI entry exceeds 2 MiB")
        members.append((source, relative.as_posix()))
    return version, members


def build_zip(project_root: Path = PROJECT_ROOT) -> Path:
    """Create a deterministic plugin archive without repository or test files."""
    project_root = project_root.resolve()
    version, members = collect_package_files(project_root)
    dist = project_root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    archive_path = dist / f"maiwill-{version}.zip"
    temporary_path = archive_path.with_suffix(".zip.tmp")

    try:
        with ZipFile(temporary_path, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for source, member_name in members:
                info = ZipInfo(member_name, date_time=FIXED_ZIP_TIME)
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, source.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)

        validate_zip(temporary_path, expected_members={name for _, name in members})
        temporary_path.replace(archive_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return archive_path


def validate_zip(archive_path: Path, *, expected_members: set[str] | None = None) -> None:
    """Reject incomplete, unsafe, or development-only ZIP contents."""
    with ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Plugin ZIP contains duplicate paths")
        if expected_members is not None and set(names) != expected_members:
            raise ValueError("Plugin ZIP contents do not match the staged plugin files")
        if not {"_manifest.json", "plugin.py", "webui.json", "config.toml", "README.md"}.issubset(names):
            raise ValueError("Plugin ZIP must keep its manifest, entry point, WebUI declaration, and README at the root")
        if any(
            isabs(name)
            or chr(92) in name
            or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
            or any(part in {".git", "tests", "logs", "__pycache__"} for part in name.split("/"))
            for name in names
        ):
            raise ValueError("Plugin ZIP contains an unsafe or non-runtime path")
        broken_member = archive.testzip()
        if broken_member is not None:
            raise ValueError(f"Plugin ZIP CRC check failed for {broken_member}")
        manifest = json.loads(archive.read("_manifest.json"))
        webui = json.loads(archive.read("webui.json"))
        if not manifest.get("id") or not re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", "")):
            raise ValueError("Plugin ZIP manifest is missing a valid id or version")
        config = archive.read("config.toml").decode("utf-8")
        if "[plugin]" not in config or "config_version" not in config:
            raise ValueError("Plugin ZIP config.toml must declare [plugin].config_version")
        if webui.get("schema_version") != 2:
            raise ValueError("Plugin ZIP must declare WebUI schema version 2")
        for page in webui.get("pages", []):
            if page.get("mode") == "custom" and page.get("entry") not in names:
                raise ValueError("Plugin ZIP is missing a declared custom WebUI entry")


if __name__ == "__main__":
    print(build_zip())
