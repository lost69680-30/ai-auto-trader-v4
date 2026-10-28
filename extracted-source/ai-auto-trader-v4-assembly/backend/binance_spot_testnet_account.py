"""Read-only Binance Spot Testnet account status helper.

This module is intentionally limited to the fixed Spot Testnet host and GET /api/v3/account.
It has no order placement or cancellation functions.
"""
import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TESTNET_BASE_URL = "https://testnet.binance.vision"


class BinanceSpotTestnetAccountClient:
    def __init__(self, api_key=None, api_secret=None, opener=None, clock_ms=None):
        self.api_key = api_key if api_key is not None else os.getenv("BINANCE_SPOT_TESTNET_API_KEY", "")
        self.api_secret = api_secret if api_secret is not None else os.getenv("BINANCE_SPOT_TESTNET_API_SECRET", "")
        self._opener = opener or urlopen
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def account_summary(self):
        base = {
            "connected": False,
            "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
            "status": "NOT_CONFIGURED",
            "balances": [],
            "orders_authorized": False,
        }
        if not self.api_key or not self.api_secret:
            return base
        try:
            params = urlencode({"timestamp": self._clock_ms(), "recvWindow": 5000})
            signature = hmac.new(self.api_secret.encode(), params.encode(), hashlib.sha256).hexdigest()
            request = Request(
                f"{TESTNET_BASE_URL}/api/v3/account?{params}&signature={signature}",
                headers={"X-MBX-APIKEY": self.api_key, "Accept": "application/json"},
                method="GET",
            )
            with self._opener(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            balances = payload.get("balances", [])
            safe_balances = []
            for item in balances:
                asset = item.get("asset")
                free = item.get("free")
                locked = item.get("locked")
                if not isinstance(asset, str) or not isinstance(free, str) or not isinstance(locked, str):
                    raise ValueError("invalid balance payload")
                safe_balances.append({"asset": asset, "free": free, "locked": locked})
            return {
                "connected": True,
                "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
                "status": "CONNECTED",
                "balances": safe_balances,
                "orders_authorized": False,
            }
        except HTTPError as exc:
            # Return only a numeric status; never expose Binance response bodies or credentials.
            return {
                "connected": False,
                "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
                "status": f"BINANCE_HTTP_{exc.code}" if 400 <= exc.code <= 599 else "BINANCE_HTTP_ERROR",
                "balances": [],
                "orders_authorized": False,
            }
        except (URLError, OSError):
            safe_status = "NETWORK_ERROR"
        except (ValueError, TypeError, json.JSONDecodeError):
            safe_status = "INVALID_RESPONSE"
        except Exception:
            safe_status = "UNEXPECTED_ERROR"
        return {
            "connected": False,
            "mode": "BINANCE_SPOT_TESTNET_READ_ONLY",
            "status": safe_status,
            "balances": [],
            "orders_authorized": False,
        }
