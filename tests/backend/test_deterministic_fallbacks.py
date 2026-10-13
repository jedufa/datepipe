from __future__ import annotations

import server


def test_llm_provider_fallback_order_is_configurable(monkeypatch):
  monkeypatch.setenv("DATEPIPE_LLM_FALLBACKS", "openai,gemini")
  monkeypatch.delenv("DATEPIPE_LLM_PROVIDER", raising=False)
  monkeypatch.delenv("DATEPIPE_AI_PROVIDER", raising=False)

  assert server.llm_provider_order("backend") == ["openai", "gemini"]

  monkeypatch.setenv("DATEPIPE_BACKEND_LLM_PROVIDER", "gemini")
  assert server.llm_provider_order("backend") == ["gemini"]


def test_deterministic_local_options_have_mobile_contract_shape(isolated_runtime):
  state = server.default_state()
  state["profile"] = {"homeBase": "Vienna", "budgetComfort": "casual", "freeTime": "an evening"}

  options = server.generate_local_options(state, mood="art", search_date="2026-06-12")

  assert options
  assert {"title", "category", "eventUrl", "visibleHint", "dateChoice", "dateLabel"} <= set(options[0])
