"""ZPAY contract boundary. Never log URLs, keys or raw gateway responses."""
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import hmac
import os
import re
from urllib.parse import urlsplit, quote, quote_plus

import httpx


class PaymentError(Exception):
    """Stable error code; optional gateway explanation is redacted at boundary."""

    def __init__(self, code, *, gateway_message=""):
        super().__init__(code)
        self.gateway_message = gateway_message


def gateway_message(data, config, *sensitive):
    """Keep only the documented msg, never entire responses or payment URLs."""
    value = data.get("msg", "")
    if not isinstance(value, str):
        return ""
    text = value[:4096]
    for secret in (config.key, config.pid, *sensitive):
        if secret:
            for variant in {str(secret), quote(str(secret), safe=""), quote_plus(str(secret))}:
                text = text.replace(variant, "[已隐藏]")
    text = re.sub(r"(?:https?://|alipays?://)\S+", "[链接已隐藏]", text, flags=re.I)
    text = re.sub(r"[A-Za-z0-9_+/=-]{20,}", "[标识已隐藏]", text)
    return " ".join(text.split())[:240]


def money_fen(value, *, display=False):
    text = str(value).strip()
    if display:
        text = re.sub(r"^(?:[¥￥]|CNY\s*)", "", text, flags=re.I)
        text = re.sub(r"\s*(?:元|CNY)$", "", text, flags=re.I).strip()
    if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", text):
        raise PaymentError("invalid_money")
    fen = int(Decimal(text) * 100)
    if fen <= 0:
        raise PaymentError("invalid_money")
    return fen


def yuan(fen):
    return f"{int(fen) // 100}.{int(fen) % 100:02d}"


def sign_params(params, key):
    canonical = "&".join(f"{k}={v}" for k, v in sorted(params.items())
                         if k not in {"sign", "sign_type"} and v is not None and str(v) != "")
    return hashlib.md5((canonical + key).encode("utf-8")).hexdigest()


def verify_signature(params, key):
    supplied = params.get("sign", "")
    return (params.get("sign_type", "MD5") == "MD5"
            and bool(re.fullmatch(r"[a-f0-9]{32}", supplied))
            and hmac.compare_digest(sign_params(params, key), supplied))


@dataclass(frozen=True)
class ZPayConfig:
    pid: str
    key: str = field(repr=False)
    public_url: str = ""
    cid: str = ""

    @classmethod
    def from_env(cls):
        pid = os.getenv("ZPAY_PID", "").strip()
        key = os.getenv("ZPAY_KEY", "").strip()
        public_url = os.getenv("PAYMENT_PUBLIC_URL", "").strip().rstrip("/")
        if not (pid and key and public_url):
            raise PaymentError("payment_not_configured")
        parsed = urlsplit(public_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise PaymentError("public_url_must_be_https_origin")
        return cls(pid, key, public_url, os.getenv("ZPAY_CID", "").strip())

    @property
    def notify_url(self):
        return self.public_url + "/payments/zpay/notify"


class ZPayClient:
    def __init__(self, config, client):
        self.config = config
        self.client = client

    async def _json(self, method, path, **kwargs):
        # No transport retry and no redirects (in particular for api.php?key=...).
        try:
            async with self.client.stream(method, "https://zpayz.cn" + path,
                                          follow_redirects=False, timeout=15, **kwargs) as response:
                if response.status_code != 200:
                    raise PaymentError(f"gateway_http_error_{response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise PaymentError("gateway_response_too_large")
                import json
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise PaymentError("gateway_invalid_response")
                return data
        except PaymentError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise PaymentError("gateway_unavailable") from None

    def submit_params(self, intent):
        """Signed browser POST, exactly the documented submit.php fields.

        This constructs a form only; the server never requests submit.php or
        mapi.php. Never add the merchant key itself to the returned fields.
        """
        params = {"pid": self.config.pid, "type": "alipay",
                  "out_trade_no": intent["payment_id"], "notify_url": self.config.notify_url,
                  "return_url": self.config.public_url + "/payments/zpay/return",
                  "name": intent["title"], "money": yuan(intent["amount_fen"])}
        if self.config.cid:
            params["cid"] = self.config.cid
        params["sign"] = sign_params(params, self.config.key)
        params["sign_type"] = "MD5"
        return params

    async def query(self, payment_id):
        return await self._json("GET", "/api.php", params={"act": "order", "pid": self.config.pid,
                                                          "key": self.config.key, "out_trade_no": payment_id})


def safe_https_url(value):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not any(ord(c) < 32 for c in value))
    except ValueError:
        return False


def validate_paid(data, intent, config, *, notification):
    if (str(data.get("pid", "")) != config.pid or data.get("type") != "alipay"
            or str(data.get("out_trade_no", "")) != intent["payment_id"]
            or money_fen(data.get("money", "")) != intent["amount_fen"]):
        raise PaymentError("payment_identity_mismatch")
    if notification:
        if not verify_signature(data, config.key) or data.get("trade_status") != "TRADE_SUCCESS":
            raise PaymentError("invalid_payment_notification")
    elif str(data.get("code")) != "1" or str(data.get("status")) != "1":
        raise PaymentError("payment_not_paid")
    trade_no = str(data.get("trade_no") or "")
    if not trade_no or len(trade_no) > 128 or (intent.get("trade_no") and intent["trade_no"] != trade_no):
        raise PaymentError("payment_trade_mismatch")
    return trade_no
