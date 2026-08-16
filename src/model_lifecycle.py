#!/usr/bin/env python3
"""
Ollama 模型生命周期工具：请求式（graceful）卸载模型。

背景
----
Ollama 默认在模型任务结束后继续让模型驻留内存/显存（keep_alive 默认 5m），
在 "Vision Worker -> Document Worker -> Vision Worker" 串行切换场景下，
切换时延主要来自旧模型迟迟不自动释放。

机制
----
本模块使用 Ollama 官方文档 "Unload a model" 记载的机制：

    POST /api/generate
    {"model": "<model>", "keep_alive": 0}

语义：
- keep_alive=0 表示"本次请求完成后立即卸载该模型"，由 Ollama 调度器正常
  释放（graceful unload）。不是强制杀进程、不是重启服务、不删除模型文件，
  也不影响 Ollama 服务本身和其他模型。
- 收到 HTTP 200 只代表卸载请求已被接受并开始执行；显存/内存是否已物理
  释放由 Ollama 异步完成。本函数不阻塞等待确认，避免拖慢 Worker 返回。

设计约束
--------
- best-effort：所有异常只记录日志并返回结果字典，绝不抛出，确保 Worker
  的统一清理阶段不会反过来影响任务结果。
- 只针对单个模型发起卸载请求，不触碰其他模型与 Ollama 服务。
"""

import logging
import os
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("model-lifecycle")

DEFAULT_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
UNLOAD_REQUEST_TIMEOUT = float(os.getenv("UNLOAD_REQUEST_TIMEOUT", "10"))
PS_TIMEOUT = float(os.getenv("UNLOAD_PS_TIMEOUT", "3"))


def _normalize_model_name(name: str) -> str:
    """'xxx:latest' 归一化为 'xxx'，便于与 /api/ps 返回的模型名比较。"""
    name = (name or "").strip()
    if name.endswith(":latest"):
        return name[: -len(":latest")]
    return name


def list_loaded_models(host: str = DEFAULT_HOST, timeout: float = PS_TIMEOUT) -> Optional[List[str]]:
    """查询当前驻留在内存/显存中的模型名列表（GET /api/ps）。

    查询失败时返回 None（区别于"确实没有任何模型驻留"的空列表）。
    """
    try:
        resp = requests.get(f"{host}/api/ps", timeout=timeout)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except requests.exceptions.RequestException as exc:
        logger.warning("查询 /api/ps 失败（host=%s）: %s", host, exc)
        return None


def is_model_loaded(model_name: str, host: str = DEFAULT_HOST, timeout: float = PS_TIMEOUT) -> bool:
    """判断指定模型当前是否驻留。ps 查询失败时保守返回 False。"""
    loaded = list_loaded_models(host=host, timeout=timeout)
    if loaded is None:
        return False
    target = _normalize_model_name(model_name)
    return any(_normalize_model_name(name) == target for name in loaded)


def unload_model(
    model_name: str,
    host: str = DEFAULT_HOST,
    timeout: float = UNLOAD_REQUEST_TIMEOUT,
) -> Dict[str, object]:
    """请求 Ollama 尽快卸载指定模型（官方 keep_alive=0 的 graceful unload）。

    Args:
        model_name: 模型名，如 "qwen3:4b" / "qwen2.5vl:7b"。
        host: Ollama 服务地址，默认取 OLLAMA_HOST。
        timeout: 卸载请求的 HTTP 超时（秒）。

    Returns:
        {
            "requested": bool,   # 是否发出了卸载请求
            "model": str,
            "detail": str,       # not_loaded / unload_requested / ps_check_failed / error:<...>
            "elapsed_ms": int,   # 本函数耗时（信息用）
        }

    行为：
    - 模型未驻留：直接返回 requested=False / detail="not_loaded"，不发出任何请求
      （避免空 prompt 的 generate 请求先把模型加载起来再卸载，白白浪费时间）。
    - 模型驻留：POST /api/generate {"model": ..., "keep_alive": 0, "stream": false}。
      HTTP 200 即视为卸载请求已生效；立即返回，不做确认轮询。
    - /api/ps 查询失败：保守跳过卸载请求（避免盲目发空 prompt 请求），返回
      detail="ps_check_failed"。
    - 任何失败：只记录日志并返回 requested=False，绝不抛出异常。
    """
    t0 = time.monotonic()
    result = {"requested": False, "model": model_name, "detail": "", "elapsed_ms": 0}

    # 1) 先确认模型当前是否驻留；ps 查询失败时保守跳过
    loaded = list_loaded_models(host=host)
    if loaded is None:
        result["detail"] = "ps_check_failed"
        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        logger.warning("跳过卸载请求：无法确认模型 %s 是否驻留（/api/ps 失败）", model_name)
        return result

    target = _normalize_model_name(model_name)
    if not any(_normalize_model_name(name) == target for name in loaded):
        result["detail"] = "not_loaded"
        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        logger.info("模型 %s 未驻留，无需卸载", model_name)
        return result

    # 2) 官方 graceful unload：空 prompt + keep_alive=0
    payload = {"model": model_name, "keep_alive": 0, "stream": False}
    try:
        resp = requests.post(f"{host}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
        result["requested"] = True
        result["detail"] = "unload_requested"
        logger.info(
            "已请求 Ollama 卸载模型 %s（keep_alive=0；该请求完成后 Ollama 将立即卸载）",
            model_name,
        )
    except requests.exceptions.RequestException as exc:
        result["detail"] = f"error: {exc}"
        logger.warning("请求卸载模型 %s 失败（不影响 Ollama 服务本身）: %s", model_name, exc)
    finally:
        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return result
