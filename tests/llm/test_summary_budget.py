"""Tests for summary budget curve and band boundaries."""

import unittest

from video_transcript_api.llm.core.summary_budget import (
    classify_original_length_band,
    compute_summary_budget,
    resolve_summary_max_tokens,
    should_send_max_tokens,
    SummaryBudgetConfig,
)


class TestSummaryBudgetBands(unittest.TestCase):
  def test_s_band_midpoint(self):
    budget = compute_summary_budget(4000)
    self.assertEqual(budget.target_min, 500)
    self.assertEqual(budget.target_max, 3000)
    self.assertEqual(budget.hard_cap, 4500)
    self.assertEqual(budget.max_tokens, 6750)

  def test_m_band_midpoint(self):
    budget = compute_summary_budget(15000)
    self.assertEqual(budget.target_min, 2000)
    self.assertEqual(budget.target_max, 4000)
    self.assertEqual(budget.hard_cap, 5000)
    self.assertEqual(budget.max_tokens, 7500)

  def test_l_band(self):
    budget = compute_summary_budget(50000)
    self.assertEqual(budget.target_min, 4000)
    self.assertEqual(budget.target_max, 6000)
    self.assertEqual(budget.hard_cap, 8000)
    self.assertEqual(budget.max_tokens, 12000)

  def test_boundary_799_vs_800(self):
    low = compute_summary_budget(799)
    high = compute_summary_budget(800)
    self.assertEqual(low.hard_cap, 1598)
    self.assertEqual(high.hard_cap, 1600)
    self.assertEqual(classify_original_length_band(799), "below_S")
    self.assertEqual(classify_original_length_band(800), "S")

  def test_s_band_low_length_clamps_target_max_to_hard_cap(self):
    budget = compute_summary_budget(1000)
    self.assertEqual(budget.hard_cap, 2000)
    self.assertEqual(budget.target_max, 2000)
    self.assertEqual(budget.target_min, 500)

  def test_boundary_7999_vs_8000(self):
    s_band = compute_summary_budget(7999)
    m_band = compute_summary_budget(8000)
    self.assertEqual(s_band.hard_cap, 4500)
    self.assertEqual(m_band.hard_cap, 5000)
    self.assertEqual(classify_original_length_band(7999), "S")
    self.assertEqual(classify_original_length_band(8000), "M")

  def test_boundary_29999_vs_30000(self):
    m_band = compute_summary_budget(29999)
    l_band = compute_summary_budget(30000)
    self.assertEqual(m_band.hard_cap, 5000)
    self.assertEqual(l_band.hard_cap, 8000)
    self.assertEqual(classify_original_length_band(29999), "M")
    self.assertEqual(classify_original_length_band(30000), "L")

  def test_config_override(self):
    cfg = SummaryBudgetConfig(s_hard_cap_max=4000, max_tokens_multiplier=2.0)
    budget = compute_summary_budget(2000, cfg)
    self.assertEqual(budget.hard_cap, 4000)
    self.assertEqual(budget.max_tokens, 8000)


class TestSummaryMaxTokensPolicy(unittest.TestCase):
  def test_should_send_max_tokens_only_for_disabled(self):
    self.assertTrue(should_send_max_tokens("disabled"))
    self.assertFalse(should_send_max_tokens(None))
    self.assertFalse(should_send_max_tokens("high"))
    self.assertFalse(should_send_max_tokens("low"))

  def test_resolve_summary_max_tokens(self):
    budget = compute_summary_budget(4000)
    self.assertIsNone(resolve_summary_max_tokens(budget, None))
    self.assertIsNone(resolve_summary_max_tokens(budget, "high"))
    self.assertEqual(resolve_summary_max_tokens(budget, "disabled"), 6750)


if __name__ == "__main__":
  unittest.main()
