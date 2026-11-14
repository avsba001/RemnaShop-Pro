"""HTTP liveness + payment-ledger accessibility; not gateway/HTTPS validation."""
import urllib.request

with urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
print('ok')
