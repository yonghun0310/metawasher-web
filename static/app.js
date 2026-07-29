/**
 * MetaWasher — 클라이언트 사이드 JavaScript
 *
 * 파일 업로드 UI 인터랙션, 드래그 앤 드롭, 폼 제출 상태 관리,
 * Google Maps API 연동을 담당한다.
 */

// ──────────────────────────────────────────────
// DOM 요소 참조 및 설정
// ──────────────────────────────────────────────
const input = document.querySelector("#files");
const dropzone = document.querySelector("#dropzone");
const selectedFiles = document.querySelector("#selected-files");
const form = document.querySelector(".upload-form");
const submitButton = document.querySelector("#submit-button");
const config = window.METAWASHER_CONFIG || {};

// ──────────────────────────────────────────────
// 선택된 파일 목록 표시
// ──────────────────────────────────────────────

/**
 * 선택된 파일 목록을 화면에 렌더링한다.
 * 최대 8개까지 파일명을 표시하고, 초과분은 "외 N개"로 축약한다.
 */
function renderFiles(files) {
  if (!selectedFiles) return;
  selectedFiles.innerHTML = "";

  if (!files || files.length === 0) {
    const empty = document.createElement("span");
    empty.textContent = "선택된 파일 없음";
    selectedFiles.append(empty);
    return;
  }

  // 최대 8개 파일명만 개별 표시
  [...files].slice(0, 8).forEach((file) => {
    const item = document.createElement("span");
    item.textContent = file.name;
    selectedFiles.append(item);
  });

  // 9개 이상이면 나머지 개수를 요약 표시
  if (files.length > 8) {
    const more = document.createElement("span");
    more.textContent = `외 ${files.length - 8}개`;
    selectedFiles.append(more);
  }
}

// 파일 선택 input 변경 시 파일 목록 업데이트
if (input) {
  input.addEventListener("change", () => renderFiles(input.files));
}

// ──────────────────────────────────────────────
// 드래그 앤 드롭 파일 업로드
// ──────────────────────────────────────────────
if (dropzone && input) {
  // 드래그 진입/유지 시 시각적 피드백 (테두리 색상 변경)
  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragging");
    });
  });

  // 드래그 종료/드롭 시 시각적 피드백 해제
  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("is-dragging");
    });
  });

  // 파일 드롭 시 input의 files에 할당하여 폼 제출에 포함
  dropzone.addEventListener("drop", (event) => {
    if (event.dataTransfer?.files?.length) {
      input.files = event.dataTransfer.files;
      renderFiles(input.files);
    }
  });
}

// ──────────────────────────────────────────────
// 폼 제출 시 로딩 상태 표시
// ──────────────────────────────────────────────
if (form && submitButton) {
  form.addEventListener("submit", () => {
    // 중복 제출 방지 및 처리 중 시각적 피드백
    submitButton.disabled = true;
    submitButton.textContent = "처리 중";
  });
}

// ──────────────────────────────────────────────
// Google Maps 연동 — GPS 촬영 위치 지도 표시
// ──────────────────────────────────────────────

// 결과 페이지에서 GPS 좌표가 포함된 카드 요소들을 수집
const locationCards = [...document.querySelectorAll("[data-location-card]")];

// API 키가 설정되어 있고 표시할 카드가 있으면 Google Maps를 로드
if (locationCards.length > 0 && config.googleMapsApiKey) {
  loadGoogleMaps(config.googleMapsApiKey)
    .then(() => renderLocationMaps(locationCards))
    .catch(() => {
      locationCards.forEach((card) => showMapError(card, "Google Maps를 불러오지 못했습니다."));
    });
}

/**
 * Google Maps JavaScript API를 비동기로 로드한다.
 * 이미 로드되었으면 캐시된 Promise를 반환하여 중복 요청을 방지한다.
 */
function loadGoogleMaps(apiKey) {
  // 이미 로드 완료된 경우
  if (window.google?.maps) {
    return Promise.resolve(window.google.maps);
  }

  // 로드 진행 중인 경우 (중복 스크립트 삽입 방지)
  if (window.__metawasherMapsPromise) {
    return window.__metawasherMapsPromise;
  }

  window.__metawasherMapsPromise = new Promise((resolve, reject) => {
    // Maps API 로드 완료 시 호출될 전역 콜백 함수
    window.__metawasherInitMaps = () => resolve(window.google.maps);

    const script = document.createElement("script");
    const params = new URLSearchParams({
      key: apiKey,
      loading: "async",
      callback: "__metawasherInitMaps",
      language: "ko",     // 한국어 지도 라벨
      region: "KR",       // 한국 지역 우선
    });

    script.src = `https://maps.googleapis.com/maps/api/js?${params.toString()}`;
    script.async = true;
    script.onerror = reject;
    document.head.append(script);
  });

  return window.__metawasherMapsPromise;
}

/**
 * 각 위치 카드에 Google Maps 지도와 마커를 렌더링하고,
 * 역지오코딩으로 주소를 표시한다.
 */
function renderLocationMaps(cards) {
  cards.forEach((card) => {
    const lat = Number(card.dataset.lat);
    const lng = Number(card.dataset.lng);
    const canvas = card.querySelector("[data-map-canvas]");
    const address = card.querySelector("[data-location-address]");

    if (!Number.isFinite(lat) || !Number.isFinite(lng) || !canvas) {
      showMapError(card, "유효한 GPS 좌표가 아닙니다.");
      return;
    }

    const center = { lat, lng };

    // 지도 생성 (줌 레벨 17: 건물 단위 확인 가능)
    const map = new google.maps.Map(canvas, {
      center,
      zoom: 17,
      mapTypeControl: false,
      streetViewControl: false,
      fullscreenControl: true,
    });

    // 촬영 위치에 마커 표시
    new google.maps.Marker({
      map,
      position: center,
      title: card.dataset.fileName || "촬영 위치",
    });

    // 역지오코딩: GPS 좌표 → 사람이 읽을 수 있는 주소로 변환
    const geocoder = new google.maps.Geocoder();
    geocoder.geocode({ location: center }, (results, status) => {
      if (status === "OK" && results?.[0]?.formatted_address && address) {
        address.textContent = results[0].formatted_address;
      }
    });
  });
}

/**
 * 지도 로드 실패 시 캔버스 영역에 에러 메시지를 표시한다.
 */
function showMapError(card, message) {
  const canvas = card.querySelector("[data-map-canvas]");
  if (!canvas) return;
  canvas.classList.add("map-canvas-error");
  canvas.textContent = message;
}
