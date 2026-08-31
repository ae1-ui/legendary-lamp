"""WATERMARK TOOL - 사진 크기 맞춤 · 톤/색감 보정 · PNG 워터마크를 한 화면에서 처리하는 로컬 웹앱.

처리 순서는 항상 아래와 같으며, 워터마크는 언제나 마지막입니다.
  1. EXIF orientation 보정
  2. 크롭 / 리사이즈 (출력 캔버스에 맞춤)
  3. 톤 보정
  4. 색감 보정 (Sharpness 포함)
  5. 워터마크 합성   ← 보정 효과가 워터마크에 절대 적용되지 않음
  6. 파일 저장

설정은 두 종류입니다.
  · 이미지별 설정 - 출력 크기 · 맞춤 방식 · 크롭 위치 · 톤 · 색감 · 워터마크 (사진마다 따로 저장)
  · 공통 설정     - 배경색 · 저장 형식 · 파일 이름 · 워터마크 동일 적용 여부
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import streamlit as st
from PIL import Image, ImageFilter, ImageOps

try:                                        # 마우스 드래그 크롭 UI (없어도 앱은 동작한다)
    from streamlit_cropper import st_cropper
    CROPPER_AVAILABLE = True
except ImportError:                         # pragma: no cover - 설치되지 않은 환경
    st_cropper = None
    CROPPER_AVAILABLE = False

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_WATERMARK_PATH = APP_DIR / "watermark.png"

SUPPORTED_TYPES = ["jpg", "jpeg", "png", "webp"]
JPEG_QUALITY = 95
WEBP_QUALITY = 95
PREVIEW_MAX_SIDE = 1200
CROP_EDITOR_MAX_SIDE = 680           # 드래그 편집기에 넘길 축소본 크기
CACHE_PIXEL_LIMIT = 6_000_000        # 이보다 큰 캔버스는 캐시하지 않는다 (메모리 보호)
MAX_ZOOM = 12.0

CANVAS_PRESETS = {
    "preset_3_4": (1080, 1440),
    "preset_9_16": (1080, 1920),
}
CANVAS_LABELS = {
    "preset_3_4": "1080 × 1440 px  (3:4)",
    "preset_9_16": "1080 × 1920 px  (9:16)",
    "custom": "직접 입력",
    "original": "원본 크기 그대로",
}
CANVAS_KEYS = list(CANVAS_LABELS)
LEGACY_CANVAS_MODES = {"preset": "preset_3_4"}

DEFAULTS = {
    # --- 이미지별: 출력 캔버스 ---
    "canvas_mode": "preset_3_4",
    "canvas_w": 1080,
    "canvas_h": 1440,

    # --- 이미지별: 크기 맞춤 · 크롭 위치 ---
    "fit_mode": "crop",               # "crop" | "fit" | "stretch" | "expand"(추후 지원)
    "crop_x": 50.0,                   # 0=왼쪽, 100=오른쪽
    "crop_y": 50.0,                   # 0=위,   100=아래
    "crop_zoom": 1.0,                 # 1.0 = 프레임을 꽉 채움, 클수록 확대

    # --- 이미지별: 톤 보정 (전부 0 이면 원본 그대로) ---
    "brightness": 0,
    "exposure": 0,
    "contrast": 0,
    "highlights": 0,
    "shadows": 0,

    # --- 이미지별: 색감 보정 ---
    "saturation": 0,
    "temperature": 0,                 # 음수=차갑게, 양수=따뜻하게
    "tint": 0,                        # 음수=green, 양수=magenta
    "sharpness": 0,                   # 음수=부드럽게, 양수=선명하게

    # --- 이미지별: 워터마크 ---
    "size_mode": "percent",           # "percent" | "pixel"
    "size_percent": 25.0,             # 출력 캔버스 너비 대비 %
    "size_px": 300,
    "opacity": 70,                    # 0 ~ 100
    "position": "bottom_right",
    "margin_mode": "percent",         # "percent" | "pixel"
    "margin_x_percent": 2.0,
    "margin_y_percent": 1.2,
    "margin_x_px": 24,
    "margin_y_px": 24,
    "custom_unit": "percent",         # "percent" | "pixel"
    "custom_x_percent": 78.0,
    "custom_y_percent": 88.0,
    "custom_x_px": 0,
    "custom_y_px": 0,

    # --- 공통 ---
    "bg_color": "#FFFFFF",
    "output_format": "jpeg",          # "jpeg" | "keep"(원본 형식 유지)
    "name_suffix": "_edited",
    "watermark_sync": True,           # 워터마크를 모든 사진에 동일하게 맞출지
}

# 톤·색감 보정값. "보정 초기화" 와 "전체 이미지에 적용" 의 대상이다 (크롭 위치는 제외).
ADJUSTMENT_KEYS = (
    "brightness", "exposure", "contrast", "highlights", "shadows",
    "saturation", "temperature", "tint", "sharpness",
)

WATERMARK_KEYS = (
    "size_mode", "size_percent", "size_px", "opacity", "position",
    "margin_mode", "margin_x_percent", "margin_y_percent", "margin_x_px", "margin_y_px",
    "custom_unit", "custom_x_percent", "custom_y_percent", "custom_x_px", "custom_y_px",
)

# 사진마다 따로 저장되는 값. 나머지(배경색·저장 옵션)는 공통 설정이다.
PER_IMAGE_KEYS = (
    ("canvas_mode", "canvas_w", "canvas_h", "fit_mode", "crop_x", "crop_y", "crop_zoom")
    + ADJUSTMENT_KEYS + WATERMARK_KEYS
)

# 새로 올린 사진은 톤·색감·크롭을 항상 "손대지 않은 상태" 로 시작한다.
# (출력 크기·맞춤 방식·워터마크는 지난 실행에서 쓰던 값을 그대로 물려받는다)
NEUTRAL_ON_NEW_IMAGE = ADJUSTMENT_KEYS + ("crop_x", "crop_y", "crop_zoom")

POSITION_LABELS = {
    "top_left": "왼쪽 위",
    "top_right": "오른쪽 위",
    "bottom_left": "왼쪽 아래",
    "bottom_right": "오른쪽 아래",
    "center": "중앙",
    "custom": "사용자 지정 (X / Y 직접 입력)",
}
POSITION_KEYS = list(POSITION_LABELS)

FIT_LABELS = {
    "crop": "Crop to Fill · 프레임 안에서 드래그해 구도 잡기 (권장)",
    "fit": "Fit · 전체가 보이도록 맞추고 남는 곳은 배경색",
    "stretch": "Stretch · 강제로 늘림 (비율 깨짐)",
    "expand": "AI Expand · 모자란 영역을 AI 로 채움 (추후 지원)",
}
FIT_KEYS = list(FIT_LABELS)

MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


# ---------------------------------------------------------------- config 저장/불러오기

def _as_float(value, fallback, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, low), high)


def _as_int(value, fallback, low, high):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return min(max(number, low), high)


def _as_choice(value, choices, fallback):
    return value if value in choices else fallback


def _as_hex_color(value, fallback):
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 7 and text.startswith("#"):
            try:
                int(text[1:], 16)
                return text.upper()
            except ValueError:
                pass
    return fallback


def load_config() -> dict:
    """config.json 을 읽어 이전 설정을 복원한다. 없거나 깨졌으면 항목별로 기본값."""
    raw = {}
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (json.JSONDecodeError, OSError):
            raw = {}

    d = DEFAULTS
    canvas_mode = raw.get("canvas_mode")
    canvas_mode = LEGACY_CANVAS_MODES.get(canvas_mode, canvas_mode)   # 예전 "preset" 값 이어받기

    config = {
        "canvas_mode": _as_choice(canvas_mode, tuple(CANVAS_KEYS), d["canvas_mode"]),
        "canvas_w": _as_int(raw.get("canvas_w"), d["canvas_w"], 16, 10000),
        "canvas_h": _as_int(raw.get("canvas_h"), d["canvas_h"], 16, 10000),

        "fit_mode": _as_choice(raw.get("fit_mode"), tuple(FIT_KEYS), d["fit_mode"]),
        "crop_x": _as_float(raw.get("crop_x"), d["crop_x"], 0.0, 100.0),
        "crop_y": _as_float(raw.get("crop_y"), d["crop_y"], 0.0, 100.0),
        "crop_zoom": _as_float(raw.get("crop_zoom"), d["crop_zoom"], 1.0, MAX_ZOOM),

        "size_mode": _as_choice(raw.get("size_mode"), ("percent", "pixel"), d["size_mode"]),
        "size_percent": _as_float(raw.get("size_percent"), d["size_percent"], 1.0, 100.0),
        "size_px": _as_int(raw.get("size_px"), d["size_px"], 1, 20000),
        "opacity": _as_int(raw.get("opacity"), d["opacity"], 0, 100),
        "position": _as_choice(raw.get("position"), tuple(POSITION_KEYS), d["position"]),
        "margin_mode": _as_choice(raw.get("margin_mode"), ("percent", "pixel"), d["margin_mode"]),
        "margin_x_percent": _as_float(raw.get("margin_x_percent"), d["margin_x_percent"], 0.0, 49.0),
        "margin_y_percent": _as_float(raw.get("margin_y_percent"), d["margin_y_percent"], 0.0, 49.0),
        "margin_x_px": _as_int(raw.get("margin_x_px"), d["margin_x_px"], 0, 20000),
        "margin_y_px": _as_int(raw.get("margin_y_px"), d["margin_y_px"], 0, 20000),
        "custom_unit": _as_choice(raw.get("custom_unit"), ("percent", "pixel"), d["custom_unit"]),
        "custom_x_percent": _as_float(raw.get("custom_x_percent"), d["custom_x_percent"], -100.0, 200.0),
        "custom_y_percent": _as_float(raw.get("custom_y_percent"), d["custom_y_percent"], -100.0, 200.0),
        "custom_x_px": _as_int(raw.get("custom_x_px"), d["custom_x_px"], -20000, 20000),
        "custom_y_px": _as_int(raw.get("custom_y_px"), d["custom_y_px"], -20000, 20000),

        "bg_color": _as_hex_color(raw.get("bg_color"), d["bg_color"]),
        "output_format": _as_choice(raw.get("output_format"), ("jpeg", "keep"), d["output_format"]),
        "name_suffix": str(raw.get("name_suffix", d["name_suffix"]))[:40] or d["name_suffix"],
        "watermark_sync": bool(raw.get("watermark_sync", d["watermark_sync"])),
    }
    for key in ADJUSTMENT_KEYS:
        config[key] = _as_int(raw.get(key), d[key], -100, 100)
    return config


def save_config(config: dict) -> bool:
    try:
        ordered = {key: config[key] for key in DEFAULTS if key in config}
        CONFIG_PATH.write_text(
            json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 1단계: 디코드 + EXIF

def open_image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def normalize_orientation(image: Image.Image) -> Image.Image:
    """EXIF orientation 을 실제 픽셀에 반영하고 orientation 태그는 제거한다."""
    return ImageOps.exif_transpose(image)


def read_source(data: bytes) -> tuple[Image.Image, dict]:
    """업로드 바이트 → (EXIF 보정된 RGBA 이미지, 원본 메타데이터)."""
    with open_image(data) as opened:
        source_format = (opened.format or "PNG").upper()
        oriented = normalize_orientation(opened)
        meta = {
            "source_format": "JPEG" if source_format == "MPO" else source_format,
            "mode": opened.mode,
            "has_alpha": opened.mode in ("RGBA", "LA", "PA") or "transparency" in opened.info,
            "icc_profile": opened.info.get("icc_profile"),
            "size": oriented.size,
        }
    meta["exif"] = oriented.info.get("exif")
    return oriented.convert("RGBA"), meta


# ---------------------------------------------------------------- 2단계: 크롭 / 리사이즈

AI_EXPAND_AVAILABLE = False       # 실제 생성 기능이 붙으면 True 로 바꾼다


def expand_with_ai(source: Image.Image, canvas_w: int, canvas_h: int, config: dict):
    """AI 아웃페인팅 자리(스텁). 지금은 항상 None 을 돌려준다.

    나중에 외부 이미지 생성 API 를 붙일 때 이 함수만 채우면 나머지 파이프라인은 그대로 동작한다.

    계약:
      · 입력  - EXIF 보정이 끝난 RGBA 원본, 목표 캔버스 크기, 현재 설정
      · 반환  - 정확히 (canvas_w, canvas_h) 크기의 RGBA 이미지, 실패하면 None
      · None 을 돌려주면 호출부가 Fit 방식으로 안전하게 되돌아간다.
    """
    return None


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def target_canvas(config: dict, source_size: tuple[int, int]) -> tuple[int, int]:
    mode = config["canvas_mode"]
    if mode == "original":
        return source_size
    if mode == "custom":
        return int(config["canvas_w"]), int(config["canvas_h"])
    return CANVAS_PRESETS.get(mode, CANVAS_PRESETS["preset_3_4"])


def crop_region_size(source_size: tuple[int, int], canvas_size: tuple[int, int], zoom: float
                     ) -> tuple[float, float]:
    """출력 비율을 지키면서 원본 안에 들어가는 크롭 영역의 크기(원본 픽셀 기준)."""
    source_w, source_h = source_size
    ratio = canvas_size[0] / canvas_size[1]
    cover_w = min(float(source_w), float(source_h) * ratio)
    zoom = min(max(float(zoom), 1.0), MAX_ZOOM)
    return cover_w / zoom, (cover_w / ratio) / zoom


def crop_box(source_size: tuple[int, int], canvas_size: tuple[int, int], config: dict
             ) -> tuple[int, int, int, int]:
    """(left, top, width, height). 출력 비율과 같은 모양이라 결과가 찌그러지지 않는다."""
    source_w, source_h = source_size
    region_w, region_h = crop_region_size(source_size, canvas_size, config["crop_zoom"])
    width = min(source_w, max(1, int(round(region_w))))
    height = min(source_h, max(1, int(round(region_h))))
    left = int(round((source_w - width) * float(config["crop_x"]) / 100.0))
    top = int(round((source_h - height) * float(config["crop_y"]) / 100.0))
    left = min(max(left, 0), source_w - width)
    top = min(max(top, 0), source_h - height)
    return left, top, width, height


def crop_box_to_position(box: dict, source_size: tuple[int, int], canvas_size: tuple[int, int]
                         ) -> tuple[float, float, float]:
    """드래그 편집기가 돌려준 상자 → (crop_x %, crop_y %, zoom) 로 되돌린다."""
    source_w, source_h = source_size
    ratio = canvas_size[0] / canvas_size[1]
    cover_w = min(float(source_w), float(source_h) * ratio)
    box_w = max(1.0, float(box.get("width", cover_w)))
    zoom = min(max(cover_w / box_w, 1.0), MAX_ZOOM)

    region_w, region_h = crop_region_size(source_size, canvas_size, zoom)
    span_x, span_y = source_w - region_w, source_h - region_h
    crop_x = 50.0 if span_x <= 0.5 else min(max(float(box.get("left", 0)) / span_x * 100.0, 0.0), 100.0)
    crop_y = 50.0 if span_y <= 0.5 else min(max(float(box.get("top", 0)) / span_y * 100.0, 0.0), 100.0)
    return crop_x, crop_y, zoom


def crop_default_coords(source_size: tuple[int, int], canvas_size: tuple[int, int], config: dict
                        ) -> tuple[int, int, int, int]:
    """드래그 편집기에 넘길 초기 상자 (xl, xr, yt, yb)."""
    left, top, width, height = crop_box(source_size, canvas_size, config)
    return left, left + width, top, top + height


def fit_to_canvas(source: Image.Image, canvas_w: int, canvas_h: int, config: dict) -> Image.Image:
    """정확히 canvas_w × canvas_h 픽셀의 RGBA 이미지를 돌려준다."""
    source_w, source_h = source.size
    background = hex_to_rgb(config["bg_color"])
    mode = config["fit_mode"]

    if mode == "expand":
        expanded = expand_with_ai(source, canvas_w, canvas_h, config)
        if expanded is not None and expanded.size == (canvas_w, canvas_h):
            return expanded.convert("RGBA")
        mode = "fit"          # 아직 지원 전이므로 안전하게 Fit 으로 되돌아간다

    if mode == "stretch":
        if (source_w, source_h) == (canvas_w, canvas_h):
            return source
        return source.resize((canvas_w, canvas_h), Image.LANCZOS)

    if mode == "fit":
        scale = min(canvas_w / source_w, canvas_h / source_h)
        new_w = max(1, min(canvas_w, round(source_w * scale)))
        new_h = max(1, min(canvas_h, round(source_h * scale)))
        scaled = source if (new_w, new_h) == (source_w, source_h) else source.resize((new_w, new_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (canvas_w, canvas_h), background + (255,))
        canvas.paste(scaled, ((canvas_w - new_w) // 2, (canvas_h - new_h) // 2), scaled)
        return canvas

    # Crop to Fill: 출력 비율과 같은 모양의 영역을 잘라낸 뒤 캔버스 크기로 맞춘다.
    left, top, width, height = crop_box((source_w, source_h), (canvas_w, canvas_h), config)
    cropped = source.crop((left, top, left + width, top + height))
    if cropped.size == (canvas_w, canvas_h):
        return cropped
    return cropped.resize((canvas_w, canvas_h), Image.LANCZOS)


# ---------------------------------------------------------------- 3·4단계: 톤 / 색감 보정

def has_adjustments(config: dict) -> bool:
    return any(int(config[key]) != 0 for key in ADJUSTMENT_KEYS)


def _smoothstep(edge0: float, edge1: float, values: np.ndarray) -> np.ndarray:
    t = np.clip((values - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0:1] * 0.2126 + rgb[..., 1:2] * 0.7152 + rgb[..., 2:3] * 0.0722


def apply_sharpness(image: Image.Image, amount: int) -> Image.Image:
    """양수는 언샵 마스크로 선명하게, 음수는 가우시안 블러로 부드럽게. 0 이면 그대로 통과."""
    if not amount:
        return image
    rgb = image.convert("RGB")
    if amount > 0:
        filtered = rgb.filter(ImageFilter.UnsharpMask(radius=2.0, percent=int(amount * 1.6), threshold=3))
    else:
        filtered = rgb.filter(ImageFilter.GaussianBlur(radius=abs(amount) / 100.0 * 1.8))
    result = filtered.convert("RGBA")
    result.putalpha(image.getchannel("A"))
    return result


def adjust_image(image: Image.Image, config: dict) -> Image.Image:
    """톤·색감 보정. 모든 값이 0이면 원본 객체를 그대로 돌려준다(무손실 통과)."""
    if not has_adjustments(config):
        return image

    array = np.asarray(image, dtype=np.float32) / 255.0
    rgb = array[..., :3].copy()
    alpha = array[..., 3:]

    # 노출 - 선형 광량 공간에서 스톱 단위로 곱한다 (±1.5 스톱)
    exposure = float(config["exposure"]) / 100.0
    if exposure:
        linear = np.power(np.clip(rgb, 0.0, 1.0), 2.2)
        linear *= float(2.0 ** (exposure * 1.5))
        rgb = np.power(np.clip(linear, 0.0, None), 1.0 / 2.2)

    # 밝기 - 감마로 중간톤만 올려 하이라이트가 타지 않게 한다
    brightness = float(config["brightness"]) / 100.0
    if brightness:
        rgb = np.power(np.clip(rgb, 0.0, 1.0), 1.0 / (1.0 + 0.7 * brightness))

    # 대비 - 중간 회색(0.5) 기준
    contrast = float(config["contrast"]) / 100.0
    if contrast:
        rgb = (rgb - 0.5) * (1.0 + 0.65 * contrast) + 0.5

    rgb = np.clip(rgb, 0.0, 1.0)

    # 하이라이트 - 밝은 영역에만 부드럽게 (smoothstep 마스크라 경계가 생기지 않는다)
    highlights = float(config["highlights"]) / 100.0
    if highlights:
        mask = _smoothstep(0.45, 1.0, _luma(rgb))
        if highlights > 0:
            rgb = rgb + highlights * 0.45 * mask * (1.0 - rgb)
        else:
            rgb = rgb * (1.0 + highlights * 0.60 * mask)

    # 그림자 - 어두운 영역에만
    shadows = float(config["shadows"]) / 100.0
    if shadows:
        mask = 1.0 - _smoothstep(0.0, 0.55, _luma(rgb))
        if shadows > 0:
            rgb = rgb + shadows * 0.50 * mask * (1.0 - rgb)
        else:
            rgb = rgb * (1.0 + shadows * 0.70 * mask)

    rgb = np.clip(rgb, 0.0, 1.0)

    # 채도
    saturation = float(config["saturation"]) / 100.0
    if saturation:
        gray = _luma(rgb)
        rgb = gray + (rgb - gray) * (1.0 + saturation)
        rgb = np.clip(rgb, 0.0, 1.0)

    # 색온도 / Tint - 채널 게인을 준 뒤 밝기를 되돌려 톤이 밀리지 않게 한다
    temperature = float(config["temperature"]) / 100.0
    tint = float(config["tint"]) / 100.0
    if temperature or tint:
        before = _luma(rgb)
        gain = np.array([
            1.0 + 0.18 * temperature + 0.06 * tint,
            1.0 - 0.10 * tint,
            1.0 - 0.18 * temperature + 0.06 * tint,
        ], dtype=np.float32)
        rgb = np.clip(rgb * gain, 0.0, 1.0)
        after = _luma(rgb)
        rgb = rgb * (before / np.maximum(after, 1e-4))

    rgb = np.clip(rgb, 0.0, 1.0)
    merged = np.concatenate([rgb, alpha], axis=-1)
    adjusted = Image.fromarray((merged * 255.0 + 0.5).astype(np.uint8), mode="RGBA")

    # Sharpness - 색 보정이 모두 끝난 뒤 마지막에
    return apply_sharpness(adjusted, int(config["sharpness"]))


# ---------------------------------------------------------------- 5단계: 워터마크 합성

def watermark_target_size(base_w: int, wm_w: int, wm_h: int, config: dict) -> tuple[int, int]:
    if config["size_mode"] == "percent":
        target_w = base_w * config["size_percent"] / 100.0
    else:
        target_w = float(config["size_px"])
    target_w = max(1, int(round(target_w)))
    target_h = max(1, int(round(wm_h * (target_w / wm_w))))
    return target_w, target_h


def watermark_position(base_w: int, base_h: int, wm_w: int, wm_h: int, config: dict) -> tuple[int, int]:
    """워터마크 왼쪽 위 모서리가 놓일 좌표. 기준은 항상 최종 출력 캔버스."""
    position = config["position"]

    if position == "custom":
        if config["custom_unit"] == "percent":
            x = base_w * config["custom_x_percent"] / 100.0
            y = base_h * config["custom_y_percent"] / 100.0
        else:
            x = float(config["custom_x_px"])
            y = float(config["custom_y_px"])
        return int(round(x)), int(round(y))

    if config["margin_mode"] == "percent":
        margin_x = base_w * config["margin_x_percent"] / 100.0
        margin_y = base_h * config["margin_y_percent"] / 100.0
    else:
        margin_x = float(config["margin_x_px"])
        margin_y = float(config["margin_y_px"])
    margin_x, margin_y = int(round(margin_x)), int(round(margin_y))

    left, top = margin_x, margin_y
    right, bottom = base_w - wm_w - margin_x, base_h - wm_h - margin_y

    return {
        "top_left": (left, top),
        "top_right": (right, top),
        "bottom_left": (left, bottom),
        "bottom_right": (right, bottom),
        "center": ((base_w - wm_w) // 2, (base_h - wm_h) // 2),
    }[position]


def apply_opacity(watermark: Image.Image, opacity: int) -> Image.Image:
    if opacity >= 100:
        return watermark
    factor = max(0, min(100, opacity)) / 100.0
    alpha = watermark.getchannel("A").point(lambda value: int(round(value * factor)))
    faded = watermark.copy()
    faded.putalpha(alpha)
    return faded


def compose(base: Image.Image, watermark: Image.Image, config: dict) -> Image.Image:
    """보정이 모두 끝난 캔버스 위에 워터마크만 얹는다. 캔버스 크기는 변하지 않는다."""
    base_w, base_h = base.size
    wm_w, wm_h = watermark.size

    target_w, target_h = watermark_target_size(base_w, wm_w, wm_h, config)
    resized = watermark.resize((target_w, target_h), Image.LANCZOS)
    resized = apply_opacity(resized, config["opacity"])

    x, y = watermark_position(base_w, base_h, target_w, target_h, config)

    layer = Image.new("RGBA", (base_w, base_h), (0, 0, 0, 0))
    layer.paste(resized, (x, y))          # 캔버스 밖으로 나가는 부분은 자동으로 잘림
    return Image.alpha_composite(base, layer)


# ---------------------------------------------------------------- 6단계: 저장

def flatten(image: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGBA", image.size, background + (255,))
    return Image.alpha_composite(canvas, image).convert("RGB")


def encode_image(result: Image.Image, meta: dict, config: dict, resized: bool) -> tuple[bytes, str]:
    """색상 프로파일을 가능한 한 유지한 채 저장한다. (bytes, 저장 포맷) 반환."""
    if config["output_format"] == "jpeg":
        out_format = "JPEG"
    else:
        out_format = meta["source_format"] if meta["source_format"] in MIME_BY_FORMAT else "PNG"

    save_kwargs: dict = {}
    icc_profile = meta.get("icc_profile")
    # CMYK/LAB 등은 RGB 로 바뀌므로 원본 프로파일을 그대로 붙이면 색이 틀어진다.
    if icc_profile and meta["mode"] not in ("CMYK", "LAB", "YCbCr"):
        save_kwargs["icc_profile"] = icc_profile

    # 크기가 바뀐 이미지에 원본 EXIF(썸네일·치수 포함)를 붙이면 정보가 어긋나므로 뺀다.
    exif = None if resized else meta.get("exif")
    background = hex_to_rgb(config["bg_color"])

    if out_format == "JPEG":
        image = flatten(result, background)
        save_kwargs.update(
            quality=JPEG_QUALITY,
            subsampling="keep" if (meta["source_format"] == "JPEG" and not resized) else 0,
        )
        if exif:
            save_kwargs["exif"] = exif
    elif out_format == "WEBP":
        image = result if meta["has_alpha"] else flatten(result, background)
        save_kwargs.update(quality=WEBP_QUALITY, method=6)
        if exif:
            save_kwargs["exif"] = exif
    else:  # PNG
        image = result if meta["has_alpha"] else flatten(result, background)

    buffer = io.BytesIO()
    try:
        image.save(buffer, format=out_format, **save_kwargs)
    except (OSError, ValueError):
        # 원본 의존 옵션이 거부되면 안전한 기본값으로 재시도해 파일은 반드시 만들어 낸다.
        buffer = io.BytesIO()
        save_kwargs.pop("subsampling", None)
        save_kwargs.pop("exif", None)
        image.save(buffer, format=out_format, **save_kwargs)
    return buffer.getvalue(), out_format


def output_name(filename: str, out_format: str, suffix: str) -> str:
    path = Path(filename)
    extension = ".jpg" if out_format == "JPEG" else (path.suffix or ".png")
    return f"{path.stem}{suffix}{extension}"


# ---------------------------------------------------------------- 파이프라인

def _prepare_canvas(data: bytes, config: dict) -> tuple[Image.Image, dict, tuple[int, int]]:
    source, meta = read_source(data)                              # 1. EXIF
    canvas_w, canvas_h = target_canvas(config, source.size)
    canvas = fit_to_canvas(source, canvas_w, canvas_h, config)    # 2. 크롭/리사이즈
    return canvas, meta, source.size


@st.cache_data(show_spinner=False, max_entries=8)
def _prepare_canvas_cached(data: bytes, canvas_key: tuple):
    config = dict(DEFAULTS)
    (config["canvas_mode"], config["canvas_w"], config["canvas_h"], config["fit_mode"],
     config["crop_x"], config["crop_y"], config["crop_zoom"], config["bg_color"]) = canvas_key
    canvas, meta, source_size = _prepare_canvas(data, config)
    return canvas.tobytes(), canvas.size, meta, source_size


def prepare_canvas(data: bytes, config: dict) -> tuple[Image.Image, dict, tuple[int, int]]:
    """1~2단계. 슬라이더를 움직일 때마다 다시 디코딩하지 않도록 캐시한다."""
    canvas_key = (config["canvas_mode"], int(config["canvas_w"]), int(config["canvas_h"]),
                  config["fit_mode"], float(config["crop_x"]), float(config["crop_y"]),
                  float(config["crop_zoom"]), config["bg_color"])
    if config["canvas_mode"] != "original" and \
            int(config["canvas_w"]) * int(config["canvas_h"]) <= CACHE_PIXEL_LIMIT:
        raw, size, meta, source_size = _prepare_canvas_cached(data, canvas_key)
        return Image.frombytes("RGBA", size, raw), meta, source_size
    return _prepare_canvas(data, config)


def render_result(data: bytes, watermark: Image.Image | None, config: dict):
    """1~5단계. 저장 직전 상태의 이미지와 메타데이터를 돌려준다."""
    canvas, meta, source_size = prepare_canvas(data, config)      # 1 · 2
    edited = adjust_image(canvas, config)                         # 3 · 4
    result = compose(edited, watermark, config) if watermark is not None else edited   # 5
    return result, meta, source_size, canvas.size != source_size


def process_one(data: bytes, filename: str, watermark: Image.Image | None, config: dict) -> dict:
    """한 장을 정해진 순서대로 처리한다."""
    result, meta, source_size, resized = render_result(data, watermark, config)
    payload, out_format = encode_image(result, meta, config, resized=resized)          # 6

    return {
        "name": output_name(filename, out_format, config["name_suffix"]),
        "bytes": payload,
        "mime": MIME_BY_FORMAT.get(out_format, "application/octet-stream"),
        "source_size": source_size,
        "result_size": result.size,
        "image": result,
    }


def make_zip(results: list[dict]) -> bytes:
    buffer = io.BytesIO()
    used: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in results:
            name = item["name"]
            if name in used:                                      # 같은 이름이 겹치면 번호를 붙인다
                used[name] += 1
                path = Path(name)
                name = f"{path.stem}_{used[name]}{path.suffix}"
            else:
                used[name] = 0
            archive.writestr(name, item["bytes"])
    return buffer.getvalue()


def to_preview(image: Image.Image, max_side: int = PREVIEW_MAX_SIDE) -> Image.Image:
    """화면 표시용 축소본 (저장되는 파일과는 무관, 비율은 그대로)."""
    preview = image.copy()
    preview.thumbnail((max_side, max_side), Image.LANCZOS)
    return preview


# ---------------------------------------------------------------- 화면
#
# 레이아웃: 위쪽에 업로드, 그 아래 [왼쪽 60 = 미리보기(sticky) | 오른쪽 40 = 편집 설정],
#          맨 아래에 가로 전체 폭으로 일괄 처리 / 다운로드.
#
# 설정 저장 방식
#   · 이미지별 설정 - st.session_state.image_settings[이미지키] (PER_IMAGE_KEYS)
#   · 공통 설정     - st.session_state.settings (배경색 · 저장 옵션 · 워터마크 동일 적용)
#   위젯에는 value= / index= 로 값을 넣고, 위젯 키는 "w_" 접두사(이미지별은 "__이미지키" 접미사)로
#   분리한다. 이미지마다 위젯 키가 달라지므로 사진 사이에 값이 섞이지 않는다.

st.set_page_config(page_title="WATERMARK TOOL", page_icon="💧", layout="wide")

# 미리보기를 화면에 붙여 두고, 이미지가 절대 찌그러지지 않게 하는 CSS.
# Streamlit 이 <img> 에 인라인으로 width:100% 를 넣기 때문에 !important 로 되돌려야
# 세로가 눌리지 않는다. 선택자는 st.container(key=...) 가 만드는 공식 클래스만 쓴다.
st.markdown(
    """
    <style>
      [data-testid="stColumn"]:has(.st-key-preview_pane) {
          position: sticky;
          top: 3.5rem;
          align-self: flex-start;
      }
      .st-key-preview_pane img {
          width: auto !important;
          height: auto !important;
          max-width: 100% !important;
          max-height: 68vh !important;
          object-fit: contain;
          margin: 0 auto;
          display: block;
          border-radius: 6px;
      }
      .st-key-preview_pane [data-testid="stImageContainer"],
      .st-key-preview_pane [data-testid="stImage"] { text-align: center; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("WATERMARK TOOL")
st.caption("오른쪽에서 설정을 바꾸면 왼쪽 미리보기가 바로 갱신됩니다. "
           "처리 순서는 EXIF → 크기 맞춤 → 톤 보정 → 색감 보정 → 워터마크 → 저장이며, 워터마크는 항상 마지막입니다.")

if "settings" not in st.session_state:
    st.session_state.settings = load_config()
    st.session_state.last_saved = dict(st.session_state.settings)
    st.session_state.image_settings = {}
    st.session_state.crop_epoch = {}
    # 이번 실행에서 새로 올라오는 이미지에 적용할 기본값. 앱을 켤 때 한 번만 정해 두어,
    # 한 사진을 보정해도 나중에 추가되는 사진이 그 값에 끌려가지 않게 한다.
    st.session_state.image_defaults = {
        name: (DEFAULTS[name] if name in NEUTRAL_ON_NEW_IMAGE else st.session_state.settings[name])
        for name in PER_IMAGE_KEYS
    }
settings = st.session_state.settings


def image_key(file) -> str:
    """업로드된 파일을 구분하는 열쇠. 같은 파일을 다시 올려도 설정이 이어진다."""
    return f"{file.name}|{file.size}"


def widget_key(name: str, key: str) -> str:
    return f"w_{name}__{key}"


def ensure_image_settings(key: str) -> dict:
    """처음 보는 이미지에는 앱을 켤 때 정해진 기본값을 복사해 준다."""
    store = st.session_state.image_settings
    if key not in store:
        store[key] = dict(st.session_state.image_defaults)
    return store[key]


def reset_adjustments() -> None:
    """현재 이미지의 톤·색감 값만 0으로."""
    key = st.session_state.get("current_image_key")
    if not key:
        return
    for name in ADJUSTMENT_KEYS:
        st.session_state.image_settings[key][name] = 0
        st.session_state[widget_key(name, key)] = 0


def apply_adjustments_to_all() -> None:
    """현재 이미지의 톤·색감·Sharpness 를 다른 모든 이미지에 복사한다. 크롭 위치는 제외."""
    key = st.session_state.get("current_image_key")
    if not key:
        return
    source = st.session_state.image_settings[key]
    copied = 0
    for other, values in st.session_state.image_settings.items():
        if other == key:
            continue
        for name in ADJUSTMENT_KEYS:
            values[name] = source[name]
            st.session_state[widget_key(name, other)] = source[name]
        copied += 1
    st.session_state.apply_all_notice = copied


def reset_crop() -> None:
    """크롭 위치를 가운데·기본 배율로 되돌리고 드래그 편집기를 다시 그린다."""
    key = st.session_state.get("current_image_key")
    if not key:
        return
    current_settings = st.session_state.image_settings[key]
    current_settings["crop_x"], current_settings["crop_y"], current_settings["crop_zoom"] = 50.0, 50.0, 1.0
    st.session_state.crop_epoch[key] = st.session_state.crop_epoch.get(key, 0) + 1


@st.cache_data(show_spinner=False, max_entries=32)
def peek_size(data: bytes) -> tuple[int, int]:
    """EXIF 회전까지 반영한 원본 크기를 전체 디코딩 없이 알아낸다."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            orientation = image.getexif().get(274, 1)
    except OSError:
        return (0, 0)
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    return (width, height)


@st.cache_data(show_spinner=False, max_entries=12)
def source_preview(data: bytes, max_side: int) -> tuple[bytes, tuple[int, int]]:
    """EXIF 보정된 원본의 축소본 (비율 그대로)."""
    source, _meta = read_source(data)
    small = to_preview(source, max_side)
    return small.tobytes(), small.size


def load_preview(data: bytes, max_side: int) -> Image.Image:
    raw, size = source_preview(data, max_side)
    return Image.frombytes("RGBA", size, raw)


def section(number: int, title: str) -> None:
    st.markdown(f"##### {number}. {title}")


def effective_settings(key: str) -> dict:
    """공통 설정 + 그 이미지의 개별 설정을 합친, 처리 함수에 넘길 설정."""
    return {**st.session_state.settings, **st.session_state.image_settings.get(key, {})}


# ---------------------------------------------------------------- 이미지 업로드 (가로 전체)
upload_col, list_col = st.columns([45, 55], gap="large")
with upload_col:
    uploaded_images = st.file_uploader(
        "이미지 업로드 · JPG / JPEG / PNG / WEBP · 여러 장 가능",
        type=SUPPORTED_TYPES,
        accept_multiple_files=True,
        key="w_image_files",
    )

# 올라온 모든 이미지의 개별 설정 칸을 미리 만들어 둔다 (전체 적용 버튼이 전부를 다룰 수 있도록).
for file in uploaded_images or []:
    ensure_image_settings(image_key(file))

with list_col:
    if uploaded_images:
        rows = []
        for file in uploaded_images:
            width, height = peek_size(file.getvalue())
            per_image = st.session_state.image_settings[image_key(file)]
            touched = sum(1 for name in ADJUSTMENT_KEYS if per_image[name] != 0)
            out_w, out_h = target_canvas(per_image, (width, height))
            rows.append({
                "파일 이름": file.name,
                "원본 크기": f"{width} × {height} px" if width else "읽을 수 없음",
                "출력": f"{out_w} × {out_h}",
                "맞춤": FIT_LABELS[per_image["fit_mode"]].split(" ·")[0],
                "보정": f"{touched}개" if touched else "-",
            })
        st.dataframe(rows, hide_index=True, width="stretch", height=min(38 + 35 * len(rows), 180))
    else:
        st.info("이미지를 올리면 목록과 원본 크기가 여기에 표시됩니다.")

st.divider()

# ---------------------------------------------------------------- 2분할: 미리보기 | 편집 설정
preview_col, control_col = st.columns([60, 40], gap="large")

# ================================================================= 오른쪽: 편집 설정
with control_col:
    st.markdown("### 편집 설정")

    # --- 1. 편집할 이미지 선택 -------------------------------------------------
    section(1, "편집할 이미지")
    if uploaded_images:
        names = [file.name for file in uploaded_images]
        chosen = st.selectbox("편집할 이미지", range(len(names)),
                              format_func=lambda i: f"{i + 1}. {names[i]}", key="w_preview_index",
                              label_visibility="collapsed")
        selected_file = uploaded_images[min(chosen, len(names) - 1)]
        current_key = image_key(selected_file)
        st.session_state.current_image_key = current_key
        current = ensure_image_settings(current_key)
        st.caption("아래 2~6번은 **이 사진에만** 적용되는 개별 설정입니다.")
    else:
        selected_file, current_key, current = None, None, None
        st.session_state.current_image_key = None
        st.caption("이미지를 먼저 올려 주세요.")

    # --- 2. 출력 크기 / 맞추는 방식 ---------------------------------------------
    section(2, "출력 크기 · 맞추는 방식")
    if current is None:
        st.caption("이미지를 올리면 출력 크기를 고를 수 있습니다.")
        canvas_w, canvas_h = CANVAS_PRESETS["preset_3_4"]
    else:
        current["canvas_mode"] = st.selectbox(
            "출력 크기",
            CANVAS_KEYS,
            index=CANVAS_KEYS.index(current["canvas_mode"]),
            format_func=lambda key: CANVAS_LABELS[key],
            key=widget_key("canvas_mode", current_key),
        )
        if current["canvas_mode"] == "custom":
            size_col1, size_col2 = st.columns(2)
            with size_col1:
                current["canvas_w"] = st.number_input(
                    "가로 (px)", 16, 10000, value=int(current["canvas_w"]), step=10,
                    key=widget_key("canvas_w", current_key))
            with size_col2:
                current["canvas_h"] = st.number_input(
                    "세로 (px)", 16, 10000, value=int(current["canvas_h"]), step=10,
                    key=widget_key("canvas_h", current_key))

        if current["canvas_mode"] == "original":
            st.caption("원본 크기를 그대로 유지합니다. 크롭·리사이즈 없이 보정과 워터마크만 적용됩니다.")
        else:
            current["fit_mode"] = st.radio(
                "맞추는 방식",
                FIT_KEYS,
                index=FIT_KEYS.index(current["fit_mode"]),
                format_func=lambda key: FIT_LABELS[key],
                key=widget_key("fit_mode", current_key),
            )
            if current["fit_mode"] == "expand":
                st.info("AI Expand 는 추후 지원 예정입니다. 지금은 **Fit** 방식으로 미리보기·저장됩니다.", icon="🧪")
            if current["fit_mode"] in ("fit", "expand"):
                settings["bg_color"] = st.color_picker(
                    "남는 영역 배경색 (공통)", value=settings["bg_color"], key="w_bg_color")
            elif current["fit_mode"] == "stretch":
                st.warning("Stretch 는 원본 비율을 무시하고 늘립니다.", icon="⚠️")
        canvas_w, canvas_h = target_canvas(current, peek_size(selected_file.getvalue()))

    with st.expander("저장 옵션 (파일 형식 · 이름 · 공통)"):
        settings["output_format"] = st.radio(
            "저장 형식",
            ["jpeg", "keep"],
            index=["jpeg", "keep"].index(settings["output_format"]),
            format_func=lambda key: "JPG (quality 95)" if key == "jpeg" else "원본 형식 유지",
            key="w_output_format",
            horizontal=True,
        )
        suffix = st.text_input("파일 이름 뒤에 붙일 말", value=settings["name_suffix"],
                               max_chars=40, key="w_name_suffix")
        settings["name_suffix"] = suffix.replace("/", "").replace("\\", "").strip() or "_edited"
        st.caption(f"예) `사진.jpg` → `사진{settings['name_suffix']}.jpg`")

    # --- 3. 크롭 위치 -----------------------------------------------------------
    section(3, "크롭 위치")
    if current is not None and current["canvas_mode"] != "original" and current["fit_mode"] == "crop":
        st.caption("왼쪽 미리보기에서 **크롭 위치 조정** 탭을 열고, 프레임을 마우스로 끌어 구도를 잡으세요. "
                   "모서리를 끌면 확대/축소됩니다.")
        st.button("가운데로 되돌리기", on_click=reset_crop, width="stretch")
    else:
        st.caption("Crop to Fill 방식일 때만 사용합니다.")

    # --- 4·5. 톤 / 색감 보정 ----------------------------------------------------
    def adjustment_slider(label: str, name: str, help_text: str | None = None) -> None:
        if current is None:
            st.slider(label, -100, 100, value=0, disabled=True, key="w_disabled_" + name)
            return
        current[name] = st.slider(label, -100, 100, value=int(current[name]),
                                  key=widget_key(name, current_key), help=help_text)

    section(4, "톤 보정")
    adjustment_slider("Brightness 밝기", "brightness")
    adjustment_slider("Exposure 노출", "exposure")
    adjustment_slider("Contrast 대비", "contrast")
    adjustment_slider("Highlights 하이라이트", "highlights", "하늘처럼 밝은 부분만 조절합니다.")
    adjustment_slider("Shadows 그림자", "shadows", "얼굴처럼 어두운 부분만 조절합니다.")

    section(5, "색감 보정")
    adjustment_slider("Saturation 채도", "saturation")
    adjustment_slider("Temperature 색온도", "temperature", "음수 = 차갑게(파랑), 양수 = 따뜻하게(주황)")
    adjustment_slider("Tint", "tint", "음수 = Green, 양수 = Magenta")
    adjustment_slider("Sharpness 선명도", "sharpness", "음수 = 부드럽게, 양수 = 또렷하게")

    # --- 6. 워터마크 설정 -------------------------------------------------------
    section(6, "워터마크 설정")

    uploaded_watermark = st.file_uploader(
        "워터마크 PNG (투명 배경 · 모든 사진 공통)", type=["png"], key="w_watermark_file"
    )

    watermark_image = None
    if uploaded_watermark is not None:
        try:
            watermark_image = open_image(uploaded_watermark.getvalue()).convert("RGBA")
            st.success(f"업로드한 워터마크 · {watermark_image.width} × {watermark_image.height} px", icon="✅")
        except OSError:
            st.error("워터마크 PNG 파일을 읽을 수 없습니다. 다른 파일을 선택해 주세요.")
    elif DEFAULT_WATERMARK_PATH.exists():
        try:
            watermark_image = Image.open(DEFAULT_WATERMARK_PATH).convert("RGBA")
            st.caption(f"폴더의 기본 파일 `watermark.png` 사용 중 · "
                       f"{watermark_image.width} × {watermark_image.height} px")
        except OSError:
            st.error("`watermark.png` 파일을 읽을 수 없습니다. 위에서 다른 PNG 를 올려 주세요.")
    else:
        st.warning("워터마크 PNG 가 없습니다. 이대로 진행하면 보정만 적용됩니다.")

    settings["watermark_sync"] = st.toggle(
        "워터마크 설정을 모든 사진에 동일하게 적용",
        value=bool(settings["watermark_sync"]), key="w_watermark_sync",
        help="켜 두면 결과물마다 워터마크가 같은 자리·같은 크기로 들어갑니다. "
             "끄면 사진마다 다르게 둘 수 있습니다.")


    def watermark_widget(name: str, render):
        """워터마크 값도 이미지별로 저장한다 (동일 적용이 켜져 있으면 뒤에서 전부에 복사)."""
        if current is None:
            return
        current[name] = render(widget_key(name, current_key), current[name])

    if current is not None:
        watermark_widget("position", lambda key, value: st.selectbox(
            "위치", POSITION_KEYS, index=POSITION_KEYS.index(value),
            format_func=lambda k: POSITION_LABELS[k], key=key))

        watermark_widget("size_mode", lambda key, value: st.radio(
            "크기 기준", ["percent", "pixel"], index=["percent", "pixel"].index(value),
            format_func=lambda k: "출력 너비 대비 %" if k == "percent" else "픽셀(px)",
            key=key, horizontal=True))

        if current["size_mode"] == "percent":
            watermark_widget("size_percent", lambda key, value: st.slider(
                "크기 (출력 너비의 %)", 1.0, 100.0, value=float(value), step=0.5, key=key))
        else:
            watermark_widget("size_px", lambda key, value: st.number_input(
                "크기 (px)", 1, 20000, value=int(value), step=10, key=key))

        watermark_widget("opacity", lambda key, value: st.slider(
            "투명도 (%)", 0, 100, value=int(value), key=key,
            help="100 = 원본 PNG 그대로, 0 = 완전히 투명"))

        if current["position"] == "custom":
            watermark_widget("custom_unit", lambda key, value: st.radio(
                "X / Y 입력 방식", ["percent", "pixel"], index=["percent", "pixel"].index(value),
                format_func=lambda k: "백분율(%)" if k == "percent" else "픽셀(px)",
                key=key, horizontal=True))
            xy_col1, xy_col2 = st.columns(2)
            if current["custom_unit"] == "percent":
                with xy_col1:
                    watermark_widget("custom_x_percent", lambda key, value: st.number_input(
                        "X (출력 너비의 %)", -100.0, 200.0, value=float(value), step=0.5, key=key))
                with xy_col2:
                    watermark_widget("custom_y_percent", lambda key, value: st.number_input(
                        "Y (출력 높이의 %)", -100.0, 200.0, value=float(value), step=0.5, key=key))
            else:
                with xy_col1:
                    watermark_widget("custom_x_px", lambda key, value: st.number_input(
                        "X (px)", -20000, 20000, value=int(value), step=10, key=key))
                with xy_col2:
                    watermark_widget("custom_y_px", lambda key, value: st.number_input(
                        "Y (px)", -20000, 20000, value=int(value), step=10, key=key))
            st.caption("X / Y 는 워터마크의 **왼쪽 위 모서리** 좌표입니다.")
        else:
            watermark_widget("margin_mode", lambda key, value: st.radio(
                "여백 입력 방식", ["percent", "pixel"], index=["percent", "pixel"].index(value),
                format_func=lambda k: "백분율(%)" if k == "percent" else "픽셀(px)",
                key=key, horizontal=True))
            margin_col1, margin_col2 = st.columns(2)
            if current["margin_mode"] == "percent":
                with margin_col1:
                    watermark_widget("margin_x_percent", lambda key, value: st.number_input(
                        "Margin X (출력 너비의 %)", 0.0, 49.0, value=float(value), step=0.1, key=key))
                with margin_col2:
                    watermark_widget("margin_y_percent", lambda key, value: st.number_input(
                        "Margin Y (출력 높이의 %)", 0.0, 49.0, value=float(value), step=0.1, key=key))
            else:
                with margin_col1:
                    watermark_widget("margin_x_px", lambda key, value: st.number_input(
                        "Margin X (px)", 0, 20000, value=int(value), step=5, key=key))
                with margin_col2:
                    watermark_widget("margin_y_px", lambda key, value: st.number_input(
                        "Margin Y (px)", 0, 20000, value=int(value), step=5, key=key))
            if current["position"] == "center":
                st.caption("중앙 정렬에서는 여백이 사용되지 않습니다.")

        st.caption("워터마크 크기와 위치는 **최종 출력 캔버스** 기준이라, 원본 비율이 달라도 결과에서는 같은 자리에 같은 크기로 들어갑니다.")

    # --- 7. 보정 초기화 ---------------------------------------------------------
    section(7, "보정 초기화")
    st.button("이 사진의 톤 · 색감 값 모두 0으로", on_click=reset_adjustments,
              disabled=current is None, width="stretch")

    # --- 8. 전체 적용 -----------------------------------------------------------
    section(8, "전체 이미지에 적용")
    st.button("현재 이미지 보정값을 전체 이미지에 적용",
              on_click=apply_adjustments_to_all,
              disabled=not uploaded_images or len(uploaded_images) < 2,
              width="stretch",
              help="톤 · 색감 · Sharpness 만 복사합니다. 크롭 위치는 사진마다 구도가 다르므로 그대로 둡니다.")
    copied = st.session_state.pop("apply_all_notice", None)
    if copied:
        st.success(f"다른 이미지 {copied}장에 톤 · 색감 · Sharpness 를 복사했습니다.", icon="📋")

# ---------------------------------------------------------------- 워터마크 동일 적용
if current is not None and settings["watermark_sync"]:
    for other, values in st.session_state.image_settings.items():
        if other != current_key:
            for name in WATERMARK_KEYS:
                values[name] = current[name]

# ================================================================= 왼쪽: 미리보기 (sticky)
with preview_col:
    with st.container(key="preview_pane"):
        st.markdown("### 미리보기")
        if selected_file is None:
            st.info("이미지를 올리면 여기에서 결과를 크게 확인할 수 있습니다.")
        else:
            crop_enabled = (current["canvas_mode"] != "original" and current["fit_mode"] == "crop")
            view_options = ["결과 미리보기", "크롭 위치 조정", "원본"]
            head_left, head_right = st.columns([38, 62], gap="small")
            with head_left:
                st.markdown(f"**{selected_file.name}**")
            with head_right:
                view = st.radio("보기", view_options, index=0, horizontal=True,
                                key="w_preview_mode", label_visibility="collapsed")

            data = selected_file.getvalue()

            if view == "크롭 위치 조정":
                if not crop_enabled:
                    st.info("크롭 위치는 **Crop to Fill** 방식일 때만 조정할 수 있습니다.", icon="ℹ️")
                elif not CROPPER_AVAILABLE:
                    st.warning("드래그 편집기(streamlit-cropper)가 설치되어 있지 않습니다. "
                               "`pip install -r requirements.txt` 를 실행해 주세요.", icon="⚠️")
                    slider_col1, slider_col2 = st.columns(2)
                    with slider_col1:
                        current["crop_x"] = st.slider("X · Left ↔ Right", 0.0, 100.0,
                                                      value=float(current["crop_x"]), step=1.0,
                                                      key=widget_key("crop_x_fallback", current_key))
                    with slider_col2:
                        current["crop_y"] = st.slider("Y · Up ↔ Down", 0.0, 100.0,
                                                      value=float(current["crop_y"]), step=1.0,
                                                      key=widget_key("crop_y_fallback", current_key))
                else:
                    editor_source = adjust_image(load_preview(data, CROP_EDITOR_MAX_SIDE), current)
                    editor_rgb = editor_source.convert("RGB")
                    epoch = st.session_state.crop_epoch.get(current_key, 0)
                    st.caption("빨간 프레임을 끌어 구도를 잡으세요. 프레임 비율은 출력 비율로 고정되어 있습니다.")
                    box = st_cropper(
                        editor_rgb,
                        realtime_update=True,
                        box_color="#FF3B30",
                        aspect_ratio=(canvas_w, canvas_h),
                        return_type="box",
                        should_resize_image=False,
                        default_coords=crop_default_coords(editor_rgb.size, (canvas_w, canvas_h), current),
                        key=f"cropper__{current_key}__{canvas_w}x{canvas_h}__{epoch}",
                    )
                    if isinstance(box, dict):
                        crop_x, crop_y, crop_zoom = crop_box_to_position(
                            box, editor_rgb.size, (canvas_w, canvas_h))
                        current["crop_x"], current["crop_y"], current["crop_zoom"] = crop_x, crop_y, crop_zoom
                    source_w, source_h = peek_size(data)
                    left, top, width, height = crop_box((source_w, source_h), (canvas_w, canvas_h), current)
                    st.caption(
                        f"원본 {source_w} × {source_h} px 에서 "
                        f"({left}, {top}) 부터 {width} × {height} px 를 잘라 "
                        f"{canvas_w} × {canvas_h} px 로 저장합니다 · "
                        f"위치 X {current['crop_x']:.0f}% / Y {current['crop_y']:.0f}% · 배율 {current['crop_zoom']:.2f}×"
                    )

            try:
                effective = effective_settings(current_key)
                if view == "원본":
                    original = load_preview(data, PREVIEW_MAX_SIDE)
                    st.image(original, width="stretch")
                    source_w, source_h = peek_size(data)
                    st.caption(f"Original · {source_w} × {source_h} px (올린 원본 그대로, 비율 유지)")
                else:
                    preview_result, _meta, source_size, _resized = render_result(
                        data, watermark_image, effective)
                    if view == "크롭 위치 조정":
                        st.markdown("**저장될 결과**")
                        st.image(to_preview(preview_result, 520))
                    else:
                        st.image(to_preview(preview_result), width="stretch")
                    st.caption(f"Edited · {preview_result.size[0]} × {preview_result.size[1]} px "
                               f"· 원본 {source_size[0]} × {source_size[1]} px 에서 만들어진 최종 결과")
                    if effective["canvas_mode"] != "original" and preview_result.size != (canvas_w, canvas_h):
                        st.error(f"출력 크기가 {canvas_w} × {canvas_h} 와 다릅니다.")
            except (OSError, ValueError) as error:
                st.error(f"미리보기를 만들지 못했습니다: {error}")

# ---------------------------------------------------------------- 설정 자동 저장
# 현재 이미지의 개별 설정을 "다음 실행에서 새 이미지에 쓸 기본값" 으로도 기억한다.
if current is not None:
    settings.update(current)
if settings != st.session_state.last_saved:
    if save_config(settings):
        st.session_state.last_saved = dict(settings)
    else:
        st.warning("설정을 config.json 에 저장하지 못했습니다. (폴더 쓰기 권한을 확인해 주세요)")

# ---------------------------------------------------------------- 하단: 일괄 처리 / 다운로드
st.divider()
st.subheader("전체 이미지 처리 · 다운로드")
st.caption(f"사진마다 저장된 개별 설정(출력 크기 · 크롭 · 보정 · 워터마크)으로 각각 처리합니다. "
           f"파일 이름: `원본이름{settings['name_suffix']}`")

if st.button("전체 이미지 처리", type="primary", disabled=not uploaded_images, width="stretch"):
    results, failures = [], []
    progress = st.progress(0.0, text="처리 중…")
    for index, file in enumerate(uploaded_images, start=1):
        try:
            per_image = effective_settings(image_key(file))
            item = process_one(file.getvalue(), file.name, watermark_image, per_image)
            item["expected_size"] = target_canvas(per_image, item["source_size"])
            results.append(item)
        except (OSError, ValueError) as error:
            failures.append((file.name, str(error)))
        progress.progress(index / len(uploaded_images), text=f"처리 중… ({index}/{len(uploaded_images)})")
    progress.empty()
    st.session_state.results = results
    st.session_state.failures = failures

results = st.session_state.get("results", [])
failures = st.session_state.get("failures", [])

for name, message in failures:
    st.error(f"{name} : {message}")

if results:
    size_ok = all(item["result_size"] == item["expected_size"] for item in results)
    sizes = sorted({f"{item['result_size'][0]} × {item['result_size'][1]}" for item in results})
    message = (f"모든 결과가 지정한 크기와 일치합니다 ({', '.join(sizes)} px)." if size_ok
               else "일부 이미지가 지정한 출력 크기와 다릅니다!")
    (st.success if size_ok else st.error)(f"{len(results)}장 완료 · {message}")

    if len(results) == 1:
        single = results[0]
        st.download_button(
            f"⬇ {single['name']} 다운로드",
            data=single["bytes"],
            file_name=single["name"],
            mime=single["mime"],
            width="stretch",
        )
    else:
        st.download_button(
            f"⬇ 전체 ZIP 다운로드 ({len(results)}장)",
            data=make_zip(results),
            file_name="edited.zip",
            mime="application/zip",
            type="primary",
            width="stretch",
        )
        with st.expander("한 장씩 따로 받기"):
            for index, item in enumerate(results):
                st.download_button(
                    f"⬇ {item['name']}  ({item['result_size'][0]} × {item['result_size'][1]})",
                    data=item["bytes"],
                    file_name=item["name"],
                    mime=item["mime"],
                    key=f"w_download_{index}",
                    width="stretch",
                )

st.caption("사진별 설정은 앱을 켜 둔 동안 사진마다 따로 유지됩니다. 다음 실행 때는 출력 크기 · 맞춤 방식 · "
           "워터마크 · 저장 옵션이 `config.json` 에서 그대로 복원되고, 톤 · 색감 · 크롭 위치는 사진마다 "
           "손대지 않은 상태(0 · 가운데)에서 시작합니다.")
