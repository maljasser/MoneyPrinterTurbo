"""LoomLoom Market clients for MoneyPrinterTurbo batch generation.

This module deliberately lives outside ``llm_provider``. LoomLoom executes a
versioned Market SkillBot with quote, confirmation, run lifecycle, and result
rows; it is not a chat-completions provider.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from loguru import logger


DEFAULT_RESULT_PORT_NAME = "output"
DEFAULT_BASE_URL = "https://loomloom.shengsuanyun.com/loom/v1"
DEFAULT_SCRIPT_MARKET_LISTING_ID = "019fd618-9baa-73d9-94f4-c9270b6f3025"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_RUN_TIMEOUT_SECONDS = 600.0
MAX_EXECUTE_ATTEMPTS = 3
MAX_POLL_RETRY_DELAY_SECONDS = 30.0
MAX_SCRIPT_CANDIDATES = 10
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled"})


class LoomLoomError(RuntimeError):
    """Base error for the LoomLoom integration."""


class LoomLoomConfigurationError(LoomLoomError):
    """Raised when the integration is enabled without complete settings."""


class LoomLoomAPIError(LoomLoomError):
    """Raised when the Public API rejects a request or returns invalid JSON."""

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


class LoomLoomRunError(LoomLoomError):
    """Raised when a submitted run fails or exceeds its wait timeout."""


def resolve_api_token(values: Mapping[str, Any]) -> str:
    """读取 WebUI/config.toml 中的胜算云 API Key。"""
    return str(values.get("loomloom_api_token", "") or "").strip()


@dataclass(frozen=True)
class LoomLoomSettings:
    base_url: str
    api_token: str = field(repr=False)
    market_listing_id: str
    listing_version_id: str = ""
    result_port_name: str = DEFAULT_RESULT_PORT_NAME
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
    ) -> "LoomLoomSettings":
        settings = cls(
            base_url=str(values.get("loomloom_base_url", "") or DEFAULT_BASE_URL)
            .strip()
            .rstrip("/"),
            api_token=resolve_api_token(values),
            market_listing_id=str(
                values.get("loomloom_market_listing_id", "")
                or DEFAULT_SCRIPT_MARKET_LISTING_ID
            ).strip(),
            listing_version_id=str(
                values.get("loomloom_market_listing_version_id", "")
            ).strip(),
            result_port_name=str(
                values.get("loomloom_result_port_name", DEFAULT_RESULT_PORT_NAME)
            ).strip(),
            request_timeout_seconds=float(
                values.get(
                    "loomloom_request_timeout_seconds",
                    DEFAULT_REQUEST_TIMEOUT_SECONDS,
                )
            ),
            poll_interval_seconds=float(
                values.get(
                    "loomloom_poll_interval_seconds",
                    DEFAULT_POLL_INTERVAL_SECONDS,
                )
            ),
            run_timeout_seconds=float(
                values.get("loomloom_run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS)
            ),
        )
        settings.validate(require_api_token=True)
        return settings

    def validate(self, *, require_api_token: bool = True) -> None:
        missing = []
        if not self.base_url:
            missing.append("loomloom_base_url")
        if require_api_token and not self.api_token:
            missing.append("loomloom_api_token")
        if not self.market_listing_id:
            missing.append("loomloom_market_listing_id")
        if not self.result_port_name:
            missing.append("loomloom_result_port_name")
        if missing:
            raise LoomLoomConfigurationError(
                "missing LoomLoom settings: " + ", ".join(missing)
            )

        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LoomLoomConfigurationError(
                "loomloom_base_url must be an absolute HTTP(S) URL"
            )
        for name, value in (
            ("loomloom_request_timeout_seconds", self.request_timeout_seconds),
            ("loomloom_poll_interval_seconds", self.poll_interval_seconds),
            ("loomloom_run_timeout_seconds", self.run_timeout_seconds),
        ):
            if value <= 0:
                raise LoomLoomConfigurationError(f"{name} must be greater than zero")


@dataclass(frozen=True)
class LoomLoomScriptBatch:
    input_rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class LoomLoomQuote:
    quote_id: str
    listing_version_id: str
    currency: str
    task_count: int
    estimated_buyer_payable_t: int
    estimated_buyer_payable_amount: str
    input_rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class LoomLoomExecution:
    run_id: str
    transaction_id: str
    transaction_status: str
    listing_version_id: str


@dataclass(frozen=True)
class LoomLoomRun:
    run_id: str
    status: str
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    cancelled_tasks: int
    first_error_message: str


@dataclass(frozen=True)
class LoomLoomScriptCandidate:
    row_index: int
    script: str
    video_terms: tuple[str, ...]


@dataclass(frozen=True)
class LoomLoomCandidateError:
    row_index: int
    message: str


@dataclass(frozen=True)
class LoomLoomScriptBatchResult:
    candidates: tuple[LoomLoomScriptCandidate, ...]
    errors: tuple[LoomLoomCandidateError, ...]


class LoomLoomScriptBackend:
    """Execute one configured LoomLoom Market Listing for script candidates."""

    def __init__(
        self,
        settings: LoomLoomSettings,
        *,
        session: requests.Session | Any | None = None,
        credential_provider: Callable[[], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        settings.validate(require_api_token=credential_provider is None)
        self.settings = settings
        self._session = session or requests.Session()
        self._credential_provider = credential_provider or (lambda: settings.api_token)
        self._sleep = sleep
        self._clock = clock

    def prepare_script_batch(
        self,
        *,
        subject: str,
        candidate_count: int,
        language: str = "auto",
        duration_seconds: int = 60,
        style: str = "",
    ) -> LoomLoomScriptBatch:
        normalized_subject = str(subject or "").strip()
        if not normalized_subject:
            raise ValueError("subject is required")
        if not 1 <= candidate_count <= MAX_SCRIPT_CANDIDATES:
            raise ValueError(
                f"candidate_count must be between 1 and {MAX_SCRIPT_CANDIDATES}"
            )
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")

        requirements = (
            f"输出语言：{str(language or 'auto').strip() or 'auto'}\n"
            f"目标时长（秒）：{duration_seconds}"
        )
        normalized_style = str(style or "").strip()
        if normalized_style:
            requirements += f"\n风格或附加要求：{normalized_style}"

        rows = tuple(
            {
                "subject": normalized_subject,
                "requirements": requirements,
                "candidateIndex": str(index),
            }
            for index in range(1, candidate_count + 1)
        )
        return LoomLoomScriptBatch(input_rows=rows)

    def quote(self, batch: LoomLoomScriptBatch) -> LoomLoomQuote:
        payload = self._listing_payload(batch)
        response = self._request(
            "POST",
            f"/marketListings/{quote(self.settings.market_listing_id, safe='')}:quote",
            json_body=payload,
        )
        estimated_buyer_payable = response.get("estimatedBuyerPayable", {})
        if not isinstance(estimated_buyer_payable, dict):
            estimated_buyer_payable = {}
        return LoomLoomQuote(
            quote_id=self._required_string(response, "quoteId"),
            listing_version_id=self._required_string(response, "listingVersionId"),
            currency=str(response.get("currency", "")).strip(),
            task_count=self._integer(response, "taskCount"),
            estimated_buyer_payable_t=self._integer(response, "estimatedBuyerPayableT"),
            estimated_buyer_payable_amount=str(
                estimated_buyer_payable.get("amount", "")
            ).strip(),
            input_rows=batch.input_rows,
        )

    def execute(
        self,
        batch: LoomLoomScriptBatch,
        *,
        client_request_id: str,
        listing_version_id: str,
        confirm: bool,
    ) -> LoomLoomExecution:
        if confirm is not True:
            raise ValueError("confirm=True is required before a paid LoomLoom run")
        normalized_request_id = str(client_request_id or "").strip()
        if not normalized_request_id:
            raise ValueError("client_request_id is required")
        normalized_listing_version_id = str(listing_version_id or "").strip()
        if not normalized_listing_version_id:
            raise ValueError("listing_version_id from the quote is required")

        payload = self._listing_payload(
            batch, listing_version_id=normalized_listing_version_id
        )
        payload.update(
            {
                "clientRequestId": normalized_request_id,
                "confirm": True,
            }
        )
        response = None
        for attempt in range(1, MAX_EXECUTE_ATTEMPTS + 1):
            try:
                response = self._request(
                    "POST",
                    f"/marketListings/{quote(self.settings.market_listing_id, safe='')}:execute",
                    json_body=payload,
                )
                break
            except LoomLoomAPIError as exc:
                if not exc.retryable or attempt >= MAX_EXECUTE_ATTEMPTS:
                    raise
                # execute 是付费操作，不能生成新的请求 ID 后盲目重试。服务端以
                # clientRequestId 保证幂等，因此这里只复用完全相同的载荷做有限
                # 重试，用于恢复“服务端已接受、客户端未收到响应”的网络故障。
                retry_delay = min(float(attempt), MAX_POLL_RETRY_DELAY_SECONDS)
                logger.warning(
                    "retry LoomLoom execute with the same client request id: "
                    f"attempt={attempt + 1}/{MAX_EXECUTE_ATTEMPTS}, "
                    f"error={type(exc).__name__}"
                )
                self._sleep(retry_delay)

        if response is None:  # pragma: no cover - 循环的成功或异常分支已覆盖
            raise LoomLoomAPIError("LoomLoom execute returned no response")
        return LoomLoomExecution(
            run_id=self._required_string(response, "runId"),
            transaction_id=str(response.get("runTransactionId", "")).strip(),
            transaction_status=str(response.get("transactionStatus", "")).strip(),
            listing_version_id=str(response.get("listingVersionId", "")).strip(),
        )

    def get_run(self, run_id: str) -> LoomLoomRun:
        normalized_run_id = self._required_identifier(run_id, "run_id")
        response = self._request(
            "GET", f"/users/me/runs/{quote(normalized_run_id, safe='')}"
        )
        run = response.get("run")
        if not isinstance(run, dict):
            raise LoomLoomAPIError("LoomLoom run response is missing run")
        return LoomLoomRun(
            run_id=self._required_string(run, "runId"),
            status=self._required_string(run, "status").lower(),
            total_tasks=self._integer(run, "totalTasks"),
            completed_tasks=self._integer(run, "completedTasks"),
            failed_tasks=self._integer(run, "failedTasks"),
            cancelled_tasks=self._integer(run, "cancelledTasks"),
            first_error_message=str(run.get("firstErrorMessage", "")).strip(),
        )

    def wait_for_run(self, run_id: str) -> LoomLoomRun:
        deadline = self._clock() + self.settings.run_timeout_seconds
        started_at = self._clock()
        last_logged_status = None
        last_progress_log_at = started_at
        consecutive_poll_errors = 0
        while True:
            try:
                run = self.get_run(run_id)
                consecutive_poll_errors = 0
            except LoomLoomAPIError as exc:
                if not exc.retryable:
                    raise
                now = self._clock()
                if now >= deadline:
                    raise LoomLoomRunError(
                        f"LoomLoom run {run_id} could not be queried within "
                        f"{self.settings.run_timeout_seconds:g} seconds"
                    ) from exc
                consecutive_poll_errors += 1
                retry_delay = min(
                    self.settings.poll_interval_seconds
                    * (2 ** min(consecutive_poll_errors - 1, 4)),
                    MAX_POLL_RETRY_DELAY_SECONDS,
                    max(deadline - now, 0),
                )
                logger.warning(
                    "retry LoomLoom run polling after a transient error: "
                    f"run_id={run_id}, attempt={consecutive_poll_errors}, "
                    f"delay={retry_delay:g}s, error={type(exc).__name__}"
                )
                self._sleep(retry_delay)
                continue
            now = self._clock()
            progress = run.completed_tasks + run.failed_tasks + run.cancelled_tasks
            # 远端视频任务可能持续数分钟。状态变化时立即记录，状态不变时每
            # 30 秒记录一次心跳，既能帮助定位卡住位置，也避免两秒一次刷屏。
            if run.status != last_logged_status or now - last_progress_log_at >= 30:
                logger.info(
                    "LoomLoom run progress: "
                    f"run_id={run.run_id}, status={run.status}, "
                    f"finished={progress}/{run.total_tasks}"
                )
                last_logged_status = run.status
                last_progress_log_at = now
            if run.status in TERMINAL_RUN_STATUSES:
                if run.status != "completed":
                    detail = run.first_error_message or run.status
                    raise LoomLoomRunError(
                        f"LoomLoom run {run.run_id} ended with {detail}"
                    )
                return run
            if now >= deadline:
                raise LoomLoomRunError(
                    f"LoomLoom run {run.run_id} did not complete within "
                    f"{self.settings.run_timeout_seconds:g} seconds"
                )
            self._sleep(self.settings.poll_interval_seconds)

    def get_script_results(self, run_id: str) -> LoomLoomScriptBatchResult:
        normalized_run_id = self._required_identifier(run_id, "run_id")
        rows = self._list_all_result_rows(normalized_run_id)
        candidates = []
        errors = []
        for row in rows:
            row_index = self._integer(row, "rowIndex")
            status = str(row.get("status", "")).strip().lower()
            if status != "completed":
                errors.append(
                    LoomLoomCandidateError(
                        row_index=row_index,
                        message=str(row.get("errorMessage", "")).strip()
                        or f"row ended with status {status or 'unknown'}",
                    )
                )
                continue
            try:
                candidates.append(self._parse_candidate(row_index, row))
            except (LoomLoomAPIError, ValueError) as exc:
                errors.append(
                    LoomLoomCandidateError(row_index=row_index, message=str(exc))
                )
        return LoomLoomScriptBatchResult(
            candidates=tuple(candidates), errors=tuple(errors)
        )

    def _listing_payload(
        self,
        batch: LoomLoomScriptBatch,
        *,
        listing_version_id: str | None = None,
    ) -> dict[str, Any]:
        if not batch.input_rows:
            raise ValueError("input_rows is required")
        payload: dict[str, Any] = {"inputRows": [dict(row) for row in batch.input_rows]}
        resolved_listing_version_id = (
            self.settings.listing_version_id
            if listing_version_id is None
            else listing_version_id
        )
        if resolved_listing_version_id:
            payload["listingVersionId"] = resolved_listing_version_id
        return payload

    def _list_all_result_rows(self, run_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"pageSize": 200}
            if page_token:
                params["pageToken"] = page_token
            response = self._request(
                "GET",
                f"/users/me/runs/{quote(run_id, safe='')}/resultRows",
                params=params,
            )
            items = response.get("items", [])
            if not isinstance(items, list) or not all(
                isinstance(item, dict) for item in items
            ):
                raise LoomLoomAPIError("LoomLoom resultRows items must be objects")
            rows.extend(items)
            page_token = str(response.get("nextPageToken", "")).strip()
            if not page_token:
                return rows

    def _parse_candidate(
        self, row_index: int, row: Mapping[str, Any]
    ) -> LoomLoomScriptCandidate:
        artifacts = row.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise LoomLoomAPIError("row artifacts must be a list")
        matching = [
            artifact
            for artifact in artifacts
            if isinstance(artifact, dict)
            and str(artifact.get("portName", "")).strip()
            == self.settings.result_port_name
        ]
        if len(matching) != 1:
            raise LoomLoomAPIError(
                f"expected one {self.settings.result_port_name!r} result artifact, "
                f"got {len(matching)}"
            )
        inline_text = str(matching[0].get("inlineText", "")).strip()
        if not inline_text:
            raise LoomLoomAPIError("result artifact does not contain inlineText")
        lines = inline_text.splitlines()
        if (
            len(lines) >= 3
            and lines[0].strip().lower() in {"```", "```json"}
            and lines[-1].strip() == "```"
        ):
            inline_text = "\n".join(lines[1:-1]).strip()
        try:
            value = json.loads(inline_text)
        except json.JSONDecodeError as exc:
            raise LoomLoomAPIError("result artifact is not valid JSON") from exc
        if not isinstance(value, dict):
            raise LoomLoomAPIError("result artifact JSON must be an object")

        script = str(value.get("script", "")).strip()
        if not script:
            raise LoomLoomAPIError("result artifact script is required")
        video_terms = value.get("videoTerms")
        if not isinstance(video_terms, list) or not video_terms:
            raise LoomLoomAPIError(
                "result artifact videoTerms must be a non-empty list"
            )
        normalized_terms = tuple(
            str(term).strip() for term in video_terms if str(term).strip()
        )
        if not normalized_terms:
            raise LoomLoomAPIError("result artifact videoTerms must not be empty")
        return LoomLoomScriptCandidate(
            row_index=row_index,
            script=script,
            video_terms=normalized_terms,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.base_url}{path}"
        api_token = str(self._credential_provider() or "").strip()
        if not api_token:
            raise LoomLoomConfigurationError(
                "a LoomLoom credential is required for this request"
            )
        try:
            response = self._session.request(
                method,
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                json=dict(json_body) if json_body is not None else None,
                params=dict(params) if params is not None else None,
                timeout=(5.0, self.settings.request_timeout_seconds),
            )
        except requests.RequestException as exc:
            raise LoomLoomAPIError(
                f"LoomLoom request failed: {type(exc).__name__}",
                retryable=True,
            ) from exc

        if not 200 <= response.status_code < 300:
            message = "request rejected"
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, dict):
                server_error = str(error_payload.get("error", "")).strip()
                if server_error:
                    message = server_error
            raise LoomLoomAPIError(
                f"LoomLoom API returned HTTP {response.status_code}: {message}",
                status_code=response.status_code,
                retryable=(
                    response.status_code in {408, 425, 429}
                    or response.status_code >= 500
                ),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LoomLoomAPIError("LoomLoom API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LoomLoomAPIError("LoomLoom API response must be a JSON object")
        return payload

    @staticmethod
    def _required_identifier(value: str, name: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} is required")
        return normalized

    @staticmethod
    def _required_string(values: Mapping[str, Any], name: str) -> str:
        value = str(values.get(name, "")).strip()
        if not value:
            raise LoomLoomAPIError(f"LoomLoom response is missing {name}")
        return value

    @staticmethod
    def _integer(values: Mapping[str, Any], name: str) -> int:
        value = values.get(name, 0)
        if isinstance(value, bool):
            raise LoomLoomAPIError(f"LoomLoom response {name} must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise LoomLoomAPIError(
                f"LoomLoom response {name} must be an integer"
            ) from exc
