"""
通用大图自动分区识别模块

背景：Qwen2.5-VL 的视觉编码器把图片切成 14x14 patch 后 2x2 合并为视觉 token
（约 像素数/784），token 数量受上下文（num_ctx）与显存限制。整页直读时，
要么图片过大被拒绝，要么密集内容在整页尺度下无法对齐。

本模块的做法：先判断图片是否可以直接单次识别；超限时按“内容”自动分区——
优先用空白行列投影把图切成自然区域（表格、示意图、多图页面），区域仍超预算时
再按自适应网格 + 重叠切分，每块独立高清识别，最后做一次纯文本综合。

本模块不依赖 run.py，通过 call_image / call_text 两个回调注入 Ollama 调用，
便于复用现有配置与错误处理。
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
from typing import Callable, List, Optional, Sequence, Tuple

from PIL import Image

logger = logging.getLogger("vision-tiler")

# 默认参数（可被 analyze_image_detailed 的入参覆盖）
DEFAULT_DIRECT_AREA = 2_000_000      # 面积小于等于此值：单次直读，不分区
DEFAULT_DIRECT_MAX_EDGE = 1920       # 单次直读时允许的最长边（避免过大 token 数）
DEFAULT_TILE_AREA = 1_200_000        # 每块面积预算（像素）
DEFAULT_MAX_TILES = 12               # 分块数量上限，防止耗时失控
DEFAULT_OVERLAP = 0.12               # 网格切分时的重叠比例
MAX_IMAGE_SIZE = 5 * 1024 * 1024     # 单张编码后上限（与 run.py 保持一致）

_THUMB_MAX = 200                     # 密度分析用缩略图最长边
_BLANK_INK = 0.02                    # 行/列投影中“空白”的墨水阈值
_MIN_GAP_RATIO = 0.025               # 可切分的空白间隙最小比例（占对应边长）
_SKIP_INK = 0.0008                   # 内容极少（几乎全白）的块直接跳过
_INK_THRESHOLD = 0.02                # 判定“有内容”的墨水密度阈值
_MIN_COVERAGE = 0.90                 # 分区需覆盖的内容比例，不足则回退整图网格
_ROI_MARGIN = 0.08                   # ROI 外扩比例
_MIN_ROI_AREA = 3.0                  # 区域框最小面积（百分比平方），过滤单引脚等碎框


def _open_rgb(image_path: str) -> Image.Image:
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _encode(img: Image.Image) -> bytes:
    """优先 PNG（文字清晰），超限则回退 JPEG 并逐步降质。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    if len(data) <= MAX_IMAGE_SIZE:
        return data
    for quality in (95, 90, 85, 75):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= MAX_IMAGE_SIZE:
            return data
    return data


def _density_profile(
    img: Image.Image, n: int
) -> Tuple[List[List[float]], List[float], List[float], int, int]:
    """
    计算整图的 n x n 网格墨水密度（0..1，越大越“黑”）。
    返回 (cells, rows_ink, cols_ink, tw, th)，坐标为缩略图坐标。
    """
    gray = img.convert("L")
    gray.thumbnail((_THUMB_MAX, _THUMB_MAX))
    tw, th = gray.size
    px = gray.load()
    cell_w = max(1, tw // n)
    cell_h = max(1, th // n)
    nc = math.ceil(tw / cell_w)
    nr = math.ceil(th / cell_h)
    cells = [[0.0] * nc for _ in range(nr)]
    for r in range(nr):
        y0 = r * cell_h
        y1 = min(th, y0 + cell_h)
        for c in range(nc):
            x0 = c * cell_w
            x1 = min(tw, x0 + cell_w)
            total = 0.0
            count = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    total += (255 - px[x, y]) / 255.0
                    count += 1
            cells[r][c] = total / max(1, count)
    rows_ink = [sum(row) / len(row) for row in cells]
    cols_ink = [sum(cells[r][c] for r in range(nr)) / nr for c in range(nc)]
    return cells, rows_ink, cols_ink, tw, th


def _find_gaps(profile: Sequence[float], cell_size_px: float, total_px: float) -> List[Tuple[int, int]]:
    """找出一维投影中的空白连续段，返回其在原图坐标下的 (start, end)。"""
    min_len_px = max(8.0, total_px * _MIN_GAP_RATIO)
    gaps: List[Tuple[int, int]] = []
    run_start = None
    for i, ink in enumerate(profile):
        if ink < _BLANK_INK:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                length = (i - run_start) * cell_size_px
                if length >= min_len_px:
                    gaps.append((int(run_start * cell_size_px), int(i * cell_size_px)))
                run_start = None
    if run_start is not None:
        length = (len(profile) - run_start) * cell_size_px
        if length >= min_len_px:
            gaps.append((int(run_start * cell_size_px), int(len(profile) * cell_size_px)))
    return gaps


def _split_bands(total: float, gaps: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """按间隙中点把 [0, total) 切成若干连续段。"""
    boundaries = [0.0]
    for start, end in gaps:
        mid = (start + end) / 2.0
        if mid > boundaries[-1] + 1:
            boundaries.append(mid)
    boundaries.append(total)
    bands: List[Tuple[int, int]] = []
    for a, b in zip(boundaries, boundaries[1:]):
        if b - a >= 4:
            bands.append((int(a), int(b)))
    return bands


def _region_ink(cells: Sequence[Sequence[float]], box: Tuple[int, int, int, int], tw: int, th: int, W: int, H: int) -> float:
    """根据密度网格估算某区域（原图坐标）的平均墨水密度。"""
    nr = len(cells)
    nc = len(cells[0]) if nr else 0
    if nr == 0 or nc == 0:
        return 0.0
    x0, y0, x1, y1 = box
    c0 = min(nc - 1, int(x0 / W * nc))
    c1 = max(c0, min(nc - 1, int(x1 / W * nc)))
    r0 = min(nr - 1, int(y0 / H * nr))
    r1 = max(r0, min(nr - 1, int(y1 / H * nr)))
    total = 0.0
    count = 0
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            total += cells[r][c]
            count += 1
    return total / max(1, count)


def _ink_coverage(
    cells: Sequence[Sequence[float]],
    tiles: Sequence[Tuple[int, int, int, int]],
    tw: int,
    th: int,
    W: int,
    H: int,
) -> float:
    """计算 tiles 覆盖了多少比例的内容单元格（按墨水密度判定）。"""
    nr = len(cells)
    nc = len(cells[0]) if nr else 0
    if nr == 0 or nc == 0 or W <= 0 or H <= 0:
        return 1.0
    ink = set()
    for r in range(nr):
        for c in range(nc):
            if cells[r][c] >= _INK_THRESHOLD:
                ink.add((r, c))
    if not ink:
        return 1.0
    covered = set()
    for x0, y0, x1, y1 in tiles:
        c0 = min(nc - 1, int(x0 / W * nc))
        c1 = min(nc - 1, int(x1 / W * nc))
        r0 = min(nr - 1, int(y0 / H * nr))
        r1 = min(nr - 1, int(y1 / H * nr))
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                if (r, c) in ink:
                    covered.add((r, c))
    return len(covered) / len(ink)


def _grid_split(
    box: Tuple[int, int, int, int], budget: int, overlap: float, limit: int
) -> List[Tuple[int, int, int, int]]:
    """把超预算的区域按自适应网格（带重叠）切成不超过 limit 块。"""
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    area = w * h
    n = max(1, math.ceil(area / budget))
    if n > limit:
        n = limit
    aspect = w / max(1.0, h)
    ncols = max(1, round(math.sqrt(n * aspect)))
    nrows = max(1, math.ceil(n / ncols))
    while ncols * nrows < n:
        ncols += 1
    tiles: List[Tuple[int, int, int, int]] = []
    for r in range(nrows):
        for c in range(ncols):
            tx0 = x0 + (c * w) // ncols - int(overlap * w / ncols)
            tx1 = x0 + ((c + 1) * w) // ncols + int(overlap * w / ncols)
            ty0 = y0 + (r * h) // nrows - int(overlap * h / nrows)
            ty1 = y0 + ((r + 1) * h) // nrows + int(overlap * h / nrows)
            tx0 = max(x0, tx0)
            ty0 = max(y0, ty0)
            tx1 = min(x1, tx1)
            ty1 = min(y1, ty1)
            if tx1 - tx0 >= 8 and ty1 - ty0 >= 8:
                tiles.append((tx0, ty0, tx1, ty1))
    return tiles


def _plan_tiles(
    img: Image.Image,
    direct_area: int,
    tile_area: int,
    max_tiles: int,
    overlap: float,
) -> List[Tuple[int, int, int, int]]:
    """规划识别区域：小图单块；大图先空白投影切自然区域，超预算再网格切。"""
    W, H = img.size
    if W * H <= direct_area:
        return [(0, 0, W, H)]

    cells, rows_ink, cols_ink, tw, th = _density_profile(img, 64)
    cell_w = W / max(1, math.ceil(tw / max(1, tw // 64)))
    cell_h = H / max(1, math.ceil(th / max(1, th // 64)))
    h_gaps = _find_gaps(rows_ink, cell_h, H)
    v_gaps = _find_gaps(cols_ink, cell_w, W)

    row_bands = _split_bands(H, h_gaps)
    col_bands = _split_bands(W, v_gaps)
    regions: List[Tuple[int, int, int, int]] = []
    for y0, y1 in row_bands:
        for x0, x1 in col_bands:
            regions.append((x0, y0, x1, y1))

    tiles: List[Tuple[int, int, int, int]] = []
    remaining = max_tiles
    for box in regions:
        x0, y0, x1, y1 = box
        if _region_ink(cells, box, tw, th, W, H) < _SKIP_INK:
            logger.info("跳过空白区域: %s", box)
            continue
        if (x1 - x0) * (y1 - y0) <= tile_area:
            tiles.append(box)
        else:
            tiles.extend(_grid_split(box, tile_area, overlap, remaining))
        remaining = max_tiles - len(tiles)
        if remaining <= 0:
            break

    if not tiles:
        tiles = [(0, 0, W, H)]

    # 空白分区若覆盖率不足（例如过度碎片化/被截断），回退为整图网格切分
    coverage = _ink_coverage(cells, tiles, tw, th, W, H)
    if coverage < _MIN_COVERAGE:
        logger.info("空白分区覆盖率仅 %.0f%%，回退为整图网格切分", coverage * 100)
        tiles = _grid_split((0, 0, W, H), tile_area, overlap, max_tiles)
        tiles = [t for t in tiles if _region_ink(cells, t, tw, th, W, H) >= _SKIP_INK]
        if not tiles:
            tiles = [(0, 0, W, H)]
    return tiles


def _box_pct(box: Tuple[int, int, int, int], W: int, H: int) -> str:
    x0, y0, x1, y1 = box
    return (
        f"x:{round(x0 / W * 100)}%-{round(x1 / W * 100)}%, "
        f"y:{round(y0 / H * 100)}%-{round(y1 / H * 100)}%"
    )


def _parse_roi(text: str) -> List[Tuple[str, List[float]]]:
    """
    从模型文本中容错解析区域框，优先 JSON 数组：
    [{"name": "CN7", "box": [x0, y0, x1, y1]}, ...] 或 [[x0, y0, x1, y1], ...]。
    失败时回退正则：名称 + 四个百分比数字。
    """
    rois: List[Tuple[str, List[float]]] = []
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("box"), (list, tuple)) and len(item["box"]) == 4:
                        name = str(item.get("name") or "region")
                        rois.append((name, [float(v) for v in item["box"]]))
                    elif isinstance(item, (list, tuple)) and len(item) == 4:
                        rois.append(("region", [float(v) for v in item]))
        except (ValueError, TypeError):
            rois = []
    if not rois:
        for mm in re.finditer(
            r"([A-Za-z0-9_\-\u4e00-\u9fff]{1,24})\s*[:：]?\s*[\[(]?\s*(\d+(?:\.\d+)?)\s*[,，]\s*(\d+(?:\.\d+)?)\s*[,，]\s*(\d+(?:\.\d+)?)\s*[,，]\s*(\d+(?:\.\d+)?)\s*[\])]?",
            text,
        ):
            name, a, b, c, d = mm.groups()
            rois.append((name, [float(a), float(b), float(c), float(d)]))
    return rois


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / max(1.0, union)


def _box_to_pct(box: Sequence[float], view_w: int, view_h: int) -> List[float]:
    """把模型返回的框归一化为百分比：坐标 <=100 视为百分比，否则按视图像素换算。"""
    x0, y0, x1, y1 = box
    if max(x0, x1) > 100:
        x0, x1 = x0 / view_w * 100, x1 / view_w * 100
    if max(y0, y1) > 100:
        y0, y1 = y0 / view_h * 100, y1 / view_h * 100
    return [
        max(0.0, min(100.0, x0)),
        max(0.0, min(100.0, y0)),
        max(0.0, min(100.0, x1)),
        max(0.0, min(100.0, y1)),
    ]


def _valid_rois(
    text: str, view_w: int, view_h: int, max_count: int
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    """解析并校验模型返回的区域框，返回 (名称, 视图内像素框)。"""
    out: List[Tuple[str, Tuple[int, int, int, int]]] = []
    for name, raw in _parse_roi(text):
        pct = _box_to_pct(raw, view_w, view_h)
        if not (pct[0] < pct[2] and pct[1] < pct[3]):
            continue
        if (pct[2] - pct[0]) * (pct[3] - pct[1]) < _MIN_ROI_AREA:
            continue
        box = (
            int(pct[0] / 100 * view_w),
            int(pct[1] / 100 * view_h),
            int(pct[2] / 100 * view_w),
            int(pct[3] / 100 * view_h),
        )
        if box[2] - box[0] >= 16 and box[3] - box[1] >= 16:
            out.append((name, box))
        if len(out) >= max_count:
            break
    # 去重（IoU > 0.8）
    dedup: List[Tuple[str, Tuple[int, int, int, int]]] = []
    for name, box in out:
        if any(_iou(box, existing) > 0.8 for _, existing in dedup):
            continue
        dedup.append((name, box))
    return dedup


def analyze_detailed(
    image_path: str,
    prompt: str,
    call_image: Callable[[str, str], str],
    call_text: Callable[[str], str],
    direct_area: int = DEFAULT_DIRECT_AREA,
    tile_area: int = DEFAULT_TILE_AREA,
    max_tiles: int = DEFAULT_MAX_TILES,
    overlap: float = DEFAULT_OVERLAP,
    roi_guided: bool = False,
) -> str:
    """
    自动分区识别大图。

    Args:
        image_path: 本地图片路径
        prompt: 用户的识别任务
        call_image: (base64, prompt) -> 文本 的图像识别回调
        call_text: (prompt) -> 文本 的纯文本回调（用于最终综合）
        其余参数控制分区策略
        roi_guided: 是否启用两级（概览->放大）模型引导定位。默认关闭，
            因其定位框精度依赖模型空间理解，对超密集图可能不理想。

    Returns:
        综合后的最终文本
    """
    import base64

    img = _open_rgb(image_path)
    W, H = img.size
    logger.info("分析图片 %s，尺寸 %dx%d", image_path, W, H)

    tiles = _plan_tiles(img, direct_area, tile_area, max_tiles, overlap)
    logger.info("预分区结果：%d 块", len(tiles))

    # 单块且就是整图：直接按最高可接受分辨率识别
    if len(tiles) == 1 and tiles[0] == (0, 0, W, H):
        work = img
        if max(W, H) > min(direct_area // min(W, H), 1920):
            scale = min(1.0, 1920 / max(W, H))
            work = img.resize((max(1, int(W * scale)), max(1, int(H * scale))), Image.LANCZOS)
        b64 = base64.b64encode(_encode(work)).decode("utf-8")
        return call_image(b64, prompt)

    overview_text = ""
    planned: List[Tuple[Tuple[int, int, int, int], str]] = []
    if roi_guided and len(tiles) > 1:
        import base64 as _b64

        # —— 第一级：整图概览，粗粒度板块定位 ——
        ov = img.copy()
        ov.thumbnail((768, 768))
        ov_w, ov_h = ov.size
        b64 = _b64.b64encode(_encode(ov)).decode("utf-8")
        try:
            overview_text = call_image(
                b64,
                "这是整张图片的低分辨率概览。请做两件事："
                "1) 简述整体布局；"
                "2) 识别图中与任务相关的独立大板块（如各图、各表格），"
                "以 JSON 数组输出，格式 [{\"name\": \"区域名\", \"box\": [x0, y0, x1, y1]}, ...]，"
                "坐标用百分比（0-100）或图片像素均可。若无法划分区域，输出 []。",
            )
            logger.info("概览识别完成")
            coarse = _valid_rois(overview_text, ov_w, ov_h, 4)
            logger.info("概览定位到 %d 个大板块", len(coarse))

            # —— 第二级：板块放大后精确定位小区域 ——
            fine: List[Tuple[Tuple[int, int, int, int], str]] = []
            for region_name, region_box in coarse:
                rx0, ry0, rx1, ry1 = region_box
                mx = int(_ROI_MARGIN * (rx1 - rx0))
                my = int(_ROI_MARGIN * (ry1 - ry0))
                px = (max(0, rx0 - mx), max(0, ry0 - my), min(W, rx1 + mx), min(H, ry1 + my))
                region_img = img.crop(px)
                zoom = region_img.copy()
                zoom.thumbnail((1200, 1200))
                zw, zh = zoom.size
                zb64 = _b64.b64encode(_encode(zoom)).decode("utf-8")
                try:
                    ztext = call_image(
                        zb64,
                        "这是从一张大图中放大出来的一个区域（%s）。"
                        "请把图中每个独立板块的整个区域（如每个排针整体、每个表格整体，包含其全部内容）"
                        "分别用一个包围盒框出，不要框单个元素（如单个引脚、单个单元格）。"
                        "输出 JSON 数组，格式 [{\"name\": \"板块名\", \"box\": [x0, y0, x1, y1]}, ...]，"
                        "坐标用百分比（0-100）或图片像素均可。看不清的跳过。" % region_name,
                    )
                    sub = _valid_rois(ztext, zw, zh, 12)
                    if sub:
                        for sub_name, sbox in sub:
                            full = (
                                px[0] + int(sbox[0] / zw * (px[2] - px[0])),
                                px[1] + int(sbox[1] / zh * (px[3] - px[1])),
                                px[0] + int(sbox[2] / zw * (px[2] - px[0])),
                                px[1] + int(sbox[3] / zh * (px[3] - px[1])),
                            )
                            fine.append((full, f"{region_name}/{sub_name}"))
                        logger.info("板块 %s 定位到 %d 个小区域", region_name, len(sub))
                    else:
                        fine.append((px, region_name))
                except Exception as e:  # noqa: BLE001
                    logger.warning("板块 %s 精确定位失败，回退为整块: %s", region_name, e)
                    fine.append((px, region_name))

            if fine:
                planned = fine[:max_tiles]
                logger.info("两级定位分区：%d 块", len(planned))
        except Exception as e:  # noqa: BLE001
            logger.warning("概览识别失败: %s", e)

    if not planned:
        planned = [(box, f"region{i}") for i, box in enumerate(tiles, 1)]

    tile_results: List[str] = []
    total = len(planned)
    for i, (box, name) in enumerate(planned, 1):
        tile_prompt = (
            f"{prompt}\n\n注意：这张子图是原图中的一个区域「{name}」"
            f"（位于 {_box_pct(box, W, H)}）。请只针对这个区域回答；"
            f"若该区域与本任务无关请直接说\"本块无关\"。看不清的写\"看不清\"，不要编造。"
        )
        tile_results.append((box, tile_prompt))

    for i, (box, tile_prompt) in enumerate(tile_results, 1):
        x0, y0, x1, y1 = box
        tile_img = img.crop((x0, y0, x1, y1))
        b64 = base64.b64encode(_encode(tile_img)).decode("utf-8")
        try:
            result = call_image(b64, tile_prompt)
            logger.info("区域 %d/%d 识别完成（%s）", i, total, _box_pct(box, W, H))
        except Exception as e:  # noqa: BLE001
            logger.error("区域 %d/%d 识别失败: %s", i, total, e)
            result = f"[本块识别失败: {e}]"
        tile_results[i - 1] = f"[区域 {i}/{total}，{_box_pct(box, W, H)}]\n{result}"

    merge_prompt = (
        "下面是对同一张大图不同区域的分析结果。请把它们综合成一份连贯、完整、"
        "按空间顺序（从上到下、从左到右）的最终回答：去重、修正冲突、"
        "省略\"本块无关\"或重复的说明。\n\n"
        "[整图概览]\n" + (overview_text or "（无）") + "\n\n" +
        "\n\n".join(tile_results) +
        "\n\n请直接输出综合后的最终回答。"
    )
    try:
        merged = call_text(merge_prompt)
        logger.info("综合完成")
        return merged
    except Exception as e:  # noqa: BLE001
        logger.error("综合失败，返回分块结果: %s", e)
        return "\n\n".join(tile_results)
