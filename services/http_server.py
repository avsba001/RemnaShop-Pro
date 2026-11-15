"""General HTTP application with pluggable routes and a ZPAY payment module.

Run only via the project's Compose stack. No bot import / no Telegram token in
this process. Callback commits a generic payment event, never calls Panel.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import sqlite3
import datetime
from io import BytesIO

from aiohttp import web
import httpx
import qrcode

from services.zpay import PaymentError, ZPayConfig, ZPayClient, verify_signature, validate_paid, yuan
from services.payment_processing import reconcile_payment
from storage.payments import (init_payments, for_checkout, get_intent, claim_redirect, mark_paid)

logger = logging.getLogger(__name__)
DB = web.AppKey("db", str)
CONFIG = web.AppKey("payment_config", object)
CLIENT = web.AppKey("zpay_client", object)
PROXIES = web.AppKey("trusted_proxies", tuple)


def real_client_ip(peer, forwarded, networks):
    try:
        address = ipaddress.ip_address(peer)
        # Only inspect XFF when the immediate peer is a configured trusted proxy.
        if any(address in network for network in networks):
            hops = [ipaddress.ip_address(part.strip()) for part in forwarded.split(",") if part.strip()]
            if not hops or len(hops) > 16:
                raise ValueError
            for hop in reversed(hops):
                address = hop
                if not any(hop in network for network in networks):
                    break
        if not address.is_global:
            raise ValueError
        return str(address)
    except ValueError:
        raise PaymentError("real_client_ip_unavailable") from None


@web.middleware
async def safe_responses(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = web.Response(status=exc.status, text="request rejected")
    except (sqlite3.Error, OSError):
        logger.warning("HTTP storage unavailable")
        response = web.Response(status=503, text="temporarily unavailable")
    except Exception as exc:
        # Deliberately omit exception text, URL, query string and checkout token.
        logger.error("HTTP request failed exception=%s", type(exc).__name__)
        response = web.Response(status=500, text="internal error")
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "same-origin",
                             "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                             })
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; img-src data: https:; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    return response


async def health(request):
    await asyncio.to_thread(get_intent, request.app[DB], '__health__')
    return web.json_response({"status": "ok", "service": "http"})


async def notify(request):
    config = request.app[CONFIG]
    if config is None:
        return web.Response(status=503, text="payment unavailable")
    if len(request.query_string) > 8192 or len(request.query) > 32:
        return web.Response(status=400, text="fail")
    if any(len(request.query.getall(k)) != 1 for k in request.query):
        return web.Response(status=400, text="fail")
    data = dict(request.query)
    if not verify_signature(data, config.key):
        return web.Response(status=400, text="fail")
    intent = await asyncio.to_thread(get_intent, request.app[DB], data.get("out_trade_no", ""))
    if not intent:
        return web.Response(status=400, text="fail")
    try:
        trade_no = validate_paid(data, intent, config, notification=True)
        await asyncio.to_thread(mark_paid, request.app[DB], intent["payment_id"], trade_no)
    except PaymentError:
        return web.Response(status=400, text="fail")
    return web.Response(text="success", content_type="text/plain")


def csrf_value(token):
    # Capability-bound anti-CSRF nonce; no cookies or browser identity involved.
    return hashlib.sha256((token + ":checkout-post").encode()).hexdigest()


def checkout_html(intent, note=""):
    token = intent["checkout_token"]
    label = "前往 ZPAY 支付宝收银台" if intent["status"] == "new" else "查询支付状态（不会重新下单）"
    form = (f'<form method="post"><input type="hidden" name="csrf" value="{csrf_value(token)}">'
            f'<button type="submit">{label}</button></form>')
    if intent.get('order_deleted_at'):
        content = ("<p>此未支付订单已由管理员从列表删除，本页不再发起付款。</p>"
                   "<p>本地删除不会关闭网关已发出的付款链接。若后来核验到账，将恢复订单并处理；如有疑问请联系管理员。</p>")
    elif intent["status"] == "paid":
        content = "<p>支付成功，商品正在交付。订阅用户请返回 Telegram 查看。</p>"
    elif intent["status"] == "redirected":
        content = ("<p>已签发跳转支付请求。请在 ZPAY 收银台完成支付后返回查询。</p>"
                   "<p>本页不会再次发起下单；若跳转失败或网关报错，请将提示告知管理员。</p>" + form)
    elif intent["status"] == "ready":
        data = json.loads(intent["gateway_json"])
        content = "<p>请使用支付宝扫码付款，或点击付款链接。</p>"
        if data.get("qrcode"):
            image = qrcode.make(data["qrcode"])
            output = BytesIO()
            image.save(output, format="PNG")
            content += '<img width="280" height="280" alt="支付宝付款码" src="data:image/png;base64,' + base64.b64encode(output.getvalue()).decode() + '">'
        elif data.get("img"):
            content += '<img width="280" alt="支付宝付款码" src="' + html.escape(data["img"], quote=True) + '">'
        if data.get("payurl"):
            content += '<p><a rel="noreferrer noopener" href="' + html.escape(data["payurl"], quote=True) + '">打开支付宝收银台</a></p>'
        content += form
    elif intent["status"] in {"creating", "unknown"}:
        content = ("<p>此前下单未取得可用付款信息，当前没有可展示的付款码。</p>"
                   "<p>这是旧 API 订单，不会自动转为跳转支付或重发。请管理员先核对并删除此未支付订单，再从机器人重新购买。</p>" + form)
        if intent.get('creation_error_code') == 'gateway_http_error_403':
            content += '<p>ZPAY 支付接口返回 HTTP 403（拒绝访问），未取得付款码。请管理员联系 ZPAY 检查接口访问拦截；本页不会自动重试下单。</p>'
        elif intent.get('creation_error_code'):
            content += '<p>下单诊断：' + html.escape(intent['creation_error_code']) + '</p>'
        if intent.get("last_checked_at"):
            checked = datetime.datetime.fromtimestamp(intent["last_checked_at"], datetime.timezone.utc)
            content += f'<p>最近发起查单：{checked:%Y-%m-%d %H:%M:%S} UTC（不代表查询成功）</p>'
    else:
        content = "<p>点击下方按钮，跳转到 ZPAY 官方收银台使用支付宝付款。本站不再调用 API 生成二维码。</p>" + form
    title = html.escape(intent["title"])
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>支付宝收银台</title><style>body{font:16px system-ui;max-width:520px;margin:40px auto;padding:20px;line-height:1.7}button{padding:12px;cursor:pointer}img{max-width:100%;height:auto}</style>'
            f'<h1>支付宝收银台</h1><h2>{title}</h2><p>金额：¥{yuan(intent["amount_fen"])}</p>'
            f'<p>{html.escape(note)}</p>{content}</html>')


def redirect_form(intent, config):
    """One browser handoff; fixed destination and nonce-scoped auto-submit."""
    params = ZPayClient(config, None).submit_params(intent)
    inputs = "".join('<input type="hidden" name="' + html.escape(k, quote=True)
                     + '" value="' + html.escape(v, quote=True) + '">'
                     for k, v in params.items())
    nonce = os.urandom(18).hex()
    body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>正在跳转 ZPAY</title><h1>正在前往 ZPAY 支付宝收银台</h1>'
            '<p>若未自动跳转，请点击下方按钮。请勿刷新或重复提交本页。</p>'
            '<form id="zpay-submit" method="post" action="https://zpayz.cn/submit.php">'
            + inputs + '<button type="submit">继续前往 ZPAY 支付</button></form>'
            + '<script nonce="' + nonce + '">document.getElementById("zpay-submit").submit();</script></html>')
    response = web.Response(text=body, content_type="text/html")
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'nonce-" + nonce + "'; form-action https://zpayz.cn; "
        "base-uri 'none'; frame-ancestors 'none'")
    return response


async def checkout(request):
    config = request.app[CONFIG]
    if config is None:
        return web.Response(status=503, text="支付尚未配置，请联系管理员。")
    token = request.match_info["token"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise web.HTTPNotFound()
    db = request.app[DB]
    intent = await asyncio.to_thread(for_checkout, db, token)
    if not intent:
        raise web.HTTPNotFound()
    if intent.get('order_deleted_at'):
        return web.Response(status=410, text=checkout_html(intent), content_type="text/html")
    note = ""
    if request.method == "POST" and intent["status"] != "paid":
        origin = request.headers.get("Origin")
        if origin and origin != config.public_url:
            raise web.HTTPForbidden()
        form = await request.post()
        if len(form.getall("csrf", [])) != 1 or not hmac.compare_digest(str(form.get("csrf", "")), csrf_value(token)):
            raise web.HTTPForbidden()
        try:
            if intent["status"] == "new" and intent.get("gateway_mode") == "redirect":
                if await asyncio.to_thread(claim_redirect, db, intent["payment_id"]):
                    return redirect_form(intent, config)
                note = "支付跳转请求已签发或订单状态已改变，请勿重复提交。"
            else:
                result = await reconcile_payment(db, intent["payment_id"], config, request.app[CLIENT])
                note = ("已确认付款。" if result["status"] == "paid" else
                        "距离最近一次查单不足 30 秒，请稍后再查询。" if result["last_checked_at"] == intent["last_checked_at"] else
                        "查询已完成，尚未确认付款。若网关页面报错，请联系管理员，不要重复下单。")
        except PaymentError as exc:
            if str(exc) == "gateway_query_rejected":
                note = "ZPAY 查单未成功：" + (exc.gateway_message or "网关未返回有效订单") + "。请联系管理员核对。"
            else:
                note = "支付状态暂不可确认（" + str(exc) + "），请联系管理员核对；不要重复付款。"
        intent = await asyncio.to_thread(for_checkout, db, token)
        if intent.get('order_deleted_at'):
            return web.Response(status=410, text=checkout_html(intent), content_type="text/html")
    return web.Response(text=checkout_html(intent, note), content_type="text/html")


async def payment_return(request):
    """Browser return never commits paid based on return parameters alone."""
    config = request.app[CONFIG]
    if config is None:
        return web.Response(status=503, text="支付尚未配置，请返回机器人查询。")
    if (len(request.query_string) > 8192 or len(request.query) > 32
            or any(len(request.query.getall(k)) != 1 for k in request.query)):
        raise web.HTTPBadRequest()
    data = dict(request.query)
    if not verify_signature(data, config.key):
        return web.Response(status=400, text="无法验证支付返回信息，请返回机器人查询订单；此页面不代表付款成功。")
    intent = await asyncio.to_thread(get_intent, request.app[DB], data.get("out_trade_no", ""))
    if not intent:
        raise web.HTTPBadRequest()
    try:
        validate_paid(data, intent, config, notification=True)
    except PaymentError:
        raise web.HTTPBadRequest() from None
    # Receipt comes only from async notify or authenticated server-side query.
    try:
        await reconcile_payment(request.app[DB], intent["payment_id"], config, request.app[CLIENT])
    except PaymentError as exc:
        logger.warning("Return payment query unconfirmed code=%s", str(exc))
    return web.Response(status=303, headers={"Location": "/payments/zpay/checkout/" + intent["checkout_token"]})


def create_app(db_file, config=None, *, client=None, trusted_proxies=(), extra_routes=()):
    app = web.Application(middlewares=[safe_responses], client_max_size=8192)
    app[DB] = db_file
    app[CONFIG] = config
    app[PROXIES] = tuple(ipaddress.ip_network(value.strip()) for value in trusted_proxies if value.strip())
    app[CLIENT] = client

    async def lifecycle(application):
        await asyncio.to_thread(init_payments, db_file)
        if client is None and config:
            async with httpx.AsyncClient(trust_env=False, limits=httpx.Limits(max_connections=20)) as http:
                application[CLIENT] = ZPayClient(config, http)
                yield
        else:
            yield
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([web.get("/healthz", health), web.get("/payments/zpay/notify", notify),
                    web.get("/payments/zpay/return", payment_return),
                    web.get("/payments/zpay/checkout/{token}", checkout),
                    web.post("/payments/zpay/checkout/{token}", checkout)])
    # Future modules register their own routes without coupling to payment logic.
    app.add_routes(extra_routes)
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if any(os.getenv(k, "").strip() for k in ("ZPAY_PID", "ZPAY_KEY", "PAYMENT_PUBLIC_URL")):
        config = ZPayConfig.from_env()  # Partial/invalid configuration fails closed.
    else:
        config = None
    app = create_app(os.getenv("REMNASHOP_DB", "/data/starlight.db"), config,
                     trusted_proxies=os.getenv("HTTP_TRUSTED_PROXIES", "").split(","))
    # Access logs would contain callback signatures and checkout capabilities.
    web.run_app(app, host="0.0.0.0", port=8080, access_log=None, print=None)


if __name__ == "__main__":
    main()
