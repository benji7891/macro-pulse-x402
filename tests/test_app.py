"""
Tests for Macro Pulse API endpoints.

These tests verify:
- Free preview endpoint works for allowed countries
- Free preview returns 402 for non-preview countries
- Basic endpoint logic (without actual payment)
- Premium endpoint logic (without actual payment)
- Discovery manifests are correctly formatted
- Recession risk computation logic
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock


@pytest.fixture
def client():
    """Create test client. Import app inside fixture to avoid startup side effects."""
    from app import app
    return TestClient(app)


@pytest.fixture
def mock_world_bank_data():
    """Mock World Bank API responses for testing."""
    return {
        "gdp_growth": [
            {"date": "2024", "value": 2.5},
            {"date": "2023", "value": 2.1},
            {"date": "2022", "value": 1.9},
        ],
        "inflation": [
            {"date": "2024", "value": 3.2},
            {"date": "2023", "value": 4.1},
            {"date": "2022", "value": 6.5},
        ],
        "unemployment": [
            {"date": "2024", "value": 4.0},
            {"date": "2023", "value": 3.8},
            {"date": "2022", "value": 3.6},
        ],
    }


class TestFreePreviewEndpoint:
    """Tests for the free preview endpoint."""
    
    def test_preview_us_returns_200(self, client, mock_world_bank_data):
        """Preview endpoint should return 200 for US (allowed country)."""
        with patch("app.fetch_indicator", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = lambda code, indicator: mock_world_bank_data.get(
                indicator.split(".")[-1].lower().replace("_zg", "_growth").replace("_zs", ""),
                mock_world_bank_data["gdp_growth"]
            )
            response = client.get("/macro-pulse/preview/US")
            assert response.status_code == 200
            data = response.json()
            assert data["country"] == "US"
            assert "momentum_score" in data
            assert "momentum_label" in data
            assert "_preview" in data
    
    def test_preview_gb_returns_200(self, client, mock_world_bank_data):
        """Preview endpoint should return 200 for GB (allowed country)."""
        with patch("app.fetch_indicator", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_world_bank_data["gdp_growth"]
            response = client.get("/macro-pulse/preview/GB")
            assert response.status_code == 200
            data = response.json()
            assert data["country"] == "GB"
    
    def test_preview_de_returns_200(self, client, mock_world_bank_data):
        """Preview endpoint should return 200 for DE (allowed country)."""
        with patch("app.fetch_indicator", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_world_bank_data["gdp_growth"]
            response = client.get("/macro-pulse/preview/DE")
            assert response.status_code == 200
            data = response.json()
            assert data["country"] == "DE"
    
    def test_preview_japan_returns_402(self, client):
        """Preview endpoint should return 402 for JP (not in preview list)."""
        response = client.get("/macro-pulse/preview/JP")
        assert response.status_code == 402
        assert "Free preview is only available for" in response.json()["detail"]
    
    def test_preview_invalid_code_returns_400(self, client):
        """Preview endpoint should return 400 for invalid country codes."""
        response = client.get("/macro-pulse/preview/INVALID")
        assert response.status_code == 400
    
    def test_preview_case_insensitive(self, client, mock_world_bank_data):
        """Preview endpoint should handle case-insensitive country codes."""
        with patch("app.fetch_indicator", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_world_bank_data["gdp_growth"]
            response = client.get("/macro-pulse/preview/us")  # lowercase
            assert response.status_code == 200


class TestDiscoveryEndpoints:
    """Tests for discovery manifest endpoints."""
    
    def test_x402_manifest_structure(self, client):
        """x402 manifest should have correct structure."""
        response = client.get("/.well-known/x402")
        assert response.status_code == 200
        data = response.json()
        assert "x402Version" in data
        assert data["x402Version"] == 2
        assert "resources" in data
        assert len(data["resources"]) >= 3  # preview, basic, premium, batch
    
    def test_x402_manifest_includes_preview(self, client):
        """x402 manifest should include free preview endpoint."""
        response = client.get("/.well-known/x402")
        data = response.json()
        preview_resources = [r for r in data["resources"] if "preview" in r["resource"]]
        assert len(preview_resources) == 1
        assert preview_resources[0]["price"] == "$0.00"
    
    def test_x402_manifest_includes_premium(self, client):
        """x402 manifest should include premium endpoint."""
        response = client.get("/.well-known/x402")
        data = response.json()
        premium_resources = [r for r in data["resources"] if "premium" in r["resource"]]
        assert len(premium_resources) == 1
        assert premium_resources[0]["price"] == "$0.04"
    
    def test_llms_txt_endpoint(self, client):
        """llms.txt should return text content."""
        response = client.get("/llms.txt")
        assert response.status_code == 200
        assert "Macro Pulse" in response.text
        assert "preview" in response.text.lower()
        assert "premium" in response.text.lower()
    
    def test_ai_plugin_manifest(self, client):
        """ai-plugin.json should have correct structure."""
        response = client.get("/.well-known/ai-plugin.json")
        assert response.status_code == 200
        data = response.json()
        assert data["name_for_model"] == "macro_pulse"
        assert "preview" in data["description_for_model"].lower()
    
    def test_root_json_response(self, client):
        """Root endpoint should return JSON for non-browser requests."""
        response = client.get("/", headers={"Accept": "application/json"})
        assert response.status_code == 200
        data = response.json()
        assert "service" in data
        assert "endpoints" in data
        assert "/macro-pulse/preview/{country_code}" in data["endpoints"]
    
    def test_root_html_response(self, client):
        """Root endpoint should return HTML for browser requests."""
        response = client.get("/", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "<!DOCTYPE html>" in response.text


class TestRecessionRiskComputation:
    """Tests for recession risk computation logic."""
    
    def test_compute_recession_risk_low(self):
        """Low risk scenario: good GDP, low unemployment, moderate inflation."""
        from app import _compute_recession_risk
        indicators = {
            "gdp_growth": {"latest_value": 3.0, "trend_momentum": 0.5},
            "unemployment": {"latest_value": 4.0, "trend_momentum": -0.2},
            "inflation": {"latest_value": 2.5, "trend_momentum": -0.3},
        }
        score, level, interp = _compute_recession_risk(indicators)
        assert level == "low"
        assert score < 0.2
    
    def test_compute_recession_risk_high(self):
        """High risk scenario: negative GDP, rising unemployment."""
        from app import _compute_recession_risk
        indicators = {
            "gdp_growth": {"latest_value": -1.5, "trend_momentum": -2.0},
            "unemployment": {"latest_value": 9.0, "trend_momentum": 2.0},
            "inflation": {"latest_value": 8.0, "trend_momentum": 0.5},
        }
        score, level, interp = _compute_recession_risk(indicators)
        assert level in ["high", "elevated"]
        assert score >= 0.35
    
    def test_compute_recession_risk_insufficient_data(self):
        """Should handle missing data gracefully."""
        from app import _compute_recession_risk
        indicators = {}
        score, level, interp = _compute_recession_risk(indicators)
        assert level == "insufficient_data"
        assert score is None


class TestMomentumComputation:
    """Tests for momentum score computation."""
    
    def test_compute_momentum_positive(self):
        """Positive momentum when latest > prior average."""
        from app import compute_momentum
        series = [
            {"value": 5.0},
            {"value": 3.0},
            {"value": 2.5},
            {"value": 2.0},
        ]
        momentum = compute_momentum(series)
        assert momentum > 0
    
    def test_compute_momentum_negative(self):
        """Negative momentum when latest < prior average."""
        from app import compute_momentum
        series = [
            {"value": 1.0},
            {"value": 3.0},
            {"value": 4.0},
            {"value": 5.0},
        ]
        momentum = compute_momentum(series)
        assert momentum < 0
    
    def test_compute_momentum_insufficient_data(self):
        """Should return None with insufficient data."""
        from app import compute_momentum
        series = [{"value": 5.0}]
        momentum = compute_momentum(series)
        assert momentum is None


class TestInputValidation:
    """Tests for input validation."""
    
    def test_invalid_country_code_too_long(self, client):
        """Should reject country codes that are too long."""
        response = client.get("/macro-pulse/preview/TOOLONG")
        assert response.status_code == 400
    
    def test_invalid_country_code_numbers(self, client):
        """Should reject country codes with numbers."""
        response = client.get("/macro-pulse/preview/US1")
        assert response.status_code == 400


class TestStarterKit:
    """Tests for the starter kit template."""
    
    def test_starter_kit_readme_exists(self):
        """Starter kit should have README."""
        import os
        assert os.path.exists("/workspace/starter-kit/README.md")
    
    def test_starter_kit_app_exists(self):
        """Starter kit should have app.py."""
        import os
        assert os.path.exists("/workspace/starter-kit/app.py")
    
    def test_starter_kit_requirements_exists(self):
        """Starter kit should have requirements.txt."""
        import os
        assert os.path.exists("/workspace/starter-kit/requirements.txt")
    
    def test_starter_kit_env_example_exists(self):
        """Starter kit should have .env.example."""
        import os
        assert os.path.exists("/workspace/starter-kit/.env.example")
    
    def test_starter_kit_no_secrets(self):
        """Starter kit files should not contain real secrets."""
        import os
        for filename in ["app.py", "README.md", ".env.example"]:
            filepath = f"/workspace/starter-kit/{filename}"
            with open(filepath) as f:
                content = f.read()
                # Check for patterns that look like real secrets
                assert "sk_live" not in content
                assert "sk_test" not in content
                assert "whsec_" not in content
                # Check it uses placeholders
                if filename == ".env.example":
                    assert "YOUR_" in content or "your_" in content
