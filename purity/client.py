"""中转客户端：统一封装 OpenAI 与 Anthropic 两种协议。"""

from .transport import Resp, curl_request


class RelayClient:
    """统一封装一个中转端点，支持 OpenAI 与 Anthropic 两种协议。"""

    def __init__(self, url: str, key: str, model: str, timeout: int = 60):
        self.root = url.rstrip("/")
        if self.root.endswith("/v1"):
            self.root = self.root[:-3]
        self.key = key
        self.model = model
        self.timeout = timeout
        self._fmt = None  # 缓存可用协议："openai" | "anthropic"

    # -- 端点 ----------------------------------------------------------------
    @property
    def openai_url(self):
        return f"{self.root}/v1/chat/completions"

    @property
    def anthropic_url(self):
        return f"{self.root}/v1/messages"

    @property
    def models_url(self):
        return f"{self.root}/v1/models"

    # -- 协议调用 ------------------------------------------------------------
    def call_openai(self, prompt, system=None, max_tokens=512, stream=False):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        body = {"model": self.model, "max_tokens": max_tokens, "messages": msgs}
        if stream:
            body["stream"] = True
        headers = {
            "Authorization": f"Bearer {self.key}",
            "content-type": "application/json",
        }
        return curl_request("POST", self.openai_url, headers, body, self.timeout)

    def call_anthropic(self, prompt, system=None, max_tokens=512):
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        headers = {
            "x-api-key": self.key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        return curl_request("POST", self.anthropic_url, headers, body, self.timeout)

    def list_models(self):
        headers = {"Authorization": f"Bearer {self.key}"}
        return curl_request("GET", self.models_url, headers, None, self.timeout)

    # -- 便捷封装 ------------------------------------------------------------
    def smart_ask(self, prompt, system=None, max_tokens=512):
        """优先 OpenAI 协议提问，失败则回退 Anthropic，返回 (文本, Resp)。"""
        order = (["openai", "anthropic"] if self._fmt != "anthropic"
                 else ["anthropic", "openai"])
        last = ""
        for fmt in order:
            if fmt == "openai":
                resp = self.call_openai(prompt, system, max_tokens)
                text = extract_openai_text(resp)
            else:
                resp = self.call_anthropic(prompt, system, max_tokens)
                text = extract_anthropic_text(resp)
            if resp.ok and text:
                self._fmt = fmt
                return text, resp
            last = resp.error or f"HTTP {resp.status}"
        return "", Resp(status=0, elapsed=0.0, error=last)


def extract_openai_text(resp: Resp) -> str:
    data = resp.json()
    if not data:
        return ""
    try:
        choice = data["choices"][0]
        content = choice.get("message", {}).get("content", "")
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):  # 部分网关返回分段 content
        return "".join(
            seg.get("text", "") for seg in content if isinstance(seg, dict)
        )
    return content or ""


def extract_anthropic_text(resp: Resp) -> str:
    data = resp.json()
    if not data:
        return ""
    content = data.get("content")
    if isinstance(content, list):
        return "".join(
            seg.get("text", "") for seg in content
            if isinstance(seg, dict) and seg.get("type") == "text"
        )
    if isinstance(content, str):
        return content
    return ""
