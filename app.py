"""
MetaWasher — Flask 메인 애플리케이션

파일 업로드, 메타데이터 삭제, 결과 다운로드를 처리하는 웹 서버.
각 업로드는 고유한 job ID로 관리되며, 처리 완료 후 개별 파일 또는
ZIP 아카이브로 다운로드할 수 있다.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from metadata_cleaner import (
    ALLOWED_EXTENSIONS,
    CleanerError,
    clean_file,
    count_non_file_tags,
    extract_gps_location,
    human_size,
    inspect_metadata,
    is_supported_extension,
    summarize_sensitive,
    tool_status,
)


# ──────────────────────────────────────────────
# 상수 정의
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent          # 프로젝트 루트 디렉토리
ENV_FILE = BASE_DIR / ".env"                        # 환경 변수 파일 경로
JOBS_DIR = BASE_DIR / "instance" / "jobs"            # 업로드/처리 파일 임시 저장 디렉토리
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024               # 최대 업로드 크기: 1GB
JOB_TTL_SECONDS = 60 * 60                           # 작업 파일 보존 시간: 1시간


def load_env_file(path: Path) -> None:
    """
    .env 파일을 읽어 환경 변수로 등록한다.

    python-dotenv 없이도 동작하도록 직접 파싱하며,
    이미 시스템 환경 변수에 설정된 키는 덮어쓰지 않는다.
    """
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 빈 줄, 주석(#), '='가 없는 줄은 무시
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")  # 따옴표 제거
        # 이미 환경 변수에 존재하면 덮어쓰지 않음
        if key and key not in os.environ:
            os.environ[key] = value


# 서버 시작 시 .env 파일을 자동으로 로드
load_env_file(ENV_FILE)


# ──────────────────────────────────────────────
# Flask 앱 생성 및 설정
# ──────────────────────────────────────────────
app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    SECRET_KEY="replace-this-secret-key-before-public-deploy",
)


# ──────────────────────────────────────────────
# 요청 전 훅: 오래된 작업 자동 정리
# ──────────────────────────────────────────────
@app.before_request
def cleanup_old_jobs() -> None:
    """
    매 요청 시 JOB_TTL_SECONDS(1시간)이 지난 작업 디렉토리를 자동 삭제한다.
    서버에 별도의 크론잡 없이도 임시 파일이 누적되지 않도록 한다.
    """
    now = time.time()
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for job_dir in JOBS_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            if now - job_dir.stat().st_mtime > JOB_TTL_SECONDS:
                shutil.rmtree(job_dir, ignore_errors=True)
        except OSError:
            continue


# ──────────────────────────────────────────────
# 라우트 핸들러
# ──────────────────────────────────────────────
@app.get("/")
def index():
    """메인 페이지를 렌더링한다. 결과 데이터 없이 업로드 폼만 표시."""
    return render_template(
        "index.html",
        results=[],
        **_template_context(),
    )


@app.post("/clean")
def clean_uploads():
    """
    업로드된 파일들의 메타데이터를 삭제하고 결과를 표시한다.

    처리 흐름:
      1. 업로드 파일을 job 디렉토리에 저장
      2. ExifTool로 삭제 전 메타데이터 분석 (GPS 위치 포함)
      3. 원본 복사 후 메타데이터 삭제
      4. 삭제 후 메타데이터 재분석 → 전/후 비교
      5. 성공한 파일들을 ZIP으로 묶어 일괄 다운로드 제공
    """
    uploaded_files = request.files.getlist("files")
    uploaded_files = [file for file in uploaded_files if file and file.filename]

    if not uploaded_files:
        flash("처리할 파일을 선택해주세요.")
        return redirect(url_for("index"))

    # 고유한 job ID로 작업 디렉토리 생성
    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    upload_dir = job_dir / "uploads"    # 원본 파일 저장 경로
    cleaned_dir = job_dir / "cleaned"   # 메타데이터 삭제된 사본 저장 경로
    upload_dir.mkdir(parents=True, exist_ok=True)
    cleaned_dir.mkdir(parents=True, exist_ok=True)

    results = []

    for uploaded in uploaded_files:
        original_name = uploaded.filename or "uploaded-file"
        safe_name = secure_filename(original_name) or f"file-{uuid.uuid4().hex}"

        # 지원하지 않는 확장자는 건너뛰고 에러 결과에 추가
        if not is_supported_extension(safe_name):
            results.append(
                {
                    "status": "error",
                    "original_name": original_name,
                    "message": "지원하지 않는 확장자입니다.",
                }
            )
            continue

        # 파일 저장 및 메타데이터 처리
        source_path = _unique_path(upload_dir, safe_name)
        uploaded.save(source_path)
        cleaned_name = _cleaned_filename(source_path.name)
        cleaned_path = _unique_path(cleaned_dir, cleaned_name)

        try:
            # 삭제 전 메타데이터 분석
            before_metadata = inspect_metadata(source_path)
            gps_location = extract_gps_location(before_metadata)

            # 메타데이터 삭제 실행
            clean_result = clean_file(source_path, cleaned_path)

            # 삭제 후 메타데이터 재분석 (잔존 태그 확인용)
            after_metadata = inspect_metadata(cleaned_path)
            before_sensitive = summarize_sensitive(before_metadata)
            after_sensitive = summarize_sensitive(after_metadata)

            results.append(
                {
                    "status": "ok",
                    "original_name": original_name,
                    "safe_name": source_path.name,
                    "cleaned_name": cleaned_path.name,
                    "download_url": url_for("download_file", job_id=job_id, filename=cleaned_path.name),
                    "original_size": human_size(source_path.stat().st_size),
                    "cleaned_size": human_size(cleaned_path.stat().st_size),
                    "before_sensitive": before_sensitive,
                    "after_sensitive": after_sensitive,
                    "gps_location": gps_location,
                    "before_count": count_non_file_tags(before_metadata),
                    "after_count": count_non_file_tags(after_metadata),
                    "engine": clean_result.engine,
                    "warnings": clean_result.warnings,
                }
            )
        except CleanerError as exc:
            results.append(
                {
                    "status": "error",
                    "original_name": original_name,
                    "message": str(exc),
                }
            )

    # 성공한 파일이 있으면 ZIP 아카이브 생성
    ok_results = [result for result in results if result.get("status") == "ok"]
    zip_url = None
    if ok_results:
        zip_path = cleaned_dir / "cleaned-files.zip"
        _build_zip(zip_path, cleaned_dir, [result["cleaned_name"] for result in ok_results])
        zip_url = url_for("download_zip", job_id=job_id)

    return render_template(
        "index.html",
        results=results,
        zip_url=zip_url,
        **_template_context(),
    )


@app.get("/download/<job_id>")
def download_zip(job_id: str):
    """job_id에 해당하는 전체 처리 결과를 ZIP 파일로 다운로드한다."""
    if not _is_safe_id(job_id):
        abort(404)
    zip_path = JOBS_DIR / job_id / "cleaned" / "cleaned-files.zip"
    if not zip_path.exists():
        abort(404)
    return send_file(zip_path, as_attachment=True, download_name="cleaned-files.zip")


@app.get("/download/<job_id>/<path:filename>")
def download_file(job_id: str, filename: str):
    """job_id 내의 개별 처리 완료 파일을 다운로드한다."""
    if not _is_safe_id(job_id):
        abort(404)

    # 경로 조작 방지: 파일명에서 디렉토리 구분자를 제거
    safe_name = Path(filename).name
    file_path = JOBS_DIR / job_id / "cleaned" / safe_name
    if not file_path.exists() or not file_path.is_file():
        abort(404)
    return send_file(file_path, as_attachment=True, download_name=safe_name)


@app.errorhandler(413)
def file_too_large(_error):
    """Flask의 MAX_CONTENT_LENGTH 초과 시 사용자에게 안내 메시지를 표시한다."""
    flash(f"파일이 너무 큽니다. 한 번에 최대 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB까지 처리할 수 있습니다.")
    return redirect(url_for("index"))


# ──────────────────────────────────────────────
# 내부 헬퍼 함수
# ──────────────────────────────────────────────
def _unique_path(directory: Path, filename: str) -> Path:
    """
    동일한 파일명이 이미 존재하면 '-1', '-2' 등의 접미사를 붙여
    충돌 없는 고유한 경로를 반환한다.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        renamed = directory / f"{stem}-{index}{suffix}"
        if not renamed.exists():
            return renamed

    # 극히 드문 경우: UUID를 접미사로 사용하여 충돌 방지
    return directory / f"{stem}-{uuid.uuid4().hex}{suffix}"


def _cleaned_filename(filename: str) -> str:
    """원본 파일명에 '_cleaned' 접미사를 추가한 새 파일명을 생성한다."""
    path = Path(filename)
    return f"{path.stem}_cleaned{path.suffix}"


def _build_zip(zip_path: Path, cleaned_dir: Path, filenames: list[str]) -> None:
    """처리 완료된 파일들을 하나의 ZIP 아카이브로 묶는다."""
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in filenames:
            file_path = cleaned_dir / Path(filename).name
            if file_path.exists() and file_path.is_file():
                archive.write(file_path, arcname=file_path.name)


def _is_safe_id(value: str) -> bool:
    """
    job ID가 유효한 UUID hex 형식(32자리 16진수)인지 검증한다.
    경로 조작(path traversal) 공격을 방지하기 위한 보안 검사.
    """
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def _template_context() -> dict[str, object]:
    """
    모든 페이지 렌더링에 공통으로 전달되는 템플릿 컨텍스트를 구성한다.
    ExifTool/FFmpeg 설치 상태, 지원 확장자, 업로드 제한, API 키 등을 포함.
    """
    return {
        "tools": tool_status(),
        "allowed_extensions": ", ".join(sorted(ext[1:].upper() for ext in ALLOWED_EXTENSIONS)),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "google_maps_api_key": os.environ.get("GOOGLE_MAPS_API_KEY", ""),
    }


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
