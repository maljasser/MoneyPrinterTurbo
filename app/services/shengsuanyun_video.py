"""胜算云统一视频生成接口。

该模块只负责胜算云 Router 的文生视频能力，不依赖 Streamlit，也不复用
LoomLoom Market 的报价和执行协议。模型目录、不同模型的最小请求参数、异步
轮询和产物下载集中维护在这里，避免 WebUI 与任务编排层散落 Provider 判断。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from loguru import logger


DEFAULT_BASE_URL = "https://router.shengsuanyun.com/api/v1"
DEFAULT_MODEL_ID = "google/veo3.1-fast-preview"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
MAX_VIDEO_SCENES = 5
MAX_VIDEO_ARTIFACT_BYTES = 512 * 1024 * 1024
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"})


class ShengsuanVideoError(RuntimeError):
    """胜算云视频生成错误基类。"""


class ShengsuanVideoConfigurationError(ShengsuanVideoError):
    """胜算云视频配置缺失或无效。"""


class ShengsuanVideoAPIError(ShengsuanVideoError):
    """胜算云接口请求失败或返回了无效数据。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ShengsuanVideoRunError(ShengsuanVideoError):
    """远端视频任务失败或等待超时。"""


@dataclass(frozen=True)
class ShengsuanVideoModel:
    """当前主流程支持的文生视频模型及其稳定默认参数。"""

    model_id: str
    display_name: str
    request_style: str
    duration_seconds: int
    resolution: str

    def build_payload(self, prompt: str, aspect_ratio: str) -> dict[str, Any]:
        """根据不同厂商 schema 生成最小请求，避免把分支暴露到调用层。"""
        base_payload: dict[str, Any] = {"model": self.model_id}
        if self.request_style == "seedance":
            return {
                **base_payload,
                "content": [{"type": "text", "text": prompt}],
                "duration": self.duration_seconds,
                "resolution": self.resolution,
                "ratio": aspect_ratio,
                "generate_audio": False,
                "watermark": False,
            }
        if self.request_style == "veo":
            return {
                **base_payload,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "duration_seconds": self.duration_seconds,
                "resolution": self.resolution,
                "generate_audio": False,
                "enhance_prompt": True,
                "sample_count": 1,
            }
        if self.request_style == "sora":
            return {
                **base_payload,
                "prompt": prompt,
                "seconds": str(self.duration_seconds),
                "size": "720x1280" if aspect_ratio == "9:16" else "1280x720",
            }
        if self.request_style == "minimax":
            return {
                **base_payload,
                "prompt": prompt,
                "duration": self.duration_seconds,
                "resolution": self.resolution,
                "prompt_optimizer": True,
                "aigc_watermark": False,
            }
        raise ShengsuanVideoConfigurationError(
            f"unsupported Shengsuan video request style: {self.request_style}"
        )


# 第一版只开放纯文本即可调用、并且支持项目现有 9:16/16:9 输出的模型。
# 图生视频、参考视频和视频编辑模型需要额外素材输入，不适合混入当前主流程。
VIDEO_MODELS = (
    ShengsuanVideoModel(
        model_id=DEFAULT_MODEL_ID,
        display_name="Google Veo 3.1 Fast",
        request_style="veo",
        duration_seconds=4,
        resolution="720p",
    ),
    ShengsuanVideoModel(
        model_id="bytedance/doubao-seedance-2-0-fast",
        display_name="ByteDance Seedance 2.0 Fast",
        request_style="seedance",
        duration_seconds=4,
        resolution="720p",
    ),
    ShengsuanVideoModel(
        model_id="bytedance/doubao-seedance-2-0",
        display_name="ByteDance Seedance 2.0",
        request_style="seedance",
        duration_seconds=4,
        resolution="720p",
    ),
    ShengsuanVideoModel(
        model_id="openai/sora2",
        display_name="OpenAI Sora 2",
        request_style="sora",
        duration_seconds=4,
        resolution="720p",
    ),
    ShengsuanVideoModel(
        model_id="google/veo3.1-preview",
        display_name="Google Veo 3.1",
        request_style="veo",
        duration_seconds=4,
        resolution="720p",
    ),
    ShengsuanVideoModel(
        model_id="minimax/t2v-01-director",
        display_name="MiniMax T2V-01 Director",
        request_style="minimax",
        duration_seconds=6,
        resolution="768P",
    ),
)
VIDEO_MODEL_BY_ID = {model.model_id: model for model in VIDEO_MODELS}


def get_video_model(model_id: str) -> ShengsuanVideoModel:
    """返回受支持模型；旧配置或无效值稳定回退到默认模型。"""
    return (
        VIDEO_MODEL_BY_ID.get(str(model_id or "").strip())
        or VIDEO_MODEL_BY_ID[DEFAULT_MODEL_ID]
    )


def require_video_model(model_id: str) -> ShengsuanVideoModel:
    """严格解析付费请求使用的模型，避免拼写错误时按默认模型扣费。"""
    normalized_model_id = str(model_id or "").strip()
    model = VIDEO_MODEL_BY_ID.get(normalized_model_id)
    if model is None:
        raise ShengsuanVideoConfigurationError(
            f"unsupported Shengsuan video model: {normalized_model_id or '<empty>'}"
        )
    return model


@dataclass(frozen=True)
class ShengsuanVideoSettings:
    base_url: str
    api_token: str = field(repr=False)
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ShengsuanVideoSettings":
        settings = cls(
            base_url=str(
                values.get("shengsuanyun_video_base_url", "") or DEFAULT_BASE_URL
            )
            .strip()
            .rstrip("/"),
            # 文案批处理和视频生成使用同一个胜算云账户，继续复用现有配置键，
            # 避免升级后要求用户重复输入或迁移 API Key。
            api_token=str(values.get("loomloom_api_token", "") or "").strip(),
            request_timeout_seconds=float(
                values.get(
                    "shengsuanyun_video_request_timeout_seconds",
                    DEFAULT_REQUEST_TIMEOUT_SECONDS,
                )
            ),
            poll_interval_seconds=float(
                values.get(
                    "shengsuanyun_video_poll_interval_seconds",
                    DEFAULT_POLL_INTERVAL_SECONDS,
                )
            ),
            run_timeout_seconds=float(
                values.get(
                    "shengsuanyun_video_run_timeout_seconds",
                    DEFAULT_RUN_TIMEOUT_SECONDS,
                )
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = []
        if not self.base_url:
            missing.append("shengsuanyun_video_base_url")
        if not self.api_token:
            missing.append("loomloom_api_token")
        if missing:
            raise ShengsuanVideoConfigurationError(
                "missing Shengsuan video settings: " + ", ".join(missing)
            )
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ShengsuanVideoConfigurationError(
                "shengsuanyun_video_base_url must be an absolute HTTP(S) URL"
            )
        for name, value in (
            (
                "shengsuanyun_video_request_timeout_seconds",
                self.request_timeout_seconds,
            ),
            ("shengsuanyun_video_poll_interval_seconds", self.poll_interval_seconds),
            ("shengsuanyun_video_run_timeout_seconds", self.run_timeout_seconds),
        ):
            if value <= 0:
                raise ShengsuanVideoConfigurationError(
                    f"{name} must be greater than zero"
                )


@dataclass(frozen=True)
class ShengsuanVideoBatch:
    model_id: str
    prompts: tuple[str, ...]
    aspect_ratio: str


@dataclass(frozen=True)
class ShengsuanConfirmedVideoRequest:
    """用户已在 WebUI 确认付费后交给后台线程的不可变请求。"""

    settings: ShengsuanVideoSettings
    batch: ShengsuanVideoBatch

    def validate(self) -> None:
        self.settings.validate()
        require_video_model(self.batch.model_id)
        if not self.batch.prompts:
            raise ValueError("Shengsuan video prompts are required")
        if self.batch.aspect_ratio not in {"9:16", "16:9"}:
            raise ValueError("aspect_ratio must be 9:16 or 16:9")


class ShengsuanVideoBackend:
    """提交、查询并下载胜算云统一异步视频任务。"""

    def __init__(
        self,
        settings: ShengsuanVideoSettings,
        *,
        session: requests.Session | Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        settings.validate()
        self.settings = settings
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock

    def prepare_batch(
        self,
        *,
        subject: str,
        scene_prompts: list[str] | tuple[str, ...],
        aspect_ratio: str,
        model_id: str,
    ) -> ShengsuanVideoBatch:
        normalized_subject = str(subject or "").strip()
        normalized_aspect_ratio = str(aspect_ratio or "").strip()
        if not normalized_subject:
            raise ValueError("subject is required")
        if normalized_aspect_ratio not in {"9:16", "16:9"}:
            raise ValueError("aspect_ratio must be 9:16 or 16:9")
        model = require_video_model(model_id)
        scenes = tuple(
            str(prompt or "").strip()
            for prompt in scene_prompts
            if str(prompt or "").strip()
        )
        if not 1 <= len(scenes) <= MAX_VIDEO_SCENES:
            raise ValueError(
                f"scene_prompts must contain between 1 and {MAX_VIDEO_SCENES} items"
            )
        prompts = tuple(
            "Create cinematic stock-footage-style video for a short video "
            f"about {normalized_subject}. Scene focus: {scene}. "
            "No text, subtitles, captions, watermarks, logos, or spoken audio."
            for scene in scenes
        )
        return ShengsuanVideoBatch(
            model_id=model.model_id,
            prompts=prompts,
            aspect_ratio=normalized_aspect_ratio,
        )

    def generate_and_download(
        self,
        batch: ShengsuanVideoBatch,
        destination_dir: str,
        *,
        on_request_submitted: Callable[[str], None] | None = None,
    ) -> tuple[str, ...]:
        """按场景生成素材；回调让任务状态及时保存可排障的远端请求 ID。"""
        model = require_video_model(batch.model_id)
        raw_destination = str(destination_dir or "").strip()
        if not raw_destination:
            raise ValueError("destination_dir is required")
        destination = os.path.realpath(raw_destination)
        os.makedirs(destination, exist_ok=True)

        downloaded = []
        for index, prompt in enumerate(batch.prompts, start=1):
            payload = model.build_payload(prompt, batch.aspect_ratio)
            request_id = self._submit(payload)
            if on_request_submitted is not None:
                on_request_submitted(request_id)
            result = self._wait_for_result(request_id)
            video_urls = self._video_urls(result)
            if len(video_urls) != 1:
                raise ShengsuanVideoRunError(
                    f"Shengsuan video request {request_id} returned "
                    f"{len(video_urls)} video URLs"
                )
            output = os.path.join(destination, f"shengsuanyun-video-{index:02d}.mp4")
            self._download(video_urls[0], output)
            downloaded.append(output)
        return tuple(downloaded)

    def _submit(self, payload: Mapping[str, Any]) -> str:
        # 视频提交可能已经在服务端计费，网络失败后不能自动重试，否则可能生成
        # 两个任务并重复扣费。查询请求可以重试，但 POST 只执行一次。
        response = self._request_json("POST", "/tasks/generations", payload=payload)
        data = self._response_data(response)
        request_id = str(data.get("request_id", "") or "").strip()
        if not request_id:
            raise ShengsuanVideoAPIError(
                "Shengsuan video response is missing request_id"
            )
        logger.info(
            "Shengsuan video request submitted: "
            f"request_id={request_id}, model={payload.get('model', '')}"
        )
        return request_id

    def _wait_for_result(self, request_id: str) -> Mapping[str, Any]:
        deadline = self._clock() + self.settings.run_timeout_seconds
        last_state: tuple[str, str] | None = None
        last_log_at = self._clock()
        consecutive_errors = 0
        while True:
            try:
                response = self._request_json(
                    "GET", f"/tasks/generations/{quote(request_id, safe='')}"
                )
                data = self._response_data(response)
                consecutive_errors = 0
            except ShengsuanVideoAPIError as exc:
                if not exc.retryable or self._clock() >= deadline:
                    raise
                consecutive_errors += 1
                delay = min(
                    self.settings.poll_interval_seconds
                    * (2 ** min(consecutive_errors - 1, 3)),
                    30.0,
                )
                logger.warning(
                    "retry Shengsuan video status query: "
                    f"request_id={request_id}, delay={delay:g}s, "
                    f"error={type(exc).__name__}"
                )
                self._sleep(delay)
                continue

            status = str(data.get("status", "") or "").strip().upper()
            progress = str(data.get("progress", "") or "").strip()
            state = (status, progress)
            now = self._clock()
            if state != last_state or now - last_log_at >= 30:
                logger.info(
                    "Shengsuan video progress: "
                    f"request_id={request_id}, status={status}, progress={progress}"
                )
                last_state = state
                last_log_at = now
            if status in TERMINAL_STATUSES:
                if status != "COMPLETED":
                    detail = str(data.get("fail_reason", "") or "").strip() or status
                    raise ShengsuanVideoRunError(
                        f"Shengsuan video request {request_id} ended with {detail}"
                    )
                return data
            if now >= deadline:
                raise ShengsuanVideoRunError(
                    f"Shengsuan video request {request_id} did not complete within "
                    f"{self.settings.run_timeout_seconds:g} seconds"
                )
            self._sleep(self.settings.poll_interval_seconds)

    @staticmethod
    def _response_data(response: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(response.get("code", "") or "").strip().lower() != "success":
            message = str(response.get("message", "") or "").strip()
            raise ShengsuanVideoAPIError(message or "Shengsuan video request failed")
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise ShengsuanVideoAPIError(
                "Shengsuan video response data must be an object"
            )
        return data

    @staticmethod
    def _video_urls(result: Mapping[str, Any]) -> tuple[str, ...]:
        output = result.get("data")
        if not isinstance(output, Mapping):
            raise ShengsuanVideoAPIError(
                "Shengsuan completed response is missing result data"
            )
        values = output.get("video_urls")
        if not isinstance(values, list):
            raise ShengsuanVideoAPIError(
                "Shengsuan completed response is missing video_urls"
            )
        urls = []
        for value in values:
            url = str(value or "").strip()
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ShengsuanVideoAPIError(
                    "Shengsuan video URL must be absolute HTTP(S)"
                )
            urls.append(url)
        return tuple(urls)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = self._session.request(
                method,
                f"{self.settings.base_url}{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.settings.api_token}",
                    "Content-Type": "application/json",
                },
                json=dict(payload) if payload is not None else None,
                timeout=(5.0, self.settings.request_timeout_seconds),
            )
        except requests.RequestException as exc:
            raise ShengsuanVideoAPIError(
                f"Shengsuan video request failed: {type(exc).__name__}",
                retryable=method.upper() == "GET",
            ) from exc
        if not 200 <= response.status_code < 300:
            message = "request rejected"
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, Mapping):
                message = str(
                    error_payload.get("message")
                    or error_payload.get("error")
                    or message
                ).strip()
            raise ShengsuanVideoAPIError(
                f"Shengsuan video API returned HTTP {response.status_code}: {message}",
                status_code=response.status_code,
                retryable=(
                    method.upper() == "GET"
                    and (
                        response.status_code in {408, 425, 429}
                        or response.status_code >= 500
                    )
                ),
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ShengsuanVideoAPIError(
                "Shengsuan video API returned invalid JSON"
            ) from exc
        if not isinstance(body, Mapping):
            raise ShengsuanVideoAPIError(
                "Shengsuan video API response must be an object"
            )
        return body

    def _download(self, url: str, destination: str) -> None:
        temporary = destination + ".part"
        downloaded_bytes = 0
        response = None
        try:
            response = self._session.get(
                url,
                stream=True,
                timeout=(5.0, self.settings.request_timeout_seconds),
            )
            response.raise_for_status()
            content_length = int(response.headers.get("content-length", 0) or 0)
            if content_length > MAX_VIDEO_ARTIFACT_BYTES:
                raise ShengsuanVideoAPIError(
                    "video artifact exceeds the download limit"
                )
            with open(temporary, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > MAX_VIDEO_ARTIFACT_BYTES:
                        raise ShengsuanVideoAPIError(
                            "video artifact exceeds the download limit"
                        )
                    output.write(chunk)
            if downloaded_bytes == 0:
                raise ShengsuanVideoAPIError("video artifact download was empty")
            os.replace(temporary, destination)
        except (requests.RequestException, OSError, ValueError) as exc:
            raise ShengsuanVideoAPIError(
                f"video artifact download failed: {type(exc).__name__}"
            ) from exc
        finally:
            close_response = getattr(response, "close", None)
            if callable(close_response):
                close_response()
            if os.path.exists(temporary):
                os.remove(temporary)
