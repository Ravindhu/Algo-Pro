"""
OpenAlgo Strategy Module for TradingView Integration
"""

from typing import Optional
import httpx

class Strategy:
    def __init__(self, host_url: str, webhook_id: str):
        """
        Initialize strategy with host URL and webhook ID
        
        Args:
            host_url (str): OpenAlgo server URL (e.g., "http://127.0.0.1:5000")
            webhook_id (str): Strategy's webhook ID from OpenAlgo
        """
        self._host_url = host_url.rstrip('/')
        self._webhook_id = webhook_id
        self._webhook_url = None
        # Reuse one keep-alive connection for repeated webhook posts instead
        # of opening a fresh socket per signal.
        self._client = httpx.Client(
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=120.0,
            ),
        )

    def close(self):
        """Close the underlying HTTP client and release pooled connections."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @property
    def webhook_url(self) -> str:
        """
        Cached property for webhook URL to avoid reconstructing it every time
        """
        if self._webhook_url is None:
            self._webhook_url = f"{self._host_url}/strategy/webhook/{self._webhook_id}"
        return self._webhook_url

    def strategyorder(self, symbol: str, action: str, position_size: Optional[int] = None) -> dict:
        """
        Send a strategy order via webhook to OpenAlgo.
        The strategy mode (LONG_ONLY, SHORT_ONLY, BOTH) is configured in OpenAlgo.
        
        Args:
            symbol (str): Trading symbol (e.g., "RELIANCE", "NIFTY")
            action (str): Order action ("BUY" or "SELL")
            position_size (Optional[int]): Position size, required for BOTH mode
            
        Returns:
            dict: Response from the webhook request
            
        Raises:
            requests.exceptions.RequestException: If the webhook request fails
        """
        # Prepare message
        post_message = {
            "symbol": symbol,
            "action": action.upper()
        }
        
        if position_size is not None:
            post_message["position_size"] = str(position_size)
            
        try:
            response = self._client.post(self.webhook_url, json=post_message)
            response.raise_for_status()  # Raise exception for bad status codes
            return response.json()
        except httpx.HTTPError as e:
            print(f"Strategy order failed: {e}")
            raise
