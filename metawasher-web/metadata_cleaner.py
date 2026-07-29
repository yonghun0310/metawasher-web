"""
MetaWasher — 메타데이터 분석 및 삭제 핵심 모듈

ExifTool과 FFmpeg를 활용하여 이미지/동영상 파일의 메타데이터를
읽고, 민감 태그를 식별하며, 안전하게 삭제하는 기능을 제공한다.

처리 우선순위:
  1. ExifTool (이미지/동영상 모두 지원)
  2. FFmpeg (동영상 전용 폴백 — ExifTool 실패 시 보조 처리)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ──────────────────────────────────────────────
# 지원 확장자 정의
# ──────────────────────────────────────────────

# 처리 가능한 이미지 확장자 (EXIF 메타데이터를 포함할 수 있는 형식)
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
}

# 처리 가능한 동영상 확장자 (컨테이너 메타데이터를 포함할 수 있는 형식)
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".3gp",
}

# 전체 지원 확장자 (이미지 + 동영상)
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# ──────────────────────────────────────────────
# 민감 메타데이터 토큰 정의
# ──────────────────────────────────────────────
# 메타데이터 키 이름에 이 토큰이 포함되면 "민감 정보"로 분류한다.
# 카테고리별 분류:
#   - 위치 정보: gps, latitude, longitude, location, position, altitude
#   - 기기 정보: make, model, device, camera, lens, serial
#   - 개인 정보: owner, author, artist, creator, phone, email
#   - 소프트웨어: software
#   - 네트워크:  host, hostname, ip, ipaddress, ipaddr, address
SENSITIVE_TOKENS = {
    "gps",
    "latitude",
    "longitude",
    "location",
    "position",
    "altitude",
    "make",
    "model",
    "device",
    "camera",
    "lens",
    "serial",
    "owner",
    "author",
    "artist",
    "creator",
    "software",
    "host",
    "hostname",
    "ip",
    "ipaddress",
    "ipaddr",
    "address",
    "phone",
    "email",
}

# ──────────────────────────────────────────────
# 메타데이터 분석 시 무시할 시스템/파일 레벨 키
# ──────────────────────────────────────────────
# ExifTool이 항상 출력하는 파일 시스템 속성으로,
# 실제 메타데이터 태그 수 계산이나 민감도 분석에서 제외한다.
IGNORED_METADATA_KEYS = {
    "SourceFile",
    "ExifTool:ExifToolVersion",
    "System:FileName",
    "System:Directory",
    "System:FileSize",
    "System:FileModifyDate",
    "System:FileAccessDate",
    "System:FileInodeChangeDate",
    "System:FilePermissions",
    "File:FileType",
    "File:FileTypeExtension",
    "File:MIMEType",
}


# ──────────────────────────────────────────────
# 예외 및 데이터 클래스
# ──────────────────────────────────────────────

class CleanerError(RuntimeError):
    """메타데이터 분석 또는 삭제 과정에서 발생하는 예외."""


@dataclass(frozen=True)
class ToolStatus:
    """ExifTool과 FFmpeg의 설치 경로 (없으면 None)."""
    exiftool: str | None
    ffmpeg: str | None


@dataclass(frozen=True)
class CleanResult:
    """메타데이터 삭제 결과를 담는 불변 데이터 클래스."""
    engine: str          # 사용된 처리 엔진 ("ExifTool" 또는 "FFmpeg")
    warnings: list[str]  # 처리 중 발생한 경고 메시지 목록


# ──────────────────────────────────────────────
# 공개 API 함수
# ──────────────────────────────────────────────

def tool_status() -> ToolStatus:
    """시스템에 설치된 ExifTool과 FFmpeg의 경로를 반환한다."""
    return ToolStatus(exiftool=shutil.which("exiftool"), ffmpeg=shutil.which("ffmpeg"))


def is_supported_extension(filename: str) -> bool:
    """파일 확장자가 처리 가능한 형식인지 확인한다."""
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def is_video(filename: str | Path) -> bool:
    """파일이 동영상 확장자를 가지는지 확인한다."""
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


def inspect_metadata(path: Path) -> dict[str, Any]:
    """
    ExifTool을 사용하여 파일의 전체 메타데이터를 JSON으로 읽어 반환한다.

    옵션 설명:
      -json: JSON 형식으로 출력
      -n  : 숫자 값을 변환하지 않고 원본 그대로 출력
      -a  : 중복 태그도 모두 표시
      -G1 : 태그 이름 앞에 그룹명 접두사 추가 (예: GPS:GPSLatitude)
    """
    exiftool = shutil.which("exiftool")
    if not exiftool:
        raise CleanerError("ExifTool이 설치되어 있지 않아 메타데이터를 읽을 수 없습니다.")

    completed = _run(
        [
            exiftool,
            "-json",
            "-n",
            "-a",
            "-G1",
            str(path),
        ],
        timeout=60,
    )

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CleanerError("ExifTool 결과를 JSON으로 해석하지 못했습니다.") from exc

    if not data:
        return {}

    return data[0]


def clean_file(source: Path, destination: Path) -> CleanResult:
    """
    파일의 메타데이터를 삭제하여 정리된 사본을 생성한다.

    처리 전략:
      1. 원본을 destination으로 복사
      2. ExifTool이 있으면 ExifTool로 메타데이터 삭제 시도
      3. ExifTool이 없거나 실패하면, 동영상인 경우 FFmpeg로 폴백
      4. macOS 확장 속성(xattr)도 추가 정리
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)

    exiftool = shutil.which("exiftool")
    warnings: list[str] = []

    if exiftool:
        result = _clean_with_exiftool(exiftool, destination)
        warnings.extend(result.warnings)
        _clear_macos_xattrs(destination)
        return CleanResult(engine=result.engine, warnings=warnings)

    # ExifTool 미설치 시 동영상은 FFmpeg로 처리 시도
    if is_video(source):
        result = _clean_video_with_ffmpeg(source, destination)
        warnings.extend(result.warnings)
        _clear_macos_xattrs(destination)
        return CleanResult(engine=result.engine, warnings=warnings)

    raise CleanerError("ExifTool이 설치되어 있지 않아 이미지 메타데이터를 삭제할 수 없습니다.")


def summarize_sensitive(metadata: dict[str, Any], limit: int = 12) -> list[dict[str, str]]:
    """
    메타데이터에서 민감한 태그만 추출하여 요약 목록을 반환한다.

    결과 화면에서 삭제 전/후 비교에 사용된다.
    최대 limit개까지만 반환하여 UI 과부하를 방지한다.
    """
    matches: list[dict[str, str]] = []

    for key, value in sorted(metadata.items(), key=lambda item: item[0].lower()):
        if key in IGNORED_METADATA_KEYS:
            continue
        if not _is_sensitive_key(key):
            continue
        if value in (None, "", [], {}):
            continue
        matches.append({"key": key, "value": _short_value(value)})
        if len(matches) >= limit:
            break

    return matches


def extract_gps_location(metadata: dict[str, Any]) -> dict[str, float | str] | None:
    """
    메타데이터에서 GPS 좌표를 추출하여 위치 정보 딕셔너리를 반환한다.

    다양한 메타데이터 형식을 지원:
      - EXIF GPS 태그 (Composite:GPSLatitude, GPS:GPSLatitude)
      - QuickTime 동영상 GPS 태그 (QuickTime:GPSLatitude)
      - 복합 좌표 문자열 (GPSCoordinates, GPSPosition)

    반환값에는 위도, 경도, 표시 라벨, Google Maps URL이 포함되며,
    고도 정보가 있으면 함께 포함한다.
    """
    # 여러 가능한 키에서 위도/경도 값을 탐색
    latitude = _metadata_float(metadata, ("Composite:GPSLatitude", "GPS:GPSLatitude", "QuickTime:GPSLatitude"))
    longitude = _metadata_float(metadata, ("Composite:GPSLongitude", "GPS:GPSLongitude", "QuickTime:GPSLongitude"))

    # 개별 키에서 못 찾으면 복합 좌표 문자열에서 파싱 시도
    if latitude is None or longitude is None:
        coordinates = _metadata_value(metadata, ("QuickTime:GPSCoordinates", "Composite:GPSPosition"))
        parsed = _parse_coordinate_pair(coordinates)
        if parsed:
            latitude, longitude = parsed

    if latitude is None or longitude is None:
        return None

    # GPS 참조(N/S, E/W)를 적용하여 부호 보정
    latitude = _apply_gps_ref(latitude, _metadata_value(metadata, ("GPS:GPSLatitudeRef",)))
    longitude = _apply_gps_ref(longitude, _metadata_value(metadata, ("GPS:GPSLongitudeRef",)))

    # 유효 범위 검증: 위도 -90~90, 경도 -180~180
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    altitude = _metadata_float(metadata, ("Composite:GPSAltitude", "GPS:GPSAltitude"))
    location: dict[str, float | str] = {
        "lat": round(latitude, 7),
        "lng": round(longitude, 7),
        "label": f"{latitude:.6f}, {longitude:.6f}",
        "maps_url": f"https://www.google.com/maps?q={latitude:.7f},{longitude:.7f}",
    }
    if altitude is not None:
        location["altitude"] = round(altitude, 2)
    return location


def count_non_file_tags(metadata: dict[str, Any]) -> int:
    """시스템/파일 레벨 태그를 제외한 실제 메타데이터 태그 수를 반환한다."""
    return sum(1 for key in metadata if key not in IGNORED_METADATA_KEYS)


def human_size(size: int) -> str:
    """바이트 단위 크기를 사람이 읽기 쉬운 형식(KB, MB, GB)으로 변환한다."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ──────────────────────────────────────────────
# 내부 삭제 엔진 함수
# ──────────────────────────────────────────────

def _clean_with_exiftool(exiftool: str, destination: Path) -> CleanResult:
    """
    ExifTool로 파일의 모든 메타데이터를 삭제한다.

    `-all=` 옵션으로 모든 태그를 제거하며,
    LargeFileSupport를 활성화하여 대용량 파일도 처리 가능.
    실패 시 동영상이면 FFmpeg로 폴백한다.
    """
    try:
        completed = _run(
            [
                exiftool,
                "-overwrite_original",  # 원본 백업 파일(_original) 생성 방지
                "-all=",                # 모든 메타데이터 태그 삭제
                "-api",
                "LargeFileSupport=1",   # 2GB 이상 파일 지원
                str(destination),
            ],
            timeout=180,
            check=False,
        )
    except CleanerError:
        # ExifTool 실행 자체가 실패하면 동영상인 경우 FFmpeg로 폴백
        if is_video(destination):
            return _clean_video_with_ffmpeg(destination, destination)
        raise

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode == 0:
        warnings = [line.strip() for line in output.splitlines() if "warning" in line.lower()]
        return CleanResult(engine="ExifTool", warnings=warnings)

    # ExifTool이 0이 아닌 종료 코드를 반환하면 동영상은 FFmpeg로 재시도
    if is_video(destination):
        return _clean_video_with_ffmpeg(destination, destination)

    raise CleanerError(output or "ExifTool이 메타데이터 삭제에 실패했습니다.")


def _clean_video_with_ffmpeg(source: Path, destination: Path) -> CleanResult:
    """
    FFmpeg로 동영상 컨테이너의 메타데이터를 삭제한다.

    영상/음성 스트림은 재인코딩 없이 복사(-c copy)하고,
    컨테이너 레벨 메타데이터만 제거(-map_metadata -1)한다.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CleanerError("동영상 보조 처리를 위한 FFmpeg가 설치되어 있지 않습니다.")

    # 임시 파일로 출력 후 원본 위치로 교체
    temp = destination.with_suffix(f".ffmpeg-clean{destination.suffix}")
    if temp.exists():
        temp.unlink()

    completed = _run(
        [
            ffmpeg,
            "-y",                 # 출력 파일 덮어쓰기 허용
            "-i",
            str(source),
            "-map",
            "0",                  # 모든 스트림 복사
            "-map_metadata",
            "-1",                 # 모든 메타데이터 제거
            "-c",
            "copy",              # 코덱 재인코딩 없이 스트림 복사
            str(temp),
        ],
        timeout=300,
        check=False,
    )

    if completed.returncode != 0 or not temp.exists():
        if temp.exists():
            temp.unlink()
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        raise CleanerError(output or "FFmpeg가 동영상 메타데이터 삭제에 실패했습니다.")

    # 임시 파일을 최종 목적지로 교체 (원자적 이동)
    temp.replace(destination)
    return CleanResult(engine="FFmpeg", warnings=[])


# ──────────────────────────────────────────────
# 내부 유틸리티 함수
# ──────────────────────────────────────────────

def _run(args: list[str], timeout: int, check: bool = True) -> subprocess.CompletedProcess[str]:
    """
    외부 명령을 실행하고 결과를 반환하는 공통 래퍼.
    타임아웃 및 OS 에러를 CleanerError로 변환한다.
    """
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CleanerError("외부 도구 실행 시간이 초과되었습니다.") from exc
    except OSError as exc:
        raise CleanerError(f"외부 도구를 실행하지 못했습니다: {exc}") from exc

    if check and completed.returncode != 0:
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        raise CleanerError(output or f"명령 실행 실패: {' '.join(args)}")

    return completed


def _clear_macos_xattrs(path: Path) -> None:
    """macOS 확장 속성(com.apple.quarantine 등)을 제거하여 추가 메타데이터를 정리한다."""
    xattr = shutil.which("xattr")
    if not xattr or os.name != "posix":
        return

    subprocess.run([xattr, "-c", str(path)], capture_output=True, check=False)


def _short_value(value: Any) -> str:
    """메타데이터 값을 UI 표시에 적합한 짧은 문자열로 변환한다 (최대 90자)."""
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())  # 연속 공백을 하나로 축약
    if len(text) > 90:
        return f"{text[:87]}..."
    return text


def _is_sensitive_key(key: str) -> bool:
    """
    메타데이터 키가 민감 정보에 해당하는지 판별한다.

    CamelCase 키를 단어 단위로 분리한 뒤 SENSITIVE_TOKENS과 매칭하고,
    'IPAddress'처럼 붙어 있는 토큰도 compact 비교로 검출한다.
    """
    # "GPS:GPSLatitude" → "GPSLatitude"로 그룹 접두사 제거
    key_without_group = key.split(":", 1)[-1]

    # CamelCase를 공백으로 분리 (예: "GPSLatitude" → "GPS Latitude")
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", key_without_group)
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced)
    tokens = set(re.findall(r"[a-z0-9]+", spaced.lower()))

    # 소문자+영숫자만 남긴 compact 형태로도 비교 (예: "ipaddress")
    compact = re.sub(r"[^a-z0-9]", "", key_without_group.lower())
    return bool(tokens & SENSITIVE_TOKENS) or compact in SENSITIVE_TOKENS


def _metadata_value(metadata: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """여러 후보 키 중 첫 번째로 유효한 값을 찾아 반환한다."""
    for key in keys:
        value = metadata.get(key)
        if value not in (None, ""):
            return value
    return None


def _metadata_float(metadata: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """여러 후보 키 중 첫 번째로 유효한 값을 float로 변환하여 반환한다."""
    value = _metadata_value(metadata, keys)
    if value is None:
        return None
    return _coerce_float(value)


def _coerce_float(value: Any) -> float | None:
    """다양한 형식의 값(int, float, 문자열)을 float로 안전하게 변환한다."""
    if isinstance(value, (int, float)):
        return float(value)

    # 문자열에서 첫 번째 숫자 패턴 추출 (예: "37.5665 deg" → 37.5665)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    return float(match.group(0))


def _parse_coordinate_pair(value: Any) -> tuple[float, float] | None:
    """
    복합 좌표 문자열(예: '37.5665 126.9780')에서
    위도/경도 쌍을 파싱하여 반환한다.
    """
    if value in (None, ""):
        return None

    numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", str(value))
    if len(numbers) < 2:
        return None

    return float(numbers[0]), float(numbers[1])


def _apply_gps_ref(value: float, ref: Any) -> float:
    """
    GPS 참조 방향(N/S/E/W)에 따라 좌표값의 부호를 보정한다.

    남위(S)와 서경(W)은 음수로, 북위(N)와 동경(E)은 양수로 변환.
    """
    if not ref:
        return value

    normalized = str(ref).strip().upper()
    if normalized.startswith(("S", "W")):
        return -abs(value)
    if normalized.startswith(("N", "E")):
        return abs(value)
    return value
