# -*- coding: utf-8 -*-
"""
OpenAlgo REST API Documentation - Telegram Notification Methods
    https://docs.openalgo.in
"""

import httpx
from .base import BaseAPI

class TelegramAPI(BaseAPI):
    """
    Telegram notification API methods for OpenAlgo.
    Inherits from the BaseAPI class.
    """

    def _make_request(self, endpoint, payload):
        """Make HTTP request with proper error handling"""
        url = self.base_url + endpoint
        try:
            response = self.client.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            return self._handle_response(response)
        except httpx.TimeoutException:
            return {
                'status': 'error',
                'message': 'Request timed out. The server took too long to respond.',
                'error_type': 'timeout_error'
            }
        except httpx.ConnectError:
            return {
                'status': 'error',
                'message': 'Failed to connect to the server. Please check if the server is running.',
                'error_type': 'connection_error'
            }
        except httpx.HTTPError as e:
            return {
                'status': 'error',
                'message': f'HTTP error occurred: {str(e)}',
                'error_type': 'http_error'
            }
        except Exception as e:
            return {
                'status': 'error',
                'message': f'An unexpected error occurred: {str(e)}',
                'error_type': 'unknown_error'
            }

    def _handle_response(self, response):
        """Helper method to handle API responses"""
        try:
            if response.status_code != 200:
                return {
                    'status': 'error',
                    'message': f'HTTP {response.status_code}: {response.text}',
                    'code': response.status_code,
                    'error_type': 'http_error'
                }

            data = response.json()
            if data.get('status') == 'error':
                return {
                    'status': 'error',
                    'message': data.get('message', 'Unknown error'),
                    'code': response.status_code,
                    'error_type': 'api_error'
                }
            return data

        except ValueError:
            return {
                'status': 'error',
                'message': 'Invalid JSON response from server',
                'raw_response': response.text,
                'error_type': 'json_error'
            }
        except Exception as e:
            return {
                'status': 'error',
                'message': str(e),
                'error_type': 'unknown_error'
            }

    def telegram(self, *, username, message, priority=5, **kwargs):
        """
        Send Custom Alert Messages to Telegram Users.

        Prerequisites:
        1. Telegram Bot Must Be Running
           - Start the bot from OpenAlgo Telegram settings
           - Bot must be active to send alerts
        2. User Must Be Linked
           - User must have linked their account using /link command in Telegram
           - Username must match your OpenAlgo login username (NOT Telegram username)
        3. Valid API Key
           - API key must be active and valid

        Parameters:
        - username (str): OpenAlgo login username (the username used to login to OpenAlgo app,
                         NOT Telegram username). Required.
        - message (str): Alert message to send. Required.
                        Supports emojis, markdown, and line breaks (use \\n).
                        Maximum length: 4096 characters (Telegram limit).
        - priority (int, optional): Message priority (1-10, higher = more urgent). Defaults to 5.
                                   Priority Levels:
                                   - 1-3: Low Priority (General updates, market news)
                                   - 4-6: Normal Priority (Trade signals, daily summaries)
                                   - 7-8: High Priority (Price alerts, position updates)
                                   - 9-10: Urgent (Stop loss hits, risk alerts)

        Returns:
        dict: JSON response containing:
            - status: success/error
            - message: Response message

        Message Formatting:
        - Bold: *text* or **text**
        - Italic: _text_ or __text__
        - Code: `text`
        - Pre-formatted: ```text```
        - Links: [text](url)
        - Line breaks: Use \\n in message string
        - Emojis: Standard Unicode emojis supported

        Example:
            # Basic notification
            result = api.telegram(
                username="john_trader",
                message="NIFTY crossed 24000! Consider taking profit on long positions."
            )

            # High priority alert
            result = api.telegram(
                username="john_trader",
                message="🚨 URGENT: Stop loss hit on BANKNIFTY position!",
                priority=10
            )

            # Multi-line trading summary
            result = api.telegram(
                username="john_trader",
                message="📊 Daily Trading Summary\\n"
                        "─────────────────────\\n"
                        "✅ Winning Trades: 8\\n"
                        "❌ Losing Trades: 2\\n"
                        "💰 Net P&L: +₹15,450\\n"
                        "📈 Win Rate: 80%\\n\\n"
                        "🎯 Great day! Keep it up!",
                priority=5
            )

            # Price alert
            result = api.telegram(
                username="trader_123",
                message="🔔 Price Alert: RELIANCE reached target price ₹2,850",
                priority=8
            )

            # Strategy signal
            result = api.telegram(
                username="algo_trader",
                message="📈 BUY Signal: RSI oversold on NIFTY 24000 CE\\n"
                        "Entry: ₹145.50\\n"
                        "Target: ₹165.00\\n"
                        "SL: ₹138.00",
                priority=9
            )

            # Risk management alert
            result = api.telegram(
                username="trader_123",
                message="⚠️ Risk Alert: Daily loss limit reached (-₹25,000)\\n"
                        "No new positions recommended.",
                priority=10
            )

            # Trade execution confirmation
            result = api.telegram(
                username="trader_123",
                message="✅ Order Executed\\n"
                        "Symbol: BANKNIFTY 48000 CE\\n"
                        "Action: BUY\\n"
                        "Qty: 30\\n"
                        "Price: ₹245.75\\n"
                        "Total: ₹7,372.50",
                priority=7
            )

        Common Use Cases:
        1. Price Alert Notifications
        2. Strategy Signal Alerts
        3. Risk Management Alerts
        4. Market Update Notifications
        5. Trade Execution Confirmation
        6. Technical Indicator Alerts
        7. Daily/Weekly Trading Summaries
        8. Position Updates

        Error Messages:
        - "Invalid or missing API key": API key is incorrect or expired
        - "User not found or not linked to Telegram": User hasn't linked account OR using wrong username
          (Use OpenAlgo login username, not Telegram username. Link via /link command)
        - "Username and message are required": Missing required fields
        - "Failed to send notification": Bot is not running

        Rate Limiting:
        - Limit: 30 requests per minute per user
        - Response: 429 status code if limit exceeded

        Notes:
        - Username is your OpenAlgo login username (case-sensitive), NOT @telegram_handle
        - User must link account via /link command in Telegram bot first
        - Bot must be running in OpenAlgo settings to send messages
        - Messages are queued if delivery fails temporarily
        - Keep messages under 4096 characters (Telegram limit)
        """
        payload = {
            "apikey": self.api_key,
            "username": username,
            "message": message,
            "priority": priority
        }
        # Add any additional kwargs
        for key, value in kwargs.items():
            if value is not None:
                payload[key] = value

        return self._make_request("telegram/notify", payload)
