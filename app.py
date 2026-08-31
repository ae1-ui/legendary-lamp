"""WATERMARK TOOL - 이미지에 PNG 워터마크를 자동으로 합성하는 로컬 웹앱.

원본 이미지의 가로/세로 픽셀 크기는 절대 바꾸지 않습니다.
(크롭/리사이즈 없음, 워터마크만 위에 합성)
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import streamlit as st
from PIL import Image, ImageOps

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_WATERMARK_PATH = APP_DIR / "watermark.png"

SUPPORTED_TYPES = ["jpg", "jpeg", "png", "webp"]
JPEG_QUALITY = 95
WEBP_QUALITY = 95
PREVIEW_MAX_SIDE = 1100

DEFAULTS = {
    "size_mode": "percent",       # "percent" | "pixel"
    "size_percent": 16.0,         # 이미지 너비 대비 %
    "size_px": 300,               # 워터마크 너비(px)
    "opacity": 100,               # 0 ~ 100
    "position": "bottom_right",
    "margin_mode": "percent",     # "percent" | "pixel"
    "margin_x_percent": 2.5,
    "margin_y_percent": 2.0,
    "margin_x_px": 24,
    "margin_y_px": 24,
    "custom_unit": "percent",     # "percent" | "pixel"
    "custom_x_percent": 78.0,
    "custom_y_percent": 88.0,
    "custom_x_px": 0,
    "custom_y_px": 0,
}

POSITION_LABELS = {
    "top_left": "왼쪽 위",
    "top_right": "오른쪽 위",
    "bottom_left": "왼쪽 아래",
    "bottom_right": "오른쪽 아래",
    "center": "중앙",
    "custom": "사용자 지정 (X / Y 직접 입력)",
}
POSITION_KEYS = list(POSITION_LABELS)

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


def load_config() -> dict:
    """config.json 을 읽어 이전 설정을 복원한다. 없거나 깨졌으면 기본값."""
    raw = {}
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (json.JSONDecodeError, OSError):
            raw = {}

    d = DEFAULTS
    return {
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
    }


def save_config(config: dict) -> bool:
    try:
        CONFIG_PATH.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 이미지 처리

def open_image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def normalize_orientation(image: Image.Image) -> Image.Image:
    """EXIF orientation 을 실제 픽셀에 반영하고 orientation 태그는 제거한다."""
    return ImageOps.exif_transpose(image)


def watermark_target_size(base_w: int, wm_w: int, wm_h: int, config: dict) -> tuple[int, int]:
    if config["size_mode"] == "percent":
        target_w = base_w * config["size_percent"] / 100.0
    else:
        target_w = float(config["size_px"])
    target_w = max(1, int(round(target_w)))
    target_h = max(1, int(round(wm_h * (target_w / wm_w))))
    return target_w, target_h


def watermark_position(base_w: int, base_h: int, wm_w: int, wm_h: int, config: dict) -> tuple[int, int]:
    """워터마크 왼쪽 위 모서리가 놓일 좌표를 계산한다."""
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
    """원본과 동일한 픽셀 크기의 RGBA 결과를 돌려준다."""
    base_w, base_h = base.size
    wm_w, wm_h = watermark.size

    target_w, target_h = watermark_target_size(base_w, wm_w, wm_h, config)
    resized = watermark.resize((target_w, target_h), Image.LANCZOS)
    resized = apply_opacity(resized, config["opacity"])

    x, y = watermark_position(base_w, base_h, target_w, target_h, config)

    layer = Image.new("RGBA", (base_w, base_h), (0, 0, 0, 0))
    layer.paste(resized, (x, y))          # 이미지 밖으로 나가는 부분은 자동으로 잘림
    return Image.alpha_composite(base.convert("RGBA"), layer)


def encode_image(result: Image.Image, source: Image.Image, out_format: str, source_format: str) -> bytes:
    """색상 프로파일(ICC)과 EXIF 를 가능한 한 유지한 채 저장한다."""
    source_mode = source.mode
    has_alpha = source_mode in ("RGBA", "LA", "PA") or "transparency" in source.info

    save_kwargs: dict = {}
    icc_profile = source.info.get("icc_profile")
    # CMYK/LAB 등은 RGB 로 바뀌므로 원본 프로파일을 그대로 붙이면 색이 틀어진다.
    if icc_profile and source_mode not in ("CMYK", "LAB", "YCbCr"):
        save_kwargs["icc_profile"] = icc_profile

    exif = source.info.get("exif")   # orientation 은 이미 제거된 상태

    if out_format == "JPEG":
        image = result.convert("RGB")
        # "keep" = 원본 JPEG 의 크로마 서브샘플링을 그대로 유지 (재인코딩 손실 최소화)
        save_kwargs.update(quality=JPEG_QUALITY, subsampling="keep" if source_format == "JPEG" else 0)
        if exif:
            save_kwargs["exif"] = exif
    elif out_format == "WEBP":
        image = result if has_alpha else result.convert("RGB")
        save_kwargs.update(quality=WEBP_QUALITY, method=6)
        if exif:
            save_kwargs["exif"] = exif
    else:  # PNG
        image = result if has_alpha else result.convert("RGB")

    buffer = io.BytesIO()
    try:
        image.save(buffer, format=out_format, **save_kwargs)
    except (OSError, ValueError):
        # subsampling="keep" 등 원본 의존 옵션이 거부되면 안전한 기본값으로 재시도
        buffer = io.BytesIO()
        save_kwargs.pop("subsampling", None)
        save_kwargs.pop("exif", None)
        image.save(buffer, format=out_format, **save_kwargs)
    return buffer.getvalue()


def output_name(filename: str) -> str:
    path = Path(filename)
    return f"{path.stem}_watermarked{path.suffix}"


def process_one(data: bytes, filename: str, watermark: Image.Image, config: dict) -> dict:
    with open_image(data) as opened:
        source = normalize_orientation(opened)
        source_format = (opened.format or "PNG").upper()

    out_format = "JPEG" if source_format == "MPO" else source_format   # 일부 카메라 JPG

    result = compose(source, watermark, config)
    payload = encode_image(result, source, out_format, source_format)

    return {
        "name": output_name(filename),
        "bytes": payload,
        "mime": MIME_BY_FORMAT.get(out_format, "application/octet-stream"),
        "source_size": source.size,
        "result_size": result.size,
        "image": result,
    }


def make_zip(results: list[dict]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in results:
            archive.writestr(item["name"], item["bytes"])
    return buffer.getvalue()


def to_preview(image: Image.Image) -> Image.Image:
    """화면 표시용 축소본 (저장되는 파일과는 무관)."""
    preview = image.copy()
    preview.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE), Image.LANCZOS)
    return preview


# ---------------------------------------------------------------- 화면

st.set_page_config(page_title="WATERMARK TOOL", page_icon="💧", layout="centered")

st.title("WATERMARK TOOL")
st.caption("이미지 크기는 그대로, 워터마크만 얹어 드립니다.")

if "config" not in st.session_state:
    st.session_state.config = load_config()
saved_config = st.session_state.config

# 1) 워터마크 PNG ------------------------------------------------------------
st.subheader("1. 워터마크 PNG 선택")

uploaded_watermark = st.file_uploader(
    "워터마크로 쓸 투명 배경 PNG 파일", type=["png"], key="watermark_file"
)

watermark_image = None
if uploaded_watermark is not None:
    try:
        watermark_image = open_image(uploaded_watermark.getvalue()).convert("RGBA")
        st.success(f"업로드한 워터마크 사용 중 · {watermark_image.width} × {watermark_image.height} px")
    except OSError:
        st.error("워터마크 PNG 파일을 읽을 수 없습니다. 다른 파일을 선택해 주세요.")
elif DEFAULT_WATERMARK_PATH.exists():
    try:
        watermark_image = Image.open(DEFAULT_WATERMARK_PATH).convert("RGBA")
        st.info(
            f"폴더에 있는 기본 파일 `watermark.png` 사용 중 · "
            f"{watermark_image.width} × {watermark_image.height} px"
        )
    except OSError:
        st.error("`watermark.png` 파일을 읽을 수 없습니다. 위에서 다른 PNG 를 올려 주세요.")
else:
    st.warning("워터마크 PNG 를 올려 주세요. (또는 앱 폴더에 `watermark.png` 를 넣어 두세요)")

if watermark_image is not None:
    st.image(to_preview(watermark_image), caption="선택된 워터마크", width=220)

# 2) 이미지 업로드 -----------------------------------------------------------
st.subheader("2. 이미지 업로드")
uploaded_images = st.file_uploader(
    "JPG / JPEG / PNG / WEBP · 여러 장을 한 번에 끌어다 놓으세요",
    type=SUPPORTED_TYPES,
    accept_multiple_files=True,
    key="image_files",
)
if uploaded_images:
    st.caption(f"{len(uploaded_images)}장 선택됨")

# 3) 설정 --------------------------------------------------------------------
st.subheader("3. 워터마크 설정")

size_col, opacity_col = st.columns(2)
with size_col:
    size_mode_label = st.radio(
        "워터마크 크기 기준",
        ["이미지 너비 대비 %", "픽셀(px)"],
        index=0 if saved_config["size_mode"] == "percent" else 1,
        horizontal=True,
        key="size_mode_label",
    )
    size_mode = "percent" if size_mode_label.startswith("이미지") else "pixel"
    if size_mode == "percent":
        size_percent = st.slider(
            "워터마크 크기 (이미지 너비의 %)", 1.0, 100.0,
            float(saved_config["size_percent"]), 0.5, key="size_percent",
        )
        size_px = saved_config["size_px"]
    else:
        size_px = st.number_input(
            "워터마크 너비 (px)", 1, 20000, int(saved_config["size_px"]), 10, key="size_px",
        )
        size_percent = saved_config["size_percent"]

with opacity_col:
    opacity = st.slider(
        "워터마크 투명도 (%)", 0, 100, int(saved_config["opacity"]), 1, key="opacity",
        help="100 = 원본 PNG 그대로, 0 = 완전히 투명",
    )

position = st.selectbox(
    "워터마크 위치",
    POSITION_KEYS,
    index=POSITION_KEYS.index(saved_config["position"]),
    format_func=lambda key: POSITION_LABELS[key],
    key="position",
)

custom_unit = saved_config["custom_unit"]
custom_x_percent, custom_y_percent = saved_config["custom_x_percent"], saved_config["custom_y_percent"]
custom_x_px, custom_y_px = saved_config["custom_x_px"], saved_config["custom_y_px"]
margin_mode = saved_config["margin_mode"]
margin_x_percent, margin_y_percent = saved_config["margin_x_percent"], saved_config["margin_y_percent"]
margin_x_px, margin_y_px = saved_config["margin_x_px"], saved_config["margin_y_px"]

if position == "custom":
    custom_unit_label = st.radio(
        "X / Y 입력 방식",
        ["백분율(%)", "픽셀(px)"],
        index=0 if custom_unit == "percent" else 1,
        horizontal=True,
        key="custom_unit_label",
    )
    custom_unit = "percent" if custom_unit_label.startswith("백분율") else "pixel"
    x_col, y_col = st.columns(2)
    if custom_unit == "percent":
        with x_col:
            custom_x_percent = st.number_input(
                "X (이미지 너비의 %)", -100.0, 200.0, float(custom_x_percent), 0.5, key="custom_x_percent")
        with y_col:
            custom_y_percent = st.number_input(
                "Y (이미지 높이의 %)", -100.0, 200.0, float(custom_y_percent), 0.5, key="custom_y_percent")
    else:
        with x_col:
            custom_x_px = st.number_input("X (px)", -20000, 20000, int(custom_x_px), 10, key="custom_x_px")
        with y_col:
            custom_y_px = st.number_input("Y (px)", -20000, 20000, int(custom_y_px), 10, key="custom_y_px")
    st.caption("X / Y 는 워터마크의 **왼쪽 위 모서리** 좌표입니다.")
else:
    margin_mode_label = st.radio(
        "여백 입력 방식",
        ["백분율(%)", "픽셀(px)"],
        index=0 if margin_mode == "percent" else 1,
        horizontal=True,
        key="margin_mode_label",
    )
    margin_mode = "percent" if margin_mode_label.startswith("백분율") else "pixel"
    x_col, y_col = st.columns(2)
    if margin_mode == "percent":
        with x_col:
            margin_x_percent = st.number_input(
                "좌우 여백 (이미지 너비의 %)", 0.0, 49.0, float(margin_x_percent), 0.1, key="margin_x_percent")
        with y_col:
            margin_y_percent = st.number_input(
                "위아래 여백 (이미지 높이의 %)", 0.0, 49.0, float(margin_y_percent), 0.1, key="margin_y_percent")
    else:
        with x_col:
            margin_x_px = st.number_input("좌우 여백 (px)", 0, 20000, int(margin_x_px), 5, key="margin_x_px")
        with y_col:
            margin_y_px = st.number_input("위아래 여백 (px)", 0, 20000, int(margin_y_px), 5, key="margin_y_px")
    if position == "center":
        st.caption("중앙 정렬에서는 여백이 사용되지 않습니다.")

config = {
    "size_mode": size_mode,
    "size_percent": float(size_percent),
    "size_px": int(size_px),
    "opacity": int(opacity),
    "position": position,
    "margin_mode": margin_mode,
    "margin_x_percent": float(margin_x_percent),
    "margin_y_percent": float(margin_y_percent),
    "margin_x_px": int(margin_x_px),
    "margin_y_px": int(margin_y_px),
    "custom_unit": custom_unit,
    "custom_x_percent": float(custom_x_percent),
    "custom_y_percent": float(custom_y_percent),
    "custom_x_px": int(custom_x_px),
    "custom_y_px": int(custom_y_px),
}

if config != saved_config:
    if save_config(config):
        st.session_state.config = config
    else:
        st.warning("설정을 config.json 에 저장하지 못했습니다. (폴더 쓰기 권한 확인)")
st.caption("바꾼 설정은 `config.json` 에 자동 저장되고, 다음에 켤 때 그대로 불러옵니다.")

# 4) 미리보기 ----------------------------------------------------------------
st.subheader("4. 미리보기")

ready = watermark_image is not None and bool(uploaded_images)
if not ready:
    st.info("워터마크 PNG 와 이미지를 모두 선택하면 미리보기가 나타납니다.")
else:
    names = [file.name for file in uploaded_images]
    chosen = st.selectbox("미리 볼 이미지", range(len(names)), format_func=lambda i: names[i], key="preview_index")
    target = uploaded_images[chosen]
    try:
        preview = process_one(target.getvalue(), target.name, watermark_image, config)
    except (OSError, ValueError) as error:
        st.error(f"미리보기를 만들지 못했습니다: {error}")
    else:
        st.image(to_preview(preview["image"]), caption=f"결과 미리보기 · {preview['name']}", width="stretch")
        st.caption(
            f"원본 {preview['source_size'][0]} × {preview['source_size'][1]} px → "
            f"결과 {preview['result_size'][0]} × {preview['result_size'][1]} px (크기 동일)"
        )

# 5) 적용 및 다운로드 ---------------------------------------------------------
st.subheader("5. 워터마크 적용")

if st.button("워터마크 적용", type="primary", disabled=not ready, width="stretch"):
    results, failures = [], []
    progress = st.progress(0.0, text="처리 중…")
    for index, file in enumerate(uploaded_images, start=1):
        try:
            results.append(process_one(file.getvalue(), file.name, watermark_image, config))
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
    ok = all(item["source_size"] == item["result_size"] for item in results)
    st.success(
        f"{len(results)}장 완료 · "
        + ("모든 결과가 원본과 같은 픽셀 크기입니다." if ok else "일부 이미지 크기가 다릅니다!")
    )

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
            file_name="watermarked.zip",
            mime="application/zip",
            type="primary",
            width="stretch",
        )
        with st.expander("한 장씩 따로 받기"):
            for index, item in enumerate(results):
                st.download_button(
                    f"⬇ {item['name']}",
                    data=item["bytes"],
                    file_name=item["name"],
                    mime=item["mime"],
                    key=f"download_{index}",
                    width="stretch",
                )
