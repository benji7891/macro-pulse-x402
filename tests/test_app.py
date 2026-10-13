"""
Tests for Macro Pulse API endpoints.

Tests verify:
- Valid ISO country codes (US/GB/DE/JP) return 402 (payment required, not error)
- Batch dedup and case handling
- >8 countries rejected with 400 before payment
- Malformed country codes rejected with 400 before payment
- Fake country codes (XX, ZZZ) rejected with 400 before payment
- World Bank aggregate codes (EUU, WLD) rejected with 400 before payment
- No bazaar extension warnings at startup
"""

import pytest
import warnings
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client with mocked environment."""
    import os
    os.environ['PAY_TO_ADDRESS'] = '0xb4a9238c9400a7f1bb7924606ff2ea634a0f3ec4'
    os.environ['NETWORK'] = 'eip155:84532'
    os.environ['PUBLIC_BASE_URL'] = 'https://macro-pulse-x402.onrender.com'
    
    from app import app
    return TestClient(app)


@pytest.fixture
def mock_world_bank_data():
    """Mock World Bank API responses for testing."""
    return [
        {"date": "2024", "value": 2.5},
        {"date": "2023", "value": 2.1},
        {"date": "2022", "value": 1.9},
        {"date": "2021", "value": 1.8},
        {"date": "2020", "value": 1.5},
        {"date": "2019", "value": 2.0},
    ]


class TestCountryCodeValidation:
    """Tests for country code validation logic."""

    def test_valid_iso_alpha2_codes(self):
        """Valid ISO alpha-2 codes should pass validation."""
        from app import validate_country_code
        
        for code in ['US', 'GB', 'DE', 'JP', 'FR', 'CN', 'BR']:
            is_valid, error = validate_country_code(code)
            assert is_valid, f"Expected {code} to be valid, got error: {error}"
            assert error is None

    def test_valid_iso_alpha3_codes(self):
        """Valid ISO alpha-3 codes should pass validation."""
        from app import validate_country_code
        
        for code in ['USA', 'GBR', 'DEU', 'JPN', 'FRA', 'CHN', 'BRA']:
            is_valid, error = validate_country_code(code)
            assert is_valid, f"Expected {code} to be valid, got error: {error}"
            assert error is None

    def test_fake_country_codes_rejected(self):
        """Fake/nonexistent country codes should be rejected."""
        from app import validate_country_code
        
        for code in ['XX', 'ZZZ', 'AAA', 'QQ', 'XYZ', 'ZZ']:
            is_valid, error = validate_country_code(code)
            assert not is_valid, f"Expected {code} to be invalid"
            assert error is not None
            assert 'not a valid ISO 3166-1 country code' in error

    def test_world_bank_aggregates_rejected(self):
        """World Bank aggregate/region codes should be rejected."""
        from app import validate_country_code
        
        for code in ['EUU', 'WLD', 'SSA', 'NAC', 'EMU', 'HIC', 'LIC', 'MIC']:
            is_valid, error = validate_country_code(code)
            assert not is_valid, f"Expected {code} (World Bank aggregate) to be invalid"
            assert error is not None
            assert 'World Bank aggregate' in error

    def test_case_insensitive_validation(self):
        """Validation should be case-insensitive."""
        from app import validate_country_code
        
        for code in ['us', 'Us', 'uS', 'US']:
            is_valid, error = validate_country_code(code)
            assert is_valid, f"Expected {code} to be valid"


class TestSingleCountryEndpoint:
    """Tests for the single-country endpoint validation."""

    def test_valid_country_returns_402(self, client, mock_world_bank_data):
        """Valid country codes should return 402 (payment required), not 400."""
        with patch("app.fetch_indicator", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_world_bank_data
            for code in ['US', 'GB', 'DE', 'JP']:
                response = client.get(f"/macro-pulse/{code}")
                assert response.status_code == 402, f"Expected 402 for {code}, got {response.status_code}"

    def test_fake_code_returns_400(self, client):
        """Fake country codes should return 400 before payment challenge."""
        response = client.get("/macro-pulse/XX")
        assert response.status_code == 400
        data = response.json()
        assert 'error' in data
        assert 'invalid_country_code' == data['error']

    def test_zzz_fake_code_returns_400(self, client):
        """ZZZ fake code should return 400 before payment."""
        response = client.get("/macro-pulse/ZZZ")
        assert response.status_code == 400
        data = response.json()
        assert 'not a valid ISO 3166-1 country code' in data['message']

    def test_world_bank_aggregate_returns_400(self, client):
        """World Bank aggregates should return 400 before payment."""
        response = client.get("/macro-pulse/EUU")
        assert response.status_code == 400
        data = response.json()
        assert 'World Bank aggregate' in data['message']

    def test_wld_aggregate_returns_400(self, client):
        """WLD (World) aggregate should return 400 before payment."""
        response = client.get("/macro-pulse/WLD")
        assert response.status_code == 400
        data = response.json()
        assert 'World Bank aggregate' in data['message']

    def test_case_insensitive_input(self, client):
        """Country code input should be case-insensitive."""
        # Both should get 402 (valid code, payment required)
        response_upper = client.get("/macro-pulse/US")
        response_lower = client.get("/macro-pulse/us")
        response_mixed = client.get("/macro-pulse/Us")
        
        assert response_upper.status_code == 402
        assert response_lower.status_code == 402
        assert response_mixed.status_code == 402


class TestBatchEndpoint:
    """Tests for the batch endpoint validation."""

    def test_batch_valid_codes_returns_402(self, client):
        """Valid batch codes should return 402 (payment required)."""
        response = client.get("/macro-pulse-batch/US,GB,DE,JP")
        assert response.status_code == 402

    def test_batch_dedupe_same_code(self, client):
        """Batch should handle duplicate codes (still valid, just dedupe)."""
        response = client.get("/macro-pulse-batch/US,US,US")
        assert response.status_code == 402

    def test_batch_case_handling(self, client):
        """Batch should handle mixed case codes."""
        response = client.get("/macro-pulse-batch/us,GB,de,Jp")
        assert response.status_code == 402

    def test_batch_exceeds_max_returns_400(self, client):
        """More than 8 countries should return 400 before payment."""
        codes = "US,GB,DE,JP,FR,CN,BR,IN,AU"  # 9 countries
        response = client.get(f"/macro-pulse-batch/{codes}")
        assert response.status_code == 400
        data = response.json()
        assert 'too_many_countries' == data['error']

    def test_batch_fake_code_returns_400(self, client):
        """Batch with fake code should return 400 before payment."""
        response = client.get("/macro-pulse-batch/US,XX,GB")
        assert response.status_code == 400
        data = response.json()
        assert 'invalid_country_code' == data['error']

    def test_batch_aggregate_code_returns_400(self, client):
        """Batch with World Bank aggregate should return 400 before payment."""
        response = client.get("/macro-pulse-batch/US,EUU,GB")
        assert response.status_code == 400
        data = response.json()
        assert 'World Bank aggregate' in data['message']

    def test_batch_empty_returns_404(self, client):
        """Empty batch path returns 404 (FastAPI routing)."""
        response = client.get("/macro-pulse-batch/")
        assert response.status_code == 404


class TestNumericRounding:
    """Tests for numeric output rounding."""

    def test_compute_momentum_rounds_to_2_decimals(self):
        """compute_momentum should return values rounded to 2 decimals."""
        from app import compute_momentum
        
        series = [
            {"value": 3.456789},
            {"value": 1.234567},
            {"value": 2.345678},
        ]
        result = compute_momentum(series)
        assert result is not None
        assert result == round(result, 2)
        decimal_places = len(str(result).split('.')[-1]) if '.' in str(result) else 0
        assert decimal_places <= 2


class TestDocstring:
    """Tests for docstring accuracy."""

    def test_compute_momentum_docstring(self):
        """compute_momentum docstring should not mention std-dev units."""
        from app import compute_momentum
        
        docstring = compute_momentum.__doc__
        assert docstring is not None
        assert 'std-dev' not in docstring.lower()
        assert 'standard deviation' not in docstring.lower()
        assert 'latest value minus average' in docstring.lower()


class TestBazaarExtension:
    """Tests for bazaar extension configuration."""

    def test_no_bazaar_warnings_at_startup(self):
        """App startup should not produce bazaar extension warnings."""
        import os
        os.environ['PAY_TO_ADDRESS'] = '0xb4a9238c9400a7f1bb7924606ff2ea634a0f3ec4'
        os.environ['NETWORK'] = 'eip155:84532'
        os.environ['PUBLIC_BASE_URL'] = 'https://macro-pulse-x402.onrender.com'
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            import importlib
            import app
            importlib.reload(app)
            
            bazaar_warnings = [
                warning for warning in w 
                if 'bazaar' in str(warning.message).lower()
            ]
            assert len(bazaar_warnings) == 0, f"Found bazaar warnings: {[str(w.message) for w in bazaar_warnings]}"


class TestDiscoveryManifest:
    """Tests for discovery manifest."""

    def test_x402_manifest_has_resources(self, client):
        """x402 manifest should have resources with correct pricing."""
        response = client.get("/.well-known/x402")
        assert response.status_code == 200
        data = response.json()
        assert "resources" in data
        assert len(data["resources"]) >= 2
        
        for resource in data["resources"]:
            if "macro-pulse/{country_code}" in resource.get("resource", ""):
                assert resource["price"] == "$0.02"
            if "macro-pulse-batch" in resource.get("resource", ""):
                assert resource["price"] == "$0.05"


class TestDebugLoggingRemoved:
    """Tests to verify debug logging was removed."""

    def test_no_debug_wrapper_installation(self):
        """App should not have debug wrappers installed."""
        import os
        os.environ['PAY_TO_ADDRESS'] = '0xb4a9238c9400a7f1bb7924606ff2ea634a0f3ec4'
        os.environ['NETWORK'] = 'eip155:84532'
        os.environ['PUBLIC_BASE_URL'] = 'https://macro-pulse-x402.onrender.com'
        
        import importlib
        import app
        importlib.reload(app)
        
        server = app.server
        assert hasattr(server, 'find_matching_requirements')
        method_name = server.find_matching_requirements.__name__ if hasattr(server.find_matching_requirements, '__name__') else ''
        assert 'debug' not in method_name.lower()
