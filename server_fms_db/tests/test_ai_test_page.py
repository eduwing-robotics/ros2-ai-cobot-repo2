from fastapi.testclient import TestClient
from api_server.main import app
def test_ai_test_page_and_assets_are_served():
    with TestClient(app) as client:
        page = client.get("/ai/test")
        css = client.get("/static/ai_test.css")
    assert page.status_code == 200
    assert "명령 해석 테스트" in page.text and "TTS 미리듣기" in page.text and "읽을 안내 문장" in page.text
    assert css.status_code == 200
