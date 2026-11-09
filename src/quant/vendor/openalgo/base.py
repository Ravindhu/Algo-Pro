# -*- coding: utf-8 -*-
"""
OpenAlgo REST API Documentation - Base API Class
    https://docs.openalgo.in
"""

import httpx

class BaseAPI:
    """
    Base class to handle all the API calls to OpenAlgo.
    """

    def __init__(self, api_key, host="http://127.0.0.1:5000", version="v1", timeout=120.0):
        """
        Initialize the api object with an API key and optionally a host URL and API version.

        Attributes:
        - api_key (str): User's API key.
        - host (str): Base URL for the API endpoints. Defaults to localhost.
        - version (str): API version. Defaults to "v1".
        - timeout (float): Request timeout in seconds. Defaults to 120.0 seconds.
        """
        self.api_key = api_key
        self.base_url = f"{host}/api/{version}/"
        self.headers = {
            'Content-Type': 'application/json'
        }
        self.timeout = timeout
        # Single connection-pooled client reused by every REST call. Without
        # this, each request went through the module-level httpx.post/get,
        # which opens and tears down a fresh TCP connection per call, leaving
        # thousands of sockets in TIME_WAIT over a trading session and
        # eventually exhausting ephemeral ports.
        self.client = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=120.0,
            ),
        )

    def close(self):
        """Close the shared HTTP client and release pooled connections."""
        client = getattr(self, "client", None)
        if client is not None:
            client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
