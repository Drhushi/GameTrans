"""OpenAI 兼容 provider。

只要服务端讲 ``POST {base_url}/chat/completions``，就能用这个 provider ——
火山方舟、DeepSeek、本地 vLLM / Ollama 都在此列。换模型只需改配置，不必改代码。

传输层可注入（``transport``），所以请求体构造与响应解析都能离线测试。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from gametrans import prompts
from gametrans.config import REQUEST_OVERRIDE_RESERVED, REQUEST_OVERRIDES_ENV
from gametrans.credentials import normalize_base_url
from gametrans.errors import ConfigError, ProviderError
from gametrans.providers.base import (
    CallRecord,
    DeclaredTerm,
    LLMProvider,
    TranslationRequest,
    TranslationResult,
)

Transport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


def _strip_code_fence(text: str) -> str:
    """模型很爱把 JSON 包在 ``` 里，这里容错一下。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _as_int(value: Any) -> int | None:
    """usage 里的计数。拿不到就 ``None`` —— 不拿 0 或估算值冒充真实用量。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """服务端自报的成本之类的浮点量。同样：拿不到就是 ``None``。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _checked_overrides(value: Any, *, source: str) -> dict[str, Any]:
    """把"请求参数覆写"检查成一份能用的字典。

    两条规矩，都是为了让"配了但没生效"和"悄悄改了别的东西"变成可查的失败：

    * 不是对象就**报错**（不是静默忽略）—— 静默忽略等于配了没用，最难查；
    * 有主的键（:data:`~gametrans.config.REQUEST_OVERRIDE_RESERVED`）**报错**：
      ``model`` 归凭证层、``messages`` 归翻译层装配、``stream`` 归协议。
    """
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"{source} 的请求参数覆写必须是一个对象（键值对），"
            f"实际是 {type(value).__name__}",
            hint='写成 {"max_tokens": 384000} 这样的对象，或去掉这一项。',
        )
    for key in value:
        if str(key) in REQUEST_OVERRIDE_RESERVED:
            raise ConfigError(
                f"{source} 的请求参数覆写里不许改 {key!r}：它有主",
                hint=(
                    f"不许覆写的是：{', '.join(REQUEST_OVERRIDE_RESERVED)}。"
                    "模型名请在凭证里配（model / GAMETRANS_MODEL），"
                    "提示词由翻译层装配，stream 由协议决定。"
                ),
            )
    return dict(value)


def _overrides_from_env() -> dict[str, Any]:
    """环境变量层的覆写（``GAMETRANS_REQUEST_JSON``，JSON 对象）。解析不了就报错。"""
    raw = os.environ.get(REQUEST_OVERRIDES_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{REQUEST_OVERRIDES_ENV} 不是合法 JSON：{exc.msg}",
            hint='写成一个对象，例如 {"max_tokens": 384000}。',
        ) from None
    return _checked_overrides(parsed, source=REQUEST_OVERRIDES_ENV)


def parse_answer_text(text: str, *, provider: str) -> list[TranslationResult]:
    """把一份**纯文本答案**解析成结果 —— 响应格式只有这一份实现。

    openai 兼容通道从响应信封里剥出正文后走它；agent 挂单通道拿到的答案本身就是
    正文，也走它。两条运输方式，一份格式 —— 译文该长什么样不随运输方式变。
    """
    text = _strip_code_fence(str(text or ""))
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"答案不是合法 JSON：{exc.msg}",
            hint="模型可能没有遵守输出格式；可换更听话的模型，或在翻译要求里强调只输出 JSON。",
        ) from None

    rows = parsed.get("translations") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise ProviderError(
            "答案缺少 translations 数组",
            hint='期望结构：{"translations": [{"unit_id": "...", "target": "..."}]}',
        )

    # 模型**自己申报**的本批实体（原文写法 → 译名 / 设定）。这一项是可选的：
    # 老模型/老提示词不回它，回填照旧；回了就带上，由翻译层落成候选。
    # 两栏都可以空，空的那一栏就当没申报 —— 这就是术语书里的一行。
    declared: list[DeclaredTerm] = []
    raw_terms = parsed.get("terms") if isinstance(parsed, dict) else None
    if isinstance(raw_terms, list):
        for row in raw_terms:
            if not isinstance(row, dict):
                continue
            source = str(row.get("source") or "").strip()
            target = str(row.get("target") or "").strip()
            profile = str(row.get("profile") or "").strip()
            if source and (target or profile):
                declared.append(
                    DeclaredTerm(source=source, target=target, profile=profile)
                )

    results: list[TranslationResult] = []
    for row in rows:
        if not isinstance(row, dict) or "unit_id" not in row:
            continue
        results.append(
            TranslationResult(
                unit_id=str(row["unit_id"]),
                target=str(row.get("target", "")),
                provider=provider,
                declared_terms=tuple(declared),
            )
        )
    return results


class OpenAICompatProvider(LLMProvider):
    """OpenAI Chat Completions 兼容接入点。"""

    name = "openai"
    display_name = "OpenAI 兼容接口"
    summary = "任何讲 /chat/completions 协议的服务：OpenAI、火山方舟、DeepSeek、本地 vLLM 等。"
    requires_credentials = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        transport: Transport | None = None,
        temperature: float = 0.2,
        request_overrides: dict[str, Any] | None = None,
    ) -> None:
        if api_key is None:
            api_key = os.environ.get("GAMETRANS_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.api_key = api_key
        # 推理型模型（真靶实测：单次 130K 输出 token、墙钟约 400 秒）会被默认超时误伤，
        # 所以超时也能由环境变量调大 —— 与模型名同一套规矩（环境变量优先）。
        self.timeout = _as_float(os.environ.get("GAMETRANS_TIMEOUT")) or timeout
        self.model = model or os.environ.get("GAMETRANS_MODEL", DEFAULT_MODEL)
        self.base_url = normalize_base_url(
            base_url or os.environ.get("GAMETRANS_BASE_URL", DEFAULT_BASE_URL)
        )
        self.temperature = temperature
        self.request_overrides = _checked_overrides(request_overrides, source="构造参数")
        self._transport = transport

    # ---- 配置 ---------------------------------------------------------------

    def request_url(self) -> str:
        """真正要打的那个地址（唯一一处拼接，测的就是它）。"""
        return f"{self.base_url}/chat/completions"

    def configure(self, **credentials: Any) -> None:
        """工作区里存的凭证是**兜底**：环境变量设了就以环境变量为准。

        这样"面板里存了一把 key"与"CI 里注入 key"不会互相打架 —— 后者赢。
        """
        api_key = str(credentials.get("api_key") or "").strip()
        if api_key and not (
            os.environ.get("GAMETRANS_API_KEY") or os.environ.get("OPENAI_API_KEY")
        ):
            self.api_key = api_key
        base_url = normalize_base_url(str(credentials.get("base_url") or ""))
        if base_url and not os.environ.get("GAMETRANS_BASE_URL"):
            self.base_url = base_url
        model = str(credentials.get("model") or "").strip()
        if model and not os.environ.get("GAMETRANS_MODEL"):
            self.model = model

    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info["model"] = self.model
        info["base_url"] = self.base_url
        return info

    # ---- 请求 / 响应 --------------------------------------------------------

    def effective_overrides(self) -> dict[str, Any]:
        """这一次调用真正要用的覆写：**环境变量优先**，与模型名同一套规矩。

        每次调用重新解析环境变量（不是构造时缓存一次）—— 否则同一进程里改了环境变量
        却"配了没生效"，而报告上看不出来。
        """
        merged = dict(self.request_overrides)
        merged.update(_overrides_from_env())
        return merged

    def build_payload(self, request: TranslationRequest) -> dict[str, Any]:
        """一次调用的请求体。**system 与正文都只来自请求自带的那一份模板。**

        这里不再持有任何提示词字面量：此前 provider 里躺着两份 system 提示词
        （``SYSTEM_PROMPT`` / ``SYSTEM_PROMPT_COMPACT``），与渲染器一起构成"看一份、
        跑另一份"的温床。现在只有 :meth:`TranslationRequest.system_message` 与
        :meth:`TranslationRequest.render` 两条出口，都指向 :mod:`gametrans.prompts`。
        """
        payload = {
            "model": self.model,
            "stream": False,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": request.system_message()},
                # 会话历史（长单元分轮时用）：只追加不改，前缀才稳定、才吃得到 cache
                *[dict(message) for message in request.history],
                {"role": "user", "content": request.render()},
            ],
        }
        # 覆写**最后**合并：它装的是"这个接入点/这个模型"的参数（例如推理型模型的
        # `max_tokens`）。有主的键在配置落地时就被拒了，所以这里不会盖掉 model/messages/stream。
        payload.update(self.effective_overrides())
        return payload

    def parse_response(self, response: dict[str, Any]) -> list[TranslationResult]:
        try:
            choice = response["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"{self.display_name} 返回了预期之外的结构：{exc!r}",
                hint="确认 base_url 指向的是 OpenAI 兼容的 /chat/completions 接口。",
            ) from None

        finish_reason = str(choice.get("finish_reason") or "")
        if not str(content or "").strip() and finish_reason == "length":
            # 真靶实测：推理型模型会把**整个**输出预算花在 reasoning 上，正文一个字都不出，
            # 到这里只表现为"响应不是合法 JSON"。把真因写进错误里 —— 否则运维者会去查格式。
            raise ProviderError(
                f"{self.display_name} 的响应没有正文：finish_reason=length，"
                f"推理把输出预算用尽了（completion_tokens="
                f"{(response.get('usage') or {}).get('completion_tokens')}）",
                hint=(
                    "给这个模型一个更大的输出预算：在配置里加 "
                    '`request_overrides: {"max_tokens": 384000}`'
                    f"（或设环境变量 {REQUEST_OVERRIDES_ENV}）；"
                    "也可以考虑关掉思考模式（`thinking: {\"type\": \"disabled\"}`），"
                    "或者把这一批拆小。"
                ),
            )

        return parse_answer_text(str(content), provider=self.name)

    def translate(self, request: TranslationRequest) -> list[TranslationResult]:
        if not self.is_configured():
            raise ProviderError(
                f"{self.display_name} 未配置 API Key，无法翻译",
                hint=(
                    "设置环境变量 GAMETRANS_API_KEY（或 OPENAI_API_KEY），"
                    "必要时再设 GAMETRANS_BASE_URL 与 GAMETRANS_MODEL；"
                    "想先离线跑通全链路可以用 --provider mock。"
                ),
            )
        if not request.items:
            return []

        payload = self.build_payload(request)
        if request.dump is not None:
            # 落的就是这个 payload —— 讨论"请求里有什么"时看文件，不看任何人的推断
            request.dump.record(payload, phase=request.phase, items=len(request.items))
        url = self.request_url()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        send = self._transport or self._http_post
        # 失败的那次调用同样花了钱、同样占了时间，所以失败也留一条记录。
        body: dict[str, Any] | None = None
        started = time.perf_counter()
        try:
            body = send(url, headers, payload)
            results = self.parse_response(body)
        except Exception as exc:  # noqa: BLE001 - 记完账再把原错误抛出去
            # 服务端给的解释（提示串）必须进账：只说"返回 HTTP 400"，
            # 事后谁也查不出是内容被拒、参数不合法，还是上游截断（真靶吃过这个亏）。
            detail = (
                exc.render() if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
            )
            self._record(
                request,
                started,
                body=body,
                ok=False,
                error=detail,
            )
            # 失败的那次同样留原文：只看 token 数是查不出"模型回了什么坏东西"的
            self._transcribe(request, payload, body, error=detail)
            raise
        self._record(request, started, body=body, ok=True)
        self._transcribe(request, payload, body, results=results, error="")
        return results

    def _transcribe(
        self,
        request: TranslationRequest,
        payload: dict[str, Any],
        body: dict[str, Any] | None,
        *,
        results: list[TranslationResult] | None = None,
        error: str = "",
    ) -> None:
        """把这次调用原样记进流水：**发出去的提示词 + 服务端回来的原文**。

        只记请求里"给模型看的部分"（system / user）与回复内容，不记密钥、不记 header。
        没有流水槽位就什么都不做（老调用方不受影响）。

        **分轮会话**（``request.history`` 非空）记的是**整段对话**：只记第一条 user 的话，
        第二轮以后就看不出实际发出去的是什么 —— 而"前缀稳定、历史只追加"正是那一形状
        最需要被核的地方。每条前面标了 role，内容逐字不变。
        """
        sink = request.transcript
        if sink is None:
            return
        messages = payload.get("messages") or []
        system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
        user = next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "")
        if request.history:
            heads = {"user": "—— 发给模型的 user ——", "assistant": "—— 模型上一轮的回复 ——"}
            user = "\n\n".join(
                f"{heads.get(str(m.get('role')), str(m.get('role')))}：\n{m.get('content') or ''}"
                for m in messages
                if m.get("role") != "system"
            )
        content = ""
        if isinstance(body, dict):
            try:
                content = str(body["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError):
                content = ""
        sink.append(
            {
                "phase": request.phase or "",
                # 这一次用的是哪套请求模板：没有它，台账里"简化版"和"复杂版"的请求
                # 长得一样，只能逐条点开看正文。记的是**模板名**（用户自己的东西，
                # 以后还能来自创意工坊），不是我们替它起的标题。
                "template": str(request.template_name or ""),
                "template_label": str((request.prompt_template or {}).get("label") or ""),
                "template_digest": prompts.template_digest(request.prompt_template),
                "model": self.model,
                "items": len(request.items),
                "unit_ids": [item.unit_id for item in request.items],
                # **真要回译的那几条**：`unit_ids` 里含"只当上下文"的命中句，
                # 不区分开就没法回答"这次到底哪几条没拿到"（面板/台账要的是这个数）
                "expected_ids": [
                    item.unit_id for item in request.items if item.expects_answer
                ],
                "prompt_system": system,
                "prompt_user": user,
                # 线路上用的短身份 → 完整槽位身份。缺了它，台账就只能拿模型回的那些
                # 短身份去比完整身份，对不上的一律报成"没拿到"——整批误报。
                # 正文本身照旧原样落盘（那是原生信息），这张表只是并排记一笔。
                "id_map": dict(request.short_ids),
                "response_raw": content,
                "parsed": [r.to_dict() for r in (results or [])],
                "usage": (body or {}).get("usage") if isinstance(body, dict) else None,
                "ok": not error,
                "error": error,
            }
        )

    def _record(
        self,
        request: TranslationRequest,
        started: float,
        *,
        body: dict[str, Any] | None,
        ok: bool,
        error: str = "",
    ) -> None:
        """把这一次调用写进账本。没有账本就不记（契约对老调用方不变）。"""
        log = request.calls
        if log is None:
            return
        payload = body if isinstance(body, dict) else {}
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        # 缓存命中：服务端给的名字不止一种，取到哪个算哪个；一个都没有就是 None
        cache_hit = _as_int(usage.get("prompt_cache_hit_tokens"))
        if cache_hit is None:
            cache_hit = _as_int(usage.get("cached_tokens"))
        # 截断信号与推理用量：真靶实测"推理吃光输出预算"时，只有这两个读数分得清
        # "正文没写"与"格式写坏了"。
        try:
            choices = payload.get("choices") or []
            finish_reason = str((choices[0] or {}).get("finish_reason") or "")
        except (AttributeError, IndexError, TypeError):
            finish_reason = ""
        completion_details = usage.get("completion_tokens_details")
        completion_details = completion_details if isinstance(completion_details, dict) else {}
        reasoning_tokens = _as_int(completion_details.get("reasoning_tokens"))
        log.add(
            CallRecord(
                provider=self.name,
                requested_model=self.model,
                reported_model=str(payload.get("model") or ""),
                prompt_tokens=_as_int(usage.get("prompt_tokens")),
                completion_tokens=_as_int(usage.get("completion_tokens")),
                total_tokens=_as_int(usage.get("total_tokens")),
                credit=_as_float(usage.get("credit")),
                cache_hit_tokens=cache_hit,
                cache_miss_tokens=_as_int(usage.get("prompt_cache_miss_tokens")),
                finish_reason=finish_reason,
                reasoning_tokens=reasoning_tokens,
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
                items=len(request.items),
                unit_ids=[item.unit_id for item in request.items],
                phase=request.phase,
                ok=ok,
                error=error,
            )
        )

    def _http_post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # pragma: no cover - 读取失败不影响主错误
                pass
            # 限流与 5xx 是**暂时**的：调用方该退避重试；其余（鉴权/参数/路由）重试只是
            # 把同样的错再买一遍。判断放在这里是因为**只有这里**知道 HTTP 状态码。
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0.0)
            except (TypeError, ValueError):  # pragma: no cover - 服务端乱写就不等
                retry_after = 0.0
            raise ProviderError(
                f"{self.display_name} 返回 HTTP {exc.code}",
                hint=f"服务端信息：{detail}" if detail else "检查 base_url、模型名与配额。",
                status=int(exc.code),
                retryable=int(exc.code) == 429 or 500 <= int(exc.code) < 600,
                retry_after=retry_after,
            ) from None
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"连不上 {self.display_name}：{exc.reason}",
                hint="检查网络、base_url 拼写，以及是否配置了代理。",
            ) from None
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(
                f"{self.display_name} 调用失败：{exc}",
                hint="可调小 --batch-size 降低单次请求压力后重试。",
            ) from None
