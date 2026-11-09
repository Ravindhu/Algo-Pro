# -*- coding: utf-8 -*-
"""
OpenAlgo REST API Documentation - WhatsApp Notification Methods
    https://docs.openalgo.in
"""

import httpx
from .base import BaseAPI


class WhatsAppAPI(BaseAPI):
    """
    WhatsApp notification API methods for OpenAlgo.
    Inherits from the BaseAPI class.

    Surface mirror of TelegramAPI with the richer fields the WhatsApp
    endpoint supports (multi-recipient broadcast capped at 5, image and
    document attachments). The SDK keeps a single ergonomic call —
    ``client.whatsapp("message")`` — and translates trader-facing
    parameters into the exact JSON shape the OpenAlgo server expects.
    """

    def _make_request(self, endpoint, payload):
        """Make HTTP request with proper error handling."""
        url = self.base_url + endpoint
        try:
            response = self.client.post(
                url, json=payload, headers=self.headers, timeout=self.timeout
            )
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
        """Helper method to handle API responses."""
        try:
            if response.status_code != 200:
                # Try to surface the JSON body so the trader sees the real
                # reason ("WhatsApp is not paired", "image_path is not allowed",
                # etc.) instead of a bare HTTP code.
                try:
                    body = response.json()
                    return {
                        'status': 'error',
                        'message': body.get('message') or f'HTTP {response.status_code}: {response.text}',
                        'code': response.status_code,
                        'error_type': 'http_error'
                    }
                except ValueError:
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

    def whatsapp(
        self,
        message=None,
        *,
        to=None,
        username=None,
        image=None,
        document=None,
        caption=None,
        filename=None,
        wait_for_delivery=True,
        **kwargs,
    ):
        """
        Send a WhatsApp message via the OpenAlgo paired device.

        One call handles every common case — send to self, to a single
        number, to a small broadcast (max 5 recipients), with text, image,
        or document attachments. The OpenAlgo server must already be paired
        to a WhatsApp account from the ``/whatsapp`` admin page; pairing is
        deliberately not exposed via the API (security: a leaked API key
        must not be able to re-pair the device).

        Prerequisites:
        1. WhatsApp Device Must Be Paired
           - From the OpenAlgo web UI, open ``/whatsapp`` and scan the QR
             code with your phone.
        2. Bot Must Be Connected
           - The bot auto-reconnects on every server boot from the
             encrypted session blob in ``openalgo.db``.
        3. Valid API Key
           - The OpenAlgo API key must be active.

        Recipient (specify exactly one form — defaults to ``self`` if none):
        - ``to`` (str): single E.164 digit string, e.g. ``"919876543210"``.
        - ``to`` (list[str]): up to 5 E.164 digit strings. Anything beyond 5
                              is dropped server-side. This is a WhatsApp
                              ToS-safety guardrail — bulk-messaging patterns
                              can get the paired device unlinked by Meta.
        - ``username`` (str): OpenAlgo username — resolves via the linked-
                              users table on the server.
        - (neither): defaults to ``self: true`` — sends to the paired
                     device's own number (the operator).

        Payload (combine freely):
        - ``message`` (str): plain text body. Max 4096 characters.
        - ``image`` (str): server-local path to an image file. Caption falls
                           back to ``message`` if no explicit caption.
        - ``document`` (str): server-local path to a document (PDF, CSV...).
        - ``caption`` (str): caption for image / follow-up text for document.
        - ``filename`` (str): override the document's display name on the
                              recipient's device.

        Behaviour:
        - ``wait_for_delivery`` (bool, default ``True``): when ``True``, the
          call blocks until WhatsApp confirms delivery and returns a
          per-recipient report. Set to ``False`` for true fire-and-forget;
          the response is a generic "queued" acknowledgement.

        Returns:
            dict: JSON response.

            With ``wait_for_delivery=True`` (default):
                {
                    "status": "success",
                    "message": "Delivered to 1, failed 0",
                    "data": {
                        "sent":    ["<self>"],
                        "failed":  [],
                        "skipped": 0
                    }
                }

            With ``wait_for_delivery=False``:
                {
                    "status": "success",
                    "message": "Queued for 1 recipient(s)",
                    "queued": 1
                }

        Examples:
            # Send to self — simplest case, one positional argument
            api.whatsapp("Build #482 deployed. P&L: +1.2%")

            # Send to a single number
            api.whatsapp("Are you free for a quick call?", to="919876543210")

            # Small broadcast (up to 5 numbers)
            api.whatsapp(
                "Server maintenance in 10 minutes",
                to=["919876543210", "919812345678", "919900112233"],
            )

            # Send a chart image with a caption
            api.whatsapp(
                "NIFTY end-of-day chart",
                to="919876543210",
                image="/srv/charts/nifty_eod.png",
            )

            # Send a daily report PDF
            api.whatsapp(
                "Daily P&L report attached",
                username="alice",
                document="/srv/reports/2026-05-17.pdf",
                filename="summary.pdf",
            )

            # Fire-and-forget for time-critical alerts where you don't need
            # the delivery report
            api.whatsapp(
                "Stop-loss hit on BANKNIFTY!",
                wait_for_delivery=False,
            )

        Common Use Cases:
        1. Trade execution confirmations (self-send)
        2. Strategy signal alerts to your own number
        3. Daily P&L reports as PDF attachments
        4. Multi-account broadcasts to a small client list
        5. Real-time price alerts
        6. End-of-day chart summaries
        7. Risk management notifications

        Error Messages:
        - "WhatsApp is not paired or not connected. Pair the device first
           from the /whatsapp page in OpenAlgo before sending.": Bot is not
           connected. Open ``/whatsapp`` in the OpenAlgo UI and pair via QR.
        - "Username not found or not linked to WhatsApp": The OpenAlgo
           username doesn't resolve to a linked WhatsApp row.
        - "image_path is not allowed" / "document_path is not allowed":
           The path is outside the ``WHATSAPP_ATTACHMENT_ROOTS`` allowlist,
           contains traversal tokens, or points to a sensitive system tree.
        - "Provide at least one of: message, image_path, document_path":
           Empty payload.
        - "Invalid or missing API key": API key is incorrect or expired.

        Rate Limiting:
        - Default: 30 requests per minute. Broadcast (multi-recipient) is
          5 per minute server-side.
        - Response: 429 status code if limit exceeded.

        Notes:
        - WhatsApp is single-user per OpenAlgo deployment — the paired
          device IS the operator. Incoming slash-commands (``/orderbook``,
          ``/positions``, ``/quote``, etc.) only respond to messages the
          operator sends from their own primary phone (gated by
          ``is_from_me=True`` in WhatsApp's multi-device protocol). Random
          contacts cannot drive the bot.
        - Image and document attachments are read from the OpenAlgo
          server's filesystem, not uploaded by the API call. Place files
          in a location under ``WHATSAPP_ATTACHMENT_ROOTS`` (defaults to
          ``<openalgo>/db/attachments/``).
        - This SDK call uses the WhatsApp ``notify`` endpoint, which is
          the only public REST surface for WhatsApp — pairing, start/stop,
          users, config, broadcast, stats, and preferences are admin-only
          behind the session-authed ``/whatsapp`` web UI.
        """
        # Build the payload to match the server's WhatsAppNotify schema.
        # We pick exactly one of self / username / phone / phones based on
        # what the caller supplied. Defaults to self=True so the simplest
        # call — ``api.whatsapp("hello")`` — sends to the operator.
        payload = {"apikey": self.api_key}

        if username:
            payload["username"] = username
        elif isinstance(to, (list, tuple)):
            payload["phones"] = list(to)
        elif isinstance(to, str) and to.strip():
            payload["phone"] = to
        else:
            payload["self"] = True

        if message is not None:
            payload["message"] = message
        if image is not None:
            payload["image_path"] = image
        if document is not None:
            payload["document_path"] = document
        if caption is not None:
            payload["caption"] = caption
        if filename is not None:
            payload["filename"] = filename
        payload["wait_for_delivery"] = bool(wait_for_delivery)

        # Forward any unrecognised kwargs verbatim so future server-side
        # fields work without an SDK release.
        for key, value in kwargs.items():
            if value is not None and key not in payload:
                payload[key] = value

        return self._make_request("whatsapp/notify", payload)
