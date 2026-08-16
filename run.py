#!/usr/bin/env python3
"""
Qwen2.5-VL MCP Server - 基于 Ollama 的视觉识别服务

通过 Model Context Protocol (MCP) 将本地 Qwen2.5-VL 视觉模型接入 Trae IDE。
遵循 deepseek 建议的硬性技术约束：
- 使用 POST /api/generate 端点
- 图片通过 images 数组字段传递，严禁拼接在 prompt 中
- 使用 Pillow 进行图片预处理（RGB转换、压缩）
- 使用 @mcp.tool() 装饰器注册工具
"""

import os
import sys
import base64
import logging
import json
import time
import glob
import functools
from pathlib import Path
from io import BytesIO
from typing import Optional

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP
from PIL import Image
from PIL import ImageGrab
from src import vision_tiler
from src import model_lifecycle

# 加载环境变量
load_dotenv()

# 配置日志
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("qwen2.5vl-mcp")

# 配置
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen2.5vl:7b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))
MAX_IMAGE_SIZE = int(os.getenv("MAX_IMAGE_SIZE", "5242880"))  # 5MB
MAX_IMAGE_DIM = int(os.getenv("MAX_IMAGE_DIM", "1024"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))  # 视觉 token 所需的上下文
VISION_DIRECT_AREA = int(os.getenv("VISION_DIRECT_AREA", str(vision_tiler.DEFAULT_DIRECT_AREA)))
VISION_TILE_AREA = int(os.getenv("VISION_TILE_AREA", str(vision_tiler.DEFAULT_TILE_AREA)))
VISION_MAX_TILES = int(os.getenv("VISION_MAX_TILES", str(vision_tiler.DEFAULT_MAX_TILES)))
VISION_OVERLAP = float(os.getenv("VISION_OVERLAP", str(vision_tiler.DEFAULT_OVERLAP)))
VISION_ROI_GUIDED = os.getenv("VISION_ROI_GUIDED", "0") == "1"

# 支持的图像格式
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

mcp = FastMCP()


def request_model_unload() -> None:
    """统一清理阶段：任务结束后主动请求 Ollama 尽快卸载视觉模型（graceful unload）。"""
    try:
        result = model_lifecycle.unload_model(VISION_MODEL, host=OLLAMA_HOST)
        logger.info(
            "模型生命周期: %s -> %s (%dms)",
            VISION_MODEL,
            result.get("detail"),
            result.get("elapsed_ms", 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("请求卸载视觉模型 %s 失败（不影响任务结果）: %s", VISION_MODEL, exc)


def unload_after_task(func):
    """工具装饰器：读取工具参数 unload_after_task，决定任务结束后是否请求卸载本 Worker 模型。

    unload_after_task=True（默认）：任务无论成功/失败/内部报错，返回前统一请求
    Ollama 尽快卸载模型（适合任务完成、准备切换到另一个 Worker 时）。
    unload_after_task=False：保持模型驻留，交给 Ollama 默认 keep_alive 自动释放
    （适合连续多次调用本 Worker 的场景，避免反复加载）。
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        do_unload = kwargs.pop("unload_after_task", True)
        try:
            return func(*args, **kwargs)
        finally:
            if do_unload:
                request_model_unload()

    return wrapper


def preprocess_pil_image(img: Image.Image) -> str:
    """
    PIL 图片预处理：转换为RGB模式、压缩尺寸、转为Base64编码

    Args:
        img: 已打开的 PIL 图像

    Returns:
        纯Base64编码字符串（无前缀）

    Raises:
        ValueError: 文件过大或格式不支持
        RuntimeError: 图片处理失败
    """
    try:
        # 转换为 RGB 模式（防止 PNG 透明通道报错）
        if img.mode != "RGB":
            img = img.convert("RGB")
        
        # 获取原始尺寸
        width, height = img.size
        
        # 若图片尺寸超过 MAX_IMAGE_DIM，等比例缩放
        if max(width, height) > MAX_IMAGE_DIM:
            if width > height:
                new_width = MAX_IMAGE_DIM
                new_height = int(height * (MAX_IMAGE_DIM / width))
            else:
                new_height = MAX_IMAGE_DIM
                new_width = int(width * (MAX_IMAGE_DIM / height))
            img = img.resize((new_width, new_height), Image.LANCZOS)
            logger.info(f"图片已缩放至 {new_width}x{new_height}")
        
        # 保存为 JPEG 格式（质量 85%），便于控制文件大小
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        image_data = buffer.getvalue()
        
        # 检查处理后的文件大小
        if len(image_data) > MAX_IMAGE_SIZE:
            raise ValueError(f"图片处理后仍过大 ({len(image_data)} bytes)，最大支持 {MAX_IMAGE_SIZE} bytes")
        
        # 转换为 Base64 字符串，去除前缀，只保留纯编码字符
        return base64.b64encode(image_data).decode("utf-8")
    
    except FileNotFoundError:
        raise
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"图片处理失败: {str(e)}")


def preprocess_image(image_path: str) -> str:
    """
    图片预处理：打开本地文件后复用 preprocess_pil_image

    Args:
        image_path: 本地图像文件路径

    Returns:
        纯Base64编码字符串（无前缀）

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 文件过大或格式不支持
        RuntimeError: 图片处理失败
    """
    # 处理 Windows 路径，转换为绝对路径
    resolved_path = Path(image_path).resolve()

    if not resolved_path.exists():
        raise FileNotFoundError(f"图像文件不存在: {resolved_path}")

    # 检查文件扩展名
    ext = resolved_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持的图像格式: {ext}，支持的格式: {SUPPORTED_EXTENSIONS}")

    try:
        img = Image.open(resolved_path)
        return preprocess_pil_image(img)
    except FileNotFoundError:
        raise
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"图片处理失败: {str(e)}")


def call_ollama_vision(image_base64: str, prompt: str) -> str:
    """
    调用 Ollama API 进行图像分析（使用 /api/generate 端点）

    Args:
        image_base64: 纯Base64编码的图像字符串（无前缀）
        prompt: 用户提问

    Returns:
        模型返回的文本结果

    Raises:
        ConnectionError: 无法连接到 Ollama 服务
        RuntimeError: API 调用失败
    """
    return _post_generate({
        "model": VISION_MODEL,
        "prompt": prompt,
        "images": [image_base64],
        "stream": False,
        "options": {"num_ctx": OLLAMA_NUM_CTX},
    })


def call_ollama_text(prompt: str) -> str:
    """
    调用 Ollama 进行纯文本推理（用于自动分区后的结果综合）
    """
    return _post_generate({
        "model": VISION_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": OLLAMA_NUM_CTX},
    })


def _post_generate(payload: dict) -> str:
    """发送 /api/generate 请求并统一处理错误。"""
    url = f"{OLLAMA_HOST}/api/generate"

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=OLLAMA_TIMEOUT
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise ConnectionError("错误：请确保 Ollama 服务已启动（ollama serve）")
    except requests.exceptions.Timeout:
        raise RuntimeError(f"请求超时，Ollama 处理时间超过 {OLLAMA_TIMEOUT} 秒")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Ollama API 调用失败: {str(e)}")

    try:
        result = response.json()
        return result.get("response", "")
    except json.JSONDecodeError:
        raise RuntimeError("Ollama 返回无效的 JSON 响应")


@mcp.tool()
@unload_after_task
def analyze_image_detailed(image_path: str, custom_prompt: Optional[str] = None, max_tiles: Optional[int] = None, unload_after_task: bool = True) -> str:
    """
    分析大图/密集图：自动按内容分块、高清识别后合并结果

    Args:
        image_path: 本地图像文件的绝对路径
        custom_prompt: 对图像的提问或分析指令，默认为"请详细描述这张图片的内容"
        max_tiles: 最大分块数（可选，默认由 VISION_MAX_TILES 控制）
        unload_after_task: True=任务结束后立即请求 Ollama 卸载模型（默认）；False=保持驻留，交给 Ollama keep_alive 自动释放

    Returns:
        综合后的图像分析结果
    """
    prompt = custom_prompt if custom_prompt else "请详细描述这张图片的内容"
    logger.info("收到详细图像分析请求: prompt=%s...", prompt[:50])
    try:
        result = vision_tiler.analyze_detailed(
            image_path=image_path,
            prompt=prompt,
            call_image=call_ollama_vision,
            call_text=call_ollama_text,
            tile_area=VISION_TILE_AREA,
            max_tiles=max_tiles or VISION_MAX_TILES,
            overlap=VISION_OVERLAP,
            direct_area=VISION_DIRECT_AREA,
            roi_guided=VISION_ROI_GUIDED,
        )
        logger.info("详细图像分析完成")
        return result
    except Exception as e:
        logger.error("详细图像分析失败: %s", str(e), exc_info=True)
        return f"错误: {str(e)}"


@mcp.tool()
@unload_after_task
def analyze_image(image_path: str, custom_prompt: Optional[str] = None, unload_after_task: bool = True) -> str:
    """
    分析图像内容

    Args:
        image_path: 本地图像文件的绝对路径（如: C:/Users/user/image.png）
        custom_prompt: 对图像的提问或分析指令，默认为"请详细描述这张图片的内容"
        unload_after_task: True=任务结束后立即请求 Ollama 卸载模型（默认）；False=保持驻留，交给 Ollama keep_alive 自动释放

    Returns:
        图像分析结果
    """
    # 设置默认 prompt
    prompt = custom_prompt if custom_prompt else "请详细描述这张图片的内容"
    logger.info(f"收到图像分析请求: prompt={prompt[:50]}...")

    # 预处理图片
    try:
        image_base64 = preprocess_image(image_path)
        logger.info(f"图像文件已加载并预处理: {image_path}")
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        logger.error(f"图像文件处理失败: {str(e)}")
        return f"错误: {str(e)}"

    # 调用 Ollama 视觉模型
    try:
        result = call_ollama_vision(image_base64, prompt)
        logger.info("图像分析完成")
        return result
    except (ConnectionError, RuntimeError) as e:
        logger.error(f"Ollama API 调用失败: {str(e)}")
        return f"错误: {str(e)}"


def _latest_screenshot_path() -> Optional[str]:
    """在常见截图保存位置中查找最新的截图文件（按修改时间倒序）。"""
    candidates = [
        os.environ.get("SCREENSHOT_DIR", ""),
        os.path.join(os.environ.get("USERPROFILE", ""), "Pictures", "Screenshots"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Pictures"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
        os.environ.get("TEMP", ""),
        os.environ.get("TMP", ""),
    ]
    patterns = []
    for d in candidates:
        if not d or not os.path.isdir(d):
            continue
        patterns.extend(os.path.join(d, f"*{ext}") for ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp"))
    newest = None
    newest_mtime = 0.0
    for p in patterns:
        for f in glob.glob(p):
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue
            if mtime > newest_mtime:
                newest = f
                newest_mtime = mtime
    return newest


def _analyze_clipboard_or_screenshot(prompt: str, allow_screen: bool, allow_latest: bool, prefer: str = "clipboard") -> str:
    """
    分析剪贴板图片 / 最新截图 / 屏幕捕获，返回分析结果或错误信息。
    prefer: "clipboard" 时优先剪贴板，其次按 allow_* 兜底；"latest" 时优先最新截图。
    """
    candidates = []
    if allow_latest:
        latest = _latest_screenshot_path()
        if latest:
            candidates.append((f"最新截图 {latest}", latest))

    # 剪贴板：优先解析为图片对象或文件列表
    clipboard_img = None
    clipboard_label = None
    try:
        grabbed = ImageGrab.grabclipboard()
        if isinstance(grabbed, Image.Image):
            clipboard_img = grabbed
            clipboard_label = "剪贴板图片"
        elif isinstance(grabbed, list) and grabbed:
            first = str(grabbed[0])
            if Path(first).suffix.lower() in SUPPORTED_EXTENSIONS and os.path.exists(first):
                clipboard_img = Image.open(first)
                clipboard_label = f"剪贴板文件 {first}"
    except Exception:
        clipboard_img = None
        clipboard_label = None

    if prefer == "clipboard":
        if clipboard_img is not None:
            try:
                return call_ollama_vision(preprocess_pil_image(clipboard_img), prompt)
            except Exception as e:
                return f"错误: 剪贴板图片处理失败: {str(e)}"
        for label, path in candidates:
            if path:
                try:
                    return call_ollama_vision(preprocess_image(path), prompt)
                except Exception as e:
                    return f"错误: {label}处理失败: {str(e)}"
        if allow_screen:
            try:
                screen = ImageGrab.grab()
                return call_ollama_vision(preprocess_pil_image(screen), prompt)
            except Exception as e:
                return f"错误: 屏幕捕获失败: {str(e)}"
        return "错误: 剪贴板中没有图片，也没有找到可用的最新截图。请先截图（Win+Shift+S 会复制到剪贴板），或直接告诉我要截取整个屏幕。"

    # prefer == "latest"
    for label, path in candidates:
        if path:
            try:
                return call_ollama_vision(preprocess_image(path), prompt)
            except Exception as e:
                return f"错误: {label}处理失败: {str(e)}"
    if clipboard_img is not None:
        try:
            return call_ollama_vision(preprocess_pil_image(clipboard_img), prompt)
        except Exception as e:
            return f"错误: 剪贴板图片处理失败: {str(e)}"
    if allow_screen:
        try:
            screen = ImageGrab.grab()
            return call_ollama_vision(preprocess_pil_image(screen), prompt)
        except Exception as e:
            return f"错误: 屏幕捕获失败: {str(e)}"
    return "错误: 未找到可用的截图或剪贴板图片。"


@mcp.tool()
@unload_after_task
def analyze_clipboard(custom_prompt: Optional[str] = None, allow_screen_fallback: bool = True, allow_latest_screenshot: bool = True, unload_after_task: bool = True) -> str:
    """
    分析当前剪贴板中的图片（截图后直接可用，无需保存文件或输入路径）。
    兜底：剪贴板无图时，可自动分析最新截图或捕获当前屏幕。

    Args:
        custom_prompt: 对图像的提问或分析指令，默认为"请详细描述这张图片的内容"
        allow_screen_fallback: 剪贴板无图片时，是否允许截取当前屏幕
        allow_latest_screenshot: 剪贴板无图片时，是否允许分析最新截图文件
        unload_after_task: True=任务结束后立即请求 Ollama 卸载模型（默认）；False=保持驻留，交给 Ollama keep_alive 自动释放

    Returns:
        图像分析结果
    """
    prompt = custom_prompt if custom_prompt else "请详细描述这张图片的内容"
    logger.info("收到剪贴板图像分析请求: prompt=%s...", prompt[:50])
    return _analyze_clipboard_or_screenshot(prompt, allow_screen=allow_screen_fallback, allow_latest=allow_latest_screenshot, prefer="clipboard")


@mcp.tool()
@unload_after_task
def analyze_latest_screenshot(custom_prompt: Optional[str] = None, allow_clipboard_fallback: bool = True, unload_after_task: bool = True) -> str:
    """
    分析最近一张截图文件（自动查找系统截图文件夹 / 桌面 / 临时目录中的最新图片），无需手动输入路径。

    Args:
        custom_prompt: 对图像的提问或分析指令，默认为"请详细描述这张图片的内容"
        allow_clipboard_fallback: 未找到截图文件时，是否回退到剪贴板图片
        unload_after_task: True=任务结束后立即请求 Ollama 卸载模型（默认）；False=保持驻留，交给 Ollama keep_alive 自动释放

    Returns:
        图像分析结果
    """
    prompt = custom_prompt if custom_prompt else "请详细描述这张图片的内容"
    logger.info("收到最新截图分析请求: prompt=%s...", prompt[:50])
    return _analyze_clipboard_or_screenshot(prompt, allow_screen=False, allow_latest=True, prefer="latest")


@mcp.tool()
@unload_after_task
def capture_screen(custom_prompt: Optional[str] = None, unload_after_task: bool = True) -> str:
    """
    捕获当前整个屏幕并立即分析，无需截图、保存或输入路径。

    Args:
        custom_prompt: 对图像的提问或分析指令，默认为"请详细描述这张图片的内容"
        unload_after_task: True=任务结束后立即请求 Ollama 卸载模型（默认）；False=保持驻留，交给 Ollama keep_alive 自动释放

    Returns:
        图像分析结果
    """
    prompt = custom_prompt if custom_prompt else "请详细描述这张图片的内容"
    logger.info("收到屏幕捕获分析请求: prompt=%s...", prompt[:50])
    try:
        screen = ImageGrab.grab()
        return call_ollama_vision(preprocess_pil_image(screen), prompt)
    except Exception as e:
        logger.error("屏幕捕获失败: %s", str(e), exc_info=True)
        return f"错误: 屏幕捕获失败: {str(e)}"


@mcp.tool()
def get_status() -> dict:
    """
    获取 MCP Server 状态信息

    Returns:
        包含服务器配置和状态的字典
    """
    # 检查 Ollama 连接
    ollama_available = False
    ollama_error = ""
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        ollama_available = response.status_code == 200
    except Exception as e:
        ollama_error = str(e)

    return {
        "ok": True,
        "version": "1.0.0",
        "ollama": {
            "available": ollama_available,
            "host": OLLAMA_HOST,
            "model": VISION_MODEL,
            "error": ollama_error
        },
        "max_image_size": MAX_IMAGE_SIZE,
        "max_image_dim": MAX_IMAGE_DIM,
        "timeout": OLLAMA_TIMEOUT,
        "num_ctx": OLLAMA_NUM_CTX
    }


if __name__ == "__main__":
    logger.info(f"启动 Qwen2.5-VL MCP Server")
    logger.info(f"Ollama 地址: {OLLAMA_HOST}")
    logger.info(f"视觉模型: {VISION_MODEL}")
    logger.info("MCP Server 已启动，等待 stdio 连接...")
    
    try:
        mcp.run()
    except KeyboardInterrupt:
        logger.info("MCP Server 已停止")
        sys.exit(0)
    except Exception as e:
        logger.error(f"MCP Server 启动失败: {str(e)}", exc_info=True)
        sys.exit(1)
