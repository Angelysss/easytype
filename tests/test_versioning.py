from pathlib import Path
import tomllib

from easytype_app import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_source_and_exe_metadata_versions_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version_info = (ROOT / "packaging" / "version_info.txt").read_text(
        encoding="utf-8"
    )
    version_tuple = ", ".join([*APP_VERSION.split("."), "0"])

    assert project["project"]["version"] == APP_VERSION
    assert f"filevers=({version_tuple})" in version_info
    assert f"prodvers=({version_tuple})" in version_info
    assert f'StringStruct("FileVersion", "{APP_VERSION}")' in version_info
    assert f'StringStruct("ProductVersion", "{APP_VERSION}")' in version_info
    assert f'StringStruct("OriginalFilename", "EasyType-{APP_VERSION}.exe")' in (
        version_info
    )
