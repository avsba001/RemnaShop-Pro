# RemnaShop-Pro：Nginx 反向代理与 HTML 首页配置

本文根据测试机 `xxxx.com` 的实际 Nginx 配置整理，核对日期为 2026-11-15。适用于 **Nginx 安装在宿主机上，应用通过 Docker Compose 运行** 的部署方式。

本文提供配置和操作说明，不包含服务器登录凭据、Bot Token、ZPAY 密钥或 HTTPS 私钥。域名为当前测试域名，部署到其他域名时须统一替换。

## 1. 当前路由结构

```text
浏览器 / ZPAY 支付通知
        │ HTTPS :443
        ▼
宿主机 Nginx：xxxx.com
        ├── /                  → /opt/service-pay/web/ 静态文件
        ├── /api/              → 127.0.0.1:8000（独立的 service-pay 项目）
        ├── /payments/         → 127.0.0.1:8080（RemnaShop-Pro HTTP 服务）
        └── /healthz           → Nginx 直接返回 nginx-ok
```

- `/api/` 和首页属于独立的 service-pay 项目，RemnaShop-Pro 本身不依赖它们。
- RemnaShop-Pro 的支付按钮直接打开 `https://zpayz.cn/submit.php?...` 签名链接。本站 `/payments/` 用于异步通知、支付返回、查询页面和旧链接兼容。
- ZPAY 页面负责展示付款二维码，不能用静态 HTML 页面代替。
- 公网 `/healthz` 只检查 Nginx 能否响应，不代表 Bot、支付、数据库或 Panel 正常。

## 2. 会用到的文件

```text
/etc/nginx/
├── nginx.conf
├── conf.d/
│   └── service-pay-logging.conf
├── sites-available/
│   └── service-pay.conf
├── sites-enabled/
│   └── service-pay.conf -> /etc/nginx/sites-available/service-pay.conf
└── snippets/
    └── remnashop-payments.conf

/opt/service-pay/web/
└── index.html

/var/www/letsencrypt/
└── .well-known/acme-challenge/       # HTTP-01 证书验证目录

/etc/letsencrypt/live/xxxx.com/
├── fullchain.pem
└── privkey.pem
```

测试机还保留发行版默认站点 `sites-enabled/default`。不要盲目复制、覆盖或删除服务器上其他站点；只需确保同一域名没有重复的 `server_name` 配置。

证书和私钥不包含在本文或配置备份中，需要通过现有证书管理工具单独申请、续期和保存。

## 3. 部署前准备

以下命令在 Linux 服务器上执行，需要 root 权限或相应 sudo 权限。

1. 将域名 A 记录指向服务器 IPv4；只有 IPv6 已正确配置时才发布 AAAA 记录。
2. 确保公网能访问该域名的 80、443 端口。
3. 安装 Nginx，并使用现有证书工具准备该域名证书。
4. 按项目 README 通过 Docker Compose 安装 RemnaShop-Pro。
5. 确认应用 HTTP 端口只发布到宿主机回环地址：`127.0.0.1:8080`。

修改前先保存当前配置：

```bash
sudo sh -c 'umask 077; tar -czf /root/nginx-before-$(date +%Y%m%d-%H%M%S).tar.gz -C /etc nginx'
```

建立目录：

```bash
sudo install -d -m 755 /etc/nginx/snippets
sudo install -d -m 755 /var/www/letsencrypt
sudo install -d -m 755 /opt/service-pay/web
```

这些命令不会申请证书。若还没有证书，先只启用第 6 节的 HTTP server 块，完成证书工具的 HTTP-01 验证，再添加 HTTPS server 块；不要引用尚不存在的证书文件后直接重载 Nginx。

## 4. 主配置中的 include

发行版已有的 `/etc/nginx/nginx.conf` 通常无需整体替换。检查其 `http { ... }` 内包含：

```nginx
http {
    # 保留原有基础配置。
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    include /etc/nginx/conf.d/*.conf;
    include /etc/nginx/sites-enabled/*;
}
```

上面是结构片段，不是可直接覆盖整个 `nginx.conf` 的完整文件。`log_format` 必须处于 `http` 上下文；站点的 `server` 块也要由 `http` 引入。

## 5. 日志格式配置

文件：`/etc/nginx/conf.d/service-pay-logging.conf`

```nginx
log_format service_pay_safe '$remote_addr [$time_local] "$request_method $uri $server_protocol" $status $body_bytes_sent $request_time';
```

该格式使用 `$uri`，不记录查询字符串。不要改成 `$request` 或追加 `$args`，避免回调签名等参数进入普通访问日志。支付路由还会单独关闭访问日志。

## 6. 站点配置

文件：`/etc/nginx/sites-available/service-pay.conf`

以下配置与测试机当前站点一致，包含静态首页、service-pay API 和 RemnaShop-Pro 支付反代：

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name xxxx.com;
    server_tokens off;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
    }

    location / {
        return 301 https://xxxx.com$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name xxxx.com;

    ssl_certificate /etc/letsencrypt/live/xxxx.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/xxxx.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:service_pay_tls:10m;
    ssl_session_tickets off;
    server_tokens off;

    root /opt/service-pay/web;
    index index.html;
    charset utf-8;
    client_max_body_size 1m;

    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header X-Frame-Options DENY always;

    access_log /var/log/nginx/service-pay.access.log service_pay_safe;
    error_log /var/log/nginx/service-pay.error.log warn;

    location = /healthz {
        access_log off;
        default_type text/plain;
        return 200 "nginx-ok\n";
    }

    # 独立的 service-pay 项目使用。仅部署 RemnaShop-Pro 时可省略此块。
    location ^~ /api/ {
        access_log off;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_redirect off;
        proxy_no_cache 1;
        add_header Cache-Control "no-store" always;
    }

    include /etc/nginx/snippets/remnashop-payments.conf;

    location / {
        try_files $uri $uri/ =404;
    }
}
```

说明：

- 仅部署 RemnaShop-Pro 时，保留 `/payments/` 反代和必要的 HTTPS 配置即可；不要把其他机器上已有的 `/api/` 业务覆盖掉。
- 示例中的 `listen 443 ssl http2` 来自测试机。若 Nginx 版本对该写法发出弃用提示，应按对应版本文档改为 `listen 443 ssl;` 并设置 `http2 on;`；旧版本未必支持后一种写法，以 `nginx -t` 结果为准。
- 如果机器未启用 IPv6，应按实际环境调整 `[::]` 监听及 DNS AAAA 记录。

## 7. RemnaShop-Pro 支付反代配置

文件：`/etc/nginx/snippets/remnashop-payments.conf`

```nginx
location ^~ /payments/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_redirect off;

    client_max_body_size 8k;
    proxy_connect_timeout 3s;
    proxy_read_timeout 25s;

    proxy_cache off;
    proxy_no_cache 1;
    access_log off;

    # 避免 Nginx 上游错误日志记录含签名或收银台 token 的完整请求。
    # 代价：此 location 的 Nginx 错误日志也不可用于排障。
    error_log /dev/null crit;

    add_header Cache-Control "no-store" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy same-origin always;
    add_header X-Frame-Options DENY always;
}
```

### 7.1 proxy_pass 末尾不加斜杠

这里应写：

```nginx
proxy_pass http://127.0.0.1:8080;
```

这样 `/payments/zpay/notify` 会以完整路径转发到上游。若写成 `proxy_pass http://127.0.0.1:8080/;`，匹配到的 `/payments/` 前缀会被替换，应用收到的可能是 `/zpay/notify`，从而返回 404。

### 7.2 不要在支付回调前增加登录或挑战页

ZPAY 必须可以直接访问通知地址。不要给该路径添加登录验证、验证码、浏览器挑战、额外跳转或页面缓存，也不要通过静态文件规则拦截它。

实际通知地址：

```text
https://xxxx.com/payments/zpay/notify
```

支付返回地址：

```text
https://xxxx.com/payments/zpay/return
```

支付返回页面不作为到账证明；应用仍须验签并核对商户、订单、金额和支付状态。

### 7.3 响应头与来源校验

支付 location 使用 `Referrer-Policy same-origin`，与站点其他页面的 `no-referrer` 分开。保留该设置，避免旧 checkout 表单的来源校验受影响。

Nginx 的 `add_header` 有继承规则，location 自行声明响应头后不能想当然地认为 server 中的全部头仍然生效，因此支付 location 明确列出所需响应头。上游应用自身也会设置安全头，实际排障时应查看最终响应。

### 7.4 此示例只适用于宿主机 Nginx

Nginx 在宿主机上才能用 `127.0.0.1:8080` 访问宿主机发布端口。若 Nginx 本身也在 Docker 容器中，`127.0.0.1` 指向 Nginx 容器自身，需要按容器网络重新设计上游地址，不能照搬本文。

如果域名前还有 CDN 或其他代理，应另行核对真实 IP 信任链；不要未经可信来源校验就相信客户端传来的 `X-Forwarded-For`。

## 8. RemnaShop-Pro 环境变量

在 `/opt/remnashop-pro/.env` 中配置：

```dotenv
PAYMENT_PUBLIC_URL=https://xxxx.com
HTTP_PORT=8080
HTTP_TRUSTED_PROXIES=172.20.0.1/32

ZPAY_PID=填写真实商户ID
ZPAY_KEY=填写真实商户密钥
ZPAY_CID=
```

上面的商户号和密钥只是文字占位符，不可原样用于启动。真实 `.env` 不要上传 GitHub。

`172.20.0.1/32` 是此前在该测试环境确认过的代理来源，不能当成所有 Docker 主机的固定值。先查看当前 HTTP 容器网络：

```bash
docker inspect remnashop-http --format '{{range $name, $net := .NetworkSettings.Networks}}{{printf "%s gateway=%s\n" $name $net.Gateway}}{{end}}'
```

网关地址只是排查线索，最终应以 HTTP 容器实际看到的直接代理来源为准。网络重建后重新核对，不要设置为 `0.0.0.0/0` 或 `::/0`。

`PAYMENT_PUBLIC_URL` 只填 HTTPS origin，不带 `/payments/`、查询参数或尾部业务路径。更改环境变量后，单纯 `docker compose restart` 不会更新容器环境；按项目原有安装/更新入口重建服务。

## 9. HTML 首页

文件：`/opt/service-pay/web/index.html`

下面是测试机实际读取到的静态首页，调整为便于阅读的缩进，文案和功能不变：

```html
<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>服务支付系统 · 测试环境</title>
    <style>
        body {
            margin: 0;
            background: #f3f6fb;
            color: #243047;
            font: 16px/1.7 system-ui, sans-serif;
        }
        main {
            max-width: 720px;
            margin: 12vh auto;
            padding: 40px;
            background: white;
            border-radius: 16px;
            box-shadow: 0 8px 40px #15244412;
        }
        h1 { font-size: 28px; }
        small { color: #64748b; }
        .tag {
            display: inline-block;
            padding: 3px 12px;
            border-radius: 20px;
            background: #fff2cf;
            color: #805d00;
        }
        code {
            background: #eef2f8;
            padding: 2px 5px;
        }
    </style>
</head>
<body>
    <main>
        <span class="tag">仅供测试 · 尚未开放支付</span>
        <h1>服务支付系统</h1>
        <p>Nginx 与 HTTPS 基础环境已就绪。</p>
        <p>注册登录、按次服务费、支付宝二维码支付及订单查询功能尚在开发阶段。当前页面不接受付款。</p>
        <small>后续按月收费将另行确认；不代表已开通自动续费。</small>
    </main>
</body>
</html>
```

该页面属于 service-pay 的占位首页，没有 JavaScript 下单、二维码生成、登录或支付状态查询功能。“尚未开放支付”仅是这张静态页面的文案，不能用来判断 RemnaShop-Pro 机器人支付是否可用；机器人在线支付及回调自动开通已经实际验证正常。

如果只需要中性的站点首页，可自行修改标题和提示文字；不要把它改成收集商户密钥或让浏览器自行签名的付款页。

写入 HTML 后可设置为普通可读文件：

```bash
sudo chmod 644 /opt/service-pay/web/index.html
```

同时确保 Nginx 工作进程可以访问其父目录。已有首页时先备份，不要直接覆盖其他项目的前端文件。

## 10. 启用并检查

1. 保存上述日志格式、站点配置、支付 snippet 和 HTML 文件。
2. 确认证书路径存在，支付服务正在监听宿主机回环端口。
3. 建立站点链接。若同名文件或链接已存在，先检查目标，不要强制覆盖：

```bash
sudo ln -s /etc/nginx/sites-available/service-pay.conf /etc/nginx/sites-enabled/service-pay.conf
```

测试机该链接已经存在，不需要重复执行。

检查语法：

```bash
sudo nginx -t
```

只有检查成功后才重载：

```bash
sudo systemctl reload nginx
```

本文编写过程中没有对服务器执行配置修改或重载。若 `nginx -t` 报错，应先修复错误，不要继续 reload。

## 11. 分层验证

### 11.1 容器状态与 HTTP 服务

```bash
cd /opt/remnashop-pro
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
```

预期 HTTP 服务返回：

```json
{"status": "ok", "service": "http"}
```

### 11.2 Nginx 与 HTTPS

```bash
curl -I https://xxxx.com/
curl -fsS https://xxxx.com/healthz
```

公网 `/healthz` 预期为 `nginx-ok`，与容器 HTTP 服务的 `/healthz` 是两个不同检查。

### 11.3 支付通知路径

不附带签名的请求可以用来检查路由是否到达应用：

```bash
curl -i https://xxxx.com/payments/zpay/notify
```

支付已完整配置时，无签名请求应被应用拒绝，通常返回 HTTP 400 和 `fail`；这不是故障，也不是有效到账通知。未配置支付时可能返回 503。不要为了让这个命令返回 `success` 而关闭验签。

不要伪造成功通知来验证开通。完整业务验收应使用真实小额订单，分别确认付款、可信到账记录和最终交付。

## 12. 常见问题

| 现象 | 检查方向 |
|---|---|
| `nginx -t` 提示未知日志格式 | 确认 `service-pay-logging.conf` 在 `http` 上下文中、且先于站点被加载 |
| 找不到证书文件 | 先准备对应域名证书，再启用 HTTPS server 块 |
| `/payments/` 返回 502 | 检查 `remnashop-http` 健康和宿主机 `127.0.0.1:8080` 是否可访问 |
| 通知路径返回 404 | 检查 `proxy_pass` 是否错误添加尾斜杠、是否被其他 location 或站点接管 |
| 首页提示“尚未开放支付” | 这是 service-pay 静态占位文案，与机器人 ZPAY 支付状态无直接关系 |
| 旧 checkout 表单返回 403 | 检查最终 `Referrer-Policy`、浏览器 `Origin`、域名与 `PAYMENT_PUBLIC_URL` 是否一致 |
| 机器人付款按钮仍指向本站 | 重新进入“我的订单 → 订单详情”生成新消息；已发送的旧按钮不会自动更新 |
| USDT 不显示 | 检查后台开关、网络/地址及套餐独立 USDT 价格，不由 Nginx 控制 |
| 公网 `/healthz` 正常但支付异常 | 该路径只检查 Nginx，继续检查容器、支付配置及订单状态 |

支付路由当前不写访问日志，且屏蔽 Nginx location 错误日志。排障优先检查容器状态及脱敏业务日志，避免把完整回调 URL、支付签名、商户密钥和 checkout token 发到公开仓库。本站日志也看不到浏览器访问 ZPAY 后的全部跳转过程。

## 13. 上传 GitHub 的建议

可将本文件上传为：

```text
docs/nginx-reverse-proxy.md
```

再在项目 README 添加：

```markdown
- [Nginx 反向代理与 HTML 首页配置](docs/nginx-reverse-proxy.md)
```

本文代码块已经包含所需 conf 和 HTML，可按路径分别保存。不要把真实 `.env`、`/etc/letsencrypt/` 私钥、支付回调原始日志或服务器登录密码一起上传。

## 14. 参考资料

- 本文的站点、日志与支付 snippet 来自 2026-11-15 测试机 Nginx 配置备份；HTML 从测试机 `/opt/service-pay/web/index.html` 只读取得。
- [Nginx proxy_pass 官方说明](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)：转发 URI 与 location 前缀替换规则。
- [Nginx add_header 官方说明](https://nginx.org/en/docs/http/ngx_http_headers_module.html#add_header)：响应头及继承规则。
- [Nginx HTTP/2 官方说明](https://nginx.org/en/docs/http/ngx_http_v2_module.html)：按安装版本选择 HTTP/2 配置写法。
