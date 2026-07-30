from __future__ import annotations

import unittest

from tests.test_regressions import asgi_request


class SecurityHardeningTests(unittest.TestCase):
    def test_same_network_browser_requires_explicit_trust_opt_in(self) -> None:
        import backend.main as main

        original_trust = main.TRUST_LOCAL_NETWORK
        main.TRUST_LOCAL_NETWORK = True
        try:
            response = asgi_request(
                main.app,
                "GET",
                "/health",
                headers={"X-Forwarded-For": "192.168.1.50"},
            )
        finally:
            main.TRUST_LOCAL_NETWORK = original_trust

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
