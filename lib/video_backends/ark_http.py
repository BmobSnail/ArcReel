"""ArkHttpVideoBackend — 火山方舟 / 代理视频生成后端（纯 httpx，不依赖 Ark SDK）。

与 ArkVideoBackend 功能对等，但直接用 httpx 构造请求体——避免 Ark SDK
把所有未传可选字段序列化为 JSON ``null`` 导致代理崩溃的问题。
同时兼容官方火山 API 与 llm-api.net 等中转站。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from lib.aspect_size import VIDEO_TIER_SHORT_EDGE, aspect_size, resolution_to_short_edge
from lib.logging_utils import format_kwargs_for_log
from lib.providers import PROVIDER_ARK
from lib.retry import (
    DOWNLOAD_BACKOFF_SECONDS,
    DOWNLOAD_MAX_ATTEMPTS,
    with_retry_async,
)
from lib.video_backends.base import (
    ProviderJobIdPersistenceMixin,
    VideoCapabilities,
    VideoCapability,
    VideoGenerationRequest,
    VideoGenerationResult,
    download_video,
    poll_with_retry,
    should_retry_poll,
    should_retry_submit,
    submit_post,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "doubao-seedance-1-5-pro-251215"

_POLL_INTERVAL_SECONDS = 10.0
_MIN_POLL_TIMEOUT_SECONDS = 600
_POLL_TIMEOUT_PER_SECOND = 60

_VIDEO_ROUND_TO = 8


def _resolve_size(resolution: str | None, aspect_ratio: str) -> tuple[int, int]:
    short = resolution_to_short_edge(resolution, tier_map=VIDEO_TIER_SHORT_EDGE)
    return aspect_size(aspect_ratio, short, round_to=_VIDEO_ROUND_TO)


def _build_content(request: VideoGenerationRequest) -> list[dict]:
    """构造 content 列表，仅包含非空字段。"""
    content: list[dict] = [{"type": "text", "text": request.prompt}]

    from lib.image_backends.base import image_to_base64_data_uri

    if request.start_image and Path(request.start_image).exists():
        content.append({
            "type": "image_url",
            "image_url": {"url": image_to_base64_data_uri(request.start_image)},
            "role": "first_frame",
        })

    if request.end_image and Path(request.end_image).exists():
        content.append({
            "type": "image_url",
            "image_url": {"url": image_to_base64_data_uri(request.end_image)},
            "role": "last_frame",
        })

    if request.reference_images:
        for ref in request.reference_images:
            p = Path(ref) if not isinstance(ref, Path) else ref
            if p.exists():
                content.append({
                    "type": "image_url",
                    "image_url": {"url": image_to_base64_data_uri(p)},
                    "role": "reference_image",
                })

    return content


class ArkHttpVideoBackend(ProviderJobIdPersistenceMixin):
    """火山方舟视频生成后端 — 纯 httpx 实现，不依赖 Ark SDK。

    兼容官方火山 API (ark.cn-beijing.volces.com) 与 llm-api.net 等中转站。
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str | None = None,
        http_timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("ArkHttpVideoBackend 需要 api_key")
        if not base_url:
            raise ValueError("ArkHttpVideoBackend 需要 base_url")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model or DEFAULT_MODEL
        self._http_timeout = http_timeout

    @property
    def name(self) -> str:
        return PROVIDER_ARK

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[VideoCapability]:
        return {
            VideoCapability.TEXT_TO_VIDEO,
            VideoCapability.IMAGE_TO_VIDEO,
        }

    @staticmethod
    def video_capabilities_for_model(model: str) -> VideoCapabilities:
        """同 ArkVideoBackend 的能力声明——兼容 resolver 的纯函数接口。"""
        model_lower = model.lower()
        is_sd2 = "seedance-2-0" in model_lower or "seedance-2.0" in model_lower
        if is_sd2:
            return VideoCapabilities(last_frame=True, reference_images=True, max_reference_images=9)
        return VideoCapabilities()

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return self.video_capabilities_for_model(self._model)

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        provider_task_id = await self._create_task(request)
        await self._persist_provider_job_id(request, provider_task_id, provider=PROVIDER_ARK)
        return await self._poll_until_done(provider_task_id, request)

    @with_retry_async(retry_if=should_retry_submit)
    async def _create_task(self, request: VideoGenerationRequest) -> str:
        """提交视频生成任务，返回 task_id。使用 submit_post 防止网络歧义导致重复提交。"""
        payload: dict = {
            "model": self._model,
            "content": _build_content(request),
            "ratio": request.aspect_ratio,
            "duration": request.duration_seconds,
        }

        if request.resolution is not None:
            payload["resolution"] = request.resolution

        if request.seed is not None:
            payload["seed"] = request.seed

        if request.generate_audio:
            payload["generate_audio"] = True

        logger.info(
            "ArkHttp 视频生成开始: model=%s duration=%s ratio=%s",
            self._model, request.duration_seconds, request.aspect_ratio,
        )
        logger.info("调用 %s 视频 API payload=%s", self.name, format_kwargs_for_log(payload))

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            resp = await submit_post(
                lambda: client.post(
                    f"{self._base_url}/contents/generations/tasks",
                    json=payload,
                    headers=self._headers(),
                ),
                provider=PROVIDER_ARK,
            )
            body = resp.json()
            task_id = body.get("id")
            if not task_id:
                raise RuntimeError(f"ArkHttp 创建任务返回体缺少 id: {body}")
            logger.info("ArkHttp 任务创建: task_id=%s", task_id)
            return task_id

    async def _poll_until_done(self, task_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        """轮询任务状态，完成后下载视频。"""
        async def _poll() -> dict:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(
                    f"{self._base_url}/contents/generations/tasks/{task_id}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json()

        final = await poll_with_retry(
            poll_fn=_poll,
            is_done=lambda state: state.get("status") in ("succeeded", "failed", "expired"),
            is_failed=lambda state: (
                f"ArkHttp 视频生成失败(status={state.get('status')}): "
                f"{state.get('error') or 'Unknown error'}"
                if state.get("status") in ("failed", "expired")
                else None
            ),
            poll_interval=_POLL_INTERVAL_SECONDS,
            max_wait=max(_MIN_POLL_TIMEOUT_SECONDS, request.duration_seconds * _POLL_TIMEOUT_PER_SECOND),
            label="ArkHttp",
            on_progress=lambda state, elapsed: logger.info(
                "ArkHttp 视频生成中... status=%s elapsed=%ds", state.get("status"), int(elapsed)
            ),
        )

        content = final.get("content") or {}
        video_url = content.get("video_url")
        if not video_url:
            raise RuntimeError(f"ArkHttp 任务完成但缺少 content.video_url: {final}")

        await self._download_with_retry(video_url, request.output_path)

        usage_tokens = (final.get("usage") or {}).get("completion_tokens")
        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_ARK,
            model=self._model,
            duration_seconds=request.duration_seconds,
            video_uri=video_url,
            task_id=task_id,
            seed=final.get("seed"),
            usage_tokens=usage_tokens,
        )

    @staticmethod
    @with_retry_async(
        max_attempts=DOWNLOAD_MAX_ATTEMPTS,
        backoff_seconds=DOWNLOAD_BACKOFF_SECONDS,
        retry_if=should_retry_poll,
    )
    async def _download_with_retry(video_url: str, output_path: Path) -> None:
        await download_video(video_url, output_path)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
