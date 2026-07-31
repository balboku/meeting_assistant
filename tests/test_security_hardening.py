from __future__ import annotations

import ipaddress
import unittest

from tests.test_regressions import asgi_request


class SecurityHardeningTests(unittest.TestCase):
    def test_same_network_browser_requires_explicit_trust_opt_in(self) -> None:
        import backend.main as main

        original_trust = main.TRUST_LOCAL_NETWORK
        original_networks = main.TRUSTED_LOCAL_NETWORKS
        main.TRUST_LOCAL_NETWORK = True
        main.TRUSTED_LOCAL_NETWORKS = (
            ipaddress.ip_network("192.168.1.0/24"),
        )
        try:
            response = asgi_request(
                main.app,
                "GET",
                "/health",
                asgi_client=("192.168.1.50", 456),
            )
            forged = asgi_request(
                main.app,
                "GET",
                "/health",
                headers={"X-Forwarded-For": "192.168.1.50"},
                asgi_client=("203.0.113.20", 456),
            )
        finally:
            main.TRUST_LOCAL_NETWORK = original_trust
            main.TRUSTED_LOCAL_NETWORKS = original_networks

        self.assertEqual(response.status_code, 200)
        self.assertEqual(forged.status_code, 403)


if __name__ == "__main__":
    unittest.main()
