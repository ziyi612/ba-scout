"""Check and stage verified GitHub Releases for the Windows portable application."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

RELEASES = "https://github.com/ziyi612/ba-scout/releases"
LATEST_RELEASE = "https://api.github.com/repos/ziyi612/ba-scout/releases/latest"
_MAX_METADATA = 1024 * 1024
_MAX_DOWNLOAD = 400 * 1024 * 1024
_MAX_EXPANDED = 800 * 1024 * 1024
_MAX_FILES = 20_000
_CHUNK = 128 * 1024
_STAGE_PREFIX = "ba-scout-update-"
_EXECUTABLE = "BA-Scout.exe"
_PRESET = ("data", "prep-layout-default.json")
_HELPER = "install_update.ps1"
_INVALID_WINDOWS_NAME = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_DEVICE = re.compile(r"(?i:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])")
_DOWNLOAD_HOSTS = frozenset({"release-assets.githubusercontent.com", "objects.githubusercontent.com"})
_RELEASE_METADATA_FILES = frozenset({"LICENSE", "README.md", "THIRD_PARTY_NOTICES.md"})


class UpdateError(Exception):
    """The update cannot safely proceed."""


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    asset_url: str
    size: int
    sha256: str


def _version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}", value):
        raise UpdateError("发布版本号无效。")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _asset_url(version: str) -> str:
    return f"{RELEASES}/download/v{version}/ba-scout-{version}-windows-x64.zip"


class _ReleaseRedirect(HTTPRedirectHandler):
    """A release asset may redirect from GitHub to its asset CDN over HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if (not req.full_url.startswith(f"{RELEASES}/download/")
                and urlsplit(req.full_url).hostname not in _DOWNLOAD_HOSTS):
            raise UpdateError("发布信息被重定向，已停止更新。")
        if target.scheme != "https" or target.netloc not in _DOWNLOAD_HOSTS:
            raise UpdateError("下载地址离开了 GitHub 发布站点，已停止更新。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, *, metadata: bool = False):
    accept = "application/vnd.github+json" if metadata else "application/octet-stream"
    request = Request(url, headers={
        "User-Agent": "BA-Scout portable updater",
        "Accept": accept,
        "Accept-Encoding": "identity",
    })
    return build_opener(_ReleaseRedirect()).open(request, timeout=10 if metadata else 20)


def check_update(current_version: str) -> Release | None:
    """Fetch the stable release from the project's own public repository."""
    current = _version(current_version)
    try:
        with _open(LATEST_RELEASE, metadata=True) as response:
            body = response.read(_MAX_METADATA + 1)
        if len(body) > _MAX_METADATA:
            raise UpdateError("GitHub 发布信息超出大小限制。")
        metadata = json.loads(body)
    except (HTTPException, OSError, ValueError) as exc:
        raise UpdateError("无法读取 GitHub 发布信息，请稍后重试。") from exc
    if not isinstance(metadata, dict):
        raise UpdateError("GitHub 发布信息格式无效。")
    if metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        raise UpdateError("GitHub 未返回正式发布版本。")
    tag = metadata.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("GitHub 发布标签格式无效。")
    version = tag[1:]
    if _version(version) <= current:
        return None
    name = f"ba-scout-{version}-windows-x64.zip"
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub 发布附件列表无效。")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        raise UpdateError("正式发布中没有唯一的 Windows x64 便携包。")
    asset = matches[0]
    url = asset.get("browser_download_url")
    size = asset.get("size")
    digest = asset.get("digest")
    if (asset.get("state") != "uploaded" or url != _asset_url(version)
            or type(size) is not int or not 0 < size <= _MAX_DOWNLOAD
            or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)):
        raise UpdateError("便携包的下载地址、大小或 SHA-256 校验值无效。")
    notes = metadata.get("body")
    return Release(version, notes[:4000] if isinstance(notes, str) else "", url, size, digest[7:].lower())


def _safe_path(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return not (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def install_supported(root: Path) -> bool:
    """Only the running, packaged Windows executable can update in place."""
    root = Path(root)
    binary = root / _EXECUTABLE
    internal = root / "_internal"
    helper_folder = internal / "ba_scout"
    helper = helper_folder / _HELPER
    try:
        return (sys.platform == "win32" and bool(getattr(sys, "frozen", False))
                and Path(sys.executable).resolve() == binary.resolve()
                and all(_safe_path(path) for path in (root, binary, internal, helper_folder, helper))
                and binary.is_file() and helper.is_file())
    except OSError:
        return False


def _zip_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    original = info.orig_filename
    parts = tuple(original.removesuffix("/").split("/"))
    mode = stat.S_IFMT(info.external_attr >> 16)
    expected_mode = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
    if (parts[0] != "BA-Scout" or len(parts) == 1 and not info.is_dir()
            or mode not in (0, expected_mode) or info.external_attr & 0x400
            or not info.is_dir() and info.external_attr & 0x10
            or info.flag_bits & 1):
        raise UpdateError("便携包中有格式不受支持的条目。")
    for part in parts:
        if (not part or part.endswith((" ", ".")) or len(part) > 255
                or _INVALID_WINDOWS_NAME.search(part)
                or _DEVICE.fullmatch(part.partition(".")[0].rstrip(" "))):
            raise UpdateError("便携包中有不安全的文件路径。")
    return parts[1:]


def _managed(parts: tuple[str, ...]) -> bool:
    return parts == (_EXECUTABLE,) or parts == _PRESET or parts[:1] == ("_internal",)


def _extract(archive_path: Path, payload: Path) -> None:
    """Validate the complete archive, then extract only the managed files."""
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > _MAX_FILES or sum(entry.file_size for entry in entries) > _MAX_EXPANDED:
            raise UpdateError("便携包解压后超出大小限制。")
        tree = {}
        files = {}
        explicit = set()
        for entry in entries:
            parts = _zip_parts(entry)
            if parts and not (_managed(parts) or parts == ("data",) and entry.is_dir()
                              or parts[0] in {"docs", "licenses"}
                              or len(parts) == 1 and parts[0] in _RELEASE_METADATA_FILES):
                raise UpdateError("便携包中含有未知的安装内容。")
            key = tuple(part.casefold() for part in parts)
            if key in explicit:
                raise UpdateError("便携包中有重复的文件路径。")
            explicit.add(key)
            for depth in range(1, len(parts) + 1):
                typed = (parts[:depth], entry.is_dir() or depth < len(parts))
                if tree.setdefault(key[:depth], typed) != typed:
                    raise UpdateError("便携包中有冲突的文件路径。")
            if not entry.is_dir():
                files[parts] = entry
        required = ((_EXECUTABLE,), _PRESET, ("_internal", "base_library.zip"),
                    ("_internal", "ba_scout", _HELPER))
        if any(parts not in files or files[parts].file_size == 0 for parts in required):
            raise UpdateError("便携包缺少安装所需文件。")
        for parts, entry in files.items():
            if _managed(parts):
                destination = payload.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target, length=_CHUNK)


def stage_update(root: Path, release: Release) -> Path:
    """Download and verify a package without changing the current installation."""
    root = Path(root)
    if not install_supported(root):
        raise UpdateError("此运行方式仅支持检查更新，请从发布页下载便携包。")
    _version(release.version)
    if (release.asset_url != _asset_url(release.version)
            or not 0 < release.size <= _MAX_DOWNLOAD
            or not re.fullmatch("[0-9a-f]{64}", release.sha256)):
        raise UpdateError("待更新包元数据无效。")
    temporary = root / "tmp"
    staged = None
    finished = False
    try:
        temporary.mkdir(exist_ok=True)
        if not _safe_path(temporary) or not temporary.is_dir():
            raise UpdateError("更新暂存目录不可用。")
        staged = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=temporary))
        archive_path = staged / "download.zip"
        digest = hashlib.sha256()
        size = 0
        with _open(release.asset_url) as response, archive_path.open("xb") as archive:
            while block := response.read(_CHUNK):
                size += len(block)
                if size > release.size:
                    raise UpdateError("下载的便携包大小与 GitHub 发布信息不一致。")
                digest.update(block)
                archive.write(block)
        if size != release.size or digest.hexdigest() != release.sha256:
            raise UpdateError("下载的便携包未通过 SHA-256 校验。")
        _extract(archive_path, staged / "payload")
        archive_path.unlink()
        finished = True
        return staged
    except (HTTPException, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise UpdateError("无法下载或解压更新包，请稍后重试。") from exc
    finally:
        if staged is not None and not finished:
            shutil.rmtree(staged, ignore_errors=True)


def launch_install(root: Path, staged: Path) -> None:
    """Start the detached installer. The application should then exit normally."""
    if not install_supported(root):
        raise UpdateError("此运行方式仅支持检查更新，请从发布页下载便携包。")
    root = Path(root).resolve()
    staged = Path(staged)
    temporary = root / "tmp"
    if (staged.parent != temporary or not re.fullmatch(r"ba-scout-update-[a-z0-9_]+", staged.name)
            or not all(_safe_path(path) for path in (temporary, staged, staged / "payload"))):
        raise UpdateError("更新暂存目录无效。")
    helper = root / "_internal" / "ba_scout" / _HELPER
    runner = staged / _HELPER
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    created = False
    try:
        with runner.open("xb") as target, helper.open("rb") as source:
            created = True
            shutil.copyfileobj(source, target)
        subprocess.Popen(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(runner), "-Root", str(root), "-StageName", staged.name,
             "-ParentPid", str(os.getpid())],
            cwd=staged,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        if created:
            try:
                runner.unlink(missing_ok=True)
            except OSError:
                pass
        raise UpdateError("无法启动更新安装程序，请稍后重试。") from exc
