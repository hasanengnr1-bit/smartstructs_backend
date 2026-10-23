from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_read_root():
    """Test that the root endpoint returns a 200 status code"""
    response = client.get("/health")
    assert response.status_code == 200