from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import server


@dataclass(frozen=True)
class SyntheticCoupleExpectation:
  min_option_count: int
  min_quality_score: int
  required_top_category_any_of: tuple[str, ...]
  required_match_reason_any_of: tuple[str, ...]


@dataclass(frozen=True)
class SyntheticCoupleModel:
  id: str
  planner_role: str
  profile: dict
  context: dict
  expectation: SyntheticCoupleExpectation


@dataclass(frozen=True)
class SyntheticUserFixture:
  test_date: str
  test_cities: tuple[str, ...]
  models: tuple[SyntheticCoupleModel, ...]


def release_synthetic_fixture() -> SyntheticUserFixture:
  fixture = json.loads(Path("tests/fixtures/synthetic_user_models.json").read_text(encoding="utf-8"))
  models = []
  for raw in fixture["models"]:
    expectation = raw["expectation"]
    models.append(SyntheticCoupleModel(
      id=raw["id"],
      planner_role=raw["plannerRole"],
      profile=raw["profile"],
      context=raw["context"],
      expectation=SyntheticCoupleExpectation(
        min_option_count=int(expectation["minOptionCount"]),
        min_quality_score=int(expectation["minQualityScore"]),
        required_top_category_any_of=tuple(expectation["requiredTopCategoryAnyOf"]),
        required_match_reason_any_of=tuple(expectation["requiredMatchReasonAnyOf"]),
      ),
    ))
  return SyntheticUserFixture(
    test_date=str(fixture["testDate"]),
    test_cities=tuple(fixture["testCities"]),
    models=tuple(models),
  )


def release_synthetic_couples() -> tuple[SyntheticCoupleModel, ...]:
  return release_synthetic_fixture().models


def _option_text(option: dict) -> str:
  values = [
    option.get("title", ""),
    option.get("why", ""),
    option.get("visibleHint", ""),
    " ".join(option.get("matchReasons", []) if isinstance(option.get("matchReasons"), list) else []),
  ]
  return " ".join(str(value) for value in values).lower()


def test_synthetic_couple_models_have_clear_release_expectations():
  fixture = release_synthetic_fixture()
  models = fixture.models

  assert len(models) == 10
  assert sum(1 for model in models if model.planner_role == "husband") == 8
  assert sum(1 for model in models if model.planner_role == "wife") == 2
  assert fixture.test_cities == ("Vienna", "Graz", "Linz", "Salzburg", "Innsbruck")
  assert len({model.id for model in models}) == len(models)
  assert all(model.context.get("locale") for model in models)
  assert all(model.context.get("background") for model in models)
  assert all(model.context.get("relationshipStage") for model in models)
  assert all(model.context.get("constraint") for model in models)
  assert all(len(model.profile.get("likes", [])) >= 5 for model in models)
  assert all(len(model.profile.get("avoids", [])) >= 4 for model in models)
  assert all(len(model.profile.get("comfort", [])) >= 4 for model in models)
  assert all(len(model.profile.get("stretch", [])) >= 4 for model in models)
  assert all(len(model.profile.get("searchKeywords", [])) >= 5 for model in models)
  assert all(model.profile.get("budget") for model in models)
  assert all(model.profile.get("budgetComfort") for model in models)
  assert all(model.profile.get("freeTime") for model in models)
  assert all(model.profile.get("preferredDuration") for model in models)
  assert all(model.profile.get("energy") for model in models)
  assert all(isinstance(model.profile.get("radiusKm"), int) for model in models)
  assert all(model.profile.get("privacyDefault") for model in models)
  assert all(model.profile.get("surpriseStyle") for model in models)
  assert all(model.profile.get("bookingStyle") for model in models)
  assert all(model.profile.get("dateGoal") for model in models)
  assert all(model.profile.get("fallbackNeed") for model in models)
  assert all(model.profile.get("partnerSensitivity") for model in models)
  assert all(model.expectation.min_quality_score >= 80 for model in models)


def test_synthetic_couples_get_relevant_source_grounded_options(monkeypatch, isolated_runtime):
  monkeypatch.setattr(
    server,
    "llm_generate_text",
    lambda *args, **kwargs: {"ok": False, "provider": "", "model": "", "text": "", "status": "synthetic-fallback"},
  )

  fixture = release_synthetic_fixture()
  evaluated_cases = 0
  for model in fixture.models:
    for city in fixture.test_cities:
      profile = dict(model.profile)
      profile["homeBase"] = city
      options, source, digest = server.generate_backend_personalized_options({
        "city": city,
        "date": fixture.test_date,
        "profile": profile,
      })
      report = server.option_quality_report(options, city=city, target_date=fixture.test_date, source=source, digest=digest)
      top_options = options[:5]
      top_text = " ".join(_option_text(option) for option in top_options)
      leaked_context_terms = [
        term
        for term in (
          "cross-cultural",
          "ethnicity",
          "background",
          "german-speaking",
          "budget-conscious",
          "dual-career",
          "local-expat",
          "expat household",
          "family household",
        )
        if term in top_text
      ]

      evaluated_cases += 1
      case_id = f"{model.id}:{city}"
      assert source == "backend-city-digest:synthetic-fallback", case_id
      assert len(options) >= model.expectation.min_option_count, case_id
      assert report["score"] >= model.expectation.min_quality_score, case_id
      assert any(option.get("eventUrl") for option in top_options), case_id
      assert any(option.get("category") in model.expectation.required_top_category_any_of for option in top_options), case_id
      assert any(term in top_text for term in model.expectation.required_match_reason_any_of), case_id
      assert leaked_context_terms == [], case_id

  assert evaluated_cases == 50
