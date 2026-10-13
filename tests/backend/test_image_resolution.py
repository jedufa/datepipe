from __future__ import annotations

import server


def test_choose_page_image_prefers_og_and_twitter_metadata(fixture_text):
  og = server.choose_page_image("https://example.test/events", fixture_text("sample_og_image_page.html"))
  twitter = server.choose_page_image("https://example.test/events", fixture_text("sample_twitter_image_page.html"))

  assert og == "https://cdn.example.test/photos/courtyard.jpg"
  assert twitter == "https://example.test/images/event-stage.webp"


def test_choose_page_image_skips_bad_and_uses_large_img_candidate():
  html = """
  <html><body>
    <img src="/logo.png" width="40" height="40">
    <img src="/photos/big-event.jpg" width="1200" height="800">
  </body></html>
  """

  assert server.choose_page_image("https://example.test/page", html) == "https://example.test/photos/big-event.jpg"


def test_resolve_page_image_uses_cache_and_handles_broken_page(monkeypatch, isolated_runtime, fixture_text):
  calls = {"count": 0}

  def fake_fetch(url: str, timeout: float = 4.0) -> str:
    calls["count"] += 1
    return fixture_text("sample_og_image_page.html")

  monkeypatch.setattr(server, "resolve_page_image", server._pytest_original_resolve_page_image)
  monkeypatch.setattr(server, "fetch_text", fake_fetch)

  first = server.resolve_page_image("https://example.test/event")
  second = server.resolve_page_image("https://example.test/event")

  assert first.endswith("courtyard.jpg")
  assert second == first
  assert calls["count"] == 1

  monkeypatch.setattr(server, "fetch_text", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
  assert server.resolve_page_image("https://example.test/broken") == ""
