# -*- coding: utf-8 -*-
"""
OpenAlgo REST API Documentation - Utilities Methods
    https://docs.openalgo.in
"""

import httpx
from datetime import datetime
from .base import BaseAPI


class UtilitiesAPI(BaseAPI):
    """
    Utilities API methods for OpenAlgo.
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
                'message': f'Error processing response: {str(e)}',
                'error_type': 'unknown_error'
            }

    def holidays(self, year=None):
        """
        Get market holidays for a specific year.

        Args:
            year (int, optional): Year for holiday data (2020-2050).
                                  Defaults to current year if not provided.

        Returns:
            dict: Response containing holiday data with the following structure:
                {
                    "status": "success",
                    "year": 2025,
                    "timezone": "Asia/Kolkata",
                    "data": [
                        {
                            "date": "2025-02-26",
                            "description": "Maha Shivaratri",
                            "holiday_type": "TRADING_HOLIDAY",
                            "closed_exchanges": ["NSE", "BSE", "NFO", "BFO", "CDS", "BCD"],
                            "open_exchanges": [
                                {
                                    "exchange": "MCX",
                                    "start_time": 1740549000000,
                                    "end_time": 1740602700000
                                }
                            ]
                        }
                    ]
                }
        """
        payload = {
            "apikey": self.api_key
        }

        if year is not None:
            payload["year"] = year

        return self._make_request("market/holidays", payload)

    def timings(self, date=None):
        """
        Get market timings for a specific date.

        Args:
            date (str, optional): Date in YYYY-MM-DD format.
                                  Defaults to current date if not provided.

        Returns:
            dict: Response containing market timing data with the following structure:
                {
                    "status": "success",
                    "data": [
                        {
                            "exchange": "NSE",
                            "start_time": 1745984700000,
                            "end_time": 1746007200000
                        },
                        {
                            "exchange": "BSE",
                            "start_time": 1745984700000,
                            "end_time": 1746007200000
                        },
                        {
                            "exchange": "MCX",
                            "start_time": 1745983800000,
                            "end_time": 1746037500000
                        }
                    ]
                }

                Note: Empty data array indicates market is closed (holiday).
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        payload = {
            "apikey": self.api_key,
            "date": date
        }

        return self._make_request("market/timings", payload)
